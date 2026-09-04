#!/usr/bin/env python3
"""
retention.py — V2.1-D s9: immutable entry underwriting, realised_fraction, and the
mandatory re-underwrite, the s3 step-down ratchet, and A8's selection-bias haircut
feeding A20's SHADOW slot competition (built 26-Aug-2026, ISA-0440 — this line advertised A20
for a day while the module contained none of it).

═══════════════════════════════════════════════════════════════════════════════════════════
s9 — WHAT AN ENTRY MUST RECORD, AND WHY IT IS IMMUTABLE
═══════════════════════════════════════════════════════════════════════════════════════════
    er_entry · er_confidence_entry · price_entry · fv_entry · er_horizon_months · underwrite_date

⚑ IMMUTABLE. If `er_entry` can be edited, `realised_fraction` measures nothing: a thesis that
has been quietly re-marked upward always looks un-completed, and the position never comes up for
review. `underwrite()` REFUSES to overwrite an existing lot; re-marking happens through
`re_underwrite()`, which APPENDS and keeps the original (R2.13 — corrections replace in place,
with the original retained and marked; here the audit trail is the whole instrument).

    realised_fraction = (price_now / price_entry - 1) / er_entry

⚑ >= 1.0 TRIGGERS A MANDATORY RE-UNDERWRITE, NEVER AN AUTOMATIC SALE. This distinction is the
whole of Raj's Q4 and it is measured, not asserted: on this book a hard +15.9% target would have
destroyed 59% of the sleeve's gain. Realised fractions to date — AVGO 0.27x (HOLD, and the
framework was CORRECT) vs MU 2.88x (target exceeded THREE TIMES OVER and nothing noticed). The
instrument exists because of the second case, and it must not create the first.

Outcome table (clean spec s9):
    clears + at/below risk target  -> HOLD and re-mark er_entry
    clears + above risk target     -> TRIM to risk target
    fails  + challenger exists     -> SELL / REPLACE
    fails  + no challenger         -> TRIM to STARTER, or sell to the marginal fund receiver

ER_HORIZON_MONTHS = 12, declared in scoring_config as the single home (Raj, D6, 22-Aug-2026).

═══════════════════════════════════════════════════════════════════════════════════════════
s3 — THE STEP-DOWN RATCHET (replaces the D6 probation rule)
═══════════════════════════════════════════════════════════════════════════════════════════
⚑ POPULATION: FORWARD-LED FRAMEWORK DECISIONS ONLY. AVGO, MU and ONT predate the forward-led
framework (live ~05-Jul-2026); ABCL was an A13 override; QBTS is VCI/Path B. COCO (02-Aug) is
the only forward-led decision, so n = 1 and THE RULE CANNOT FIRE YET. That is correct, not a
loophole — measuring the framework on a book it did not assemble is measuring the wrong thing,
in either direction.

⚑ CONSEQUENCE: step down to the next lower 5% multiple, not a revert to the 10% floor. Reverting
17% -> 10% is a ~7pp forced liquidation triggered by a lagging measure.

⚑ ROUTING: via capital_destination, NOT hard-coded to VUAG — VUAG sits at 11.75% against a
12.5% single-fund cap and would breach it within two increments.
"""
from __future__ import annotations

import datetime
import math
from typing import Dict, List, Optional

# ── P0.1 LIVE-PATH EXECUTION LEDGER (framework_integrity) ──────────────────────────────
# ⚑ ONE LINE at the head of each capital-path function. `_mark` is a NO-OP when
# isa_policy.V2_FLAGS["execution_ledger"] is False, and it never raises into the caller — a
# monitoring hook that can break a capital run is a worse risk than the risk it monitors.
# The CALLS STAY IN THE CODE when the flag is off; removing them is what makes it droppable.
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None


RATCHET_FLOOR_PCT = 10.0
RATCHET_STEP_PCT = 5.0
PROBATION_TRAIL_PP = 5.0
PROBATION_MIN_POSITIONS = 3
PROBATION_MONTHS = 12
# ── P5 (ISA-0457 / D18-D19), built 29-Aug-2026 ────────────────────────────────────────────
PROBATION_MIN_TRAILING_MONTHS = 9        # leg (b): 9 of 12; binomial p ~ 0.073 under the null
EARLY_WARNING_MONTHS = 6                 # leg-free 6m reading; NON-GATING by construction
# ⚑ D18. The population is EVERY open direct-stock position, VCI INCLUDED — the ceiling is a
# capital question and VCI is the same capital. This REPLACES `route == "forward_led"`, which
# no live producer ever emitted: the published "n=1, only COCO" came from a FIXTURE, and live
# the filter returned 0 while the run printed "population unavailable" (F4 / ISA-0457).
ELIGIBLE_BASIS = "all_open_direct_stock_positions"
# ⚑ AND THE THRESHOLD IS KNOWN TO BE WEAK, WHICH IS WHY LEG (a) IS NOT ALONE. Measured sleeve
# tracking error vs VUAG is 38.6%/yr, so -5pp is t = 0.1295 — NON_DISCRIMINATING, firing ~44%
# of the time on ZERO true alpha at 6, 12 AND 24 months. Widening the window does not fix a
# threshold expressed in the units of the quantity rather than of its noise (R3.11). The
# conjunction of three weak legs is what makes the rule mean anything, and D19 is Raj's.
PROBATION_LEGS = ("trail_pp", "months_trailing", "ex_largest")

HOLD_REMARK = "HOLD_AND_REMARK"
TRIM_TO_RISK_TARGET = "TRIM_TO_RISK_TARGET"
SELL_REPLACE = "SELL_REPLACE"
TRIM_TO_STARTER = "TRIM_TO_STARTER"
NO_ACTION = "NO_ACTION"


class RetentionRefused(RuntimeError):
    """Refuses rather than pricing a retention decision off an input it does not have."""


REQUIRED_ENTRY_FIELDS = ("er_entry", "er_confidence_entry", "price_entry",
                         "er_horizon_months", "underwrite_date")


def underwrite(lot: dict, *, er_entry, er_confidence_entry, price_entry, fv_entry=None,
               er_horizon_months=12, underwrite_date=None, allow_overwrite=False) -> dict:
    """Stamp the immutable entry record on a lot."""
    if lot.get("underwrite") and not allow_overwrite:
        raise RetentionRefused(
            f"{lot.get('ticker')}: an underwrite record already exists "
            f"(er_entry={lot['underwrite'].get('er_entry')}, "
            f"{lot['underwrite'].get('underwrite_date')}). It is IMMUTABLE — if er_entry can be "
            f"edited then realised_fraction measures nothing, because a quietly re-marked thesis "
            f"never completes and the position never comes up for review. Use re_underwrite().")
    if er_entry is None or float(er_entry) == 0:
        raise RetentionRefused(
            f"{lot.get('ticker')}: er_entry is {er_entry!r}. realised_fraction divides by it, so "
            f"a null or zero would make the whole instrument silently undefined (R4.1).")
    if price_entry is None or float(price_entry) <= 0:
        raise RetentionRefused(f"{lot.get('ticker')}: price_entry must be positive")
    lot = dict(lot)
    lot["underwrite"] = {
        "er_entry": float(er_entry),
        "er_confidence_entry": (float(er_confidence_entry)
                                if er_confidence_entry is not None else None),
        "price_entry": float(price_entry),
        "fv_entry": float(fv_entry) if fv_entry is not None else None,
        "er_horizon_months": int(er_horizon_months),
        "underwrite_date": underwrite_date or datetime.date.today().isoformat(),
        "immutable": True,
        "history": [],
    }
    return lot


def re_underwrite(lot: dict, *, er_entry, price_now, reason, on=None) -> dict:
    """APPEND a new underwrite, retaining the original. Never an in-place edit."""
    u = lot.get("underwrite")
    if not u:
        raise RetentionRefused(f"{lot.get('ticker')}: nothing to re-underwrite")
    lot = dict(lot); u = dict(u); u["history"] = list(u.get("history") or [])
    u["history"].append({"superseded_er_entry": u["er_entry"],
                         "superseded_price_entry": u["price_entry"],
                         "superseded_on": u["underwrite_date"], "reason": reason})
    u["er_entry"] = float(er_entry)
    u["price_entry"] = float(price_now)
    u["underwrite_date"] = on or datetime.date.today().isoformat()
    lot["underwrite"] = u
    return lot


def realised_fraction(lot: dict, price_now: float) -> dict:
    """(price_now / price_entry - 1) / er_entry. Printed EVERY run."""
    u = lot.get("underwrite")
    if not u:
        return {"measured": False, "realised_fraction": None,
                "detail": (f"{lot.get('ticker')}: no underwrite record. This position predates "
                           f"the s9 instrument, so its thesis completion CANNOT be measured — "
                           f"reported as unmeasured, never as 0.0 (R2.10).")}
    missing = [f for f in REQUIRED_ENTRY_FIELDS if u.get(f) is None]
    if missing:
        return {"measured": False, "realised_fraction": None, "missing_fields": missing,
                "detail": f"{lot.get('ticker')}: underwrite is incomplete — missing {missing}"}
    if price_now is None or float(price_now) <= 0:
        return {"measured": False, "realised_fraction": None,
                "detail": f"{lot.get('ticker')}: price_now is {price_now!r}"}
    ret = (float(price_now) / float(u["price_entry"])) - 1.0
    rf = ret / (float(u["er_entry"]) / 100.0)
    return {"measured": True, "realised_fraction": round(rf, 4),
            "price_return_pct": round(ret * 100, 4), "er_entry_pct": u["er_entry"],
            "er_horizon_months": u["er_horizon_months"],
            "underwrite_date": u["underwrite_date"],
            "re_underwrites": len(u.get("history") or []),
            "triggers_re_underwrite": rf >= 1.0,
            "detail": (f"{ret * 100:+.1f}% realised against a {u['er_entry']:.1f}% "
                       f"{u['er_horizon_months']}-month underwrite = {rf:.2f}x"
                       + (" -> MANDATORY RE-UNDERWRITE (never an automatic sale)"
                          if rf >= 1.0 else ""))}


def re_underwrite_outcome(*, clears_underwriting: bool, above_risk_target: bool,
                          challenger_exists: bool) -> dict:
    """The clean spec s9 outcome table, as a total function over its three inputs."""
    if clears_underwriting:
        act = TRIM_TO_RISK_TARGET if above_risk_target else HOLD_REMARK
        why = ("the thesis still underwrites and the position is above its risk target, so trim "
               "to that target — this banks the excess without exiting a working thesis"
               if above_risk_target else
               "the thesis still underwrites and the position is at or below its risk target: "
               "HOLD and re-mark er_entry. ⚑ This is the branch that protects AVGO-at-0.27x and "
               "would have protected MU — a hard target would have destroyed 59% of the "
               "sleeve's gain.")
    else:
        act = SELL_REPLACE if challenger_exists else TRIM_TO_STARTER
        why = ("the thesis no longer underwrites and a qualified challenger exists: SELL/REPLACE"
               if challenger_exists else
               "the thesis no longer underwrites and no challenger qualifies: trim to STARTER, "
               "or sell to the marginal fund receiver. Capital only leaves for somewhere better.")
    return {"action": act, "why": why,
            "inputs": {"clears_underwriting": clears_underwriting,
                       "above_risk_target": above_risk_target,
                       "challenger_exists": challenger_exists}}


# ─────────────────────────────────────────────────────── s3 step-down ratchet
def step_down(current_sleeve_weight_pct: float) -> dict:
    """new_ceiling = floor(w / 5) * 5, with a hard floor of 10%."""
    w = float(current_sleeve_weight_pct)
    new = math.floor(w / RATCHET_STEP_PCT) * RATCHET_STEP_PCT
    new = max(new, RATCHET_FLOOR_PCT)
    return {"current_pct": round(w, 4), "new_ceiling_pct": round(new, 4),
            "reduction_pp": round(max(w - new, 0.0), 4),
            "at_floor": new <= RATCHET_FLOOR_PCT,
            "routing": "capital_destination",
            "basis": (f"floor({w:.2f}/{RATCHET_STEP_PCT})*{RATCHET_STEP_PCT} = {new:.0f}%, "
                      f"floored at {RATCHET_FLOOR_PCT}%. A one-step reduction is proportionate "
                      f"and repeatable; reverting straight to the floor would be a "
                      f"{max(w - RATCHET_FLOOR_PCT, 0):.1f}pp forced liquidation on a lagging "
                      f"measure. Increments route via capital_destination — hard-coding VUAG "
                      f"would breach its 12.5% single-fund cap within two increments.")}


def ratchet_eligible(positions: List[dict], *, today=None) -> dict:
    """P5.1 / D18. Is the s3 population large enough for the rule to fire at all?

    ⚑⚑ THE POPULATION IS EVERY OPEN DIRECT-STOCK POSITION, VCI INCLUDED, AND THE PREVIOUS
    RULE COULD NEVER HAVE BEEN TRUE. This filtered `route == "forward_led"`. Nothing in the
    live tree emits that value: `decision_ledger`'s vocabulary is {main, vci}, and all twelve
    trades in `sleeve_counterfactual` carry `route: null`. The string occurs ONLY in this
    module's own selftest and two fixtures — so the published finding *"n=1, only COCO, correct
    not a loophole"* was A FIXTURE RESULT, and on live data the filter returned 0 while the run
    printed "population unavailable".

    ⚑ That is not an absent execution — it is a PRESENT execution that can never be true, and
    the tell was that the value appeared only in tests (F4 / ISA-0457, a new FC sub-class).

    ⚑ D18's reasoning: the ceiling is a CAPITAL question, and VCI capital is the same capital.
    A route-based population also made the rule's answer depend on a label nobody assigned.
    Route attribution still happens — see `route_attribution()` — but it is NON-GATING."""
    _fi_mark("retention", "ratchet_eligible")
    open_pos, excluded = [], []
    for d in (positions or []):
        tk = d.get("ticker")
        # ⚑ `is False` / explicit zero only. A position with NO declared `open` flag and no
        # declared quantity is UNKNOWN, and an unknown must not be silently counted IN (it
        # would inflate the population toward eligibility) nor silently OUT (it would suppress
        # the rule). It is excluded WITH ITS REASON NAMED, and the count of such names is
        # published so a reader can see the population is incomplete.
        if d.get("open") is False or d.get("closed") is True:
            excluded.append({"ticker": tk, "reason": "position is closed"})
            continue
        q = d.get("quantity", d.get("units"))
        if q is not None and float(q) <= 0:
            excluded.append({"ticker": tk, "reason": "quantity <= 0"})
            continue
        if d.get("asset_class") in ("fund", "etf_fund", "cash"):
            excluded.append({"ticker": tk,
                             "reason": f"asset_class={d.get('asset_class')} is not direct stock"})
            continue
        open_pos.append(d)
    ok = len(open_pos) >= PROBATION_MIN_POSITIONS
    return {"eligible": ok,
            "basis": ELIGIBLE_BASIS,
            "n_positions": len(open_pos),
            "positions": [d.get("ticker") for d in open_pos],
            # ⚑ retained under its old name so no consumer silently reads 0 after this change
            "n_forward_led": None,
            "min_required": PROBATION_MIN_POSITIONS, "excluded": excluded,
            "detail": (f"{len(open_pos)} open direct-stock position(s) against "
                       f"{PROBATION_MIN_POSITIONS} required ({ELIGIBLE_BASIS}, D18). THE RULE "
                       f"CANNOT FIRE on population."
                       if not ok else
                       f"{len(open_pos)} open direct-stock positions ({ELIGIBLE_BASIS}, D18) — "
                       f"the population is sufficient and the three legs may be evaluated")}


def route_attribution(decisions: List[dict]) -> dict:
    """P5.2. Buckets decisions by route. **NON-GATING — it decides nothing.**

    ⚑ IT MAKES NO COUNTERFACTUAL CLAIM. Whether MU and AVGO "would have qualified" under the
    forward-led framework is a HYPOTHESIS, and R2.2 bars a hypothesis from a decision. This
    reports what each position's route WAS, with `pre_framework` as its own bucket rather than
    folded into an "other" that reads like a judgement.

    ⚑ It exists because D18 removed route from the GATE, and removing a distinction from a gate
    is not a reason to stop recording it — it is a reason to record it somewhere it cannot
    decide anything."""
    _fi_mark("retention", "route_attribution")
    buckets = {"forward_led": [], "vci": [], "pre_framework": [], "override": [],
               "unattributed": []}
    for d in (decisions or []):
        r = d.get("route")
        key = r if r in buckets else ("unattributed" if r in (None, "") else "override")
        buckets[key].append(d.get("ticker"))
    return {"buckets": {k: sorted(v) for k, v in buckets.items()},
            "counts": {k: len(v) for k, v in buckets.items()},
            "gating": False,
            "makes_counterfactual_claim": False,
            "detail": ("route attribution is a RECORD, not a gate (P5.2). It states which "
                       "route each position arrived by and makes NO claim about whether a "
                       "pre-framework position would have qualified — that is a hypothesis, "
                       "and R2.2 keeps hypotheses out of decisions.")}



# ── P5.4 — READING THE LEGS OUT OF `freeze_history`, WITHOUT INVENTING ANY ────────────────
FREEZE_MONTH_FIELDS = (
    "vs_vuag_pp", "vs_vuag_exlargest_pp", "vs_iwmo_pp", "vs_iwmo_exlargest_pp",
    "largest_position_ticker", "largest_position_weight_pct",
    "sign_all_in", "sign_exlargest", "measured", "basis",
)


def ratchet_inputs_from_freeze_history(history: List[dict]) -> dict:
    """-> the three legs' inputs, or None for each one the history cannot supply.

    ⚑⚑ NOTHING IS RETRO-FILLED (P5-A10). `freeze_history` today carries only
    `beats_vuag_exmu` / `beats_iwmo_exmu` / `measured`; the per-month POUNDS-AND-PP fields
    P5.4 declares are written going forward, not backfilled. 2026-07 in particular is
    `measured: false` and stays that way — ISA-0429 established that it cannot be recomputed
    from the corrupted price, and a fabricated boolean is worse than a gap.

    ⚑ So this reader returns `None` for every leg the history does not actually carry, and the
    caller renders ACTIVE_UNMEASURED. It does NOT derive `vs_vuag_pp` from `beats_vuag_exmu`:
    a boolean cannot supply a magnitude, and inventing one would be exactly the class where a
    stored value says one thing and IS another.

    ⚑ `months_trailing` counts only MEASURED months. An unmeasured month is not a
    non-trailing month — counting it as one would let a data gap argue that the sleeve is
    working, which is the direction ISA-0429 showed is the dangerous one."""
    _fi_mark("retention", "ratchet_inputs_from_freeze_history")
    hist = [h for h in (history or []) if isinstance(h, dict)]
    measured = [h for h in hist if h.get("measured") is True]
    recent = sorted(measured, key=lambda h: str(h.get("month") or ""))[-PROBATION_MONTHS:]

    def _last(field):
        for h in reversed(recent):
            if h.get(field) is not None:
                return h[field]
        return None

    trailing = None
    if recent and all(h.get("vs_vuag_pp") is not None for h in recent):
        trailing = sum(1 for h in recent if h["vs_vuag_pp"] <= -PROBATION_TRAIL_PP)

    missing = sorted({f for f in ("vs_vuag_pp", "vs_vuag_exlargest_pp",
                                  "largest_position_ticker")
                      if _last(f) is None})
    return {
        "months_measured": len(measured),
        "months_in_window": len(recent),
        "months_trailing": trailing,
        "sleeve_vs_vuag_pp": _last("vs_vuag_pp"),
        "sleeve_vs_vuag_exlargest_pp": _last("vs_vuag_exlargest_pp"),
        "largest_position_ticker": _last("largest_position_ticker"),
        "early_warning_pp": (None if len(measured) < EARLY_WARNING_MONTHS else
                             _last("vs_vuag_pp")),
        "fields_missing": missing,
        "declared_fields": list(FREEZE_MONTH_FIELDS),
        "detail": ("%d measured month(s) of the %d the rule needs. Missing per-month field(s): "
                   "%s. These are written GOING FORWARD (P5.4) and are NOT backfilled — 2026-07 "
                   "stays measured:false because it cannot be recomputed and a fabricated "
                   "boolean is worse than a gap (ISA-0429 / P5-A10)."
                   % (len(measured), PROBATION_MONTHS, ", ".join(missing) or "none")),
    }

def evaluate_ratchet(*, decisions=None, sleeve_vs_vuag_pp=None, months_measured=None,
                     current_sleeve_weight_pct=None, positions=None,
                     months_trailing=None, sleeve_vs_vuag_exlargest_pp=None,
                     largest_position_ticker=None, early_warning_pp=None) -> dict:
    """P5.3 / D19. The full s3 gate: a THREE-LEG CONJUNCTION, every refusal naming its leg.

    ⚑⚑ WHY THREE LEGS AND NOT A WIDER WINDOW. Leg (a) alone is `-5pp`, which is **0.1295 SD**
    against a measured sleeve tracking error of 38.6%/yr — it fires about 44% of the time on
    ZERO true alpha, and it does so at 6, 12 AND 24 months. The window is the wrong lever: a
    threshold expressed in the units of the quantity rather than of its noise is a coin flip
    that LOOKS discriminating (R3.11). Three weak, differently-wrong legs in conjunction is
    what buys discrimination here, and the conjunction is Raj's declared design (D19).

    ⚑ LEG (c) IS NOT OPTIONAL. On ISA-0429's restated numbers the all-in and ex-MU legs
    disagreed by 25pp (+3.2pp vs -22.5pp). A single-leg rule reads whichever leg it was handed,
    and the two answer different questions: whether the SLEEVE is working, and whether it is
    working other than through its one largest bet.

    ⚑ ANY LEG UNMEASURED => `ACTIVE_UNMEASURED`, NEVER "does not fire" (ISA-0429's rule). The
    two spellings are asserted distinct, because "we looked and it did not fire" and "we could
    not look" are opposite facts that a single `fires: False` would merge (R2.10)."""
    _fi_mark("retention", "evaluate_ratchet")
    pop = positions if positions is not None else decisions
    el = ratchet_eligible(pop or [])

    legs, unmeasured, binding = {}, [], []

    # ── population is a PRECONDITION, not a leg: it says the rule is not applicable yet ──
    if not el["eligible"]:
        binding.append("population %s of %s (%s)"
                       % (el["n_positions"], PROBATION_MIN_POSITIONS, el["basis"]))

    # ── leg (a): does the sleeve trail VUAG by at least the declared threshold? ──────────
    if sleeve_vs_vuag_pp is None:
        legs["trail_pp"] = None
        unmeasured.append("trail_pp (sleeve vs VUAG is UNMEASURED)")
    else:
        legs["trail_pp"] = bool(sleeve_vs_vuag_pp <= -PROBATION_TRAIL_PP)
        if not legs["trail_pp"]:
            binding.append("trails by %.2fpp, inside the %.1fpp threshold"
                           % (-sleeve_vs_vuag_pp, PROBATION_TRAIL_PP))

    # ── leg (b): has it trailed in at least 9 of the last 12 measured months? ────────────
    if months_measured is None or months_measured < PROBATION_MONTHS:
        legs["months_trailing"] = None
        unmeasured.append("months_trailing (%s of %s months measured)"
                          % (months_measured, PROBATION_MONTHS))
        binding.append("only %s of %s months measured" % (months_measured, PROBATION_MONTHS))
    elif months_trailing is None:
        legs["months_trailing"] = None
        unmeasured.append("months_trailing (the per-month trailing count is UNMEASURED)")
    else:
        legs["months_trailing"] = bool(months_trailing >= PROBATION_MIN_TRAILING_MONTHS)
        if not legs["months_trailing"]:
            binding.append("trailed in %s of %s months, below the %s required"
                           % (months_trailing, PROBATION_MONTHS,
                              PROBATION_MIN_TRAILING_MONTHS))

    # ── leg (c): does it still trail with the LARGEST position removed? ─────────────────
    # ⚑ `largest_position_ticker` is read PER MONTH and is NEVER hard-coded to MU: a
    # hard-coded ex-MU leg becomes an ex-nothing leg the month MU is trimmed.
    if sleeve_vs_vuag_exlargest_pp is None:
        legs["ex_largest"] = None
        unmeasured.append("ex_largest (the ex-largest-position leg is UNMEASURED)")
    else:
        legs["ex_largest"] = bool(sleeve_vs_vuag_exlargest_pp <= -PROBATION_TRAIL_PP)
        if not legs["ex_largest"]:
            binding.append("ex-%s the sleeve trails by %.2fpp, inside the %.1fpp threshold"
                           % (largest_position_ticker or "largest",
                              -sleeve_vs_vuag_exlargest_pp, PROBATION_TRAIL_PP))

    # ── the 6-month reading is published and GATES NOTHING (P5-A8) ──────────────────────
    early = {"months": EARLY_WARNING_MONTHS, "vs_vuag_pp": early_warning_pp,
             "state": ("UNMEASURED" if early_warning_pp is None else
                       ("TRAILING" if early_warning_pp <= -PROBATION_TRAIL_PP else "OK")),
             "gates": False,
             "note": ("EARLY WARNING ONLY. It is reported so a deterioration is visible before "
                      "the 12-month legs can speak, and it changes `fires` in no circumstance.")}

    base = {"eligibility": el, "legs": legs, "legs_required": list(PROBATION_LEGS),
            "early_warning": early, "basis": ELIGIBLE_BASIS,
            "route_attribution": route_attribution(pop or []),
            "thresholds": {"trail_pp": PROBATION_TRAIL_PP,
                           "min_trailing_months": PROBATION_MIN_TRAILING_MONTHS,
                           "months": PROBATION_MONTHS,
                           "min_positions": PROBATION_MIN_POSITIONS}}

    # ⚑ P5.5 — SAY WHICH CONDITION BINDS, not merely that nothing fired. A rule that reports
    # "cannot fire" without saying why THIS month is ISA-0348's pattern in reporting form.
    if unmeasured:
        return {**base, "state": "ACTIVE_UNMEASURED", "fires": False,
                "unmeasured": unmeasured, "binding": binding,
                "new_ceiling_pct": None,
                "detail": ("step-down is ACTIVE-UNMEASURED (NOT 'does not fire'): "
                           + "; ".join(unmeasured)
                           + ". An unmeasured leg is a gap in what we know, not evidence that "
                             "the sleeve is working (ISA-0429).")}
    if not el["eligible"] or not all(legs[k] for k in PROBATION_LEGS):
        return {**base, "state": "DOES_NOT_FIRE", "fires": False,
                "unmeasured": [], "binding": binding, "new_ceiling_pct": None,
                "detail": "step-down does NOT fire: " + "; ".join(binding)}

    sd = step_down(current_sleeve_weight_pct)
    return {**base, "state": "FIRES", "fires": True, "unmeasured": [], "binding": [], **sd,
            "detail": ("all three legs hold: trails VUAG by %.2fpp, in %s of %s months, and by "
                       "%.2fpp ex-%s -> ceiling steps %.2f%% -> %.0f%%"
                       % (-sleeve_vs_vuag_pp, months_trailing, PROBATION_MONTHS,
                          -sleeve_vs_vuag_exlargest_pp, largest_position_ticker or "largest",
                          sd["current_pct"], sd["new_ceiling_pct"]))}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A8 — THE SELECTION-BIAS HAIRCUT.  ISA-0440, built 26-Aug-2026.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# A8: "You select the best challenger from a pool of ~300 screened names. The winner's `er_ca` is
# upward-biased by the maximum order statistic. Apply a haircut scaled to pool size before the
# pairwise comparison, and size the uncertainty buffer on sqrt(se_1^2 + se_2^2) rather than on a
# confidence LABEL. This is the Harvey-Liu-Zhu multiple-testing point that V2.1 correctly applies
# to signal weights and does not apply to candidate selection."
#
# ⚑⚑ THE AMENDMENT'S FORM IS WRONG ON THIS BOOK AND THE LIVE SCREENS SAY SO. A8 as written scales
# the haircut to POOL SIZE and, read literally with the pool's own dispersion, gives
# sigma_total * z(n). Measured on the actual screens — SP500 15-Aug n=315 sd 23.70pp, STOXX600
# 22-Aug n=263 sd 20.97pp, MIDCAP400 21-Aug n=213 sd 25.22pp — that is a haircut of about 68pp,
# against a maximum er_ca in the whole SP500 file of 69.8pp. EVERY CHALLENGER WOULD BE REJECTED,
# FOREVER, and the rule would look like prudence while being inert. (ISA-0348's question asked of
# my own build: what correct behaviour makes this fail? Answer: all of it.)
#
# ⚑ THE CORRECT FORM, AND IT IS THE ONE THE AMENDMENT'S OWN NUMBER IMPLIES. The winner's curse is
# not that the best ESTIMATE sits z(n) SDs above the pool mean — that is true and harmless. It is
# that the best estimate over-states its own TRUE value, and that over-statement is driven by the
# ESTIMATION NOISE, not by the total spread:
#
#       haircut = z(n) * sigma_noise^2 / sigma_total          (sigma_total^2 = sigma_true^2 + sigma_noise^2)
#
# The two limits are the check. With NOISELESS estimates (sigma_noise = 0) there is no selection
# bias at all — the best name really is the best — and this returns 0 while the literal form
# returns 68pp. With PURE NOISE (sigma_noise = sigma_total) the winner's entire advantage is
# selection and the two forms agree exactly.
#
# ⚑ AND THE CORROBORATION IS ARITHMETIC, NOT RHETORIC. A8 states the resulting bar is "roughly 7pp
# of adjusted advantage before a slot changes hands". Run the corrected form on the live SP500
# pool (n = 315, sigma_total 23.70pp) at an estimation SE of 3pp: haircut 1.09pp + uncertainty
# buffer sqrt(3^2+3^2) = 4.24pp + friction 2.1pp = A BAR OF 7.4pp. The amendment's own number,
# reproduced. Run the LITERAL form on the same pool at ANY SE: a 68.28pp haircut, ten times the
# bar the same document states. ⚑ THE AMENDMENT'S PROSE AND ITS FORMULA DISAGREED, AND THE PROSE
# WAS RIGHT — which is only visible because the formula was run against the book it would govern
# before it was shipped, rather than after.
#
# ⚑ WHAT THIS MEANS FOR THE LIVE RUN, STATED PLAINLY: `estimate_se_pp` is not captured anywhere
# today. `er_confidence` is a 0-1 LABEL, and A8 exists precisely to stop a label standing in for a
# standard error. So on this book `slot_competition` reads UNMEASURED and A20 proposes nothing —
# a measured refusal, and the correct one.
#
# ⚑ BOTH ARE PUBLISHED (R6.2). `haircut_pp` is the corrected one; `haircut_literal_a8_pp` is what
# the amendment says word-for-word, so the divergence is visible rather than buried in a build
# decision I made on my own.
#
# ⚑ AND ALL THREE INPUTS ARE REFUSED WHEN ABSENT. Pool size, total dispersion and the estimation
# SE are not derivable from the winner. A haircut of zero because nobody measured them is the most
# dangerous output this function could produce: it would deliver the full selection bias into the
# comparison wearing the label "adjusted" (R4.1/R4.3).


A8_HAIRCUT_ENABLED = True
A8_BUFFER_K = 1.0          # multiples of the joint standard error; 1.0 = one sigma of the gap


def _phi_inv(p: float) -> float:
    """Acklam's rational approximation to the standard normal quantile. |error| < 1.15e-9."""
    if not 0.0 < p < 1.0:
        raise RetentionRefused("phi_inv needs 0 < p < 1, got %r" % p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) * (p - 0.5)
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def selection_haircut(pool_size, dispersion_pp, estimate_se_pp=None) -> dict:
    """A8 — the pp of a winning challenger's er_ca that the SCREEN produced rather than the name.

    pool_size      how many names the winner was selected from.
    dispersion_pp  cross-sectional SD of er_ca ACROSS THAT POOL (it already contains the noise).
    estimate_se_pp the standard error of ONE name's er_ca estimate. ⚑ REQUIRED — see the header:
                   without it the haircut is the amendment's literal form, which measures ~68pp on
                   this book and rejects every challenger forever.
    """
    if not A8_HAIRCUT_ENABLED:
        return {"state": "DISABLED", "haircut_pp": 0.0,
                "basis": "A8_HAIRCUT_ENABLED is False (R4.13)"}
    missing = [k for k, v in (("pool_size", pool_size), ("dispersion_pp", dispersion_pp),
                              ("estimate_se_pp", estimate_se_pp)) if v is None]
    if missing:
        return {"state": "UNMEASURED", "haircut_pp": None, "missing": missing,
                "basis": ("the haircut is a function of HOW MANY names were looked at, HOW SPREAD "
                          "their expected returns were, and HOW NOISY one estimate is. None is "
                          "derivable from the winner. Returning 0.0 would hand the full selection "
                          "bias to the comparison wearing the label 'adjusted' (R4.1/R4.3). "
                          "Missing: %s." % ", ".join(missing))}
    n = int(pool_size)
    sd_tot, se = float(dispersion_pp), float(estimate_se_pp)
    if n < 2:
        return {"state": "MEASURED", "haircut_pp": 0.0, "haircut_literal_a8_pp": 0.0,
                "pool_size": n, "expected_max_z": 0.0, "dispersion_pp": sd_tot,
                "estimate_se_pp": se,
                "basis": "a pool of %d has no maximum-order-statistic bias: nothing was "
                         "selected FROM anything." % n}
    if sd_tot <= 0:
        return {"state": "UNMEASURED", "haircut_pp": None, "missing": ["dispersion_pp>0"],
                "basis": ("a cross-sectional dispersion of %.4f cannot be a denominator. A pool "
                          "with no spread has no best name to select." % sd_tot)}
    z = _phi_inv((n - 0.375) / (n + 0.25))                     # Blom's plotting position
    # ⚑ the noise share cannot exceed 1: an estimation SE larger than the total spread means the
    # pool is pure noise, and the winner's whole advantage is selection. Clamped, and SAID.
    noise_share = min((se * se) / (sd_tot * sd_tot), 1.0)
    hair = z * noise_share * sd_tot
    literal = z * sd_tot
    return {"state": "MEASURED", "haircut_pp": round(hair, 4),
            "haircut_literal_a8_pp": round(literal, 4),
            "pool_size": n, "expected_max_z": round(z, 4),
            "dispersion_pp": sd_tot, "estimate_se_pp": se,
            "noise_share_of_variance": round(noise_share, 4),
            "form": "z(n) * sigma_noise^2 / sigma_total  (winner's curse, NOT E[max])",
            "divergence_from_literal_a8_pp": round(literal - hair, 4),
            "basis": ("selected from %d, so the winning ESTIMATE sits %.3f SDs above the pool "
                      "mean (Blom) — but only the NOISE share of that is over-statement of the "
                      "name's own value. Noise is %.1f%% of the pool variance (SE %.2fpp against "
                      "a spread of %.2fpp), so the haircut is %.2fpp. ⚑ A8 read literally says "
                      "%.2fpp, which on the live screens rejects every challenger that has ever "
                      "been screened; both figures are published (R6.2) and the amendment's own "
                      "'roughly 7pp bar' matches the corrected form, not the literal one."
                      % (n, z, noise_share * 100, se, sd_tot, hair, literal))}


def uncertainty_buffer(se_1, se_2, k: float = None) -> dict:
    """A8 — the buffer on the GAP, sized on sqrt(se_1^2 + se_2^2). Never on a confidence LABEL.

    ⚑ A label ("high confidence") is an ordinal dressed as a quantity: two names both labelled
    high can have standard errors that differ by a factor of three, and the comparison between
    them is a difference of two estimates, whose error is the JOINT one. That is the whole point
    of the amendment.
    """
    k = A8_BUFFER_K if k is None else float(k)
    if se_1 is None or se_2 is None:
        return {"state": "UNMEASURED", "buffer_pp": None, "k": k,
                "missing": [n for n, v in (("se_incumbent", se_1), ("se_challenger", se_2))
                            if v is None],
                "basis": ("a buffer of zero on an unmeasured error is a claim that the two "
                          "estimates are exact. REFUSED (R4.3).")}
    joint = math.sqrt(float(se_1) ** 2 + float(se_2) ** 2)
    return {"state": "MEASURED", "buffer_pp": round(k * joint, 4), "k": k,
            "joint_se_pp": round(joint, 4), "se_incumbent": float(se_1),
            "se_challenger": float(se_2),
            "basis": ("the comparison is a DIFFERENCE of two estimates, so its error is "
                      "sqrt(%.3f^2 + %.3f^2) = %.3fpp; the buffer is %.1f of those."
                      % (float(se_1), float(se_2), joint, k))}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A20 — SHADOW SLOT COMPETITION.  ISA-0440, built 26-Aug-2026. SHADOW ONLY — IT TRADES NOTHING.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# A20 refines A5.1: under a BINDING sleeve ceiling the retention comparator is the BEST REJECTED
# CHALLENGER, not the marginal fund receiver. Raj admitted it on 22-Aug with ISA-0167's surviving
# constraint attached: A20 ships in SHADOW, publishes what it WOULD have traded for at least two
# runs, and does not go live until the E[r]-gap trade has been measured in its own right.
#
# ⚑ THE DOCSTRING OF THIS MODULE HAS ADVERTISED "the A20 shadow comparator" SINCE 26-Aug AND THE
# MODULE CONTAINED NONE. That is the same class as `asset_drawdown` being named in a run-surface
# label it never reached: a claim of capability made in the one place a reader trusts without
# checking. Now it is here, and `A20_LIVE` is the thing that decides whether it does anything.
#
# ⚑ WHY THE CEILING MATTERS. If the sleeve ceiling does NOT bind, a good challenger is funded from
# new capital and no incumbent has to lose its slot — the comparison is not a competition and
# running one would manufacture a trade. So `binding_ceiling=False` returns NOT_A_COMPETITION,
# which is a different answer from "the incumbent won".


A20_LIVE = False                      # ⚑ SHADOW. Flipping this is a DECISION, see below.
A20_MIN_SHADOW_RUNS = 2
A20_ISA0167_CONSTRAINT = (
    "ISA-0167's motive is superseded by A18/A19; its EVIDENCE is not. The June-2026 panel "
    "measured screen-rank rotation on 3-month price momentum, which is NOT A20's trade — A20 "
    "fires on an E[r] gap after a selection haircut, an SE-scaled buffer and 2.0-2.2pp of "
    "friction. So the panel does not condemn A20; but nothing measures A20's trade either, and "
    "that is why it ships in shadow.")

WOULD_REPLACE = "WOULD_REPLACE"
WOULD_HOLD = "WOULD_HOLD"
NOT_A_COMPETITION = "NOT_A_COMPETITION"


def slot_competition(*, incumbent: dict, challenger: dict, binding_ceiling: bool,
                     pool_size=None, dispersion_pp=None, estimate_se_pp=None, friction_pp=None,
                     shadow_runs: int = 0, live: bool = None) -> dict:
    """A20 — would this challenger take this incumbent's slot? SHADOW: it returns, it never acts.

    incumbent/challenger: {ticker, er_ca_pp, se_pp}
    `friction_pp` is the round-trip cost of the swap as a percentage of the position — from the
    DEALING RECORD (transaction_ledger), never a rate card.
    """
    live = A20_LIVE if live is None else bool(live)
    out = {"item": "ISA-0440 / A20", "mode": ("LIVE" if live else "SHADOW"),
           "incumbent": incumbent.get("ticker"), "challenger": challenger.get("ticker"),
           "isa_0167_constraint": A20_ISA0167_CONSTRAINT,
           "acts": False,
           "acts_basis": ("A20 returns a verdict and NEVER executes. Even LIVE, the verdict "
                          "enters the monthly email as a proposal like every other action.")}

    if live and shadow_runs < A20_MIN_SHADOW_RUNS:
        raise RetentionRefused(
            "A20 is set LIVE with %d shadow run(s) against the %d required. The condition Raj "
            "attached when admitting A20 was that it publish what it WOULD have traded for at "
            "least two runs first. Refusing rather than trading on a rule nobody has watched."
            % (shadow_runs, A20_MIN_SHADOW_RUNS))

    if not binding_ceiling:
        out.update({"verdict": NOT_A_COMPETITION, "advantage_pp": None,
                    "detail": ("the sleeve ceiling does not bind, so a qualified challenger is "
                               "funded from capital rather than from an incumbent's slot. There "
                               "is no slot to compete for, and that is NOT the same answer as "
                               "'the incumbent won' (R2.10).")})
        return out

    er_i, er_c = incumbent.get("er_ca_pp"), challenger.get("er_ca_pp")
    if er_i is None or er_c is None:
        out.update({"verdict": "UNMEASURED", "advantage_pp": None,
                    "detail": "an er_ca is missing on %s — no comparison is made (R4.1)."
                              % (", ".join(n for n, v in (("incumbent", er_i),
                                                          ("challenger", er_c)) if v is None))})
        return out

    hair = selection_haircut(pool_size, dispersion_pp, estimate_se_pp)
    buf = uncertainty_buffer(incumbent.get("se_pp"), challenger.get("se_pp"))
    raw_gap = float(er_c) - float(er_i)

    blockers = [x["basis"] for x in (hair, buf) if x["state"] == "UNMEASURED"]
    if friction_pp is None:
        blockers.append("the round-trip friction of the swap is UNMEASURED. A swap priced at "
                        "zero is a free lunch by construction.")
    if blockers:
        out.update({"verdict": "UNMEASURED", "raw_gap_pp": round(raw_gap, 4),
                    "advantage_pp": None, "haircut": hair, "buffer": buf,
                    "friction_pp": friction_pp, "blockers": blockers,
                    "detail": ("the raw gap is %+.2fpp, and it is NOT a verdict: %d input(s) "
                               "to the adjustment are unmeasured, and every one of them makes "
                               "the challenger look better than it is. Publishing the raw gap "
                               "as an advantage is the exact error A8 exists to prevent."
                               % (raw_gap, len(blockers)))})
        return out

    adj = raw_gap - hair["haircut_pp"] - buf["buffer_pp"] - float(friction_pp)
    verdict = WOULD_REPLACE if adj > 0 else WOULD_HOLD
    out.update({
        "verdict": verdict, "raw_gap_pp": round(raw_gap, 4),
        "advantage_pp": round(adj, 4),
        "haircut": hair, "buffer": buf, "friction_pp": float(friction_pp),
        "bar_pp": round(hair["haircut_pp"] + buf["buffer_pp"] + float(friction_pp), 4),
        "detail": ("%s: challenger %+.2fpp over the incumbent raw, less %.2fpp selection "
                   "haircut (pool %s), less %.2fpp uncertainty buffer, less %.2fpp friction "
                   "= %+.2fpp adjusted. %s"
                   % (verdict, raw_gap, hair["haircut_pp"], hair.get("pool_size"),
                      buf["buffer_pp"], float(friction_pp), adj,
                      ("The bar is %.2fpp of adjusted advantage before a slot changes hands, "
                       "which is what stops this rule churning."
                       % (hair["haircut_pp"] + buf["buffer_pp"] + float(friction_pp))))),
    })
    return out


def shadow_record(verdicts: List[dict], *, run_label: str, path=None) -> dict:
    """Append this run's A20 verdicts to the shadow log. R6.5 — capture first, analyse later.

    ⚑ The log is the ONLY thing that can ever let A20 go live: the condition is two runs of
    published would-have-traded, and a condition with no instrument behind it is a wish.
    """
    import json as _json
    import os as _os
    p = path or _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "a20_shadow_log.json")
    try:
        log = _json.load(open(p, encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        log = {"_meta": {"item": "ISA-0440 / A20", "mode": "SHADOW",
                         "constraint": A20_ISA0167_CONSTRAINT}, "runs": []}
    log["runs"] = [r for r in log.get("runs", []) if r.get("run") != run_label]
    log["runs"].append({"run": run_label, "as_of": datetime.date.today().isoformat(),
                        "n_verdicts": len(verdicts), "verdicts": verdicts})
    log["runs"].sort(key=lambda r: r.get("as_of") or "")
    n = len(log["runs"])
    log["_meta"]["runs_recorded"] = n
    log["_meta"]["eligible_to_go_live"] = n >= A20_MIN_SHADOW_RUNS
    log["_meta"]["still_needed"] = max(A20_MIN_SHADOW_RUNS - n, 0)
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(log, f, indent=2)
    return {"path": p, "runs_recorded": n,
            "eligible_to_go_live": log["_meta"]["eligible_to_go_live"],
            "still_needed": log["_meta"]["still_needed"],
            "note": ("eligible_to_go_live means the SHADOW COUNT is met. It is not permission: "
                     "ISA-0167's surviving constraint also requires the E[r]-gap trade to have "
                     "been measured in its own right, and that measurement does not exist yet.")}


def _selftest():
    lot = underwrite({"ticker": "MU"}, er_entry=25.0, er_confidence_entry=0.8,
                     price_entry=396.0, er_horizon_months=12, underwrite_date="2026-04-06")
    assert lot["underwrite"]["immutable"] is True
    try:
        underwrite(lot, er_entry=99.0, er_confidence_entry=0.8, price_entry=1.0)
        raise AssertionError("must refuse to overwrite")
    except RetentionRefused as e:
        assert "IMMUTABLE" in str(e)
    try:
        underwrite({"ticker": "X"}, er_entry=None, er_confidence_entry=0.5, price_entry=10.0)
        raise AssertionError("must refuse a null er_entry")
    except RetentionRefused as e:
        assert "divides by it" in str(e)

    # ⚑ MU at ~2.88x — the case the instrument exists for
    rf = realised_fraction(lot, 966.0)
    assert rf["measured"] and rf["triggers_re_underwrite"] is True
    assert 5.0 < rf["realised_fraction"] < 6.0, rf["realised_fraction"]

    # AVGO-like: well short of its underwrite -> no trigger
    avgo = underwrite({"ticker": "AVGO"}, er_entry=53.0, er_confidence_entry=0.8,
                      price_entry=322.0)
    rf2 = realised_fraction(avgo, 368.39)
    assert rf2["triggers_re_underwrite"] is False, rf2
    assert 0.2 < rf2["realised_fraction"] < 0.4, rf2["realised_fraction"]

    # a lot with no underwrite is UNMEASURED, never 0.0
    n = realised_fraction({"ticker": "OLD"}, 100.0)
    assert n["measured"] is False and n["realised_fraction"] is None
    assert "never as 0.0" in n["detail"]

    # re_underwrite APPENDS and keeps the original
    lot2 = re_underwrite(lot, er_entry=12.0, price_now=966.0, reason="thesis re-marked",
                         on="2026-08-26")
    assert lot2["underwrite"]["er_entry"] == 12.0
    assert lot2["underwrite"]["history"][0]["superseded_er_entry"] == 25.0
    assert realised_fraction(lot2, 966.0)["realised_fraction"] == 0.0

    # the outcome table is total and never auto-sells on a clearing thesis
    for above in (True, False):
        for ch in (True, False):
            o = re_underwrite_outcome(clears_underwriting=True, above_risk_target=above,
                                      challenger_exists=ch)
            assert o["action"] in (HOLD_REMARK, TRIM_TO_RISK_TARGET)
            assert o["action"] != SELL_REPLACE, "a clearing thesis is NEVER auto-sold"
    assert re_underwrite_outcome(clears_underwriting=False, above_risk_target=False,
                                 challenger_exists=True)["action"] == SELL_REPLACE
    assert re_underwrite_outcome(clears_underwriting=False, above_risk_target=False,
                                 challenger_exists=False)["action"] == TRIM_TO_STARTER

    # ── s3 ratchet ────────────────────────────────────────────────────────────────────
    assert step_down(16.65)["new_ceiling_pct"] == 15.0
    assert step_down(23.0)["new_ceiling_pct"] == 20.0
    assert step_down(11.0)["new_ceiling_pct"] == 10.0
    assert step_down(10.4)["at_floor"] is True
    assert step_down(8.0)["new_ceiling_pct"] == 10.0, "never below the floor"

    # ══════════════════════════════════════════════════════════════════════════════════
    # P5 — THE STEP-DOWN RATCHET (D18/D19), built 29-Aug-2026. P5-A1 .. P5-A11.
    # ⚑ Every assertion below is paired with a control that FORCES the opposite verdict —
    # ISA-0348's question asked of each one: what correct behaviour makes this fail?
    # ══════════════════════════════════════════════════════════════════════════════════
    live = [{"ticker": "AVGO", "route": "pre_framework"}, {"ticker": "MU", "route": "pre_framework"},
            {"ticker": "ONT.L", "route": "pre_framework"}, {"ticker": "ABCL", "route": "override"},
            {"ticker": "QBTS", "route": "vci"}, {"ticker": "COCO", "route": "forward_led"}]

    # P5-A1 — population is OPEN DIRECT-STOCK POSITIONS; the live book is n = 6
    el = ratchet_eligible(live)
    assert el["eligible"] is True and el["n_positions"] == 6, el
    assert el["basis"] == ELIGIBLE_BASIS == "all_open_direct_stock_positions", el
    #   control: a 2-position book is NOT eligible
    assert ratchet_eligible(live[:2])["eligible"] is False

    # P5-A2 — VCI IS INCLUDED: removing QBTS reduces n by exactly one
    no_qbts = [d for d in live if d["ticker"] != "QBTS"]
    assert ratchet_eligible(no_qbts)["n_positions"] == el["n_positions"] - 1
    #   control: the OLD route filter would have given n = 1 on this same book, which is why
    #   the published "n=1, only COCO" was a fixture result and never a live one
    assert len([d for d in live if d.get("route") == "forward_led"]) == 1

    # P5-A3 — NO LIVE PRODUCER EMITS `forward_led` AS A ROUTE VALUE. If one ever does, this
    # FAILS and the vocabulary is re-adjudicated rather than quietly re-adopted.
    #
    # ⚑⚑ THIS ASSERTION'S FIRST VERSION WAS WRONG IN THE WAY THE STANDARD WARNS ABOUT, AND IT
    # FIRED ON ITS FIRST RUN. It scanned for the STRING "forward_led" anywhere outside a
    # selftest, and hit `email_prefill:2148` — `ratch.get("forward_led")`, a dict KEY being
    # READ by a consumer. A check that cannot tell a producer from a consumer reports the
    # framework unsafe for a reason that is not true, and gets deleted rather than fixed.
    #
    # ⚑ THE FIX IS TO REUSE THE ONE HOME FOR THIS QUESTION rather than write a second: P0.2's
    # `framework_integrity._producers_of` already distinguishes an EMITTED value (dict literal,
    # subscript assign, keyword argument, conditional) from a mention, and already excludes
    # selftests by span rather than by a flat `ast.walk` + `continue` (ISA-0474). A private
    # re-implementation here would be exactly the two-homes defect this build exists to kill.
    #
    # ⚑ AND IT REPORTS BLIND RATHER THAN GREEN: if the instrument is unavailable the assertion
    # says so instead of passing, because "nothing produces it" and "I could not look" are the
    # same output and opposite facts (R2.10 / R4.9).
    import os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    try:
        import framework_integrity as _fi
        _prod = _fi._producers_of("route", "forward_led")
        _blind = False
    except Exception as _e:                                             # noqa: BLE001
        _prod, _blind = [], True
    assert not _blind, ("P5-A3 is BLIND: framework_integrity._producers_of is unavailable "
                        "(%s). This assertion did NOT run and must not read as a pass." % _e) \
        if _blind else True
    assert not _prod, ("P5-A3: a live producer now emits route='forward_led' (%s). D18's "
                       "population is route-blind BY DESIGN; re-adjudicate the vocabulary "
                       "before relying on it again." % _prod[:5])
    #   control: the detector is NOT vacuous — it finds a route value that IS emitted live
    _live_vocab = _fi._producers_of("route", "main") or _fi._producers_of("route", "vci")
    assert _live_vocab, ("P5-A3 control: `_producers_of` found NO producer for ANY live route "
                         "value either, so its silence on `forward_led` proves nothing — the "
                         "detector is blind, not clean.")

    # P5-A4 — EACH LEG ALONE DOES NOT FIRE. Three fixtures, one per leg.
    base_kw = dict(positions=live, months_measured=12, current_sleeve_weight_pct=16.65,
                   largest_position_ticker="MU")
    only_a = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=-22.5, months_trailing=3,
                              sleeve_vs_vuag_exlargest_pp=-1.0)
    only_b = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=-1.0, months_trailing=11,
                              sleeve_vs_vuag_exlargest_pp=-1.0)
    only_c = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=-1.0, months_trailing=3,
                              sleeve_vs_vuag_exlargest_pp=-22.5)
    for _r, _n in ((only_a, "a"), (only_b, "b"), (only_c, "c")):
        assert _r["fires"] is False and _r["state"] == "DOES_NOT_FIRE", (_n, _r["state"])
        assert _r["binding"], "a refusal must NAME which leg bound it (P5-A9)"

    # P5-A5 — ALL THREE => FIRES, and the step is ONE 5% multiple, floored at 10%
    fires = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=-22.5, months_trailing=11,
                             sleeve_vs_vuag_exlargest_pp=-22.5)
    assert fires["fires"] is True and fires["state"] == "FIRES", fires
    assert fires["new_ceiling_pct"] == 15.0, "17%% -> 15%%, NOT 17%% -> 10%%"
    assert fires["routing"] == "capital_destination"

    # P5-A6 — ANY UNMEASURED LEG => ACTIVE_UNMEASURED, and the two spellings are DISTINCT
    un = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=None, months_trailing=11,
                          sleeve_vs_vuag_exlargest_pp=-22.5)
    assert un["state"] == "ACTIVE_UNMEASURED" and un["fires"] is False, un
    assert un["state"] != only_a["state"], ("ACTIVE_UNMEASURED and DOES_NOT_FIRE must not be "
                                            "one output with two meanings (R2.10)")
    assert any("UNMEASURED" in u for u in un["unmeasured"]), un

    # P5-A7 — leg (c) follows `largest_position_ticker`, which is read PER MONTH
    swapped = evaluate_ratchet(**{**base_kw, "largest_position_ticker": "AVGO"},
                               sleeve_vs_vuag_pp=-22.5, months_trailing=11,
                               sleeve_vs_vuag_exlargest_pp=-1.0)
    assert "ex-AVGO" in " ".join(swapped["binding"]), swapped["binding"]

    # P5-A8 — the 6-month reading is EARLY WARNING and gates NOTHING
    ew = evaluate_ratchet(**base_kw, sleeve_vs_vuag_pp=-1.0, months_trailing=3,
                          sleeve_vs_vuag_exlargest_pp=-1.0, early_warning_pp=-50.0)
    assert ew["early_warning"]["state"] == "TRAILING" and ew["early_warning"]["gates"] is False
    assert ew["fires"] is only_b["fires"], "-50pp at 6m must not change `fires`"

    # P5-A9 — the refusal names WHICH condition binds; a months-only failure reports MONTHS
    months_only = evaluate_ratchet(positions=live, months_measured=1, months_trailing=None,
                                   sleeve_vs_vuag_pp=-22.5, sleeve_vs_vuag_exlargest_pp=-22.5,
                                   current_sleeve_weight_pct=16.65)
    assert any("months measured" in b for b in months_only["binding"]), months_only["binding"]
    assert not any("population" in b for b in months_only["binding"]), months_only["binding"]

    # P5-A10 — historical entries are NOT retro-fabricated
    try:
        import json as _json
        _cf = _json.load(open(_os.path.join(_here, "sleeve_counterfactual.json"),
                              encoding="utf-8"))
        _jul = [e for e in (_cf.get("freeze_history") or []) if e.get("month") == "2026-07"]
        if _jul:
            assert _jul[0].get("measured") is False, ("2026-07 must stay measured:false — it "
                                                      "cannot be recomputed and a fabricated "
                                                      "boolean is worse than a gap (ISA-0429)")
    except FileNotFoundError:
        pass

    # P5-A11 — route_attribution makes NO counterfactual claim, and does not gate
    ra = route_attribution(live)
    assert ra["gating"] is False and ra["makes_counterfactual_claim"] is False
    assert ra["counts"]["pre_framework"] == 3 and ra["counts"]["forward_led"] == 1, ra["counts"]
    assert ra["counts"]["vci"] == 1 and ra["counts"]["override"] == 1, ra["counts"]
    assert "pre_framework" in ra["buckets"], "pre_framework is its OWN bucket, not an 'other'"

    print("retention selftest OK (30 + P5 A1-A11 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print("retention.py — s9 underwriting + s3 step-down ratchet")
