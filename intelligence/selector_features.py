"""09:45 feature matrix.

Input is ONLY a SessionInput (candles < cutoff + pre-open metadata) and baselines
computed from PRIOR sessions. This module never imports the outcome evaluator and
never sees post-cutoff data. Missing inputs produce None (null) plus quality flags;
nothing is fabricated.

Every function is single-pass over a stock's ~30 bars; the whole 989-stock matrix
is a few hundred thousand float operations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from math import log, sqrt
from statistics import median

from .selector_config import SelectorConfig
from .selector_data import SessionInput, Series

# Features compared across the universe (percentile 0..1 among eligible stocks).
CROSS_SECTIONAL = (
    "gap_pct", "abs_gap_pct", "return_since_open", "return_5m", "return_15m",
    "turnover", "cumulative_volume", "relative_volume", "volume_acceleration",
    "realized_vol_pct", "or_expansion", "rel_market", "momentum_short", "activity", "efficiency_ratio",
)

CORE_FIELDS = ("current_price", "opening_price", "return_since_open", "return_5m", "return_15m",
               "ema9", "atr_pct", "cumulative_volume")


@dataclass(frozen=True)
class MarketContext:
    market_return: float | None        # % since open (NIFTY if available, else universe median)
    market_source: str                 # "NIFTY" | "UNIVERSE_MEDIAN"
    market_minute_returns: dict        # minute index -> % 1-minute market return
    breadth: float | None              # share of usable stocks green since open
    regime: str                        # UP | DOWN | MIXED
    sector_returns: dict               # sector -> median return since open
    sector_available: bool


@dataclass(frozen=True)
class FeatureTable:
    features: dict                     # symbol -> {feature: value|None}
    eligible: tuple[str, ...]          # sorted
    exclusions: dict                   # symbol -> reason
    market: MarketContext


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1.0) * 100.0


def _minute(t: datetime) -> int:
    return t.hour * 60 + t.minute - (9 * 60 + 15)


def _ema(values: tuple[float, ...], span: int) -> float | None:
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1.0)
    e = sum(values[:span]) / span            # SMA seed, then recursive EMA
    for x in values[span:]:
        e = alpha * x + (1 - alpha) * e
    return e


def _slope_pct(values: tuple[float, ...]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    mx = (n - 1) / 2.0
    my = sum(values) / n
    num = sum((i - mx) * (y - my) for i, y in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return (num / den) / values[-1] * 100.0 if den and values[-1] else None


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def stock_features(s: Series, cfg: SelectorConfig, cutoff: datetime, baseline: tuple | None) -> dict:
    """Raw per-stock features from candles < cutoff. Returns {} when there are no bars."""
    n = len(s)
    if n == 0:
        return {}
    o, h, l, c, v = s.o, s.h, s.l, s.c, s.v
    f: dict = {}
    first_is_open = s.ts[0].time() == cfg.session_start
    open_px = o[0] if first_is_open else s.today_open
    price = c[-1]
    pc = s.previous_close

    # --- price / gap ---
    f["previous_close"] = pc
    f["current_price"] = price
    f["opening_price"] = open_px
    f["opening_price_source"] = "09:15_CANDLE" if first_is_open else ("FEED_TODAY_OPEN" if s.today_open else None)
    f["gap_pct"] = _pct(open_px, pc)
    f["abs_gap_pct"] = abs(f["gap_pct"]) if f["gap_pct"] is not None else None
    f["return_since_open"] = _pct(price, open_px)
    f["dist_prev_close_pct"] = _pct(price, pc)
    for k in (1, 5, 10, 15, 30):
        ref = c[-1 - k] if n > k else (open_px if n == k else None)
        f[f"return_{k}m"] = _pct(price, ref)

    # --- opening structure (09:15-09:29) ---
    or_idx = [i for i, t in enumerate(s.ts) if t.time() < cfg.opening_range_end]
    day_high, day_low = max(h), min(l)
    if len(or_idx) >= 10 and first_is_open:
        orh, orl = max(h[i] for i in or_idx), min(l[i] for i in or_idx)
        rng = orh - orl
        f.update({
            "opening_high": orh, "opening_low": orl,
            "opening_range_pct": rng / open_px * 100.0 if open_px else None,
            "or_position": max(-1.5, min(1.5, (price - (orh + orl) / 2.0) / rng)) if rng > 0 else 0.0,
            "dist_or_high_pct": _pct(price, orh),
            "dist_or_low_pct": _pct(price, orl),
            "or_break": 1 if price > orh else (-1 if price < orl else 0),
            "or_expansion": (day_high - day_low) / rng if rng > 0 else None,
        })
    else:
        for k in ("opening_high", "opening_low", "opening_range_pct", "or_position", "dist_or_high_pct",
                  "dist_or_low_pct", "or_break", "or_expansion"):
            f[k] = None
    day_rng = day_high - day_low
    f["day_range_position"] = ((price - day_low) / day_rng - 0.5) if day_rng > 0 else 0.0

    # --- volume ---
    cum = sum(v)
    f["cumulative_volume"] = cum
    f["turnover"] = sum(ci * vi for ci, vi in zip(c, v))
    f["volume_1m"] = v[-1]
    f["volume_5m"] = sum(v[-5:])
    f["volume_15m"] = sum(v[-15:])
    prev10 = v[-15:-5]
    f["volume_acceleration"] = ((sum(v[-5:]) / 5.0) / (sum(prev10) / len(prev10))
                                if len(prev10) == 10 and sum(prev10) > 0 else None)
    f["volume_imbalance"] = (sum(_sign(c[i] - o[i]) * v[i] for i in range(n)) / cum) if cum > 0 else None
    base_vol, base_rv = (baseline or (None, None))
    f["relative_volume"] = cum / base_vol if base_vol else None
    f["relative_volume_source"] = "PRIOR_SESSIONS_MEDIAN" if base_vol else "UNAVAILABLE_NO_BASELINE"

    # --- volatility ---
    rets = [log(c[i] / c[i - 1]) for i in range(1, n)]
    rv = sqrt(sum(r * r for r in rets)) * 100.0 if rets else None
    f["realized_vol_pct"] = rv
    trs = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    f["atr_pct"] = (sum(trs[-10:]) / len(trs[-10:])) / price * 100.0
    f["range_1m_pct"] = (h[-1] - l[-1]) / price * 100.0
    f["range_5m_pct"] = (max(h[-5:]) - min(l[-5:])) / price * 100.0
    first10 = max(h[:10]) - min(l[:10]) if n >= 20 else None
    f["range_expansion"] = ((max(h[-10:]) - min(l[-10:])) / first10) if first10 else None
    f["relative_volatility"] = rv / base_rv if (rv is not None and base_rv) else None
    f["activity"] = f["relative_volume"] if f["relative_volume"] is not None else f["volume_acceleration"]

    # --- trend / structure ---
    e9, e20 = _ema(c, 9), _ema(c, 20)
    f["ema9"], f["ema20"] = e9, e20
    f["ema_spread_pct"] = (e9 - e20) / price * 100.0 if (e9 is not None and e20 is not None) else None
    f["price_vs_ema9_pct"] = _pct(price, e9)
    f["price_vs_ema20_pct"] = _pct(price, e20)
    f["slope_10_pct"] = _slope_pct(c[-10:]) if n >= 10 else None
    run, d0 = 0, _sign(c[-1] - o[-1])
    if d0:
        for i in range(n - 1, -1, -1):
            if _sign(c[i] - o[i]) != d0:
                break
            run += 1
    f["consecutive_candles"] = d0 * run
    if n >= 15:
        blocks = [(max(h[j:j + 5]), min(l[j:j + 5])) for j in (n - 15, n - 10, n - 5)]
        hh = blocks[0][0] < blocks[1][0] < blocks[2][0]
        hl = blocks[0][1] < blocks[1][1] < blocks[2][1]
        lh = blocks[0][0] > blocks[1][0] > blocks[2][0]
        ll = blocks[0][1] > blocks[1][1] > blocks[2][1]
        f["structure"] = 1 if (hh and hl) else (-1 if (lh and ll) else 0)
    else:
        f["structure"] = None
    f["pullback_from_high"] = (day_high - price) / day_rng if day_rng > 0 else 0.0
    f["pullback_from_low"] = (price - day_low) / day_rng if day_rng > 0 else 0.0

    # Kaufman efficiency ratio since the open: net move / total path travelled (signed,
    # -1..1). A clean trend is near +/-1; a choppy walk with the same net move is near 0.
    path = (abs(c[0] - open_px) if open_px else 0.0) + sum(abs(c[i] - c[i - 1]) for i in range(1, n))
    f["efficiency_ratio"] = ((price - open_px) / path) if (open_px and path > 0) else None
    f["extension_vs_range"] = ((price - e20) / day_rng) if (e20 is not None and day_rng > 0) else None

    # --- momentum ---
    f["momentum_short"] = f["return_5m"]
    r5, r10 = f["return_5m"], f["return_10m"]
    f["acceleration"] = (r5 - (r10 - r5)) if (r5 is not None and r10 is not None) else None
    last10 = range(max(0, n - 10), n)
    f["persistence_10"] = (sum(_sign(c[i] - o[i]) for i in last10) / len(last10)) if n else None
    ro = f["return_since_open"]
    f["reversal_flag"] = (1 if (r5 is not None and ro is not None and _sign(r5) * _sign(ro) < 0) else 0)

    # --- data quality ---
    minutes = {_minute(t) for t in s.ts}
    span = _minute(cutoff) - 0
    f["n_bars"] = n
    f["missing_minutes"] = max(0, span - len(minutes & set(range(span))))
    f["last_bar_age_min"] = (cutoff - s.ts[-1]).total_seconds() / 60.0 - 1.0
    f["stale"] = f["last_bar_age_min"] > cfg.max_stale_minutes
    f["spread_available"] = False          # PSYGRID stock feed carries no bid/ask
    f["missing_field_count"] = sum(1 for k in CORE_FIELDS if f.get(k) is None) + (pc is None)
    f["data_quality_score"] = round(max(0.0, 1.0 - f["missing_minutes"] / span - 0.1 * f["missing_field_count"]
                                        - (0.5 if f["stale"] else 0.0)), 4)
    return f


def _percentiles(values: dict) -> dict:
    """Average-rank percentile in [0, 1]; None stays None. Deterministic on ties."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    out = {k: None for k in values}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        p = ((i + j) / 2.0) / (n - 1) if n > 1 else 0.5
        for q in range(i, j + 1):
            out[items[q][1]] = p
        i = j + 1
    return out


def _market_context(si: SessionInput, feats: dict, usable: list[str], sectors: dict) -> MarketContext:
    rets = [feats[s]["return_since_open"] for s in usable if feats[s].get("return_since_open") is not None]
    breadth = (sum(1 for r in rets if r > 0) / len(rets)) if rets else None
    minute_rets: dict = {}
    source = "UNIVERSE_MEDIAN"
    idx = si.index
    if idx is not None and len(idx) >= 2 and idx.ts[0].time() == time(9, 15):
        mret = _pct(idx.c[-1], idx.o[0])
        minute_rets = {_minute(idx.ts[i]): _pct(idx.c[i], idx.c[i - 1]) for i in range(1, len(idx))}
        source = "NIFTY"
    else:
        mret = median(rets) if rets else None
        buckets: dict = {}
        for sym in usable:
            s = si.stocks[sym]
            for i in range(1, len(s)):
                buckets.setdefault(_minute(s.ts[i]), []).append(_pct(s.c[i], s.c[i - 1]))
        minute_rets = {m: median(x) for m, x in buckets.items()}
    if breadth is None or mret is None:
        regime = "MIXED"
    else:
        regime = "UP" if (breadth >= 0.6 and mret > 0) else ("DOWN" if (breadth <= 0.4 and mret < 0) else "MIXED")
    groups: dict = {}
    for sym in usable:
        sec = sectors.get(sym)
        r = feats[sym].get("return_since_open")
        if sec and r is not None:
            groups.setdefault(sec, []).append(r)
    sector_returns = {k: median(v) for k, v in groups.items()}
    return MarketContext(mret, source, minute_rets, breadth, regime, sector_returns, bool(sectors))


def build_feature_table(si: SessionInput, cfg: SelectorConfig, sectors: dict | None = None,
                        baselines: dict | None = None) -> FeatureTable:
    sectors = sectors or {}
    baselines = baselines or {}
    feats: dict = {}
    exclusions: dict = {}
    for sym in si.received_symbols:
        if sym in si.unparseable:
            exclusions[sym] = si.unparseable[sym]
            continue
        s = si.stocks.get(sym)
        try:
            f = stock_features(s, cfg, si.cutoff, baselines.get(sym)) if s is not None else {}
        except Exception as exc:                     # a bad stock is excluded, never fatal
            f, exclusions[sym] = {}, f"FEATURE_ERROR:{type(exc).__name__}"
        feats[sym] = f
        if sym in exclusions:
            continue
        if not f:
            exclusions[sym] = "NO_PRE_CUTOFF_CANDLES"
        elif f["n_bars"] < cfg.min_bars:
            exclusions[sym] = "TOO_FEW_BARS"
        elif f["stale"]:
            exclusions[sym] = "STALE_DATA"
        elif f["opening_price"] is None:
            exclusions[sym] = "NO_OPENING_PRICE"
        elif f["cumulative_volume"] <= 0:
            exclusions[sym] = "ZERO_VOLUME"

    usable = sorted(sym for sym in feats if sym not in exclusions)
    turn_pct = _percentiles({sym: feats[sym]["turnover"] for sym in usable})
    liquid = [sym for sym in usable if turn_pct[sym] >= cfg.min_liquidity_percentile]
    if not liquid:                       # never leave the engine without a candidate pool
        liquid = usable
    for sym in usable:
        if sym not in liquid:
            exclusions[sym] = "LOW_LIQUIDITY"

    market = _market_context(si, feats, usable, sectors)
    for sym in usable:
        f = feats[sym]
        ro = f["return_since_open"]
        f["market_return"] = market.market_return
        f["rel_market"] = (ro - market.market_return) if (ro is not None and market.market_return is not None) else None
        pairs = [(_pct(si.stocks[sym].c[i], si.stocks[sym].c[i - 1]), market.market_minute_returns.get(_minute(si.stocks[sym].ts[i])))
                 for i in range(1, len(si.stocks[sym]))]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        beta = None
        if len(pairs) >= cfg.beta_min_pairs:
            mb = sum(b for _, b in pairs) / len(pairs)
            ma = sum(a for a, _ in pairs) / len(pairs)
            var = sum((b - mb) ** 2 for _, b in pairs)
            if var > 0:
                beta = cfg.beta_shrink * (sum((a - ma) * (b - mb) for a, b in pairs) / var) + (1 - cfg.beta_shrink)
        f["beta"] = beta
        f["residual_return"] = (ro - beta * market.market_return
                                if (ro is not None and beta is not None and market.market_return is not None) else None)
        sec = sectors.get(sym)
        peers = sum(1 for x in usable if sectors.get(x) == sec) if sec else 0
        f["sector"] = sec
        f["sector_rel"] = (ro - market.sector_returns[sec]
                           if (sec and peers >= cfg.sector_min_peers and ro is not None) else None)

    eligible = tuple(sorted(liquid))
    for name in CROSS_SECTIONAL:
        pct = _percentiles({sym: feats[sym].get(name) for sym in eligible})
        for sym in eligible:
            feats[sym][f"pct_{name}"] = pct[sym]
    return FeatureTable(feats, eligible, exclusions, market)
