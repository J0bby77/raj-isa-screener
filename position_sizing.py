#!/usr/bin/env python3
"""
position_sizing.py — V2.1-C (ISA-0356). THE single home for how big a direct-stock position is.
Authority: ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md s1, s2, s4, s5. Raj decisions D3, D5, D12-D14.

═══════════════════════════════════════════════════════════════════════════════════════════
THE FRAME (Raj, 22-Aug-2026): position size x name count = sleeve ceiling, and he declared all
three. This module owns the first and DERIVES the third; the second falls out of the arithmetic.
═══════════════════════════════════════════════════════════════════════════════════════════

s1  THE LADDER IS FIXED.  STARTER 3.5 · NORMAL 4.5 · HIGH 5.5 · EARNED_MAX 6.5 (% of NAV).
    Not derived from any rung. The A1 diversification multiplier (x d) is WITHDRAWN.
    ⚑ "A position reaches 3.5% or it does not exist." The 0.75% Stage-1 probe was superseded
    by this on 26-Aug-2026 (Raj, D5) — the two were two homes for one rule.

s2  THERE IS NO PERCENTAGE CEILING. Sleeve size is an OUTCOME:

        stock_max = SUM over QUALIFYING candidates and top-ups of
                        ( ladder_target[state] x NAV - current_value )
                    capped ONLY by the capital on offer
        nothing qualifies -> stock_max = 0 -> capital_destination routes it to funds

    ⚑ CAPITAL IS THE GOVERNOR. At a GBP 156,321 post-subscription NAV a STARTER is GBP 5,471,
    so GBP 11,250 funds 2.06 positions. No artificial count cap is needed.

    ⚑⚑ THE TRADE THIS MAKES, STATED PLAINLY BECAUSE IT IS THE RISK OF THE WHOLE BUILD.
    With no percentage ceiling, QUALIFICATION IS THE ONLY THING BETWEEN CAPITAL AND A BAD
    POSITION. August's screen excluded 0 of 13 names on the E[r] hurdle. So the evidence state
    (s6) and the correlation gate (s7) are not refinements — they ARE the replacement control.
    `size()` therefore REFUSES to run when they are absent, rather than falling back to the old
    band. That refusal is the safety property; see `SizingRefused`.

s5  VCI SIZING IS DERIVED FROM TWO DECLARED NUMBERS AND ONLY TWO: budget B = 1.5% of ISA and
    concurrent live binaries N = 1.

        w_vci = min( B_available / ((1 - p_thesis) x L x correlation_rider),
                     ladder[state], hard-cap stack )

    The 0.75/1.0/1.5% binary map and the ACS->size platform table are DELETED. sigma-ratio
    sizing is WITHDRAWN — it under-sizes the entry and grants size only after the payoff.
    `is_binary` is STATEFUL: RESOLVED releases the budget commitment ON THE SAME RUN.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["fixed_ladder"] / ["demand_pull_stock_max"] /
["vci_budget_sizing"] = False.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import ast
import inspect
import os
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


HERE = os.path.dirname(os.path.abspath(__file__))

PENDING = "PENDING"
RESOLVED_POSITIVE = "RESOLVED_POSITIVE"
RESOLVED_NEGATIVE = "RESOLVED_NEGATIVE"

# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0655 — THE SUCCESSOR STATE. Resolution was modelled as TERMINAL and it is not.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Raj, 12-Sep-2026: *"if this is still considered a binary event based on expected phase 3
# results then does it not remain a VCI stock."* Yes — and the framework could not represent
# it. `binary_commitment` on a RESOLVED name returned "commitment RELEASED on this run; it now
# sizes as an ordinary platform holding", which is false in all three of its parts for a
# multi-stage thesis. A positive Phase 2 does not END a binary sequence, it PROMOTES it.
#
# ⚑ AND THE MISSING SUCCESSOR IS NOT AN ABSENCE OF RISK. Releasing the commitment because the
#   next event cannot be priced reads a null as a zero — the V-1 defect that flipped
#   DENY -> ADMIT on QBTS on 09-Aug-2026 — and it understates the budget, so the error runs
#   TOWARD the risk. The framework's own precedent for an unmeasurable input is A2.3's adverse
#   correlation default (rho = max(rho_bar, 0.70)): a refusal expressed as a number that
#   cannot flatter. This applies the same doctrine.
RESOLVED_POSITIVE_SUCCESSOR_PENDING = "RESOLVED_POSITIVE_SUCCESSOR_PENDING"
CATALYST_STATUSES = (PENDING, RESOLVED_POSITIVE, RESOLVED_NEGATIVE,
                     RESOLVED_POSITIVE_SUCCESSOR_PENDING)

# A successor whose (p, L) cannot be sourced is UNPRICED. It is neither released nor priced:
# it re-arms at the MOST ADVERSE declared prior for its asset_structure.
SUCCESSOR_UNPRICED = "UNPRICED"
SUCCESSOR_PRICED = "PRICED"
UNPRICEABLE_BY_NATURE = "UNPRICEABLE_BY_NATURE"
UNMEASURED_BY_DEFECT = "UNMEASURED_BY_DEFECT"


def adverse_prior(asset_structure: Optional[str]) -> dict:
    """The most adverse declared (p, L) for a structure class — the re-arm price of an
    UNPRICED successor.

    ⚑ MOST ADVERSE ACROSS THE DECLARED ROWS, not a new number. It reads
    `vci_base_rates.json` and takes min(p) over that structure's rows and its declared L, so
    the framework never invents a prior it has not sourced (R12.3: a constant with no recorded
    rationale auto-raises an item). If the structure itself is undeclared it REFUSES — an
    unpriceable successor on an unknown structure has no adverse case to fall back to either."""
    import json as _json
    path = os.path.join(HERE, "vci_base_rates.json")
    try:
        with open(path, encoding="utf-8") as fh:
            br = _json.load(fh)
    except Exception as exc:                                         # noqa: BLE001
        raise SizingRefused(
            "vci_base_rates.json is unreadable (%s), so an UNPRICED successor cannot even be "
            "re-armed adversely. A missing prior is not a zero (V-1)." % exc)
    if not asset_structure:
        raise SizingRefused(
            "an UNPRICED successor with no declared asset_structure cannot be re-armed: there "
            "is no structure class whose adverse prior would apply. Declare the structure or "
            "the position cannot be priced at all (R4.3).")
    rows = {k: v for k, v in (br.get("p_thesis") or {}).items()
            if k.split("/")[0] == asset_structure}
    if not rows:
        raise SizingRefused(
            "vci_base_rates.json declares no p_thesis row for asset_structure %r, so there is "
            "no adverse prior to re-arm at." % asset_structure)
    worst_key = min(rows, key=lambda k: float(rows[k]["p"]))
    p = float(rows[worst_key]["p"])
    L_rows = br.get("L_by_structure") or {}
    if asset_structure not in L_rows:
        raise SizingRefused(
            "vci_base_rates.json declares no L for asset_structure %r." % asset_structure)
    L = float(L_rows[asset_structure]["L"])
    return {"p_thesis": p, "L": L, "basis": worst_key,
             "why": ("most adverse DECLARED prior for %s: p = %.2f from %r, L = %.2f. This is "
                     "a refusal expressed as a number that cannot flatter — the same doctrine "
                     "as A2.3's adverse rho = 0.70 for an unmeasured correlation (ISA-0655)."
                     % (asset_structure, p, worst_key, L))}

# ISA-0647 — ONE HOME (R4.4). This tuple used to be DEFINED here with four entries while
# scoring_config defined its own with three, and the prerun published THIS one. Read, never
# redeclared: a second definition of a capital-gating constant is a defect on the day it is
# created, and the divergence is invisible until someone diffs two files.
try:
    from scoring_config import MIN_HOLD_EXEMPT
except Exception:                                                    # noqa: BLE001
    # R4.7 — an un-importable contract RAISES rather than silently keeping a local copy.
    raise ImportError(
        "position_sizing requires scoring_config.MIN_HOLD_EXEMPT — the single declared home "
        "for the min-hold exemption grounds (ISA-0647). A local fallback tuple here is what "
        "produced two homes with different contents in the first place.")


class SizingRefused(RuntimeError):
    """The sizing engine cannot price this position. NEVER downgraded to a default size —
    with the percentage ceiling removed, a fallback size IS the failure mode (s2)."""


# ────────────────────────────────────────────────────────────── policy access
def load_policy(path=None) -> dict:
    p = path or os.path.join(HERE, "target_weights.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def ladder(policy=None) -> Dict[str, float]:
    policy = policy if policy is not None else load_policy()
    lad = ((policy.get("stock_sleeve") or {}).get("sizing_ladder_pct")) or {}
    if not lad:
        raise SizingRefused(
            "stock_sleeve.sizing_ladder_pct is absent from target_weights.json. The ladder is "
            "the operative position-size policy (clean spec s1) and there is no fallback: a "
            "default ladder would be a second home for the one rule this module owns.")
    return {k: float(v) for k, v in lad.items()}


def hard_caps(policy=None) -> dict:
    policy = policy if policy is not None else load_policy()
    th = policy.get("thresholds") or {}
    cap = th.get("max_stock_position_pct")
    if cap is None:
        raise SizingRefused("thresholds.max_stock_position_pct is absent — the hard cap that "
                            "portfolio_analytics enforces has no declared value.")
    return {"max_stock_position_pct": float(cap) * 100.0}


def tier_caps(policy=None) -> dict:
    """The two-tier CONCENTRATION caps, from their one home in `target_weights.thresholds`."""
    policy = policy if policy is not None else load_policy()
    th = policy.get("thresholds") or {}
    out = {}
    for k in ("tier1_soft_cap_pct", "tier2_hard_cap_pct"):
        v = th.get(k)
        if v is None:
            raise SizingRefused(
                "thresholds.%s is absent. The two-tier concentration control is executed from "
                "Run_Context Step 8 prose, so an absent threshold does not fail loudly — it "
                "makes the reviewer invent a number. Refusing." % k)
        out[k] = float(v) * 100.0
    return out


def effective_soft_cap_pct(evidence_state: Optional[str] = None, *, earned_rung_pct=None,
                           policy=None) -> dict:
    """THE Tier 1 trigger for ONE position — ISA-0496, resolved by Raj 02-Sep-2026.

    ⚑⚑ THE CONTRADICTION THIS REPLACES. The entry ladder can size a position to EARNED_MAX
    6.5% of NAV. The Tier 1 concentration soft cap is 5.0%. Both are expressed as "% of total
    ISA" for a direct-stock position, so the top of the ladder sat ABOVE the cap and a
    top-rung position was in "soft cap breached" the moment it opened — with the Step 8
    remedy on a failed five-question review being a trim to 3.5%, i.e. from EARNED_MAX to
    STARTER, and the Tier 2 remedy a trim "back to 5%", BELOW its earned rung.

    ⚑ THEY ARE TWO DIFFERENT RULES AND BOTH SHOULD EXIST. The ladder answers *"how big may
    this position be BOUGHT, given the evidence it has earned"*. The two-tier framework
    answers *"how far may a position DRIFT on appreciation before someone re-underwrites it"*.
    Raising the soft cap flat to 6.5% would have satisfied the ladder and silently removed the
    drift control for every position below the top rung — a STARTER name could then triple
    without review. Lowering the ladder to 5.0% would delete a rung Raj earned the right to use.

    ⚑⚑ THE RESOLUTION IS THAT THE TRIGGER IS RELATIVE TO WHAT THE POSITION EARNED:

        effective Tier 1 trigger = max(tier1_soft_cap_pct, the position's own earned rung)

    An EARNED_MAX position is unconstrained to 6.5% and reviewed only if it drifts ABOVE that
    — which is genuine drift. A STARTER or NORMAL position is still reviewed at 5.0%, exactly
    as today. **Nothing stops an EARNED_MAX position** (Raj, 02-Sep-2026), and no lower rung
    loses its control.

    ⚑ Both remedies become rung-relative too: a failed five-question review trims to the
    position's EARNED RUNG, not to a literal 3.5%; a Tier 2 breach trims to its effective
    soft cap, not to a literal 5.0%. A remedy that trims below what the evidence earned is the
    same contradiction one layer down.

    Pass `evidence_state` (preferred — the rung is derived) or `earned_rung_pct` directly.
    """
    _fi_mark("position_sizing", "effective_soft_cap_pct")
    policy = policy if policy is not None else load_policy()
    tc = tier_caps(policy)
    soft = tc["tier1_soft_cap_pct"]
    if earned_rung_pct is None:
        if evidence_state is None:
            raise SizingRefused(
                "effective_soft_cap_pct needs either an evidence_state or an earned_rung_pct. "
                "Defaulting to the bare soft cap would reintroduce ISA-0496 for exactly the "
                "positions the fix is for — the ones that earned a rung above it.")
        t = target_pct(evidence_state, policy=policy)
        if t["rung"] == "HOLD_AT_CURRENT":
            # A frozen position earned no rung this run. The bare soft cap applies, and the
            # artefact SAYS so rather than implying a rung it does not have.
            earned = None
        else:
            earned = float(t["target_pct"])
    else:
        earned = float(earned_rung_pct)
    eff = soft if earned is None else max(soft, earned)
    return {
        "effective_soft_cap_pct": round(eff, 4),
        "tier1_soft_cap_pct": round(soft, 4),
        "tier2_hard_cap_pct": round(tc["tier2_hard_cap_pct"], 4),
        "earned_rung_pct": None if earned is None else round(earned, 4),
        "binds": ("earned_rung" if (earned is not None and earned > soft) else "tier1_soft_cap"),
        "trim_to_on_five_question_fail_pct": round(eff, 4),
        "trim_to_on_tier2_breach_pct": round(eff, 4),
        "basis": ("ISA-0496 (Raj, 02-Sep-2026): the Tier 1 trigger is "
                  "max(tier1_soft_cap_pct, the position's own earned ladder rung), and both "
                  "remedies trim to that same figure. The ladder governs how big a position "
                  "may be BOUGHT; the two-tier framework governs how far it may DRIFT on "
                  "appreciation before re-underwriting. Neither number is restated in prose."),
    }


def rung_for_state(evidence_state: str, policy=None) -> str:
    policy = policy if policy is not None else load_policy()
    m = ((policy.get("stock_sleeve") or {}).get("evidence_state_to_rung")) or {}
    if evidence_state not in m:
        raise SizingRefused(
            f"evidence state {evidence_state!r} has no declared rung. Declared: {sorted(m)}. "
            f"A state without a rung would silently size at whatever the caller passed.")
    return m[evidence_state]


# ────────────────────────────────────────────────────────────── the ladder
def target_pct(evidence_state: str, *, current_pct: float = 0.0, policy=None) -> dict:
    """The ladder target for ONE position, before the correlation cap and the hard-cap stack."""
    _fi_mark("position_sizing", "target_pct")
    policy = policy if policy is not None else load_policy()
    lad, caps = ladder(policy), hard_caps(policy)
    rung = rung_for_state(evidence_state, policy)
    if rung == "HOLD_AT_CURRENT":
        return {"rung": rung, "target_pct": round(float(current_pct), 4),
                "capped_by": None, "may_receive_new_capital": False,
                "basis": ("DEGRADED_UNMEASURED freezes at the current weight (D13). Not a "
                          "ladder rung — the position is held, not sized.")}
    if rung not in lad:
        raise SizingRefused(f"rung {rung!r} is not on the ladder {sorted(lad)}")
    t = lad[rung]
    capped_by = None
    if t > caps["max_stock_position_pct"]:
        t, capped_by = caps["max_stock_position_pct"], "max_stock_position_pct"
    return {"rung": rung, "target_pct": round(t, 4), "capped_by": capped_by,
            "may_receive_new_capital": evidence_state not in
            ("DEGRADED_UNMEASURED", "DEGRADED_REVERSED"),
            "basis": f"fixed ladder rung {rung} = {lad[rung]}% of NAV (clean spec s1)"}


def apply_correlation(target: dict, corr_rec: Optional[dict], policy=None) -> dict:
    """A2.3/A2.1. An UNMEASURED correlation caps the position at STARTER — it never scales it.

    ⚑ The clean spec withdrew the x d multiplier, so correlation no longer SIZES a position.
    It does two things only: it CAPS an unmeasured one at STARTER, and it RECLASSIFIES a
    duplicative one as REPLACEMENT_ONLY. Both are floors on risk, never grants of size."""
    _fi_mark("position_sizing", "apply_correlation")
    policy = policy if policy is not None else load_policy()
    lad = ladder(policy)
    out = dict(target)
    if corr_rec is None:
        raise SizingRefused(
            "no correlation record supplied. With the percentage ceiling removed (s2) the "
            "correlation gate is part of the replacement control, not an optional refinement — "
            "sizing without it is precisely the configuration the amendment schedule calls "
            "'the dangerous one'.")
    out["correlation_measured"] = bool(corr_rec.get("measured"))
    out["rho_sleeve"] = corr_rec.get("rho_sleeve")
    out["rho_basis"] = corr_rec.get("rho_basis")
    if not corr_rec.get("measured"):
        starter = lad.get("STARTER")
        if out["target_pct"] > starter:
            out["target_pct"], out["capped_by"] = starter, "UNMEASURED_CORRELATION_STARTER_CAP"
        out["basis"] += (f" | correlation UNMEASURED -> capped at STARTER {starter}% until 52 "
                         f"weeks of Friday-to-Friday GBP returns exist (A2.3). This is a "
                         f"MEASURED REFUSAL, not an estimate.")
    return out


# ────────────────────────────────────────────────────────────── VCI (s5)
def class_prior(asset_structure, catalyst_type, path=None):
    """ISA-0687 — select p_thesis and L from THE CATALYST'S OWN CLASS.

    The ISA-0653 refusal message already told the reader this is what a declared
    catalyst_type buys: "p_thesis and L cannot be selected from the catalyst's own
    class and would fall back to `platform` structure defaults". Nothing implemented
    it, so a declared type bought nothing and the position stayed UNPRICEABLE.

    REFUSES rather than defaulting, in both directions (R4.3/V-1):
      * an undeclared structure or type -> refuse; there is no class to price from
      * a declared type with NO row in vci_base_rates.json -> refuse, and say which
        key is missing. Falling back to `<structure>/_default` here would be the
        exact defect this function exists to close: the caller declared a class and
        would silently be priced on another one.
    """
    import json as _json
    path = path or os.path.join(HERE, "vci_base_rates.json")
    if not asset_structure or asset_structure in (None, UNDECLARED):
        raise SizingRefused(
            "class_prior needs a declared asset_structure; got %r (R4.3)." % (asset_structure,))
    if not catalyst_type or catalyst_type in (None, UNDECLARED):
        raise SizingRefused(
            "class_prior needs a declared catalyst_type; got %r. An undeclared catalyst has no "
            "class, and pricing it on the structure default would present a guess as a "
            "measurement (R2.10)." % (catalyst_type,))
    try:
        with open(path, encoding="utf-8") as fh:
            br = _json.load(fh)
    except Exception as exc:                                         # noqa: BLE001
        raise SizingRefused("vci_base_rates.json is unreadable (%s); a missing prior is not a "
                            "zero (V-1)." % exc)
    key = "%s/%s" % (asset_structure, catalyst_type)
    row = (br.get("p_thesis") or {}).get(key)
    if row is None:
        raise SizingRefused(
            "vci_base_rates.json declares no p_thesis row for %r. The catalyst class was "
            "DECLARED and the base rates do not carry it, so the position cannot be priced "
            "from its own class - and it must NOT be silently priced from "
            "%r/_default instead (R4.3, ISA-0687). Add the row or change the declared type."
            % (key, asset_structure))
    L_rows = br.get("L_by_structure") or {}
    if asset_structure not in L_rows:
        raise SizingRefused(
            "vci_base_rates.json declares no L for asset_structure %r." % asset_structure)
    return {"p_thesis": float(row["p"]), "L": float(L_rows[asset_structure]["L"]),
            "basis": key, "confidence": row.get("confidence"),
            "why": ("priced from the catalyst's own class %r: p = %.2f, L = %.2f (%s). "
                    "Declared, not defaulted - an undeclared type REFUSES here rather than "
                    "falling through to the structure default."
                    % (key, float(row["p"]), float(L_rows[asset_structure]["L"]),
                       row.get("note", "no note")))}


def vci_size_pct(*, p_thesis, L, budget_available_pct, evidence_state,
                 correlation_rider=1.0, policy=None) -> dict:
    """w_vci = min( B_available / ((1-p)*L*rider), ladder[state], hard caps ).

    ⚑ A missing p_thesis is NOT p = 0. That exact null once flipped DENY->ADMIT on QBTS
    (FC-F, V-1). It RAISES here."""
    _fi_mark("position_sizing", "vci_size_pct")   # ISA-0699: execution-ledger observation
    if p_thesis is None or L is None:
        raise SizingRefused(
            "VCI sizing needs both p_thesis and L. A missing p_thesis silently read as p = 0 is "
            "the defect that flipped DENY to ADMIT on QBTS — a missing input is not a measured "
            "zero (V-1, R4.3).")
    p, l_, rider = float(p_thesis), float(L), float(correlation_rider or 1.0)
    if not (0.0 < p < 1.0):
        raise SizingRefused(f"p_thesis must be strictly between 0 and 1; got {p_thesis!r}")
    if l_ <= 0:
        raise SizingRefused(f"loss-given-failure must be positive; got {L!r}")
    denom = (1.0 - p) * l_ * rider
    budget_size = float(budget_available_pct) / denom
    lad_t = target_pct(evidence_state, policy=policy)
    caps = hard_caps(policy)
    binding, w = "expected_loss_budget", budget_size
    if lad_t["target_pct"] < w:
        binding, w = "ladder_" + lad_t["rung"], lad_t["target_pct"]
    if caps["max_stock_position_pct"] < w:
        binding, w = "max_stock_position_pct", caps["max_stock_position_pct"]
    return {"w_vci_pct": round(w, 4), "binding_constraint": binding,
            "budget_implied_pct": round(budget_size, 4),
            "ladder_pct": lad_t["target_pct"], "rung": lad_t["rung"],
            "expected_loss_per_pct_weight": round(denom, 6),
            "correlation_rider": rider,
            "basis": (f"B_available {budget_available_pct}% / ((1-{p})x{l_}x{rider}) = "
                      f"{budget_size:.2f}%, then min'd against the ladder and the hard-cap "
                      f"stack. The 0.75/1.0/1.5 binary map and the ACS->size table are DELETED "
                      f"(clean spec s5).")}


# ══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0688 (20-Sep-2026) — TWO QUESTIONS, TWO FIELDS, TWO CONSUMERS
# ══════════════════════════════════════════════════════════════════════════════════════════
# One `is_binary` flag answered two different questions and QBTS answered them differently:
#
#   POSITION_EVENT_RISK  — is the economic distribution of OWNING this position sufficiently
#                          discontinuous / event-dominated that it belongs in the VCI
#                          expected-loss and joint-tail treatment?
#                          CONSUMER: the L1 expected-loss budget, joint-tail/correlation
#                          treatment, and any position-level VCI risk ceiling. NOTHING ELSE.
#
#   CATALYST_IS_BINARY   — is the CURRENT NEXT material catalyst a discrete success/failure
#                          event capable of materially changing value?
#                          CONSUMER: the concurrent-binary-event count/cap. NOTHING ELSE.
#
# ⚑ MEASURED CONSEQUENCE (Wave 2 BuildSpec §2): QBTS consumed 0.1181% of the 1.5% expected-loss
# budget AND one of two concurrent-event slots, although its next catalyst was separately
# described as observable. One flag, two answers, and the position paid for both.
#
# ⚑ ONE FIELD IS NEVER INFERRED FROM THE OTHER, and an UNKNOWN is NEVER silently FALSE:
# each consumer declares its own conservative treatment for UNKNOWN. `is_binary` remains the
# DECLARED legacy field and is read as the fallback for BOTH, because a position declared
# under the old schema said something true about itself — it just said it once.
BINARY_TRUE, BINARY_FALSE, BINARY_UNKNOWN = True, False, "UNKNOWN"
BINARY_STATES = (BINARY_TRUE, BINARY_FALSE, BINARY_UNKNOWN)


def _typed_binary(value, legacy):
    """Normalise a declared typed field. Absence falls back to the LEGACY `is_binary`
    declaration; an absent legacy field too is UNKNOWN, never False (V-1/R4.3)."""
    if value is None:
        return BINARY_UNKNOWN if legacy is None else bool(legacy)
    if isinstance(value, str):
        v = value.strip().upper()
        if v == "UNKNOWN":
            return BINARY_UNKNOWN
        if v in ("TRUE", "YES"):
            return BINARY_TRUE
        if v in ("FALSE", "NO"):
            return BINARY_FALSE
        raise SizingRefused(
            "%r is not a declared binary state. ISA-0688 declares exactly %s — a new one is a "
            "change to the contract, not a string." % (value, list(BINARY_STATES)))
    return bool(value)


def position_event_risk(position: dict):
    """ISA-0688 — does OWNING this position belong in the expected-loss / joint-tail treatment?

    ⚑ Read ONLY by the L1 expected-loss budget and the joint-tail treatment. A consumer that
    reads this to decide the CONCURRENT-EVENT CAP is reading the wrong field, and
    `consistency_check.pair_binary_fields_have_separate_consumers` says so."""
    return _typed_binary(position.get("position_event_risk"), position.get("is_binary"))


def catalyst_is_binary(position: dict):
    """ISA-0688 — is the CURRENT NEXT catalyst a discrete go/no-go event?

    ⚑ Read ONLY by the concurrent-binary-event count/cap. A catalyst RESOLUTION invalidates the
    previous answer: the successor event must be identified and classified afresh, which is why
    this reads the SUCCESSOR's own declaration once the parent has resolved."""
    st = position.get("catalyst_status")
    if st == RESOLVED_POSITIVE_SUCCESSOR_PENDING:
        succ = position.get("successor") or {}
        # The old classification died with the old catalyst. If the successor declares nothing,
        # the honest answer is UNKNOWN — not the parent's answer carried forward.
        return _typed_binary(succ.get("catalyst_is_binary"), None)
    if st in (RESOLVED_POSITIVE, RESOLVED_NEGATIVE):
        return BINARY_FALSE          # resolved: there is no live dated go/no-go event
    return _typed_binary(position.get("catalyst_is_binary"), position.get("is_binary"))


def binary_fields(position: dict) -> dict:
    """Both typed fields plus the mechanical mapping, as DATA a reader can check (§5.7)."""
    per = position_event_risk(position)
    cib = catalyst_is_binary(position)
    return {
        "ticker": position.get("ticker"),
        "position_event_risk": per,
        "catalyst_is_binary": cib,
        # Wave 2 §5.7's table, mechanised. UNKNOWN never collapses to FALSE on either axis.
        "consumes_expected_loss_budget": (True if per is BINARY_TRUE
                                          else (BINARY_UNKNOWN if per == BINARY_UNKNOWN else False)),
        "consumes_concurrent_event_slot": (True if cib is BINARY_TRUE
                                           else (BINARY_UNKNOWN if cib == BINARY_UNKNOWN else False)),
        "declared_legacy_is_binary": position.get("is_binary"),
        "basis": ("ISA-0688: position event risk and current-catalyst binary character are "
                  "SEPARATE typed concepts with separate consumers. Neither is inferred from "
                  "the other and an UNKNOWN is never silently FALSE."),
    }


def binary_commitment(position: dict) -> dict:
    """`is_binary` is STATEFUL. RESOLVED releases the budget commitment ON THE SAME RUN.

    ⚑ ISA-0424: ABCL's Phase 2 resolved POSITIVE on 10-Aug-2026 and its 0.209 commitment was
    still reserved against nothing, because `is_binary` had no expiry. That is 14% of a 1.5%
    budget held against an event that has already happened."""
    st = position.get("catalyst_status")
    if position.get("is_binary") and st not in CATALYST_STATUSES:
        raise SizingRefused(
            f"{position.get('ticker')}: is_binary is true but catalyst_status is {st!r}. "
            f"Declared: {list(CATALYST_STATUSES)}. A binary with no catalyst state cannot have "
            f"its commitment released and would reserve budget forever (ISA-0424).")
    # ── ISA-0655 — a SUCCESSOR re-arms; it is never released ────────────────────────────
    if position.get("is_binary") and st == RESOLVED_POSITIVE_SUCCESSOR_PENDING:
        succ = position.get("successor") or {}
        w = float(position.get("size_pct") or 0.0)
        if succ.get("priceable") and succ.get("p_thesis") is not None and succ.get("L") is not None:
            p_, l_ = float(succ["p_thesis"]), float(succ["L"])
            c = w * (1.0 - p_) * l_
            return {"ticker": position.get("ticker"), "commits_budget": True,
                    "commitment_pct": round(c, 6), "successor_state": SUCCESSOR_PRICED,
                    "catalyst_date": succ.get("date"),
                    "reason": (f"successor binary PRICED (p={p_}, L={l_}); commits "
                               f"w x (1-p) x L = {c:.4f}% of the ISA")}
        # UNPRICED: re-arm at the most adverse DECLARED prior rather than release. Reading an
        # unpriceable successor as "no binary" is a measured zero and understates the budget.
        adv = adverse_prior(position.get("asset_structure"))
        c = w * (1.0 - adv["p_thesis"]) * adv["L"]
        return {"ticker": position.get("ticker"), "commits_budget": True,
                "commitment_pct": round(c, 6), "successor_state": SUCCESSOR_UNPRICED,
                "refusal_kind": succ.get("refusal_kind") or UNPRICEABLE_BY_NATURE,
                "adverse_prior": adv,
                "reason": (f"successor binary UNPRICED — RE-ARMED at the most adverse declared "
                           f"prior ({adv['basis']}: p={adv['p_thesis']}, L={adv['L']}), "
                           f"committing {c:.4f}% of the ISA. NOT released: a successor that "
                           f"cannot be priced is not an absence of one (V-1/R4.3, ISA-0655). "
                           f"It also counts toward VCI_BINARY_MAX_CONCURRENT and bars further "
                           f"VCI deployment into this name.")}
    if not position.get("is_binary") or st != PENDING:
        return {"ticker": position.get("ticker"), "commits_budget": False, "commitment_pct": 0.0,
                "reason": ("not a binary" if not position.get("is_binary") else
                           f"catalyst {st} — commitment RELEASED on this run; it now sizes as an "
                           f"ordinary platform holding")}
    p, l_ = position.get("p_thesis"), position.get("L")
    basis = "declared on the position"
    if p is None or l_ is None:
        # ISA-0687. An explicit p/L on the record still wins. Failing that, a DECLARED
        # catalyst_type prices the position from its own class - which is what the
        # ISA-0653 refusal told the reader a declared type would buy. An undeclared
        # type still refuses, inside class_prior, naming what is missing.
        ct = position.get("catalyst_type")
        if ct and ct != UNDECLARED:
            cp = class_prior(position.get("asset_structure"), ct)
            p, l_, basis = cp["p_thesis"], cp["L"], cp["basis"]
        else:
            raise SizingRefused(f"{position.get('ticker')}: a PENDING binary with no p_thesis/L "
                                f"and no declared catalyst_type cannot be priced; a null is not "
                                f"a zero (V-1)")
    c = float(position.get("size_pct") or 0.0) * (1.0 - float(p)) * float(l_)
    return {"ticker": position.get("ticker"), "commits_budget": True,
            "commitment_pct": round(c, 6), "catalyst_date": position.get("catalyst_date"),
            "prior_basis": basis, "p_thesis": float(p), "L": float(l_),
            "reason": (f"PENDING binary commits w x (1-p) x L = {c:.4f}% of the ISA "
                       f"(p={float(p):.2f}, L={float(l_):.2f}, basis: {basis})")}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0646 / ISA-0653 / ISA-0654 — THE BUDGET IS ABOUT WHAT IS HELD, AND NOTHING READ THAT
# ═══════════════════════════════════════════════════════════════════════════════════════════
# `summary.vci_binary_risk_committed` was computed as
#     [e for e in _ranked if e.get("deploy_eligible")]
# i.e. over deploy-eligible CANDIDATES from vci_watchlist. A held name is deleted from that
# watchlist the moment it is bought (update_watchlist's purge), so the figure could only ever
# describe positions that DO NOT EXIST. It was not stale; the population was wrong. It then
# read 0 for a second, independent reason — ISA-0617 nulled the candidate fields, so the
# eligible list was empty — and the whole block sat inside `except Exception: pass`. Three
# faults, one output of `0`, and nothing to distinguish them (R2.10).
#
# ⚑ ABSENCE IS NOT `is_binary: false`. binary_commitment() reads a missing `is_binary` as
#   "not a binary" and returns a commitment of 0.0. For a CANDIDATE dict that is fine. For a
#   HELD position it is the V-1 defect: `false` is a declaration somebody made, absence means
#   nobody has, and those are different facts that must not produce the same number.

BINARY_POSITIONS_FILE = "vci_binary_positions.json"
UNDECLARED = "UNDECLARED"


def load_declared_binaries(root: Optional[str] = None) -> dict:
    import json as _json
    import os as _os
    root = root or _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(root, BINARY_POSITIONS_FILE)
    if not _os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return (_json.load(fh) or {}).get("positions") or {}


def held_binary_rows(held, declared=None, root: Optional[str] = None) -> dict:
    """Join broker-truth holdings to the DECLARED binary registry.

    `held`: [{ticker, size_pct}] from portfolio_data — broker truth, never the watchlist.
    Returns {rows, refusals, unpriceable} where `rows` is safe to hand to
    budget_available() and every name that could NOT be resolved is named in `refusals`.

    R4.9 — a reader that cannot match a row COUNTS it and fails; it never drops it."""
    declared = declared if declared is not None else load_declared_binaries(root)
    rows, refusals, unpriceable = [], [], []
    for h in (held or []):
        t = str(h.get("ticker") or "").upper()
        d = declared.get(t)
        if d is None:                       # DECLARED aliases only — never suffix-stripping
            for _k, _v in declared.items():
                if t in [str(a).upper() for a in (_v.get("aliases") or [])]:
                    d = _v
                    break
        if d is None:
            refusals.append({
                "ticker": t, "control": "is_binary",
                "why": ("%s is HELD and absent from %s. Absence is REFUSED, not read as "
                        "not-a-binary: `false` is a declaration, absence means nobody has "
                        "decided, and a null is not a zero (V-1/R4.3). Declare it."
                        % (t, BINARY_POSITIONS_FILE))})
            continue
        if not d.get("is_binary"):
            continue
        _bf = binary_fields(dict(d, ticker=t))
        row = {"ticker": t, "size_pct": h.get("size_pct"), "is_binary": True,
               # ISA-0688 — both typed fields travel with the row to their own consumers
               "position_event_risk": _bf["position_event_risk"],
               "catalyst_is_binary": _bf["catalyst_is_binary"],
               "catalyst_status": d.get("catalyst_status"),
               "catalyst_date": d.get("catalyst_date"),
               "catalyst_type": (None if d.get("catalyst_type") == UNDECLARED
                                 else d.get("catalyst_type")),
               "catalyst_domain": (None if d.get("catalyst_domain") == UNDECLARED
                                   else d.get("catalyst_domain")),
               "asset_structure": d.get("asset_structure")}
        # ⚑ D6 (Raj, 12-Sep-2026): an absent catalyst_domain REFUSES and never defaults to
        #   "not correlated". The x1.5 rider exists to stop two names failing on one event;
        #   reading absence as "no shared domain" is how a correlated pair gets admitted.
        if d.get("catalyst_domain") == UNDECLARED and d.get("catalyst_status") == PENDING:
            refusals.append({
                "ticker": t, "control": "catalyst_domain",
                "why": ("%s is a live PENDING binary with catalyst_domain UNDECLARED, so the "
                        "x1.5 shared-domain rider CANNOT be evaluated. Under D6 an absent "
                        "domain refuses; it never reads as not-correlated (ISA-0653)." % t)})
        if d.get("catalyst_type") == UNDECLARED and d.get("catalyst_status") == PENDING:
            refusals.append({
                "ticker": t, "control": "catalyst_type",
                "why": ("%s is a live PENDING binary with catalyst_type UNDECLARED, so "
                        "p_thesis and L cannot be selected from the catalyst's own class and "
                        "would fall back to `%s` structure defaults — a countdown to an "
                        "unnamed event pricing real capital (ISA-0171/ISA-0653)."
                        % (t, d.get("asset_structure")))})
        succ = d.get("successor") or {}
        if d.get("catalyst_status") in (RESOLVED_POSITIVE, RESOLVED_NEGATIVE) and succ:
            unpriceable.append({"ticker": t, "successor": succ})
        rows.append(row)
    return {"rows": rows, "refusals": refusals, "unpriceable_successors": unpriceable,
            "n_held": len(held or []), "n_binary": len(rows)}


def budget_available(positions: List[dict], budget_pct: float, max_concurrent: int = 1) -> dict:
    """B_available after live PENDING binaries. N is declared, not inferred."""
    # ⚑ ISA-0650 — L2 COUNTS OPEN BINARIES, NOT NAMES. `live` is every position still
    #   committing budget, which now includes a RESOLVED_POSITIVE_SUCCESSOR_PENDING name
    #   re-armed under ISA-0655. That is the whole point of D1/D3: a resolved name releases
    #   its slot the same run, and a name with a live successor does not. Counting NAMES plus
    #   C-1's loss block would leave only the two winners removable and mechanically sell the
    #   winner while protecting the worst risk-adjusted position — ISA-0167 inverted.
    recs = [binary_commitment(p) for p in positions]
    live = [r for r in recs if r["commits_budget"]]
    committed = round(sum(r["commitment_pct"] for r in live), 6)
    released = [r for r in recs if not r["commits_budget"] and r["reason"].startswith("catalyst")]
    # ══ ISA-0688 — THE COUNT CAP IS A DIFFERENT QUESTION FROM THE BUDGET ══════════════
    # `live` is what COMMITS EXPECTED-LOSS BUDGET (the position question). The concurrent-event
    # cap asks whether the position's CURRENT NEXT CATALYST is a dated go/no-go event, and a
    # position can be event-dominated without having one pending — QBTS paid for both on one
    # flag. The two populations are now computed separately and BOTH are published, so a reader
    # can see where they differ instead of being told they are the same set.
    fields = {f["ticker"]: f for f in (binary_fields(p) for p in positions)}
    slot_rows = [f for f in fields.values() if f["consumes_concurrent_event_slot"] is True]
    slot_unknown = [f["ticker"] for f in fields.values()
                    if f["consumes_concurrent_event_slot"] == BINARY_UNKNOWN]
    # ⚑ AN UNKNOWN COUNTS AGAINST THE CAP. R4.3/V-1: an unclassified catalyst is not a measured
    #   absence of one, and the conservative direction for a CAP is to count it.
    n_slots = len(slot_rows) + len(slot_unknown)
    return {"budget_pct": budget_pct, "committed_pct": committed,
            "available_pct": round(max(budget_pct - committed, 0.0), 6),
            "live_binaries": [r["ticker"] for r in live],
            "released_this_run": [r["ticker"] for r in released],
            "max_concurrent": max_concurrent,
            "count_cap_breached": n_slots > max_concurrent,
            # ISA-0688 — the two populations, side by side and never conflated
            "binary_fields": fields,
            "expected_loss_population": [r["ticker"] for r in live],
            "concurrent_event_population": sorted(f["ticker"] for f in slot_rows),
            "concurrent_event_unknown": sorted(slot_unknown),
            "n_concurrent_event_slots": n_slots,
            "cap_basis": ("ISA-0688: the concurrent-event cap counts positions whose CURRENT "
                          "NEXT CATALYST is a dated go/no-go event, plus those whose catalyst "
                          "character is UNKNOWN (an unclassified catalyst is not a measured "
                          "absence of one). It does NOT count a position merely because the "
                          "position is event-dominated — that is the expected-loss question."),
            "commitments": recs}


def budget_available_reported(positions, budget_pct: float, max_concurrent: int = 1) -> dict:
    """budget_available() that REPORTS a refusal instead of raising it.

    ⚑ WHY BOTH FORMS EXIST. `budget_available()` RAISES on an unpriceable PENDING binary, and
    that is right for any caller about to move capital — it must not proceed. But the monthly
    pre-run is not moving capital; it is producing the artefact a human reads, and a control
    that aborts the whole pre-run on first sight of a problem is a control that gets switched
    off (R5.7, and the reason `framework_integrity` is REPORT-ONLY on purpose).

    So this form keeps the refusal and drops the exception: the unpriceable names are NAMED,
    the committed figure is returned as None rather than a number, and `blocks_capital` is
    True. R4.3 — the control returns UNKNOWN and BLOCKS; it never returns PASS, and it never
    returns the 0 that a silent skip would have produced (ISA-0654)."""
    priced, refused = [], []
    for pos in (positions or []):
        try:
            binary_commitment(pos)
            priced.append(pos)
        except SizingRefused as exc:
            refused.append({"ticker": pos.get("ticker"), "why": str(exc)})
    out = budget_available(priced, budget_pct, max_concurrent)
    out["refused_unpriceable"] = refused
    out["n_refused"] = len(refused)
    out["blocks_capital"] = bool(refused)
    if refused:
        # ⚑ The figure is WITHHELD, not reduced. A committed_pct computed over the priceable
        #   subset would be a smaller number that looks like a measurement of the whole book,
        #   and headroom derived from it would license a deployment the refused names might
        #   forbid. "We could not price this" must not read as "this costs less".
        out["committed_pct_measured_over"] = [p.get("ticker") for p in priced]
        out["committed_pct"] = None
        out["available_pct"] = None
        out["why"] = ("%d live PENDING binary(ies) cannot be priced, so the committed figure "
                      "is WITHHELD rather than computed over the remainder: %s"
                      % (len(refused), "; ".join(r["ticker"] for r in refused)))
    return out


def held_binary_budget(held, *, declared=None, budget_pct: float = 1.5,
                       max_concurrent: int = 2, root: Optional[str] = None) -> dict:
    """ISA-0706 — **THE ONE** held-binary L1 budget calculation, with an identity.

    ⚑ WHY THIS EXISTS. Two computations of this budget disagreed on the live Sep-2026 book.
    `binary_budget_report` (the declared one home) MEASURED committed 0.305593% / available
    1.194407%. `held_position_review.review` assembled its OWN binary rows — without
    `catalyst_type`, without `catalyst_date`, without `catalyst_domain` and skipping
    `held_binary_rows`' refusal machinery entirely — read QBTS as unpriceable, WITHHELD the
    budget, and therefore returned `vci_size` UNEVALUATED for every held binary. Two answers
    to one question, and the one that decided capital was the worse-informed copy.

    Every consumer now calls THIS, and every consumer records the returned `calc_id`, so a
    reader can prove two figures came from one calculation rather than hoping they agree
    (R4.4/R4.5: two paths call one function, or they are one function).

    `held`: [{ticker, size_pct}] from portfolio_data — broker truth, never the watchlist.
    """
    _fi_mark("position_sizing", "held_binary_budget")
    hb = held_binary_rows(held, declared=declared, root=root)
    bud = budget_available_reported(hb["rows"], budget_pct=budget_pct,
                                    max_concurrent=max_concurrent)
    import hashlib as _hl
    import json as _json
    basis = _json.dumps({"rows": hb["rows"], "budget_pct": budget_pct,
                         "max_concurrent": max_concurrent}, sort_keys=True, default=str)
    calc_id = "VCIB-%s" % _hl.sha256(basis.encode("utf-8")).hexdigest()[:12]
    return {"calc_id": calc_id,
            "rows": hb["rows"], "refusals": hb["refusals"],
            "unpriceable_successors": hb["unpriceable_successors"],
            "n_held": hb["n_held"], "n_binary": hb["n_binary"],
            "budget_pct": budget_pct, "max_concurrent": max_concurrent,
            "committed_pct": bud.get("committed_pct"),
            "available_pct": bud.get("available_pct"),
            "blocks_capital": bud.get("blocks_capital"),
            "refused_unpriceable": bud.get("refused_unpriceable"),
            "why": bud.get("why"),
            "budget": bud,
            "authority": ("position_sizing.held_binary_budget — the single home (ISA-0706). "
                          "A consumer that assembles its own binary rows is a second sizing "
                          "authority and is refused by "
                          "consistency_check.pair_single_binary_budget_authority.")}


def binary_budget_report(portfolio_path: str, *, budget_pct: float = 1.5,
                         max_concurrent: int = 2, root: Optional[str] = None) -> dict:
    """THE one home for the E4 held-binary budget (R4.4/R4.5).

    Called by monthly_isa_prerun step 6.5 AND by its `--vci-budget-only` entry point, so the
    orchestrated path and the operable path are the SAME function rather than two copies that
    drift (R4.5: two paths call one function, or they are one function).

    Returns {summary, warnings, ok}. Every refusal is named in `warnings`; the committed
    figure is None — never 0 — whenever it could not be measured (R4.3/V-1)."""
    _fi_mark("position_sizing", "binary_budget_report")   # ISA-0699: execution-ledger observation
    import json as _json
    import os as _os
    warn = []
    if not _os.path.exists(portfolio_path):
        return {"ok": False, "warnings": ["Step 6.5: portfolio_data not found at %s; the VCI "
                                          "binary budget is UNCOMPUTED, not zero."
                                          % portfolio_path],
                "summary": {"vci_binary_risk_committed": None,
                            "vci_binary_risk_budget": budget_pct}}
    with open(portfolio_path, encoding="utf-8") as fh:
        pd = _json.load(fh)
    nav = (pd.get("summary", {}) or {}).get("total_value_gbp")          # ISA-0673
    stocks = pd.get("stocks") or []
    if not nav or not stocks:
        warn.append("Step 6.5: the VCI binary risk budget could not be computed — %s. "
                    "Published as None, never as 0 (R4.3/V-1)."
                    % ("portfolio_data has no summary.total_value_gbp (ISA-0673)" if not nav
                       else "no stock positions in portfolio_data"))
        return {"ok": False, "warnings": warn,
                "summary": {"vci_binary_risk_committed": None,
                            "vci_binary_risk_budget": budget_pct}}
    held = [{"ticker": x.get("ticker"),
             "size_pct": 100.0 * float(x.get("value_gbp") or 0.0) / float(nav)}
            for x in stocks]
    # ISA-0706: through the single home, so this report and held_position_review cannot
    # disagree — they are the same calculation and they publish the same calc_id.
    _auth = held_binary_budget(held, budget_pct=budget_pct, max_concurrent=max_concurrent,
                               root=root)
    hb = {"rows": _auth["rows"], "refusals": _auth["refusals"],
          "unpriceable_successors": _auth["unpriceable_successors"],
          "n_held": _auth["n_held"], "n_binary": _auth["n_binary"]}
    bud = _auth["budget"]
    for r in hb["refusals"]:
        warn.append("Step 6.5 VCI budget REFUSAL [%s/%s]: %s" % (r["ticker"], r["control"], r["why"]))
    for r in bud["refused_unpriceable"]:
        warn.append("Step 6.5 VCI budget UNPRICEABLE [%s]: %s" % (r["ticker"], r["why"]))
    if bud["blocks_capital"]:
        warn.append("Step 6.5: summary.vci_binary_risk_committed is WITHHELD (None), not 0 — "
                    "%s. A committed figure computed over the priceable remainder would "
                    "understate the book and license a deployment the refused names may "
                    "forbid." % bud.get("why"))
    for u in hb["unpriceable_successors"]:
        warn.append("Step 6.5 (ISA-0655): %s resolved its catalyst but carries a successor in "
                    "state %s (%s). CATALYST_STATUSES has no successor state, so its "
                    "commitment is currently RELEASED as though the binary sequence had ended."
                    % (u["ticker"], u["successor"].get("state"),
                       u["successor"].get("refusal_kind")))
    return {
        "ok": not bud["blocks_capital"],
        "warnings": warn,
        "calc_id": _auth["calc_id"],                       # ISA-0706: one calculation, named
        "summary": {
            "vci_binary_risk_calc_id": _auth["calc_id"],
            "vci_binary_risk_budget": budget_pct,
            "vci_binary_risk_committed": bud["committed_pct"],
            "vci_binary_risk": {
                "basis": "HELD positions (broker truth), not watchlist candidates",
                "nav_gbp": nav,
                "n_held": hb["n_held"], "n_binary_declared": hb["n_binary"],
                "live_pending_binaries": bud["live_binaries"],
                "released_this_run": bud["released_this_run"],
                "committed_pct": bud["committed_pct"],
                "available_pct": bud["available_pct"],
                "count_cap": max_concurrent,
                "count_cap_breached": bud["count_cap_breached"],
                "blocks_capital": bud["blocks_capital"],
                "refused_unpriceable": bud["refused_unpriceable"],
                "registry_refusals": hb["refusals"],
                "unpriceable_successors": hb["unpriceable_successors"],
                "why": bud.get("why"),
            },
        },
    }


# ────────────────────────────────────────────────────────────── s2 demand-pull
def stock_max(candidates: List[dict], *, nav_gbp: float, capital_on_offer_gbp: float,
              policy=None, population_binding: Optional[str] = None) -> dict:
    """THE demand-pull rule (ISA-0430). Capital is pulled by qualified USES, not pushed by a band.

    Each candidate: {ticker, qualifies: bool, evidence_state, current_value_gbp,
                     correlation: <record>, disqualified_reason}

    ⚑ This is strictly STRONGER than the freeze it replaces. The freeze asked "has enough time
    passed"; the demand test asks "does this specific pound have a qualified destination". A
    pound with no qualified use does not enter the sleeve at all.
    ⚑ And it is only as strong as the qualification behind it — see the module docstring.

    ⚑⚑ `population_binding` (ISA-0535, 02-Sep-2026) — WHY AN EMPTY LIST IS NOT ONE FACT.
    `stock_candidates` distinguishes THREE empties and names them: `no_candidates_built`
    (the pipeline never produced a list), `all_candidates_rejected` (it produced one and every
    gate said no), and a REFUSAL (a name's verdict was unknown, so nothing was assessed). All
    three arrived here as `candidates == []` and left as `binding: "nothing_qualifies"` —
    collapsing, at this one seam, exactly the distinction the layer below raises an exception
    to preserve. The router then published *"Nothing qualifies"*, which reads as a measured
    rejection of every name, on a book where nothing had been assessed at all.

    ⚑ Found by the §7.7 eye-inspection, not by an assertion: through `build()` on the delivered
    tree the sleeve read `STOCK_SLEEVE_BLOCKED · stock_max GBP 0.00 · "Nothing qualifies"`,
    and the true cause was that the DELIVERED `step9_pre_aug_2026.json` predates ISA-0487 and
    carries no `t1_qualified` on any of its 32 rows. Both facts print the same number.

    ⚑ Callers pass the candidate artefact's own `binding` through. When the list is empty and
    NOBODY named the reason, the binding is `empty_candidate_list_unattributed` — never
    `nothing_qualifies`, because an unattributed empty list is not a measured rejection."""
    _fi_mark("position_sizing", "stock_max")
    policy = policy if policy is not None else load_policy()
    uses, rejected, total = [], [], 0.0
    for c in candidates:
        tk = c.get("ticker")
        if not c.get("qualifies"):
            rejected.append({"ticker": tk, "reason": c.get("disqualified_reason")
                             or "did not qualify", "gbp": 0.0})
            continue
        t = apply_correlation(target_pct(c["evidence_state"], policy=policy,
                                         current_pct=(float(c.get("current_value_gbp") or 0.0)
                                                      / nav_gbp * 100.0 if nav_gbp else 0.0)),
                              c.get("correlation"), policy=policy)
        if not t["may_receive_new_capital"]:
            rejected.append({"ticker": tk, "gbp": 0.0,
                             "reason": f"evidence_state {c['evidence_state']} may not receive "
                                       f"new capital (D13)"})
            continue
        want = t["target_pct"] / 100.0 * float(nav_gbp)
        gap = want - float(c.get("current_value_gbp") or 0.0)
        if gap <= 0:
            rejected.append({"ticker": tk, "gbp": 0.0,
                             "reason": f"already at or above its {t['rung']} target of "
                                       f"{t['target_pct']}% — no demand"})
            continue
        # ⚑ CARRY THE CURRENT VALUE ONTO THE USE (ISA-0563, 02-Sep-2026). `allocate()` decides
        # NEW-vs-TOP-UP with `is_new = current_value_gbp <= 0`, and the use row did not carry
        # the field — so `.get()` returned None, every use read as NEW, and the D16 entry floor
        # was applied to top-ups of positions Raj already owns. It was invisible while the
        # queue held only new names; the moment held top-ups joined it, COCO (held at
        # GBP 1,214.60) was refused as "a NEW position may not open below the floor".
        # The gap two lines above is ALREADY computed from this value, so the row was carrying
        # the arithmetic and dropping the fact it was derived from.
        # ⚑ ISA-0752 (25-Sep-2026): the use row CARRIES the evidence_state it was sized on. It
        #   was dropped here, so every proposal/obligation downstream read u.get("evidence_state")
        #   as None (HALO 12-Sep: evidence_state_at_entry null). Carried, never recomputed - the
        #   value is the canonical producer's (capital_destination pipeline.evidence_states ->
        #   stock_candidates), the same one the judgement scope now carries (ISA-0698).
        uses.append({"ticker": tk, "rung": t["rung"], "target_pct": t["target_pct"],
                     "gbp": round(gap, 2), "capped_by": t["capped_by"],
                     "current_value_gbp": round(float(c.get("current_value_gbp") or 0.0), 2),
                     "correlation_measured": t["correlation_measured"],
                     "evidence_state": c["evidence_state"]})
        total += gap
    derived = min(total, float(capital_on_offer_gbp))
    return {
        "stock_max_gbp": round(derived, 2),
        "demand_gbp": round(total, 2),
        "capital_on_offer_gbp": round(float(capital_on_offer_gbp), 2),
        "binding": ("capital_on_offer" if total > capital_on_offer_gbp else
                    "qualified_demand" if total > 0 else
                    # ⚑ ISA-0535: an EMPTY input is attributed to the population that produced
                    #   it, or declared unattributed. `nothing_qualifies` is reserved for the
                    #   case it actually describes — candidates existed and none had demand.
                    ("nothing_qualifies" if candidates else
                     (population_binding or "empty_candidate_list_unattributed"))),
        "population_binding": population_binding,
        "n_candidates_in": len(candidates),
        "empty_input_note": (None if candidates else
                             ("the candidate list was EMPTY. `nothing_qualifies` would state "
                              "that every name was assessed and rejected; that is a different "
                              "fact from 'no list was built' and from 'a name's verdict was "
                              "unknown', and only one of them is evidence (R2.10). Attributed "
                              "as %r." % (population_binding or "unattributed"))),
        "qualifying_uses": uses, "rejected": rejected,
        "routes_to_funds_gbp": round(max(float(capital_on_offer_gbp) - derived, 0.0), 2),
        # ⚑ ISA-0535 — the basis describes the MECHANISM; it must not assert this run's verdict.
        #   It previously ended "Nothing qualifies -> 0 -> routes to funds", which is a claim
        #   about the run and was printed verbatim on a book where nothing had been ASSESSED.
        "basis": ("clean spec s2 demand-pull. stock_max = sum of (ladder target x NAV - current "
                  "value) over QUALIFYING uses, capped ONLY by the capital on offer; a total of "
                  "0 routes the capital to funds. The Phase-1 band, A3's N_eff ladder and the "
                  "one-position-per-run cap are all REMOVED. This run's verdict is in "
                  "`binding`, not here."),
        "derived_not_typed": "R4.4 — computed from live values every run, never stored",
    }


# ══════════════════════════════════════════════════════════════════════════════════════
# P4.3 — allocate(): FLOOR-THEN-PRIORITY FILL  (Raj D15 / D16 / D17, 26-Aug-2026)
# ══════════════════════════════════════════════════════════════════════════════════════
# ⚑ D15 AMENDS THE CLEAN SPEC. §1's *"a position reaches 3.5% or it does not exist"* becomes
# an END STATE, not an ENTRY RULE. Fill #1 to a full STARTER; walk the declared order; open a
# later candidate only if its allocation clears the entry floor; the remainder routes back to
# `capital_destination`.
#
# ⚑ D16 — MIN_ENTRY_FRACTION_OF_STARTER = 0.80, DERIVED FROM THE LADDER EVERY RUN, NEVER
# TYPED. At NAV 156,321.05 that is 2.80% = GBP 4,377.
#
# ⚑⚑ P4.4 — THE 0.75 / 0.80 PAIR IS TWO RULES AND A FUTURE SESSION WILL TRY TO UNIFY THEM.
#     risk_contribution.FLAG_FRACTION_OF_STARTER = 0.75  -> "below this a HELD position is not
#                                                           carrying its risk share"  (REVIEW)
#     position_sizing.MIN_ENTRY_FRACTION_OF_STARTER = 0.80 -> "below this, do not OPEN a
#                                                             position at all"        (ENTRY)
# They answer different questions about different populations. Reading them as a duplicate and
# unifying them would silently move the entry floor, which is why
# `consistency_check.pair_entry_and_review_fractions_distinct()` asserts both exist, are
# unequal, and each carries a docstring naming its own rule.

MIN_ENTRY_FRACTION_OF_STARTER = 0.80

FILL_STORE = os.path.join(os.environ.get("ISA_OUT", HERE), "underfilled_positions.json")


def _partial_starter_entry() -> bool:
    """P4.7's second rollback constant. False ⇒ whole STARTERs only, residual to funds."""
    try:
        import isa_policy as _p
        if "partial_starter_entry" in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS["partial_starter_entry"])
    except Exception:                                                   # noqa: BLE001
        pass
    return True


def min_entry_gbp(nav_gbp: float, policy=None) -> dict:
    """The entry floor in GBP. DERIVED from the ladder every run (R4.4), never stored."""
    lad = ladder(policy)
    starter_pct = lad["STARTER"]
    _fi_mark("position_sizing", "min_entry_gbp")   # ISA-0695: the quantity register's declared computer
    return {"min_entry_gbp": round(MIN_ENTRY_FRACTION_OF_STARTER * starter_pct / 100.0
                                   * float(nav_gbp), 2),
            "starter_gbp": round(starter_pct / 100.0 * float(nav_gbp), 2),
            "fraction": MIN_ENTRY_FRACTION_OF_STARTER, "starter_pct": starter_pct,
            "basis": ("MIN_ENTRY = %.2f x STARTER (D16). ⚑ DISTINCT from "
                      "risk_contribution.FLAG_FRACTION_OF_STARTER 0.75, which is the REVIEW "
                      "fraction for a HELD position — two rules, two populations, two "
                      "questions (P4.4)." % MIN_ENTRY_FRACTION_OF_STARTER)}


def load_fill_obligations(path=None) -> dict:
    p = path or FILL_STORE
    if not os.path.exists(p):
        return {"_what": "P4.6 fill-obligation store. A sub-STARTER entry carries a FIRST "
                         "CLAIM on the next tranche, ahead of any new position (D17).",
                "obligations": []}
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_fill_obligations(doc, path=None) -> str:
    p = path or FILL_STORE
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)
    return p


def void_obligations(doc: dict, states: Dict[str, dict], *, today=None) -> dict:
    """D17: an obligation VOIDS on `evidence_state` DEGRADED_* or `thesis_state` BROKEN.

    ⚑ VOIDED ENTRIES ARE RETAINED, NEVER REMOVED (R2.13, R6.5). That an obligation was created
    and then abandoned IS the learning — deleting it leaves a store that only ever shows
    obligations that worked out."""
    today = today or datetime.date.today().isoformat()
    for o in doc.get("obligations", []):
        if o.get("voided"):
            continue
        st = states.get(o.get("ticker")) or {}
        ev, th = st.get("evidence_state"), st.get("thesis_state")
        why = None
        if ev and str(ev).startswith("DEGRADED_"):
            why = "evidence_state %s" % ev
        elif th == "BROKEN":
            why = "thesis_state BROKEN"
        if why:
            o["voided"] = True
            o["state"] = "VOIDED"
            o["voided_on"] = today
            o["voided_reason"] = why
    return doc


def refresh_obligations(states: Optional[Dict[str, dict]] = None, *, today=None,
                        path=None, root: Optional[str] = None,
                        dry_run: bool = False) -> dict:
    """ISA-0669 — the obligation LIFECYCLE, run every pre-run.

    Loads the store, VOIDS what D17 says must void (evidence_state DEGRADED_* or thesis_state
    BROKEN), persists, and reports what is open. This is `void_obligations`' first caller:
    it too had zero call sites, so an obligation could never have been retired either — the
    store would have accumulated claims that D17 had already cancelled.

    ⚑ VOIDED ENTRIES ARE RETAINED, NEVER DELETED (R2.13/R6.5). That an obligation was created
    and then abandoned IS the learning; a store showing only obligations that worked out
    measures nothing."""
    doc = load_fill_obligations(path)
    if states is None:
        states = {}
        try:
            import thesis_state as _ts
            for tk, row in (_ts.load_states(root) or {}).items():
                states.setdefault(tk, {})["thesis_state"] = row.get("state")
        except Exception:                                            # noqa: BLE001
            pass
    before = [o["ticker"] for o in doc.get("obligations", []) if not o.get("voided")]
    # ⚑ ISA-0701: every pre-run retains-but-voids open rows without execution provenance, so a
    #   fabricated proposal-time row can never become (or stay) a first claim (R14.1).
    _unproven = invalidate_unproven_obligations(doc, today=today)["voided"]
    doc = void_obligations(doc, states, today=today)
    after = [o["ticker"] for o in doc.get("obligations", []) if not o.get("voided")]
    # ⚑ A DRY RUN MUST NOT WRITE A STORE. Caught by running the real pre-run with --dry-run
    #   and finding underfilled_positions.json had a fresh mtime: the first version of this
    #   function persisted unconditionally, so a rehearsal mutated live state. R18.1's point
    #   exactly — development belongs in a CANDIDATE, and a function that cannot be rehearsed
    #   without side effects makes that impossible.
    written = None if dry_run else save_fill_obligations(doc, path)
    voided = sorted(set(before) - set(after))
    return {"store": written, "dry_run": bool(dry_run),
            "n_total": len(doc.get("obligations", [])),
            "open": after, "voided_this_run": voided,
            "voided_unproven_isa0701": _unproven,
            "warnings": (["Step 6.5 (ISA-0669): fill obligation VOIDED for %s — D17 voids on "
                          "evidence_state DEGRADED_* or thesis_state BROKEN. The claim on the "
                          "next tranche is cancelled and the row is RETAINED, not deleted." % t
                          for t in voided]
                         + (["Step 6.5 (ISA-0669): %d open fill obligation(s) have FIRST CLAIM "
                             "on the next tranche, ahead of any new position: %s"
                             % (len(after), ", ".join(after))] if after else []))}


# ══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0701 (16-Sep-2026) — FILL OBLIGATIONS ARE EXECUTION-OWNED
# ══════════════════════════════════════════════════════════════════════════════════════════
# A first claim on the next tranche becomes ACTIVE only when an AUTHORISED decision is matched to
# BROKER-CONFIRMED execution (activate_from_executions, pre-run Step 1.5). A proposal, report,
# dry run, SHADOW run, scenario or re-run never creates one: allocate() is READ-ONLY on the store
# and returns ephemeral `proposed_obligations`. ISA-0669 made obligations persist at all; it put
# the write on PROPOSAL production, which fabricated HALO/ZAB.WA (12-Sep) - 9.0% NAV of first
# claims for positions nobody bought. ROLLBACK (R4.13): none that re-enables proposal persistence;
# V2_FLAGS["obligation_activation"]=False stops NEW activations (obligations then never become
# ACTIVE - the safe state), it never restores the TB-04 write.
OBLIGATION_KINDS = ("UNDERFILLED_ENTRY", "CAP_CONSTRAINED_ENTRY")
OBLIGATION_STATES = ("ACTIVE", "FULFILLED", "VOIDED")
OBLIGATION_PROVENANCE_KEYS = ("obligation_id", "source_decision_id", "execution_reference")
ACTIVATION_OUTCOMES = ("ACTIVATED", "FULFILLED", "PARTIAL_FILL_RECORDED", "IDEMPOTENT_SKIP",
                       "FILLED_AT_EXECUTION", "NO_PLANNED_SHORTFALL", "NO_AUTHORISED_PLAN",
                       "PLAN_NOT_CONTEMPORANEOUS", "UNVERIFIED_EXECUTION",
                       "EXECUTION_DEVIATION_BELOW_MIN_ENTRY", "BLOCKED_ON_ISA-0686",
                       "BLOCKED_NOT_ADMITTED",
                       "NOT_BUY_LIKE", "ACTIVATION_DISABLED")
# ⚑ ISA-0701 / ISA-0686 (20-Sep-2026) — THE VCI ROUTE IS NO LONGER BLOCKED.
# This map existed for one reason, stated in the code that read it: "VCI deploy decisions are
# not canonical in decision_ledger (ISA-0686)". ISA-0686 CLOSED on 20-Sep-2026 —
# vci_run_capture.write() now writes every VCI decision through decision_ledger.log_decision
# with a route, a build id, an input snapshot and supersession, and the boundary contract
# consistency_check.pair_vci_decision_captured proves it over every artefact on disk. A VCI
# execution can therefore be matched to a canonical authorised decision like any other, and
# blocking it now would be refusing on a reason that no longer exists (R7.9: an old corrective
# action is not executed because the register still says so).
# ⚑ It is emptied, not deleted: the mechanism stays, so a future route that genuinely lacks
# canonical decisions can be blocked by declaration rather than by a new code path.
ACTIVATION_BLOCKED_ROUTES: Dict[str, str] = {}


def obligation_binding(o: dict) -> dict:
    """-> {"binding": bool, "reason": str}. The ONE definition of a claim allocate() honours."""
    if o.get("voided") or o.get("state") == "VOIDED":
        return {"binding": False, "reason": "VOIDED"}
    if o.get("state") != "ACTIVE":
        return {"binding": False,
                "reason": ("UNPROVEN_NO_EXECUTION_PROVENANCE (ISA-0701): state %r - only an ACTIVE, "
                           "execution-activated obligation carries a first claim" % o.get("state"))}
    missing = [k for k in OBLIGATION_PROVENANCE_KEYS if not o.get(k)]
    if missing:
        return {"binding": False,
                "reason": "UNPROVEN_NO_EXECUTION_PROVENANCE (ISA-0701): missing %s" % ", ".join(missing)}
    return {"binding": True, "reason": "ACTIVE with decision + execution provenance"}


def invalidate_unproven_obligations(doc: dict, *, today=None, item: str = "ISA-0701") -> dict:
    """Retain-but-VOID every open row without execution provenance (never delete - R2.13/R6.5).
    Original fields are kept; correction provenance is appended. Idempotent."""
    today = today or datetime.date.today().isoformat()
    voided = []
    for o in doc.get("obligations", []):
        if o.get("voided") or o.get("state") in ("VOIDED", "FULFILLED"):
            continue
        b = obligation_binding(o)
        if b["binding"]:
            continue
        o.setdefault("correction_provenance", []).append({
            "item": item, "on": today, "action": "VOIDED_NON_BINDING",
            "original": {"voided": o.get("voided"), "voided_reason": o.get("voided_reason"),
                         "state": o.get("state")},
            "why": b["reason"]})
        o["voided"], o["state"], o["voided_on"] = True, "VOIDED", today
        o["voided_reason"] = ("%s remediation: proposal-only obligation - written by a reporting/"
                              "proposal run with no authorised decision matched to a confirmed broker "
                              "execution; non-binding, retained as evidence" % item)
        voided.append(o.get("ticker"))
    return {"doc": doc, "voided": voided}


def month_label_of(date_iso: str) -> str:
    return datetime.date.fromisoformat(str(date_iso)[:10]).strftime("%b_%Y").lower()


def activate_from_executions(entries: List[dict], plan_loader, *, today=None, path=None,
                             dry_run: bool = False, doc: Optional[dict] = None,
                             membership=None) -> dict:
    """ISA-0701 — the ONLY creator of an ACTIVE obligation (and of FULFILLED transitions).

    `entries`: decision_ledger entries AFTER reconcile_executions_from_transactions.
    `plan_loader(month_label)` -> {"allocation": {...}, "artifact": str, "sha256": str,
    "as_of": "YYYY-MM-DD"} or None: the AUTHORISED allocation of the decision's month.
    Requires, per obligation: buy-like decision + confirmed broker execution (transaction_record,
    executed amount) + a PROPOSED obligation for that ticker in a plan dated no later than the
    decision + a unique execution identity. Never infers from `held`; unmatched/off-framework trades
    are never entries, so they can never activate anything."""
    _fi_mark("position_sizing", "activate_from_executions")   # ISA-0699: execution-ledger observation
    today = today or datetime.date.today().isoformat()
    own = doc is None
    doc = load_fill_obligations(path) if own else doc
    try:
        import isa_policy as _p
        enabled = bool(_p.V2_FLAGS.get("obligation_activation", True))
    except Exception:                                                   # noqa: BLE001
        enabled = True
    obls = doc.setdefault("obligations", [])
    by_id = {o.get("obligation_id"): o for o in obls if o.get("obligation_id")}
    outcomes, changed = [], False
    for e in entries or []:
        d = str(e.get("decision") or "").strip().lower()
        if e.get("execution_status") != "confirmed_executed":
            continue
        tk, did = str(e.get("ticker") or "").upper(), e.get("_id")
        rec = {"decision_id": did, "ticker": tk, "route": e.get("route")}
        if d not in ("buy", "top_up"):
            outcomes.append(dict(rec, outcome="NOT_BUY_LIKE"))
            continue
        if not enabled:
            outcomes.append(dict(rec, outcome="ACTIVATION_DISABLED"))
            continue
        if str(e.get("route") or "").lower() in ACTIVATION_BLOCKED_ROUTES:
            outcomes.append(dict(rec, outcome="BLOCKED_ON_ISA-0686",
                                 why="VCI deploy decisions are not canonical in decision_ledger (ISA-0686); "
                                     "no obligation is inferred from the holding"))
            continue
        # ⚑ ISA-0685 (20-Sep-2026) — ISA-0701 mandatory negative 6, now mechanical: a held name
        #   with no canonical admission decision creates NO obligation. Until today there was
        #   nowhere to ASK the question; decision_ledger.current_decision is that place.
        try:
            import sleeve_membership as _sm_act
            if callable(membership):
                _mem = membership(tk)
            elif isinstance(membership, dict) and tk in membership:
                _mem = membership[tk]
            else:
                _mem = _sm_act.classify(tk, held=True)
            _gate = _sm_act.may_activate_obligation(_mem)
        except Exception as _mex:                                       # noqa: BLE001
            _gate = {"allowed": False, "state": "UNKNOWN_DISABLED",
                     "why": ("sleeve_membership unavailable (%s) - the admission question could "
                             "not be asked, which BLOCKS (R4.3)" % _mex)}
        if not _gate["allowed"]:
            outcomes.append(dict(rec, outcome="BLOCKED_NOT_ADMITTED",
                                 membership=_gate.get("state"), why=_gate["why"]))
            continue
        amt = e.get("executed_amount_gbp")
        if e.get("execution_source") != "transaction_record" or amt is None or not did:
            outcomes.append(dict(rec, outcome="UNVERIFIED_EXECUTION",
                                 why="execution not from the broker dealing record with an amount"))
            continue
        exec_ref = e.get("executed_reference") or ("DERIVED:%s|%s|%s" % (
            e.get("executed_date"), e.get("executed_quantity"), amt))
        oid = "OBL-" + hashlib.sha256(("%s|%s" % (did, exec_ref)).encode("utf-8")).hexdigest()[:16]
        label = month_label_of(e.get("date"))
        plan = plan_loader(label)
        if not plan:
            outcomes.append(dict(rec, outcome="NO_AUTHORISED_PLAN", month=label))
            continue
        if str(plan.get("as_of") or "9999")[:10] > str(e.get("date"))[:10]:
            outcomes.append(dict(rec, outcome="PLAN_NOT_CONTEMPORANEOUS", artifact=plan.get("artifact"),
                                 plan_as_of=plan.get("as_of"),
                                 why="the plan on disk post-dates the decision; it cannot be the authorised plan"))
            continue
        al = plan.get("allocation") or {}
        existing = [o for o in obls if o.get("ticker") == tk and obligation_binding(o)["binding"]]
        if existing and oid not in by_id:
            row = next((r for r in al.get("rows") or [] if r.get("ticker") == tk), {})
            ob = existing[0]
            ev = {"on": today, "decision_id": did, "execution_reference": exec_ref,
                  "executed_gbp": amt, "plan_state": row.get("state"), "artifact": plan.get("artifact")}
            ob.setdefault("fill_events", []).append(ev)
            if row.get("state") == "OBLIGATION_FILLED":
                ob.setdefault("lifecycle", []).append({"on": today, "to": "FULFILLED", "evidence": ev})
                ob["state"], ob["fulfilled_on"] = "FULFILLED", e.get("executed_date")
                outcomes.append(dict(rec, outcome="FULFILLED", obligation_id=ob.get("obligation_id")))
            else:
                outcomes.append(dict(rec, outcome="PARTIAL_FILL_RECORDED", obligation_id=ob.get("obligation_id")))
            by_id[oid] = ob
            changed = True
            continue
        if oid in by_id:
            outcomes.append(dict(rec, outcome="IDEMPOTENT_SKIP", obligation_id=oid))
            continue
        prop = next((x for x in al.get("proposed_obligations") or [] if x.get("ticker") == tk), None)
        if not prop:
            outcomes.append(dict(rec, outcome="NO_PLANNED_SHORTFALL", artifact=plan.get("artifact")))
            continue
        target = float(prop.get("target_gbp") or 0.0)
        shortfall = round(target - float(amt), 2)
        if shortfall <= 0.005:
            outcomes.append(dict(rec, outcome="FILLED_AT_EXECUTION", target_gbp=target, executed_gbp=amt))
            continue
        if prop.get("is_new") and float(amt) < float(prop.get("min_entry_gbp") or 0.0):
            outcomes.append(dict(rec, outcome="EXECUTION_DEVIATION_BELOW_MIN_ENTRY", executed_gbp=amt,
                                 min_entry_gbp=prop.get("min_entry_gbp"),
                                 why=("broker execution left a NEW position below the minimum meaningful "
                                      "entry - named for review; no automatic first claim is created")))
            continue
        row = {"obligation_id": oid, "ticker": tk, "kind": prop.get("kind"), "state": "ACTIVE",
               "source_decision_id": did, "route": e.get("route"),
               "source_allocation_artifact": plan.get("artifact"), "source_allocation_sha256": plan.get("sha256"),
               "source_allocation_as_of": plan.get("as_of"),
               "execution_reference": exec_ref, "executed_date": e.get("executed_date"),
               "executed_quantity": e.get("executed_quantity"), "executed_gbp": amt,
               "opened_on": e.get("executed_date"),
               "target_rung": prop.get("target_rung"), "target_pct": prop.get("target_pct"),
               "target_gbp_at_authorisation": target,
               "authorised_allocation_gbp": prop.get("authorised_allocation_gbp"),
               "shortfall_at_activation_gbp": shortfall, "obligation_gbp": shortfall,
               "evidence_state_at_entry": prop.get("evidence_state"),
               # ISA-0752: a plan written before the use row carried evidence says so, typed
               "evidence_state_at_entry_missing_reason": (None if prop.get("evidence_state") is not None
                                                          else "PLAN_PREDATES_ISA_0752: the authorised plan's "
                                                               "proposal carried no evidence_state"),
               "conditional": bool(prop.get("conditional")), "condition": prop.get("condition"),
               "voided": False, "voided_reason": None,
               "lifecycle": [{"on": today, "to": "ACTIVE", "by": "position_sizing.activate_from_executions"}],
               "basis": ("ISA-0701: ACTIVE only after an authorised decision matched to broker execution. "
                         "`obligation_gbp` is evidence at activation; the next fill uses the CURRENT "
                         "qualifying gap. D17 first claim; RAJ D24 clock rule unchanged.")}
        obls.append(row)
        by_id[oid] = row
        changed = True
        outcomes.append(dict(rec, outcome="ACTIVATED", obligation_id=oid, shortfall_gbp=shortfall))
    # ⚑ Report each (decision, outcome) ONCE: historic executions are re-evaluated every run (a blocked
    #   VCI row can become activatable when ISA-0686 closes) but only a NEW or CHANGED outcome is
    #   escalated, so the review is not flooded by the same July row each month.
    alog = doc.setdefault("activation_log", {})
    fresh = []
    for o in outcomes:
        assert o["outcome"] in ACTIVATION_OUTCOMES, o
        key = str(o.get("decision_id"))
        prev = alog.get(key)
        if prev is None or prev.get("outcome") != o["outcome"]:
            alog[key] = {"outcome": o["outcome"], "first_seen": today, "ticker": o.get("ticker")}
            changed = True
            fresh.append(o)
    written = save_fill_obligations(doc, path) if (own and changed and not dry_run) else None
    return {"store": written, "dry_run": bool(dry_run), "changed": changed, "outcomes": outcomes,
            "activated": [o["ticker"] for o in outcomes if o["outcome"] == "ACTIVATED"],
            "fulfilled": [o["ticker"] for o in outcomes if o["outcome"] == "FULFILLED"],
            "new_or_changed": [o.get("decision_id") for o in fresh],
            "review_required": [o for o in fresh if o["outcome"] in (
                "EXECUTION_DEVIATION_BELOW_MIN_ENTRY", "PLAN_NOT_CONTEMPORANEOUS", "BLOCKED_ON_ISA-0686",
                "UNVERIFIED_EXECUTION", "NO_AUTHORISED_PLAN")],
            "doc": doc}


def _declared_evidence_states() -> set:
    """The declared evidence-state vocabulary, read from its one home (evidence_state.STATE_TO_RUNG)."""
    import evidence_state as _es
    return set(_es.STATE_TO_RUNG)


def allocate(qualifying_uses: List[dict], *, capital_gbp: float, nav_gbp: float,
             ranking_basis: str, policy=None, obligations=None,
             sequencer_order: Optional[List[str]] = None, today=None,
             concentration=None, replacement_only=None, donor_releases=None,
             membership=None, underwriting=None) -> dict:
    """Floor-then-priority fill. Returns the per-name allocation and the residual.

    `concentration` (ISA-0465): optional callable (ticker, gbp, is_new, admitted) -> gate dict from
    concentration_control.gate, consulted SEQUENTIALLY so candidate 2 sees candidate 1. A refused
    name is skipped (its pound stays in the queue for later destinations, then funds); a capped
    name receives only its headroom. None = no concentration gate (OFF/SHADOW authoritative pass).

    ⚑⚑ ISA-0705 (Wave 2 §5.3) — `replacement_only` IS A FUNDING VERDICT, NOT A SORT KEY.
    Before this, a name the sequencer classified REPLACEMENT_ONLY was simply absent from
    `sequencer_order`, and `pos.get(ticker, 10_000)` sorted it LAST — **and then funded it in
    full as an ADDITION**. Measured on the aug-2026 fixture book: a candidate at rho 0.92
    against a held name received GBP 6,794.48 alongside an ordered name. That makes the
    correlation admission gate decision-ineffective on capital and contradicts A2.1 ("you do
    not own both; you own the better one").

    `REPLACEMENT_ONLY` now means **NO NET INCREMENTAL CAPITAL**. Such a name may receive money
    only through a documented paired donor release in the SAME authorisation chain:
      `donor_releases = {ticker: {donor, released_gbp, state, provenance}}` with state in
      REALISED / AUTHORISED (PROPOSED is not fundable — a donor leg that has not been
      authorised cannot pay for a buy).
    The allocation is capped at the donor release, is drawn from the DONOR and never from
    `capital_gbp`, and carries `net_incremental_gbp = 0`. With no admissible donor the name is
    REFUSED at GBP 0 and its pound stays in the queue for the next admissible destination —
    which is what makes an all-excluded run a measured zero spend re-offered to funds, rather
    than a REFUSED hold-back that reaches no destination at all (ISA-0563).

    ⚑⚑ THE £9,000 CASE IS RAJ'S OWN WORKED EXAMPLE AND IT DOES **NOT** GIVE £5k + £4k.
    Floor-then-priority fills #1 COMPLETELY first, so the residual is £3,528.76 — below the
    £4,377 floor — and ONE position opens. Recorded here, in the code that does it, so the
    outcome is not a surprise on 06-Sep.
    """
    _fi_mark("position_sizing", "allocate")
    policy = policy if policy is not None else load_policy()
    if not ranking_basis:
        raise SizingRefused(
            "allocate() requires a declared `ranking_basis`. An order with no declared key is "
            "an order by accident, and §2 could not state what produced it.")
    me = min_entry_gbp(nav_gbp, policy)
    floor = me["min_entry_gbp"]
    # ⚑ ISA-0669 — WHO OWNS PERSISTENCE. `allocate()` has always BUILT obligation rows into
    #   `doc` and never SAVED it, which is why `save_fill_obligations` carried zero call sites
    #   and no obligation has ever been recorded. D17 says a sub-STARTER entry carries a FIRST
    #   CLAIM on the next tranche; an obligation that is never written has no claim on
    #   anything. R4.11: capture is a property of PRODUCING the artefact, not a prose step —
    #   so the save happens here, inside the function that creates the rows, where it cannot
    #   be dropped by a caller.
    #   Ownership rule: if the caller SUPPLIED the doc it owns persistence; if `allocate`
    #   loaded it, `allocate` saves it. That keeps tests and dry runs able to pass a doc in
    #   and get no side effect.
    # ⚑⚑ ISA-0701 SUPERSEDES THE OWNERSHIP RULE ABOVE: allocate() NEVER writes the store and never
    #   mutates a caller's doc. A proposal is not an execution; activation is owned by
    #   activate_from_executions() at the execution-reconciliation boundary (pre-run Step 1.5).
    import copy as _copy
    doc = _copy.deepcopy(obligations if obligations is not None else load_fill_obligations())
    proposed, block_events, non_binding = [], [], []
    proposal_refusals = []                 # ISA-0752: named, never silent

    def _propose(p):
        """ISA-0752. A fill-obligation PROPOSAL carries the declared evidence_state it was sized on.
        Missing or undeclared evidence is REFUSED AS AN OBLIGATION (named in
        `obligation_proposals_refused`); the allocation row itself is unchanged - this gate decides
        only whether a first claim may be proposed, never how much is bought."""
        _ev = p.get("evidence_state")
        if _ev is None or _ev not in _declared_evidence_states():
            proposal_refusals.append({"ticker": p.get("ticker"), "kind": p.get("kind"),
                                      "evidence_state": _ev,
                                      "reason": ("EVIDENCE_STATE_MISSING_OR_UNDECLARED - a fill "
                                                 "obligation may not be proposed on an evidence state "
                                                 "the canonical producer did not supply (ISA-0752)")})
            return
        proposed.append(p)

    # ── ISA-0705 — partition BEFORE the queue is built. A replacement-only name never
    #    competes for `capital_gbp` at all, so no ordering accident can fund it.
    repl = {str(t).upper() for t in (replacement_only or [])}
    donors = {str(k).upper(): dict(v) for k, v in (donor_releases or {}).items()}
    uses = [u for u in qualifying_uses if str(u.get("ticker", "")).upper() not in repl]
    repl_uses = [u for u in qualifying_uses if str(u.get("ticker", "")).upper() in repl]
    for u in uses:
        if u.get(ranking_basis) is None and sequencer_order is None:
            raise SizingRefused(
                "%s carries no %r and no sequencer order was supplied — there is nothing to "
                "order the queue BY (P4.3 step 1)." % (u.get("ticker"), ranking_basis))
    if sequencer_order:
        pos = {t: i for i, t in enumerate(sequencer_order)}
        uses.sort(key=lambda u: pos.get(u["ticker"], 10_000))
        order_basis = "deployment_sequencer"
    else:
        uses.sort(key=lambda u: -(u.get(ranking_basis) or 0.0))
        order_basis = ranking_basis

    # ⚑ D17 — UNDERFILLED OBLIGATIONS TAKE THE HEAD OF THE ORDER, ahead of ANY new position.
    live_obl = {}
    for o in doc.get("obligations", []):
        _b = obligation_binding(o)
        if _b["binding"]:
            live_obl[o["ticker"]] = o
        elif not (o.get("voided") or o.get("state") in ("VOIDED", "FULFILLED")):
            non_binding.append({"ticker": o.get("ticker"), "reason": _b["reason"]})
    # ⚑ ISA-0701: a claim needs a position. An ACTIVE row for a name not currently held carries no
    #   head priority and does not waive the new-entry floor (published, not silently honoured).
    _held_now = {u["ticker"] for u in uses if float(u.get("current_value_gbp") or 0.0) > 0.0}
    for _t in [t for t in live_obl if t not in _held_now and t in {u["ticker"] for u in uses}]:
        non_binding.append({"ticker": _t, "reason": "ACTIVE obligation but position not held"})
        live_obl.pop(_t)
    # ⚑ ISA-0685 (20-Sep-2026) — HELD IS NOT ADMITTED. A first claim is new-capital PRIORITY,
    #   and priority belongs to a name the framework DECIDED to admit. "Held + in the registry"
    #   used to be the whole membership test, so a legacy or outside-the-framework position
    #   could hold the head of the queue on the strength of being owned. The verdict is read
    #   from the canonical decision ledger, never inferred from `held` — that inference IS the
    #   defect. A name with no current admitting decision keeps its position, its risk count
    #   and its place in the report; it loses only the claim it was never granted.
    #   `membership=` injects the resolver the way `obligations=` injects the store: a
    #   callable (ticker) -> verdict, or a {ticker: verdict} map. Omitted, it reads the live
    #   canonical ledger. A fixture that supplies one is STATING the admission it assumes,
    #   which is stronger than a fixture that silently inherits whatever the live book says.
    def _resolve_membership(t):
        if callable(membership):
            return membership(t)
        if isinstance(membership, dict) and t in membership:
            return membership[t]
        import sleeve_membership as _sm
        return _sm.classify(t, held=True)

    _memb = {}
    for _t in list(live_obl):
        try:
            _m = _resolve_membership(_t)
        except Exception as _me:                                        # noqa: BLE001
            _m = {"state": "UNKNOWN_DISABLED", "may_hold_new_capital_priority": False,
                  "why": "sleeve_membership unavailable (%s) - UNKNOWN blocks (R4.3)" % _me}
        _memb[_t] = _m
        if not _m.get("may_hold_new_capital_priority"):
            non_binding.append({"ticker": _t, "membership": _m.get("state"),
                                "reason": "ISA-0685: %s - %s" % (_m.get("state"), _m.get("why"))})
            live_obl.pop(_t)
    head = [u for u in uses if u["ticker"] in live_obl]
    tail = [u for u in uses if u["ticker"] not in live_obl]
    uses = head + tail

    # ⚑⚑ RAJ DECISION 23-Sep-2026 (ISA-0721) — A D17 OBLIGATION IS NOT UNCONDITIONAL FUTURE
    #   CAPITAL AUTHORITY. The obligation and its history are preserved, but every future fill or
    #   top-up of a HELD name is FRESH positive capital and must consume a valid CURRENT canonical
    #   underwriting case (this month's, from underwriting_cases.jsonl). A missing / invalid /
    #   stale / non-admissible E[r] means NO FILL FOR THIS TRANCHE: the pound stays in the queue
    #   for the next admissible destination. It never manufactures a SELL, never voids or deletes
    #   the obligation, never touches the holding, and never fabricates an E[r].
    #   `underwriting=` injects the resolver the way `membership=` does: callable(ticker) -> case
    #   dict, or {ticker: case}. Omitted, it reads THIS month's case from the live store.
    _uw_month = (today or datetime.date.today().isoformat())[:7]

    def _resolve_case(t):
        if callable(underwriting):
            return underwriting(t)
        if isinstance(underwriting, dict):
            return underwriting.get(t)
        import underwriting as _uw_ps
        return _uw_ps.latest_case(t, month=_uw_month)

    # RAJ D23 (ISA-0544) — the ISA cash reserve, DECLARED in target_weights, never typed here.
    reserve = ((policy.get("stock_sleeve") or {}).get("cash_reserve_gbp"))
    if reserve is None:
        raise SizingRefused(
            "`target_weights.stock_sleeve.cash_reserve_gbp` is NOT DECLARED. Raj declared GBP 250 "
            "on 02-Sep-2026 (D23) as the SINGLE control on residual deployment. A missing reserve "
            "is refused rather than defaulted to zero, because zero is a decision to deploy every "
            "last pound and nobody made it (R4.1, R14.2).")
    reserve = float(reserve)
    reserve_binding = False
    remaining = float(capital_gbp)
    rows, opened = [], []
    stopped_reason = None
    skipped = []          # names the queue passed OVER without exhausting capital (ISA-0563)
    for u in uses:
        tk = u["ticker"]
        gap = float(u.get("gbp") or 0.0)
        is_new = float(u.get("current_value_gbp") or 0.0) <= 0.0          # ISA-0701: held truth only
        if not is_new and remaining > 0 and gap > 0:
            try:
                _case = _resolve_case(tk)
            except Exception as _ce:                                    # noqa: BLE001
                _case = {"er": {"state": "UNKNOWN (%s)" % type(_ce).__name__}}
            if not (_case or {}).get("admissible_for_positive_size"):
                _st = ((_case or {}).get("er") or {}).get("state") or "NO_CURRENT_CASE"
                _obl = tk in live_obl
                rows.append({"ticker": tk, "allocated_gbp": 0.0,
                             "state": ("OBLIGATION_FILL_BLOCKED_NO_ADMISSIBLE_ER" if _obl
                                       else "TOPUP_BLOCKED_NO_ADMISSIBLE_ER"),
                             "gap_gbp": round(gap, 2),
                             "underwriting_case_id": (_case or {}).get("case_id"),
                             "er_state": _st,
                             "skip_reason": ("ISA-0721 / Raj 23-Sep-2026: a fill or top-up is fresh "
                                             "positive capital and needs an admissible CURRENT "
                                             "underwriting case; %s has %s. No fill this tranche - "
                                             "the obligation is retained, nothing is sold, the "
                                             "capital stays in the queue." % (tk, _st))})
                if _obl:
                    block_events.append({"obligation_id": live_obl[tk].get("obligation_id"),
                                         "ticker": tk,
                                         "blocked_on": today or datetime.date.today().isoformat(),
                                         "verdict": "NO_ADMISSIBLE_ER", "er_state": _st,
                                         "released_gbp": 0.0})
                skipped.append(tk)
                continue
        if remaining <= 0:
            rows.append({"ticker": tk, "allocated_gbp": 0.0, "state": "NO_CAPITAL",
                         "gap_gbp": round(gap, 2)})
            continue
        if remaining >= gap:
            alloc, state = gap, ("FULL" if tk not in live_obl else "OBLIGATION_FILLED")
        elif tk in live_obl:
            # topping an EXISTING underfilled position is the obligation being served
            alloc, state = remaining, "OBLIGATION_PARTIAL"
        elif is_new and not _partial_starter_entry():
            # ⚑ P4.7 ROLLBACK: whole STARTERs only. A name that cannot be filled to its full
            # rung does not open, and the remainder is residual. This is the pre-D15 behaviour
            # exactly — it is NOT a smaller partial fill, because a rollback that allocates a
            # DIFFERENT amount is a second sizing rule.
            # ⚑ SKIP THIS NAME, DO NOT ABANDON THE QUEUE (ISA-0563, 02-Sep-2026). See the
            # note on the entry-floor branch below: the rule is about THIS name, not about
            # every destination behind it.
            rows.append({"ticker": tk, "allocated_gbp": 0.0,
                         "state": "WHOLE_RUNGS_ONLY", "gap_gbp": round(gap, 2),
                         "skip_reason": ("partial_starter_entry is OFF: only whole rungs open, "
                                         "and GBP %.2f is short of the GBP %.2f gap"
                                         % (remaining, gap))})
            skipped.append(tk)
            continue
        elif is_new:
            # ⚑ A NEW POSITION IS GOVERNED BY THE ENTRY FLOOR AND BY NOTHING ELSE. The first
            # version of this branch fell through to the TOP-UP path when a new name could not
            # clear the floor, and so RAISED `SizingRefused` on the missing `min_topup_gbp` —
            # on the live £11,250 case, where the correct answer is simply "£307.52 residual
            # to funds". A refusal in the wrong branch is not a safe default: it would have
            # stopped the September run on a question that case never asks.
            if remaining >= floor:
                alloc, state = remaining, "UNDERFILLED"
            else:
                # ⚑ SKIP THIS NAME, DO NOT ABANDON THE QUEUE (ISA-0563, 02-Sep-2026). This
                # was a `break`, which was correct while the queue held ONLY new entries —
                # they are all equally unaffordable, so stopping cost nothing and the residual
                # went to funds. The moment held TOP-UPS joined the queue it became wrong:
                # D16's own words are "a NEW position may not open below it", and applying a
                # new-position floor to every destination BEHIND the new position is scope
                # leakage (Class C). Measured on the September book: the queue stopped at HRMY
                # (a new entry needing GBP 5,116.63) while COCO and MU sat behind it with
                # top-up gaps of GBP 3,902.03 and GBP 1,626.39 that the capital COULD fill.
                # The name does not open — that rule is untouched. The queue continues.
                rows.append({"ticker": tk, "allocated_gbp": 0.0,
                             "state": "BELOW_ENTRY_FLOOR", "gap_gbp": round(gap, 2),
                             "skip_reason": ("remaining GBP %.2f is below the entry floor GBP "
                                             "%.2f — a NEW position may not open below it "
                                             "(D16). Skipped; later destinations still "
                                             "considered." % (remaining, floor))})
                skipped.append(tk)
                continue
        else:
            # ══════════════════════════════════════════════════════════════════════════════
            # RAJ D23 (02-Sep-2026) — ISA-0544. THE GBP 250 CASH RESERVE IS THE SINGLE CONTROL.
            # ══════════════════════════════════════════════════════════════════════════════
            # ⚑ The build previously REFUSED here on an absent `min_topup_gbp`, a PER-TICKET
            # floor. Raj's rule is an ACCOUNT-LEVEL RESERVE: "leave GBP 250 of cash in the ISA
            # account... if there is GBP 1,000 left after those positions then there is GBP 750
            # to top up funds or stocks." Those are different shapes, and shipping both would
            # put TWO controls on one quantity (R4.4, FC-D). The reserve binds; the per-ticket
            # floor is RETIRED, not given a number — and `min_topup_gbp` is asserted ABSENT by
            # `consistency_check.pair_cash_reserve_is_single_topup_control()` so a future
            # session cannot quietly reintroduce the second control (ISA-0442's lesson).
            avail = round(remaining - reserve, 2)
            if avail > 0:
                alloc, state = avail, "TOPUP"
                reserve_binding = True
            else:
                stopped_reason = ("remaining GBP %.2f is at or below the declared GBP %.2f ISA "
                                  "cash reserve (D23), so no top-up is made"
                                  % (remaining, reserve))
                rows.append({"ticker": tk, "allocated_gbp": 0.0, "state": "HELD_AS_RESERVE",
                             "gap_gbp": round(gap, 2)})
                break
        _cg = None
        if concentration is not None:
            _cg = concentration(tk, alloc, is_new, {r["ticker"]: r["allocated_gbp"]
                                                   for r in rows if r.get("allocated_gbp")})
            if _cg.get("allowed_gbp", 0.0) <= 0.0:
                # ⚑ RAJ A1.3 (16-Sep-2026): a blocked fill RELEASES its capital to the next
                # admissible destination in THIS run — it never retains a first claim it cannot use.
                _blocked_obl = tk in live_obl
                rows.append({"ticker": tk, "allocated_gbp": 0.0,
                             "state": ("CONDITIONAL_FILL_BLOCKED" if _blocked_obl else _cg["verdict"]),
                             "gap_gbp": round(gap, 2), "released_gbp": round(alloc, 2),
                             "concentration": _cg,
                             "skip_reason": ("ISA-0465 concentration gate: %s — GBP %.2f released to "
                                             "the next admissible destination"
                                             % (_cg["verdict"], alloc))})
                if _blocked_obl:
                    # ISA-0701: reported, never written back from a proposal run
                    block_events.append({"obligation_id": live_obl[tk].get("obligation_id"), "ticker": tk,
                                         "blocked_on": today or datetime.date.today().isoformat(),
                                         "verdict": _cg["verdict"], "released_gbp": round(alloc, 2)})
                skipped.append(tk)
                continue
            if _cg.get("verdict") == "CAP_CONSTRAINED_ENTRY":
                alloc, state = float(_cg["allowed_gbp"]), "CAP_CONSTRAINED_ENTRY"
            elif _cg["allowed_gbp"] + 0.005 < alloc:
                alloc, state = float(_cg["allowed_gbp"]), state + "_CONCENTRATION_CAPPED"
        remaining -= alloc
        row = {"ticker": tk, "allocated_gbp": round(alloc, 2), "state": state,
               "gap_gbp": round(gap, 2), "rung": u.get("rung"),
               "target_pct": u.get("target_pct")}
        if _cg is not None:
            row["concentration_verdict"] = _cg.get("verdict")
        if state == "CAP_CONSTRAINED_ENTRY":
            # ⚑ RAJ A1.3 — distinct from UNDERFILLED: the binding constraint is concentration
            # headroom, not capital. Its later fill is CONDITIONAL (fresh headroom + continued
            # eligibility); a blocked fill releases capital the same run (branch above).
            row["obligation_gbp"] = round(gap - alloc, 2)
            _propose({
                "ticker": tk, "state": "PROPOSED", "proposed_on": today or datetime.date.today().isoformat(),
                "authorised_allocation_gbp": round(alloc, 2), "target_rung": u.get("rung"),
                "target_pct": u.get("target_pct"), "target_gbp": round(gap, 2), "is_new": is_new,
                "min_entry_gbp": floor, "shortfall_gbp": round(gap - alloc, 2),
                "evidence_state": u.get("evidence_state"),
                "kind": "CAP_CONSTRAINED_ENTRY", "conditional": True,
                "condition": ("fill only with fresh ISA-0465 concentration headroom AND continued "
                              "eligibility (a qualifying use); when blocked, capital is released to "
                              "the next admissible destination that run"),
                "basis": ("RAJ A1.3 16-Sep-2026 (ISA-0465 R19.3 amendment); D24 clock rule unchanged. "
                          "ISA-0701: PROPOSED only - activates on confirmed execution")})
        elif state == "UNDERFILLED":
            row["obligation_gbp"] = round(gap - alloc, 2)
            _propose({
                "ticker": tk, "state": "PROPOSED", "kind": "UNDERFILLED_ENTRY",
                "proposed_on": today or datetime.date.today().isoformat(),
                "authorised_allocation_gbp": round(alloc, 2), "target_rung": u.get("rung"),
                "target_pct": u.get("target_pct"), "target_gbp": round(gap, 2), "is_new": is_new,
                "min_entry_gbp": floor, "shortfall_gbp": round(gap - alloc, 2),
                "evidence_state": u.get("evidence_state"), "conditional": False,
                "basis": ("ISA-0701: PROPOSED only - activates on confirmed execution. D17 — a sub-STARTER entry carries a FIRST CLAIM on the next "
                          "tranche, ahead of any new position. ⚑ RAJ D24 (02-Sep-2026, "
                          "ISA-0545): filling it does NOT restart the min-hold clock. A9's lot "
                          "clock is RETIRED — the framework COMPELS this fill, so a resetting "
                          "clock would let it extend Raj's own lock-in involuntarily.")})
        elif state in ("OBLIGATION_FILLED", "OBLIGATION_PARTIAL"):
            # ⚑ ISA-0543 — A9 INSTRUMENTED (13-Sep-2026). The obligation-fill branch above
            # (D17/D24, ISA-0545) has always ASSERTED that filling an obligation does not
            # restart the min-hold clock; nothing MEASURED it. `would_have_reset_clock` answers,
            # from THIS fill's own dates, whether the RETIRED A9 lot-clock rule (a fresh clock
            # starting at the fill's own date) would have read a LATER min_hold_until than
            # D24's position-level clock (anchored to the position's first entry) does. It
            # reuses `min_hold_ok()` itself — the production function — rather than
            # re-deriving the 182-day arithmetic a second time (R4.4: one home for the rule).
            _fill_date = today or datetime.date.today().isoformat()
            _entry = live_obl.get(tk, {}).get("opened_on")
            if _entry:
                _real = min_hold_ok(position_first_entry_date=_entry, today=_fill_date)
                _cf = min_hold_ok(position_first_entry_date=_fill_date, today=_fill_date)
                _real_until = datetime.date.fromisoformat(_real["min_hold_until"])
                _cf_until = datetime.date.fromisoformat(_cf["min_hold_until"])
                _extend_days = (_cf_until - _real_until).days
                row["position_first_entry_date"] = _entry
                row["would_have_reset_clock"] = _extend_days > 0
                row["would_have_reset_clock_days"] = _extend_days
                row["would_have_reset_clock_basis"] = (
                    ("A9 (RETIRED, RAJ D24/ISA-0545): a lot clock anchored to THIS fill's own "
                     "date (%s) would read min_hold_until %s. D24's position-level clock, "
                     "anchored to the position's first entry (%s), reads %s. %s Instrumented "
                     "for ISA-0543.") % (
                        _fill_date, _cf["min_hold_until"], _entry, _real["min_hold_until"],
                        ("The retired A9 rule would have extended the lock-in by %d day(s)."
                         % _extend_days) if _extend_days > 0 else
                        "This fill lands on the position's own first-entry date, so the two "
                        "clocks agree."))
            else:
                # ⚑ NAMED ABSENCE, not a silently-skipped field (R4.9). An obligation-fill row
                # whose live obligation record carries no `opened_on` cannot be
                # counterfactually dated — that is itself worth seeing rather than hiding as a
                # missing key.
                row["would_have_reset_clock"] = None
                row["would_have_reset_clock_basis"] = (
                    "UNMEASURED: the live obligation record for %s carries no `opened_on`, so "
                    "the D24 anchor date is unavailable this fill." % tk)
        rows.append(row)
        opened.append(tk)
    # ── ISA-0705 — the replacement-only leg, funded ONLY by a paired donor release ──────
    repl_rows = []
    for u in repl_uses:
        tk = str(u["ticker"]).upper()
        gap = float(u.get("gbp") or 0.0)
        d = donors.get(tk)
        st = str((d or {}).get("state") or "").upper()
        rel = float((d or {}).get("released_gbp") or 0.0)
        if not d:
            repl_rows.append({
                "ticker": tk, "allocated_gbp": 0.0, "state": "REFUSED_REPLACEMENT_ONLY",
                "gap_gbp": round(gap, 2), "route": "REPLACEMENT_ONLY",
                "net_incremental_gbp": 0.0, "funding_source": None,
                "skip_reason": ("ISA-0705: the correlation gate classified %s "
                                "REPLACEMENT_ONLY and NO paired donor release was supplied. "
                                "Replacement-only means no net incremental capital, so this "
                                "is GBP 0 — not a smaller addition." % tk)})
            continue
        if st not in ("REALISED", "AUTHORISED"):
            repl_rows.append({
                "ticker": tk, "allocated_gbp": 0.0, "state": "REFUSED_DONOR_NOT_EXECUTABLE",
                "gap_gbp": round(gap, 2), "route": "REPLACEMENT_ONLY",
                "net_incremental_gbp": 0.0, "donor": d.get("donor"),
                "donor_state": st or None, "funding_source": None,
                "skip_reason": ("ISA-0705: the donor leg for %s is %s. Only a REALISED or "
                                "AUTHORISED release can pay for a replacement buy; a proposed "
                                "or refused donor leg leaves the buy unauthorised, never a "
                                "standalone addition." % (tk, st or "ABSENT"))})
            continue
        alloc = round(min(gap, rel), 2)
        repl_rows.append({
            "ticker": tk, "allocated_gbp": alloc, "state": "REPLACEMENT_FILL",
            "gap_gbp": round(gap, 2), "route": "REPLACEMENT_ONLY",
            "rung": u.get("rung"), "target_pct": u.get("target_pct"),
            "donor": d.get("donor"), "donor_state": st,
            "donor_released_gbp": round(rel, 2),
            "funding_source": "DONOR_RELEASE",
            "net_incremental_gbp": 0.0,
            "donor_provenance": d.get("provenance"),
            "basis": ("ISA-0705: funded from the paired donor release (GBP %.2f from %s), "
                      "capped at the release and drawn from the DONOR, never from this run's "
                      "capital. Net incremental exposure is zero."
                      % (rel, d.get("donor")))})
    rows.extend(repl_rows)

    residual = round(max(remaining, 0.0), 2)
    # ⚑ D23: the reserve is retained from the FINAL residual too, not only from top-ups — Raj's
    # rule is "leave GBP 250 of cash in the ISA account", and money swept to a fund has left it.
    deployable_residual = round(max(residual - reserve, 0.0), 2)
    return {
        "cash_reserve_gbp": reserve,
        "deployable_residual_gbp": deployable_residual,
        "reserve_held_gbp": round(min(residual, reserve), 2),
        "reserve_binding": bool(reserve_binding or deployable_residual < residual),
        "reserve_basis": ("RAJ D23, 02-Sep-2026 (ISA-0544). The GBP 250 ISA cash reserve is the "
                          "SINGLE control on residual deployment; there is no per-ticket top-up "
                          "floor. Instrumented for ISA-0543."),
        "rows": rows, "opened": opened, "n_opened": len(opened),
        # ⚑ ISA-0705 — `allocated_gbp` is what THIS RUN'S CAPITAL bought. A replacement fill is
        #   paid for by its donor, so it is deliberately NOT in this figure: including it would
        #   understate what is re-offered to funds and would re-create the net-new exposure the
        #   route exists to prevent. The replacement leg is published separately, in full.
        "allocated_gbp": round(float(capital_gbp) - residual, 2),
        "replacement_only": {
            "n_candidates": len(repl_uses),
            "rows": repl_rows,
            "funded_gbp": round(sum(r["allocated_gbp"] for r in repl_rows), 2),
            "net_incremental_gbp": 0.0,
            "refused": [r["ticker"] for r in repl_rows if r["allocated_gbp"] == 0.0],
            "basis": ("ISA-0705 / Wave 2 §5.3: REPLACEMENT_ONLY means no net incremental "
                      "capital. Funded only from a paired REALISED/AUTHORISED donor release, "
                      "capped at that release, drawn from the donor and never from this run's "
                      "capital. Absent or non-executable donor leg -> GBP 0, never a "
                      "standalone addition.")},
        "residual_gbp": residual,
        "min_entry_gbp": floor, "starter_gbp": me["starter_gbp"],
        "order_basis": order_basis, "ranking_basis": ranking_basis,
        "order": [u["ticker"] for u in uses],
        "obligations_at_head": sorted(live_obl),
        # ISA-0685: the membership verdict behind every claim that reached the head, and every
        # one that did not — so a reader can see WHY a holding held priority or lost it.
        "membership_at_head": {t: {"state": m.get("state"), "why": m.get("why")}
                               for t, m in _memb.items()},
        "obligations_persisted_to": None,          # ISA-0701: a proposal never writes the store
        "obligations_store_mutated": False,
        "proposed_obligations": proposed,
        "obligation_proposals_refused": proposal_refusals,
        "obligation_block_events": block_events,
        "obligations_non_binding": non_binding,
        "obligations_open": sorted(live_obl),
        "stopped_reason": stopped_reason,
        # ⚑ SKIPPED IS NOT STOPPED (ISA-0563). A name the queue passed over is a different
        # fact from the queue ending, and collapsing them is how "we could not afford HRMY"
        # became "no capital was deployed".
        "skipped": skipped,
        "obligations": doc,
        "residual_note": ("the residual returns to capital_destination for existing fund / MMF "
                          "routing. NO new routing logic — the B2 CSH2.L sweep already owns it."),
        "basis": me["basis"],
    }


def min_hold_ok(*, position_first_entry_date: Optional[str] = None, today: Optional[str] = None,
                min_hold_days: int = 182, exempt_reason: Optional[str] = None,
                at_a_loss: bool = False, entry_date: Optional[str] = None) -> dict:
    """Lot-level min-hold with the D14 exemption set.

    ⚑⚑ RAJ D24 (02-Sep-2026, ISA-0545) — `entry_date` IS THE POSITION'S FIRST ENTRY, NOT A LOT.
    A9 previously declared that a top-up starts a NEW lot clock, so topping up EXTENDED the
    minimum commitment. That was retired because it CONTRADICTED D17: a sub-STARTER entry
    carries a fill obligation with first claim on the next tranche, so the framework COMPELS the
    top-up — and a clock that resets on a compelled fill lets the framework extend Raj's own
    lock-in. C-1's purpose is THESIS horizon, and a top-up reinforces an existing thesis rather
    than starting a new one. The gaming vector a lot clock would guard against (open a token
    position to start the clock, load up later, exit at once) is closed by the D16 ENTRY FLOOR:
    a position is at 0.80 x STARTER or it does not exist. MIN_HOLD_DAYS (182) and the C-1
    anti-churn rule are UNCHANGED — only the date the clock is measured from moves.
    There is no tax consequence inside an ISA, so lot selection is governed by the clock's
    PURPOSE (anti-churn), not by lot order.
    ⚑ And inside min-hold a position IN PROFIT may be trimmed; a position AT A LOSS may not."""
    _fi_mark("position_sizing", "min_hold_ok")   # ISA-0699: execution-ledger observation
    # ⚑ D24 (ISA-0545). `entry_date` was the LOT date. Its meaning changed, so the OLD NAME
    # RAISES rather than quietly carrying the new semantics — a contract change must fail an
    # un-updated caller, never default (R4.7). This is the FC-B rename discipline: when a value
    # starts meaning something else, the name changes with it.
    if entry_date is not None and position_first_entry_date is None:
        raise SizingRefused(
            "min_hold_ok(entry_date=...) is RETIRED. Raj D24 (02-Sep-2026, ISA-0545): the "
            "182-day clock attaches to the POSITION at its FIRST entry, not to a lot, so a "
            "top-up or obligation fill does not reset it. Pass "
            "`position_first_entry_date=` with the date the POSITION was opened. Passing the "
            "lot date here would silently restore the retired A9 rule.")
    if not position_first_entry_date:
        raise SizingRefused("min_hold_ok requires `position_first_entry_date` (D24, ISA-0545).")
    t = datetime.date.fromisoformat(today or datetime.date.today().isoformat())
    e = datetime.date.fromisoformat(position_first_entry_date)
    held = (t - e).days
    until = (e + datetime.timedelta(days=min_hold_days)).isoformat()
    if exempt_reason:
        if exempt_reason not in MIN_HOLD_EXEMPT:
            raise SizingRefused(f"{exempt_reason!r} is not a declared min-hold exemption. "
                                f"Declared: {list(MIN_HOLD_EXEMPT)}.")
        return {"ok": True, "days_held": held, "min_hold_until": until,
                "basis": f"EXEMPT: {exempt_reason}"}
    if held >= min_hold_days:
        return {"ok": True, "days_held": held, "min_hold_until": until,
                "basis": f"{held}d held >= {min_hold_days}d"}
    if at_a_loss:
        return {"ok": False, "days_held": held, "min_hold_until": until,
                "basis": (f"inside min-hold ({held}/{min_hold_days}d) AND at a loss — a losing "
                          f"position may not be trimmed inside the clock")}
    return {"ok": True, "trim_only": True, "days_held": held, "min_hold_until": until,
            "basis": (f"inside min-hold ({held}/{min_hold_days}d) but IN PROFIT — a trim is "
                      f"permitted, a full exit is not")}


def _selftest_admit_all(t):
    """Selftest-only resolver: every held name has an admissible CURRENT case, so the
    pre-existing obligation/top-up fixtures keep testing what they were written to test.
    The D17 underwriting gate itself is exercised separately with explicit resolvers."""
    return {"case_id": "UWC-SELFTEST-%s" % t, "er": {"state": "VALID_MECHANICAL"},
            "admissible_for_positive_size": True}


def _selftest_allocate(*a, **k):
    k.setdefault("underwriting", _selftest_admit_all)
    return allocate(*a, **k)


def _selftest_isa0548(verbose: bool = True) -> int:
    """ISA-0548 build — negative controls for the held-binary budget, the successor re-arm and
    the obligation lifecycle. liveness_ref: position_sizing._selftest_isa0548"""
    allocate = _selftest_allocate    # D17 gate: fixtures carry admissible cases
    import json as _json
    import tempfile as _tf
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    # ⚑ NEGATIVE CONTROL — absence is REFUSED, not read as not-a-binary (V-1/ISA-0646).
    r = held_binary_rows([{"ticker": "NOT_DECLARED_ANYWHERE", "size_pct": 1.0}], declared={})
    ok(r["refusals"] and r["refusals"][0]["control"] == "is_binary",
       "⚑ NEGATIVE CONTROL: a HELD stock absent from the registry MUST be refused by name. "
       "`false` is a declaration someone made; absence means nobody has, and they must not "
       "produce the same commitment of 0.0")
    ok(not r["rows"], "...and it must NOT appear as a priced binary row")

    # ⚑ NEGATIVE CONTROL — a PENDING binary with no p/L must WITHHOLD, never compute over the
    #   priceable remainder (ISA-0654).
    b = budget_available_reported(
        [{"ticker": "A", "size_pct": 1.0, "is_binary": True, "catalyst_status": PENDING,
          "p_thesis": 0.5, "L": 0.35},
         {"ticker": "B", "size_pct": 1.0, "is_binary": True, "catalyst_status": PENDING}],
        budget_pct=1.5, max_concurrent=2)
    ok(b["committed_pct"] is None and b["blocks_capital"] is True,
       "⚑ NEGATIVE CONTROL: one unpriceable binary MUST withhold the whole figure. A sum over "
       "the priceable remainder understates the book and would license a deployment the "
       "refused name may forbid: %r" % b["committed_pct"])
    ok(any(x["ticker"] == "B" for x in b["refused_unpriceable"]),
       "...and the unpriceable name must be NAMED, not counted")

    # positive control: fully specified, the figure is measured (R5.8 — test the test)
    b2 = budget_available_reported(
        [{"ticker": "A", "size_pct": 1.0, "is_binary": True, "catalyst_status": PENDING,
          "p_thesis": 0.5, "L": 0.35}], budget_pct=1.5, max_concurrent=2)
    ok(b2["committed_pct"] is not None and b2["blocks_capital"] is False,
       "positive control: a fully specified binary yields a measured committed figure")

    # ⚑ NEGATIVE CONTROL — an UNPRICED successor must RE-ARM, never release (ISA-0655).
    c = binary_commitment({"ticker": "S", "size_pct": 1.0, "is_binary": True,
                           "asset_structure": "platform",
                           "catalyst_status": RESOLVED_POSITIVE_SUCCESSOR_PENDING,
                           "successor": {"priceable": False}})
    ok(c["commits_budget"] is True and c["commitment_pct"] > 0,
       "⚑ NEGATIVE CONTROL: an unpriceable successor MUST still commit budget. Releasing it "
       "reads a null as a zero — the V-1 defect that flipped DENY to ADMIT on QBTS — and the "
       "error would run TOWARD the risk: %r" % c)
    ok(c["successor_state"] == SUCCESSOR_UNPRICED, c)
    # and a genuinely resolved, successor-free name MUST release (or the release never happens)
    c2 = binary_commitment({"ticker": "R", "size_pct": 1.0, "is_binary": True,
                            "catalyst_status": RESOLVED_POSITIVE})
    ok(c2["commits_budget"] is False,
       "positive control: a resolved binary with no successor releases its commitment the "
       "same run (ISA-0424), or the budget would be reserved against nothing forever")

    # ⚑ NEGATIVE CONTROL — an undeclared asset_structure must RAISE, not fall back.
    try:
        adverse_prior(None)
        ok(False, "⚑ adverse_prior(None) must RAISE — an unpriceable successor on an unknown "
                  "structure has no adverse case to fall back to either")
    except SizingRefused:
        ok(True, "")

    # ⚑ NEGATIVE CONTROL — a dry run must not write the obligation store.
    d = _tf.mkdtemp()
    import os as _os
    st = _os.path.join(d, "ob.json")
    save_fill_obligations({"obligations": []}, st)
    m0 = _os.path.getmtime(st)
    refresh_obligations({}, path=st, dry_run=True)
    ok(_os.path.getmtime(st) == m0,
       "⚑ NEGATIVE CONTROL: dry_run=True must NOT persist the obligation store — a rehearsal "
       "that mutates the thing it rehearses cannot be used (R18.1)")
    # ── ISA-0687 — a DECLARED catalyst_type prices the position from its own class,
    #    and an undeclared or unknown one REFUSES rather than falling through to the
    #    structure default. The refusal text for ISA-0653 promised this behaviour for a
    #    day before anything implemented it, so the controls below are what make the
    #    promise checkable.
    _q = {"ticker": "QBTS_T", "is_binary": True, "catalyst_status": PENDING,
          "asset_structure": "platform", "catalyst_type": "revenue_ramp",
          "catalyst_domain": "sovereign_capital", "size_pct": 0.75}
    _r = binary_commitment(_q)
    ok(_r["prior_basis"] == "platform/revenue_ramp",
       "ISA-0687: a declared catalyst_type prices from the catalyst's own class")
    ok(abs(_r["commitment_pct"] - 0.75 * (1 - 0.55) * 0.35) < 1e-9,
       "ISA-0687: the class prior is APPLIED, not merely reported")
    ok(_r["p_thesis"] == 0.55 and _r["L"] == 0.35,
       "ISA-0687: p and L travel with the verdict so the basis is checkable (R2.6)")

    # NEGATIVE CONTROL — an explicit p/L on the record still wins. Without this the
    # change could have silently overridden hand-declared priors everywhere.
    _q2 = dict(_q); _q2["p_thesis"], _q2["L"] = 0.28, 0.60
    ok(binary_commitment(_q2)["prior_basis"] == "declared on the position",
       "NEGATIVE CONTROL ISA-0687: an explicit p/L on the record is NOT overridden by the class")

    # MUST FIRE — three ways the lookup must refuse rather than default.
    for _mut, _lbl in ((("catalyst_type", UNDECLARED), "undeclared catalyst_type"),
                       (("catalyst_type", "no_such_class"), "declared type with no base-rate row"),
                       (("asset_structure", None), "undeclared asset_structure")):
        _t = dict(_q); _t[_mut[0]] = _mut[1]
        _raised = False
        try:
            binary_commitment(_t)
        except SizingRefused:
            _raised = True
        ok(_raised, "MUST-FIRE ISA-0687: %s REFUSES rather than defaulting" % _lbl)

    # NEGATIVE CONTROL — the refusal is not a blanket ban: the valid case above still
    # prices. Asserted again here so deleting the must-fires cannot leave a silent pass.
    ok(binary_commitment(dict(_q))["commits_budget"] is True,
       "NEGATIVE CONTROL ISA-0687: the valid class still prices - the refusal discriminates")

    if verbose:
        print("position_sizing._selftest_isa0548: %d assertions, 0 failed" % n)
    return n


def _selftest_isa0465(verbose: bool = False) -> int:
    """ISA-0465 R19.3 A1.3 — CAP_CONSTRAINED_ENTRY and conditional fills through the REAL allocate().
    liveness_ref: position_sizing._selftest_isa0465"""
    allocate = _selftest_allocate    # D17 gate: fixtures carry admissible cases
    import concentration_control as _ccm
    n = 0
    NAV = 146_189.0
    pol = load_policy()
    floor = min_entry_gbp(NAV, pol)["min_entry_gbp"]
    tax = {"sector": {"H1": {"value": "Tech"}, "NEW1": {"value": "Tech"}, "NEW2": {"value": "Health"},
                      "OB1": {"value": "Tech"}},
           "theme": {"H1": ["AI"], "NEW1": ["AI"], "NEW2": [], "OB1": ["AI"]}}

    def hook(held):
        def fn(tk, gbp, is_new, admitted):
            b = dict(held)
            for k, v in admitted.items():
                b[k] = b.get(k, 0.0) + v
            return _ccm.gate(b, tk, gbp, nav_gbp=NAV, tax=tax, is_new=is_new, min_entry_gbp=floor)
        return fn
    sec_cap = _ccm.SECTOR_CAP_NAV * NAV
    held = {"H1": sec_cap - floor - 500.0, "ZZ": 60_000.0}
    tax["sector"]["ZZ"], tax["theme"]["ZZ"] = {"value": "Other"}, []
    # ISA-0752: fixtures carry the evidence_state the real producer (stock_max) now always supplies
    uses = [{"ticker": "NEW1", "gbp": 6000.0, "current_value_gbp": 0.0, "rung": "NORMAL", "source_score": 9,
             "evidence_state": "CONFIRMED"},
            {"ticker": "NEW2", "gbp": 5000.0, "current_value_gbp": 0.0, "rung": "STARTER", "source_score": 8,
             "evidence_state": "THIN"}]
    out = allocate(uses, capital_gbp=12_000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                   obligations={"obligations": []}, concentration=hook(held))
    r = {x["ticker"]: x for x in out["rows"]}
    assert r["NEW1"]["state"] == "CAP_CONSTRAINED_ENTRY" and abs(r["NEW1"]["allocated_gbp"] - (floor + 500.0)) < 0.02, r["NEW1"]
    n += 1
    ob = [o for o in out["proposed_obligations"] if o["ticker"] == "NEW1"]
    assert ob and ob[0]["conditional"] is True and ob[0]["kind"] == "CAP_CONSTRAINED_ENTRY" \
        and ob[0]["state"] == "PROPOSED", ob
    n += 1
    assert r["NEW2"]["allocated_gbp"] == 5000.0, "released headroom capital must reach the next destination"
    n += 1
    # ⚑ NEGATIVE CONTROL — headroom below the minimum meaningful entry: no entry, capital released
    held2 = {"H1": sec_cap - floor + 100.0, "ZZ": 60_000.0}
    out2 = allocate(uses, capital_gbp=12_000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                    obligations={"obligations": []}, concentration=hook(held2))
    r2 = {x["ticker"]: x for x in out2["rows"]}
    assert r2["NEW1"]["allocated_gbp"] == 0.0 and r2["NEW1"]["state"] == "REFUSE_SECTOR", r2["NEW1"]
    assert not [o for o in out2["proposed_obligations"] if o["ticker"] == "NEW1"]
    assert r2["NEW2"]["allocated_gbp"] == 5000.0
    n += 3
    # ⚑ MUST-FIRE — a conditional obligation with NO fresh headroom is blocked and RELEASES capital
    obl = {"obligations": [{"ticker": "OB1", "opened_on": "2026-09-01", "allocated_gbp": floor,
                            "obligation_gbp": 2000.0, "kind": "CAP_CONSTRAINED_ENTRY",
                            "conditional": True, "voided": False, "state": "ACTIVE",
                            "obligation_id": "OBL-ob1", "source_decision_id": "2026-09-01::OB1::buy",
                            "execution_reference": "REF-ob1"}]}
    held3 = {"OB1": sec_cap, "ZZ": 60_000.0}
    uses3 = [{"evidence_state": "CONFIRMED", "ticker": "OB1", "gbp": 2000.0, "current_value_gbp": sec_cap, "rung": "NORMAL", "source_score": 1},
             {"evidence_state": "CONFIRMED", "ticker": "NEW2", "gbp": 5000.0, "current_value_gbp": 0.0, "rung": "STARTER", "source_score": 8}]
    out3 = allocate(uses3, capital_gbp=5_300.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                    obligations=obl, today="2026-10-03", concentration=hook(held3), membership=_ADMITTED)
    r3 = {x["ticker"]: x for x in out3["rows"]}
    assert r3["OB1"]["state"] == "CONDITIONAL_FILL_BLOCKED" and r3["OB1"]["released_gbp"] == 2000.0, r3["OB1"]
    assert r3["NEW2"]["allocated_gbp"] == 5000.0, "the released first claim must fund the next name"
    assert out3["obligation_block_events"][0]["blocked_on"] == "2026-10-03" \
        and "last_blocked_on" not in obl["obligations"][0], "ISA-0701: block recorded in the result, never written back"
    n += 3
    # POSITIVE CONTROL — with fresh headroom the conditional obligation fills first
    held4 = {"OB1": floor, "ZZ": 60_000.0}
    obl4 = {"obligations": [dict(obl["obligations"][0], blocked_count=0)]}
    out4 = allocate(uses3, capital_gbp=7_300.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                    obligations=obl4, concentration=hook(held4), membership=_ADMITTED)
    r4 = {x["ticker"]: x for x in out4["rows"]}
    assert r4["OB1"]["allocated_gbp"] == 2000.0 and r4["OB1"]["state"] == "OBLIGATION_FILLED", r4["OB1"]
    n += 1
    if verbose:
        print("position_sizing._selftest_isa0465: %d assertions, 0 failed" % n)
    return n


def _selftest_isa0701(verbose: bool = False) -> int:
    """ISA-0701 BuildSpec §7 acceptance through the REAL allocate() / activate_from_executions().
    liveness_ref: position_sizing._selftest_isa0701"""
    allocate = _selftest_allocate    # D17 gate: fixtures carry admissible cases
    import tempfile
    n = 0
    NAV = 146_189.45
    pol = load_policy()
    floor = min_entry_gbp(NAV, pol)["min_entry_gbp"]
    new_uses = [{"evidence_state": "CONFIRMED", "ticker": "HALOX", "gbp": 6578.53, "current_value_gbp": 0.0, "rung": "NORMAL",
                 "target_pct": 4.5, "source_score": 9},
                {"evidence_state": "CONFIRMED", "ticker": "ZABX", "gbp": 6578.53, "current_value_gbp": 0.0, "rung": "NORMAL",
                 "target_pct": 4.5, "source_score": 8},
                {"evidence_state": "CONFIRMED", "ticker": "GEN", "gbp": 5116.63, "current_value_gbp": 0.0, "rung": "STARTER",
                 "target_pct": 3.5, "source_score": 7}]
    with tempfile.TemporaryDirectory() as td:
        st = os.path.join(td, "underfilled_positions.json")
        # 1 NEGATIVE: a reporting/proposal allocate() with the store loaded by allocate leaves it byte-identical
        global FILL_STORE
        _saved_store = FILL_STORE
        FILL_STORE = st
        try:
            save_fill_obligations({"obligations": []}, st)
            b0 = open(st, "rb").read()
            out = allocate(new_uses, capital_gbp=11000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol)
            assert open(st, "rb").read() == b0 and out["obligations_persisted_to"] is None, \
                "ISA-0701 NEGATIVE CONTROL: a proposal run must leave the store byte-identical"
            n += 1
            # 3 NEGATIVE: the underfilled proposal is PROPOSED only
            assert out["proposed_obligations"] and all(p["state"] == "PROPOSED" for p in out["proposed_obligations"]) \
                and load_fill_obligations(st)["obligations"] == [], "ISA-0701: proposal creates no ACTIVE row"
            n += 1
            # 2 NEGATIVE: re-run / dry-run shapes (obligations passed, repeated) - still no write, caller doc untouched
            passed = {"obligations": []}
            allocate(new_uses, capital_gbp=11000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                     obligations=passed, membership=_ADMITTED)
            allocate(new_uses, capital_gbp=11000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol)
            assert passed == {"obligations": []} and open(st, "rb").read() == b0, \
                "ISA-0701 NEGATIVE CONTROL: re-runs never mutate the store or a caller's doc"
            n += 1
        finally:
            FILL_STORE = _saved_store
        # 15 HALO/ZAB-like fabricated rows: non-binding at head, retained when invalidated
        fab = {"obligations": [dict(ticker=t, opened_on="2026-09-12", allocated_gbp=6657.34,
                                    target_rung="NORMAL", obligation_gbp=250.11, voided=False,
                                    voided_reason=None) for t in ("HALOX", "ZABX")]}
        out_f = allocate(new_uses, capital_gbp=13564.79, nav_gbp=NAV, ranking_basis="source_score",
                         policy=pol, obligations=fab, membership=_ADMITTED)
        assert out_f["obligations_at_head"] == [] and len(out_f["obligations_non_binding"]) == 2, \
            "ISA-0701 MUST-FIRE: fabricated rows without provenance are NOT at the head of the queue"
        n += 1
        r_f = {r["ticker"]: r for r in out_f["rows"]}
        assert r_f["HALOX"]["state"] in ("FULL", "UNDERFILLED") and "OBLIGATION" not in r_f["HALOX"]["state"], \
            "ISA-0701: an unheld name is a NEW entry under the entry floor, never an obligation fill"
        n += 1
        held_uses = [dict(u, current_value_gbp=1000.0) for u in new_uses[:2]]
        out_h = allocate(held_uses, capital_gbp=13564.79, nav_gbp=NAV, ranking_basis="source_score",
                         policy=pol, obligations=fab, membership=_ADMITTED)
        assert out_h["obligations_at_head"] == [] and all(
            "UNPROVEN" in x["reason"] for x in out_h["obligations_non_binding"]), \
            "ISA-0701 NEGATIVE CONTROL: even for a HELD name, a row without execution provenance is no first claim (0685-safe)"
        n += 1
        inv = invalidate_unproven_obligations(json.loads(json.dumps(fab)), today="2026-09-16")
        assert inv["voided"] == ["HALOX", "ZABX"] and all(
            o["state"] == "VOIDED" and o["allocated_gbp"] == 6657.34 and o["opened_on"] == "2026-09-12"
            and o["correction_provenance"][0]["item"] == "ISA-0701" for o in inv["doc"]["obligations"]), \
            "ISA-0701: fabricated rows are RETAINED with original fields + correction provenance"
        n += 1
        assert invalidate_unproven_obligations(inv["doc"], today="2026-09-17")["voided"] == [], \
            "ISA-0701: invalidation is idempotent"
        n += 1
        # activation fixtures
        plan_doc = allocate(new_uses[2:], capital_gbp=floor + 100.0, nav_gbp=NAV, ranking_basis="source_score",
                            policy=pol, obligations={"obligations": []})
        assert plan_doc["proposed_obligations"][0]["ticker"] == "GEN"
        plans = {"oct_2026": {"allocation": plan_doc, "artifact": "capital_destination_oct_2026.json",
                              "sha256": "x", "as_of": "2026-10-03"}}
        loader = lambda m: plans.get(m)                                          # noqa: E731

        def ent(tk="GEN", decision="buy", status="confirmed_executed", amount=floor + 100.0, route="growth",
                source="transaction_record", ref="REF1", date="2026-10-04"):
            return {"_id": "%s::%s::%s" % (date, tk, decision), "date": date, "ticker": tk, "route": route,
                    "decision": decision, "execution_status": status, "execution_source": source,
                    "executed_amount_gbp": amount, "executed_date": "2026-10-06", "executed_quantity": 10,
                    "executed_reference": ref}
        d0 = {"obligations": []}
        # 4 NEGATIVE: unexecuted recommendation
        r = activate_from_executions([ent(status="recommended")], loader, doc=d0, membership=_ADMITTED)
        assert not d0["obligations"] and not r["outcomes"], "ISA-0701: an unexecuted recommendation creates nothing"
        n += 1
        # 5 NEGATIVE: off-framework trades never appear as entries at all, so they can never
        #   activate anything.
        # ⚑ ISA-0701/ISA-0686 (20-Sep-2026): the VCI route USED to be blocked here because VCI
        #   decisions were not canonical in the ledger. ISA-0686 closed, so a VCI execution now
        #   activates like any other route. This is the positive control for lifting the block.
        r = activate_from_executions([ent(route="vci")], loader, doc=d0, membership=_ADMITTED)
        assert d0["obligations"] and r["activated"] == ["GEN"] \
            and d0["obligations"][0]["state"] == "ACTIVE", \
            ("ISA-0686 MUST-FIRE: a VCI execution matched to a canonical decision and an "
             "authorised plan now creates an ACTIVE obligation — the ISA-0686 block is lifted "
             "because the reason for it no longer exists (%s)" % r)
        n += 1
        assert not ACTIVATION_BLOCKED_ROUTES, \
            ("ISA-0701: the blocked-route map is EMPTY, not deleted — a future route without "
             "canonical decisions is blocked by declaration, not by a new code path")
        n += 1
        d0 = {"obligations": []}

        # ⚑ 6 NEGATIVE / ISA-0685 MUST-FIRE: a HELD name with NO canonical admission decision
        #   creates NO obligation, however confirmed the execution is. Until ISA-0686 closed
        #   there was nowhere to ask this question; now there is, and the refusal is named.
        d_na = {"obligations": []}
        r = activate_from_executions([ent(tk="LEGACY")], loader, doc=d_na, membership=_UNDECIDED)
        assert not d_na["obligations"] and r["outcomes"][0]["outcome"] == "BLOCKED_NOT_ADMITTED" \
            and r["outcomes"][0]["membership"] == "ADMITTED_UNDECIDED", \
            ("ISA-0685 MUST-FIRE: held is not admitted — an execution on a name the framework "
             "never decided to admit creates no first claim (%s)" % r)
        n += 1
        r = activate_from_executions([ent(tk="LEGACY")], loader, doc=d_na,
                                     membership=lambda t: {"state": "UNKNOWN_DISABLED",
                                                           "may_generate_fill_obligation": False,
                                                           "why": "resolver unavailable"})
        assert not d_na["obligations"] and r["outcomes"][0]["outcome"] == "BLOCKED_NOT_ADMITTED", \
            ("ISA-0685 NEGATIVE CONTROL: an UNKNOWN membership verdict BLOCKS and never passes "
             "(R4.3) — not being able to ask is not the same as a yes")
        n += 1
        d0 = {"obligations": []}
        r = activate_from_executions([ent(source="holdings_delta")], loader, doc=d0, membership=_ADMITTED)
        assert not d0["obligations"] and r["outcomes"][0]["outcome"] == "UNVERIFIED_EXECUTION"
        n += 1
        # NEGATIVE: plan post-dating the decision is not the authorised plan
        r = activate_from_executions([ent(date="2026-10-02")], loader, doc=d0, membership=_ADMITTED)
        assert not d0["obligations"] and r["outcomes"][0]["outcome"] in ("PLAN_NOT_CONTEMPORANEOUS", "NO_AUTHORISED_PLAN")
        n += 1
        # 8 NEGATIVE: new-position execution below minimum entry -> named deviation, no claim
        r = activate_from_executions([ent(amount=floor - 500.0)], loader, doc=d0, membership=_ADMITTED)
        assert not d0["obligations"] and r["outcomes"][0]["outcome"] == "EXECUTION_DEVIATION_BELOW_MIN_ENTRY", r
        n += 1
        # 10 POSITIVE: authorised underfilled entry + matching execution -> exactly one ACTIVE with provenance
        r = activate_from_executions([ent()], loader, doc=d0, membership=_ADMITTED)
        a = d0["obligations"]
        assert r["activated"] == ["GEN"] and len(a) == 1 and a[0]["state"] == "ACTIVE" \
            and a[0]["source_decision_id"] == "2026-10-04::GEN::buy" and a[0]["execution_reference"] == "REF1" \
            and a[0]["opened_on"] == "2026-10-06" and obligation_binding(a[0])["binding"], a
        n += 1
        # 7 NEGATIVE: idempotent re-reconciliation
        r = activate_from_executions([ent()], loader, doc=d0, membership=_ADMITTED)
        assert len(d0["obligations"]) == 1 and r["outcomes"][0]["outcome"] == "IDEMPOTENT_SKIP", r
        n += 1
        # 11/12 POSITIVE: the ACTIVE claim heads the next queue and fills the CURRENT gap, not the stale amount
        nxt = [{"evidence_state": "CONFIRMED", "ticker": "NEWB", "gbp": 6000.0, "current_value_gbp": 0.0, "rung": "NORMAL", "source_score": 99},
               {"evidence_state": "CONFIRMED", "ticker": "GEN", "gbp": 777.77, "current_value_gbp": floor + 100.0, "rung": "STARTER",
                "source_score": 1}]
        o2 = allocate(nxt, capital_gbp=10000.0, nav_gbp=NAV, ranking_basis="source_score", policy=pol, obligations=d0, membership=_ADMITTED)
        assert o2["order"][0] == "GEN" and o2["rows"][0]["state"] == "OBLIGATION_FILLED" \
            and o2["rows"][0]["allocated_gbp"] == 777.77 and d0["obligations"][0]["obligation_gbp"] != 777.77, o2["rows"][0]
        n += 1
        # 14 POSITIVE: fulfil on a later confirmed top-up whose plan row filled the obligation; row retained
        plans["nov_2026"] = {"allocation": o2, "artifact": "capital_destination_nov_2026.json", "sha256": "y",
                             "as_of": "2026-11-07"}
        r = activate_from_executions([ent(decision="top_up", date="2026-11-08", ref="REF2", amount=777.77)],
                                     loader, doc=d0, membership=_ADMITTED)
        assert r["fulfilled"] == ["GEN"] and d0["obligations"][0]["state"] == "FULFILLED" \
            and len(d0["obligations"]) == 1 and not obligation_binding(d0["obligations"][0])["binding"], r
        n += 1
        # 9/13 CAP_CONSTRAINED_ENTRY: proposal creates nothing; executed one activates conditional
        cc_plan = {"proposed_obligations": [{"ticker": "CAPC", "kind": "CAP_CONSTRAINED_ENTRY", "conditional": True,
                                             "condition": "fresh headroom", "target_gbp": 6000.0, "is_new": True,
                                             "min_entry_gbp": floor, "authorised_allocation_gbp": floor + 10,
                                             "target_rung": "NORMAL", "state": "PROPOSED"}], "rows": []}
        plans["dec_2026"] = {"allocation": cc_plan, "artifact": "a", "sha256": "z", "as_of": "2026-12-05"}
        d1 = {"obligations": []}
        activate_from_executions([ent(tk="CAPC", status="recommended", date="2026-12-06")], loader, doc=d1, membership=_ADMITTED)
        assert d1["obligations"] == [], "ISA-0701: proposal-only CAP_CONSTRAINED_ENTRY creates no obligation"
        n += 1
        activate_from_executions([ent(tk="CAPC", date="2026-12-06", amount=floor + 10, ref="R9")],
                                 loader, doc=d1, membership=_ADMITTED)
        assert d1["obligations"][0]["kind"] == "CAP_CONSTRAINED_ENTRY" and d1["obligations"][0]["conditional"] is True \
            and d1["obligations"][0]["state"] == "ACTIVE"
        n += 1
        # NEGATIVE CONTROL of the control: flag off -> no activation (safe state), never proposal persistence
        import isa_policy as _ip
        _had = "obligation_activation" in _ip.V2_FLAGS
        _old = _ip.V2_FLAGS.get("obligation_activation")
        try:
            _ip.V2_FLAGS["obligation_activation"] = False
            d2 = {"obligations": []}
            r = activate_from_executions([ent(ref="REF3")], loader, doc=d2, membership=_ADMITTED)
            assert d2["obligations"] == [] and r["outcomes"][0]["outcome"] == "ACTIVATION_DISABLED"
            n += 1
        finally:
            if _had:
                _ip.V2_FLAGS["obligation_activation"] = _old
            else:
                _ip.V2_FLAGS.pop("obligation_activation", None)
    if verbose:
        print("position_sizing._selftest_isa0701: %d assertions, 0 failed" % n)
    return n


# ── ISA-0685 test helper ────────────────────────────────────────────────────────────────
# Every obligation fixture below predates the membership contract and implicitly assumes the
# held name WAS admitted. `_ADMITTED` states that assumption instead of inheriting whatever the
# live ledger happens to say about a synthetic ticker — a fixture that silently depends on the
# live book is not a fixture. The refusing case has its own explicit resolver at each must-fire.
def _ADMITTED(_t):
    return {"ticker": _t, "state": "ADMITTED_DECIDED",
            "may_generate_fill_obligation": True, "may_hold_new_capital_priority": True,
            "why": "selftest fixture: this name is DECLARED admitted (ISA-0685)"}


def _UNDECIDED(_t):
    return {"ticker": _t, "state": "ADMITTED_UNDECIDED",
            "may_generate_fill_obligation": False, "may_hold_new_capital_priority": False,
            "why": "selftest fixture: held with no admitting decision (ISA-0685)"}


def _selftest_isa0752(verbose: bool = False) -> int:
    """ISA-0752 (25-Sep-2026) — evidence_state is CARRIED from the canonical producer through the use
    row, the proposal and the activated obligation; missing evidence cannot become an obligation.
    liveness_ref: position_sizing._selftest_isa0752"""
    n = 0
    NAV = 146_189.0
    pol = load_policy()
    _unm = {"measured": False, "rho_sleeve": 0.70,
            "rho_basis": "UNMEASURED_ADVERSE_DEFAULT", "size_ceiling": "STARTER"}
    sm = stock_max([{"ticker": "NEWE", "qualifies": True, "evidence_state": "CONFIRMED",
                     "current_value_gbp": 0.0, "source_score": 9.0, "correlation": _unm}],
                   nav_gbp=NAV, capital_on_offer_gbp=20_000.0, policy=pol)
    u = sm["qualifying_uses"][0]
    assert u.get("evidence_state") == "CONFIRMED", ("MUST-FIRE ISA-0752: the use row carries the "
                                                    "producer's evidence_state", u)
    n += 1
    floor = min_entry_gbp(NAV, pol)["min_entry_gbp"]
    cap = round(floor + 100.0, 2)
    uses = [dict(u, source_score=9.0)]
    out = allocate(uses, capital_gbp=cap, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                   obligations={"obligations": []})
    pr = [p for p in out["proposed_obligations"] if p["ticker"] == "NEWE"]
    assert pr and pr[0]["evidence_state"] == "CONFIRMED" and not out["obligation_proposals_refused"], (
        "MUST-FIRE ISA-0752: an UNDERFILLED proposal carries the same evidence_state (no recompute)", out)
    n += 1
    bare = [{k: v for k, v in uses[0].items() if k != "evidence_state"}]
    out2 = allocate(bare, capital_gbp=cap, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                    obligations={"obligations": []})
    assert not out2["proposed_obligations"] and out2["obligation_proposals_refused"] \
        and out2["obligation_proposals_refused"][0]["ticker"] == "NEWE", (
        "NEGATIVE CONTROL ISA-0752: a use WITHOUT evidence_state cannot become a fill obligation - "
        "refused by name, never silently", out2)
    n += 1
    a1 = {r["ticker"]: r["allocated_gbp"] for r in out["rows"]}
    a2 = {r["ticker"]: r["allocated_gbp"] for r in out2["rows"]}
    assert a1 == a2, ("NEGATIVE CONTROL ISA-0752: the evidence gate decides only whether a first claim "
                      "is PROPOSED - the allocation itself is unchanged", a1, a2)
    n += 1
    bad = [dict(uses[0], evidence_state="T9")]
    out3 = allocate(bad, capital_gbp=cap, nav_gbp=NAV, ranking_basis="source_score", policy=pol,
                    obligations={"obligations": []})
    assert not out3["proposed_obligations"] and out3["obligation_proposals_refused"], (
        "NEGATIVE CONTROL ISA-0752: an UNDECLARED evidence_state is refused like a missing one", out3)
    n += 1
    if verbose:
        print("position_sizing._selftest_isa0752: %d assertions, 0 failed" % n)
    return n


def _selftest():
    """⚑ Added 26-Aug-2026 after the delivered-location sweep found this module's --selftest
    importing a scaffolding module that was never written. The other eight V2.1 modules each
    carry a real one, and a self-test that cannot run is the same class of defect as a module
    that is never called: it reports nothing and nobody notices."""
    allocate = _selftest_allocate    # D17 gate: fixtures carry admissible cases
    lad, caps = ladder(), hard_caps()
    assert [lad[k] for k in ("STARTER", "NORMAL", "HIGH", "EARNED_MAX")] == [3.5, 4.5, 5.5, 6.5]
    assert abs(max(lad.values()) - caps["max_stock_position_pct"]) < 1e-9, "ladder max != hard cap"

    MEAS = {"measured": True, "rho_sleeve": 0.25, "rho_basis": "MEASURED_SHRUNK"}
    UNM = {"measured": False, "rho_sleeve": 0.70,
           "rho_basis": "UNMEASURED_ADVERSE_DEFAULT", "size_ceiling": "STARTER"}
    assert apply_correlation(target_pct("STRONG"), MEAS)["target_pct"] == 5.5
    # an unmeasured correlation CAPS at STARTER and never scales
    for st in ("CONFIRMED", "STRONG", "EARNED_MAX"):
        r = apply_correlation(target_pct(st), UNM)
        assert r["target_pct"] == 3.5 and r["capped_by"] == "UNMEASURED_CORRELATION_STARTER_CAP"
    # sizing without a correlation record must REFUSE
    for bad, needle in ((lambda: apply_correlation(target_pct("STRONG"), None), "dangerous"),
                        (lambda: target_pct("MADE_UP"), "no declared rung")):
        try:
            bad(); raise AssertionError("should have refused")
        except SizingRefused as e:
            assert needle in str(e), str(e)
    # the DEGRADED split reaches sizing with DIFFERENT outputs (R2.10, D13)
    u = target_pct("DEGRADED_UNMEASURED", current_pct=4.12)
    d = target_pct("DEGRADED_REVERSED")
    assert u["rung"] == "HOLD_AT_CURRENT" and u["target_pct"] == 4.12
    assert d["target_pct"] == 3.5 and u["target_pct"] != d["target_pct"]
    assert not u["may_receive_new_capital"] and not d["may_receive_new_capital"]
    # demand-pull: nothing qualifies -> 0 -> routes to funds
    nq = stock_max([{"ticker": "X", "qualifies": False, "disqualified_reason": "failed T1"}],
                   nav_gbp=156_321.05, capital_on_offer_gbp=11_250.0)
    assert nq["stock_max_gbp"] == 0.0 and nq["binding"] == "nothing_qualifies"
    assert abs(nq["routes_to_funds_gbp"] - 11_250.0) < 0.01
    # ── ISA-0535 — an EMPTY list is attributed, never read as a measured rejection ────────
    e0 = stock_max([], nav_gbp=156_321.05, capital_on_offer_gbp=11_250.0)
    assert e0["binding"] == "empty_candidate_list_unattributed", e0["binding"]
    assert e0["stock_max_gbp"] == 0.0 and e0["empty_input_note"], e0
    e1 = stock_max([], nav_gbp=156_321.05, capital_on_offer_gbp=11_250.0,
                   population_binding="no_candidates_built")
    assert e1["binding"] == "no_candidates_built", e1["binding"]
    e2 = stock_max([], nav_gbp=156_321.05, capital_on_offer_gbp=11_250.0,
                   population_binding="unadjudicated_present")
    assert e2["binding"] == "unadjudicated_present", e2["binding"]
    # ⚑ THE FOUR EMPTIES ARE FOUR DIFFERENT OUTPUTS, and that is the whole point (R2.10):
    #   nothing_qualifies (assessed and rejected) · no_candidates_built (no list) ·
    #   unadjudicated_present (a verdict was unknown) · unattributed (nobody said).
    assert len({nq["binding"], e0["binding"], e1["binding"], e2["binding"]}) == 4
    # ⚑ and the basis prose no longer asserts this run's verdict
    assert "Nothing qualifies ->" not in e0["basis"] and "binding" in e0["basis"]
    # ── ISA-0496 — THE TIER 1 TRIGGER IS RELATIVE TO WHAT THE POSITION EARNED ────────────
    _em = effective_soft_cap_pct("EARNED_MAX")
    _thin = effective_soft_cap_pct("THIN")
    # Raj, 02-Sep-2026: "there should be nothing stopping an earned_max position now."
    assert _em["effective_soft_cap_pct"] == lad["EARNED_MAX"], _em
    assert _em["binds"] == "earned_rung", _em
    # ...and the lower rungs KEEP their drift control — this is the half a flat raise would
    # have deleted, and it is asserted rather than assumed.
    assert _thin["effective_soft_cap_pct"] == _thin["tier1_soft_cap_pct"], _thin
    assert _thin["binds"] == "tier1_soft_cap" and _thin["earned_rung_pct"] == lad["STARTER"]
    assert _thin["effective_soft_cap_pct"] > _thin["earned_rung_pct"], _thin
    # both remedies trim to the trigger, never below the earned rung
    for _st in ("THIN", "CONFIRMED", "STRONG", "EARNED_MAX"):
        _e = effective_soft_cap_pct(_st)
        _r = target_pct(_st)["target_pct"]
        assert _e["trim_to_on_five_question_fail_pct"] >= _r, (_st, _e)
        assert _e["trim_to_on_tier2_breach_pct"] >= _r, (_st, _e)
        assert _e["tier2_hard_cap_pct"] >= _e["effective_soft_cap_pct"], (_st, _e)
    # a frozen position earned no rung this run and SAYS so, rather than implying one
    _frozen = effective_soft_cap_pct("DEGRADED_UNMEASURED")
    assert _frozen["earned_rung_pct"] is None and _frozen["binds"] == "tier1_soft_cap", _frozen
    # REFUSES rather than defaulting to the bare soft cap — the default would reintroduce
    # ISA-0496 for exactly the positions the fix is for
    try:
        effective_soft_cap_pct()
        raise AssertionError("effective_soft_cap_pct() with no state must RAISE")
    except SizingRefused:
        pass
    # one qualifying STARTER on the live September book
    one = stock_max([{"ticker": "NEW", "qualifies": True, "evidence_state": "CONFIRMED",
                      "current_value_gbp": 0.0, "correlation": UNM}],
                    nav_gbp=156_321.05, capital_on_offer_gbp=11_250.0)
    assert 5400 < one["stock_max_gbp"] < 5500, one["stock_max_gbp"]
    # VCI: a missing p_thesis RAISES, never p = 0
    try:
        vci_size_pct(p_thesis=None, L=0.6, budget_available_pct=1.5,
                     evidence_state="CONFIRMED")
        raise AssertionError("should have refused")
    except SizingRefused as e:
        assert "not a measured zero" in str(e)
    # is_binary is stateful and RESOLVED releases on the same run
    pend = {"ticker": "T", "is_binary": True, "catalyst_status": PENDING,
            "p_thesis": 0.5, "L": 0.35, "size_pct": 0.78}
    assert binary_commitment(pend)["commits_budget"] is True
    assert binary_commitment({**pend, "catalyst_status": RESOLVED_POSITIVE})["commitment_pct"] == 0.0
    # min-hold: loss blocks, profit permits a trim, a declared exemption clears it
    assert min_hold_ok(position_first_entry_date="2026-08-02", today="2026-08-26",
                       at_a_loss=True)["ok"] is False
    assert min_hold_ok(position_first_entry_date="2026-08-02", today="2026-08-26")["trim_only"] is True
    assert min_hold_ok(position_first_entry_date="2026-08-02", today="2026-08-26", at_a_loss=True,
                       exempt_reason="evidence_reversal")["ok"] is True
    # ── D24 (ISA-0545) — THE CLOCK IS A PROPERTY OF THE POSITION ──────────────────────────
    _open, _topup, _now = "2026-03-01", "2026-08-15", "2026-08-20"
    _a = min_hold_ok(position_first_entry_date=_open, today=_now)
    assert _a["min_hold_until"] == "2026-08-30", _a          # 182d from the POSITION's opening
    # POSITIVE CONTROL — a top-up on 15-Aug does not move it, because the clock never took the
    # lot date as an input at all. Under retired A9 this would have read 2027-02-13.
    assert min_hold_ok(position_first_entry_date=_open,
                       today=_now)["min_hold_until"] == _a["min_hold_until"]
    assert min_hold_ok(position_first_entry_date=_topup,
                       today=_now)["min_hold_until"] == "2027-02-13"   # what A9 would have said
    # NEGATIVE CONTROL — a genuinely NEW position still starts its own clock, and the retired
    # keyword still refuses rather than silently meaning the new thing.
    try:
        min_hold_ok(entry_date=_topup, today=_now)
        raise AssertionError("D24: min_hold_ok(entry_date=) must RAISE, not accept")
    except SizingRefused:
        pass
    # ── D23 (ISA-0544) — THE RESERVE IS THE SINGLE CONTROL ────────────────────────────────
    _pol = load_policy()
    assert "min_topup_gbp" not in (_pol.get("stock_sleeve") or {}), \
        "D23: min_topup_gbp must stay ABSENT - the GBP 250 reserve is the single control"
    assert float((_pol["stock_sleeve"])["cash_reserve_gbp"]) > 0

    # ══ ISA-0688 — two questions, two fields, two consumers ═════════════════════════
    # The QBTS shape: event-dominated POSITION whose next catalyst is observable, not go/no-go.
    _q688 = {"ticker": "QBTSX", "size_pct": 1.0, "is_binary": True, "catalyst_status": PENDING,
             "p_thesis": 0.55, "L": 0.35, "asset_structure": "platform",
             "position_event_risk": True, "catalyst_is_binary": False}
    _f = binary_fields(_q688)
    assert _f["position_event_risk"] is True and _f["catalyst_is_binary"] is False \
        and _f["consumes_expected_loss_budget"] is True \
        and _f["consumes_concurrent_event_slot"] is False, \
        ("⚑ ISA-0688 MUST-FIRE: a position may consume the EXPECTED-LOSS budget without "
         "consuming a CONCURRENT-EVENT slot. One flag answered both and QBTS paid twice (%s)" % _f)
    _f2 = binary_fields({"ticker": "OBS", "size_pct": 1.0, "catalyst_status": PENDING,
                         "position_event_risk": False, "catalyst_is_binary": True})
    assert _f2["consumes_expected_loss_budget"] is False \
        and _f2["consumes_concurrent_event_slot"] is True, \
        ("⚑ ISA-0688 MUST-FIRE: and the reverse — an ordinary position facing a dated go/no-go "
         "event consumes a SLOT without entering the binary expected-loss budget (%s)" % _f2)
    _bud688 = budget_available([_q688], budget_pct=1.5, max_concurrent=1)
    assert _bud688["expected_loss_population"] == ["QBTSX"] \
        and _bud688["concurrent_event_population"] == [] \
        and _bud688["count_cap_breached"] is False, \
        ("⚑ ISA-0688 MUST-FIRE THROUGH THE CONSUMER: the budget counts QBTSX and the cap does "
         "NOT — the two populations are computed separately and both are published (%s)"
         % {k: _bud688[k] for k in ("expected_loss_population", "concurrent_event_population",
                                    "count_cap_breached")})
    _unk688 = binary_fields({"ticker": "U", "size_pct": 1.0, "catalyst_status": PENDING,
                             "position_event_risk": "UNKNOWN", "catalyst_is_binary": "UNKNOWN"})
    assert _unk688["position_event_risk"] == BINARY_UNKNOWN \
        and _unk688["consumes_expected_loss_budget"] == BINARY_UNKNOWN \
        and _unk688["consumes_concurrent_event_slot"] == BINARY_UNKNOWN, \
        ("⚑ ISA-0688 NEGATIVE CONTROL: UNKNOWN is never silently FALSE on either axis (V-1) "
         "— %s" % _unk688)
    _bud_unk = budget_available([dict(_q688, ticker="U1", catalyst_is_binary="UNKNOWN")],
                                budget_pct=1.5, max_concurrent=0)
    assert _bud_unk["concurrent_event_unknown"] == ["U1"] and _bud_unk["count_cap_breached"] is True, \
        ("ISA-0688: an UNKNOWN catalyst character COUNTS against the cap — the conservative "
         "direction for a cap is to count it, and the row is NAMED (%s)" % _bud_unk)
    _legacy = binary_fields({"ticker": "OLD", "is_binary": True, "catalyst_status": PENDING})
    assert _legacy["position_event_risk"] is True and _legacy["catalyst_is_binary"] is True, \
        ("ISA-0688: a position declared under the OLD single-flag schema still says something "
         "true about itself — the legacy declaration is the fallback for both fields, so the "
         "split is not a silent re-declaration of every held name (%s)" % _legacy)
    _bare = binary_fields({"ticker": "BARE", "catalyst_status": PENDING})
    assert _bare["position_event_risk"] == BINARY_UNKNOWN \
        and _bare["catalyst_is_binary"] == BINARY_UNKNOWN, \
        ("ISA-0688 NEGATIVE CONTROL: with NO declaration at all both fields are UNKNOWN, never "
         "False — absence means nobody decided (V-1/R4.3)")
    _res = binary_fields({"ticker": "R", "is_binary": True,
                          "catalyst_status": RESOLVED_POSITIVE_SUCCESSOR_PENDING,
                          "successor": {"state": "PENDING"}})
    assert _res["catalyst_is_binary"] == BINARY_UNKNOWN, \
        ("⚑ ISA-0688 MUST-FIRE: a catalyst RESOLUTION invalidates the previous classification "
         "— the successor must be classified AFRESH, and the parent's answer is not carried "
         "forward (%s)" % _res)
    assert _res["position_event_risk"] is True, \
        ("ISA-0688: resolving a catalyst does not change whether OWNING the position is "
         "event-dominated — that is the other question, and it survives")
    try:
        binary_fields({"ticker": "X", "position_event_risk": "MAYBE"})
        assert False, "unreachable"
    except SizingRefused:
        pass
    assert True, "ISA-0688 NEGATIVE CONTROL: an undeclared binary state is REFUSED"

    # ══ ISA-0685 — HELD IS NOT ADMITTED, at the allocation queue ════════════════════
    def _m_use(tk, gbp=300.0, cv=100.0, score=10.0):
        return {"ticker": tk, "gbp": gbp, "current_value_gbp": cv, "rung": "STARTER",
                "target_pct": 3.5, "evidence_state": "T1", "source_score": score}

    def _m_doc(tk="LEG"):
        return {"obligations": [{"ticker": tk, "voided": False, "state": "ACTIVE",
                                 "obligation_id": "OBL-m", "source_decision_id": "D-m",
                                 "execution_reference": "X-m", "allocated_gbp": 200.0,
                                 "target_rung": "STARTER", "obligation_gbp": 300.0,
                                 "opened_on": "2026-08-14"}]}
    _mk = [_m_use("LEG", gbp=300.0, score=1.0), _m_use("NEW", gbp=4000.0, cv=0.0, score=99.0)]
    _adm = allocate(_mk, capital_gbp=1000.0, nav_gbp=100_000.0, ranking_basis="source_score",
                    obligations=_m_doc(), membership=_ADMITTED)
    assert _adm["order"][0] == "LEG" and _adm["obligations_at_head"] == ["LEG"], \
        ("ISA-0685 POSITIVE CONTROL: an ADMITTED name with an ACTIVE obligation still heads "
         "the queue — the contract must not break a legitimate first claim (%s)" % _adm["order"])
    _und = allocate(_mk, capital_gbp=1000.0, nav_gbp=100_000.0, ranking_basis="source_score",
                    obligations=_m_doc(), membership=_UNDECIDED)
    assert _und["obligations_at_head"] == [] and _und["order"][0] == "NEW", \
        ("⚑ ISA-0685 MUST-FIRE: an ADMITTED_UNDECIDED holding loses NEW-CAPITAL PRIORITY — "
         "being owned is not being admitted, and a first claim is priority (%s)" % _und)
    assert any(x.get("membership") == "ADMITTED_UNDECIDED"
               and "ISA-0685" in x.get("reason", "") for x in _und["obligations_non_binding"]), \
        ("ISA-0685: the lost claim is PUBLISHED with its membership state and reason, never "
         "silently dropped (R2.10) — %s" % _und["obligations_non_binding"])
    assert _und["membership_at_head"]["LEG"]["state"] == "ADMITTED_UNDECIDED", \
        "ISA-0685: the verdict behind every head-of-queue decision travels with the allocation"
    _row = {r["ticker"]: r for r in _und["rows"]}["LEG"]
    assert _row["allocated_gbp"] >= 0 and "SELL" not in str(_row.get("state")), \
        ("⚑ ISA-0685 NEGATIVE CONTROL: ADMITTED_UNDECIDED is NOT an exit signal. The position "
         "stays in the queue, stays sizeable and stays reportable; it loses only the priority "
         "it was never granted (%s)" % _row)
    _unk = allocate(_mk, capital_gbp=1000.0, nav_gbp=100_000.0, ranking_basis="source_score",
                    obligations=_m_doc(),
                    membership=lambda t: {"state": "UNKNOWN_DISABLED",
                                          "may_hold_new_capital_priority": False,
                                          "why": "resolver unavailable"})
    assert _unk["obligations_at_head"] == [], \
        ("ISA-0685 NEGATIVE CONTROL: an UNKNOWN membership verdict removes priority and never "
         "grants it (R4.3) — the safe direction is the refusing one")

    # ── ISA-0543 (A9 half, 13-Sep-2026) — would_have_reset_clock, MEASURED not DECLARED ────
    def _a9_use(tk="AAA", gbp=300.0, cv=100.0, score=10.0):
        return {"ticker": tk, "gbp": gbp, "current_value_gbp": cv, "rung": "STARTER",
                "target_pct": 3.5, "evidence_state": "T1", "source_score": score}

    def _a9_doc(opened_on):
        _obl = {"ticker": "AAA", "voided": False, "voided_reason": None, "state": "ACTIVE",
                "obligation_id": "OBL-fixture", "source_decision_id": "2026-08-01::AAA::buy",
                "execution_reference": "REF-fixture",
                "allocated_gbp": 200.0, "target_rung": "STARTER", "obligation_gbp": 300.0,
                "evidence_state_at_entry": "T1", "basis": "ISA-0543 selftest fixture"}
        if opened_on is not None:
            _obl["opened_on"] = opened_on
        return {"obligations": [_obl]}

    # POSITIVE CONTROL — a full obligation fill 30 days after the position's first entry:
    # the retired A9 rule would have anchored a fresh clock at the fill date, extending the
    # lock-in by exactly the gap between the two dates.
    _a9r1 = allocate([_a9_use(gbp=300.0)], capital_gbp=1000.0, nav_gbp=100_000.0,
                     ranking_basis="source_score", obligations=_a9_doc("2026-08-14"), membership=_ADMITTED,
                     today="2026-09-13")["rows"][0]
    assert _a9r1["state"] == "OBLIGATION_FILLED"
    assert _a9r1["would_have_reset_clock"] is True and _a9r1["would_have_reset_clock_days"] == 30, \
        f"ISA-0543 POSITIVE CONTROL: a fill 30 days after first entry must read " \
        f"would_have_reset_clock=True, days=30, got {_a9r1}"

    # BOUNDARY CONTROL — a fill landing on the position's OWN first-entry date: the two
    # clocks agree, so the retired rule would NOT have extended anything.
    _a9r2 = allocate([_a9_use(gbp=300.0)], capital_gbp=1000.0, nav_gbp=100_000.0,
                     ranking_basis="source_score", obligations=_a9_doc("2026-09-13"), membership=_ADMITTED,
                     today="2026-09-13")["rows"][0]
    assert _a9r2["would_have_reset_clock"] is False and _a9r2["would_have_reset_clock_days"] == 0, \
        f"ISA-0543 BOUNDARY CONTROL: a same-day fill must read would_have_reset_clock=False, " \
        f"days=0, got {_a9r2}"

    # POSITIVE CONTROL — a PARTIAL obligation fill (remaining < gap) is the same defect class
    # and must be instrumented identically to a full fill.
    _a9r3 = allocate([_a9_use(gbp=300.0)], capital_gbp=100.0, nav_gbp=100_000.0,
                     ranking_basis="source_score", obligations=_a9_doc("2026-07-01"), membership=_ADMITTED,
                     today="2026-09-13")["rows"][0]
    assert _a9r3["state"] == "OBLIGATION_PARTIAL" and _a9r3["would_have_reset_clock"] is True, \
        f"ISA-0543 POSITIVE CONTROL: OBLIGATION_PARTIAL must be instrumented too, got {_a9r3}"

    # NEGATIVE CONTROL — an obligation record with no `opened_on` is a NAMED absence
    # (None + an UNMEASURED basis string), never a silently-omitted key or a guessed date.
    _a9r4 = allocate([_a9_use(gbp=300.0)], capital_gbp=1000.0, nav_gbp=100_000.0,
                     ranking_basis="source_score", obligations=_a9_doc(None), membership=_ADMITTED,
                     today="2026-09-13")["rows"][0]
    assert _a9r4["would_have_reset_clock"] is None and "UNMEASURED" in _a9r4["would_have_reset_clock_basis"], \
        f"ISA-0543 NEGATIVE CONTROL: a missing opened_on must read None with a named " \
        f"UNMEASURED basis, not a guessed value, got {_a9r4}"

    # NEGATIVE CONTROL — a brand-new position (UNDERFILLED, not an obligation fill) must NOT
    # carry the field at all: the counterfactual only means something for a fill onto an
    # EXISTING position, and a new open getting a stray True/False/None would be noise.
    _a9r5 = allocate([{"ticker": "NEWX", "gbp": 5000.0, "current_value_gbp": 0.0,
                       "rung": "STARTER", "target_pct": 3.5, "evidence_state": "T1",
                       "source_score": 5.0}],
                     capital_gbp=3000.0, nav_gbp=100_000.0, ranking_basis="source_score",
                     obligations={"obligations": []}, today="2026-09-13")["rows"][0]
    assert _a9r5["state"] == "UNDERFILLED" and "would_have_reset_clock" not in _a9r5, \
        f"ISA-0543 NEGATIVE CONTROL: a new-position UNDERFILLED row must carry no " \
        f"would_have_reset_clock key at all, got {_a9r5}"

    # ── ISA-0721 / Raj D17 decision 23-Sep-2026: a fill/top-up is FRESH positive capital ──
    # MUST-FIRE: a HELD obligation name with NO admissible current underwriting case gets NO
    #   fill this tranche; the obligation is retained (block event, released 0), nothing is
    #   sold, and the capital stays in the queue for the next admissible destination.
    _d17_obl = _a9_doc("2026-08-14")          # a binding ACTIVE obligation on held AAA
    _d17_uses = [_a9_use(tk="AAA", gbp=300.0, cv=100.0, score=9.0),
                 _a9_use(tk="NEWD", gbp=400.0, cv=0.0, score=5.0)]
    _d17_miss = lambda t: None if t == "AAA" else _selftest_admit_all(t)
    _d17a = _selftest_allocate(_d17_uses, capital_gbp=1000.0, nav_gbp=100_000.0,
                               ranking_basis="source_score", obligations=_d17_obl,
                               today="2026-09-23", underwriting=_d17_miss, membership=_ADMITTED)
    _d17r = {r["ticker"]: r for r in _d17a["rows"]}
    assert _d17r["AAA"]["state"] == "OBLIGATION_FILL_BLOCKED_NO_ADMISSIBLE_ER" \
        and _d17r["AAA"]["allocated_gbp"] == 0.0 and _d17r["AAA"]["er_state"] == "NO_CURRENT_CASE", \
        "MUST-FIRE: a held fill with no admissible current case must be blocked, got %r" % _d17r["AAA"]
    assert _d17r["NEWD"]["allocated_gbp"] > 0.0, "the blocked tranche's capital must stay in the queue"
    assert any(e.get("verdict") == "NO_ADMISSIBLE_ER" and e.get("released_gbp") == 0.0
               for e in _d17a["obligation_block_events"]), \
        "the obligation must be RETAINED (block event, nothing released), never voided"
    assert not any(str(r.get("state", "")).upper().startswith("SELL") for r in _d17a["rows"]), \
        "a missing E[r] must never manufacture a SELL"
    # MUST-FIRE: a non-admissible (UNVERIFIED_LEGACY_PARSER) case blocks exactly like none.
    _d17b = _selftest_allocate(_d17_uses[:1], capital_gbp=1000.0, nav_gbp=100_000.0,
                               ranking_basis="source_score", obligations=_d17_obl, today="2026-09-23", membership=_ADMITTED,
                               underwriting={"AAA": {"case_id": "UWC-L", "admissible_for_positive_size": False,
                                                       "er": {"state": "UNVERIFIED_LEGACY_PARSER"}}})
    assert _d17b["rows"][0]["state"] == "OBLIGATION_FILL_BLOCKED_NO_ADMISSIBLE_ER" \
        and _d17b["rows"][0]["underwriting_case_id"] == "UWC-L", _d17b["rows"][0]
    # MUST-FIRE: a held non-obligation top-up is gated the same way.
    _d17c = _selftest_allocate(_d17_uses[:1], capital_gbp=1000.0, nav_gbp=100_000.0,
                               ranking_basis="source_score", obligations={"obligations": []},
                               today="2026-09-23", underwriting=lambda t: None)
    assert _d17c["rows"][0]["state"] == "TOPUP_BLOCKED_NO_ADMISSIBLE_ER", _d17c["rows"][0]
    # NEGATIVE CONTROL: the SAME held obligation with an admissible current case must still
    #   fill — the gate must not become a blanket block on held names.
    _d17d = _selftest_allocate(_d17_uses[:1], capital_gbp=1000.0, nav_gbp=100_000.0,
                               ranking_basis="source_score", obligations=_d17_obl, today="2026-09-23", membership=_ADMITTED,
                               underwriting=_selftest_admit_all)
    assert _d17d["rows"][0]["state"] == "OBLIGATION_FILLED" and _d17d["rows"][0]["allocated_gbp"] == 300.0, \
        "NEGATIVE CONTROL: an admissible current case must not be blocked, got %r" % _d17d["rows"][0]

    _n = sum(1 for _nd in ast.walk(ast.parse(inspect.getsource(_selftest)))
             if isinstance(_nd, (ast.Assert,)))
    # ── ISA-0548 negative controls, INSIDE _selftest so the R5.5 census sees them ──────
    # The census counts labelled markers in THIS function's body by AST; a delegated call
    # would run the controls and count as none, which is the vacuous pass it exists to catch.
    _n0548 = _selftest_isa0548(verbose=False)
    _n0548 += _selftest_isa0465(verbose=False)
    _n0548 += _selftest_isa0701(verbose=False)
    _n0548 += _selftest_isa0752(verbose=False)
    assert _n0548 >= 10, "the ISA-0548 control block must not be emptied by a refactor"
    # ⚑ NEGATIVE CONTROL: a HELD stock absent from the binary registry must be REFUSED, never
    #   read as not-a-binary — `false` is a declaration, absence means nobody has decided.
    assert held_binary_rows([{"ticker": "NOPE", "size_pct": 1.0}],
                            declared={})["refusals"], "absence must refuse"
    # ⚑ NEGATIVE CONTROL: one unpriceable PENDING binary must WITHHOLD the whole committed
    #   figure — a sum over the priceable remainder understates the book and would license a
    #   deployment the refused name may forbid.
    assert budget_available_reported(
        [{"ticker": "B", "size_pct": 1.0, "is_binary": True, "catalyst_status": PENDING}],
        budget_pct=1.5, max_concurrent=2)["committed_pct"] is None, "must withhold, not compute"
    # ⚑ NEGATIVE CONTROL: an UNPRICED successor must RE-ARM, not release. Releasing it reads a
    #   null as a zero and the error would then run TOWARD the risk (V-1, ISA-0655).
    assert binary_commitment({"ticker": "S", "size_pct": 1.0, "is_binary": True,
                              "asset_structure": "platform",
                              "catalyst_status": RESOLVED_POSITIVE_SUCCESSOR_PENDING,
                              "successor": {"priceable": False}})["commits_budget"] is True, \
        "an unpriceable successor must still commit budget"
    print("position_sizing selftest OK (%d + %d ISA-0548 negative controls)" % (_n, _n0548))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps({"ladder": ladder(), "hard_caps": hard_caps(),
                      "min_hold_exempt": list(MIN_HOLD_EXEMPT)}, indent=1))
