#!/usr/bin/env python3
"""wealth_record.py - P-D (B08-R07, ISA-0889): the ISA's FLOW-ADJUSTED drawdown record.

THE QUESTION: "How far is the ISA below its own best, with the money Raj paid in stripped out?" - the basis Raj chose
for his loss limit on 24-Oct-2026 ("Fall from peak, flow-adjusted (Recommended)").

METHOD - reuses account_return (one home for valuation points, flows and coverage; nothing re-derived):
* Valuation points: every AJ Bell portfolio export and quarterly valuation PDF (account_return.merge_points).
* External flows: cash-statement subscriptions + TRANSFER_IN (account_return.statement_flows), with coverage.
* A wealth INDEX chain-links Modified Dietz sub-period returns between consecutive valuation points (start = 100).
  This is account_return's APPROXIMATE TWR, extended over every covered sub-period instead of a YTD window.
* A sub-period whose flows the statements do not cover is NOT chained (UNVERIFIED_FLOWS): the index stops at the last
  covered point and the uncovered tail is named with its dates - a deposit is never read as growth.
* Drawdown = index / running peak - 1. Also published, separately and never as the trigger: the GBP value against
  the net money paid in over the covered window, the cash share (liquidity), and the required return to the target
  from today's value (target_state, the one solver).

LIMITS, STATED: valuation points are monthly/quarterly, so a fall and recovery inside a month is invisible here - the
whole_book_risk static-book weekly path is the intra-month view (current holdings, labelled). The record is RECOMPUTED
from the retained originals every run (deterministic; no second store that could drift - R4.4).
Reporting and appetite input only: no capital decision reads the index directly; whole_book_stress compares its
current drawdown with Raj's limit (Step 5 review item, never a sale).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

SCHEMA = "wealth_record/1"
BASIS = ("FLOW_ADJUSTED_PEAK: chain-linked Modified Dietz index between broker valuation points (account_return), flows "
         "= cash-statement subscriptions + TRANSFER_IN; drawdown = index / running peak - 1")


def build_index(points: List[dict], flows: List[dict], coverage: dict) -> dict:
    """Pure: -> {index: [{date, value_gbp, index, peak, drawdown_pct, flows_gbp, source}], tail_uncovered: [...]}."""
    import account_return as ar
    pts = sorted(points, key=lambda p: p["date"])
    if not pts:
        return {"state": "NO_POINTS", "index": [], "tail_uncovered": []}
    cfrom, cto = coverage.get("from"), coverage.get("to")
    start = None
    for i, p in enumerate(pts):
        if cfrom and p["date"] >= cfrom:
            start = i
            break
    if start is None:
        return {"state": "NO_COVERED_POINT", "index": [], "tail_uncovered": [p["date"] for p in pts],
                "why": "no valuation point on/after the flow coverage start %s" % cfrom}
    rows = [{"date": pts[start]["date"], "value_gbp": pts[start]["value_gbp"], "index": 100.0, "flows_gbp": 0.0,
             "source": pts[start].get("source")}]
    tail = []
    lvl = 100.0
    for a, b in zip(pts[start:], pts[start + 1:]):
        if not cto or b["date"] > cto:
            tail.append({"date": b["date"], "value_gbp": b["value_gbp"], "source": b.get("source"),
                         "why": "UNVERIFIED_FLOWS: the cash statements end %s" % cto})
            continue
        if tail:                     # a covered point after an uncovered one cannot be chained across the gap
            tail.append({"date": b["date"], "value_gbp": b["value_gbp"], "source": b.get("source"),
                         "why": "after an uncovered gap"})
            continue
        ad, bd = dt.date.fromisoformat(a["date"]), dt.date.fromisoformat(b["date"])
        fl = [f for f in flows if ad < dt.date.fromisoformat(f["date"]) <= bd]
        r = ar.modified_dietz(a["value_gbp"], b["value_gbp"], ad, bd, fl)
        if r is None:
            tail.append({"date": b["date"], "why": "DEGENERATE_PERIOD"})
            continue
        lvl *= 1.0 + r
        rows.append({"date": b["date"], "value_gbp": b["value_gbp"], "index": round(lvl, 4),
                     "flows_gbp": round(sum(f["amount_gbp"] for f in fl), 2), "period_return_pct": round(100 * r, 3),
                     "source": b.get("source")})
    peak = -1.0
    mdd, mdd_at = 0.0, None
    for r in rows:
        peak = max(peak, r["index"])
        r["peak"] = round(peak, 4)
        r["drawdown_pct"] = round(100.0 * (r["index"] / peak - 1.0), 3)
        if r["drawdown_pct"] < mdd:
            mdd, mdd_at = r["drawdown_pct"], r["date"]
    return {"state": "OK", "index": rows, "tail_uncovered": tail, "max_drawdown_pct": round(mdd, 3),
            "max_drawdown_at": mdd_at}


def compute(folder: Optional[str] = None, as_of: Optional[dt.date] = None, cs=None, nav_gbp=None, cash_gbp=None) -> dict:
    _fi_mark("wealth_record", "compute")
    import account_return as ar
    folder = folder or os.path.dirname(HERE)
    as_of = as_of or dt.date.today()
    if cs is None:
        import extract_cash_statement as ecs
        cs = ecs.parse(folder=folder, as_of=as_of)
    xp, _ = ar._xlsx_points(folder)
    pp, _ = ar._pdf_points(folder)
    points, _checks = ar.merge_points(xp, pp)
    flows, cov = ar.statement_flows(cs)
    ix = build_index(points, flows, cov)
    out = {"schema": SCHEMA, "as_of": as_of.isoformat(), "basis": BASIS, "flow_coverage": cov, **ix}
    if ix.get("state") != "OK" or not ix["index"]:
        out["current_drawdown_pct"] = None
        out["line"] = "Flow-adjusted drawdown record: %s - %s" % (ix.get("state"), ix.get("why"))
        return out
    last = ix["index"][-1]
    first = ix["index"][0]
    net_in = round(sum(r.get("flows_gbp") or 0.0 for r in ix["index"][1:]), 2)
    out.update(current_drawdown_pct=last["drawdown_pct"], current_index=last["index"], peak_index=last["peak"],
               last_covered_date=last["date"],
               money_view={"window_from": first["date"], "start_value_gbp": first["value_gbp"],
                           "net_paid_in_gbp": net_in, "value_gbp": last["value_gbp"],
                           "gain_over_start_plus_paid_in_gbp": round(last["value_gbp"] - first["value_gbp"] - net_in, 2),
                           "note": "GBP view over the covered window only - shown alongside, never the appetite trigger"})
    if nav_gbp is not None and cash_gbp is not None and nav_gbp:
        out["liquidity"] = {"cash_gbp": round(cash_gbp, 2), "cash_pct_nav": round(100.0 * cash_gbp / nav_gbp, 2)}
    try:
        import whole_book_stress as wbs
        rr = wbs.required_return_pct(float(nav_gbp or last["value_gbp"]), as_of, "floor")
        out["goal"] = {"required_floor_pct_now": None if rr is None else round(rr, 2), "guardrail_pct": wbs.guardrail_pct()}
    except Exception as exc:                                            # noqa: BLE001
        out["goal"] = {"state": "UNAVAILABLE", "why": str(exc)}
    out["line"] = ("Flow-adjusted drawdown (valuation points %s..%s): %.1f%% from peak now, worst %.1f%% (%s); "
                   "%d later point(s) await cash-statement coverage"
                   % (first["date"], last["date"], last["drawdown_pct"], ix["max_drawdown_pct"],
                      ix["max_drawdown_at"] or "-", len(ix["tail_uncovered"])))
    return out


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %s" % detail))
        if not cond:
            fails.append(name)

    pts = [{"date": "2026-01-31", "value_gbp": 100000.0}, {"date": "2026-02-28", "value_gbp": 120000.0},
           {"date": "2026-03-31", "value_gbp": 108000.0}, {"date": "2026-04-30", "value_gbp": 140000.0}]
    flows = [{"date": "2026-02-15", "amount_gbp": 20000.0}]
    cov = {"from": "2026-01-01", "to": "2026-03-31"}
    ix = build_index(pts, flows, cov)
    ok("a deposit is not growth: Feb's +GBP20k inflow gives ~0% period return", abs(ix["index"][1]["period_return_pct"]) < 0.5,
       ix["index"][1])
    ok("a real fall is a drawdown: Mar -10% from the Feb peak", -10.5 < ix["index"][2]["drawdown_pct"] < -9.5, ix["index"][2])
    ok("negative control: the uncovered April point is NOT chained (UNVERIFIED_FLOWS)", len(ix["index"]) == 3 and
       ix["tail_uncovered"] and "UNVERIFIED_FLOWS" in ix["tail_uncovered"][0]["why"])
    ix2 = build_index(pts, [], {"from": "2026-06-01", "to": "2026-07-01"})
    ok("negative control: no covered point -> refused, no index", ix2["state"] == "NO_COVERED_POINT" and not ix2["index"])
    if verbose:
        print("wealth_record selftest: %d FAIL" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    import json
    print(json.dumps(compute(), indent=1, default=str)[:4000])
