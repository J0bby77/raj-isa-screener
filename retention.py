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

RATCHET_FLOOR_PCT = 10.0
RATCHET_STEP_PCT = 5.0
PROBATION_TRAIL_PP = 5.0
PROBATION_MIN_POSITIONS = 3
PROBATION_MONTHS = 12

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


def ratchet_eligible(decisions: List[dict], *, today=None) -> dict:
    """Is the s3 population large enough for the rule to fire at all?

    ⚑ Only FORWARD-LED framework decisions count."""
    fl = [d for d in decisions if d.get("route") == "forward_led"]
    excluded = [{"ticker": d.get("ticker"), "route": d.get("route"),
                 "reason": d.get("exclusion_reason") or f"route={d.get('route')}, not forward_led"}
                for d in decisions if d.get("route") != "forward_led"]
    ok = len(fl) >= PROBATION_MIN_POSITIONS
    return {"eligible": ok, "n_forward_led": len(fl),
            "forward_led": [d.get("ticker") for d in fl],
            "min_required": PROBATION_MIN_POSITIONS, "excluded": excluded,
            "detail": (f"{len(fl)} forward-led decision(s) against {PROBATION_MIN_POSITIONS} "
                       f"required. THE RULE CANNOT FIRE and that is CORRECT, not a loophole: "
                       f"measuring the framework on a book it did not assemble is measuring the "
                       f"wrong thing, in either direction."
                       if not ok else
                       f"{len(fl)} forward-led decisions — the population is real and the rule "
                       f"may be evaluated")}


def evaluate_ratchet(*, decisions, sleeve_vs_vuag_pp, months_measured,
                     current_sleeve_weight_pct) -> dict:
    """The full s3 gate. Every refusal names WHICH condition failed."""
    el = ratchet_eligible(decisions)
    reasons = []
    if not el["eligible"]:
        reasons.append(f"population {el['n_forward_led']} < {PROBATION_MIN_POSITIONS}")
    if months_measured is None or months_measured < PROBATION_MONTHS:
        reasons.append(f"only {months_measured} of {PROBATION_MONTHS} months measured")
    if sleeve_vs_vuag_pp is None:
        reasons.append("sleeve-vs-VUAG is UNMEASURED — an unmeasured month reads "
                       "ACTIVE-UNMEASURED, never as underperformance (ISA-0429)")
    elif sleeve_vs_vuag_pp > -PROBATION_TRAIL_PP:
        reasons.append(f"trails by {-sleeve_vs_vuag_pp:.2f}pp, inside the "
                       f"{PROBATION_TRAIL_PP}pp threshold")
    if reasons:
        return {"fires": False, "reasons": reasons, "eligibility": el,
                "new_ceiling_pct": None,
                "detail": "step-down does NOT fire: " + "; ".join(reasons)}
    sd = step_down(current_sleeve_weight_pct)
    return {"fires": True, "reasons": [], "eligibility": el, **sd,
            "detail": (f"trails VUAG by {-sleeve_vs_vuag_pp:.2f}pp over {months_measured} "
                       f"months across {el['n_forward_led']} forward-led positions -> ceiling "
                       f"steps {sd['current_pct']:.2f}% -> {sd['new_ceiling_pct']:.0f}%")}


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

    # the LIVE population: only COCO is forward-led -> cannot fire
    live = [{"ticker": "AVGO", "route": "pre_framework"}, {"ticker": "MU", "route": "pre_framework"},
            {"ticker": "ONT.L", "route": "pre_framework"}, {"ticker": "ABCL", "route": "a13_override"},
            {"ticker": "QBTS", "route": "vci_path_b"}, {"ticker": "COCO", "route": "forward_led"}]
    el = ratchet_eligible(live)
    assert el["eligible"] is False and el["n_forward_led"] == 1, el
    assert "CORRECT, not a loophole" in el["detail"]
    assert len(el["excluded"]) == 5

    ev = evaluate_ratchet(decisions=live, sleeve_vs_vuag_pp=-22.5, months_measured=12,
                          current_sleeve_weight_pct=10.40)
    assert ev["fires"] is False, "n=1 must block even at -22.5pp"
    assert any("population" in r for r in ev["reasons"]), ev["reasons"]

    # an UNMEASURED month must not read as underperformance (ISA-0429)
    many = live + [{"ticker": f"F{i}", "route": "forward_led"} for i in range(3)]
    un = evaluate_ratchet(decisions=many, sleeve_vs_vuag_pp=None, months_measured=12,
                          current_sleeve_weight_pct=16.65)
    assert un["fires"] is False and any("UNMEASURED" in r for r in un["reasons"]), un

    fires = evaluate_ratchet(decisions=many, sleeve_vs_vuag_pp=-22.5, months_measured=12,
                             current_sleeve_weight_pct=16.65)
    assert fires["fires"] is True and fires["new_ceiling_pct"] == 15.0, fires
    assert fires["routing"] == "capital_destination"
    print("retention selftest OK (30 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print("retention.py — s9 underwriting + s3 step-down ratchet")
