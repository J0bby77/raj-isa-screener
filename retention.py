#!/usr/bin/env python3
# ISA-0695 note (17-Sep-2026): "R3.11" was proposed by the 27-Aug BuildSpec but never adopted into
# ISA_Engineering_Rules.md; the binding basis for noise-normalised thresholds is R13.1/R15.2.
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
# threshold expressed in the units of the quantity rather than of its noise (noise-normalisation, R15.2). The
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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0418 — THE PERSISTED ENTRY RECORD. `underwrite()` existed; nothing stored its output.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# `underwrite()` has enforced REQUIRED_ENTRY_FIELDS and immutability since it shipped, and no
# `er_entry` appeared in ANY artefact on disk — so `realised_fraction` was unreachable for
# every holding and `re_underwrite_outcome`'s TRIM_TO_RISK_TARGET branch, which the code
# itself calls "banks the excess without exiting a working thesis", had never been runnable.
# The missing half was a STORE and a CALLER, not the design.
#
# ⚑⚑ EXISTING POSITIONS ARE NOT BACK-FILLED, AND THAT IS THE POINT. R7.5 forbids back-filling
#    a judgement that was never made. MU, AVGO, ABCL, ONT, COCO, QBTS and NTAP were opened
#    with no recorded expected return, and inventing one now would make `realised_fraction`
#    report a number derived from a figure nobody ever committed to — the H3 conviction
#    backfill was refused as fabrication for exactly this reason. They are recorded as
#    NOT_UNDERWRITTEN, which is the truth, and `realised_fraction` REFUSES for them.
#    Positions opened from here on are underwritten AT ENTRY, and the instrument becomes
#    real as the book turns over. A gap that closes honestly beats a number that is wrong now.
#
# ⚑ er_horizon_months is READ from scoring_config.ER_HORIZON_MONTHS (Raj D26/D6 = 12). No
#   caller passes its own: a 12-month and a 24-month reading of the same E[r] give different
#   realised_fraction answers for the same position, and the difference would be invisible.

UNDERWRITE_STORE = "position_underwriting.json"
NOT_UNDERWRITTEN = "NOT_UNDERWRITTEN"


def _underwrite_path(root=None):
    import os as _os
    return _os.path.join(root or _os.path.dirname(_os.path.abspath(__file__)),
                         UNDERWRITE_STORE)


def load_underwriting(root=None) -> dict:
    import json as _json
    import os as _os
    p = _underwrite_path(root)
    if not _os.path.exists(p):
        return {"schema_version": "1.0.0", "lots": {}}
    with open(p, encoding="utf-8") as fh:
        return _json.load(fh)


def save_underwriting(doc: dict, root=None) -> str:
    import json as _json
    import os as _os
    p = _underwrite_path(root)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(doc, fh, indent=1, ensure_ascii=False)
    _os.replace(tmp, p)
    return p


def _declared_horizon() -> int:
    try:
        import scoring_config as _sc
        return int(getattr(_sc, "ER_HORIZON_MONTHS"))
    except Exception as exc:                                         # noqa: BLE001
        raise RetentionRefused(
            "scoring_config.ER_HORIZON_MONTHS is not declared (%s). er_entry has no meaning "
            "without the period it is expressed over (R2.6, ISA-0675/ISA-0168)." % exc)


def record_entry(ticker: str, *, er_entry, er_confidence_entry, price_entry,
                 position_first_entry_date, fv_entry=None, underwrite_date=None,
                 root=None) -> dict:
    """Persist the IMMUTABLE entry record for a newly opened position."""
    # ⚑ UNITS. `er_entry` is a PERCENT, not a fraction — realised_fraction divides a
    #   price_return in PERCENT by it. Passing 0.19 for "19%" yields a realised_fraction of
    #   105x instead of 1.05x, which would trip the >= 1.0 MANDATORY RE-UNDERWRITE on a
    #   position that had barely moved. Caught in this build's own first test. A plausible
    #   wrong number is worse than a refusal (R4.1), so anything under 1.0 is REFUSED rather
    #   than silently rescaled — rescaling would guess which unit the caller meant.
    if er_entry is not None and 0 < float(er_entry) < 1.0:
        raise RetentionRefused(
            "%s: er_entry=%r looks like a FRACTION. It must be a PERCENT (19.0, not 0.19) — "
            "realised_fraction divides a percent price return by it, so a fraction inflates "
            "the result ~100x and would fire the mandatory re-underwrite on a flat position. "
            "Refused rather than rescaled: guessing the caller's unit is how the /100 "
            "inversion of ISA-0429 ran for five months." % (ticker, er_entry))
    doc = load_underwriting(root)
    tk = str(ticker).upper()
    lot = (doc.get("lots") or {}).get(tk) or {"ticker": tk}
    lot["position_first_entry_date"] = position_first_entry_date
    lot = underwrite(lot, er_entry=er_entry, er_confidence_entry=er_confidence_entry,
                     price_entry=price_entry, fv_entry=fv_entry,
                     er_horizon_months=_declared_horizon(),
                     underwrite_date=underwrite_date)
    doc.setdefault("lots", {})[tk] = lot
    save_underwriting(doc, root)
    return lot


def underwriting_state(held_tickers, root=None) -> dict:
    """Which held positions carry an entry record, and which do not. No back-fill (R7.5)."""
    doc = load_underwriting(root)
    lots = doc.get("lots") or {}
    have, missing = [], []
    for t in held_tickers:
        tk = str(t).upper()
        (have if (lots.get(tk) or {}).get("underwrite") else missing).append(tk)
    return {
        "underwritten": sorted(have), "not_underwritten": sorted(missing),
        "store": UNDERWRITE_STORE, "horizon_months": _declared_horizon(),
        "warnings": ([("Step 6.5 (ISA-0418): %d held position(s) carry NO entry underwriting "
                       "(%s), so realised_fraction and the re-underwrite outcome are "
                       "UNREACHABLE for them. ⚑ They are NOT back-filled: inventing an "
                       "er_entry nobody committed to would make the instrument report a "
                       "number derived from a fabricated figure (R7.5). Positions opened from "
                       "here are underwritten at entry."
                       % (len(missing), ", ".join(sorted(missing))))] if missing else []),
    }


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
# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0658 — REALISATION: mandate_years_banked and the realisation t-statistic
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Raj, 12-Sep-2026: "is the stock's return above the hurdle + transaction costs". Taken
# literally that compares a 64-day realised return to an ANNUAL rate — the same dimensional
# error as the "13.7% + 2% = 15.7%" hurdle, where a one-off round trip was added to a
# compound annual rate (a 2.00pp/yr drag implies a 9.1-month hold, shorter than the 182-day
# min-hold's own intent). Two quantities fix it, and neither is a threshold:
#
#   mandate_years_banked = ln(1+r) / ln(1+required_return)
#       "how many years of REQUIRED return has this position already delivered?"
#       ABCL: +53% in 64 days = 3.31 mandate-years. It has banked over three years of the
#       return the plan needs, in nine weeks.
#
#   realisation t = r / (sigma_ann * sqrt(days/365))
#       the same gain expressed in units of the position's OWN noise. R15.2 (noise-normalisation): before declaring
#       ANY threshold, divide it by the SD of the thing it tests and publish the t.
#
# ⚑ WHY THE t AND NOT A PERCENTAGE. ONT peaked at +24.0% and ABCL sits at +53%, on sigmas of
#   63.6% and 90.8% — a raw-percentage rule ranks them by how volatile they are, not by how
#   much has been achieved. The t is SCALE-FREE and measured to be so: simulated false-fire
#   rates are 20.9% / 21.3% / 21.4% at sigma 50% / 91% / 158%. That invariance is the whole
#   advance here.
#
# ⚑⚑ AND IT IS NOT A SIGNIFICANCE TEST. R3.4 forbids using one as an estimator. t >= k does
#   not mean the return is real; it means enough of this position's own noise-scale has been
#   banked to justify RE-UNDERWRITING it. The discrimination in the D5 rule comes from the
#   conjunction — catalyst RESOLVED and in profit and an UNPRICEABLE_BY_NATURE refusal — not
#   from k. Measured: at k = 1.0 monthly x2, a driftless position fires 21.3% per 182-day
#   window and one earning exactly the 13.7% hurdle fires 23.6%. The leg has almost no power
#   to separate those two states ALONE, and saying so is part of shipping it honestly.

REALISATION_K = 1.0                 # Raj, 12-Sep-2026. See REALISATION_K_RATIONALE.
REALISATION_OBS_CADENCE = "monthly"
REALISATION_CONSECUTIVE_REQUIRED = 2

REALISATION_K_RATIONALE = {
    "constant": "REALISATION_K", "home": "retention.py",
    "who_set_it": "Raj", "set_on": "2026-09-12", "evidence_basis": "DECLARED",
    "bounded_below_by": ("0.82 — ONT's peak realisation t. Below this the realisation leg "
                         "pre-empts the give-back leg and the two-route structure collapses "
                         "into one, which is the opposite of what Addendum 3 established. "
                         "On the framework's OWN weekly-close basis ONT's peak t is 0.77 "
                         "(+24.03% over 88 days at sigma 63.6%); 0.82 comes from an intraday "
                         "high the framework does not hold (ISA-0670). The CONSERVATIVE "
                         "(higher) bound governs."),
    "bounded_above_by": ("1.39 — ABCL's realisation t at the 12-Sep price. Above this the "
                         "rule does not fire on the only realisation episode the framework "
                         "has ever had and ships INERT, which is the never-fires half of "
                         "M1's failure pair. On the latest STORED price (04-Sep) it is 1.61; "
                         "the conservative (lower) bound governs."),
    "why_1_0": ("the interior point of (0.82, 1.39] that survives BOTH measurement choices "
                "on BOTH bounds — 0.18 sigma of headroom above ONT, 0.39 below ABCL. k = 1.28 "
                "also survives but sits 0.11 sigma under ABCL's conservative figure: one bad "
                "week and the rule is inert. k = 1.5 fails outright, and k = 2.0 (the "
                "significance-flavoured choice) is inert on arrival — R3.4."),
    "why_not_calibrated": ("REFUSED_FOR_POWER. resolved_count = 1 of 12 and "
                           "calibration_gate_passed = FALSE. A k fitted to one observation is "
                           "a number wearing a decimal point (R3.2, R3.5)."),
    "cadence_reason": ("evaluated MONTHLY and required at TWO CONSECUTIVE observations. "
                       "Checking weekly gives a 43.1% driftless false-fire rate per 182-day "
                       "window — a running-maximum artefact, not the 15.9% single-observation "
                       "tail the threshold suggests. Monthly x2 halves it to 21.3%. This "
                       "mirrors T4's existing sustained-observation doctrine (ISA-0165)."),
    "what_would_falsify_it": ("realisation exits that systematically underperform continued "
                              "holding over the following 12 months, or a measured fire rate "
                              "outside the declared 10-35% band"),
    "revalidate_by": "2027-03-31",
    "expected_fire_rate_band_pct": [10.0, 35.0],     # R15.2
}


def mandate_years_banked(realised_return: float, required_return: float = None) -> dict:
    """How many YEARS of required return this position has already delivered.

    Refuses rather than returning a number on a loss or a non-positive hurdle: ln of a
    non-positive quantity is not a small number, it is undefined, and R4.3 forbids a control
    returning PASS on an input it could not evaluate."""
    import math as _m
    if required_return is None:
        required_return = _required_return_operative()
    if required_return is None:
        return {"value": None, "state": "UNKNOWN",
                "why": "the required-return anchor could not be read from target_state.json; "
                       "R16.1 forbids guessing it"}
    if realised_return is None:
        return {"value": None, "state": "UNKNOWN", "why": "no realised return supplied"}
    if 1.0 + realised_return <= 0 or 1.0 + required_return <= 0:
        return {"value": None, "state": "UNDEFINED",
                "why": "a total loss has no expression in mandate-years"}
    if realised_return < 0:
        return {"value": round(_m.log(1.0 + realised_return) / _m.log(1.0 + required_return), 4),
                "state": "NEGATIVE",
                "why": "the position has given back mandate-years; the realisation leg cannot "
                       "fire on it (C5 requires profit)"}
    return {"value": round(_m.log(1.0 + realised_return) / _m.log(1.0 + required_return), 4),
            "state": "MEASURED", "required_return_used": required_return}


def realisation_t(realised_return: float, sigma_ann: float, days_held: int) -> dict:
    """The realised gain in units of the position's OWN annualised volatility.

    REFUSES on a missing sigma — a null sigma is not a zero, and dividing by a substituted
    one would manufacture an arbitrarily large t on the names with no price history, which
    are exactly the names least entitled to a harvest decision (V-1 / R4.3)."""
    import math as _m
    if sigma_ann is None or not sigma_ann or sigma_ann <= 0:
        return {"t": None, "state": "UNKNOWN",
                "why": "sigma_ann is absent or non-positive, so the gain cannot be expressed "
                       "in units of the position's own noise. A null sigma is not a zero "
                       "(V-1): substituting one would manufacture an unbounded t on precisely "
                       "the names with no measured price history."}
    if realised_return is None or not days_held or days_held <= 0:
        return {"t": None, "state": "UNKNOWN",
                "why": "realised return or holding period absent"}
    period_sigma = float(sigma_ann) * _m.sqrt(float(days_held) / 365.0)
    return {"t": round(float(realised_return) / period_sigma, 4), "state": "MEASURED",
            "period_sigma": round(period_sigma, 4), "sigma_ann": sigma_ann,
            "days_held": days_held}


# ── D31 (Raj, 12-Sep-2026) — the consecutive requirement depends on the leg's ROLE ──────
# The x2 requirement exists to stop the realisation leg firing on NOISE: measured false-fire
# rates per 182-day window are 43.1% weekly, 36.2% monthly x1, 21.3% monthly x2. That
# rationale holds when realisation is the REASON a position is sold.
#
# ⚑ IT DOES NOT HOLD WHEN REALISATION IS ONLY THE PERMISSION. Where the disposition is
#   already SELL because the forward case is UNPRICEABLE_BY_NATURE, the realisation leg is
#   not deciding anything — it is unlocking the 182-day min-hold. And the min-hold's own
#   purpose (C-1, which closed a realised -GBP 1,097 churn pattern) is to stop round-tripping
#   ON NOISE. A sale driven by "the binary resolved and the successor cannot be priced" is a
#   THESIS event, not a noise event, so applying a noise-control device to it is a category
#   error whose only effect is a further month of exposure. Measured on ABCL: waiting one
#   month at sigma 90.8% risks +/- GBP 462, which is 68% of the entire GBP 680 realised gain;
#   a 1-sigma fall retains 40% of it and a 2-sigma fall puts the position at a loss.
#
#   C1 (resolved) AND C5 (in profit) AND by-nature are already three independent gates. The
#   second observation adds a month of exposure and no information.
REALISATION_CONSECUTIVE_AS_REASON = 2
REALISATION_CONSECUTIVE_AS_PERMISSION = 1
REALISATION_ROLE_REASON = "REASON"
REALISATION_ROLE_PERMISSION = "PERMISSION"


def realisation_consecutive_required(role: str) -> int:
    """D31 — how many consecutive monthly observations this role needs.

    REFUSES an undeclared role rather than defaulting to the looser number: defaulting to
    PERMISSION would silently relax the noise control everywhere, which is the direction the
    error must never run (R4.3/R4.8)."""
    if role == REALISATION_ROLE_REASON:
        return REALISATION_CONSECUTIVE_AS_REASON
    if role == REALISATION_ROLE_PERMISSION:
        return REALISATION_CONSECUTIVE_AS_PERMISSION
    raise RetentionRefused(
        "realisation role %r is not declared. D31 requires the caller to state whether the "
        "realisation leg is the REASON for the sale (x%d, the noise control) or the "
        "PERMISSION for one already decided on UNPRICEABLE_BY_NATURE grounds (x%d). An "
        "undeclared role must not default to the looser number."
        % (role, REALISATION_CONSECUTIVE_AS_REASON, REALISATION_CONSECUTIVE_AS_PERMISSION))


def realisation_trigger(history, k: float = None, consecutive: int = None,
                        role: str = None) -> dict:
    """Has the realisation t held at or above k for CONSECUTIVE monthly observations?

    `history`: monthly observations oldest-first, each {as_of, t}. The consecutive
    requirement is the control, not k — see REALISATION_K_RATIONALE['cadence_reason'] — and
    under D31 it depends on `role`: REASON (x2) or PERMISSION (x1).

    ⚑ An observation whose t is None BREAKS the run rather than being skipped. Skipping it
    would let two readings a year apart count as consecutive, and an unmeasured month is not
    a month that passed (R4.3)."""
    k = REALISATION_K if k is None else k
    if consecutive is None and role is not None:
        consecutive = realisation_consecutive_required(role)
    need = REALISATION_CONSECUTIVE_REQUIRED if consecutive is None else consecutive
    obs = list(history or [])
    run, best = 0, 0
    for o in obs:
        t = o.get("t")
        run = run + 1 if (t is not None and t >= k) else 0
        best = max(best, run)
    fired = run >= need
    n_unmeasured = sum(1 for o in obs if o.get("t") is None)
    return {
        "fired": fired, "k": k, "consecutive_required": need,
        "current_run": run, "longest_run": best,
        "n_observations": len(obs), "n_unmeasured": n_unmeasured,
        "cadence": REALISATION_OBS_CADENCE,
        "latest_t": (obs[-1].get("t") if obs else None),
        "why": ("realisation t >= %.2f at %d consecutive %s observations"
                % (k, run, REALISATION_OBS_CADENCE) if fired else
                "realisation t has held above %.2f for %d of the %d consecutive %s "
                "observations required%s"
                % (k, run, need, REALISATION_OBS_CADENCE,
                   ("; %d observation(s) were UNMEASURED and broke the run rather than being "
                    "skipped" % n_unmeasured) if n_unmeasured else "")),
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0651 — THE GRADUATION DISPOSITION. "HOLD" IS NOT AN AVAILABLE ANSWER.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Raj, 12-Sep-2026: *"the framework should either be saying keep because we believe there is
# still a lot of upside in this stock or sell because the original binary has been realised
# and the framework is unable to calculate its future upside. It should not simply say hold
# at this juncture as I do not want its returns eroded before I get to capitalise the
# position."*
#
# ⚑ THE TWO QUESTIONS WERE CONFLATED, AND THAT IS WHY "HOLD" KEPT WINNING.
#   (a) DISPOSITION — keep or sell? Decided by whether the FORWARD case can be priced and
#       clears the hurdle. There are exactly two answers.
#   (b) EXECUTABILITY — may the sale happen inside the 182-day min-hold? Decided by D5:
#       C1 (catalyst resolved) AND C5 (in profit) AND (REALISATION OR GIVEBACK) AND the
#       refusal being UNPRICEABLE_BY_NATURE rather than UNMEASURED_BY_DEFECT.
#
#   Answering (a) with (b)'s machinery produces "hold": the exemption is unavailable, so
#   nothing happens, and nothing-happening gets published as a decision. A position whose
#   disposition is SELL but whose exemption has not yet matured is **SELL_PENDING_MIN_HOLD**
#   — a decided sale awaiting permission, with a named date — not a hold.
#
# ⚑ AND HOLDING AN UNPRICEABLE POSITION IS AN ACTIVE BET, NOT INACTION. The realised gain is
#   certain; the forward return is unknown. Requiring evidence to SELL while requiring none to
#   KEEP puts the burden on the wrong side. `evidence_state`'s DEGRADED_UNMEASURED ->
#   HOLD_AT_CURRENT encodes exactly that error, which is why this function refuses to emit it.

KEEP_AND_TOP_UP = "KEEP_AND_TOP_UP"
KEEP_AT_SIZE = "KEEP_AT_SIZE"      # only when the rung is already reached
SELL = "SELL"
SELL_PENDING_MIN_HOLD = "SELL_PENDING_MIN_HOLD"
BLOCKED_AT_A_LOSS = "BLOCKED_AT_A_LOSS"
DEFERRED_ON_NAMED_DEFECT = "DEFERRED_ON_NAMED_DEFECT"
NOT_IN_SCOPE = "NOT_IN_SCOPE"

UNPRICEABLE_BY_NATURE = "UNPRICEABLE_BY_NATURE"
UNMEASURED_BY_DEFECT = "UNMEASURED_BY_DEFECT"


def graduation_disposition(*, ticker, catalyst_status, forward_case, in_profit,
                           realisation=None, giveback=None, size_gbp=None,
                           min_entry_gbp=None, risk_ceiling_gbp=None, rung_gbp=None,
                           min_hold_until=None, today=None) -> dict:
    """KEEP or SELL for a position whose binary has resolved. Never HOLD.

    `forward_case`: {"priceable": bool, "refusal_kind": ..., "er_pct": float|None,
                     "hurdle_pct": float|None}
    `realisation` / `giveback`: the trigger dicts, each carrying `fired`.

    Returns a disposition plus the D5 exemption verdict, kept separate so a decided sale that
    cannot yet execute is visible AS a decided sale."""
    _fi_mark("retention", "graduation_disposition")   # ISA-0699: execution-ledger observation
    import datetime as _dt
    today = today or _dt.date.today().isoformat()
    out = {"ticker": ticker, "as_of": today, "catalyst_status": catalyst_status}

    # ⚑ ISA-0655's successor state is IN SCOPE and is the whole point. A name whose first
    #   binary resolved and whose successor cannot be priced is EXACTLY the population this
    #   rule exists for — it was omitted here when the state was added, and running the
    #   orchestrated review caught it returning NOT_IN_SCOPE for ABCL, the case it was built
    #   to decide. A new enum value that silently falls out of a downstream membership test is
    #   the R4.7 shape: a contract change must fail an un-updated caller, not skip it.
    RESOLVED = ("RESOLVED_POSITIVE", "RESOLVED_NEGATIVE",
                "RESOLVED_POSITIVE_SUCCESSOR_PENDING")
    if catalyst_status not in RESOLVED:
        out.update(state=NOT_IN_SCOPE,
                   why=("the binary has not resolved; this rule governs post-resolution names "
                        "only. In scope: %s" % ", ".join(RESOLVED)))
        return out

    fc = forward_case or {}
    priceable = bool(fc.get("priceable"))
    er, hurdle = fc.get("er_pct"), fc.get("hurdle_pct")

    # ── (a) DISPOSITION ────────────────────────────────────────────────────────────────
    if priceable and er is not None and hurdle is not None:
        if er >= hurdle:
            # KEEP. D16 then decides at what SIZE, and a risk ceiling below the capital
            # floor turns KEEP into SELL — there is no size worth owning (D27/ISA-0652).
            # ⚑ Raj, 12-Sep-2026: *"if it mechanically decides to keep I should have said
            #   keep AND TOP UP."* So KEEP is never a licence to leave a position where it
            #   happens to be. The target is the LADDER RUNG that evidence_state earns and
            #   thesis_state caps — not MIN_ENTRY, which is only the floor below which a
            #   holding may not exist at all. Filling to the floor would satisfy D16 and still
            #   leave capital idle in a position the framework has just said it believes in,
            #   which is R16.2: every pound must count.
            target = min([x for x in (rung_gbp, risk_ceiling_gbp) if x is not None]
                         or [min_entry_gbp])
            if (risk_ceiling_gbp is not None and min_entry_gbp is not None
                    and risk_ceiling_gbp < min_entry_gbp):
                out.update(disposition=SELL,
                           why=("forward case clears the hurdle (E[r] %.2f%% vs %.2f%%) but no "
                                "size of this name is both material and risk-acceptable: the "
                                "risk ceiling GBP %.0f is below MIN_ENTRY GBP %.0f (D27). KEEP "
                                "is unavailable because there is nothing to keep it AT."
                                % (er, hurdle, risk_ceiling_gbp, min_entry_gbp)))
            elif target is not None and size_gbp is not None and size_gbp < target:
                capped = (risk_ceiling_gbp is not None and rung_gbp is not None
                          and risk_ceiling_gbp < rung_gbp)
                out.update(disposition=KEEP_AND_TOP_UP,
                           top_up_to_gbp=round(target, 2),
                           top_up_amount_gbp=round(target - size_gbp, 2),
                           target_basis=("risk ceiling (below the earned ladder rung)" if capped
                                         else "earned ladder rung"),
                           why=("forward case clears the hurdle (E[r] %.2f%% vs %.2f%%). KEEP "
                                "means TOP UP to GBP %.0f (%s) — an addition of GBP %.0f. A "
                                "position the framework has just backed is not left at GBP %.0f "
                                "(R16.2)." % (er, hurdle, target,
                                              "risk ceiling, below the earned rung" if capped
                                              else "earned ladder rung",
                                              target - size_gbp, size_gbp)))
            else:
                out.update(disposition=KEEP_AT_SIZE,
                           why=("forward case clears the hurdle (E[r] %.2f%% vs %.2f%%) and the "
                                "position is already at or above its earned rung, so there is "
                                "nothing to top up." % (er, hurdle)))
            out["state"] = out["disposition"]
            return out
        out.update(disposition=SELL,
                   why=("forward case is PRICEABLE and does NOT clear the hurdle "
                        "(E[r] %.2f%% vs %.2f%%)." % (er, hurdle)))
    else:
        kind = fc.get("refusal_kind")
        if kind == UNMEASURED_BY_DEFECT:
            # ⚑ A named open defect must NOT become a standing licence to do nothing. The
            #   position is deferred WITH the item id and an expiry; C3's by-nature/by-defect
            #   split exists so a data outage is not a liquidation signal (C-1 inverted).
            out.update(state=DEFERRED_ON_NAMED_DEFECT, disposition=None,
                       blocking_items=fc.get("blocking_items") or [],
                       why=("the forward case is UNMEASURED_BY_DEFECT (%s), so neither KEEP nor "
                            "SELL is evidenced. This is NOT a hold: it is a deferral with a "
                            "named cause, and the cause is escalated rather than tolerated."
                            % ", ".join(fc.get("blocking_items") or ["unnamed"])))
            return out
        out.update(disposition=SELL,
                   why=("the binary has resolved and the forward case is %s. KEEP would be an "
                        "active bet that an unknown forward return exceeds the hurdle, placed "
                        "with no evidence; the realised gain is certain and the forward return "
                        "is not." % (kind or "UNPRICEABLE")))

    # ── (b) EXECUTABILITY — D5's min-hold exemption, kept separate ─────────────────────
    if not in_profit:
        out.update(state=BLOCKED_AT_A_LOSS,
                   why=out["why"] + " ⚑ C-1 blocks a full exit at a loss inside the min-hold "
                                    "window; a trim is not available below MIN_ENTRY either.",
                   min_hold_until=min_hold_until)
        return out
    # D31 — inside graduation_disposition the realisation leg is by construction PERMISSION:
    # the disposition above was already decided by the forward case, not by the t.
    r_fired = bool((realisation or {}).get("fired"))
    r_role = (realisation or {}).get("role")
    g_fired = bool((giveback or {}).get("fired"))
    by_nature = (fc.get("refusal_kind") == UNPRICEABLE_BY_NATURE) or bool(priceable)
    inside = bool(min_hold_until and today < min_hold_until)
    exemption = {
        "available": bool(r_fired or g_fired) and by_nature,
        "C1_catalyst_resolved": True,
        "C5_in_profit": True,
        "realisation_fired": r_fired,
        "realisation_role": r_role or REALISATION_ROLE_PERMISSION,
        "realisation_consecutive_required": realisation_consecutive_required(
            r_role or REALISATION_ROLE_PERMISSION),
        "giveback_fired": g_fired,
        "refusal_by_nature": by_nature,
        "inside_min_hold": inside,
        "min_hold_until": min_hold_until,
    }
    out["min_hold_exemption"] = exemption
    if not inside or exemption["available"]:
        out["state"] = SELL
    else:
        out["state"] = SELL_PENDING_MIN_HOLD
        out["why"] += (" ⚑ The DISPOSITION is SELL. Execution is pending the `thesis_realised` "
                       "min-hold exemption, which needs the realisation or give-back leg: "
                       "realisation fired=%s, give-back fired=%s, min-hold to %s. This is a "
                       "decided sale awaiting permission, NOT a hold."
                       % (r_fired, g_fired, min_hold_until))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0620 — GIVE-BACK. THE MEASUREMENT SHIPS; THE THRESHOLD REFUSES UNTIL DECLARED.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Addendum 3 called this "the single highest-value build in this thread" because nothing in
# the framework could say "sell at +20%": ONT ran to +24.03% and returned most of it, and not
# one trigger was within reach at any point — 1.15x entry against a 2x milestone schedule,
# 57% below modelled FV against a 15-20%-of-FV trigger, fv_asymmetry 2.33x against a 2.00
# floor and therefore reading as HEALTHY.
#
# ⚑⚑ BUT NOISE-NORMALISATION (R15.2) CHANGES THE CONCLUSION, AND IT IS WORTH STATING PLAINLY. Divide the threshold
#    by the SD of the thing it tests before declaring it. Measured on the framework's own
#    weekly Friday closes (the only series it holds — an intraday high is not admissible,
#    ISA-0670):
#
#      ONT.L  entry 141.977p  peak 176.100p on 28-Aug-2026  = +24.03%
#             sigma_ann 63.6%  ->  monthly sigma 18.3%
#             ⚑ THE ENTIRE PEAK GAIN WAS 1.31 MONTHLY SIGMA.
#             give-back t, measured since the peak: 0.948 at 7 days, 0.877 at 15 days.
#
#      ABCL   entry $7.094  peak $11.430 on 04-Sep-2026  = +61.12%
#             sigma_ann 90.8%  ->  monthly sigma 26.2%   peak gain = 2.33 monthly sigma
#
#    So a FIXED give-back percentage is not one rule. A give-back of 50% of the peak gain is
#    **0.65 sigma for ONT and 1.17 sigma for ABCL** — the same declared number, nearly twice
#    the move required of one name as the other. That is exactly the error noise-normalisation (R15.2) exists to
#    prevent, and the same class as the 15-20%-of-FV trigger that never fired.
#
#    And ONT's give-back t never exceeds ~0.95. **Both of ONT's legs are sub-1-sigma**: its
#    realisation t peaked at 0.77 and its give-back t at 0.95. On the framework's own numbers
#    ONT is NOT cleanly a give-back case either — its drawdown is not an unusual move for a
#    63.6%-sigma stock. What actually went wrong is upstream: a 63.6%-sigma position was held
#    on a thesis whose peak gain was 1.3 monthly sigma. **There was never a gain that noise
#    would not take.** That is the risk-correct-sizing architecture (D27 / ISA-0419), not a
#    harvest trigger — and D27 already says ONT.L at GBP 1,549.68 is below the capital floor
#    and must be filled or exited.
#
# ⚑ THEREFORE: this block ships the MEASUREMENT, which is unambiguously worth having, and
#   REFUSES to declare a threshold. GIVEBACK_K is None and `giveback_trigger` returns
#   state UNDECLARED rather than a verdict. A control fed an undeclared threshold must not
#   return PASS or FAIL (R4.3), and inventing a number here would be the third home for a
#   guess (R12.3: NO_RECORDED_RATIONALE auto-raises an item).

PEAK_BASIS = "weekly_friday_close"      # ISA-0670. An intraday high is NOT admissible.
# ── Raj D32 (12-Sep-2026), on ISA-0678 ──────────────────────────────────────────────────
# The GATE is the decision that matters, and Raj took it: a give-back is only harvestable
# where there WAS a securely banked gain. Measured, the peak gain in units of the position's
# own monthly sigma: ONT.L 1.31, ABCL 2.33. At a minimum of 2.0 the trigger returns
# GAIN_NOT_WORTH_PROTECTING on ONT — correctly reclassifying it as a POSITION-SIZE problem
# (D27 / ISA-0419) rather than a harvest one — and admits ABCL.
#
# ⚑ WITH THE GATE AT 2.0, k NO LONGER HAS TO REACH DOWN TO ONT. That is the whole point of
#   accepting the gate: ONT's give-back t maxes at 0.948, so any k capable of catching it
#   would have been BELOW 1 sigma and would have fired inside the noise on every name. k is
#   therefore set at 1.0, consistent with REALISATION_K, and the two legs of
#   (REALISATION or GIVEBACK) now share one noise scale instead of two.
#
# ⚑ RAJ ASKED FOR THIS TO BE MONITORED CLOSELY, WITH A FORMAL REVIEW. Both constants carry
#   `revisit_by` and an R15.2 fire-rate band; `giveback_trigger` publishes the measurement
#   on every evaluation whether it fires or not, so the review has a series to read rather
#   than a recollection. The gate is the FIRST thing to re-examine: it is declared on two
#   observations (n=2), which is not a calibration, and it is capable of silencing the leg
#   entirely if real harvest cases cluster below 2.0 monthly sigma.
GIVEBACK_K = 1.0
GIVEBACK_MIN_PEAK_GAIN_MONTHLY_SIGMA = 2.0

GIVEBACK_RATIONALE = {
    "constant": "GIVEBACK_MIN_PEAK_GAIN_MONTHLY_SIGMA", "home": "retention.py",
    "who_set_it": "Raj", "set_on": "2026-09-12", "evidence_basis": "DECLARED",
    "measured_basis": ("peak gain / (sigma_ann / sqrt(12)) on the framework's own weekly "
                       "Friday closes: ONT.L +24.03% on sigma 63.6% = 1.31; ABCL +61.12% on "
                       "sigma 90.8% = 2.33. n = 2 — this is a DECLARED judgement, not a "
                       "calibration (R3.2, R13.1)."),
    "what_would_falsify_it": ("real harvest cases clustering BELOW 2.0 monthly sigma of peak "
                              "gain, which would mean the gate silences the give-back leg "
                              "rather than focusing it; or ONT-shaped positions recurring "
                              "after D27's floor/ceiling refusal is live, which would mean the "
                              "sizing architecture is not actually catching them"),
    "revisit_by": "2027-03-31",
    "review_trigger": ("Raj, 12-Sep-2026: 'we should monitor this closely and perhaps schedule "
                       "a formal review at some point when relevant.' Relevant = the earlier "
                       "of 2027-03-31, the third resolved VCI outcome, or the first give-back "
                       "episode that the gate BLOCKS — the last of which is the one worth "
                       "reading, because a blocked episode is the gate's own falsification "
                       "test presenting itself."),
    "expected_fire_rate_band_pct": [5.0, 30.0],     # R15.2
    "k": {"constant": "GIVEBACK_K", "value": 1.0,
          "why": ("consistent with REALISATION_K so both legs of (REALISATION or GIVEBACK) "
                  "share one noise scale. Freed to sit at 1.0 by the gate above: without it, "
                  "any k able to catch ONT (give-back t max 0.948) would have been sub-1-sigma."),
          "revisit_by": "2027-03-31"},
}


def peak_since_entry(observations, entry_date, *, basis: str = PEAK_BASIS) -> dict:
    """The highest observed level since entry, with its date and its declared BASIS.

    `observations`: {date: level} or {date: {level: ...}} — the weekly store's own shape.
    ⚑ The basis is carried on the RESULT, not assumed by the reader (R4.2/R2.6): a give-back
    measured against a high the framework never observed, and could never have dealt at,
    overstates the give-back, and this figure is about to gate capital."""
    rows = []
    for k in sorted(observations or {}):
        if k < entry_date:
            continue
        v = observations[k]
        rows.append((k, float(v["level"] if isinstance(v, dict) else v)))
    if not rows:
        return {"peak": None, "peak_date": None, "n_observations": 0, "basis": basis,
                "state": "UNKNOWN",
                "why": "no observations at or after the entry date; a peak cannot be "
                       "manufactured from an empty series (R4.3)"}
    d, p = max(rows, key=lambda x: x[1])
    return {"peak": p, "peak_date": d, "n_observations": len(rows), "basis": basis,
            "state": "MEASURED",
            "latest": rows[-1][1], "latest_date": rows[-1][0]}


def giveback(peak: float, current: float, entry: float, *, sigma_ann: float = None,
             days_since_peak: int = None) -> dict:
    """How much of the peak gain has been returned — as a fraction AND in sigma units.

    BOTH are published. The fraction is what a person reads; the t is what makes the
    threshold honest, and R6.2 says a disagreement between two readings is published rather
    than blended. Here they do not disagree so much as measure different things, and the
    second one is the reason the first cannot be thresholded directly."""
    import math as _m
    out = {"basis": PEAK_BASIS}
    if None in (peak, current, entry) or peak <= entry:
        out.update(state="UNKNOWN", giveback_pct=None, t=None,
                   why=("give-back is undefined without a peak ABOVE the entry: there is no "
                        "gain to give back. This is not a give-back of zero (R4.3)."))
        return out
    frac = (peak - current) / (peak - entry)
    drawdown = (peak - current) / peak
    peak_gain = peak / entry - 1.0
    out.update(state="MEASURED", giveback_pct=round(frac * 100.0, 2),
               drawdown_from_peak_pct=round(drawdown * 100.0, 2),
               peak_gain_pct=round(peak_gain * 100.0, 2))
    if not sigma_ann or sigma_ann <= 0:
        out.update(t=None, peak_gain_monthly_sigma=None,
                   why=("sigma_ann absent, so the give-back cannot be expressed in units of "
                        "the position's own noise. R15.2/R13.1 bind (noise-normalised threshold): a give-back percentage "
                        "declared without its t is 0.65 sigma on one name and 1.17 on another."))
        return out
    monthly_sigma = sigma_ann / _m.sqrt(12.0)
    out["peak_gain_monthly_sigma"] = round(peak_gain / monthly_sigma, 3)
    if days_since_peak and days_since_peak > 0:
        out["t"] = round(drawdown / (sigma_ann * _m.sqrt(days_since_peak / 365.0)), 4)
        out["days_since_peak"] = days_since_peak
    else:
        out.update(t=None, why="days_since_peak absent")
    out["giveback_in_monthly_sigma"] = round(frac * peak_gain / monthly_sigma, 3)
    return out


def giveback_trigger(gb: dict, k: float = None,
                     min_peak_gain_monthly_sigma: float = None) -> dict:
    """REFUSES until GIVEBACK_K is declared. It never guesses a threshold.

    The recommended shape, recorded on ISA-0678 for Raj: fire on the sigma-normalised t
    AND only where the peak gain was worth protecting in the first place
    (`peak_gain_monthly_sigma` >= a declared minimum). ONT's peak gain was 1.31 monthly
    sigma; a minimum of 2.0 would correctly say ONT was never a harvest case but a sizing
    one, and route it to D27's floor/ceiling refusal instead of to a harvest it cannot pass."""
    k = GIVEBACK_K if k is None else k
    floor_ = (GIVEBACK_MIN_PEAK_GAIN_MONTHLY_SIGMA if min_peak_gain_monthly_sigma is None
              else min_peak_gain_monthly_sigma)
    if k is None:
        return {"fired": False, "state": "UNDECLARED", "k": None,
                "measurement": gb,
                "why": ("GIVEBACK_K is not declared. R13.1/R15.2 require the threshold to be "
                        "divided by the SD of what it tests before it is declared at all, and "
                        "the measurement shows a fixed give-back percentage is 0.65 sigma on "
                        "ONT and 1.17 sigma on ABCL — it is not one rule. UNDECLARED returns "
                        "neither a fire nor a pass (R4.3); it blocks and says so. See "
                        "ISA-0678.")}
    if gb.get("state") != "MEASURED" or gb.get("t") is None:
        return {"fired": False, "state": "UNKNOWN", "k": k, "measurement": gb,
                "why": "the give-back could not be measured; an unmeasured leg does not fire "
                       "and does not pass"}
    if floor_ is not None and (gb.get("peak_gain_monthly_sigma") or 0) < floor_:
        return {"fired": False, "state": "GAIN_NOT_WORTH_PROTECTING", "k": k,
                "measurement": gb,
                "why": ("the peak gain was %.2f monthly sigma against a declared minimum of "
                        "%.2f — there was no securely banked gain to harvest. This is a "
                        "POSITION-SIZE finding (D27 / ISA-0419), not a harvest one, and "
                        "routing it to a harvest trigger would fire inside the noise."
                        % (gb.get("peak_gain_monthly_sigma") or 0, floor_))}
    return {"fired": bool(gb["t"] >= k), "state": "MEASURED", "k": k, "measurement": gb,
            "why": "give-back t %.3f vs k %.3f" % (gb["t"], k)}


def _required_return_operative(root: str = None) -> float:
    """The A19 anchor, READ not re-derived (R16.1 forbids a local re-derivation)."""
    import json as _json
    import os as _os
    root = root or _os.path.dirname(_os.path.abspath(__file__))
    try:
        with open(_os.path.join(root, "target_state.json"), encoding="utf-8") as fh:
            ts = _json.load(fh)
    except Exception:                                                # noqa: BLE001
        return None
    v = ts.get("required_return_operative_pct")
    return None if v is None else float(v) / 100.0


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
    that LOOKS discriminating (noise-normalisation, R15.2). Three weak, differently-wrong legs in conjunction is
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
