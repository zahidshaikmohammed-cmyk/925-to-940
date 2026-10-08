"""Local browser dashboard for a running LiveSession (stdlib only).

Security model (it can send real square-off and arm commands):
  * binds 127.0.0.1 by default; anything else needs --allow-remote
  * Host header must be the bound host:port (defeats DNS rebinding)
  * every POST needs: same-origin Origin header, JSON body, and the per-session control
    token in X-Control-Token (constant-time compare). The token is embedded in the page
    only in localhost mode; in remote mode it must be typed (it is written to
    <state-dir>/dashboard_token.txt with 0600 permissions)
  * no CORS headers; strict Content-Security-Policy; at most 10 control requests/minute
  * GET endpoints are read-only; controls are queued to the engine thread, which is the
    only thread that ever touches trading state, and every control is written to the
    hash-chained audit log
"""
from __future__ import annotations

import hmac
import json
import threading
import time as _time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).with_name("static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
ACTIONS = {"ARM", "DISARM", "PAUSE", "RESUME", "SQUAREOFF"}


class DashboardServer:
    def __init__(self, session, host: str, port: int, token: str, embed_token: bool = True,
                 max_controls_per_min: int = 10):
        self.session = session
        self.host, self.token, self.embed_token = host, token, embed_token
        self.max_controls = max_controls_per_min
        self._control_times: list[float] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(_Handler):
            server_ref = outer

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"{host}:{self.port}"} | ({f"localhost:{self.port}", f"127.0.0.1:{self.port}"}
                                                        if host in ("127.0.0.1", "localhost") else set())
        self.url = f"http://{'127.0.0.1' if host in ('127.0.0.1', 'localhost') else host}:{self.port}/"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="dashboard-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def allow_control(self) -> bool:
        with self._lock:
            now = _time.monotonic()
            self._control_times = [t for t in self._control_times if now - t < 60]
            if len(self._control_times) >= self.max_controls:
                return False
            self._control_times.append(now)
            return True


class _Handler(BaseHTTPRequestHandler):
    server_ref: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):      # keep the engine's stdout clean
        return

    # ---------------------------------------------------------- helpers
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server_ref.allowed_hosts

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    # ---------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "bad Host header"})
        path = self.path.split("?", 1)[0]
        q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) if "?" in self.path else {}
        s = self.server_ref
        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text()
            tok = s.token if s.embed_token else ""
            html = html.replace("__CONTROL_TOKEN__", tok)
            return self._send(200, html.encode(), CONTENT_TYPES[".html"])
        if path in ("/app.js", "/style.css"):
            f = STATIC / path.lstrip("/")
            return self._send(200, f.read_bytes(), CONTENT_TYPES[f.suffix])
        if path == "/api/state":
            return self._send(200, s.session.snapshot_json.encode(), "application/json")
        if path == "/api/stream":
            return self._stream()
        if path == "/api/audit":
            return self._audit(int(q.get("offset", 0)), min(500, int(q.get("limit", 200))), q.get("kind"))
        if path == "/api/audit/verify":
            from ..audit import verify
            p = self._audit_path()
            if p is None or not p.exists():
                return self._json(200, {"ok": True, "records": 0})
            ok, n = verify(p)
            return self._json(200, {"ok": ok, "records": n})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/healthz":
            age = _time.monotonic() - getattr(s.session, "heartbeat", 0)
            return self._json(200, {"engine_heartbeat_age_s": round(age, 2), "seq": s.session.snapshot_seq})
        return self._json(404, {"error": "not found"})

    def _audit_path(self) -> Path | None:
        r = getattr(self.server_ref.session, "runner", None)
        return Path(r.audit.path) if r is not None else None

    def _audit(self, offset: int, limit: int, kind: str | None) -> None:
        p = self._audit_path()
        if p is None or not p.exists():
            return self._json(200, {"total": 0, "records": []})
        lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
        recs = []
        for i, ln in enumerate(lines):
            try:
                o = json.loads(ln)
            except ValueError:
                o = {"kind": "UNPARSEABLE", "raw": ln[:200]}
            o["n"] = i
            if kind and o.get("kind") != kind:
                continue
            recs.append(o)
        total = len(recs)
        # newest first; offset counts back from the newest record
        page = list(reversed(recs))[offset:offset + limit]
        return self._json(200, {"total": total, "records": page})

    def _stream(self) -> None:
        sess = self.server_ref.session
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = -1
        try:
            while True:
                sess = self.server_ref.session          # may switch from BootHolder to the live session
                with sess.snapshot_cond:
                    if sess.snapshot_seq == last:
                        sess.snapshot_cond.wait(timeout=2.0)
                    seq, body = sess.snapshot_seq, sess.snapshot_json
                if seq != last:
                    last = seq
                    self.wfile.write(b"data: " + body.encode() + b"\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---------------------------------------------------------- POST (controls)
    def do_POST(self):
        s = self.server_ref
        # always drain the body first (bounded), so a rejected request never leaves bytes on a
        # keep-alive connection to be misparsed as the next request; rejections close the socket
        try:
            n = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        self.close_connection = True
        if not self._host_ok():
            return self._json(HTTPStatus.MISDIRECTED_REQUEST, {"ok": False, "message": "bad Host header"})
        origin = self.headers.get("Origin", "")
        if origin.split("://", 1)[-1] not in s.allowed_hosts:
            return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "cross-origin request refused"})
        if not hmac.compare_digest(self.headers.get("X-Control-Token", ""), s.token):
            return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "missing or wrong control token"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "message": "JSON body required"})
        if self.path != "/api/control":
            return self._json(404, {"ok": False, "message": "not found"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "message": "bad JSON"})
        action = str(body.get("action", "")).upper()
        if action not in ACTIONS:
            return self._json(400, {"ok": False, "message": f"unknown action {action}"})
        if not s.allow_control():
            return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "message": "too many control requests"})
        res = s.session.submit(action, str(body.get("confirm", ""))[:100], self.client_address[0])
        return self._json(200 if res.get("ok") else 409, res)
