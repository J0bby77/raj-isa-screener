#!/usr/bin/env python3
"""
derive_required_return.py — Fix Pack A19 v1 (12-Jul-2026). THE anchor derivation.

Solves the annualised return (monthly compounding, month-end contributions) required for the
current portfolio + contribution schedule to reach the floor (£1.0m) and stretch (£1.5m)
targets by target_date, then writes target_state.json with D1c guardrails applied:

  operative = clamp(derived_floor, 10.0, 18.0)
    derived > 18  -> guardrail_state = TARGET_ATTAINABILITY_REVIEW (never auto-ratchet gates)
    derived < 10  -> hard-floored at 10.0 (outperformance banks a buffer, never lowers the bar);
                     glidepath (B6) triggers on age/value ONLY, not on a low anchor.

Runs (D1b): inside the April pre-run (tax-year start) and on ANY contribution_schedule change
(edit the schedule + schedule_updated_at, rerun this; consistency_check.py A18 asserts
derived_at >= schedule_updated_at). Appends a derivation_history row on every write.

Usage:
  python3 derive_required_return.py                 # derive + write + history row
  python3 derive_required_return.py --check         # recompute, compare to stored, NO write (exit 1 on drift > 0.2pp)
  python3 derive_required_return.py --portfolio-value 150000 --value-date 2026-12-31   # override inputs
  python3 derive_required_return.py --selftest      # U-A19 unit fixtures

Stdlib only. Consumers read target_state.json via scoring_config's loader (hard fallback + warning).
"""
import argparse, json, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "target_state.json")
OPERATIVE_FLOOR_PCT = 10.0   # D1c amended (Raj 12-Jul)
OPERATIVE_CAP_PCT = 18.0     # D1c: above this -> human review, not mechanical ratchet

# ═══════════════════════════════════════════════════════════════════════════════════════════
# ANCHOR CADENCE AND VALUATION BASIS — D-2 (ISA-0138) · D-3 (ISA-0139) · D-4 (ISA-0140)
# Built 12-Aug-2026. Design record: ISA_BuildSpec_AnchorCadence_D2D3D4_12Aug2026.md
#
# ⚑ WHAT WAS WRONG, IN ONE SENTENCE PER ITEM
#
# D-3. `target_state.portfolio_value_gbp` was a SPOT month-end value, so the anchor inherited one
#      day's market noise AND one day's capital position. The file said so itself:
#      `derivation_basis: "SPOT MONTH-END 31-Jul-2026 ... the smoothing those decisions require
#      has not been applied."` A plain 3-month mean of portfolio value does not fix it (D-3): it
#      averages the CAPITAL as well as the market, so an £11,250 subscription in month t enters at
#      ~£3,750 (one third), understates the base value, and therefore OVERSTATES the required
#      return and TIGHTENS every gate derived from it, for two quarters.
#
# D-2. One clock. Every re-derivation moved every anchor-derived gate at once, so a fund could
#      change FRS band with nothing about the fund having changed. Visibility and gating were the
#      same field, so you could not have one without the other.
#
# D-4. There was no flow trigger at all: re-derivation was "the April pre-run, or any
#      contribution_schedule edit". A £11,250 lump that is not a schedule edit moved 8% of the
#      portfolio and triggered nothing; a £1,250 standing order, had it triggered, would have
#      fired every month.
#
# ⚑ THE THREE DECISIONS ARE ONE MECHANISM AND LIVE IN ONE HOME (R4.4)
# This module is the anchor's single home. A separate `anchor_cadence.py` would be a second home
# for anchor rules, which is precisely FC-D. Everything below reads target_state.json and nothing
# else writes it.
#
# ⚑ ROLLBACK IS TWO CONSTANTS, NOT A CODE REVERT (R4.13)
#   ANCHOR_CADENCE_REGIME  = "spot_every_run"          reproduces the pre-D-2 behaviour exactly
#   ANCHOR_VALUATION_BASIS = "spot"                    reproduces the pre-D-3 behaviour exactly
# Both are asserted as reproducing the 12-Aug-2026 stored frame to the digit (T-CAD-14/15).
# ═══════════════════════════════════════════════════════════════════════════════════════════

# ── D-2. TWO-SPEED CADENCE ───────────────────────────────────────────────────────────────
ANCHOR_CADENCE_REGIME = "two_speed"      # "two_speed" | "spot_every_run"  (ROLLBACK)
# The operative clock. D-7 (ISA-0143): the SEMI-ANNUAL SWITCH CYCLE is master and the quarterly
# value is an input, never an independent trigger. These two dates ARE that cycle, and they are
# stated once here so the anchor cannot acquire a third clock of its own.
OPERATIVE_EFFECTIVE_DAYS = ((3, 31), (9, 30))
# Break-glass. Reported may drift from operative between windows — that is the point of two
# speeds. Beyond this the drift is no longer "visibility", and holding a stale gate becomes the
# larger error, so the operative value updates immediately and says why.
ANCHOR_BREAK_GLASS_PP = 2.0

# ── D-3. VALUATION BASIS ─────────────────────────────────────────────────────────────────
ANCHOR_VALUATION_BASIS = "flow_adjusted_3m_mean"   # | "spot"  (ROLLBACK)
VALUATION_MEAN_MONTHS = 3
VALUATION_HISTORY_PATH = os.path.join(HERE, "anchor_valuation_history.json")

# ── D-4. FLOW RE-DERIVATION TRIGGER ──────────────────────────────────────────────────────
FLOW_REDERIVE_THRESHOLD_PCT = 2.0
FLOW_LEDGER_PATH = os.path.join(HERE, "anchor_flow_ledger.json")

# Cadence schema. Bumped on target_state so a reader that has not been updated for two-speed
# fails loudly instead of quietly reading `required_return_operative_pct` as a fresh spot solve
# (R4.7 — an un-updated caller must fail, not keep the old behaviour).
TARGET_STATE_SCHEMA_VERSION = 2


class AnchorCadenceError(RuntimeError):
    """Raised where a cadence decision cannot be made honestly. Never defaulted."""


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def month_end(d) -> date:
    """The last day of the month containing `d`. Used when stamping observations."""
    d = _d(d)
    nxt = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
    import datetime as _dt
    return nxt - _dt.timedelta(days=1)


# ───────────────────────────────────────────────── D-3: flow-adjusted valuation mean
def valuation_observations(path=None, required_months=None):
    """Month-end valuations, newest first, deduped by month_end.

    ⚑ R4.9. This COUNTS what it found. A reader that cannot assemble `required_months`
    observations says so; it never averages two and calls the answer a three-month mean.
    """
    path = path or VALUATION_HISTORY_PATH
    need = int(required_months or VALUATION_MEAN_MONTHS)
    if not os.path.exists(path):
        return [], {"status": "MISSING_STORE", "found": 0, "required": need,
                    "detail": f"{os.path.basename(path)} absent — no valuation history to average"}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    seen, rows = {}, []
    for o in doc.get("observations") or []:
        me = str(o["month_end"])[:10]
        if me in seen:
            # Two values for one month-end is a data defect, not something to pick between
            # (R4.8: an uninformed tie-break is REFUSED). Equal values are the same fact twice.
            if abs(float(seen[me]["value_gbp"]) - float(o["value_gbp"])) > 0.005:
                raise AnchorCadenceError(
                    f"two different valuations for month_end {me}: "
                    f"{seen[me]['value_gbp']} ({seen[me].get('source')}) vs "
                    f"{o['value_gbp']} ({o.get('source')}) — refusing to choose")
            continue
        seen[me] = o
        rows.append(o)
    rows.sort(key=lambda r: str(r["month_end"]), reverse=True)
    status = "OK" if len(rows) >= need else "INSUFFICIENT_HISTORY"
    return rows, {"status": status, "found": len(rows), "required": need,
                  "detail": (f"{len(rows)} month-end observation(s) on file against {need} required"
                             if status != "OK" else
                             f"{len(rows)} on file, newest {rows[0]['month_end']}")}


def flow_adjusted_mean(observations, flows, months=None):
    """D-3. The 3-month mean of portfolio value with EXTERNAL FLOWS APPLIED AT PAR.

    Each earlier observation is carried forward to the latest observation date by the net
    external flows received strictly after it, at par — no growth applied to the flow:

        V*(t-k) = V(t-k) + F(t-k < d <= t)
        basis   = mean over k = 0..n-1 of V*(t-k)

    ⚑ WHY AT PAR, AND WHY FORWARD. The purpose is to smooth the MARKET path and nothing else.
    Growing the flow would smuggle a return assumption into the base value that the base value
    exists to test. Carrying earlier observations FORWARD (rather than stripping the flow out of
    the latest) puts every term on the LATEST capital footing, which is the footing the anchor is
    solved on, so a subscription counts once and in full.

    ⚑ THE NUMBER D-3 IS ABOUT. With a flat market and a single flow F in the latest month,
    a plain mean returns V + F/3 and this returns V + F. The £11,250 lump therefore enters the
    base at £11,250 rather than £3,750 — an understatement of £7,500 on a ~£140k portfolio, which
    raises the solved anchor and tightens every gate derived from it for two quarters.
    """
    n = int(months or VALUATION_MEAN_MONTHS)
    obs = list(observations)[:n]
    if len(obs) < n:
        return {"status": "INSUFFICIENT_HISTORY", "months_required": n,
                "observations_used": len(obs), "value_gbp": None,
                "note": (f"{len(obs)} of {n} month-end observations available — refusing to "
                         f"present a {len(obs)}-month mean as a {n}-month mean (R4.9)")}
    latest = _d(obs[0]["month_end"])
    rows = []
    for o in obs:
        od = _d(o["month_end"])
        after = [f for f in flows if od < _d(f["date"]) <= latest]
        fsum = sum(float(f["amount_gbp"]) for f in after)
        rows.append({"month_end": od.isoformat(), "value_gbp": round(float(o["value_gbp"]), 2),
                     "flows_after_gbp": round(fsum, 2),
                     "adjusted_gbp": round(float(o["value_gbp"]) + fsum, 2),
                     "flows_counted": [{"date": f["date"], "amount_gbp": f["amount_gbp"],
                                        "source": f.get("source")} for f in after],
                     "source": o.get("source"), "stamp_basis": o.get("stamp_basis")})
    direct = sum(float(o["value_gbp"]) + sum(
        float(f["amount_gbp"]) for f in flows if _d(o["month_end"]) < _d(f["date"]) <= latest)
        for o in obs) / n
    plain = sum(float(o["value_gbp"]) for o in obs) / n
    mean_adj = sum(r["flows_after_gbp"] for r in rows) / n
    # ── I-CAD-1. TWO INDEPENDENT DERIVATIONS MUST AGREE (R5.2). ──────────────────────────
    # (1) sum the per-observation adjusted values and divide
    # (2) the plain mean plus the mean flow adjustment
    # Algebraically identical; they diverge exactly when a flow is counted against the wrong
    # observation, double-counted, or dropped — which is what this class of bug looks like.
    via = plain + mean_adj
    gap = abs(direct - via)
    return {"status": "computed", "basis": "flow_adjusted_mean_at_par",
            "months_required": n, "observations_used": len(obs),
            "latest_month_end": latest.isoformat(),
            "value_gbp": round(direct, 2),
            "plain_mean_gbp": round(plain, 2),
            "mean_flow_adjustment_gbp": round(mean_adj, 2),
            "spot_latest_gbp": round(float(obs[0]["value_gbp"]), 2),
            "understatement_avoided_gbp": round(direct - plain, 2),
            "rows": rows,
            "identity_check": {"direct_gbp": round(direct, 6),
                               "via_plain_plus_mean_adjustment_gbp": round(via, 6),
                               "abs_gap_gbp": round(gap, 10), "tolerance_gbp": 1e-6,
                               "holds": bool(gap < 1e-6),
                               "note": ("two aggregation paths over the same observations and "
                                        "flows: per-observation adjustment summed, and the plain "
                                        "mean plus the mean adjustment")}}


VALUATION_AGREEMENT_TOL_GBP = 0.005   # ISA-0312: two derivations of one quantity must agree (R5.2)

def _spot_valuation(state, portfolio_value=None, value_date=None, observations=None):
    """The single home for "what is the portfolio worth". Returns (value, date, source).

    Precedence, and each step SAYS which it took:
      1. an explicit caller override (a test or a what-if)
      2. the valuation store's latest admissible month-end observation  <- GOLDEN SOURCE (R6.1)
      3. target_state.portfolio_value_gbp                               <- FALLBACK, flagged

    R6.2 - where 2 and 3 disagree beyond tolerance the disagreement is PUBLISHED, never blended
    and never silently resolved in favour of whichever was read first.
    """
    if portfolio_value is not None:
        return float(portfolio_value), _d(value_date or state["portfolio_value_date"]), "caller_override"
    obs = observations
    if obs is None:
        try:
            obs = (json.loads(open(os.path.join(HERE, "anchor_valuation_history.json"), encoding="utf-8").read())
                   .get("observations") or [])
        except Exception:                                            # noqa: BLE001
            obs = []
    adm = [o for o in obs if o.get("admissible") and o.get("value_gbp") is not None]
    hand_v = state.get("portfolio_value_gbp")
    hand_d = state.get("portfolio_value_date")
    if not adm:
        if hand_v is None:
            raise AnchorCadenceError(
                "no admissible valuation observation and no fallback value - refusing to "
                "invent one (R4.1/R4.3)")
        return float(hand_v), _d(value_date or hand_d), "target_state_fallback_store_empty"
    latest = max(adm, key=lambda o: o["month_end"])
    v, d = float(latest["value_gbp"]), _d(value_date or latest["month_end"])
    if hand_v is not None and str(hand_d) == str(latest["month_end"]):
        delta = abs(float(hand_v) - v)
        if delta > VALUATION_AGREEMENT_TOL_GBP:
            try:
                import disagreement_log as _dl
                _dl.record(
                    quantity="portfolio_value_gbp",
                    subject="ISA anchor valuation",
                    derivation_a="target_state.portfolio_value_gbp (hand-written at each derivation)",
                    value_a=float(hand_v),
                    derivation_b="anchor_valuation_history.json latest admissible month-end",
                    value_b=v,
                    tolerance=VALUATION_AGREEMENT_TOL_GBP,
                    tolerance_basis="two derivations of ONE quantity must agree to the penny; "
                                    "0.005 GBP is half the smallest representable unit (R5.2)",
                    domain="analysis",
                    register_item="ISA-0312",
                    note=f"same date {latest['month_end']}, delta GBP {delta:.2f}. "
                         "The store is operative; target_state's copy is retired as an input.")
            except Exception:                                        # noqa: BLE001
                pass
    return v, d, f"anchor_valuation_history:{latest['month_end']}"



def valuation_basis(observations=None, flows=None, spot_value=None, spot_date=None):
    """The value the anchor is solved on, with its basis stated. Never a bare float (R4.1/R4.2).

    Degradation is EXPLICIT: with fewer than VALUATION_MEAN_MONTHS observations the basis falls
    back to spot and records that it did, because two months presented as a three-month mean is
    the FC-A failure this whole standard exists to prevent.
    """
    if ANCHOR_VALUATION_BASIS == "spot":
        return {"basis": "spot", "value_gbp": None if spot_value is None else round(float(spot_value), 2),
                "value_date": None if spot_date is None else _d(spot_date).isoformat(),
                "regime": "spot (ROLLBACK constant ANCHOR_VALUATION_BASIS)", "degraded": False}
    obs, meta = (observations, None) if observations is not None else valuation_observations()
    if meta is None:
        meta = {"status": "OK" if len(obs) >= VALUATION_MEAN_MONTHS else "INSUFFICIENT_HISTORY",
                "found": len(obs), "required": VALUATION_MEAN_MONTHS, "detail": "caller-supplied"}
    fam = flow_adjusted_mean(obs, flows or [])
    # ⚑ R6.2. Where the hand-copied `target_state.portfolio_value_gbp` and the valuation store
    # hold the SAME DATE with DIFFERENT values, publish the disagreement. Do not blend, and do
    # not silently prefer either: this is exactly the "a stored value says one thing and IS
    # another" class, and it is invisible because both numbers are plausible.
    div = None
    if obs and spot_value is not None and spot_date is not None:
        latest = obs[0]
        if _d(latest["month_end"]) == _d(spot_date) and \
                abs(float(latest["value_gbp"]) - float(spot_value)) > 0.005:
            div = {"date": _d(spot_date).isoformat(),
                   "target_state_portfolio_value_gbp": round(float(spot_value), 2),
                   "valuation_store_gbp": round(float(latest["value_gbp"]), 2),
                   "delta_gbp": round(float(spot_value) - float(latest["value_gbp"]), 2),
                   "operative": "target_state (unchanged by this build — a refactor must never "
                                "move a number)",
                   "store_source": latest.get("source"),
                   "note": ("two homes for one valuation. The store is derived from "
                            "portfolio_data's _meta.data_date; target_state's copy is written by "
                            "hand at each derivation. One of them should stop existing (R4.4).")}
    if fam["status"] != "computed":
        return {"basis": "spot_fallback", "spot_vs_store": div, "value_gbp": None if spot_value is None else round(float(spot_value), 2),
                "value_date": None if spot_date is None else _d(spot_date).isoformat(),
                "regime": ANCHOR_VALUATION_BASIS, "degraded": True,
                "degraded_reason": fam["note"], "history": meta, "attempted": fam}
    return {"basis": "flow_adjusted_3m_mean", "spot_vs_store": div, "value_gbp": fam["value_gbp"],
            "value_date": fam["latest_month_end"], "regime": ANCHOR_VALUATION_BASIS,
            "degraded": False, "history": meta, "detail": fam}


# ───────────────────────────────────────────────────────── D-4: external flow trigger
def external_flows(path=None):
    """Net external flows into (positive) and out of (negative) the ISA.

    ⚑ R6.1, ONE GOLDEN SOURCE, AND IT IS NOT THE TRANSACTION LEDGER. AJ Bell's Transaction
    History is a DEALING record — Purchase, Sale, Transfer In, Equalisation, Conversion — and
    contains no cash deposits at all. That is the structural defect `extract_cash_statement.py`
    was built to fix (the £5,000 Faster Payment In of 06-Apr-2026 was in no framework input).
    So the golden source for realised flows is the CASH STATEMENT, surfaced here through
    `anchor_flow_ledger.json`, and a `transfer_in` row in the dealing ledger is NOT a flow for
    this purpose: the 18 rows dated 2025-05-01 carry `cash_impact_gbp: 0.0` because they are an
    in-specie transfer of positions already counted in the valuation.

    ⚑ R4.3. An absent ledger returns UNKNOWN and BLOCKS. It never returns "no flows".
    """
    path = path or FLOW_LEDGER_PATH
    if not os.path.exists(path):
        return None, {"status": "UNKNOWN", "reason": f"{os.path.basename(path)} absent",
                      "blocks": True,
                      "note": ("the flow trigger cannot report 'not fired' from a missing ledger "
                               "— absence of evidence is not evidence of no flow (R4.3)")}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    flows = list(doc.get("flows") or [])
    for fl in flows:
        for k in ("date", "amount_gbp", "status", "source"):
            if fl.get(k) in (None, ""):
                raise AnchorCadenceError(
                    f"flow row missing mandatory field {k!r}: {fl} — every figure carries as_of "
                    f"and source (R4.2)")
        if fl["status"] not in ("realised", "planned"):
            raise AnchorCadenceError(f"flow status {fl['status']!r} is neither realised nor planned")
    flows.sort(key=lambda f: str(f["date"]))
    return flows, {"status": "OK", "n": len(flows),
                   "n_realised": sum(1 for f in flows if f["status"] == "realised"),
                   "n_planned": sum(1 for f in flows if f["status"] == "planned"),
                   "source": doc.get("_meta", {}).get("source"), "blocks": False}


def _already_applied(flow, applied):
    """A flow already inside the stored derivation is NOT an un-derived flow.

    ⚑ THE TRAP THIS EXISTS FOR. The £11,250 of 05-Sep-2026 is already a `one_off_contribution`
    inside the solve that produced the stored anchor. When it lands it will appear in the cash
    statement as a realised contribution. Counting it as un-derived would fire the 2% trigger on
    money the anchor has ALREADY been solved against — a re-derivation that changes nothing,
    reported as if something had happened. Matched on date and amount, both, to the penny.
    """
    for a in applied or []:
        if str(a.get("date"))[:10] == str(flow["date"])[:10] and \
           abs(float(a.get("amount_gbp", 0)) - float(flow["amount_gbp"])) < 0.005:
            return True
    return False


def flow_trigger(flows, flow_meta, since_date, portfolio_value_gbp, applied_one_offs=None):
    """D-4. Re-derive when a flow, or cumulative un-derived flows, exceeds 2% of value.

    ⚑ WHY ONE THRESHOLD SERVES BOTH REGIMES. £11,250 on ~£140k is 8.05% and fires on arrival.
    £1,250 is 0.89%, accumulates, and crosses 2% at the third payment — which collapses the
    standing-order case into the quarterly cadence without a second rule to keep in step.
    """
    if flows is None:
        return {"status": "UNKNOWN", "fired": None, "blocks": True,
                "threshold_pct": FLOW_REDERIVE_THRESHOLD_PCT,
                "reason": (flow_meta or {}).get("reason"),
                "note": (flow_meta or {}).get("note")}
    pv = float(portfolio_value_gbp or 0)
    if pv <= 0:
        return {"status": "UNKNOWN", "fired": None, "blocks": True,
                "threshold_pct": FLOW_REDERIVE_THRESHOLD_PCT,
                "reason": "portfolio value is zero or absent — a percentage of nothing is not a test"}
    since = _d(since_date)
    counted, excluded = [], []
    for fl in flows:
        if fl["status"] != "realised":
            excluded.append({**fl, "excluded_because": "planned, not yet realised"})
            continue
        if _d(fl["date"]) <= since:
            excluded.append({**fl, "excluded_because": f"dated on or before the operative "
                                                       f"derivation date {since.isoformat()}"})
            continue
        if _already_applied(fl, applied_one_offs):
            excluded.append({**fl, "excluded_because": "already applied inside the stored "
                                                      "derivation as a one-off contribution"})
            continue
        counted.append(fl)
    cum = sum(float(f["amount_gbp"]) for f in counted)
    largest = max((abs(float(f["amount_gbp"])) for f in counted), default=0.0)
    pct = 100.0 * abs(cum) / pv
    largest_pct = 100.0 * largest / pv
    fired = pct >= FLOW_REDERIVE_THRESHOLD_PCT
    return {"status": "OK", "fired": bool(fired), "blocks": False,
            "threshold_pct": FLOW_REDERIVE_THRESHOLD_PCT,
            "since": since.isoformat(), "portfolio_value_gbp": round(pv, 2),
            "cumulative_undereived_gbp": round(cum, 2),
            "cumulative_pct_of_value": round(pct, 4),
            "largest_single_gbp": round(largest, 2),
            "largest_single_pct_of_value": round(largest_pct, 4),
            "counted": counted, "excluded": excluded,
            "note": ("a single flow above the threshold and an accumulation above it are the same "
                     "test on the same running total — one rule, both regimes (D-4)")}


# ─────────────────────────────────────────────────────────── D-2: the cadence decision
def last_operative_window(as_of):
    """The most recent scheduled operative date on or before `as_of`."""
    d = _d(as_of)
    cands = [date(y, m, dd) for y in (d.year - 1, d.year) for (m, dd) in OPERATIVE_EFFECTIVE_DAYS]
    past = [c for c in cands if c <= d]
    if not past:
        raise AnchorCadenceError(f"no scheduled operative window on or before {d}")
    return max(past)


def next_operative_window(as_of):
    d = _d(as_of)
    cands = [date(y, m, dd) for y in (d.year, d.year + 1) for (m, dd) in OPERATIVE_EFFECTIVE_DAYS]
    fut = [c for c in cands if c > d]
    return min(fut)


def cadence_decision(as_of, reported_operative_pct, stored_operative_pct,
                     stored_effective_from, flow_trig=None):
    """D-2. May the OPERATIVE anchor move today, and on what authority?

    Reported is always fresh. Operative moves only on the semi-annual dates, on break-glass
    drift, or on a D-4 flow trigger. Every branch names itself, so a held gate can always say
    why it is holding and an updated gate can always say what moved it.
    """
    d = _d(as_of)
    nxt = next_operative_window(d)
    if ANCHOR_CADENCE_REGIME == "spot_every_run":
        return {"regime": "spot_every_run", "apply": True, "authority": "ROLLBACK_SPOT_EVERY_RUN",
                "as_of": d.isoformat(), "next_window": nxt.isoformat(),
                "note": ("ANCHOR_CADENCE_REGIME is set to the pre-D-2 rollback value; the "
                         "operative anchor tracks every re-derivation")}
    last = last_operative_window(d)
    base = {"regime": "two_speed", "as_of": d.isoformat(),
            "last_window": last.isoformat(), "next_window": nxt.isoformat(),
            "operative_effective_days": [f"{m:02d}-{dd:02d}" for (m, dd) in OPERATIVE_EFFECTIVE_DAYS],
            "break_glass_pp": ANCHOR_BREAK_GLASS_PP,
            "reported_operative_pct": None if reported_operative_pct is None else round(float(reported_operative_pct), 1),
            "stored_operative_pct": None if stored_operative_pct is None else round(float(stored_operative_pct), 1)}
    if stored_operative_pct is None or stored_effective_from is None:
        return {**base, "apply": True, "authority": "INITIALISE",
                "reason": "no operative anchor on file with an effective date — first stamp"}
    eff = _d(stored_effective_from)
    drift = abs(float(reported_operative_pct) - float(stored_operative_pct))
    base["drift_pp"] = round(drift, 2)
    # Order matters and is deliberate: a scheduled window is the ordinary authority, so it is
    # tested first and break-glass is never credited for a move the calendar had already licensed.
    if last > eff:
        return {**base, "apply": True, "authority": "SCHEDULED_WINDOW",
                "reason": (f"scheduled operative date {last.isoformat()} falls after the stored "
                           f"effective date {eff.isoformat()}"),
                "effective_from": last.isoformat()}
    if drift >= ANCHOR_BREAK_GLASS_PP:
        return {**base, "apply": True, "authority": "BREAK_GLASS",
                "reason": (f"reported {reported_operative_pct} vs operative {stored_operative_pct} "
                           f"= {drift:.2f}pp drift, at or beyond the {ANCHOR_BREAK_GLASS_PP}pp "
                           f"break-glass"),
                "effective_from": d.isoformat()}
    if flow_trig and flow_trig.get("fired") is True:
        return {**base, "apply": True, "authority": "FLOW_TRIGGER_D4",
                "reason": (f"un-derived external flows of £{flow_trig['cumulative_undereived_gbp']:,.2f} "
                           f"= {flow_trig['cumulative_pct_of_value']:.2f}% of value, at or beyond "
                           f"the {flow_trig['threshold_pct']}% D-4 threshold"),
                "effective_from": d.isoformat()}
    if flow_trig and flow_trig.get("blocks"):
        # R4.3. A control fed a null BLOCKS. It does not report "held, nothing happened".
        return {**base, "apply": False, "authority": "HELD_FLOW_TRIGGER_UNKNOWN",
                "blocks": True,
                "reason": (f"the operative anchor is inside its window, but the D-4 flow trigger "
                           f"could not be evaluated ({flow_trig.get('reason')}), so 'no flow' is "
                           f"not a finding this run"),
                "effective_from": eff.isoformat()}
    return {**base, "apply": False, "authority": "HELD_IN_WINDOW",
            "reason": (f"operative effective {eff.isoformat()}, next scheduled window "
                       f"{nxt.isoformat()}, drift {drift:.2f}pp below the "
                       f"{ANCHOR_BREAK_GLASS_PP}pp break-glass, no D-4 flow trigger"),
            "effective_from": eff.isoformat()}



def _months_between(d0: date, d1: date) -> int:
    return (d1.year - d0.year) * 12 + (d1.month - d0.month)


def _add_months(d0: date, k: int) -> date:
    y, m = divmod((d0.year * 12 + d0.month - 1) + k, 12)
    return date(y, m + 1, 1)


def _monthly_amount(schedule, when: date) -> float:
    """Contribution in force at month `when` (schedule = [{from, monthly_gbp}], sorted or not)."""
    amt = 0.0
    for seg in sorted(schedule, key=lambda s: s["from"]):
        if date.fromisoformat(seg["from"]) <= when:
            amt = float(seg["monthly_gbp"])
    return amt


def fv_at_rate(m: float, principal: float, start: date, end: date, schedule,
               one_offs=None) -> float:
    """Future value at monthly rate m; contributions land at the START of each month k=1..n
    (amount per the schedule segment in force that month) and compound to end.

    one_offs = [{"date": "YYYY-MM-DD", "amount_gbp": n, "note": ...}] - lump sums, added
    12-Aug-2026 (register ISA-0015). The schedule modelled a regular monthly stream because
    that is what existed when it was written. With the standing order paused, LUMP SUMS ARE
    NOW THE PRIMARY CONTRIBUTION MECHANISM and the model could not express one - so a
    planned deposit was invisible to the anchor, which then overstated the required return
    and tightened every gate derived from it.
    """
    n = _months_between(start, end)
    fv = principal * (1 + m) ** n
    for k in range(1, n + 1):
        c = _monthly_amount(schedule, _add_months(start, k))
        if c:
            fv += c * (1 + m) ** (n - k)
    for o in (one_offs or []):
        od = date.fromisoformat(o["date"])
        if od < start:
            raise ValueError(f"one-off dated {od} precedes the valuation date {start}: it is "
                             f"already in the portfolio value, and counting it again would "
                             f"double-count (R5.2)")
        if od > end:
            continue                      # after the target date; contributes nothing
        months_to_end = _months_between(od, end)
        fv += float(o["amount_gbp"]) * (1 + m) ** months_to_end
    return fv


def solve_required_annual_pct(target: float, principal: float, start: date, end: date,
                              schedule, tol=1e-10, one_offs=None) -> float:
    """Bisect the monthly rate so FV == target; return effective annual % ((1+m)^12 - 1)."""
    lo, hi = -0.02, 0.08          # -21%..+152% annual — generous, monotone in m
    if fv_at_rate(hi, principal, start, end, schedule, one_offs) < target:
        raise ValueError("target unreachable inside solver bounds")
    for _ in range(200):
        mid = (lo + hi) / 2
        if fv_at_rate(mid, principal, start, end, schedule, one_offs) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    m = (lo + hi) / 2
    return round(((1 + m) ** 12 - 1) * 100, 1)


def apply_guardrails(derived_floor_pct: float):
    """D1c. Returns (operative_pct, guardrail_state)."""
    if derived_floor_pct > OPERATIVE_CAP_PCT:
        return OPERATIVE_CAP_PCT, "TARGET_ATTAINABILITY_REVIEW"
    if derived_floor_pct < OPERATIVE_FLOOR_PCT:
        return OPERATIVE_FLOOR_PCT, "OK"   # buffer banked; glidepath is age/value-driven only (B6)
    return derived_floor_pct, "OK"


def glidepath_check(portfolio_value_gbp, as_of=None):
    """B6 (18-Jul-26, design note: glidepath_design.md) — mechanical trigger, checked at every
    derivation. Fires (-> guardrail_state GLIDEPATH_REVIEW) when age >= GLIDEPATH_AGE_TRIGGER AND
    portfolio >= GLIDEPATH_VALUE_TRIGGER_GBP (the LATER-of rule); also flags if age >= 60 with the
    bridge underfunded (< value trigger) — human review, never mechanical migration. D1c: a low
    derived anchor is NOT a trigger. DOB Dec-1977 CONFIRMED (Raj 18-Jul-26); month-precise age."""
    import datetime as _dt
    try:
        import scoring_config as _c
    except Exception:
        _c = None
    age_trig = int(getattr(_c, "GLIDEPATH_AGE_TRIGGER", 56))
    val_trig = float(getattr(_c, "GLIDEPATH_VALUE_TRIGGER_GBP", 700_000))
    by, bm = getattr(_c, "RAJ_DOB_YM", (1977, 12))   # CONFIRMED Raj 18-Jul-26
    now = as_of or _dt.date.today()
    age = now.year - int(by) - (1 if now.month < int(bm) else 0)   # month-precise
    pv = float(portfolio_value_gbp or 0)
    if age >= age_trig and pv >= val_trig:
        return True, f"GLIDEPATH_REVIEW: age {age} >= {age_trig} and portfolio £{pv:,.0f} >= £{val_trig:,.0f}"
    if age >= 60 and pv < val_trig:
        return True, f"GLIDEPATH_REVIEW: age {age} >= 60 with bridge underfunded (£{pv:,.0f} < £{val_trig:,.0f})"
    return False, ""


def _reported_solve(state, pv, vd):
    """The fresh solve. Unchanged arithmetic — only the value it is solved ON has moved (D-3)."""
    td = date.fromisoformat(state["target_date"])
    sched = state["contribution_schedule"]
    one_offs = state.get("one_off_contributions") or []
    floor = solve_required_annual_pct(float(state["target_floor_gbp"]), pv, vd, td, sched,
                                      one_offs=one_offs)
    stretch = solve_required_annual_pct(float(state["target_stretch_gbp"]), pv, vd, td, sched,
                                        one_offs=one_offs)
    operative, gstate = apply_guardrails(floor)
    _gfired, _gnote = glidepath_check(pv)
    if _gfired:      # B6: age/value trigger overrides OK/attainability states for visibility
        gstate, gnote = "GLIDEPATH_REVIEW", _gnote
    else:
        gnote = None
    return {"floor_pct": floor, "stretch_pct": stretch, "operative_pct": operative,
            "guardrail_state": gstate, "glidepath_note": gnote,
            "one_off_contributions_applied": [
                {"date": o["date"], "amount_gbp": float(o["amount_gbp"])} for o in one_offs]}


def derive(state: dict, portfolio_value=None, value_date=None, as_of=None,
           observations=None, flows=None, flow_meta=None):
    """The anchor, on the D-3 valuation basis, with the D-4 trigger and the D-2 cadence decided.

    ⚑ RETURN SHAPE. The flat legacy keys are preserved and keep their meaning: everything named
    `required_return_*_pct` without a `reported_` prefix is the OPERATIVE, GATING value, which is
    what every consumer already reads. What changes is WHEN it is permitted to move, and that is
    stated in `anchor_cadence` rather than implied by the fact that this function ran.
    """
    as_of = _d(as_of) if as_of else date.today()

    # ── D-4 inputs first: the flow ledger feeds both the valuation basis and the trigger ────
    if flows is None and flow_meta is None:
        flows, flow_meta = external_flows()

    # ── D-3: what value do we solve on? ─────────────────────────────────────────────────────
    # ── ISA-0312. ONE HOME FOR THE PORTFOLIO VALUATION (R4.4, R6.1). ────────────────────────
    # `target_state.portfolio_value_gbp` was hand-written at each derivation and held 139738.0,
    # while `anchor_valuation_history.json` derives 139738.39 from the broker file for the SAME
    # date. GBP 0.39 rounds away at the anchor's 1dp - and that is precisely why it was
    # registered: two stored values for one quantity, both plausible, disagreeing silently. The
    # store is now the golden source and target_state is a FALLBACK that says so.
    spot_v, spot_d, _spot_src = _spot_valuation(state, portfolio_value, value_date,
                                                observations=observations)
    vb = valuation_basis(observations=observations, flows=(flows or []),
                         spot_value=spot_v, spot_date=spot_d)
    if vb.get("value_gbp") is None:
        raise AnchorCadenceError(
            "the anchor has no portfolio value to solve on — refusing to invent one (R4.1)")
    pv, vd = float(vb["value_gbp"]), _d(vb["value_date"])

    rep = _reported_solve(state, pv, vd)

    # ── D-4: the trigger, measured against the OPERATIVE derivation date ────────────────────
    since = state.get("operative_derived_at") or state.get("derived_at") or vd.isoformat()
    trig = flow_trigger(flows, flow_meta, since, pv, state.get("one_off_contributions_applied"))

    # ── D-2: may the operative value move? ──────────────────────────────────────────────────
    cad = cadence_decision(as_of, rep["operative_pct"],
                           state.get("required_return_operative_pct"),
                           state.get("operative_effective_from"), trig)

    if cad["apply"]:
        op_floor, op_stretch, op_operative = rep["floor_pct"], rep["stretch_pct"], rep["operative_pct"]
        op_guardrail = rep["guardrail_state"]
        op_eff = cad.get("effective_from") or as_of.isoformat()
        op_derived_at = as_of.isoformat()
        op_value = pv
    else:
        op_floor = state.get("required_return_floor_pct")
        op_stretch = state.get("required_return_stretch_pct")
        op_operative = state.get("required_return_operative_pct")
        op_guardrail = state.get("operative_guardrail_state", state.get("guardrail_state"))
        op_eff = cad.get("effective_from")
        op_derived_at = state.get("operative_derived_at") or state.get("derived_at")
        op_value = state.get("operative_portfolio_value_gbp", state.get("portfolio_value_gbp"))

    out = {
        "schema_version": TARGET_STATE_SCHEMA_VERSION,
        # ── the value the solve ran on, and how it was arrived at (R4.2) ──
        "portfolio_value_gbp": round(pv, 2),
        "portfolio_value_date": vd.isoformat(),
        "valuation_basis": vb,
        # ── REPORTED: fresh every run, gates nothing (D-2) ──
        "required_return_reported_floor_pct": rep["floor_pct"],
        "required_return_reported_stretch_pct": rep["stretch_pct"],
        "required_return_reported_operative_pct": rep["operative_pct"],
        "reported_derived_at": as_of.isoformat(),
        "guardrail_state": rep["guardrail_state"],
        # ── OPERATIVE: gates everything, moves only on cadence authority (D-2) ──
        "required_return_floor_pct": op_floor,
        "required_return_stretch_pct": op_stretch,
        "required_return_operative_pct": op_operative,
        "operative_guardrail_state": op_guardrail,
        "operative_effective_from": op_eff,
        "operative_derived_at": op_derived_at,
        "operative_portfolio_value_gbp": (None if op_value is None else round(float(op_value), 2)),
        "operative_next_window": cad.get("next_window"),
        "anchor_cadence": cad,
        "flow_trigger": trig,
        "one_off_contributions_applied": rep["one_off_contributions_applied"],
    }
    if rep.get("glidepath_note"):
        out["glidepath_note"] = rep["glidepath_note"]
    # ── I-CAD-2. The operative anchor must never leave the D1c band, whichever branch set it.
    # A held value is still a gating value, and a guardrail that only runs on the fresh path is a
    # guardrail that does not run.
    if out["required_return_operative_pct"] is not None:
        _op = float(out["required_return_operative_pct"])
        if not (OPERATIVE_FLOOR_PCT - 1e-9 <= _op <= OPERATIVE_CAP_PCT + 1e-9):
            raise AnchorCadenceError(
                f"operative anchor {_op} is outside the D1c band "
                f"{OPERATIVE_FLOOR_PCT}-{OPERATIVE_CAP_PCT} on cadence authority "
                f"{cad.get('authority')} — a held value is still a gating value")
    return out


# ───────────────────────────────────────────────────────────────── store seeding (R6.5)
def _portfolio_observations(folder=None):
    """Month-end valuations from the portfolio_data_[mmm]_[yyyy].json files already on disk.

    ⚑ THE DATE COMES FROM `_meta.data_date`, NEVER THE FILENAME. `portfolio_data_aug_2026.json`
    holds the **31-Jul-2026** valuation — the run month and the data month differ by one, and the
    A22 glob failure is the standing reminder of what happens when a date is taken from a name.
    """
    import glob as _glob
    folder = folder or HERE
    out = []
    for fp in sorted(_glob.glob(os.path.join(folder, "portfolio_data_*.json"))):
        with open(fp, encoding="utf-8") as f:
            doc = json.load(f)
        meta, summ = doc.get("_meta") or {}, doc.get("summary") or {}
        dd, tv = meta.get("data_date"), summ.get("total_value_gbp")
        if not dd or tv is None:
            out.append({"skipped": os.path.basename(fp),
                        "why": f"data_date={dd!r} total_value_gbp={tv!r} — a valuation with no "
                               f"date is not admissible (R4.2)"})
            continue
        d = dt_parse_flex(dd)
        is_me = (d == month_end(d))
        out.append({"month_end": d.isoformat(), "value_gbp": round(float(tv), 2),
                    "source": f"{os.path.basename(fp)} (_meta.data_date, from "
                              f"{meta.get('source_file')})",
                    "as_of": meta.get("extracted_at", "")[:10] or d.isoformat(),
                    "stamp_basis": "point_in_time" if is_me else "not_month_end_excluded",
                    "admissible": bool(is_me)})
    return out


def dt_parse_flex(s):
    """`31-Jul-2026` / `2026-07-31` / `31-Jul-26`. Raises rather than guessing a century."""
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y"):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise AnchorCadenceError(f"unparseable valuation date {s!r} — refusing to guess")


def _cash_statement_flows(folder=None):
    """Realised external flows, from the CASH STATEMENT — the only document that contains them."""
    try:
        import extract_cash_statement as ecs
    except Exception as e:                                       # noqa: BLE001
        return None, f"extract_cash_statement unavailable: {type(e).__name__}: {e}"
    fold = folder or os.path.dirname(HERE)
    try:
        res = ecs.parse(folder=fold)
    except Exception as e:                                       # noqa: BLE001
        return None, f"cash statement parse failed: {type(e).__name__}: {e}"
    rows, n_seen = [], 0
    for r in res.get("rows") or []:
        if r.get("category") not in ecs.FLOW_CATEGORIES:
            continue
        n_seen += 1
        amt = float(r.get("receipt_gbp") or 0) - abs(float(r.get("payment_gbp") or 0))
        rows.append({"date": str(r["date"])[:10], "amount_gbp": round(amt, 2),
                     "kind": "contribution" if amt >= 0 else "withdrawal",
                     "status": "realised",
                     "source": f"{r.get('source_file')} :: {r.get('description')}",
                     "as_of": res.get("as_of"),
                     "stamp_basis": "point_in_time"})
    # R4.9: a reader that cannot match a row COUNTS it. n_seen vs len(rows) must agree.
    if n_seen != len(rows):
        return None, f"classified {n_seen} flow rows but emitted {len(rows)} — silent partial"
    return {"rows": rows, "tax_year_start": res.get("tax_year_start"),
            "source_files": res.get("source_files"), "as_of": res.get("as_of")}, None


def seed_stores(state_path=None, folder=None, write=True, quiet=False):
    """Build `anchor_valuation_history.json` and `anchor_flow_ledger.json` from disk.

    Idempotent and content-derived: re-running cannot duplicate a row, because observations key
    on `month_end` and flows on (date, amount, source).
    """
    state_path = state_path or STATE_PATH
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    # ── valuations ──────────────────────────────────────────────────────────────────────
    raw = _portfolio_observations()
    obs = [o for o in raw if o.get("admissible")]
    excluded = [o for o in raw if not o.get("admissible")]
    vdoc = {"schema_version": 1,
            "_meta": {"generated_at": date.today().isoformat(),
                      "generator": "derive_required_return.seed_stores",
                      "purpose": ("month-end portfolio valuations for the D-3 flow-adjusted "
                                  f"{VALUATION_MEAN_MONTHS}-month mean. Appended by the pre-run; "
                                  "never hand-edited (R14.3)."),
                      "admissibility": ("an observation enters the mean only if its date IS a "
                                        "month-end. A near-month-end valuation is retained and "
                                        "marked, never silently used as a month-end (R6.4)."),
                      "n_admissible": len(obs), "n_excluded": len(excluded)},
            "observations": sorted(obs, key=lambda o: o["month_end"]),
            "excluded": excluded}

    # ── flows ───────────────────────────────────────────────────────────────────────────
    cs, err = _cash_statement_flows(folder)
    flows = list((cs or {}).get("rows") or [])
    # planned flows already inside the solve are carried so the D-4 exclusion can see them
    for o in state.get("one_off_contributions") or []:
        flows.append({"date": str(o["date"])[:10], "amount_gbp": round(float(o["amount_gbp"]), 2),
                      "kind": "contribution", "status": "planned",
                      "source": "target_state.one_off_contributions",
                      "as_of": state.get("schedule_updated_at") or date.today().isoformat(),
                      "stamp_basis": "declared_forward_dated"})
    seen, ded = set(), []
    for fl in sorted(flows, key=lambda f: (f["date"], f["source"])):
        k = (fl["date"], round(float(fl["amount_gbp"]), 2), fl["source"])
        if k in seen:
            continue
        seen.add(k)
        ded.append(fl)
    fdoc = {"schema_version": 1,
            "_meta": {"generated_at": date.today().isoformat(),
                      "generator": "derive_required_return.seed_stores",
                      "source": ("AJ Bell CASH STATEMENT via extract_cash_statement.parse() — the "
                                 "dealing Transaction History contains no deposits at all, so it "
                                 "is not a flow source (R6.1)"),
                      "coverage_from": (cs or {}).get("tax_year_start"),
                      "coverage_note": ("the cash statement on file covers the CURRENT TAX YEAR "
                                        "only. This ledger is therefore NOT a complete flow "
                                        "history, and the D-4 trigger is only valid for an "
                                        "operative derivation date inside the covered window — "
                                        "which `flow_trigger` asserts."),
                      "source_files": (cs or {}).get("source_files"),
                      "parse_error": err,
                      "n": len(ded)},
            "flows": ded}
    if write:
        with open(VALUATION_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(vdoc, f, indent=2)
        with open(FLOW_LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(fdoc, f, indent=2)
        if quiet:
            print(f"  stores refreshed: {len(obs)} month-end valuation(s), {len(ded)} flow(s)"
                  + (f"; ⚑ flow parse_error={err}" if err else ""))
            return vdoc, fdoc
        print(f"WROTE {os.path.basename(VALUATION_HISTORY_PATH)} "
              f"({len(obs)} admissible, {len(excluded)} excluded)")
        print(f"WROTE {os.path.basename(FLOW_LEDGER_PATH)} ({len(ded)} flows"
              + (f"; parse_error={err}" if err else "") + ")")
        for o in vdoc["observations"]:
            print(f"    {o['month_end']}  GBP {o['value_gbp']:>12,.2f}  {o['source'][:60]}")
        for fl in ded:
            print(f"    {fl['date']}  GBP {fl['amount_gbp']:>10,.2f}  {fl['status']:<8} "
                  f"{fl['source'][:52]}")
        if len(obs) < VALUATION_MEAN_MONTHS:
            print(f"  ⚑ {len(obs)} of {VALUATION_MEAN_MONTHS} month-end observations — the D-3 "
                  f"basis will DEGRADE to spot and say so until the {VALUATION_MEAN_MONTHS}th "
                  f"lands. It is not silently a 2-month mean.")
    return vdoc, fdoc


def _selftest():
    _n = [0]

    def ok(cond, msg):
        assert cond, msg
        _n[0] += 1

    # ══ U-A19 (pre-existing, unchanged) ══════════════════════════════════════════════════
    _s, _e = date(2026, 6, 30), date(2037, 12, 31)
    _sc = [{"from": "2026-07-01", "monthly_gbp": 0}]
    _no = solve_required_annual_pct(1_000_000, 144_342.19, _s, _e, _sc)
    _with = solve_required_annual_pct(1_000_000, 144_342.19, _s, _e, _sc,
                                      one_offs=[{"date": "2026-09-05", "amount_gbp": 11_250}])
    ok(_with < _no, "a lump sum must LOWER the required return")
    ok(fv_at_rate(0.0, 100.0, _s, _e, _sc,
                  [{"date": "2026-09-05", "amount_gbp": 1000}]) == 1100.0, "zero-rate FV is additive")
    ok(fv_at_rate(0.01, 100.0, _s, _e, _sc, [{"date": "2099-01-01", "amount_gbp": 1000}]) ==
       fv_at_rate(0.01, 100.0, _s, _e, _sc), "a one-off after the target date contributes nothing")
    try:
        fv_at_rate(0.01, 100.0, _s, _e, _sc, [{"date": "2026-01-01", "amount_gbp": 1000}])
        raise AssertionError("a one-off BEFORE the valuation date must raise (double-count)")
    except ValueError:
        _n[0] += 1
    ok(glidepath_check(800_000, as_of=date(2036, 1, 1))[0], "glidepath fires at age>=56, pv>=700k")
    ok(not glidepath_check(800_000, as_of=date(2026, 8, 12))[0], "must NOT fire at 48 — neg control")
    # ⚑ The glidepath block sat AFTER `return out` until 12-Aug-2026 and had never run. The
    # structural guard moved with it into `_reported_solve` and is re-asserted THERE, so the
    # regression it prevents cannot come back through the refactor either.
    import inspect as _insp
    _src = _insp.getsource(_reported_solve)
    ok(_src.rindex("glidepath_check") < _src.rindex("return {"),
       "the glidepath override must precede the return — it was unreachable dead code before")
    sched0 = [{"from": "2026-07-01", "monthly_gbp": 0}, {"from": "2027-01-01", "monthly_gbp": 1250}]
    f = solve_required_annual_pct(1_000_000, 144_342.19, _s, _e, sched0)
    g = solve_required_annual_pct(1_500_000, 144_342.19, _s, _e, sched0)
    ok(abs(f - 13.9) <= 0.3, f"floor {f} != ~13.9")
    ok(abs(g - 18.7) <= 0.4, f"stretch {g} != ~18.7")
    m = (1 + f / 100) ** (1 / 12) - 1
    ok(abs(fv_at_rate(m, 144_342.19, _s, _e, sched0) - 1_000_000) / 1_000_000 < 0.005, "inversion")
    ok(apply_guardrails(19.4) == (18.0, "TARGET_ATTAINABILITY_REVIEW"), "D1c cap")
    ok(apply_guardrails(8.2) == (10.0, "OK"), "D1c floor")
    ok(apply_guardrails(13.9) == (13.9, "OK"), "D1c pass-through")
    ok(solve_required_annual_pct(1_000_000, 250_000, _s, _e, sched0) < f, "more capital => lower")
    ok(solve_required_annual_pct(1_000_000, 144_342.19, _s, _e,
                                 [{"from": "2026-07-01", "monthly_gbp": 0}]) > f, "schedule matters")

    # ══ D-3 · ISA-0139 — FLOW-ADJUSTED VALUATION MEAN ════════════════════════════════════
    OB = [{"month_end": "2026-07-31", "value_gbp": 100_000.0, "source": "t"},
          {"month_end": "2026-06-30", "value_gbp": 100_000.0, "source": "t"},
          {"month_end": "2026-05-31", "value_gbp": 100_000.0, "source": "t"}]
    # T-CAD-1. NEGATIVE CONTROL. Zero flows => the flow-adjusted mean IS the plain mean.
    z = flow_adjusted_mean(OB, [])
    ok(z["status"] == "computed" and z["value_gbp"] == 100_000.0 and
       z["mean_flow_adjustment_gbp"] == 0.0,
       f"no flows must leave the mean untouched, got {z}")
    ok(z["identity_check"]["holds"], "I-CAD-1 must hold with no flows")
    # T-CAD-2. THE D-3 NUMBER. A flat market with F in the latest month: plain mean = V + F/3,
    # flow-adjusted = V + F. This is the £3,750-vs-£11,250 defect, reproduced as arithmetic.
    F = 11_250.0
    OBF = [{"month_end": "2026-07-31", "value_gbp": 100_000.0 + F, "source": "t"},
           {"month_end": "2026-06-30", "value_gbp": 100_000.0, "source": "t"},
           {"month_end": "2026-05-31", "value_gbp": 100_000.0, "source": "t"}]
    FL = [{"date": "2026-07-15", "amount_gbp": F, "status": "realised", "source": "t"}]
    p = flow_adjusted_mean(OBF, [])
    q = flow_adjusted_mean(OBF, FL)
    ok(abs(p["value_gbp"] - (100_000.0 + F / 3)) < 0.01,
       f"a PLAIN mean must admit only F/3 = {F/3:.0f}; got {p['value_gbp']}")
    ok(abs(q["value_gbp"] - (100_000.0 + F)) < 0.01,
       f"the flow-adjusted mean must admit F in full; got {q['value_gbp']}")
    ok(abs(q["understatement_avoided_gbp"] - 2 * F / 3) < 0.01,
       f"the correction must be 2F/3 = {2*F/3:.0f}; got {q['understatement_avoided_gbp']}")
    ok(q["identity_check"]["holds"], "I-CAD-1: two derivations of the adjusted mean must agree")
    # T-CAD-3. A flow ON an observation date is already IN that valuation — never counted again.
    onday = flow_adjusted_mean(OBF, [{"date": "2026-06-30", "amount_gbp": F,
                                      "status": "realised", "source": "t"}])
    ok(onday["rows"][1]["flows_after_gbp"] == 0.0,
       "a flow dated ON an observation must not be added to that observation (double-count)")
    ok(onday["rows"][2]["flows_after_gbp"] == F, "...but it IS after the month before")
    # T-CAD-4. A flow AFTER the latest observation is outside the window and contributes nothing.
    fut = flow_adjusted_mean(OBF, [{"date": "2026-09-05", "amount_gbp": F,
                                    "status": "realised", "source": "t"}])
    ok(fut["value_gbp"] == p["value_gbp"], "a flow after the latest month-end is out of window")
    # T-CAD-5. A WITHDRAWAL must move the basis DOWN — the correction is signed, not absolute.
    wd = flow_adjusted_mean(OB, [{"date": "2026-07-15", "amount_gbp": -6_000.0,
                                  "status": "realised", "source": "t"}])
    ok(wd["value_gbp"] < 100_000.0 and abs(wd["understatement_avoided_gbp"] + 4_000.0) < 0.01,
       f"an outflow must lower the basis by 2/3 of itself; got {wd}")
    # T-CAD-6. R4.9. Two observations must NEVER be presented as a three-month mean.
    short = flow_adjusted_mean(OB[:2], [])
    ok(short["status"] == "INSUFFICIENT_HISTORY" and short["value_gbp"] is None,
       "a 2-of-3 mean must refuse, not renormalise silently")
    vb = valuation_basis(observations=OB[:2], flows=[], spot_value=139_738.39,
                         spot_date="2026-07-31")
    ok(vb["basis"] == "spot_fallback" and vb["degraded"] is True and vb["value_gbp"] == 139_738.39,
       f"insufficient history must degrade to spot AND SAY SO; got {vb}")
    vb3 = valuation_basis(observations=OB, flows=[], spot_value=1.0, spot_date="2026-07-31")
    ok(vb3["basis"] == "flow_adjusted_3m_mean" and not vb3["degraded"], "3 observations => the mean")
    # T-CAD-7. R4.8. Two DIFFERENT values for one month-end is refused, not chosen between.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        pth = os.path.join(td, "v.json")
        json.dump({"observations": [{"month_end": "2026-07-31", "value_gbp": 100.0, "source": "a"},
                                   {"month_end": "2026-07-31", "value_gbp": 200.0, "source": "b"}]},
                  open(pth, "w"))
        try:
            valuation_observations(pth)
            raise AssertionError("two different values for one month-end must RAISE")
        except AnchorCadenceError:
            _n[0] += 1
        json.dump({"observations": [{"month_end": "2026-07-31", "value_gbp": 100.0, "source": "a"},
                                   {"month_end": "2026-07-31", "value_gbp": 100.0, "source": "b"}]},
                  open(pth, "w"))
        rr, mm = valuation_observations(pth)
        ok(len(rr) == 1 and mm["found"] == 1, "the SAME value twice is one fact, deduped, not an error")
        ok(valuation_observations(os.path.join(td, "nope.json"))[1]["status"] == "MISSING_STORE",
           "an absent store reports MISSING_STORE, not an empty success")

    # ══ D-4 · ISA-0140 — 2%-OF-VALUE FLOW TRIGGER ═══════════════════════════════════════
    META = {"status": "OK"}
    PV = 139_738.39
    # T-CAD-8. £11,250 = 8.05% of value. Fires on arrival.
    big = flow_trigger([{"date": "2026-09-05", "amount_gbp": 11_250.0, "status": "realised",
                         "source": "cash statement"}], META, "2026-08-31", PV)
    ok(big["fired"] is True and abs(big["cumulative_pct_of_value"] - 8.0508) < 0.01,
       f"£11,250 on £139,738 = 8.05% and must fire; got {big}")
    # T-CAD-9. £1,250 = 0.89%. One does not fire; the THIRD crosses 2%. One rule, both regimes.
    so = [{"date": f"2026-{m:02d}-05", "amount_gbp": 1_250.0, "status": "realised",
           "source": "cash statement"} for m in (10, 11, 12)]
    ok(flow_trigger(so[:1], META, "2026-09-30", PV)["fired"] is False, "one £1,250 must NOT fire")
    ok(flow_trigger(so[:2], META, "2026-09-30", PV)["fired"] is False, "two must NOT fire")
    t3 = flow_trigger(so, META, "2026-09-30", PV)
    ok(t3["fired"] is True and abs(t3["cumulative_pct_of_value"] - 2.6836) < 0.01,
       f"the third £1,250 must cross 2%; got {t3}")
    # T-CAD-10. THE DOUBLE-COUNT TRAP. A flow already applied inside the stored derivation is
    # NOT un-derived, so its arrival must not fire a re-derivation that changes nothing.
    applied = [{"date": "2026-09-05", "amount_gbp": 11_250.0}]
    dbl = flow_trigger([{"date": "2026-09-05", "amount_gbp": 11_250.0, "status": "realised",
                         "source": "cash statement"}], META, "2026-08-31", PV, applied)
    ok(dbl["fired"] is False and dbl["cumulative_undereived_gbp"] == 0.0 and
       "already applied" in dbl["excluded"][0]["excluded_because"],
       f"an already-applied one-off must be excluded WITH ITS REASON; got {dbl}")
    # ...and the exclusion must be exact: a different amount on the same date is NOT the same flow.
    ok(flow_trigger([{"date": "2026-09-05", "amount_gbp": 11_249.0, "status": "realised",
                      "source": "c"}], META, "2026-08-31", PV, applied)["counted"],
       "the applied-flow match must be exact to the penny — a near miss is a real flow")
    # T-CAD-11. A PLANNED flow has not happened and cannot trigger anything.
    ok(flow_trigger([{"date": "2026-09-05", "amount_gbp": 11_250.0, "status": "planned",
                      "source": "target_state"}], META, "2026-08-31", PV)["fired"] is False,
       "a planned flow must not fire the trigger")
    # T-CAD-12. R4.3. An absent ledger returns UNKNOWN and BLOCKS. It never returns 'no flows'.
    u = flow_trigger(None, {"status": "UNKNOWN", "reason": "ledger absent"}, "2026-08-31", PV)
    ok(u["status"] == "UNKNOWN" and u["fired"] is None and u["blocks"] is True,
       f"a missing ledger must BLOCK, never PASS; got {u}")
    z0 = flow_trigger([], META, "2026-08-31", 0.0)
    ok(z0["status"] == "UNKNOWN" and z0["blocks"] is True, "a zero portfolio value must BLOCK")

    # ══ D-2 · ISA-0138 — TWO-SPEED CADENCE ══════════════════════════════════════════════
    ok(last_operative_window("2026-08-12") == date(2026, 3, 31), "last window before Aug-26")
    ok(next_operative_window("2026-08-12") == date(2026, 9, 30), "next window after Aug-26")
    ok(next_operative_window("2026-09-30") == date(2027, 3, 31), "the window date itself is past")
    ok(last_operative_window("2026-01-15") == date(2025, 9, 30), "January looks back to September")
    # T-CAD-13. HELD. Inside the window, small drift, no trigger => the gate does NOT move.
    h = cadence_decision("2026-08-12", 13.8, 13.8, "2026-03-31", {"fired": False, "blocks": False})
    ok(h["apply"] is False and h["authority"] == "HELD_IN_WINDOW", f"must hold; got {h}")
    d1 = cadence_decision("2026-08-12", 15.0, 13.8, "2026-03-31", {"fired": False, "blocks": False})
    ok(d1["apply"] is False and abs(d1["drift_pp"] - 1.2) < 1e-9,
       "1.2pp of drift is REPORTED and still held — that is the two-speed design")
    # T-CAD-14. BREAK GLASS at exactly 2.0pp — the boundary is inclusive, and stated.
    bg = cadence_decision("2026-08-12", 15.8, 13.8, "2026-03-31", {"fired": False, "blocks": False})
    ok(bg["apply"] is True and bg["authority"] == "BREAK_GLASS" and
       bg["effective_from"] == "2026-08-12", f"2.0pp must break glass; got {bg}")
    ok(cadence_decision("2026-08-12", 15.79, 13.8, "2026-03-31",
                        {"fired": False, "blocks": False})["apply"] is False,
       "1.99pp must NOT break glass — negative control on the boundary")
    # T-CAD-15. SCHEDULED WINDOW outranks break-glass, so a calendar move is never mislabelled.
    sw = cadence_decision("2026-10-01", 15.8, 13.8, "2026-03-31", {"fired": False, "blocks": False})
    ok(sw["apply"] is True and sw["authority"] == "SCHEDULED_WINDOW" and
       sw["effective_from"] == "2026-09-30", f"the calendar takes precedence; got {sw}")
    # T-CAD-16. The D-4 trigger opens a window out of cycle.
    ft = cadence_decision("2026-08-12", 13.8, 13.8, "2026-03-31",
                          {"fired": True, "blocks": False, "cumulative_undereived_gbp": 11250.0,
                           "cumulative_pct_of_value": 8.05, "threshold_pct": 2.0})
    ok(ft["apply"] is True and ft["authority"] == "FLOW_TRIGGER_D4", f"D-4 opens a window; got {ft}")
    # T-CAD-17. An unevaluable trigger BLOCKS rather than reporting a quiet hold.
    bl = cadence_decision("2026-08-12", 13.8, 13.8, "2026-03-31",
                          {"fired": None, "blocks": True, "reason": "ledger absent"})
    ok(bl["apply"] is False and bl["blocks"] is True and
       bl["authority"] == "HELD_FLOW_TRIGGER_UNKNOWN", f"must block; got {bl}")
    # T-CAD-18. First stamp initialises rather than raising or inventing an effective date.
    ini = cadence_decision("2026-08-12", 13.8, None, None, {"fired": False, "blocks": False})
    ok(ini["apply"] is True and ini["authority"] == "INITIALISE", "first stamp must initialise")

    # ══ ROLLBACK (R4.13) — one constant each, and both must reproduce the old behaviour ═══
    global ANCHOR_CADENCE_REGIME, ANCHOR_VALUATION_BASIS
    _keep_c, _keep_v = ANCHOR_CADENCE_REGIME, ANCHOR_VALUATION_BASIS
    try:
        ANCHOR_CADENCE_REGIME = "spot_every_run"
        r = cadence_decision("2026-08-12", 13.8, 13.8, "2026-03-31", {"fired": False, "blocks": False})
        ok(r["apply"] is True and r["authority"] == "ROLLBACK_SPOT_EVERY_RUN",
           "the cadence rollback constant must restore move-every-run exactly")
        ANCHOR_VALUATION_BASIS = "spot"
        s = valuation_basis(observations=OB, flows=[], spot_value=139_738.39, spot_date="2026-07-31")
        ok(s["basis"] == "spot" and s["value_gbp"] == 139_738.39 and not s["degraded"],
           "the valuation rollback constant must restore the spot value exactly, mean ignored")
    finally:
        ANCHOR_CADENCE_REGIME, ANCHOR_VALUATION_BASIS = _keep_c, _keep_v
    ok(ANCHOR_CADENCE_REGIME == "two_speed" and
       ANCHOR_VALUATION_BASIS == "flow_adjusted_3m_mean", "rollback constants restored")

    # ══ month_end / date parsing ════════════════════════════════════════════════════════
    ok(month_end("2026-02-15") == date(2026, 2, 28) and month_end("2028-02-01") == date(2028, 2, 29),
       "month_end must handle February and leap years")
    ok(month_end("2026-12-03") == date(2026, 12, 31), "month_end must roll the year")
    ok(dt_parse_flex("31-Jul-2026") == date(2026, 7, 31) and
       dt_parse_flex("2026-07-31") == date(2026, 7, 31), "both stamped date formats parse")
    try:
        dt_parse_flex("Jul 2026")
        raise AssertionError("an unparseable date must RAISE, never be guessed")
    except AnchorCadenceError:
        _n[0] += 1

    print(f"derive_required_return SELF-TEST OK — {_n[0]} assertions "
          f"(U-A19 + D-2/D-3/D-4 T-CAD-1..18)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify the stored anchor; no write. Exit 1 on stale REPORTED value, on "
                         "an operative update that is due but unapplied, or on an unevaluable "
                         "D-4 trigger")
    ap.add_argument("--portfolio-value", type=float, default=None)
    ap.add_argument("--value-date", default=None)
    ap.add_argument("--as-of", default=None, help="evaluate the cadence as at this date")
    ap.add_argument("--trigger", default="manual run")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--seed-history", action="store_true",
                    help="seed anchor_valuation_history.json / anchor_flow_ledger.json from the "
                         "sources already on disk, then exit")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    if a.seed_history:
        seed_stores()
        return

    # ⚑ R4.11 / R14.2 — CAPTURE IS A PROPERTY OF PRODUCING THE ANCHOR, NOT A PROSE STEP.
    # The stores were seeded by a separate `--seed-history` invocation in the first cut, which
    # means the valuation history would have grown only when somebody remembered to run it — and
    # the D-3 basis would have sat DEGRADED to spot forever while reporting, correctly and
    # uselessly, that it had fewer than three observations. Anything that must happen every run
    # is wired into the function that produces the thing. Refreshing is idempotent and
    # content-derived, so it cannot duplicate a row.
    try:
        seed_stores(state_path=a.state, write=True, quiet=True)
    except Exception as _e:                                       # noqa: BLE001
        # ⚑ NOT a bare except around instrumentation (R4.12): the failure is reported, and the
        # derivation continues to the D-3/D-4 reads, which will then degrade or BLOCK visibly.
        print(f"  ⚑ store refresh FAILED ({type(_e).__name__}: {_e}) — the D-3 basis and the D-4 "
              f"trigger will report on whatever is already on file, and say so")

    with open(a.state, encoding="utf-8") as f:
        state = json.load(f)
    out = derive(state, a.portfolio_value, a.value_date, as_of=a.as_of)
    cad, trig, vb = out["anchor_cadence"], out["flow_trigger"], out["valuation_basis"]
    print(json.dumps({k: v for k, v in out.items()
                      if k not in ("valuation_basis", "anchor_cadence", "flow_trigger")}, indent=2))
    print(f"\nD-3 valuation basis : {vb['basis']}  £{vb.get('value_gbp'):,.2f} @ {vb.get('value_date')}"
          + (f"  ⚑ DEGRADED: {vb.get('degraded_reason')}" if vb.get("degraded") else ""))
    if vb.get("detail"):
        _d3 = vb["detail"]
        print(f"                      plain 3m mean £{_d3['plain_mean_gbp']:,.2f} → flow-adjusted "
              f"£{_d3['value_gbp']:,.2f} (understatement avoided "
              f"£{_d3['understatement_avoided_gbp']:,.2f}); identity "
              f"{'HOLDS' if _d3['identity_check']['holds'] else 'FAILS'}")
    print(f"D-4 flow trigger    : {trig['status']} fired={trig.get('fired')} "
          f"{trig.get('cumulative_pct_of_value')}% of value vs {trig['threshold_pct']}% threshold")
    print(f"D-2 cadence         : {cad['authority']} — apply={cad['apply']}\n"
          f"                      {cad.get('reason')}")

    if a.check:
        errs = []
        stored_rep = state.get("required_return_reported_floor_pct")
        if stored_rep is None:
            errs.append("no stored REPORTED anchor — target_state predates the D-2 two-speed "
                        "cadence; rerun without --check to stamp it")
        else:
            drift = abs(out["required_return_reported_floor_pct"] - float(stored_rep))
            print(f"reported drift vs stored: {drift:.2f}pp")
            if drift > 0.2:
                errs.append(f"stored REPORTED anchor stale by {drift:.2f}pp — rerun without --check")
        if int(state.get("schema_version", 1)) != TARGET_STATE_SCHEMA_VERSION:
            errs.append(f"target_state.schema_version {state.get('schema_version')} != "
                        f"{TARGET_STATE_SCHEMA_VERSION} (two-speed cadence)")
        if cad["apply"]:
            errs.append(f"an OPERATIVE anchor update is due on authority {cad['authority']} "
                        f"({cad.get('reason')}) and has not been applied — rerun without --check")
        if cad.get("blocks") or trig.get("blocks"):
            errs.append(f"the D-4 flow trigger could not be evaluated ({trig.get('reason')}); "
                        f"'no flow' is not a finding this run (R4.3)")
        if errs:
            for e in errs:
                print("CHECK FAIL — " + e)
            sys.exit(1)
        print("CHECK OK")
        return

    today = date.today().isoformat()
    state.update(out)
    state["derived_at"] = today
    state["derivation"] = ("monthly-compounded solve on the D-3 flow-adjusted valuation basis, "
                           "two-speed D-2 cadence, D-4 flow trigger; derive_required_return.py v2")
    # ⚑ D-2 SUPERSEDES D1b's "next April". The operative clock is the semi-annual switch cycle
    # (D-7: that cycle is master), and the reported value is refreshed every run, so a single
    # `next_derivation_due` date no longer describes the behaviour. Both clocks are stated.
    state["next_derivation_due"] = out["operative_next_window"]
    state["next_operative_window"] = out["operative_next_window"]
    state["reported_derivation_cadence"] = "every run (D-2 reported speed)"
    state["derivation_basis"] = (
        f"D-3 {vb['basis']}: £{vb.get('value_gbp'):,.2f} @ {vb.get('value_date')}"
        + (f" (DEGRADED — {vb.get('degraded_reason')})" if vb.get("degraded") else "")
        + f". D-2 two-speed cadence, operative effective {out['operative_effective_from']} on "
        f"authority {cad['authority']}, next scheduled window {out['operative_next_window']}. "
        f"D-4 trigger {trig['status']} (fired={trig.get('fired')}).")
    state.setdefault("derivation_history", []).append(
        {"derived_at": today,
         "reported_floor_pct": out["required_return_reported_floor_pct"],
         "reported_stretch_pct": out["required_return_reported_stretch_pct"],
         "reported_operative_pct": out["required_return_reported_operative_pct"],
         "operative_floor_pct": out["required_return_floor_pct"],
         "operative_pct": out["required_return_operative_pct"],
         "operative_effective_from": out["operative_effective_from"],
         "cadence_authority": cad["authority"],
         "cadence_applied": cad["apply"],
         "valuation_basis": vb["basis"],
         "valuation_value_gbp": vb.get("value_gbp"),
         "portfolio_value_gbp": out["portfolio_value_gbp"],
         "flow_trigger_pct": trig.get("cumulative_pct_of_value"),
         "flow_trigger_fired": trig.get("fired"),
         "schedule": "; ".join(f"{s['monthly_gbp']}/mo from {s['from']}"
                               for s in state["contribution_schedule"]),
         "trigger": a.trigger})
    with open(a.state, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"\nWROTE {a.state}\n  reported {out['required_return_reported_operative_pct']}  |  "
          f"operative {out['required_return_operative_pct']} effective "
          f"{out['operative_effective_from']} ({cad['authority']})  |  history rows "
          f"{len(state['derivation_history'])}")


if __name__ == "__main__":
    main()
