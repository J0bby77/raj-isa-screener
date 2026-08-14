#!/usr/bin/env python3
"""
vci_risk_budget.py — E4 sleeve-level binary risk budget (replaces the count cap as the primary control).

Spec: VCI_Framework_Enhancements_Implementation_Jul2026.md E4.

Ruin is a PORTFOLIO property, not a per-name one: two tiny survivable starters are safer than one
oversized bet. Each open/proposed binary starter carries an expected loss:

    risk_i = size_pct_i × L_i × (1 − p_i)          (% ISA expected loss)

Admit a new starter only if Σ risk stays within VCI_SLEEVE_BINARY_RISK_BUDGET. Names sharing a
catalyst_domain are correlated — the proposed starter's risk is inflated by VCI_BINARY_CORR_RIDER
when it shares a domain with an already-open starter. A loosened count cap remains a secondary guard.

Pure functions, stdlib-only. Safe to import anywhere.
"""
from __future__ import annotations
from typing import Optional

try:
    import scoring_config as cfg
except Exception:
    cfg = object()


def _c(name, default):
    return getattr(cfg, name, default)


try:   # D-24 Stage 6 / V-1 — THE one shared null-handling helper (gated_control.py)
    from gated_control import gated as _gated, unknown_controls as _unknown_controls
except Exception:                                            # noqa: BLE001
    def _gated(v, *, control_name, registry=None):
        ok = v is not None and not isinstance(v, bool)
        if registry is not None:
            registry[control_name] = "OK" if ok else "UNKNOWN"
        return ("OK" if ok else "UNKNOWN"), (not ok), (float(v) if ok else None)

    def _unknown_controls(reg):
        return sorted(k for k, v in (reg or {}).items() if v == "UNKNOWN")


def position_risk(size_pct: Optional[float], L: Optional[float], p: Optional[float]) -> float:
    """Expected loss (% ISA) of one binary starter. Missing p/L -> conservative (p=0, L=1).

    ⚑ D-24 Stage 6 / V-1 (09-Aug-2026). The substitution above is conservative FOR THE ARITHMETIC
    and catastrophic for the DECISION: the number it returns is indistinguishable from a measured
    one. On 09-Aug a caller-key typo (`p` where the entry stores `p_thesis`) therefore flipped a
    DENY into an ADMIT on QBTS — the first deploy-eligible new VCI name in the sleeve's history —
    and the wrong figure, 1.55, was entirely plausible. The float return is UNCHANGED so no caller
    breaks; the missing inputs are now also NAMED via position_risk_detail(), and admit() blocks
    on them. A control fed a null must return UNKNOWN and block, never pass.
    """
    return position_risk_detail(size_pct, L, p)["risk"]


def position_risk_detail(size_pct: Optional[float], L: Optional[float],
                         p: Optional[float], label: str = "") -> dict:
    """{risk, unknown[]} — the same number, plus the controls that could not be evaluated."""
    reg = {}
    _, _, s_ = _gated(size_pct, control_name=f"{label}size_pct", registry=reg)
    _, _, l_ = _gated(L, control_name=f"{label}L", registry=reg)
    _, _, p_ = _gated(p, control_name=f"{label}p_thesis", registry=reg)
    s = float(s_ if s_ is not None else 0.0)
    ll = float(l_ if l_ is not None else 1.0)
    pp = float(p_ if p_ is not None else 0.0)
    return {"risk": round(s * ll * (1.0 - pp), 4), "unknown": _unknown_controls(reg)}


def committed_risk(open_positions) -> float:
    """Σ expected-loss across currently open binary starters. Each: {size_pct, L, p_thesis}."""
    return round(sum(position_risk(e.get("size_pct"), e.get("L"), e.get("p_thesis"))
                     for e in (open_positions or [])), 4)


def admit(proposed: dict, open_positions=None, budget: Optional[float] = None,
          corr_rider: Optional[float] = None, max_concurrent: Optional[int] = None) -> dict:
    """Decide whether a proposed binary starter fits the sleeve risk budget.
    Returns {ok, committed, proposed_risk, headroom, correlated, reason}."""
    open_positions = open_positions or []
    budget = float(budget if budget is not None else _c("VCI_SLEEVE_BINARY_RISK_BUDGET", 1.5) or 0.0)
    rider = float(corr_rider if corr_rider is not None else _c("VCI_BINARY_CORR_RIDER", 1.5))
    max_concurrent = int(max_concurrent if max_concurrent is not None else _c("VCI_BINARY_MAX_CONCURRENT", 3))

    committed = committed_risk(open_positions)
    _pd = position_risk_detail(proposed.get("size_pct"), proposed.get("L"),
                              proposed.get("p_thesis"), label="proposed.")
    p_risk = _pd["risk"]
    # V-1: every control that could not be evaluated, across the proposed AND open starters.
    unknown = list(_pd["unknown"])
    for _i, _e in enumerate(open_positions):
        unknown += position_risk_detail(_e.get("size_pct"), _e.get("L"), _e.get("p_thesis"),
                                        label=f"open[{_e.get('ticker') or _i}].")["unknown"]

    # correlation rider: shared catalyst domain with any open starter inflates the proposed risk
    dom = proposed.get("catalyst_domain")
    correlated = bool(dom) and any(e.get("catalyst_domain") == dom for e in open_positions)
    p_risk_eff = round(p_risk * (rider if correlated else 1.0), 4)

    # budget disabled -> fall back to the count cap only
    if unknown:
        # A control fed a null returns UNKNOWN and BLOCKS. It never quietly becomes p = 0.
        return {"ok": False, "committed": committed, "proposed_risk": p_risk_eff,
                "headroom": None, "correlated": correlated, "unknown_controls": unknown,
                "reason": "DENY: UNKNOWN control(s) " + ", ".join(unknown)
                          + " — a missing input is not a measured zero (V-1)"}

    if not budget:
        ok = len(open_positions) < max_concurrent
        return {"ok": ok, "committed": committed, "proposed_risk": p_risk_eff,
                "headroom": None, "correlated": correlated, "unknown_controls": [],
                "reason": "budget disabled; count cap " + ("ok" if ok else "breached")}

    headroom = round(budget - committed, 4)
    ok_budget = (committed + p_risk_eff) <= budget + 1e-9
    ok_count = len(open_positions) < max_concurrent
    ok = ok_budget and ok_count
    if ok:
        reason = f"admit: committed {committed} + proposed {p_risk_eff} <= budget {budget}"
    elif not ok_budget:
        reason = (f"DENY: committed {committed} + proposed {p_risk_eff}"
                  + (" (corr-inflated)" if correlated else "") + f" > budget {budget}")
    else:
        reason = f"DENY: count cap {max_concurrent} reached"
    return {"ok": ok, "committed": committed, "proposed_risk": p_risk_eff,
            "headroom": headroom, "correlated": correlated, "unknown_controls": [],
            "reason": reason}


if __name__ == "__main__":
    openp = [dict(ticker="A", size_pct=1.0, L=0.60, p_thesis=0.30, catalyst_domain="biotech_readout"),
             dict(ticker="B", size_pct=0.75, L=0.35, p_thesis=0.55, catalyst_domain="ai_optical")]
    print("committed:", committed_risk(openp))
    # Mechanism demo: with the count cap loosened to 3 (E4's original design point), a small 3rd
    # binary should ADMIT on budget grounds alone (a tight count cap at 2 would have blocked it).
    # The LIVE config cap is 2, not 3 (31-Jul-2026 reconciliation, scoring_config.py §9.4) -- pass
    # max_concurrent explicitly here to demonstrate the mechanism independent of that live policy value.
    r1 = admit(dict(ticker="C", size_pct=0.5, L=0.35, p_thesis=0.55, catalyst_domain="rare_earth"),
               openp, max_concurrent=3)
    print("small 3rd (cap loosened to 3):", r1["ok"], r1["reason"])
    assert r1["ok"]
    # Regression: under the LIVE cap (2), that same 3rd binary is correctly blocked by count alone.
    r1_live = admit(dict(ticker="C", size_pct=0.5, L=0.35, p_thesis=0.55, catalyst_domain="rare_earth"), openp)
    print("small 3rd (live cap=2):", r1_live["ok"], r1_live["reason"])
    assert not r1_live["ok"] and "count cap" in r1_live["reason"]
    # an oversized 3rd should DENY on a tight budget (committed 0.54 + 0.675 = 1.21 > 1.0)
    r2 = admit(dict(ticker="D", size_pct=1.5, L=0.60, p_thesis=0.25, catalyst_domain="quantum"), openp, budget=1.0)
    print("oversized:", r2["ok"], r2["reason"])
    assert not r2["ok"]
    # correlated pair (same domain as B) -> inflated risk
    r3 = admit(dict(ticker="E", size_pct=1.0, L=0.35, p_thesis=0.55, catalyst_domain="ai_optical"), openp)
    print("correlated:", r3["ok"], r3["correlated"], r3["proposed_risk"], r3["reason"])
    assert r3["correlated"]
    # ⚑ V-1 (09-Aug-2026): a MISSING p_thesis must DENY, not silently price at p = 0. This is the
    # exact shape of the QBTS flip — same call, same plausible number, opposite decision.
    r4 = admit(dict(ticker="F", size_pct=0.75, L=0.60, catalyst_domain="quantum"), openp[:1])
    print("missing p_thesis:", r4["ok"], r4["reason"])
    assert not r4["ok"] and r4["unknown_controls"] == ["proposed.p_thesis"], r4
    # ...and a missing input on an ALREADY-OPEN starter poisons the committed total just as badly.
    r5 = admit(dict(ticker="G", size_pct=0.5, L=0.35, p_thesis=0.55), [dict(ticker="H", size_pct=1.0, L=0.6)])
    assert not r5["ok"] and "open[H].p_thesis" in r5["unknown_controls"], r5
    # A real ZERO is not a missing value.
    r6 = admit(dict(ticker="I", size_pct=0.5, L=0.35, p_thesis=0.0), [], budget=1.0)
    assert r6["ok"] and r6["unknown_controls"] == [], r6
    print("vci_risk_budget self-test PASSED (incl. V-1 UNKNOWN-blocks)")
