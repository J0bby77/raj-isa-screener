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
import os
from typing import Dict, List, Optional

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
              policy=None) -> dict:
    """THE demand-pull rule (ISA-0430). Capital is pulled by qualified USES, not pushed by a band.

    Each candidate: {ticker, qualifies: bool, evidence_state, current_value_gbp,
                     correlation: <record>, disqualified_reason}

    ⚑ This is strictly STRONGER than the freeze it replaces. The freeze asked "has enough time
    passed"; the demand test asks "does this specific pound have a qualified destination". A
    pound with no qualified use does not enter the sleeve at all.
    ⚑ And it is only as strong as the qualification behind it — see the module docstring."""
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
        uses.append({"ticker": tk, "rung": t["rung"], "target_pct": t["target_pct"],
                     "gbp": round(gap, 2), "capped_by": t["capped_by"],
                     "correlation_measured": t["correlation_measured"]})
        total += gap
    derived = min(total, float(capital_on_offer_gbp))
    return {
        "stock_max_gbp": round(derived, 2),
        "demand_gbp": round(total, 2),
        "capital_on_offer_gbp": round(float(capital_on_offer_gbp), 2),
        "binding": ("capital_on_offer" if total > capital_on_offer_gbp else
                    "qualified_demand" if total > 0 else "nothing_qualifies"),
        "qualifying_uses": uses, "rejected": rejected,
        "routes_to_funds_gbp": round(max(float(capital_on_offer_gbp) - derived, 0.0), 2),
        "basis": ("clean spec s2 demand-pull. stock_max = sum of (ladder target x NAV - current "
                  "value) over QUALIFYING uses, capped ONLY by the capital on offer. Nothing "
                  "qualifies -> 0 -> capital_destination routes it to funds. The Phase-1 band, "
                  "A3's N_eff ladder and the one-position-per-run cap are all REMOVED."),
        "derived_not_typed": "R4.4 — computed from live values every run, never stored",
    }


def min_hold_ok(*, entry_date: str, today: Optional[str] = None, min_hold_days: int = 182,
                exempt_reason: Optional[str] = None, at_a_loss: bool = False) -> dict:
    """Lot-level min-hold with the D14 exemption set.

    ⚑ A9: a top-up creates a NEW lot clock, so topping up EXTENDS the minimum commitment. The
    trim rule is therefore 'reduce whichever lots minimise blocked capital, and log the choice'
    — not 'preferentially respect protected lots'. There is no tax consequence inside an ISA,
    so lot selection is governed by the clock's PURPOSE (anti-churn), not by lot order.
    ⚑ And inside min-hold a position IN PROFIT may be trimmed; a position AT A LOSS may not."""
    t = datetime.date.fromisoformat(today or datetime.date.today().isoformat())
    e = datetime.date.fromisoformat(entry_date)
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
    assert min_hold_ok(entry_date="2026-08-02", today="2026-08-26", at_a_loss=True)["ok"] is False
    assert min_hold_ok(entry_date="2026-08-02", today="2026-08-26")["trim_only"] is True
    assert min_hold_ok(entry_date="2026-08-02", today="2026-08-26", at_a_loss=True,
                       exempt_reason="evidence_reversal")["ok"] is True
    print("position_sizing selftest OK (22 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps({"ladder": ladder(), "hard_caps": hard_caps(),
                      "min_hold_exempt": list(MIN_HOLD_EXEMPT)}, indent=1))
