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


def position_risk(size_pct: Optional[float], L: Optional[float], p: Optional[float]) -> float:
    """Expected loss (% ISA) of one binary starter. Missing p/L -> conservative (p=0, L=1)."""
    s = float(size_pct or 0.0)
    ll = float(L if L is not None else 1.0)
    pp = float(p if p is not None else 0.0)
    return round(s * ll * (1.0 - pp), 4)


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
    p_risk = position_risk(proposed.get("size_pct"), proposed.get("L"), proposed.get("p_thesis"))

    # correlation rider: shared catalyst domain with any open starter inflates the proposed risk
    dom = proposed.get("catalyst_domain")
    correlated = bool(dom) and any(e.get("catalyst_domain") == dom for e in open_positions)
    p_risk_eff = round(p_risk * (rider if correlated else 1.0), 4)

    # budget disabled -> fall back to the count cap only
    if not budget:
        ok = len(open_positions) < max_concurrent
        return {"ok": ok, "committed": committed, "proposed_risk": p_risk_eff,
                "headroom": None, "correlated": correlated,
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
            "headroom": headroom, "correlated": correlated, "reason": reason}


if __name__ == "__main__":
    openp = [dict(ticker="A", size_pct=1.0, L=0.60, p_thesis=0.30, catalyst_domain="biotech_readout"),
             dict(ticker="B", size_pct=0.75, L=0.35, p_thesis=0.55, catalyst_domain="ai_optical")]
    print("committed:", committed_risk(openp))
    # a small 3rd binary should ADMIT (count cap alone at 2 would have blocked)
    r1 = admit(dict(ticker="C", size_pct=0.5, L=0.35, p_thesis=0.55, catalyst_domain="rare_earth"), openp)
    print("small 3rd:", r1["ok"], r1["reason"])
    assert r1["ok"]
    # an oversized 3rd should DENY on a tight budget (committed 0.54 + 0.675 = 1.21 > 1.0)
    r2 = admit(dict(ticker="D", size_pct=1.5, L=0.60, p_thesis=0.25, catalyst_domain="quantum"), openp, budget=1.0)
    print("oversized:", r2["ok"], r2["reason"])
    assert not r2["ok"]
    # correlated pair (same domain as B) -> inflated risk
    r3 = admit(dict(ticker="E", size_pct=1.0, L=0.35, p_thesis=0.55, catalyst_domain="ai_optical"), openp)
    print("correlated:", r3["ok"], r3["correlated"], r3["proposed_risk"], r3["reason"])
    assert r3["correlated"]
    print("vci_risk_budget self-test PASSED")
