#!/usr/bin/env python3
"""prospective_weight_shadow.py — ISA-0714 residual, SHADOW ONLY (26-Sep-2026).

THE QUESTION (P6.4, BuildSpec 27-Aug, ISA-0464): rho_sleeve for the remaining candidates is
recomputed "against the sleeve AS IT WILL THEN STAND, including names admitted earlier in the same
run". On the LIVE path `capital_destination` passes weights = held_value / NAV, so a name admitted
earlier in the run carries weight 0 in the next candidate's WEIGHTED rho_sleeve (it still counts in
rho_max_pairwise). The letter of P6.4 is not met; P6.4 does not say what weight to use.

THE APPROVED TARGET STATE (handoff v2, Stage G1):
  each candidate admitted earlier in the same sequence enters the hypothetical sleeve at its
  AUTHORITATIVE PRE-CORRELATION PROPOSED FUNDING WEIGHT, capped by remaining deployable stock capital:
      prospective_weight = min(pre-correlation target weight, remaining deployable stock capital / NAV)
  Rejected as standing policy: zero, an arbitrary MIN_ENTRY or STARTER weight, equal weighting, and
  the final post-sequencer allocation (circular).

THIS MODULE INVENTS NO SIZE. It composes the canonical sizing authority:
  * position_sizing.target_pct(evidence_state, current_pct)   — the ladder target BEFORE the
    correlation cap (the "pre-correlation target"), and whether the name may receive new capital;
  * position_sizing.min_entry_gbp(nav)                        — the D16 floor: a NEW name that cannot
    reach it cannot receive a valid positive size, so it is NOT represented as a funded position;
  * the stock capital on offer (`stock_max`) that the router already computed.
No threshold, rung or fraction is declared here.

SHADOW ONLY. `MODE` is "SHADOW"; anything else REFUSES. The record is published beside the
authoritative plan (`sleeve_split["prospective_weight_shadow"]`) and nothing reads it for capital.
A failure is published as UNAVAILABLE and is never raised into the allocation.
"""
from __future__ import annotations

import itertools
from typing import Callable, Dict, List, Optional

MODE = "SHADOW"
_ALLOWED_MODES = ("SHADOW", "OFF")
_REGISTRY: Dict[str, dict] = {}
_COUNTER = itertools.count(1)
_MAX_KEPT = 4


class ShadowRefused(Exception):
    pass


def _mode() -> str:
    if MODE not in _ALLOWED_MODES:
        raise ShadowRefused(
            "prospective_weight_shadow.MODE=%r - only SHADOW/OFF are permitted before a separate "
            "acceptance decision on the 03-Oct shadow evidence (ISA-0714, handoff v2 Stage G2)" % MODE)
    return MODE


def remember(**inputs) -> Optional[str]:
    """Keep the sequencer's exact inputs (process-local) and return a JSON-safe token."""
    if _mode() == "OFF":
        return None
    tok = "PWS-%d" % next(_COUNTER)
    _REGISTRY[tok] = inputs
    while len(_REGISTRY) > _MAX_KEPT:
        _REGISTRY.pop(next(iter(_REGISTRY)))
    return tok


def recall(token: Optional[str]) -> Optional[dict]:
    return _REGISTRY.get(token) if token else None


def weight_fn(candidates: List[dict], *, nav_gbp: float, capital_gbp: float,
              policy=None, ps=None) -> Callable[[List[str]], Dict[str, Optional[float]]]:
    """-> f(admitted_in_run) giving each in-run admitted name its prospective weight (fraction of NAV).

    Walks the admitted names IN ADMISSION ORDER against the remaining deployable stock capital:
      target  = position_sizing.target_pct(evidence_state, current_pct)   (pre-correlation)
      want    = target% x NAV - current value
      funded  = min(want, remaining)
      NEW name with funded < MIN_ENTRY, or a name that may not receive new capital, or no
      evidence_state -> NOT a funded hypothetical position (weight = current weight, 0 if new).
    """
    if ps is None:
        import position_sizing as ps                                    # the ONE sizing authority
    nav = float(nav_gbp)
    by = {c.get("ticker"): c for c in (candidates or []) if isinstance(c, dict) and c.get("ticker")}
    floor = float(ps.min_entry_gbp(nav, policy)["min_entry_gbp"]) if nav > 0 else float("inf")
    detail: Dict[str, dict] = {}

    def f(in_run: List[str]) -> Dict[str, Optional[float]]:
        remaining = max(float(capital_gbp or 0.0), 0.0)
        out: Dict[str, Optional[float]] = {}
        for tk in in_run:
            c = by.get(tk) or {}
            cur = float(c.get("current_value_gbp") or 0.0)
            cur_w = cur / nav if nav > 0 else 0.0
            ev = c.get("evidence_state")
            if not ev:
                out[tk] = cur_w
                detail[tk] = {"funded_gbp": 0.0, "why": "NO_EVIDENCE_STATE - not represented as funded"}
                continue
            try:
                t = ps.target_pct(ev, current_pct=cur_w * 100.0, policy=policy)
            except Exception as e:                                      # noqa: BLE001
                out[tk] = cur_w
                detail[tk] = {"funded_gbp": 0.0, "why": "target_pct refused (%s)" % e}
                continue
            if not t.get("may_receive_new_capital", True):
                out[tk] = cur_w
                detail[tk] = {"funded_gbp": 0.0, "rung": t.get("rung"),
                              "why": "sizing authority: may not receive new capital"}
                continue
            want = max(float(t["target_pct"]) / 100.0 * nav - cur, 0.0)
            funded = min(want, remaining)
            if funded <= 0 or (cur <= 0 and funded < floor):
                out[tk] = cur_w
                detail[tk] = {"funded_gbp": 0.0, "rung": t.get("rung"), "target_pct": t.get("target_pct"),
                              "why": ("remaining GBP %.2f cannot fund a valid positive size (D16 floor "
                                      "GBP %.2f) - not represented as funded" % (remaining, floor))}
                continue
            remaining -= funded
            out[tk] = (cur + funded) / nav
            detail[tk] = {"funded_gbp": round(funded, 2), "rung": t.get("rung"),
                          "target_pct": t.get("target_pct"), "prospective_weight": round(out[tk], 6),
                          "remaining_after_gbp": round(remaining, 2)}
        return out

    f.detail = detail                                                   # type: ignore[attr-defined]
    return f


def _funded(alloc: Optional[dict]) -> Dict[str, float]:
    rows = (alloc or {}).get("rows") or []
    return {r["ticker"]: round(float(r.get("allocated_gbp") or 0.0), 2)
            for r in rows if (r.get("allocated_gbp") or 0) > 0}


def evaluate(token: Optional[str], *, candidates: List[dict], nav_gbp: float, capital_gbp: float,
             current_sequence: Optional[dict], current_allocation: Optional[dict],
             allocate: Optional[Callable] = None, policy=None, ps=None) -> dict:
    """Re-run the sequencer on its exact inputs with prospective weights; publish current vs
    proposed order, verdicts and funding. Never raises (the caller also guards)."""
    base = {"mode": MODE, "authoritative": False, "issue": "ISA-0714",
            "basis": ("P6.4 residual, handoff v2 Stage G target: an in-run admitted name enters the next "
                      "candidate's weighted rho_sleeve at min(position_sizing.target_pct pre-correlation "
                      "target, remaining deployable stock capital / NAV); a name the sizing authority "
                      "cannot fund (D16 floor / may not receive new capital) is not represented as funded. "
                      "SHADOW: nothing reads this for capital.")}
    try:
        if _mode() == "OFF":
            return dict(base, state="OFF")
        inp = recall(token)
        if not inp:
            return dict(base, state="UNAVAILABLE", reason="sequencer inputs not retained for this run")
        import deployment_sequencer as ds
        fn = weight_fn(candidates, nav_gbp=nav_gbp, capital_gbp=capital_gbp, policy=policy, ps=ps)
        prop = ds.sequence(inp["cands"], held=inp["held"], matrix=inp["matrix"], sigmas=inp["sigmas"],
                           weights=inp["weights"], se_rho=inp.get("se_rho"),
                           ranking_basis=inp.get("ranking_basis", "source_score"),
                           prospective_weights=fn)
        cur = current_sequence or {}

        def _v(seq):
            return {r["ticker"]: {"verdict": r["verdict"],
                                  "rho_at_decision": (r.get("correlation_at_decision") or {}).get("rho_sleeve")}
                    for r in (seq.get("records") or [])}
        cv, pv = _v(cur), _v(prop)
        co, po = list(cur.get("order") or []), list(prop.get("order") or [])
        first_div = next((i + 1 for i, (a, b) in enumerate(zip(co, po)) if a != b),
                         (min(len(co), len(po)) + 1) if len(co) != len(po) else None)
        verdict_changes = {t: {"current": cv.get(t, {}).get("verdict"), "proposed": pv.get(t, {}).get("verdict")}
                           for t in sorted(set(cv) | set(pv))
                           if cv.get(t, {}).get("verdict") != pv.get(t, {}).get("verdict")}
        rho_shift = {t: round(pv[t]["rho_at_decision"] - cv[t]["rho_at_decision"], 4) for t in pv
                     if t in cv and pv[t]["rho_at_decision"] is not None and cv[t]["rho_at_decision"] is not None
                     and abs(pv[t]["rho_at_decision"] - cv[t]["rho_at_decision"]) > 1e-9}
        cur_f = _funded(current_allocation)
        prop_alloc, prop_f, alloc_err = None, None, None
        if allocate is not None:
            try:
                prop_alloc = allocate(po, prop.get("replacement_only") or [])
                prop_f = _funded(prop_alloc)
            except Exception as e:                                      # noqa: BLE001
                alloc_err = "%s: %s" % (type(e).__name__, e)
        fund_changes = (None if prop_f is None else
                        {t: {"current_gbp": cur_f.get(t, 0.0), "proposed_gbp": prop_f.get(t, 0.0)}
                         for t in sorted(set(cur_f) | set(prop_f)) if cur_f.get(t, 0.0) != prop_f.get(t, 0.0)})
        return dict(base, state="OK",
                    current={"order": co, "funded": cur_f},
                    proposed={"order": po, "funded": prop_f, "funding_unavailable": alloc_err,
                              "prospective_weights": getattr(fn, "detail", {})},
                    diff={"order_first_divergence_position": first_div,
                          "verdict_changes": verdict_changes,
                          "rho_at_decision_shift": rho_shift,
                          "funding_changes": fund_changes,
                          "decision_relevant": bool(verdict_changes or fund_changes)})
    except Exception as e:                                              # noqa: BLE001
        return dict(base, state="UNAVAILABLE", reason="%s: %s" % (type(e).__name__, e))


def _selftest() -> int:
    """Must-fire synthetic cases around the ~3.33% NAV flip point (rho_to_set_diagnostic 25-Sep)."""
    import deployment_sequencer as ds

    class _PS:                                  # a declared stand-in for the ONE sizing authority
        LAD = {"STARTER": 3.5, "NORMAL": 4.5}

        @staticmethod
        def target_pct(ev, current_pct=0.0, policy=None):
            if ev == "DEGRADED_UNMEASURED":
                return {"rung": "HOLD_AT_CURRENT", "target_pct": current_pct, "may_receive_new_capital": False}
            return {"rung": ev, "target_pct": _PS.LAD[ev], "may_receive_new_capital": True}

        @staticmethod
        def min_entry_gbp(nav, policy=None):
            return {"min_entry_gbp": 0.028 * nav}

    n = 0
    HELD = ["H1", "H2"]; W = {"H1": 0.03, "H2": 0.03}
    M = {"H1|H2": 0.40, "C1|H1": 0.30, "C1|H2": 0.30, "C2|H1": 0.55, "C2|H2": 0.55, "C1|C2": 0.69}
    SIG = {"H1": 0.5, "H2": 0.5, "C1": 0.5, "C2": 0.5}
    C = [{"ticker": "C1", "source_score": 70.0}, {"ticker": "C2", "source_score": 69.0}]
    NAV = 100000.0

    def run(ev, cap):
        fn = weight_fn([{"ticker": "C1", "evidence_state": ev, "current_value_gbp": 0.0}],
                       nav_gbp=NAV, capital_gbp=cap, ps=_PS)
        r = ds.sequence(C, held=HELD, matrix=M, sigmas=SIG, weights=W, se_rho=0.0995, prospective_weights=fn)
        return {x["ticker"]: x["verdict"] for x in r["records"]}, fn

    base = ds.sequence(C, held=HELD, matrix=M, sigmas=SIG, weights=W, se_rho=0.0995)
    assert {x["ticker"]: x["verdict"] for x in base["records"]}["C2"] == "ADMIT", \
        "positive control: the LIVE path (no prospective weights) is unchanged - C1 at weight 0, C2 ADMIT"; n += 1
    v, fn = run("STARTER", 10000.0)
    assert v["C2"] == "REPLACEMENT_ONLY", \
        "must fire: C1 at its STARTER 3.5% target (> ~3.33% flip) makes C2 REPLACEMENT_ONLY, reproduced on demand"; n += 1
    assert abs(fn.detail["C1"]["prospective_weight"] - 0.035) < 1e-9, "negative control: weight is the target, not invented"; n += 1
    v, fn = run("STARTER", 3000.0)
    assert v["C2"] == "ADMIT" and abs(fn.detail["C1"]["prospective_weight"] - 0.03) < 1e-9, \
        "must fire: capital-capped at 3.0% (< flip) C2 stays ADMIT - the cap is the remaining capital, not a rung"; n += 1
    v, fn = run("STARTER", 2000.0)
    assert v["C2"] == "ADMIT" and fn.detail["C1"]["funded_gbp"] == 0.0, \
        "negative control: below the D16 floor C1 must not be represented as a funded position"; n += 1
    v, fn = run("DEGRADED_UNMEASURED", 10000.0)
    assert v["C2"] == "ADMIT" and fn.detail["C1"]["funded_gbp"] == 0.0, \
        "negative control: a name the authority may not fund must not carry a prospective weight"; n += 1
    global MODE
    _saved = MODE
    try:
        MODE = "LIVE"
        try:
            remember(x=1)
            raise AssertionError("must fail: LIVE mode is refused before an acceptance decision")
        except ShadowRefused:
            n += 1
    finally:
        MODE = _saved
    tok = remember(cands=C, held=HELD, matrix=M, sigmas=SIG, weights=W, se_rho=0.0995)
    rec = evaluate(tok, candidates=[{"ticker": "C1", "evidence_state": "STARTER", "current_value_gbp": 0.0}],
                   nav_gbp=NAV, capital_gbp=10000.0, current_sequence=base, current_allocation=None, ps=_PS)
    assert rec["state"] == "OK" and rec["authoritative"] is False \
        and rec["diff"]["verdict_changes"] == {"C2": {"current": "ADMIT", "proposed": "REPLACEMENT_ONLY"}}, \
        "must fire: evaluate() publishes the verdict change and is never authoritative"; n += 1
    assert evaluate("PWS-missing", candidates=[], nav_gbp=NAV, capital_gbp=1.0, current_sequence=None,
                    current_allocation=None)["state"] == "UNAVAILABLE", \
        "negative control: absent inputs are UNAVAILABLE, never an empty OK"; n += 1
    print("prospective_weight_shadow selftest: %d assertions, 0 failed" % n)
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    print(__doc__)
