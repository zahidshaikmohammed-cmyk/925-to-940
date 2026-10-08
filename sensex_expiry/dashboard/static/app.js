"use strict";
// SENSEX Expiry Engine dashboard. Read-only view of the engine snapshot stream plus four
// explicit controls. All rendering uses textContent (audit payloads are untrusted text).

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
let S = null;                 // latest snapshot
let lastMsgAt = 0;            // ms timestamp of the last snapshot received
let es = null;
let pollTimer = null;
let auditOffset = 0;
let auditRecords = [];
let auditTotal = 0;

// ---------------------------------------------------------------- utils
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v; else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) if (k !== null && k !== undefined) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
function svg(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
function num(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function rs(v) { return v === null || v === undefined ? "—" : (v < 0 ? "−₹" + num(-v, 2) : "₹" + num(v, 2)); }
function rr(v) { return v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + num(v, 2) + "R"; }
function hhmmss(iso) { if (!iso) return "—"; const m = String(iso).match(/T(\d\d:\d\d:\d\d)/); return m ? m[1] : String(iso); }
function age(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${Math.max(0, Math.round(ms))} ms` : `${(ms / 1000).toFixed(1)} s`;
}
function signed(v, text) {
  const s = el("span", { class: v > 0 ? "sign-pos" : v < 0 ? "sign-neg" : "" });
  s.textContent = text;
  return s;
}
function kv(target, rows) {
  const dl = $(target);
  dl.replaceChildren();
  for (const [k, v] of rows) {
    dl.append(el("dt", { text: k }));
    const dd = el("dd");
    if (v instanceof Node) dd.append(v); else dd.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
    dl.append(dd);
  }
}
function badge(text, level, title) {
  return el("span", { class: `badge ${level || ""}`, title: title || null }, el("span", { class: "dot", "aria-hidden": "true" }), text);
}
function storageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
function storageSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } }

// ---------------------------------------------------------------- stream
function connect() {
  if (es) es.close();
  try {
    es = new EventSource("/api/stream");
    es.onmessage = (ev) => { try { onSnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore a torn frame */ } };
    es.onerror = () => { startPolling(); };
  } catch (e) { startPolling(); }
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (r.ok) onSnapshot(await r.json());
      if (es && es.readyState === EventSource.OPEN) { clearInterval(pollTimer); pollTimer = null; }
    } catch (e) { /* engine unreachable: the stale banner covers it */ }
  }, 2000);
}
function onSnapshot(s) {
  S = s;
  lastMsgAt = Date.now();
  render();
}
setInterval(() => renderBanners(), 1000);

// ---------------------------------------------------------------- render
function render() {
  if (!S) return;
  const m = S.meta || {};
  $("meta-line").textContent = `v${m.engine_version || "?"} · config ${m.config_hash || "?"} · ${m.day || ""}` +
    (m.expiry ? ` · expiry ${m.expiry}` : "") + (m.now ? ` · engine time ${hhmmss(m.now)}` : "");
  renderBadges(); renderBanners(); renderControls();
  if (S.boot) return;
  renderTiles(); renderChart(); renderPosition(); renderDecision(); renderRisk(); renderData(); renderOrders();
  renderReasons(); renderTrades(); renderControlLog();
}

function renderBadges() {
  const b = $("badges");
  b.replaceChildren();
  if (!S) return;
  const m = S.meta || {}, c = S.control || {};
  b.append(badge(m.mode === "LIVE" ? "LIVE MODE" : "PAPER MODE", m.mode === "LIVE" ? "serious" : "", "Broker: " + (m.broker || "?")));
  if (S.boot) { b.append(badge("ENGINE NOT RUNNING", "critical")); return; }
  if (m.feed === "REPLAY") b.append(badge(`REPLAY ×${m.replay_speed}`, "warning", "Recorded data, not the live market"));
  b.append(c.armed ? badge(m.mode === "LIVE" ? "ARMED · REAL ORDERS" : "ARMED · PAPER ORDERS", m.mode === "LIVE" ? "critical" : "warning")
                   : badge("DISARMED · NO NEW ENTRIES", ""));
  if (c.paused) b.append(badge("ENTRIES PAUSED", "warning"));
  if (c.kill && c.kill.tripped) b.append(badge("KILL SWITCH: " + c.kill.reason, "critical"));
  const conn = S.connection || {};
  b.append(badge(conn.connected ? `${conn.kind} FEED UP` : `${conn.kind || "FEED"} DOWN`, conn.connected ? "good" : "critical"));
  const q = (S.data || {}).quality;
  b.append(badge("DATA " + (q || "—"), q === "GOOD" ? "good" : q ? "critical" : ""));
  b.append(badge(m.is_expiry ? "EXPIRY DAY" : "NOT AN EXPIRY DAY", m.is_expiry ? "good" : "warning"));
}

function renderBanners() {
  const box = $("banners");
  box.replaceChildren();
  const add = (text, warn) => box.append(el("div", { class: "banner" + (warn ? " warn" : "") },
    el("span", { class: "icon", "aria-hidden": "true", text: warn ? "⚠" : "⛔" }), el("span", { text })));
  if (!lastMsgAt) { add("Waiting for the engine…", true); return; }
  const staleS = (Date.now() - lastMsgAt) / 1000;
  if (staleS > 3.5) add(`Dashboard has had no update from the engine for ${staleS.toFixed(0)} s. This view is STALE. ` +
    "An armed engine keeps trading on its own; use EMERGENCY SQUARE-OFF only if the engine itself is unreachable.");
  if (!S) return;
  if (S.boot) { add(`Engine not running: ${S.boot.error}` + (S.boot.retry_in_s ? ` (retrying every ${S.boot.retry_in_s} s)` : "")); return; }
  const c = S.control || {}, m = S.meta || {};
  if (m.engine_error) add("Engine exception, kill switch tripped. Last error: " + m.engine_error.split("\n").slice(-2).join(" "));
  if (c.kill && c.kill.tripped) add(`Kill switch tripped (${c.kill.reason}). New entries are blocked and positions are squared off. ` +
    `To reset after review, delete ${c.kill.lock_file} and restart the engine.`);
  if (m.mode === "LIVE" && c.gate && !c.gate.allowed) add("Live validation gate NOT passed: ARM is unavailable. " +
    "The engine is observing only. (" + c.gate.failures.length + " failing criteria; see ARM.)", true);
}

function renderControls() {
  const c = (S && S.control) || {};
  const running = S && !S.boot;
  $("btn-arm").disabled = !running || !!c.armed;
  $("btn-arm").title = running && c.arm_blockers && c.arm_blockers.length ? "Blocked: " + c.arm_blockers.join("; ") : "";
  $("btn-disarm").disabled = !running || !c.armed;
  const p = $("btn-pause");
  p.disabled = !running;
  p.textContent = c.paused ? "RESUME ENTRIES" : "PAUSE NEW ENTRIES";
  p.classList.toggle("active", !!c.paused);
  $("btn-squareoff").disabled = !running;
}

function renderTiles() {
  const mk = S.market || {}, d = S.data || {}, e = S.engine || {}, p = S.pnl || {};
  $("spot").textContent = mk.spot ? num(mk.spot, 2) : "—";
  $("spot-sub").textContent = `last tick ${hhmmss(mk.spot_ts)} · age ${age(d.index_tick_age_ms)}`;
  $("engine-state").textContent = e.state || "—";
  const t = (e.transitions || []).slice(-1)[0];
  $("engine-sub").textContent = t ? `since ${hhmmss(t.ts)}${t.why ? " · " + t.why : ""}` : "no transitions yet";
  $("regime").textContent = mk.regime || "—";
  const lastCodes = ((S.reasons || {}).last || []).join(", ");
  $("regime-sub").textContent = mk.atr ? `ATR(14, 1m) ${num(mk.atr, 1)} pts`
    : (mk.bars || []).length < 20 ? "regime needs 20 closed minutes" : `not evaluated on the last bar (${lastCodes || "no decision"})`;
  const pnl = $("pnl");
  pnl.replaceChildren(signed(p.total_rs || 0, rs(p.total_rs)));
  $("pnl-sub").textContent = `realised ${rs(p.realized_rs)} (${rr(p.realized_r)}) · open ${rs(p.unrealized_rs)} · ${p.trades || 0} trade(s)`;
}

// ---------------------------------------------------------------- chart
function renderChart() {
  const box = $("chart");
  const mk = S.market || {};
  const bars = mk.bars || [];
  box.replaceChildren();
  const tbl = $("chart-table");
  tbl.replaceChildren();
  if (bars.length < 2) { box.append(el("p", { class: "muted", text: "Waiting for closed 1-minute candles…" })); return; }
  const W = Math.max(320, box.clientWidth), H = box.clientHeight || 300;
  const M = { l: 58, r: 118, t: 10, b: 24 };
  const toMin = (hm) => { const [h, m] = hm.split(":").map(Number); return (h - 9) * 60 + m - 15; };
  const closes = bars.map((b) => b[1]);
  let lo = Math.min(...closes), hi = Math.max(...closes);
  const span = Math.max(hi - lo, 20);
  const lines = [];
  for (const [name, v] of Object.entries(mk.levels || {})) if (v >= lo - span * 0.6 && v <= hi + span * 0.6) lines.push({ name, v, cls: "" });
  const pos = S.position, lb = S.last_buy;
  if (pos && pos.invalidation) lines.push({ name: "Invalidation", v: pos.invalidation, cls: "inval" });
  if (pos && lb && lb.trigger) lines.push({ name: "Trigger", v: lb.trigger, cls: "trigger" });
  for (const l of lines) { lo = Math.min(lo, l.v); hi = Math.max(hi, l.v); }
  const pad = (hi - lo) * 0.06 || 10;
  lo -= pad; hi += pad;
  const x = (i) => M.l + (i / 375) * (W - M.l - M.r);
  const y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
  const step = niceStep((hi - lo) / 4);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    grid.append(svg("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) }));
    const t = svg("text", { x: M.l - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = num(v, 0); axis.append(t);
  }
  for (const hm of ["09:15", "10:30", "12:00", "13:30", "15:00"]) {
    const t = svg("text", { x: x(toMin(hm)), y: H - 6, "text-anchor": "middle" }); t.textContent = hm; axis.append(t);
  }
  s.append(grid, axis);
  // reference levels, labels de-collided on the right edge
  const placed = [];
  for (const l of lines.sort((a, b) => b.v - a.v)) {
    const g = svg("g", { class: "lvl " + l.cls });
    g.append(svg("line", { x1: M.l, x2: W - M.r, y1: y(l.v), y2: y(l.v) }));
    let ly = y(l.v) + 4;
    for (const p of placed) if (Math.abs(p - ly) < 12) ly = p + 12;
    placed.push(ly);
    const t = svg("text", { x: W - M.r + 6, y: ly }); t.textContent = `${l.name} ${num(l.v, 0)}`;
    g.append(t);
    s.append(g);
  }
  const d = bars.map((b, i) => `${i ? "L" : "M"}${x(toMin(b[0])).toFixed(1)},${y(b[1]).toFixed(1)}`).join("");
  s.append(svg("path", { class: "series", d }));
  // hover layer: crosshair + tooltip
  const cross = svg("line", { class: "cross", y1: M.t, y2: H - M.b, visibility: "hidden" });
  const dot = svg("circle", { class: "cross-dot", r: 4, visibility: "hidden" });
  const hit = svg("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" });
  s.append(cross, dot, hit);
  const tip = $("tooltip");
  hit.addEventListener("mousemove", (ev) => {
    const r = s.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    const minute = ((px - M.l) / (W - M.l - M.r)) * 375;
    let best = 0;
    for (let i = 1; i < bars.length; i++) if (Math.abs(toMin(bars[i][0]) - minute) < Math.abs(toMin(bars[best][0]) - minute)) best = i;
    const bx = x(toMin(bars[best][0])), by = y(bars[best][1]);
    cross.setAttribute("x1", bx); cross.setAttribute("x2", bx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", bx); dot.setAttribute("cy", by); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${bars[best][0]}  close ${num(bars[best][1], 2)}`;
    tip.style.left = ev.clientX + 14 + "px"; tip.style.top = ev.clientY + 14 + "px";
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; });
  box.append(s);
  $("chart-note").textContent = `${bars.length} closed minutes`;
  const t = el("table");
  t.append(el("tr", {}, el("th", { text: "Time" }), el("th", { text: "Close" })));
  for (const b of bars.slice(-60).reverse()) t.append(el("tr", {}, el("td", { text: b[0] }), el("td", { text: num(b[1], 2) })));
  tbl.append(t);
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}
window.addEventListener("resize", () => { if (S && !S.boot) renderChart(); });

// ---------------------------------------------------------------- panels
function renderPosition() {
  const p = S.position;
  const b = $("pos-badge");
  b.replaceChildren(p ? badge(p.pending_exit ? "EXITING" : "OPEN", p.pending_exit ? "warning" : "good") : badge("FLAT", ""));
  if (!p) { kv("position", [["Status", "No open position"], ["Orders", (S.orders.open || []).length + " open at broker"]]); return; }
  kv("position", [
    ["Instrument", p.instrument], ["Setup", `${p.setup} · ${p.direction}`], ["Quantity", p.qty],
    ["Entry", `${num(p.entry)} at ${hhmmss(p.entry_ts)}`], ["Mark (bid)", num(p.mark)], ["LTP", num(p.ltp)],
    ["Premium stop (SL)", `${num(p.stop)}${p.stop !== p.initial_stop ? ` (initial ${num(p.initial_stop)})` : ""}`],
    ["Invalidation (SENSEX close)", num(p.invalidation)],
    ["Target", p.target === null ? `none: ${p.exit_policy} exit` : p.target],
    ["1R", rs(p.one_r)], ["Unrealised", signed(p.upnl_rs, `${rs(p.upnl_rs)} (${rr(p.upnl_r)})`)],
    ["MFE", rr(p.mfe_r)], ["Bars held", p.bars_held], ["Pending exit", p.pending_exit || "none"],
  ]);
}

function renderDecision() {
  const d = S.decision || {};
  kv("decision", [
    ["Bar", hhmmss(d.bar_timestamp)], ["Action", d.action], ["Setup", d.setup ? `${d.setup} · ${d.direction}` : "none"],
    ["Level", d.level ? `${d.level.name} ${num(d.level.price)}` : null],
    ["Trigger / invalidation", d.trigger ? `${num(d.trigger)} / ${num(d.invalidation)}` : null],
    ["Option", d.instrument || null], ["Entry / SL", d.entry ? `${num(d.entry)} / ${num(d.stop_loss)}` : null],
    ["Target", d.action === "BUY" ? (d.target === null ? "none (trail)" : d.target) : null],
    ["Lots / 1R", d.lots ? `${d.lots} · ${rs(d.one_r_rupees)}` : null], ["Score (logged only)", d.score],
  ]);
  const box = $("reason-last");
  box.replaceChildren();
  const pos = new Set(["SWEEP_DETECTED", "LEVEL_RECLAIMED", "STRUCTURE_SHIFT", "DISPLACEMENT", "ORB_ACCEPTANCE", "ORB_RETEST_HELD",
    "COMPRESSION_BREAK", "VOLATILITY_EXPANSION", "LEVEL_CONFLUENCE", "RISK_REWARD_VALID", "REGIME_ALIGNED"]);
  for (const c of (S.reasons || {}).last || []) box.append(el("span", { class: "code" + (pos.has(c) ? "" : " neg"), text: c }));
}

function meter(label, used, max, text, escalate) {
  // only loss meters escalate to warning/critical colours; capacity meters stay neutral
  const frac = max ? Math.min(1, Math.max(0, used / max)) : 0;
  const lvl = !escalate ? "" : frac >= 1 ? "critical" : frac >= 0.5 ? "warning" : "";
  return el("div", { class: "meter" },
    el("div", { class: "meter-top" }, el("span", { text: label }), el("b", { text })),
    el("div", { class: "meter-track", role: "meter", "aria-valuemin": 0, "aria-valuemax": max, "aria-valuenow": used, "aria-label": label },
      Object.assign(el("div", { class: "meter-fill " + lvl }), { style: `width:${(frac * 100).toFixed(1)}%` })));
}
function renderRisk() {
  const r = S.risk || {};
  const lossUsed = Math.max(0, -(r.daily_r || 0));
  const box = $("risk");
  box.replaceChildren(
    meter("Trades used", r.trades_used, r.max_trades, `${r.trades_used} / ${r.max_trades}`, false),
    meter("Daily loss used", lossUsed, r.max_daily_loss_r, `${num(lossUsed)}R / ${num(r.max_daily_loss_r)}R`, true),
    meter("Consecutive losses", r.consecutive_losses, r.max_consecutive, `${r.consecutive_losses} / ${r.max_consecutive}`, true),
    meter("Open positions", r.open_positions, r.max_open, `${r.open_positions} / ${r.max_open}`, false),
  );
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of [["Risk per trade", rs(r.risk_per_trade_rs)], ["Capital", rs(r.capital)],
    ["Kill switch at day loss", rs(r.kill_loss_rs)], ["Cooldown until", r.cooldown_until ? hhmmss(r.cooldown_until) : "—"]]) {
    dl.append(el("dt", { text: k }), el("dd", { text: v }));
  }
  box.append(dl);
}
function renderData() {
  const d = S.data || {}, c = S.connection || {};
  const fresh = (ms, lim) => ms === null || ms === undefined ? "—" : `${age(ms)} ${ms <= lim ? "✓" : "✕ stale"}`;
  kv("data", [
    ["Quality", d.quality ? `${d.quality}${d.quality_reasons && d.quality_reasons.length ? " · " + d.quality_reasons.join(", ") : ""}` : "—"],
    ["SENSEX tick age", fresh(d.index_tick_age_ms, d.max_underlying_age_ms)],
    ["Freshest option quote", fresh(d.option_quote_age_ms, 5000)],
    ["Option contracts quoting", d.option_quotes], ["Closed candles", d.candles],
    ["Missing minutes", d.missing_minutes], ["Late / duplicate / out-of-order", `${d.late_ticks} / ${d.duplicates} / ${d.out_of_order}`],
    ["Quarantined jumps / invalid", `${d.rejected_jumps} / ${d.invalid}`],
    ["Feed", `${c.kind || "?"} · ${c.connected ? "connected" : "DISCONNECTED"}${c.instruments ? ` · ${c.instruments} instruments` : ""}`],
    ["Reconnects", c.reconnects], ["Last disconnect", c.last_disconnect ? `${hhmmss(c.last_disconnect.at)} ${c.last_disconnect.error}` : "none"],
    ["Broker", `${(S.meta || {}).broker} · ${c.broker_ok ? "responding" : "ERROR " + (c.broker_error || "")}`],
  ]);
}
function renderOrders() {
  const o = S.orders || {};
  const le = o.last_event;
  kv("orders", [
    ["Protective stop", o.sl ? `${o.sl.order_id} · ${o.sl.status}` : "none"],
    ["Exit order", o.exit && o.exit.order_id ? `${o.exit.order_id} · attempt ${o.exit.attempts} · ${o.exit.reason || ""}` : "none"],
    ["Last order event", le ? `${hhmmss(le.ts)} ${le.kind} ${le.side} ${le.qty} @ ${num(le.price)} → ${le.status}` : "none"],
  ]);
  const box = $("open-orders");
  box.replaceChildren();
  const rows = o.open || [];
  if (!rows.length) { box.append(el("p", { class: "muted", text: "No open orders at the broker." })); return; }
  const t = el("table");
  t.append(el("tr", {}, ...["Order", "Side", "Type", "Qty", "Price", "Trigger", "Status"].map((h) => el("th", { text: h }))));
  for (const r of rows) t.append(el("tr", {}, ...[r.order_id, r.side, r.type, r.qty, num(r.price), num(r.trigger), r.status].map((v) => el("td", { text: v }))));
  box.append(t);
}
function renderReasons() {
  const counts = Object.entries((S.reasons || {}).counts || {}).slice(0, 12);
  const box = $("reason-counts");
  box.replaceChildren();
  if (!counts.length) { box.append(el("p", { class: "muted", text: "No decisions yet." })); return; }
  const max = Math.max(...counts.map((c) => c[1]));
  for (const [code, n] of counts) {
    box.append(el("div", { class: "row", title: `${code}: ${n}` }, el("span", { class: "lbl", text: code }),
      el("span", { class: "bar-wrap" }, Object.assign(el("span", { class: "bar" }), { style: `width:${Math.max(2, (n / max) * 100)}%` }),
        el("span", { class: "val", text: n }))));
  }
}
function renderTrades() {
  const t = $("trades");
  t.replaceChildren(el("tr", {}, ...["Entry", "Exit", "Setup", "Side", "Contract", "Qty", "Entry ₹", "Exit ₹", "Net ₹", "R net", "MFE", "Exit reason"]
    .map((h) => el("th", { text: h }))));
  const rows = S.trades || [];
  if (!rows.length) { t.append(el("tr", {}, el("td", { colspan: 12, class: "muted", text: "No trades today." }))); return; }
  for (const r of rows) t.append(el("tr", {}, ...[hhmmss(r.entry_ts), hhmmss(r.exit_ts), r.setup, r.direction, `${r.strike} ${r.right}`,
    r.qty, num(r.entry), num(r.exit)].map((v) => el("td", { text: v })),
    el("td", {}, signed(r.net, rs(r.net))), el("td", { text: rr(r.r_net) }), el("td", { text: rr(r.mfe_r) }), el("td", { text: r.reason })));
}
function renderControlLog() {
  const c = $("control-log");
  c.replaceChildren(el("tr", {}, ...["Time", "Control", "Result"].map((h) => el("th", { text: h }))));
  for (const r of ((S.control || {}).log || []).slice().reverse()) {
    c.append(el("tr", {}, el("td", { text: hhmmss(r.ts) }), el("td", { text: r.action }),
      el("td", { class: "wrap", text: (r.result.ok ? "✓ " : "✕ ") + r.result.message + (r.result.blockers ? ": " + r.result.blockers.join("; ") : "") })));
  }
  const t = $("transitions");
  t.replaceChildren(el("tr", {}, ...["Time", "From → to", "Why"].map((h) => el("th", { text: h }))));
  for (const r of ((S.engine || {}).transitions || []).slice().reverse()) {
    t.append(el("tr", {}, el("td", { text: hhmmss(r.ts) }), el("td", { text: `${r.from} → ${r.to}` }), el("td", { class: "wrap", text: r.why })));
  }
}

// ---------------------------------------------------------------- audit log
async function loadAudit(reset) {
  const kind = $("audit-kind").value;
  if (reset) { auditOffset = 0; auditRecords = []; }
  try {
    const r = await fetch(`/api/audit?offset=${auditOffset}&limit=200${kind ? "&kind=" + encodeURIComponent(kind) : ""}`, { cache: "no-store" });
    const j = await r.json();
    auditTotal = j.total;
    auditRecords = auditRecords.concat(j.records);
    auditOffset = auditRecords.length;
    drawAudit();
  } catch (e) { /* retried on the next refresh */ }
}
async function refreshAuditHead() {
  if (auditOffset > 200) return;          // user is paging back; do not jump
  await loadAudit(true);
}
function auditSummary(rec) {
  const p = rec.payload || {};
  switch (rec.kind) {
    case "DECISION": return `${p.action} ${p.setup || ""} ${p.direction || ""} · ${(p.reason_codes || []).join(", ")}`;
    case "GUARD_BLOCK": return (p.reasons || []).join(", ");
    case "ORDER": return `${p.request ? p.request.side + " " + p.request.qty + " @ " + p.request.price : ""} → ${p.status} (${p.order_id})`;
    case "FILL": return `filled ${p.qty} @ ${p.price} · stop ${p.stop} · 1R ₹${num(p.one_r)}`;
    case "EXIT_ORDER": return `${p.reason} · sell @ ${p.price} · attempt ${p.attempt} → ${p.status}`;
    case "TRADE_CLOSED": return `${p.setup} exit ${p.exit} (${p.reason}) · net ₹${num(p.net)} · ${rr(p.r_net)}`;
    case "STOP_MODIFIED": return `stop ${p.from} → ${p.to}`;
    case "CONTROL": return `${p.action}${p.reason ? " · " + p.reason : ""}${p.client ? " · " + p.client : ""}`;
    case "KILL_SWITCH": return `${p.why} · square-off ${(p.square_off || []).join(", ")}`;
    default: return "";
  }
}
function drawAudit() {
  const t = $("audit");
  t.replaceChildren(el("tr", {}, ...["#", "Time", "Kind", "Summary", "Payload"].map((h) => el("th", { text: h }))));
  for (const rec of auditRecords) {
    const det = el("details", { class: "payload" }, el("summary", { text: "json" }), el("pre", { text: JSON.stringify(rec.payload, null, 1) }));
    t.append(el("tr", {}, el("td", { text: rec.n }), el("td", { text: hhmmss(rec.ts) }), el("td", { text: rec.kind }),
      el("td", { class: "wrap", text: auditSummary(rec) }), el("td", {}, det)));
  }
  $("audit-more").disabled = auditOffset >= auditTotal;
  $("audit-more").textContent = `Load older (${auditOffset} of ${auditTotal})`;
}
async function verifyAudit() {
  try {
    const j = await (await fetch("/api/audit/verify", { cache: "no-store" })).json();
    $("audit-verify").textContent = j.ok ? `hash chain intact ✓ (${j.records} records)` : `HASH CHAIN BROKEN ✕ after record ${j.records}`;
  } catch (e) { $("audit-verify").textContent = "verification unavailable"; }
}
$("audit-kind").addEventListener("change", () => loadAudit(true));
$("audit-more").addEventListener("click", () => loadAudit(false));

// ---------------------------------------------------------------- controls
function token() {
  const meta = document.querySelector('meta[name="control-token"]').content;
  if (meta && meta !== "__CONTROL_TOKEN__") return meta;
  return storageGet("sx-token") || "";
}
const ACTIONS = {
  ARM: () => ({
    title: (S.meta.mode === "LIVE") ? "ARM LIVE: the engine will place REAL orders autonomously" : "ARM PAPER: the engine will place paper orders autonomously",
    text: "Once armed, the engine trades every qualifying signal on its own until DISARM, PAUSE, the kill switch, or the end of day. " +
      "Arming never survives a restart.", phrase: S.control.arm_phrase, blockers: S.control.arm_blockers || [] }),
  DISARM: () => ({ title: "DISARM", text: "No new entries. An open position stays protected by its stop and is still managed and exited by the engine.", phrase: null, blockers: [] }),
  PAUSE: () => ({ title: "PAUSE NEW ENTRIES", text: "The engine keeps running, keeps managing and exiting any open position, and logs the signals it skips.", phrase: null, blockers: [] }),
  RESUME: () => ({ title: "RESUME ENTRIES", text: "The engine may enter new trades again (if armed).", phrase: null, blockers: [] }),
  SQUAREOFF: () => ({ title: "EMERGENCY SQUARE-OFF", text: "Trips the kill switch: cancels every open order, exits every position at market-protected prices, and LOCKS the engine " +
    "until the lock file is deleted and the engine restarted. Use only when something is wrong.", phrase: S.control.squareoff_phrase, blockers: [] }),
};
let pendingAction = null;
function openDialog(action) {
  if (!S || S.boot) return;
  pendingAction = action;
  const spec = ACTIONS[action]();
  $("confirm-title").textContent = spec.title;
  $("confirm-text").textContent = spec.text;
  const bl = $("confirm-blockers");
  bl.replaceChildren(...spec.blockers.map((b) => el("li", { text: b })));
  $("confirm-phrase-wrap").hidden = !spec.phrase;
  $("confirm-phrase").textContent = spec.phrase || "";
  $("confirm-input").value = "";
  $("token-wrap").hidden = !!token();
  $("confirm-result").textContent = spec.blockers.length ? "ARM is blocked until every item above is resolved." : "";
  $("confirm-go").disabled = spec.blockers.length > 0;
  $("confirm-go").className = "btn" + (action === "SQUAREOFF" || (action === "ARM" && S.meta.mode === "LIVE") ? " btn-danger" : "");
  $("confirm").showModal();
}
$("confirm-go").addEventListener("click", async () => {
  if (!pendingAction) return;
  if (!$("token-wrap").hidden && $("token-input").value) storageSet("sx-token", $("token-input").value.trim());
  const go = $("confirm-go");
  go.disabled = true;
  $("confirm-result").textContent = "sending…";
  try {
    const r = await fetch("/api/control", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Control-Token": token() },
      body: JSON.stringify({ action: pendingAction, confirm: $("confirm-input").value }),
    });
    const j = await r.json();
    $("confirm-result").textContent = (j.ok ? "✓ " : "✕ ") + j.message + (j.blockers ? ": " + j.blockers.join("; ") : "");
    if (j.ok) setTimeout(() => $("confirm").close(), 900);
  } catch (e) {
    $("confirm-result").textContent = "✕ could not reach the engine";
  } finally { go.disabled = false; }
});
$("btn-arm").addEventListener("click", () => openDialog("ARM"));
$("btn-disarm").addEventListener("click", () => openDialog("DISARM"));
$("btn-pause").addEventListener("click", () => openDialog(S && S.control && S.control.paused ? "RESUME" : "PAUSE"));
$("btn-squareoff").addEventListener("click", () => openDialog("SQUAREOFF"));

// ---------------------------------------------------------------- start
connect();
loadAudit(true);
verifyAudit();
setInterval(refreshAuditHead, 5000);
setInterval(verifyAudit, 30000);
