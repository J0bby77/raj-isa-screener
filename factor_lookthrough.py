#!/usr/bin/env python3
"""
factor_lookthrough.py — Doc B B3: factor look-through concentration metric + soft cap (D15).

ai_complex_effective_weight (% of total ISA) =
    sum(direct stock value x ai_complex class) + sum(fund value x fund_ai_share)
Classification lives in factor_map.json (stocks 0/0.5/1; funds = share estimates — floor,
coverage honesty). Breach (> FACTOR_AI_SOFT_CAP_PCT, default 30) => mechanical escalation:
Step 8 must carry a Category-6 de-concentration option, and a BUY that raises the factor
weight while in breach is BLOCKED at Checkpoint-D (tick via factor_state).

Usage: python3 factor_lookthrough.py --portfolio portfolio_data_jul_2026.json
       python3 factor_lookthrough.py --selftest
"""
from __future__ import annotations
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import scoring_config as _cfg
except Exception:
    _cfg = None

MAP_PATH = os.path.join(HERE, "factor_map.json")


def _cap():
    return float(getattr(_cfg, "FACTOR_AI_SOFT_CAP_PCT", 30.0)) if _cfg else 30.0


def load_map(path=MAP_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute(portfolio, fmap):
    """portfolio = portfolio_data dict; fmap = factor_map dict. Pure (U-B3)."""
    total = (portfolio.get("summary") or {}).get("total_value_gbp") or 0.0
    stocks_map = fmap.get("stocks", {})
    funds_map = fmap.get("funds", {})
    contrib, unclassified = [], []
    eff = 0.0
    for h in portfolio.get("stocks", []):
        t, v = h.get("ticker"), h.get("value_gbp") or 0.0
        c = stocks_map.get(t)
        if c is None:
            unclassified.append(t)
            continue
        if c > 0:
            eff += v * c
            contrib.append({"ticker": t, "kind": "stock", "class": c,
                            "value_gbp": round(v, 2), "effective_gbp": round(v * c, 2)})
    for h in portfolio.get("funds", []):
        t, v = h.get("ticker"), h.get("value_gbp") or 0.0
        fm = funds_map.get(t)
        if fm is None:
            unclassified.append(t)
            continue
        share = float(fm.get("fund_ai_share") or 0.0)
        if share > 0:
            eff += v * share
            contrib.append({"ticker": t, "kind": "fund", "class": share,
                            "value_gbp": round(v, 2), "effective_gbp": round(v * share, 2)})
    pct = round(eff / total * 100.0, 1) if total > 0 else None
    cap = _cap()
    classified_val = sum(c["value_gbp"] for c in contrib)
    return {
        "ai_complex_effective_weight_pct": pct,
        "cap_pct": cap,
        "breach": bool(pct is not None and pct > cap),
        "effective_gbp": round(eff, 2),
        "total_gbp": round(total, 2),
        "contributors": sorted(contrib, key=lambda c: -c["effective_gbp"]),
        "unclassified": sorted(set(unclassified)),
        "coverage_note": ("fund shares are FLOOR estimates from top-10 holdings (~40-60% of "
                          "assets) + seeded judgment — see factor_map.json _meta"),
        "email_line": (f"AI-complex effective weight: {pct}% vs {cap:.0f}% cap — "
                       f"{'BREACH' if (pct is not None and pct > cap) else 'OK'}"
                       if pct is not None else "AI-complex weight: n/a (no portfolio total)"),
    }


def _selftest():
    fmap = {"stocks": {"AAA": 1.0, "BBB": 0.5, "CCC": 0.0}, "funds": {"FND": {"fund_ai_share": 0.4}}}
    port = {"summary": {"total_value_gbp": 1000.0},
            "stocks": [{"ticker": "AAA", "value_gbp": 100.0}, {"ticker": "BBB", "value_gbp": 100.0},
                       {"ticker": "CCC", "value_gbp": 100.0}, {"ticker": "NEW", "value_gbp": 50.0}],
            "funds": [{"ticker": "FND", "value_gbp": 500.0}]}
    r = compute(port, fmap)
    # 100*1 + 100*0.5 + 500*0.4 = 350 -> 35% -> breach at 30
    assert r["ai_complex_effective_weight_pct"] == 35.0, r
    assert r["breach"] is True and r["unclassified"] == ["NEW"], r
    port["funds"][0]["value_gbp"] = 100.0
    r2 = compute(port, fmap)   # 100+50+40=190 -> /600... total still 1000 -> 19% OK
    assert r2["breach"] is False
    print("factor_lookthrough SELF-TEST OK (U-B3)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--map", default=MAP_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        with open(a.portfolio, encoding="utf-8") as f:
            port = json.load(f)
        print(json.dumps(compute(port, load_map(a.map)), indent=2))
