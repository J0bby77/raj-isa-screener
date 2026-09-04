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
CATALYST_STATUSES = (PENDING, RESOLVED_POSITIVE, RESOLVED_NEGATIVE)

MIN_HOLD_EXEMPT = ("hard_thesis_break", "drawdown_mandate", "preclearance",
                   "evidence_reversal")     # D14, Raj 26-Aug-2026


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
def vci_size_pct(*, p_thesis, L, budget_available_pct, evidence_state,
                 correlation_rider=1.0, policy=None) -> dict:
    """w_vci = min( B_available / ((1-p)*L*rider), ladder[state], hard caps ).

    ⚑ A missing p_thesis is NOT p = 0. That exact null once flipped DENY->ADMIT on QBTS
    (FC-F, V-1). It RAISES here."""
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
    if not position.get("is_binary") or st != PENDING:
        return {"ticker": position.get("ticker"), "commits_budget": False, "commitment_pct": 0.0,
                "reason": ("not a binary" if not position.get("is_binary") else
                           f"catalyst {st} — commitment RELEASED on this run; it now sizes as an "
                           f"ordinary platform holding")}
    p, l_ = position.get("p_thesis"), position.get("L")
    if p is None or l_ is None:
        raise SizingRefused(f"{position.get('ticker')}: a PENDING binary with no p_thesis/L "
                            f"cannot be priced; a null is not a zero (V-1)")
    c = float(position.get("size_pct") or 0.0) * (1.0 - float(p)) * float(l_)
    return {"ticker": position.get("ticker"), "commits_budget": True,
            "commitment_pct": round(c, 6), "catalyst_date": position.get("catalyst_date"),
            "reason": f"PENDING binary commits w x (1-p) x L = {c:.4f}% of the ISA"}


def budget_available(positions: List[dict], budget_pct: float, max_concurrent: int = 1) -> dict:
    """B_available after live PENDING binaries. N is declared, not inferred."""
    recs = [binary_commitment(p) for p in positions]
    live = [r for r in recs if r["commits_budget"]]
    committed = round(sum(r["commitment_pct"] for r in live), 6)
    released = [r for r in recs if not r["commits_budget"] and r["reason"].startswith("catalyst")]
    return {"budget_pct": budget_pct, "committed_pct": committed,
            "available_pct": round(max(budget_pct - committed, 0.0), 6),
            "live_binaries": [r["ticker"] for r in live],
            "released_this_run": [r["ticker"] for r in released],
            "max_concurrent": max_concurrent,
            "count_cap_breached": len(live) > max_concurrent,
            "commitments": recs}


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
        uses.append({"ticker": tk, "rung": t["rung"], "target_pct": t["target_pct"],
                     "gbp": round(gap, 2), "capped_by": t["capped_by"],
                     "current_value_gbp": round(float(c.get("current_value_gbp") or 0.0), 2),
                     "correlation_measured": t["correlation_measured"]})
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
            o["voided_on"] = today
            o["voided_reason"] = why
    return doc


def allocate(qualifying_uses: List[dict], *, capital_gbp: float, nav_gbp: float,
             ranking_basis: str, policy=None, obligations=None,
             sequencer_order: Optional[List[str]] = None, today=None) -> dict:
    """Floor-then-priority fill. Returns the per-name allocation and the residual.

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
    doc = obligations if obligations is not None else load_fill_obligations()

    uses = list(qualifying_uses)
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
    live_obl = {o["ticker"]: o for o in doc.get("obligations", [])
                if not o.get("voided")}
    head = [u for u in uses if u["ticker"] in live_obl]
    tail = [u for u in uses if u["ticker"] not in live_obl]
    uses = head + tail

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
        is_new = float(u.get("current_value_gbp") or 0.0) <= 0.0 and tk not in live_obl
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
        remaining -= alloc
        row = {"ticker": tk, "allocated_gbp": round(alloc, 2), "state": state,
               "gap_gbp": round(gap, 2), "rung": u.get("rung"),
               "target_pct": u.get("target_pct")}
        if state == "UNDERFILLED":
            row["obligation_gbp"] = round(gap - alloc, 2)
            doc.setdefault("obligations", []).append({
                "ticker": tk, "opened_on": today or datetime.date.today().isoformat(),
                "allocated_gbp": round(alloc, 2), "target_rung": u.get("rung"),
                "obligation_gbp": round(gap - alloc, 2),
                "evidence_state_at_entry": u.get("evidence_state"),
                "voided": False, "voided_reason": None,
                "basis": ("D17 — a sub-STARTER entry carries a FIRST CLAIM on the next "
                          "tranche, ahead of any new position. ⚑ RAJ D24 (02-Sep-2026, "
                          "ISA-0545): filling it does NOT restart the min-hold clock. A9's lot "
                          "clock is RETIRED — the framework COMPELS this fill, so a resetting "
                          "clock would let it extend Raj's own lock-in involuntarily.")})
        rows.append(row)
        opened.append(tk)
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
        "allocated_gbp": round(float(capital_gbp) - residual, 2),
        "residual_gbp": residual,
        "min_entry_gbp": floor, "starter_gbp": me["starter_gbp"],
        "order_basis": order_basis, "ranking_basis": ranking_basis,
        "order": [u["ticker"] for u in uses],
        "obligations_at_head": sorted(live_obl),
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


def _selftest():
    """⚑ Added 26-Aug-2026 after the delivered-location sweep found this module's --selftest
    importing a scaffolding module that was never written. The other eight V2.1 modules each
    carry a real one, and a self-test that cannot run is the same class of defect as a module
    that is never called: it reports nothing and nobody notices."""
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

    _n = sum(1 for _nd in ast.walk(ast.parse(inspect.getsource(_selftest)))
             if isinstance(_nd, (ast.Assert,)))
    print("position_sizing selftest OK (%d assertions)" % _n)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps({"ladder": ladder(), "hard_caps": hard_caps(),
                      "min_hold_exempt": list(MIN_HOLD_EXEMPT)}, indent=1))
