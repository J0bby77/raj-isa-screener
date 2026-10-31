#!/usr/bin/env python3
"""scale_readiness.py - P-D (B08-R10, ISA-0890): does the SAME strategy still work at GBP 1m and GBP 1.5m?

The target (target_state.json) is GBP 1m floor / GBP 1.5m stretch. This module re-sizes the CURRENT book's weights and
the framework's ACTUAL sizing rules at the current NAV, GBP 1m and GBP 1.5m, and measures what changes:
* direct stocks: each position and the framework's maximum single-stock position (max_stock_position_pct of NAV,
  target_weights - one home) against the name's measured average daily traded value (63 trading days, from the held-
  stock history cache) -> % of ADV and days to trade at 10% participation (a NAMED reporting convention, not a gate);
* funds: OEICs deal once a day at NAV (no ADV); the constraint is the fund's own size, which is NOT sourced -> UNKNOWN,
  stated. The ETF (VUAG) is measured like a stock.
* data costs and workload: stated as the current declared state - nothing in the framework's workload scales with NAV
  (it scales with the number of cases); no cost figure is invented.
⚑ B08-R10: NO NAV-based upgrade threshold is produced or implied. The output is measurements and their basis.
Reporting only (Step 6.10s / email s6); no capital decision reads it.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SCHEMA = "scale_readiness/1"
SCALES = (1_000_000.0, 1_500_000.0)
PARTICIPATION_REFERENCE = 0.10   # reporting convention (days to trade at 10% of ADV) - NOT a gate, NOT a threshold
ADV_DAYS = 63


def adv_gbp(hist: Optional[dict], days: int = ADV_DAYS) -> Optional[dict]:
    """Average daily traded value in GBP over the last `days` sessions with volume, from the held-stock history cache
    record {daily: {date: {close, volume, fx_to_gbp}}}. None when the record has no volume."""
    if not hist:
        return None
    rows = [(d, r) for d, r in sorted((hist.get("daily") or {}).items())
            if r.get("volume") and r.get("close") and r.get("fx_to_gbp")]
    rows = rows[-days:]
    if len(rows) < 20:
        return None
    vals = [r["close"] * r["volume"] * r["fx_to_gbp"] for _, r in rows]
    return {"adv_gbp": sum(vals) / len(vals), "n_days": len(rows), "from": rows[0][0], "to": rows[-1][0]}


def assess(portfolio: dict, history: Dict[str, dict], tw: Optional[dict] = None, as_of: Optional[dt.date] = None) -> dict:
    as_of = as_of or dt.date.today()
    nav = float(((portfolio or {}).get("summary") or {}).get("total_value_gbp") or 0.0)
    if nav <= 0:
        return {"schema": SCHEMA, "state": "REFUSED", "why": "no NAV"}
    max_pos = None
    try:
        max_pos = float(((tw or {}).get("thresholds") or {}).get("max_stock_position_pct"))
    except Exception:                                                   # noqa: BLE001
        max_pos = None
    scales = [("current", nav)] + [("GBP_%dk" % int(s / 1000), s) for s in SCALES]
    rows = []
    for s in (portfolio.get("stocks") or []) + [f for f in (portfolio.get("funds") or []) if f.get("ticker") == "VUAG"]:
        t = s.get("ticker")
        w = float(s.get("value_gbp") or 0.0) / nav
        a = adv_gbp(history.get(t))
        r = {"ticker": t, "weight_pct": round(100 * w, 3), "adv": a and {k: (round(v, 2) if isinstance(v, float) else v)
                                                                          for k, v in a.items()},
             "at_scale": {}}
        for lab, sc in scales:
            pos = w * sc
            cell = {"position_gbp": round(pos, 2)}
            if a:
                cell["pct_of_adv"] = round(100 * pos / a["adv_gbp"], 3)
                cell["days_at_10pct_participation"] = round(pos / (PARTICIPATION_REFERENCE * a["adv_gbp"]), 3)
            else:
                cell["liquidity"] = "UNMEASURED (no volume history)"
            r["at_scale"][lab] = cell
        rows.append(r)
    max_rows = {}
    if max_pos:
        advs = [adv_gbp(history.get(s.get("ticker"))) for s in portfolio.get("stocks") or []]
        advs = sorted(x["adv_gbp"] for x in advs if x)
        min_adv = advs[0] if advs else None
        for lab, sc in scales:
            gbp = max_pos * sc
            max_rows[lab] = {"max_stock_position_gbp": round(gbp, 2),
                             "days_at_10pct_of_least_liquid_held_adv": (round(gbp / (PARTICIPATION_REFERENCE * min_adv), 3)
                                                                        if min_adv else None)}
    funds = [{"ticker": f.get("ticker"), "dealing": "ETF (exchange)" if f.get("ticker") == "VUAG" else
              "OEIC/trust daily at NAV or market", "capacity": "UNKNOWN - fund size not sourced (no invented figure)"}
             for f in portfolio.get("funds") or []]
    worst = None
    for r in rows:
        c = r["at_scale"].get("GBP_1500k") or {}
        if c.get("days_at_10pct_participation") is not None and (worst is None or c["days_at_10pct_participation"] >
                                                                  worst[1]):
            worst = (r["ticker"], c["days_at_10pct_participation"], c.get("pct_of_adv"))
    out = {"schema": SCHEMA, "state": "OK", "as_of": as_of.isoformat(), "nav_gbp": round(nav, 2),
           "participation_reference": "%d%% of ADV - a reporting convention, not a gate (B08-R10)" % (100 * PARTICIPATION_REFERENCE),
           "positions": rows, "max_single_stock_position": max_rows, "funds": funds,
           "data_costs": "current routes: Yahoo/FT/HL (no fee), EODHD and Firecrawl connected 24-Oct-2026 as secondary "
                         "routes (cost per plan: UNKNOWN to the framework) - no NAV-dependent data cost identified",
           "workload": "the monthly review's work scales with the number of cases (12 typed answers each) and holdings, "
                       "not with NAV - no NAV-dependent workload driver identified",
           "no_threshold": "B08-R10: no NAV-based upgrade threshold is produced or implied"}
    out["line"] = ("Scale readiness: at GBP 1.5m the least liquid held position (%s) would take %.2f days at 10%% of its "
                   "ADV (%.2f%% of one day)" % worst if worst else
                   "Scale readiness: liquidity UNMEASURED (no held-stock volume history)")
    return out


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %s" % detail))
        if not cond:
            fails.append(name)
    hist = {"AAA": {"daily": {"2026-%02d-%02d" % (1 + i // 28, 1 + i % 28): {"close": 10.0, "volume": 100000,
                                                                            "fx_to_gbp": 0.75} for i in range(70)}}}
    pf = {"summary": {"total_value_gbp": 150000.0}, "stocks": [{"ticker": "AAA", "value_gbp": 7500.0},
                                                               {"ticker": "BBB", "value_gbp": 1500.0}], "funds": []}
    a = assess(pf, hist, {"thresholds": {"max_stock_position_pct": 0.065}})
    p = {r["ticker"]: r for r in a["positions"]}
    ok("ADV in GBP = close x volume x fx", abs(p["AAA"]["adv"]["adv_gbp"] - 750000.0) < 1e-6, p["AAA"]["adv"])
    ok("a 5% weight at GBP 1.5m = GBP 75k = 10% of a GBP 750k ADV = 1 day at 10% participation",
       abs(p["AAA"]["at_scale"]["GBP_1500k"]["days_at_10pct_participation"] - 1.0) < 1e-9)
    ok("negative control: no volume history -> UNMEASURED, never zero",
       p["BBB"]["at_scale"]["GBP_1500k"].get("liquidity", "").startswith("UNMEASURED"))
    ok("no NAV threshold or verdict field is emitted (B08-R10)",
       not any(k in a for k in ("upgrade_threshold", "trigger_nav", "verdict", "nav_threshold")), list(a))
    if verbose:
        print("scale_readiness selftest: %d FAIL" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
