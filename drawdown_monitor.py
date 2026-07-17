#!/usr/bin/env python3
"""
drawdown_monitor.py — Doc B B1 (Drawdown-Deployment Protocol) + B7(1) (computed regime state).

B1: a standing, fully mechanical mandate — deploy reserve capital into VUAG at predefined
drawdown depths (D9: VUAG GBP close vs trailing 252d high; D10: -10/-20/-30 -> 1/3 of reserve
each; D11: reserve = max(0, cash + MMF - £500 buffer - committed actions); D12: one shot per
threshold per episode, episode resets on a new 252d high). No judgment anywhere in the loop.

B7: regime_state in {RISK_ON, LATE_CYCLE, RISK_OFF, RECOVERY} — pure decision-table lookup
(scoring_config.REGIME_RULES) on: price vs 200d MA, drawdown band, 63d slope sign. Step 4's
REGIME output becomes an ANNOTATION; disagreement is logged (calibration data).

Appended to every existing scheduled run (~12 checks/month; worst-case lag ~4 days — the
ladder self-corrects, deeper tranche fires at next check). State: drawdown_state.json.

Usage:
  python3 drawdown_monitor.py                       # fetch VUAG, update state, print status
  python3 drawdown_monitor.py --reserve 10600       # supply reserve explicitly
  python3 drawdown_monitor.py --check-only          # no state write
  python3 drawdown_monitor.py --selftest            # U-B1-1/2/3 + U-B7-1 (no network)
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import scoring_config as _cfg
except Exception:
    _cfg = None

STATE_PATH = os.path.join(HERE, "drawdown_state.json")


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"high_252d": None, "high_date": None, "last_close": None, "drawdown_pct": 0.0,
                "episode_id": 1, "tranches_fired": {"t10": False, "t20": False, "t30": False},
                "reserve_gbp": None, "last_check": None, "regime_state": None}


def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def update_ladder(state, closes_252, today=None):
    """Pure B1 core (U-B1-1). closes_252: iterable of daily closes, most recent LAST
    (>=1 element; ideally 252 for the trailing high + 200 for the MA). Returns
    (state, fired:list) — fired lists thresholds newly crossed THIS update."""
    closes = [float(c) for c in closes_252 if c is not None]
    if not closes:
        return state, []
    last = closes[-1]
    lookback = int(_c("DRAWDOWN_LOOKBACK", 252))
    high = max(closes[-lookback:])
    prev_high = state.get("high_252d")
    # D12: NEW 252d high => episode reset (hysteresis — one shot per level per episode)
    if last >= high and (prev_high is None or last >= prev_high):
        if any(state.get("tranches_fired", {}).values()):
            state["episode_id"] = int(state.get("episode_id") or 1) + 1
        state["tranches_fired"] = {"t10": False, "t20": False, "t30": False}
    state["high_252d"] = round(max(high, prev_high or 0.0) if prev_high else high, 4)
    # trailing high can also DECAY as old highs age out of the window: use window max when the
    # stored high has left the lookback (approximation: window max is authoritative)
    if prev_high and high < prev_high:
        # stored high no longer in window -> window max governs (episode continues)
        state["high_252d"] = round(high, 4)
    state["last_close"] = round(last, 4)
    dd = (last / state["high_252d"] - 1.0) * 100.0 if state["high_252d"] else 0.0
    state["drawdown_pct"] = round(dd, 2)
    fired = []
    for lvl in _c("DRAWDOWN_TRANCHES", [10, 20, 30]):
        key = f"t{int(lvl)}"
        if dd <= -float(lvl) and not state["tranches_fired"].get(key):
            state["tranches_fired"][key] = True
            fired.append(key)
    state["last_check"] = (today or date.today()).isoformat()
    return state, fired


def tranche_size(reserve_gbp, tranches_fired, just_fired):
    """D10/D11 (U-B1-3): each firing deploys reserve / remaining-unfired-count computed at
    fire time (so sizes step up as the ladder depletes: 1/3, then 1/2 of remainder, then all)."""
    if reserve_gbp is None:
        return None
    remaining_before = sum(1 for k, v in tranches_fired.items()
                           if (not v) or k in just_fired)
    if remaining_before <= 0:
        return None
    return round(max(0.0, float(reserve_gbp)) / remaining_before, 2)


def compute_reserve(cash_gbp, mmf_gbp=0.0, committed_gbp=0.0, buffer_gbp=500.0):
    """D11 (U-B1-2)."""
    return round(max(0.0, (cash_gbp or 0.0) + (mmf_gbp or 0.0)
                     - (buffer_gbp or 0.0) - (committed_gbp or 0.0)), 2)


# ── B7(1): regime classifier — pure lookup, no judgment ─────────────────────
DEFAULT_REGIME_RULES = {
    # (above_200dma, dd_band, slope_sign) -> regime. dd_band: 0 (>-5%), 1 (-5..-15), 2 (<=-15)
    ("above", 0, "+"): "RISK_ON",
    ("above", 0, "-"): "LATE_CYCLE",
    ("above", 1, "+"): "RECOVERY",
    ("above", 1, "-"): "LATE_CYCLE",
    ("above", 2, "+"): "RECOVERY",
    ("above", 2, "-"): "RISK_OFF",
    ("below", 0, "+"): "LATE_CYCLE",
    ("below", 0, "-"): "RISK_OFF",
    ("below", 1, "+"): "RECOVERY",
    ("below", 1, "-"): "RISK_OFF",
    ("below", 2, "+"): "RECOVERY",
    ("below", 2, "-"): "RISK_OFF",
}


def classify_regime(closes, dd_pct):
    """B7 decision table on: VUAG vs 200d MA, drawdown band, 63d slope sign."""
    closes = [float(c) for c in closes if c is not None]
    if len(closes) < 63:
        return None, {}
    last = closes[-1]
    ma200 = sum(closes[-200:]) / min(len(closes), 200)
    above = "above" if last >= ma200 else "below"
    band = 0 if dd_pct > -5.0 else (1 if dd_pct > -15.0 else 2)
    slope = "+" if closes[-1] >= closes[-63] else "-"
    rules = _c("REGIME_RULES", DEFAULT_REGIME_RULES)
    key = (above, band, slope)
    regime = rules.get(key) or DEFAULT_REGIME_RULES.get(key)
    return regime, {"above_200dma": above == "above", "dd_band": band, "slope_63d": slope,
                    "ma200": round(ma200, 2), "last": round(last, 2)}


def emit_block(state, fired, reserve_gbp):
    """The DRAWDOWN_TRIGGER / standing-line block for run_context/intramonth/email."""
    line = (f"Drawdown ladder: {state['drawdown_pct']:+.1f}% from 252d high "
            f"({state.get('high_252d')}) | tranches fired "
            f"{sum(state['tranches_fired'].values())}/3 | reserve "
            f"{'£{:,.0f}'.format(reserve_gbp) if reserve_gbp is not None else 'n/a'} | "
            f"regime {state.get('regime_state') or 'n/a'}")
    block = {"standing_line": line, "state": {k: state[k] for k in
             ("high_252d", "drawdown_pct", "episode_id", "tranches_fired", "regime_state")
             if k in state}}
    if fired:
        size = tranche_size(reserve_gbp, state["tranches_fired"], fired)
        lim = round(state["last_close"] * 1.005, 2) if state.get("last_close") else None
        block["DRAWDOWN_TRIGGER"] = {
            "thresholds_fired_now": fired, "episode_id": state.get("episode_id"),
            "tranche_gbp": size,
            "instrument": "VUAG (D13 — held, UCITS/preclearance-exempt, GBP line)",
            "order": f"limit at last close +0.5% = {lim}" if lim else "limit at last close +0.5%",
            "shares_est": (round(size / state["last_close"]) if size and state.get("last_close")
                           else None),
            "ledger_route": "drawdown_protocol",
            "precedence": "fired tranches take the pounds BEFORE Step-10 stock deployments (B1.6b)",
        }
    return block


def run(reserve_gbp=None, check_only=False, state_path=STATE_PATH):
    import yfinance as yf
    t = yf.Ticker("VUAG.L")
    hist = t.history(period="2y")
    closes = list(hist["Close"].dropna()) if hist is not None and len(hist) else []
    if not closes:
        print("DRAWDOWN_MONITOR ERROR: no VUAG history", file=sys.stderr)
        return 1
    state = load_state(state_path)
    state, fired = update_ladder(state, closes)
    regime, basis = classify_regime(closes, state["drawdown_pct"])
    state["regime_state"] = regime
    state["regime_basis"] = basis
    if reserve_gbp is not None:
        state["reserve_gbp"] = reserve_gbp
    if not check_only and _c("DRAWDOWN_PROTOCOL_ACTIVE", True):
        save_state(state, state_path)
    block = emit_block(state, fired, reserve_gbp if reserve_gbp is not None
                       else state.get("reserve_gbp"))
    print(json.dumps(block, indent=2))
    return 0


def _selftest():
    # U-B1-1: threshold/re-arm truth table (synthetic series)
    st = load_state("/nonexistent")
    base = [100.0] * 260
    st, fired = update_ladder(st, base, today=date(2026, 1, 1))
    assert fired == [] and st["drawdown_pct"] == 0.0
    # -12%: fires t10 only
    st, fired = update_ladder(st, base + [88.0], today=date(2026, 1, 2))
    assert fired == ["t10"] and st["tranches_fired"]["t10"], (fired, st)
    # echo low at -11%: NO re-fire
    st, fired = update_ladder(st, base + [88.0, 89.0], today=date(2026, 1, 3))
    assert fired == []
    # gap through two levels: -32% fires t20 AND t30 in one update
    st, fired = update_ladder(st, base + [88.0, 89.0, 68.0], today=date(2026, 1, 4))
    assert fired == ["t20", "t30"], fired
    # recovery to new 252d high resets episode + flags (window rolls past old highs)
    rec = base + [88.0, 89.0, 68.0] + [101.0]
    st, fired = update_ladder(st, rec, today=date(2026, 1, 5))
    assert st["tranches_fired"] == {"t10": False, "t20": False, "t30": False}
    assert st["episode_id"] == 2
    # double-dip SAME episode (no new high): -25% fires t10+t20 once
    st2 = load_state("/nonexistent")
    st2, _ = update_ladder(st2, base, today=date(2026, 1, 1))
    st2, f1 = update_ladder(st2, base + [75.0], today=date(2026, 1, 2))
    assert f1 == ["t10", "t20"]
    st2, f2 = update_ladder(st2, base + [75.0, 85.0, 74.0], today=date(2026, 1, 3))
    assert f2 == []   # same episode, already fired
    # U-B1-2: reserve math
    assert compute_reserve(10778.0, 0.0, 0.0) == 10278.0
    assert compute_reserve(10778.0, 2000.0, 3000.0) == 9278.0
    assert compute_reserve(400.0) == 0.0
    # U-B1-3: tranche sizing as ladder depletes
    tf = {"t10": True, "t20": False, "t30": False}
    assert tranche_size(9000.0, tf, ["t10"]) == 3000.0          # 1/3 at first fire
    tf2 = {"t10": True, "t20": True, "t30": False}
    assert tranche_size(6000.0, tf2, ["t20"]) == 3000.0         # 1/2 of remainder
    tf3 = {"t10": True, "t20": True, "t30": True}
    assert tranche_size(3000.0, tf3, ["t30"]) == 3000.0         # all of remainder
    # U-B7-1: classifier decision table
    up = list(range(100, 360))                                   # rising, above MA, dd 0
    r, b = classify_regime([float(x) for x in up], 0.0)
    assert r == "RISK_ON", (r, b)
    down = [float(300 - i * 0.5) for i in range(260)]            # falling, below MA
    r, b = classify_regime(down, -16.0)
    assert r == "RISK_OFF", (r, b)
    # trough bounce: below MA but 63d slope positive, deep dd -> RECOVERY
    vshape = [300.0] * 150 + [float(300 - i * 3) for i in range(50)] + \
             [float(150 + i * 1.0) for i in range(60)]
    r, b = classify_regime(vshape, -20.0)
    assert r == "RECOVERY", (r, b)
    r, b = classify_regime([300.0] * 200 + [301.0] * 60, -1.0)   # flat/up, above MA, shallow
    assert r == "RISK_ON", (r, b)
    r, b = classify_regime([300.0] * 200 + [299.0] * 60, -1.0)   # above-ish but slipping
    assert r in ("LATE_CYCLE", "RISK_OFF"), (r, b)
    print("drawdown_monitor SELF-TEST OK (U-B1-1/2/3 + U-B7-1)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reserve", type=float, default=None)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        sys.exit(run(a.reserve, a.check_only, a.state))
