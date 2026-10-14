"""Telegram Bot API alerts (standard library only).

Configuration (environment, never hard-coded or logged):
    TELEGRAM_BOT_TOKEN   the bot token from @BotFather
    TELEGRAM_CHAT_ID     the chat / channel id to post to

Rate limits (Telegram Bot FAQ): about one message per second per chat; a 429 response
carries ``parameters.retry_after`` seconds, which is honoured. Messages are HTML-escaped.
A signal whose entry window has already passed is never sent late.
"""
from __future__ import annotations

import html
import json
import os
import time as systime
import urllib.error
import urllib.request
from datetime import datetime

from strategy_930 import IST

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, transport=None,
                 sleep=systime.sleep, clock=systime.monotonic, min_interval: float = 1.1,
                 max_retries: int = 3, timeout: float = 10.0):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.transport = transport or self._post
        self.sleep, self.clock = sleep, clock
        self.min_interval, self.max_retries, self.timeout = min_interval, max_retries, timeout
        self._last_send = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def _post(self, url: str, body: bytes, timeout: float) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {"ok": False, "description": str(e)}

    def send(self, text: str) -> tuple[bool, str, int | None]:
        """(ok, info, message_id). Honors one message per ~1.1 s and 429 retry_after."""
        if not self.enabled:
            return False, "telegram_not_configured", None
        url = API.format(token=self.token, method="sendMessage")
        body = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode("utf-8")
        err = "unknown"
        for attempt in range(self.max_retries + 1):
            if self._last_send is not None:
                wait = self.min_interval - (self.clock() - self._last_send)
                if wait > 0:
                    self.sleep(wait)
            try:
                status, payload = self.transport(url, body, self.timeout)
            except Exception as exc:                       # network failure: back off and retry
                err = self._redact(f"network:{type(exc).__name__}:{exc}")
                self.sleep(min(2 ** attempt, 8))
                continue
            finally:
                self._last_send = self.clock()
            if status == 200 and payload.get("ok"):
                return True, "sent", (payload.get("result") or {}).get("message_id")
            desc = self._redact(str(payload.get("description", "")))
            if status == 429:
                retry = (payload.get("parameters") or {}).get("retry_after", 5)
                err = f"429_retry_after_{retry}"
                self.sleep(min(float(retry), 60.0))
                continue
            if 500 <= status < 600:
                err = f"http_{status}:{desc}"
                self.sleep(min(2 ** attempt, 8))
                continue
            return False, f"http_{status}:{desc}", None    # 4xx: not retryable (bad token/chat/text)
        return False, err, None


def _p(x: float) -> str:
    return f"₹{x:,.2f}"


def format_signal(rec: dict) -> str:
    """HTML alert for one ledger record (see ledger.SignalLedger.record)."""
    e = html.escape
    long = rec["direction"] == "LONG"
    st = datetime.fromisoformat(rec["signal_time"]).astimezone(IST)
    bt = datetime.fromisoformat(rec["bar_time"]).astimezone(IST)
    vu = datetime.fromisoformat(rec["valid_until"]).astimezone(IST)
    comps = sorted(rec["components"], key=lambda c: -c["weight"] * c["quality"])
    lines = [
        f"{'🟢' if long else '🔴'} <b>{e(rec['direction'])} {e(rec['symbol'])}</b> — score <b>{rec['score']:.1f}</b>/100",
        f"<b>Engine:</b> {e(rec['engine'])}",
        f"<b>Strategy:</b> {e(rec['strategy'])}",
        f"<b>Entry:</b> {'buy' if long else 'sell'}-stop {'above' if long else 'below'} {_p(rec['entry'])}"
        f" (cancel if not triggered by {vu:%H:%M} IST)",
        f"<b>Stop-loss:</b> {_p(rec['stop'])} | <b>Target:</b> {_p(rec['target'])} ({rec['reward_r']:.1f}R)",
        f"<b>Risk:</b> {rec['risk_pct']:.2f}% of entry | costs ≈ {rec['cost_r']:.2f}R",
        f"<b>Signal time:</b> {st:%Y-%m-%d %H:%M} IST (decision candle {bt:%H:%M})",
        "<b>Why:</b>",
    ]
    lines += [f"• {e(c)}" for c in rec["conditions"]]
    lines.append("<b>Score drivers:</b> " + e(", ".join(f"{c['name']} {c['quality']:.2f}" for c in comps[:4])))
    lines.append("<i>Deterministic research signal. Score 90+ = all strategy conditions met at high quality; "
                 "it is not a probability of profit.</i>")
    return "\n".join(lines)


def format_feed_problem(now: datetime, detail: str) -> str:
    return (f"⚠️ <b>DATA FEED PROBLEM</b> {now.astimezone(IST):%H:%M} IST\n{html.escape(detail)}\n"
            "No signals can be generated from this data. Silence does NOT mean no opportunity.")


def format_daily_summary(day: str, summary: dict, scans: int, feed_failures: int) -> str:
    lines = [f"📋 <b>Strategy engines — {html.escape(day)}</b>", f"scans {scans} | feed failures {feed_failures}"]
    if not summary:
        lines.append("No 90+ signals today.")
    for eng, s in sorted(summary.items()):
        states = ", ".join(f"{k} {v}" for k, v in sorted(s["states"].items()))
        r = s["r"]
        rtxt = f" | closed {len(r)}, sum {sum(r):+.2f}R (net of modelled costs)" if r else ""
        lines.append(f"• {html.escape(eng)}: {s['signals']} signal(s): {html.escape(states)}{rtxt}")
    return "\n".join(lines)
