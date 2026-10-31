#!/usr/bin/env python3
"""
drawdown_monitor.py — Doc B B1 (Drawdown-Deployment Protocol) + B7(1) (computed regime state).

B1: a standing mechanical mandate, REVISED by S3/B04 (24-Oct-2026, Raj 10-Oct-2026 decisions): a 10%
drawdown creates ONE purchase opportunity, 20% creates TWO more (20% first retains all three), 30% retains
the three and triggers a FRESH REVIEW (no fourth opportunity, no t30 purchase). Opportunities carry stable
IDs and a lifecycle (OPEN -> APPROVED -> STARTED -> FULFILLED | CLOSED | EXPIRED); an unfilled one is
retained until a genuinely NEW episode high and is paused above its threshold. Cash is the eligible cash
at each review (no saved reserve as authority); per-opportunity sizing is Raj's 24-Oct option a (all eligible cash, capped by VUAG's approved limit).
The protocol OFF switch suppresses every opportunity, actionable state and trigger.

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
import argparse, json, math, os, sys
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
            return _ensure_schema(json.load(f))
    except Exception:
        return _ensure_schema({"high_252d": None, "high_date": None, "last_close": None, "drawdown_pct": 0.0,
                               "episode_id": 1, "tranches_fired": {"t10": False, "t20": False, "t30": False},
                               "last_check": None, "regime_state": None, "schema_version": STATE_SCHEMA})


def save_state(state, path=STATE_PATH):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# S3 / B04 (24-Oct-2026, ISA-0884) — THE DRAWDOWN OPPORTUNITY LIFECYCLE (Raj 10-Oct-2026 decisions)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Adopted policy (Raj_Decisions_10Oct.md): ONE opportunity at a 10% drawdown, TWO MORE at 20% (reaching
# 20% first retains all three); at 30% the three are retained and a FRESH REVIEW is triggered - there is
# NO fourth opportunity and no t30 purchase. Cash is the eligible cash AT EACH REVIEW (no earmark, no
# saved reserve as authority). An unfilled opportunity is RETAINED until a genuinely NEW episode high
# (strictly above the episode high: equality or a 252-day window that has simply aged does not reset);
# above its threshold it is PAUSED (not proposed) while existing orders still reconcile. A crossing or
# a regime refresh is an OBSERVATION, never a fill or a consumed entitlement.
# Lifecycle: OPEN (actionable_now / paused) -> APPROVED -> STARTED (first confirmed VUAG fill) ->
# FULFILLED (fills >= the approved amount, or the proposed amount when no explicit approval was recorded)
# | CLOSED (explicitly approved closure of the remainder) | EXPIRED (a new episode, never submitted).
# ⚑ SIZING (B04-R05, Raj 24-Oct-2026, option a): an opportunity may take up to ALL eligible cash at the review,
#   capped by VUAG's approved single-fund limit; the review's capacity is claimed once (oldest opportunity
#   first, others QUEUED) and the approval chooses the amount; stocks receive what remains after approved buys.
OPPORTUNITY_POLICY = dict(_c("DRAWDOWN_OPPORTUNITY_POLICY", {10: 1, 20: 2}))   # one home: scoring_config
REVIEW_THRESHOLD_PCT = int(_c("DRAWDOWN_REVIEW_PCT", 30))                       # one home: scoring_config
MAX_OPPORTUNITIES = sum(OPPORTUNITY_POLICY.values())
SIZING_BASIS = ("RAJ_24OCT2026_OPTION_A: up to ALL eligible cash at this review, capped by VUAG's approved single-fund "
                "limit; claimed once per review by the oldest actionable opportunity; the approval chooses the amount")
OPEN, APPROVED, STARTED, FULFILLED, CLOSED, EXPIRED = ("OPEN", "APPROVED", "STARTED", "FULFILLED",
                                                       "CLOSED", "EXPIRED")
LIVE_STATES = (OPEN, APPROVED, STARTED)
FILL_FRACTION = 0.9                        # = funding_waterfall.TRANCHE_EXECUTED_FRACTION (one home there)
STATE_SCHEMA = "2.0"


def protocol_on():
    return bool(_c("DRAWDOWN_PROTOCOL_ACTIVE", True))


def _thresholds():
    return sorted(set(list(OPPORTUNITY_POLICY) + [REVIEW_THRESHOLD_PCT]))


def _ensure_schema(state):
    """Migrate a v1 state in place (history preserved; nothing manufactured - B04-R09)."""
    st = state
    if st.get("schema_version") != STATE_SCHEMA:
        if st.get("tranche_log"):
            st.setdefault("legacy_tranche_log", list(st.get("tranche_log") or []))
        st.pop("tranche_log", None)
        if "reserve_gbp" in st:
            # ⚑ B04-R05: a saved reserve is never authority again - kept as dated history only
            st["legacy_reserve_gbp_v1"] = {"gbp": st.pop("reserve_gbp"), "as_of": st.get("last_check")}
        st["schema_version"] = STATE_SCHEMA
    st.setdefault("episode_id", 1)
    st.setdefault("episode_high", st.get("high_252d"))
    st.setdefault("opportunities", [])
    st.setdefault("crossings", [])
    st.setdefault("tranches_fired", {"t%d" % t: False for t in _thresholds()})
    return st


def _episode_opps(state, ep=None):
    ep = state.get("episode_id") if ep is None else ep
    return [o for o in state.get("opportunities") or [] if o.get("episode_id") == ep]


def update_ladder(state, closes_252, today=None):
    """Pure ladder update. Returns (state, newly_crossed_threshold_keys). Crossings are OBSERVATIONS that
    create the policy's opportunities; nothing here is a purchase or a consumed entitlement."""
    state = _ensure_schema(state)
    closes = [float(c) for c in closes_252 if c is not None and math.isfinite(float(c))]
    if not closes:
        return state, []
    today_s = (today or date.today()).isoformat()
    last = closes[-1]
    lookback = int(_c("DRAWDOWN_LOOKBACK", 252))
    window_high = max(closes[-lookback:])
    eh = state.get("episode_high")
    if eh is None:
        eh = window_high
    # ⚑ B04-R03 / EV-0109/0110: ONLY a close STRICTLY above the episode high starts a new episode.
    if last > float(eh) + 1e-12:
        ep_opps = _episode_opps(state)
        if ep_opps or any((state.get("tranches_fired") or {}).values()):
            for o in ep_opps:
                if o.get("state") in (OPEN, APPROVED) and not o.get("fills"):
                    o["state"] = EXPIRED
                    o.setdefault("history", []).append({"on": today_s, "to": EXPIRED,
                                                        "why": "new episode high %.4f > %.4f" % (last, float(eh))})
            state["episode_id"] = int(state.get("episode_id") or 1) + 1
        state["tranches_fired"] = {"t%d" % t: False for t in _thresholds()}
        state.pop("fresh_review", None)
        eh = last
    state["episode_high"] = round(float(eh), 4)
    state["high_252d"] = round(window_high, 4)          # diagnostic lookback (D9 window), never resets an episode
    state["last_close"] = round(last, 4)
    dd = (last / float(eh) - 1.0) * 100.0 if eh else 0.0
    state["drawdown_pct"] = round(dd, 2)
    state["drawdown_vs_252d_pct"] = round((last / window_high - 1.0) * 100.0, 2) if window_high else None
    fired = []
    for t in _thresholds():
        key = "t%d" % t
        if dd <= -float(t) and not state["tranches_fired"].get(key):
            state["tranches_fired"][key] = True
            fired.append(key)
            state["crossings"].append({"episode_id": state["episode_id"], "threshold_pct": t, "on": today_s,
                                       "close": round(last, 4), "drawdown_pct": round(dd, 2)})
            if t == REVIEW_THRESHOLD_PCT:
                state["fresh_review"] = {"episode_id": state["episode_id"], "on": today_s,
                                         "why": "30%% drawdown: the three opportunities are retained and a fresh "
                                                "review is required - no fourth opportunity (Raj 10-Oct-2026)"}
            # ⚑ B04-R07: with the protocol OFF the crossing is recorded (diagnostic) but NO opportunity is created
            for n in (range(1, OPPORTUNITY_POLICY.get(t, 0) + 1) if protocol_on() else ()):
                oid = "OPP-E%d-T%d-%d" % (state["episode_id"], t, n)
                if not any(o.get("opportunity_id") == oid for o in state["opportunities"]):
                    state["opportunities"].append({"opportunity_id": oid, "episode_id": state["episode_id"],
                                                   "threshold_pct": t, "seq": n, "state": OPEN,
                                                   "observed_on": today_s, "fills": [],
                                                   "history": [{"on": today_s, "to": OPEN, "why": "threshold crossed"}]})
    for o in _episode_opps(state):
        o["actionable_now"] = bool(o.get("state") in (OPEN, APPROVED) and dd <= -float(o["threshold_pct"]))
        o["paused_above_threshold"] = bool(o.get("state") in (OPEN, APPROVED) and not o["actionable_now"])
    state["last_check"] = today_s
    return state, fired


def _destination_tickers(state):
    """P-D / ISA-0886: fund-router destinations proposed to a LIVE opportunity also count as its fills."""
    return {str(d.get("ticker") or "").upper() for o in _episode_opps(state) if o.get("state") in LIVE_STATES
            for d in o.get("destinations") or [] if d.get("ticker") and d.get("ticker") != "VUAG"}


def reconcile_fills(state, ledger_entries, today=None):
    """Assign confirmed VUAG purchases (dealing record) to the episode's opportunities: child fills belong to
    a STARTED opportunity first; a fill dated before an opportunity was observed never counts; one fill, one
    opportunity (by reference). -> list of assignments made this call."""
    state = _ensure_schema(state)
    today_s = (today or date.today()).isoformat()
    used = {f.get("reference") for o in state["opportunities"] for f in o.get("fills") or []}
    buys = sorted((e for e in ledger_entries or [] if e.get("type") == "buy"
                   and (str(e.get("ticker") or "").upper().split(".")[0] == "VUAG"
                        or str(e.get("ticker") or "").upper() in _destination_tickers(state))
                   and e.get("reference") and e.get("reference") not in used
                   and float(e.get("amount_gbp") or 0) > 0), key=lambda e: str(e.get("date")))
    made = []
    for b in buys:
        cands = [o for o in _episode_opps(state) if o.get("state") in LIVE_STATES
                 and str(b.get("date")) >= str(o.get("observed_on"))]
        if not cands:
            continue
        cands.sort(key=lambda o: ({STARTED: 0, APPROVED: 1, OPEN: 2}[o["state"]], o["threshold_pct"], o["seq"]))
        o = cands[0]
        o["fills"].append({"reference": b.get("reference"), "date": b.get("date"),
                           "gbp": round(abs(float(b.get("amount_gbp"))), 2), "quantity": b.get("quantity")})
        if o["state"] != STARTED:
            o["history"].append({"on": today_s, "to": STARTED, "why": "first confirmed fill %s" % b.get("reference")})
            o["state"] = STARTED
            o["started_on"] = b.get("date")
            o.setdefault("proposed_gbp_at_start", o.get("proposed_gbp"))
        tot = sum(f["gbp"] for f in o["fills"])
        target = o.get("approved_gbp") if o.get("approved_gbp") is not None else o.get("proposed_gbp_at_start")
        if target and tot >= FILL_FRACTION * float(target):
            o["state"] = FULFILLED
            o["fulfilled_on"] = b.get("date")
            o["fulfilled_basis"] = "APPROVED_AMOUNT" if o.get("approved_gbp") is not None else "PROPOSED_AMOUNT_INTERIM"
            o["history"].append({"on": today_s, "to": FULFILLED, "why": "fills GBP %.2f >= %.0f%% of GBP %.2f"
                                 % (tot, FILL_FRACTION * 100, float(target))})
        made.append({"opportunity_id": o["opportunity_id"], "reference": b.get("reference"), "state": o["state"]})
    return made


def approve_opportunity(state, opportunity_id, amount_gbp, approved_by, on=None):
    """Record an explicit approval (amount chosen at the review within approved limits)."""
    o = next((x for x in state.get("opportunities") or [] if x.get("opportunity_id") == opportunity_id), None)
    if o is None or o.get("state") not in (OPEN, APPROVED):
        raise ValueError("%s is not an OPEN/APPROVED opportunity" % opportunity_id)
    a = float(amount_gbp)
    if not math.isfinite(a) or a <= 0:
        raise ValueError("approved amount must be finite and positive")
    o.update(state=APPROVED, approved_gbp=round(a, 2), approved_by=approved_by, approved_on=on or date.today().isoformat())
    o["history"].append({"on": o["approved_on"], "to": APPROVED, "why": "approved GBP %.2f by %s" % (a, approved_by)})
    return o


def close_opportunity(state, opportunity_id, reason, approved_by, on=None):
    """Explicitly approved closure of an opportunity's remainder (FULFILLED is never inferred from silence)."""
    o = next((x for x in state.get("opportunities") or [] if x.get("opportunity_id") == opportunity_id), None)
    if o is None or o.get("state") not in LIVE_STATES:
        raise ValueError("%s is not a live opportunity" % opportunity_id)
    o.update(state=CLOSED, closed_on=on or date.today().isoformat(), closed_by=approved_by, close_reason=reason)
    o["history"].append({"on": o["closed_on"], "to": CLOSED, "why": "%s (%s)" % (reason, approved_by)})
    return o


def _overflow_on():
    return bool(_c("DRAWDOWN_FUND_ROUTER_OVERFLOW", False))


def _split_destinations(amt, vuag_room, overflow):
    """VUAG first (the protocol's instrument, up to its approved-limit room); the rest pro rata to the fund router's
    allocation for this review (P-D / ISA-0886). -> [{ticker, gbp, route}]."""
    out = []
    v = round(min(amt, max(0.0, float(vuag_room or 0.0))), 2)
    if v > 0:
        out.append({"ticker": "VUAG", "gbp": v, "route": "VUAG_APPROVED_LIMIT"})
    rest = round(amt - v, 2)
    alloc = (overflow or {}).get("allocation") or {}
    tot = sum(alloc.values())
    if rest > 0.005 and tot > 0:
        scale = min(1.0, rest / tot)
        for k, g in sorted(alloc.items(), key=lambda kv: -kv[1]):
            x = round(g * scale, 2)
            if x > 0.005:
                out.append({"ticker": k, "gbp": x, "route": "FUND_ROUTER_OVERFLOW (ISA-0886)"})
    return out


def size_proposals(state, eligible_cash_gbp, last_close=None, fund_headroom_gbp=None, headroom_basis=None,
                   overflow=None):
    """Raj 24-Oct-2026 (B04-R05, option a): at THIS review an opportunity may take up to ALL eligible cash,
    capped by VUAG's APPROVED single-fund limit (`fund_headroom_gbp`). The review's capacity
    min(eligible cash, headroom) is claimed ONCE, by the oldest actionable opportunity (an APPROVED amount
    first); any other actionable opportunity is QUEUED for a later review - pounds are never claimed twice
    and the count of opportunities never implies equal tickets. The proposed figure is a CEILING: the
    review's approval chooses the amount within it. Shares are FLOORED at the quoted limit (EV-0121)."""
    state = _ensure_schema(state)
    ep = _episode_opps(state)
    px = last_close if last_close is not None else state.get("last_close")
    lim = round(float(px) * 1.005, 2) if px else None
    known = (eligible_cash_gbp is not None and math.isfinite(float(eligible_cash_gbp))
             and fund_headroom_gbp is not None and math.isfinite(float(fund_headroom_gbp)))
    _ov = overflow if (_overflow_on() and isinstance(overflow, dict) and overflow.get("capacity_gbp") is not None) else None
    _room = (float(fund_headroom_gbp) + float(_ov["capacity_gbp"])) if (known and _ov) else (
        float(fund_headroom_gbp) if known else None)
    cap_total = round(max(0.0, min(float(eligible_cash_gbp), _room)), 2) if known else None
    state["review_capacity"] = {"eligible_cash_gbp": eligible_cash_gbp, "vuag_headroom_gbp": fund_headroom_gbp,
                                "headroom_basis": headroom_basis, "capacity_gbp": cap_total,
                                "fund_router_overflow": ({k: _ov.get(k) for k in ("state", "capacity_gbp", "allocation",
                                                                                  "unallocated_gbp", "basis")}
                                                         if _ov else ("OFF (scoring_config.DRAWDOWN_FUND_ROUTER_OVERFLOW)"
                                                                      if not _overflow_on() else
                                                                      "NOT_SUPPLIED (VUAG-capped, S3 behaviour)")),
                                "rule": SIZING_BASIS + ("; P-D/ISA-0886: beyond VUAG's room the remaining capacity flows "
                                                        "through the existing fund router (approved caps/bands)" if _ov else "")}
    left = cap_total
    claimant = None
    order = sorted(ep, key=lambda o: (0 if o.get("state") == APPROVED else 1, o["threshold_pct"], o["seq"]))
    for o in order:
        if not (o.get("actionable_now") and o.get("state") in (OPEN, APPROVED)):
            o.update(proposed_gbp=None, shares_est=None, consideration_at_limit_gbp=None,
                     sizing_basis=("PAUSED_ABOVE_THRESHOLD" if o.get("paused_above_threshold") else
                                   "NOT_PROPOSED (%s)" % o.get("state")))
            continue
        if left is None:
            # A standalone check without the review's cash figure (weekly screen) keeps the LAST REVIEW's
            #   ceiling as a PROTECTIVE reservation (never as authority to buy); with none, UNMEASURED.
            if o.get("proposed_gbp") is not None and o.get("proposed_on"):
                o["sizing_basis"] = "CARRIED_FROM_REVIEW %s (reservation only; re-sized at the next review)" % o["proposed_on"]
                continue
            o.update(proposed_gbp=None, shares_est=None, consideration_at_limit_gbp=None, limit_price=lim,
                     sizing_basis="UNMEASURED: eligible cash or the VUAG limit headroom is unknown at this review")
            continue
        if o.get("state") == APPROVED:
            amt, basis = round(min(float(o["approved_gbp"]), left), 2), "APPROVED (within this review's capacity)"
        elif claimant is None:
            amt, basis = left, SIZING_BASIS
        else:
            amt, basis = 0.0, "QUEUED: this review's capacity is claimed by %s" % claimant
        if amt > 0 and claimant is None:
            claimant = o["opportunity_id"]
        left = round(max(0.0, left - amt), 2)
        dest = _split_destinations(amt, fund_headroom_gbp, _ov) if amt > 0 else []
        v_amt = sum(d["gbp"] for d in dest if d["ticker"] == "VUAG") if dest else amt
        sh = int(math.floor(v_amt / lim)) if (lim and v_amt > 0) else 0
        o.update(proposed_gbp=amt, sizing_basis=basis, limit_price=lim, shares_est=sh,
                 consideration_at_limit_gbp=round(sh * lim, 2) if lim else None,
                 destinations=dest, proposed_on=state.get("last_check"))
    return state


def actionable(state):
    return [o for o in _episode_opps(state) if o.get("actionable_now") and o.get("state") in (OPEN, APPROVED)]


def review(state, closes, *, ledger_entries=None, eligible_cash_gbp=None, today=None,
           fund_headroom_gbp=None, headroom_basis=None, overflow=None):
    """ONE review: reconcile fills -> ladder (crossings / episode) -> size proposals. -> (state, fired)."""
    state = _ensure_schema(state)
    reconcile_fills(state, ledger_entries or [], today=today)
    state, fired = update_ladder(state, closes, today=today)
    size_proposals(state, eligible_cash_gbp, fund_headroom_gbp=fund_headroom_gbp, headroom_basis=headroom_basis,
                   overflow=overflow)
    state["eligible_cash_gbp_at_review"] = (round(float(eligible_cash_gbp), 2)
                                            if eligible_cash_gbp is not None else None)
    state["protocol_active"] = protocol_on()
    return state, fired


def compute_reserve(cash_gbp, mmf_gbp=0.0, committed_gbp=0.0, buffer_gbp=500.0):
    """Eligible cash at the review (D11 arithmetic; no longer a saved reserve with authority - B04-R05)."""
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


def classify_regime(closes, dd_pct, dates=None):
    """B7 decision table on: VUAG vs its moving average, drawdown band, 63d slope sign.

    ⚑ B04-R08 (S3, EV-0120): the moving average is published with its ACTUAL observation count and
    endpoints. A 63-point average is labelled MA63 (PARTIAL_63_OF_200), never a measured 200-day average.
    The table, thresholds and lookback are unchanged (no retune without separate accepted analysis)."""
    closes = [float(c) for c in closes if c is not None]
    if len(closes) < 63:
        return None, {}
    last = closes[-1]
    n = min(len(closes), 200)
    ma200 = sum(closes[-n:]) / n
    above = "above" if last >= ma200 else "below"
    band = 0 if dd_pct > -5.0 else (1 if dd_pct > -15.0 else 2)
    slope = "+" if closes[-1] >= closes[-63] else "-"
    rules = _c("REGIME_RULES", DEFAULT_REGIME_RULES)
    key = (above, band, slope)
    regime = rules.get(key) or DEFAULT_REGIME_RULES.get(key)
    basis = {"above_200dma": above == "above", "dd_band": band, "slope_63d": slope,
             "ma200": round(ma200, 2), "last": round(last, 2),
             "ma_observations": n, "ma_label": "MA%d" % n, "ma200_measured": n >= 200,
             "ma_basis": "MEASURED_200" if n >= 200 else "PARTIAL_%d_OF_200" % n}
    if dates is not None and len(dates) >= n:
        basis["ma_start"] = str(dates[-n])[:10]
        basis["ma_end"] = str(dates[-1])[:10]
    return regime, basis


def emit_block(state, fired, reserve_gbp=None):
    """The DRAWDOWN_TRIGGER / standing-line block for run_context/intramonth/email.

    ⚑ B04-R07 (EV-0116): with the protocol OFF no capital opportunity, actionable state or trigger is
    emitted - only the diagnostic observation line. `reserve_gbp` is the eligible cash at this review
    (display only); amounts come from the opportunities themselves (no reserve division, B04-R09)."""
    state = _ensure_schema(state)
    on = protocol_on()
    ep = _episode_opps(state)
    act = actionable(state) if on else []
    crossed = sum(1 for v in (state.get("tranches_fired") or {}).values() if v)
    line = (f"Drawdown: {state.get('drawdown_pct', 0.0):+.1f}% from episode high "
            f"({state.get('episode_high')}; 252d high {state.get('high_252d')}) | thresholds crossed "
            f"{crossed}/{len(_thresholds())} | opportunities "
            + (f"{sum(1 for o in ep if o.get('state') in LIVE_STATES)} live ({len(act)} actionable, "
               f"{sum(1 for o in ep if o.get('state') == STARTED)} started, "
               f"{sum(1 for o in ep if o.get('state') == FULFILLED)} fulfilled)" if on else "SUPPRESSED (protocol OFF)")
            + f" | eligible cash {'£{:,.0f}'.format(reserve_gbp) if reserve_gbp is not None else 'n/a'} | "
            f"regime {state.get('regime_state') or 'n/a'}")
    block = {"standing_line": line, "protocol_active": on,
             "state": {k: state[k] for k in ("episode_high", "high_252d", "drawdown_pct", "episode_id",
                                             "tranches_fired", "regime_state") if k in state},
             "observations_this_review": list(fired or [])}
    if on:
        block["opportunities"] = [{k: o.get(k) for k in ("opportunity_id", "threshold_pct", "state",
                                                         "actionable_now", "paused_above_threshold",
                                                         "proposed_gbp", "approved_gbp", "sizing_basis",
                                                         "limit_price", "shares_est", "consideration_at_limit_gbp",
                                                         "destinations", "observed_on", "started_on", "fills")} for o in ep]
        block["review_capacity"] = state.get("review_capacity")
        if act:
            block["DRAWDOWN_TRIGGER"] = {
                "episode_id": state.get("episode_id"),
                "opportunities": [{"opportunity_id": o["opportunity_id"], "threshold_pct": o["threshold_pct"],
                                   "state": o["state"], "proposed_gbp": o.get("proposed_gbp"),
                                   "sizing_basis": o.get("sizing_basis"), "limit_price": o.get("limit_price"),
                                   "shares_est": o.get("shares_est"), "destinations": o.get("destinations"),
                                   "consideration_at_limit_gbp": o.get("consideration_at_limit_gbp")} for o in act],
                "newly_observed_thresholds": list(fired or []),
                "instrument": "VUAG (D13 — held, UCITS/preclearance-exempt, GBP line); conditional fallback per Raj 10-Oct",
                "order": "limit at last close +0.5%; shares FLOORED so the consideration never exceeds the amount",
                "ledger_route": "drawdown_protocol",
                "precedence": "actionable opportunities take their proposed pounds BEFORE Step-10 stock deployments (B1.6b)",
                "authority": "PROPOSAL - executes only on the review's approval; an unfilled opportunity is retained until a new episode high",
            }
        if state.get("fresh_review") and state["fresh_review"].get("episode_id") == state.get("episode_id"):
            block["DRAWDOWN_REVIEW"] = state["fresh_review"]
    return block


def run(reserve_gbp=None, check_only=False, state_path=STATE_PATH, ledger_path=None):
    import yfinance as yf
    t = yf.Ticker("VUAG.L")
    hist = t.history(period="2y")
    closes = list(hist["Close"].dropna()) if hist is not None and len(hist) else []
    if not closes:
        print("DRAWDOWN_MONITOR ERROR: no VUAG history", file=sys.stderr)
        return 1
    state = load_state(state_path)
    try:
        led = json.load(open(ledger_path or os.path.join(HERE, "transaction_ledger.json"), encoding="utf-8")).get("entries") or []
    except Exception:
        led = []
    # ⚑ EV-0118 / B04-R05: the standalone path no longer re-uses a SAVED reserve - without --reserve the
    #   eligible cash is unknown and actionable opportunities read UNMEASURED.
    # ⚑ A18/ISA-0498: the standalone (weekly-screen) path must not pull capital_destination into the screen's
    #   fallback import closure - the VUAG headroom is supplied by the monthly pre-run (one home:
    #   capital_destination.vuag_drawdown_headroom). Without it, a prior review's ceiling is CARRIED as a
    #   reservation only, or the amount reads UNMEASURED.
    state, fired = review(state, closes, ledger_entries=led, eligible_cash_gbp=reserve_gbp,
                          fund_headroom_gbp=None, headroom_basis="NOT_SUPPLIED_STANDALONE")
    regime, basis = classify_regime(closes, state["drawdown_pct"], dates=list(hist.index))
    state["regime_state"] = regime
    state["regime_basis"] = basis
    if not check_only and protocol_on():
        save_state(state, state_path)          # EV-0115 safeguard kept: the standalone path does not write when OFF
    block = emit_block(state, fired, reserve_gbp)
    print(json.dumps(block, indent=2, default=str))
    return 0


def _selftest():
    fails = []

    def ok(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)[:200]) if (detail and not cond) else ""))
        if not cond:
            fails.append(name)
    D = lambda d: date(2026, 10, d)                                     # noqa: E731
    base = [100.0] * 260

    def fresh():
        return load_state("/nonexistent")
    # ── policy: 10% -> one opportunity; 20% -> two more; 20% first retains all three; 30% -> review, no fourth
    st, f = update_ladder(fresh(), base, today=D(1))
    ok("positive control: a healthy high creates nothing", f == [] and st["opportunities"] == [])
    st, f = update_ladder(st, base + [89.0], today=D(2))
    ok("10% crossing creates ONE opportunity (Raj 10-Oct)", [o["opportunity_id"] for o in st["opportunities"]] == ["OPP-E1-T10-1"], st["opportunities"])
    st, f = update_ladder(st, base + [89.0, 79.0], today=D(3))
    ok("20% crossing creates TWO more (three in all)", len(_episode_opps(st)) == 3)
    st, f = update_ladder(st, base + [89.0, 79.0, 69.0], today=D(4))
    ok("negative control: 30% must not create a fourth opportunity", len(_episode_opps(st)) == 3 and f == ["t30"])
    ok("30% triggers a FRESH REVIEW", (st.get("fresh_review") or {}).get("episode_id") == 1)
    s2, _ = update_ladder(fresh(), base, today=D(1))
    s2, f2 = update_ladder(s2, base + [78.0], today=D(2))
    ok("reaching 20% first retains all three (EV-0112: no single divided tranche)", len(_episode_opps(s2)) == 3 and f2 == ["t10", "t20"])
    # ── EV-0106/0117: an unfunded crossing is not consumed; a later funded review proposes the SAME opportunity
    s3, _ = update_ladder(fresh(), base, today=D(1))
    s3, _ = review(s3, base + [89.0], eligible_cash_gbp=0.0, fund_headroom_gbp=50000.0, today=D(2))
    o = _episode_opps(s3)[0]
    ok("negative control: no legal funding must not consume the opportunity (stays OPEN)", o["state"] == OPEN and o["proposed_gbp"] == 0.0)
    s3, f3 = review(s3, base + [89.0, 88.5], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(3))
    o = _episode_opps(s3)[0]
    ok("EV-0107: a repeated unfilled opportunity is proposed again at the next review (option a: all eligible cash)",
       f3 == [] and o["actionable_now"] and o["proposed_gbp"] == 9000.0, o)
    s3x, _ = review(json.loads(json.dumps(s3)), base + [89.0, 88.5], eligible_cash_gbp=9000.0, fund_headroom_gbp=4000.0, today=D(3))
    ok("option a: the VUAG approved-limit headroom caps the review's capacity", _episode_opps(s3x)[0]["proposed_gbp"] == 4000.0)
    s3y, _ = update_ladder(fresh(), base, today=D(1))
    s3y, _ = review(s3y, base + [89.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=None, today=D(2))
    ok("negative control: an unknown VUAG headroom must not propose a number", _episode_opps(s3y)[0]["proposed_gbp"] is None)
    s3z, _ = update_ladder(fresh(), base, today=D(1))
    s3z, _ = review(s3z, base + [78.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(2))
    _pp = [o["proposed_gbp"] for o in _episode_opps(s3z)]
    ok("negative control: three actionable opportunities must not claim the review's pounds three times",
       sum(_pp) == 9000.0 and _pp.count(0.0) == 2 and all(o["sizing_basis"].startswith(("RAJ_24OCT", "QUEUED")) for o in _episode_opps(s3z)), _pp)
    # ── EV-0108: recover above the threshold -> PAUSED (retained); recross -> actionable again
    s3, _ = review(s3, base + [89.0, 88.5, 95.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(4))
    o = _episode_opps(s3)[0]
    ok("above its threshold the opportunity is PAUSED, not dropped", o["state"] == OPEN and o["paused_above_threshold"] and o["proposed_gbp"] is None)
    s3, _ = review(s3, base + [89.0, 88.5, 95.0, 88.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(5))
    ok("EV-0108: a recross makes the retained opportunity actionable again", _episode_opps(s3)[0]["actionable_now"])
    # ── EV-0109/0110: equality and window decay never start a new episode
    s4, _ = update_ladder(fresh(), base, today=D(1))
    s4, _ = update_ladder(s4, base + [89.0], today=D(2))
    s4, _ = update_ladder(s4, base + [89.0, 100.0], today=D(3))
    ok("R5.14 EPISODE T: a close EQUAL to the episode high does not reset", s4["episode_id"] == 1 and _episode_opps(s4)[0]["state"] == OPEN)
    s4b = json.loads(json.dumps(s4))
    s4b, _ = update_ladder(s4b, base + [89.0, 100.0, 99.99], today=D(4))
    ok("R5.14 EPISODE T-1: a close just BELOW the episode high does not reset", s4b["episode_id"] == 1)
    s4c = json.loads(json.dumps(s4))
    s4c, _ = update_ladder(s4c, base + [89.0, 100.0, 100.01], today=D(4))
    ok("R5.14 EPISODE T+1: a close just ABOVE the episode high starts a new episode and expires the unsubmitted opportunity",
       s4c["episode_id"] == 2 and s4["opportunities"][0]["opportunity_id"] == "OPP-E1-T10-1"
       and [o["state"] for o in s4c["opportunities"]] == [EXPIRED])
    s5, _ = update_ladder(fresh(), base, today=D(1))
    s5, _ = update_ladder(s5, [95.0] * 300, today=D(2))
    ok("EV-0110: an aged-out window high does not reset the episode or the drawdown reference",
       s5["episode_id"] == 1 and s5["episode_high"] == 100.0 and s5["drawdown_pct"] == -5.0 and s5["high_252d"] == 95.0, s5)
    rep1 = update_ladder(json.loads(json.dumps(s4)), base + [89.0, 100.0], today=D(3))[0]
    ok("R5.14 EPISODE repeat_same_occurrence: the same closes give the same state", rep1["episode_id"] == s4["episode_id"])
    nxt = update_ladder(json.loads(json.dumps(s4)), base + [89.0, 100.0, 88.0], today=date(2026, 10, 31))[0]
    ok("R5.14 EPISODE next_occurrence: at the 2026-10-31 pre-run the retained opportunity is actionable", _episode_opps(nxt)[0]["actionable_now"])
    # ── fills: first fill STARTED, child fills belong to it, FULFILLED at the approved amount, no early fill counts
    s6, _ = update_ladder(fresh(), base, today=D(1))
    s6, _ = review(s6, base + [89.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(2))
    approve_opportunity(s6, "OPP-E1-T10-1", 2000.0, "test", on="2026-10-02")
    led = [{"type": "buy", "ticker": "VUAG", "date": "2026-09-30", "amount_gbp": 999.0, "reference": "OLD"},
           {"type": "buy", "ticker": "VUAG", "date": "2026-10-03", "amount_gbp": 1000.0, "reference": "F1"}]
    reconcile_fills(s6, led, today=D(3))
    o = _episode_opps(s6)[0]
    ok("negative control: a fill dated before the observation must not count", [f["reference"] for f in o["fills"]] == ["F1"])
    ok("first confirmed fill marks STARTED", o["state"] == STARTED)
    reconcile_fills(s6, led + [{"type": "buy", "ticker": "VUAG.L", "date": "2026-10-04", "amount_gbp": 900.0, "reference": "F2"}], today=D(4))
    ok("a child fill belongs to the STARTED opportunity and completes it at the approved amount", o["state"] == FULFILLED and o["fulfilled_basis"] == "APPROVED_AMOUNT")
    reconcile_fills(s6, led + [{"type": "buy", "ticker": "VUAG.L", "date": "2026-10-04", "amount_gbp": 900.0, "reference": "F2"}], today=D(5))
    ok("negative control: a replayed fill must not be counted twice", len(o["fills"]) == 2)
    # ── OFF switch
    _save = _c("DRAWDOWN_PROTOCOL_ACTIVE", True)
    try:
        if _cfg is not None:
            _cfg.DRAWDOWN_PROTOCOL_ACTIVE = False
        s7, _ = update_ladder(fresh(), base, today=D(1))
        s7, f7 = review(s7, base + [89.0], eligible_cash_gbp=9000.0, fund_headroom_gbp=50000.0, today=D(2))
        b7 = emit_block(s7, f7, 9000.0)
        ok("negative control: protocol OFF must not emit a capital trigger or create an opportunity (EV-0116)",
           "DRAWDOWN_TRIGGER" not in b7 and "opportunities" not in b7 and s7["opportunities"] == [] and f7 == ["t10"], b7)
    finally:
        if _cfg is not None:
            _cfg.DRAWDOWN_PROTOCOL_ACTIVE = _save
    # ── affordable units (EV-0121)
    s8, _ = update_ladder(fresh(), base, today=D(1))
    s8, _ = review(s8, base + [89.0], eligible_cash_gbp=3000.0, fund_headroom_gbp=50000.0, today=D(2))
    o = _episode_opps(s8)[0]
    ok("negative control: the share estimate must not exceed the amount at the quoted limit",
       o["shares_est"] * o["limit_price"] <= o["proposed_gbp"] + 1e-9 and o["shares_est"] == 33, o)
    b8 = emit_block(s8, [], 9000.0)
    ok("an actionable opportunity emits a trigger carrying its own amount and sizing basis",
       b8["DRAWDOWN_TRIGGER"]["opportunities"][0]["sizing_basis"].startswith("RAJ_24OCT2026_OPTION_A"))
    # ── unknown cash -> UNMEASURED, never zero
    s9, _ = update_ladder(fresh(), base, today=D(1))
    s9, _ = review(s9, base + [89.0], eligible_cash_gbp=None, fund_headroom_gbp=50000.0, today=D(2))
    ok("negative control: unknown eligible cash must not propose a number", _episode_opps(s9)[0]["proposed_gbp"] is None
       and _episode_opps(s9)[0]["sizing_basis"].startswith("UNMEASURED"))
    s9, _ = review(s9, base + [89.0, 88.0], eligible_cash_gbp=5000.0, fund_headroom_gbp=50000.0, today=D(3))
    s9, _ = review(s9, base + [89.0, 88.0, 88.5], eligible_cash_gbp=None, fund_headroom_gbp=50000.0, today=D(4))
    ok("a standalone check without cash keeps the last review's ceiling as a reservation (labelled carried)",
       _episode_opps(s9)[0]["proposed_gbp"] == 5000.0 and _episode_opps(s9)[0]["sizing_basis"].startswith("CARRIED_FROM_REVIEW 2026-10-03"))
    # ── migration preserves history and manufactures nothing (B04-R09)
    v1 = {"high_252d": 114.36, "episode_id": 1, "tranches_fired": {"t10": False, "t20": False, "t30": False},
          "reserve_gbp": 17470.94, "last_check": "2026-10-24", "tranche_log": [{"episode_id": 0, "key": "t10"}]}
    m = _ensure_schema(json.loads(json.dumps(v1)))
    ok("migration keeps history, retires the saved reserve as authority and creates no opportunity",
       m["opportunities"] == [] and m["legacy_tranche_log"] == v1["tranche_log"] and "reserve_gbp" not in m
       and m["legacy_reserve_gbp_v1"]["gbp"] == 17470.94 and m["episode_high"] == 114.36)
    # ── reserve arithmetic (eligible cash) unchanged
    ok("eligible cash arithmetic", compute_reserve(10778.0, 0.0, 0.0) == 10278.0 and compute_reserve(400.0) == 0.0)
    # ── B7 classifier unchanged + honest MA labelling (EV-0120)
    up = [float(x) for x in range(100, 360)]
    r, b = classify_regime(up, 0.0)
    ok("classifier: rising above MA -> RISK_ON with a MEASURED 200", r == "RISK_ON" and b["ma200_measured"] and b["ma_observations"] == 200)
    r, b = classify_regime([100.0 + i * 0.01 for i in range(63)], -1.0)
    ok("negative control: a 63-point average must not be labelled a measured 200-day MA",
       b["ma_observations"] == 63 and b["ma200_measured"] is False and b["ma_basis"] == "PARTIAL_63_OF_200")
    down = [float(300 - i * 0.5) for i in range(260)]
    ok("classifier: falling below MA deep -> RISK_OFF", classify_regime(down, -16.0)[0] == "RISK_OFF")
    vshape = [300.0] * 150 + [float(300 - i * 3) for i in range(50)] + [float(150 + i * 1.0) for i in range(60)]
    ok("classifier: trough bounce -> RECOVERY", classify_regime(vshape, -20.0)[0] == "RECOVERY")
    print("drawdown_monitor selftest: %d FAIL(s)" % len(fails)
          + ("" if fails else " - drawdown_monitor SELF-TEST OK (S3/B04 lifecycle + U-B7-1)"))
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reserve", type=float, default=None, help="eligible cash at this review (GBP)")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    sys.exit(run(a.reserve, a.check_only, a.state))
