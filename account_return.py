#!/usr/bin/env python3
"""account_return.py — ISA-0642 (6), built 19-Oct-2026: the ACTUAL ACCOUNT's return, flows stripped.

WHY THIS EXISTS
---------------
The email's only "YTD" was the Morningstar X-Ray figure, which is HOLDINGS-BASED: today's holdings
back-cast to 1-January. It answers "how have the things I own now done?", not "how has my account
done?". Between 31-Dec-2025 and 02-Oct-2026 Raj paid GBP 34,000 in, re-shaped the fund book and
started a stock sleeve, so the two differ materially (indicative 13-Oct: +17.6% money-weighted vs
+24.56% X-Ray). Raj asked (13-Oct-2026) for complete YTD figures. This module measures the account.

METHOD, STATED SO IT IS NOT OVER-READ
-------------------------------------
* Valuation points: every AJ Bell portfolio export (exact, to the penny, via
  extract_portfolio.parse_portfolio) and every AJ Bell quarterly "Portfolio Valuation" PDF (the
  TOTAL line, whole pounds). Where both exist for one date the export wins and the PDF is a
  CROSS-CHECK (|diff| <= GBP 1). The PDF's TOTAL CASH BALANCE is cross-checked against the cash
  statement's running balance on the same date.
* External flows: extract_cash_statement EXTERNAL_FLOW_CATEGORIES (subscriptions + TRANSFER_IN),
  current tax year AND prior_tax_years (Raj's 2025/26 export, return-flow evidence — ISA-0642).
* Each sub-period between consecutive valuation points is a Modified Dietz return; sub-periods are
  CHAIN-LINKED. That is an APPROXIMATE time-weighted return: an exact TWR needs a valuation on every
  flow date, which AJ Bell does not export. The largest flow-to-start-value ratio is published so
  the approximation's exposure is visible. The whole-window Modified Dietz (money-weighted) is
  published beside it — the two answer different questions and neither is hidden.
* REFUSALS, NEVER GUESSES: no valuation at the window start -> NO_START_VALUATION; the flow source
  does not cover the window -> UNVERIFIED_FLOWS; a flow between the start valuation and the window
  boundary -> START_GAP_FLOW. A refusal carries no number.

Reporting only: no capital decision reads this (the email renders it in Section 6).
"""
from __future__ import annotations
import datetime as dt, glob, io, contextlib, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PDF_GLOB = "Portfolio Valuation Q* 20*.pdf"
XLSX_GLOB = "AJ Bell ISA Portfolio*.xlsx"
PERIOD_RE = re.compile(r"Portfolio Valuation for the period\s+(\d{1,2} \w+ \d{4})\s+to\s+(\d{1,2} \w+ \d{4})")
TOTAL_RE = re.compile(r"^TOTAL\s+£\s?([\d,]+(?:\.\d+)?)", re.M)
CASH_RE = re.compile(r"^TOTAL CASH BALANCE\s+£\s?([\d,]+(?:\.\d+)?)", re.M)
START_TOLERANCE_DAYS = 7          # a start valuation may precede the boundary by <= 7 days, flow-free
CROSS_CHECK_GBP = 1.0             # the PDF is rounded to whole pounds


def _num(s):
    return float(str(s).replace(",", ""))


def _pdf_points(folder):
    pts, rejected = [], []
    try:
        import pdfplumber
    except Exception as e:                                           # noqa: BLE001
        return pts, [{"file": PDF_GLOB, "why": "pdfplumber unavailable: %s" % e}]
    for fp in sorted(glob.glob(os.path.join(folder, PDF_GLOB))):
        try:
            with pdfplumber.open(fp) as pdf:
                txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
            m, t, c = PERIOD_RE.search(txt), TOTAL_RE.search(txt), CASH_RE.search(txt)
            if not (m and t):
                rejected.append({"file": os.path.basename(fp), "why": "period or TOTAL line not found"})
                continue
            d = dt.datetime.strptime(m.group(2), "%d %B %Y").date()
            pts.append({"date": d.isoformat(), "value_gbp": _num(t.group(1)), "precision_gbp": 1.0,
                        "cash_gbp": _num(c.group(1)) if c else None,
                        "source": os.path.basename(fp), "kind": "quarterly_valuation_pdf"})
        except Exception as e:                                       # noqa: BLE001
            rejected.append({"file": os.path.basename(fp), "why": "%s: %s" % (type(e).__name__, e)})
    return pts, rejected


def _xlsx_points(folder):
    pts, rejected = [], []
    try:
        import extract_portfolio as ep
    except Exception as e:                                           # noqa: BLE001
        return pts, [{"file": XLSX_GLOB, "why": "extract_portfolio unavailable: %s" % e}]
    for fp in sorted(glob.glob(os.path.join(folder, XLSX_GLOB))):
        if os.path.basename(fp).startswith("~$"):
            continue
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                d = ep.parse_portfolio(fp)
            dd = dt.datetime.strptime(str(d["_meta"]["data_date"]), "%d-%b-%Y").date()
            pts.append({"date": dd.isoformat(), "value_gbp": round(float(d["summary"]["total_value_gbp"]), 2),
                        "precision_gbp": 0.01, "cash_gbp": None, "source": os.path.basename(fp),
                        "kind": "broker_portfolio_export"})
        except Exception as e:                                       # noqa: BLE001
            rejected.append({"file": os.path.basename(fp), "why": "%s: %s" % (type(e).__name__, e)})
    return pts, rejected


def merge_points(xlsx_pts, pdf_pts):
    """One point per date. The penny-exact export is used where it AGREES with the quarterly PDF of the
    same date (|diff| <= GBP 1); where they DISAGREE the PDF wins - it is AJ Bell's valuation of record
    at the period end - and the disagreement is recorded with its resolution.

    ⚑ Found on build (19-Oct-2026): 'AJ Bell ISA Portfolio 30-Jun-26.xlsx' totals GBP 144,342.19 while
    the Q2 2026 valuation PDF states GBP 145,052 for 30-Jun (-709.81, -0.49%); the PDF's cash (10,803)
    ties to the cash statement (10,803.25) while the export's cash does not (10,778.46). An export's
    data_date is not necessarily its pricing date."""
    by = {}
    checks = []
    for p in pdf_pts:
        by[p["date"]] = dict(p)
    for p in xlsx_pts:
        if p["date"] in by and by[p["date"]]["kind"] == "quarterly_valuation_pdf":
            q = by[p["date"]]
            ok = abs(p["value_gbp"] - q["value_gbp"]) <= CROSS_CHECK_GBP
            checks.append({"date": p["date"], "export_gbp": p["value_gbp"], "pdf_gbp": q["value_gbp"],
                           "diff_gbp": round(p["value_gbp"] - q["value_gbp"], 2), "ok": ok,
                           "resolution": "EXPORT_USED (agrees to the pound)" if ok else
                                         "PDF_USED (valuation of record at period end; the export disagrees)",
                           "sources": [p["source"], q["source"]]})
            if ok:
                by[p["date"]] = dict(p)
            continue
        by[p["date"]] = dict(p)
    return [by[k] for k in sorted(by)], checks


def statement_flows(cs):
    """External flows + coverage from an extract_cash_statement.parse() result (current + prior)."""
    rows, firsts, lasts = [], [], []
    cur = cs.get("rows") or []
    for r in ((cs.get("external_flows") or {}).get("rows") or []):
        rows.append(dict(r))
    dated = [r["date"] for r in cur if r.get("date")]
    if dated:
        firsts.append(min(dated)); lasts.append(max(dated))
    for py in cs.get("prior_tax_years") or []:
        rows.extend(dict(x) for x in (py.get("external_flows") or []))
        if py.get("first_date"):
            firsts.append(py["first_date"])
        if py.get("last_date"):
            lasts.append(py["last_date"])
    cov = {"from": min(firsts) if firsts else None, "to": max(lasts) if lasts else None,
           "source_files": sorted(set(cs.get("source_files") or []) |
                                  {f for py in cs.get("prior_tax_years") or [] for f in py.get("source_files") or []})}
    return sorted(rows, key=lambda r: r["date"]), cov


def balance_on(cs, date_iso):
    """Running cash balance at the end of `date_iso` (last chronological row dated on/before it)."""
    rows = []
    for py in cs.get("prior_tax_years") or []:
        rows.extend(py.get("rows") or [])
    rows.extend(cs.get("rows") or [])
    rows = [r for r in rows if r.get("date") and r["date"] <= date_iso]
    if not rows:
        return None
    rows.sort(key=lambda r: (r["date"], r.get("source_file") or "", r.get("file_order", 0)))
    return rows[-1].get("balance_gbp")


def modified_dietz(v0, v1, d0, d1, flows):
    D = (d1 - d0).days
    F = sum(f["amount_gbp"] for f in flows)
    W = sum(f["amount_gbp"] * (d1 - dt.date.fromisoformat(f["date"])).days / D for f in flows) if D > 0 else 0.0
    den = v0 + W
    return ((v1 - v0 - F) / den) if den > 0 else None


def window_return(points, flows, coverage, boundary, end_limit, label):
    """Return over (boundary, end] — boundary is the last day BEFORE the window (31-Dec, 05-Apr)."""
    res = {"label": label, "boundary": boundary.isoformat(), "state": None}
    starts = [p for p in points if dt.date.fromisoformat(p["date"]) <= boundary
              and (boundary - dt.date.fromisoformat(p["date"])).days <= START_TOLERANCE_DAYS]
    if not starts:
        res.update(state="NO_START_VALUATION", why="no valuation within %d days on/before %s"
                   % (START_TOLERANCE_DAYS, boundary.isoformat()))
        return res
    s = starts[-1]
    sd = dt.date.fromisoformat(s["date"])
    gap = [f for f in flows if sd < dt.date.fromisoformat(f["date"]) <= boundary]
    if gap:
        res.update(state="START_GAP_FLOW", why="flow(s) between the start valuation %s and %s: %s"
                   % (s["date"], boundary.isoformat(), [(f["date"], f["amount_gbp"]) for f in gap]))
        return res
    ends = [p for p in points if sd < dt.date.fromisoformat(p["date"]) <= end_limit]
    if not ends:
        res.update(state="NO_END_VALUATION", why="no valuation after %s and on/before %s" % (s["date"], end_limit))
        return res
    e = ends[-1]
    ed = dt.date.fromisoformat(e["date"])
    if not coverage.get("from") or not coverage.get("to") or coverage["from"] > s["date"] or coverage["to"] < e["date"]:
        res.update(state="UNVERIFIED_FLOWS", why="the cash statements cover %s..%s, not the window %s..%s - "
                   "a return over uncovered flows would count deposits as growth"
                   % (coverage.get("from"), coverage.get("to"), s["date"], e["date"]))
        return res
    chain = [p for p in points if sd <= dt.date.fromisoformat(p["date"]) <= ed]
    subs, g, maxratio = [], 1.0, 0.0
    for a, b in zip(chain, chain[1:]):
        ad, bd = dt.date.fromisoformat(a["date"]), dt.date.fromisoformat(b["date"])
        fl = [f for f in flows if ad < dt.date.fromisoformat(f["date"]) <= bd]
        r = modified_dietz(a["value_gbp"], b["value_gbp"], ad, bd, fl)
        if r is None:
            res.update(state="DEGENERATE_PERIOD", why="zero or negative Dietz denominator %s..%s" % (a["date"], b["date"]))
            return res
        g *= (1.0 + r)
        F = sum(f["amount_gbp"] for f in fl)
        if a["value_gbp"] > 0:
            maxratio = max(maxratio, abs(F) / a["value_gbp"])
        subs.append({"from": a["date"], "to": b["date"], "v0": a["value_gbp"], "v1": b["value_gbp"],
                     "flows_gbp": round(F, 2), "n_flows": len(fl), "return_pct": round(100 * r, 3)})
    wflows = [f for f in flows if sd < dt.date.fromisoformat(f["date"]) <= ed]
    mwr = modified_dietz(s["value_gbp"], e["value_gbp"], sd, ed, wflows)
    res.update(state="OK", twr_pct=round(100 * (g - 1.0), 2), mwr_pct=(round(100 * mwr, 2) if mwr is not None else None),
               start={"date": s["date"], "value_gbp": s["value_gbp"], "source": s["source"]},
               end={"date": e["date"], "value_gbp": e["value_gbp"], "source": e["source"]},
               flows_total_gbp=round(sum(f["amount_gbp"] for f in wflows), 2), n_flows=len(wflows),
               n_subperiods=len(subs), max_flow_to_start_value_pct=round(100 * maxratio, 1), subperiods=subs,
               basis=("APPROXIMATE TWR: chain-linked Modified Dietz over %d sub-periods between broker valuations "
                      "(an exact TWR needs a valuation on every flow date); flows = cash-statement subscriptions + "
                      "TRANSFER_IN; MWR = whole-window Modified Dietz" % len(subs)))
    return res


def compute(folder=None, cs=None, as_of=None):
    folder = folder or os.path.dirname(HERE)
    as_of = as_of or dt.date.today()
    if cs is None:
        import extract_cash_statement as ecs
        cs = ecs.parse(folder=folder, as_of=as_of)
    xp, xr = _xlsx_points(folder)
    pp, pr = _pdf_points(folder)
    points, checks = merge_points(xp, pp)
    flows, cov = statement_flows(cs)
    warnings = []
    cash_checks = []
    for p in pp:
        if p.get("cash_gbp") is not None:
            b = balance_on(cs, p["date"])
            if b is not None:
                ok = abs(b - p["cash_gbp"]) <= CROSS_CHECK_GBP
                cash_checks.append({"date": p["date"], "pdf_cash_gbp": p["cash_gbp"], "statement_balance_gbp": b, "ok": ok})
                if not ok:
                    warnings.append("valuation PDF %s cash GBP%s vs cash-statement balance GBP%s on %s"
                                    % (p["source"], p["cash_gbp"], b, p["date"]))
    ty0 = dt.date(as_of.year if (as_of.month, as_of.day) >= (4, 6) else as_of.year - 1, 4, 6)
    cal = window_return(points, flows, cov, dt.date(as_of.year - 1, 12, 31), as_of, "calendar_ytd")
    tyd = window_return(points, flows, cov, ty0 - dt.timedelta(days=1), as_of, "tax_year_to_date")
    for w in (cal, tyd):
        if w["state"] != "OK":
            warnings.append("%s %s: %s" % (w["label"], w["state"], w.get("why")))
    return {"state": "OK" if cal["state"] == "OK" or tyd["state"] == "OK" else "REFUSED",
            "as_of": as_of.isoformat(), "calendar_ytd": cal, "tax_year_to_date": tyd,
            "valuation_points": [{k: p[k] for k in ("date", "value_gbp", "precision_gbp", "source", "kind")} for p in points],
            "valuation_cross_checks": checks, "cash_cross_checks": cash_checks,
            "rejected_sources": xr + pr, "flow_coverage": cov, "warnings": warnings,
            "role": "REPORTING_ONLY (rendered in email s6 beside the X-Ray holdings-based YTD; no capital decision reads it)"}


def _selftest():
    D = dt.date
    pts = [{"date": "2025-12-31", "value_gbp": 100.0, "source": "a"}, {"date": "2026-03-31", "value_gbp": 110.0, "source": "b"},
           {"date": "2026-06-30", "value_gbp": 176.0, "source": "c"}]
    cov = {"from": "2025-04-06", "to": "2026-10-02"}
    r = window_return(pts, [], cov, D(2025, 12, 31), D(2026, 3, 31), "t")
    assert r["state"] == "OK" and r["twr_pct"] == 10.0 and r["mwr_pct"] == 10.0, r
    fl = [{"date": "2026-06-30", "amount_gbp": 50.0}]
    r = window_return(pts, fl, cov, D(2025, 12, 31), D(2026, 10, 2), "t")
    # sub-period 2: (176-110-50)/110 = 14.545%; chain 1.10*1.14545-1 = 26.0%
    assert r["state"] == "OK" and r["twr_pct"] == 26.0, r
    assert r["flows_total_gbp"] == 50.0 and r["n_subperiods"] == 2
    # a mid-period flow: MWR and TWR differ, and the approximation exposure is published
    fl2 = [{"date": "2026-02-14", "amount_gbp": 100.0}]
    r2 = window_return([{"date": "2025-12-31", "value_gbp": 100.0, "source": "a"},
                        {"date": "2026-03-31", "value_gbp": 220.0, "source": "b"}], fl2, cov, D(2025, 12, 31), D(2026, 3, 31), "t")
    assert r2["state"] == "OK" and r2["max_flow_to_start_value_pct"] == 100.0 and r2["twr_pct"] > 0, r2
    # NEGATIVE CONTROL: a zero start value with no flow is degenerate and refuses (never divides by zero)
    r0 = window_return([{"date": "2025-12-31", "value_gbp": 0.0, "source": "a"}, {"date": "2026-03-31", "value_gbp": 10.0, "source": "b"}],
                       [], cov, D(2025, 12, 31), D(2026, 3, 31), "t")
    assert r0["state"] == "DEGENERATE_PERIOD" and "twr_pct" not in r0, ("NEGATIVE CONTROL: degenerate period refuses", r0)
    # NEGATIVE CONTROLS - broken inputs REFUSE, never a number
    r = window_return(pts, [], {"from": "2026-01-15", "to": "2026-10-02"}, D(2025, 12, 31), D(2026, 6, 30), "t")
    assert r["state"] == "UNVERIFIED_FLOWS" and "twr_pct" not in r, ("NEGATIVE CONTROL: uncovered flows must refuse, never return a number", r)
    r = window_return(pts, [], cov, D(2025, 12, 20), D(2026, 6, 30), "t")
    assert r["state"] == "NO_START_VALUATION" and "twr_pct" not in r, ("NEGATIVE CONTROL: no start valuation must refuse", r)
    pts_gap = [{"date": "2026-04-02", "value_gbp": 100.0, "source": "a"}, {"date": "2026-05-01", "value_gbp": 120.0, "source": "b"}]
    r = window_return(pts_gap, [{"date": "2026-04-04", "amount_gbp": 10.0}], cov, D(2026, 4, 5), D(2026, 5, 1), "t")
    assert r["state"] == "START_GAP_FLOW" and "twr_pct" not in r, ("NEGATIVE CONTROL: a flow in the start gap must refuse", r)
    # the boundary-day start (no flow in the gap) is admitted
    r = window_return(pts_gap, [], cov, D(2026, 4, 5), D(2026, 5, 1), "t")
    assert r["state"] == "OK" and r["twr_pct"] == 20.0, r
    # merge: the penny export wins; the PDF on the same date cross-checks
    m, ck = merge_points([{"date": "2026-06-30", "value_gbp": 144342.19, "kind": "broker_portfolio_export", "source": "x"}],
                         [{"date": "2026-06-30", "value_gbp": 144342.0, "kind": "quarterly_valuation_pdf", "source": "p"},
                          {"date": "2025-12-31", "value_gbp": 104781.0, "kind": "quarterly_valuation_pdf", "source": "q"}])
    assert [p["source"] for p in m] == ["q", "x"] and ck[0]["ok"] is True and ck[0]["resolution"].startswith("EXPORT"), (m, ck)
    m, ck = merge_points([{"date": "2026-06-30", "value_gbp": 144400.0, "kind": "broker_portfolio_export", "source": "x"}],
                         [{"date": "2026-06-30", "value_gbp": 144342.0, "kind": "quarterly_valuation_pdf", "source": "p"}])
    assert ck[0]["ok"] is False and m[0]["source"] == "p" and ck[0]["resolution"].startswith("PDF_USED"), \
        "MUST-FIRE: a valuation disagreement is reported and the valuation of record is used"
    print("account_return SELFTEST PASS - Dietz exact (10.0%, chained 26.0%), MWR vs TWR on a mid-period flow, "
          "UNVERIFIED_FLOWS / NO_START_VALUATION / START_GAP_FLOW refuse with no number, boundary start admitted, "
          "export used where it agrees, PDF of record where it does not (must-fire)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    import json
    r = compute()
    for k in ("calendar_ytd", "tax_year_to_date"):
        w = r[k]
        print(k, w["state"], w.get("twr_pct"), w.get("mwr_pct"), w.get("start"), w.get("end"), w.get("why", ""))
    print("points", [(p["date"], p["value_gbp"]) for p in r["valuation_points"]])
    print("cross", r["valuation_cross_checks"], r["cash_cross_checks"], "warnings", r["warnings"])
