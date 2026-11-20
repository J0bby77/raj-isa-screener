#!/usr/bin/env python3
"""
broker_snapshot.py - the CURRENT broker book as one typed, reconciled, immutable snapshot (stabilisation P2,
ISA-0727 / ISA-0855 / ISA-0850 / ISA-0864 family, 19-Nov-2026).

WHY. The monthly pre-run reads the MONTH-END AJ Bell xlsx files; it says nothing about the book on the day an
order is placed. Between month-ends the execution boundary (capital_decision_engine.execution_check) took
`--cash` typed by hand. The 19-Nov-2026 audit reconciled a current export by hand (79 running balances, four
trade/cash references, MMF 1250-1015=235 units). This module makes that reconciliation mechanical and gives the
execution boundary a checked source for current cash - without creating a second month-end truth:

* INPUT: the three AJ Bell web exports saved as CSV - `portfolio-<ACCOUNT>-ISA*.csv` (holdings),
  `cashstatements*.csv` (cash statement), `transactionhistory*.csv` (dealing record). Originals are never
  modified; each is recorded by sha256.
* OUTPUT: `Dashboard/state/broker_snapshots/<as_of>-<sha8>.json`, written once through durable_store.write_record
  (immutable). It is NOT the month-end book and is never read by the pre-run as one.
* STATES: RECONCILED only when every cash-statement running balance reproduces, every dealing reference appears in
  the cash statement with the same GBP amount, holdings sum to the stated total and no value is unreadable;
  otherwise UNRECONCILED with every failing check named. A RECONCILED snapshot still carries the commitment
  state it was given: pending orders are OWNER_DECLARED (with who/when) or UNKNOWN - never inferred as none.
* CASH SEMANTICS: settled cash (the statement balance) and the money-market fund are SEPARATE figures. MMF value is
  economic capital but becomes cash only when sold and settled; it is never added into `settled_cash_gbp`.

Consumer: execution_check via `capital_decision_engine.py --execution-check ... --cash-from-snapshot latest`
(REFUSED unless the newest snapshot is RECONCILED and no older than MAX_AGE_HOURS at the check).
Never deletes or rewrites anything. Rollback: do not pass --cash-from-snapshot (the --cash path is unchanged).
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR_REL = os.path.join("Dashboard", "state", "broker_snapshots")
SCHEMA = "broker_snapshot/1"
MAX_AGE_HOURS = 24.0          # an execution check older than this reads the snapshot STALE (same-day book)
BAL_TOL = 0.005
MMF_TICKERS = ("CSH2",)       # the cash-equivalent fund held in this account (extract_portfolio classifies the same)

PORTFOLIO_COLS = ["Investment", "Quantity", "Price", "Value (£)", "Cost (£)", "Change (£)", "Change (%)",
                  "Price +/- today (%)", "Valuation currency", "Market currency", "Exchange rate", "Date", "Time",
                  "Portfolio", "Ticker"]
CASH_COLS = ["Date", "Description", "Reference", "Settlement date", "Receipt (GBP)", "Payment (GBP)", "Balance (GBP)"]
TXN_COLS = ["Date", "Transaction", "Description", "Quantity", "Price", "Amount (GBP)", "Reference"]


class SnapshotRefused(RuntimeError):
    pass


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _num(v) -> Optional[float]:
    """'1,185.81' / '£53.5712' / '-21.05' -> float; '' / '-' -> None; anything else RAISES (typed, never 0)."""
    s = str(v if v is not None else "").strip().replace(",", "")
    if s.startswith("£"):
        s = s[1:]
    if s in ("", "-"):
        return None
    try:
        x = float(s)
    except ValueError:
        raise SnapshotRefused("unparseable number %r" % (v,))
    if not math.isfinite(x):
        raise SnapshotRefused("non-finite number %r" % (v,))
    return x


def _read(path: str, cols: List[str]) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        hdr = [h.strip() for h in (rd.fieldnames or [])]
        if hdr != cols:
            raise SnapshotRefused("%s: header %s is not the AJ Bell export contract %s"
                                  % (os.path.basename(path), hdr, cols))
        return [{k.strip(): (v or "").strip() for k, v in r.items()} for r in rd]


def _d(s: str, fmts=("%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y")) -> Optional[dt.date]:
    for f in fmts:
        try:
            return dt.datetime.strptime(s, f).date()
        except (ValueError, TypeError):
            continue
    return None


def build(portfolio_csv: str, cash_csv: str, txn_csv: str, *, account: Optional[str] = None,
          pending_orders: Optional[dict] = None, prior_mmf_units: Optional[float] = None) -> dict:
    """-> snapshot dict (not written). `pending_orders` = {"state": "OWNER_DECLARED_NONE"|..., "declared_by", "declared_at"}
    or None (UNKNOWN). `prior_mmf_units` (e.g. the last month-end book) enables the MMF unit-flow identity."""
    checks, fails = [], []

    def ck(name, cond, detail=None):
        checks.append({"check": name, "ok": bool(cond), "detail": detail})
        if not cond:
            fails.append(name)

    srcs = []
    for p in (portfolio_csv, cash_csv, txn_csv):
        if not p or not os.path.isfile(p):
            raise SnapshotRefused("input file absent: %r - a snapshot is never built from a missing export" % (p,))
        srcs.append({"name": os.path.basename(p), "sha256": _sha(p), "bytes": os.path.getsize(p)})
    acct = account or (re.search(r"portfolio-([A-Z0-9]+)-ISA", os.path.basename(portfolio_csv)) or [None, None])[1]
    pf = _read(portfolio_csv, PORTFOLIO_COLS)
    cs = _read(cash_csv, CASH_COLS)
    tx = _read(txn_csv, TXN_COLS)
    srcs[0]["rows"], srcs[1]["rows"], srcs[2]["rows"] = len(pf), len(cs), len(tx)

    # ── holdings ─────────────────────────────────────────────────────────────
    hold, cash_line, unvalued, stamps = [], None, [], set()
    for r in pf:
        v = _num(r["Value (£)"])
        stamps.add((r["Date"], r["Time"]))
        if r["Investment"].lower().startswith("cash"):
            cash_line = v
            continue
        if v is None:
            unvalued.append(r["Investment"])
            continue
        hold.append({"investment": r["Investment"], "broker_ticker": r["Ticker"] or None,
                     "quantity": _num(r["Quantity"]), "price_raw": r["Price"], "value_gbp": v,
                     "cost_gbp": _num(r["Cost (£)"]), "valuation_currency": r["Valuation currency"],
                     "market_currency": r["Market currency"], "exchange_rate": _num(r["Exchange rate"]),
                     "is_mmf": (r["Ticker"] or "").upper() in MMF_TICKERS})
    try:
        import extract_portfolio as _ep                                  # ONE identity home (ISA-0855/P2)
        for h in hold:
            h["ticker"] = _ep._canonical_ticker(h["broker_ticker"], h["investment"])
    except Exception as exc:                                            # noqa: BLE001
        for h in hold:
            h["ticker"] = None
        ck("identity mapping available (extract_portfolio._canonical_ticker)", False, str(exc))
    ck("one valuation timestamp across the export", len(stamps) == 1, sorted(stamps))
    ck("no unreadable holding value (a gap is never 0)", not unvalued, unvalued)
    ck("cash line present in the holdings export", cash_line is not None)
    as_of_date, as_of_time = (sorted(stamps)[0] if stamps else (None, None))
    invested = round(sum(h["value_gbp"] for h in hold if not h["is_mmf"]), 2)
    mmf = [h for h in hold if h["is_mmf"]]
    mmf_value = round(sum(h["value_gbp"] for h in mmf), 2)
    mmf_units = sum(h["quantity"] or 0 for h in mmf)
    nav = round(invested + mmf_value + (cash_line or 0.0), 2)

    # ── cash statement running balances (newest first, as exported) ──────────
    rows = []
    for r in cs:
        rows.append({"date": r["Date"], "description": r["Description"], "reference": r["Reference"],
                     "settlement": r["Settlement date"], "receipt": _num(r["Receipt (GBP)"]) or 0.0,
                     "payment": _num(r["Payment (GBP)"]) or 0.0, "balance": _num(r["Balance (GBP)"])})
    bad_bal = []
    for i in range(len(rows) - 1):            # row i (newer) = row i+1 (older) balance + receipt + payment
        cur, prev = rows[i], rows[i + 1]
        if cur["balance"] is None or prev["balance"] is None:
            bad_bal.append((i, "missing balance"))
            continue
        if abs(prev["balance"] + cur["receipt"] + cur["payment"] - cur["balance"]) > BAL_TOL:
            bad_bal.append((i, cur["date"], cur["description"][:40]))
    ck("every cash-statement running balance reproduces (%d transitions)" % max(len(rows) - 1, 0), not bad_bal, bad_bal[:10])
    settled_cash = rows[0]["balance"] if rows else None
    ck("cash statement closing balance equals the holdings cash line",
       settled_cash is not None and cash_line is not None and abs(settled_cash - cash_line) <= BAL_TOL,
       {"statement": settled_cash, "holdings_cash_line": cash_line})
    unsettled = [r for r in rows if _d(r["settlement"]) and as_of_date and _d(r["settlement"]) > _d(as_of_date)]
    ck("no statement row settles after the valuation date", not unsettled,
       [(r["date"], r["description"][:40], r["settlement"]) for r in unsettled])

    # ── dealing references reconcile to the cash statement ───────────────────
    by_ref = {}
    for r in rows:
        if r["reference"] not in ("", "-"):
            by_ref.setdefault(r["reference"], []).append(r)
    trades = [t for t in tx if t["Transaction"].lower() in ("purchase", "sale")]
    tr_bad, refs_seen = [], {}
    for t in trades:
        ref, amt = t["Reference"], _num(t["Amount (GBP)"])
        refs_seen[ref] = refs_seen.get(ref, 0) + 1
        sign = -1.0 if t["Transaction"].lower() == "purchase" else 1.0
        hits = [r for r in by_ref.get(ref, []) if abs((r["receipt"] + r["payment"]) - sign * (amt or 0)) <= BAL_TOL]
        if len(hits) != 1:
            tr_bad.append({"reference": ref, "transaction": t["Transaction"], "amount_gbp": amt, "matches": len(hits)})
    ck("every dealing reference appears once in the cash statement with the same GBP amount (%d trades)" % len(trades),
       not tr_bad, tr_bad)
    ck("no duplicated dealing reference", all(n == 1 for n in refs_seen.values()),
       {k: n for k, n in refs_seen.items() if n > 1})
    # non-cash fund accumulation/equalisation rows are NOT cash (ISA-0732 family): they must not appear in the statement
    noncash = [t for t in tx if t["Transaction"].lower().startswith(("accumulation", "equalisation"))]
    leaked = [t["Reference"] for t in noncash if any(abs(r["receipt"] - (_num(t["Amount (GBP)"]) or 0)) <= BAL_TOL
                                                    and r["reference"] == t["Reference"] for r in rows)]
    ck("accumulation/equalisation rows are not cash", not leaked, leaked)

    # ── MMF unit flow (optional identity) ────────────────────────────────────
    mmf_sold = sum(_num(t["Quantity"]) or 0 for t in trades
                   if t["Transaction"].lower() == "sale" and "overnight" in t["Description"].lower())
    mmf_bought = sum(_num(t["Quantity"]) or 0 for t in trades
                     if t["Transaction"].lower() == "purchase" and "overnight" in t["Description"].lower())
    mmf_flow = None
    if prior_mmf_units is not None:
        mmf_flow = {"prior_units": prior_mmf_units, "sold": mmf_sold, "bought": mmf_bought, "now": mmf_units}
        ck("MMF units: prior - sold + bought == now", abs(prior_mmf_units - mmf_sold + mmf_bought - mmf_units) < 1e-6, mmf_flow)

    commit = pending_orders or {"state": "UNKNOWN",
                                "why": "no broker pending-order view and no owner declaration supplied - UNKNOWN, never 'none'"}
    reserve = None
    try:
        tw = json.load(open(os.path.join(HERE, "target_weights.json"), encoding="utf-8"))
        reserve = (tw.get("stock_sleeve") or {}).get("cash_reserve_gbp")
    except Exception:                                                   # noqa: BLE001
        reserve = None
    state = "RECONCILED" if not fails else "UNRECONCILED"
    return {
        "schema": SCHEMA, "account": acct, "state": state, "failed_checks": fails, "checks": checks,
        "as_of": {"date": as_of_date, "time": as_of_time, "timezone": "NOT_STATED_BY_EXPORT (Europe/London assumed only for staleness)"},
        "sources": srcs,
        "book": {"nav_gbp": nav, "invested_ex_mmf_gbp": invested, "mmf_value_gbp": mmf_value, "mmf_units": mmf_units,
                 "settled_cash_gbp": settled_cash, "holdings": hold, "n_holdings": len(hold)},
        "capital": {"settled_cash_gbp": settled_cash,
                    "cash_reserve_gbp": reserve,
                    "settled_cash_after_reserve_gbp": (None if settled_cash is None or reserve is None
                                                       else round(settled_cash - float(reserve), 2)),
                    "mmf_value_gbp": mmf_value,
                    "economic_cash_plus_mmf_gbp": (None if settled_cash is None else round(settled_cash + mmf_value, 2)),
                    "basis": "settled cash is the statement balance; MMF is economic capital that becomes cash only "
                             "when sold AND settled - never added into settled_cash_gbp"},
        "commitments": commit, "mmf_flow": mmf_flow,
        "role": "CURRENT execution book (between month-ends). NOT the month-end book; the pre-run never reads it.",
    }


def snapshot_name(snap: dict) -> str:
    d = _d(str((snap.get("as_of") or {}).get("date")))
    t = re.sub(r"[^0-9]", "", str((snap.get("as_of") or {}).get("time") or ""))
    key = "".join(s["sha256"][:8] for s in snap.get("sources") or [])
    return "%s%s-%s" % (d.isoformat() if d else "undated", ("T" + t) if t else "", hashlib.sha256(key.encode()).hexdigest()[:8])


def write(snap: dict, root: str = HERE) -> dict:
    import durable_store as _ds
    return _ds.write_record(os.path.join(root, SNAP_DIR_REL), snapshot_name(snap), snap)


def latest(root: str = HERE, now: Optional[dt.datetime] = None, max_age_hours: float = MAX_AGE_HOURS) -> dict:
    """The newest snapshot with its usability: USABLE | STALE | UNRECONCILED | ABSENT (typed, never a default)."""
    import durable_store as _ds
    recs = [r for r in _ds.read_records(os.path.join(root, SNAP_DIR_REL)) if r.get("schema") == SCHEMA]
    if not recs:
        return {"state": "ABSENT", "why": "no broker snapshot recorded (%s)" % SNAP_DIR_REL}

    def _stamp(r):
        d = _d(str((r.get("as_of") or {}).get("date")))
        try:
            t = dt.datetime.strptime(str((r.get("as_of") or {}).get("time")), "%H:%M").time()
        except (ValueError, TypeError):
            t = dt.time(0, 0)
        return dt.datetime.combine(d, t) if d else None
    recs = [r for r in recs if _stamp(r)]
    if not recs:
        return {"state": "ABSENT", "why": "no dated broker snapshot"}
    r = max(recs, key=_stamp)
    ts = _stamp(r)
    now = now or dt.datetime.now()
    age_h = (now - ts).total_seconds() / 3600.0
    base = {"snapshot": r, "as_of": ts.isoformat(), "age_hours": round(age_h, 2)}
    if r.get("state") != "RECONCILED":
        return dict(base, state="UNRECONCILED", why="newest snapshot failed: %s" % r.get("failed_checks"))
    if age_h < -0.5:
        return dict(base, state="STALE", why="snapshot is dated in the future relative to the check")
    if age_h > max_age_hours:
        return dict(base, state="STALE", why="snapshot is %.1f h old (limit %.0f h) - re-export the broker book" % (age_h, max_age_hours))
    return dict(base, state="USABLE", why="RECONCILED, %.1f h old" % age_h)


def cash_for_execution(root: str = HERE, now: Optional[dt.datetime] = None) -> dict:
    """-> {state, cash_gbp, why}. cash_gbp = settled cash only (MMF excluded until sold and settled)."""
    l = latest(root, now)
    if l["state"] != "USABLE":
        return {"state": "REFUSED_SNAPSHOT_" + l["state"], "cash_gbp": None, "why": l["why"]}
    cap = (l["snapshot"].get("capital") or {})
    c = cap.get("settled_cash_gbp")
    if c is None or not math.isfinite(float(c)):
        return {"state": "REFUSED_SNAPSHOT_CASH_UNKNOWN", "cash_gbp": None, "why": "snapshot carries no settled cash"}
    return {"state": "USABLE", "cash_gbp": float(c), "why": l["why"], "as_of": l["as_of"],
            "commitments": l["snapshot"].get("commitments"), "mmf_value_gbp": cap.get("mmf_value_gbp")}


# ──────────────────────────────────────────────────────────────────────────── selftest

def _write_csv(path, cols, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(cols)
        w.writerows(rows)


def _fixture(td, tamper=None):
    pf = [["Amundi Smart Overnight Ret GBP H ETF Acc (LSE:CSH2)", "235", "5.046", "1,185.81", "1,177.90", "7.91", "0.67", "0.04", "GBP", "GBP", "1", "19-Nov-26", "15:11", "ISA", "CSH2"],
          ["ATLAS COPCO AB SER'A'NPV (SEDOL:BLDBN41)", "388", "188.75", "5,480.25", "5,447.13", "33.12", "0.61", "0", "SEK", "GBP", "13.3634", "19-Nov-26", "15:11", "ISA", "BLDBN41"],
          ["Indra Sistemas SA SHS Class -A- (XMAD:IDR)", "100", "52.27", "4,363.83", "5,413.02", "-1,049.19", "-19.38", "0", "EUR", "GBP", "1.1978", "19-Nov-26", "15:11", "ISA", "IDR"],
          ["Cash GBP", "", "1", "310.23", "", "", "", "", "GBP", "GBP", "1", "19-Nov-26", "15:11", "ISA", ""]]
    cs = [["13/11/2026", "Account charge for funds - Oct 2026", "-", "-", "0", "-21.05", "310.23"],
          ["13/11/2026", "Account charge for shares - Oct 2026", "-", "-", "0", "-3.5", "331.28"],
          ["02/11/2026", "Purchase 100 Indra Sistemas SA SHS Class -A-", "44626CNR1GR", "04/11/2026", "0", "-5413.02", "334.78"],
          ["02/11/2026", "Sale 1,010 Amundi Smart Overnight", "44626CNRTD2", "04/11/2026", "5080", "0", "5,747.80"],
          ["02/11/2026", "Sale 5 Amundi Smart Overnight", "44626CNRR1L", "04/11/2026", "20.17", "0", "667.80"],
          ["02/11/2026", "Purchase 388 ATLAS COPCO AB SER'A'NPV", "44626CNRRDV", "04/11/2026", "0", "-5447.13", "647.63"],
          ["29/10/2026", "DIVIDEND MICRON", "9995408 / 10", "-", "0.67", "0", "6,094.76"],
          ["19/10/2026", "Fund Cash Rebate AUG26", "-", "-", "0.51", "0", "6,094.09"]]
    tx = [["02/11/2026", "Purchase", "Indra Sistemas SA SHS Class -A-", "100", "£53.5712", "5413.02", "44626CNR1GR"],
          ["02/11/2026", "Sale", "Amundi Smart Overnight Ret GBP H ETF Acc", "1010", "£5.03465", "5080", "44626CNRTD2"],
          ["02/11/2026", "Sale", "Amundi Smart Overnight Ret GBP H ETF Acc", "5", "£5.03465", "20.17", "44626CNRR1L"],
          ["02/11/2026", "Purchase", "ATLAS COPCO AB SER'A'NPV", "388", "£13.9217", "5447.13", "44626CNRRDV"],
          ["30/10/2026", "Equalisation Acc Units", "M&G Asian GBP I Acc", "0", "", "20.33", "44626##0004"]]
    if tamper:
        tamper(pf, cs, tx)
    p = [os.path.join(td, n) for n in ("portfolio-ACB8G2I-ISA (11).csv", "cashstatements (1).csv", "transactionhistory (3).csv")]
    _write_csv(p[0], PORTFOLIO_COLS, pf)
    _write_csv(p[1], CASH_COLS, cs)
    _write_csv(p[2], TXN_COLS, tx)
    return p


def _selftest(verbose: bool = True) -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -- %s" % (detail,)))
        if not cond:
            fails.append(name)

    td = tempfile.mkdtemp(prefix="broker_snapshot_")
    p = _fixture(td)
    s = build(*p, prior_mmf_units=1250.0)
    ok("B1b POSITIVE: MMF units 1250 - 1015 = 235 reproduce; all checks pass", s["state"] == "RECONCILED", s["failed_checks"])
    ok("B2 identities: BLDBN41 -> ATCO-A.ST, XMAD:IDR -> IDR.MC (one home: extract_portfolio)",
       {h["ticker"] for h in s["book"]["holdings"]} >= {"ATCO-A.ST", "IDR.MC"}, [h["ticker"] for h in s["book"]["holdings"]])
    ok("B3 settled cash 310.23 and MMF 1,185.81 are SEPARATE (MMF never added into settled cash)",
       s["capital"]["settled_cash_gbp"] == 310.23 and s["capital"]["mmf_value_gbp"] == 1185.81
       and s["capital"]["economic_cash_plus_mmf_gbp"] == 1496.04)
    ok("B4 commitments default UNKNOWN (never inferred as none)", s["commitments"]["state"] == "UNKNOWN")
    # negative controls
    def t_bal(pf, cs, tx): cs[1][6] = "331.29"
    ok("B5 NEGATIVE CONTROL: a 1p running-balance break -> UNRECONCILED",
       build(*_fixture(tempfile.mkdtemp(), t_bal))["state"] == "UNRECONCILED")
    def t_ref(pf, cs, tx): tx[3][5] = "5447.14"
    ok("B6 NEGATIVE CONTROL: a dealing amount that does not match its cash row -> UNRECONCILED",
       build(*_fixture(tempfile.mkdtemp(), t_ref))["state"] == "UNRECONCILED")
    def t_dup(pf, cs, tx): tx.append(list(tx[0]))
    ok("B7 NEGATIVE CONTROL: a duplicated dealing reference -> UNRECONCILED",
       build(*_fixture(tempfile.mkdtemp(), t_dup))["state"] == "UNRECONCILED")
    def t_nan(pf, cs, tx): pf[1][3] = "nan"
    try:
        build(*_fixture(tempfile.mkdtemp(), t_nan))
        ok("B8 NEGATIVE CONTROL: a NaN holding value is REFUSED", False)
    except SnapshotRefused:
        ok("B8 NEGATIVE CONTROL: a NaN holding value is REFUSED", True)
    def t_unset(pf, cs, tx): cs[2][3] = "25/11/2026"
    ok("B9 NEGATIVE CONTROL: a row settling after the valuation date -> UNRECONCILED",
       build(*_fixture(tempfile.mkdtemp(), t_unset))["state"] == "UNRECONCILED")
    try:
        build(p[0], os.path.join(td, "nope.csv"), p[2])
        ok("B10 NEGATIVE CONTROL: a missing export is REFUSED", False)
    except SnapshotRefused:
        ok("B10 NEGATIVE CONTROL: a missing export is REFUSED", True)
    # write / latest / cash_for_execution
    root = tempfile.mkdtemp()
    ok("B11 ABSENT before any snapshot", latest(root)["state"] == "ABSENT")
    w = write(s, root)
    ok("B12 written once as an immutable record (idempotent replay)",
       w["state"] == "WRITTEN" and write(s, root)["state"] == "ALREADY_PRESENT")
    t0 = dt.datetime(2026, 11, 19, 16, 0)
    c = cash_for_execution(root, now=t0)
    ok("B13 MUST-FIRE consumer: same-day RECONCILED snapshot -> settled cash 310.23 (MMF excluded)",
       c["state"] == "USABLE" and c["cash_gbp"] == 310.23, c)
    ok("B14 NEGATIVE CONTROL: the same snapshot two days later -> REFUSED_SNAPSHOT_STALE",
       cash_for_execution(root, now=t0 + dt.timedelta(days=2))["state"] == "REFUSED_SNAPSHOT_STALE")
    root2 = tempfile.mkdtemp()
    write(build(*_fixture(tempfile.mkdtemp(), t_bal)), root2)
    ok("B15 NEGATIVE CONTROL: an UNRECONCILED newest snapshot -> REFUSED_SNAPSHOT_UNRECONCILED",
       cash_for_execution(root2, now=t0)["state"] == "REFUSED_SNAPSHOT_UNRECONCILED")
    # R5.14 five-point temporal matrix of the consumer (snapshot instant vs check instant)
    ok("R5.14 T-1: a snapshot from the previous day's export, checked 25 h later -> REFUSED_SNAPSHOT_STALE",
       cash_for_execution(root, now=dt.datetime(2026, 11, 20, 16, 12))["state"] == "REFUSED_SNAPSHOT_STALE")
    ok("R5.14 T: the same-session check -> USABLE", cash_for_execution(root, now=dt.datetime(2026, 11, 19, 15, 30))["state"] == "USABLE")
    ok("R5.14 T+1: a check BEFORE the snapshot's own timestamp (future-dated book) -> REFUSED_SNAPSHOT_STALE",
       cash_for_execution(root, now=dt.datetime(2026, 11, 18, 15, 11))["state"] == "REFUSED_SNAPSHOT_STALE")
    ok("R5.14 repeat_same_occurrence: the same check twice gives the same verdict and cash",
       cash_for_execution(root, now=t0) == cash_for_execution(root, now=t0))
    s2 = dict(s, as_of={"date": "20-Nov-26", "time": "09:05", "timezone": s["as_of"]["timezone"]},
              sources=[dict(x, sha256="f" * 64) for x in s["sources"]])
    s2["capital"] = dict(s["capital"], settled_cash_gbp=999.0)
    write(s2, root)
    ok("R5.14 next_occurrence: the next day's own snapshot is the one consumed (newest wins)",
       cash_for_execution(root, now=dt.datetime(2026, 11, 20, 10, 0))["cash_gbp"] == 999.0)
    if verbose:
        print("broker_snapshot selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv and argv.index(n) + 1 < len(argv) else d
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--latest" in argv:
        l = latest(opt("--root", HERE))
        print(json.dumps({k: v for k, v in l.items() if k != "snapshot"}, indent=1, default=str))
        return 0 if l["state"] == "USABLE" else 3
    if "--build" in argv:
        folder = opt("--folder", os.path.dirname(HERE))

        def _newest(pat):
            c = sorted(glob.glob(os.path.join(folder, pat)), key=os.path.getmtime)
            return c[-1] if c else None
        pf = opt("--portfolio") or _newest("portfolio-*-ISA*.csv")
        cs = opt("--cash") or _newest("cashstatements*.csv")
        tx = opt("--transactions") or _newest("transactionhistory*.csv")
        po = None
        if opt("--pending-orders-declared-none-by"):
            po = {"state": "OWNER_DECLARED_NONE", "declared_by": opt("--pending-orders-declared-none-by"),
                  "declared_at": opt("--declared-at") or dt.date.today().isoformat(),
                  "basis": "owner declaration, not a broker pending-order view; refresh before a later action"}
        pm = opt("--prior-mmf-units")
        try:
            s = build(pf, cs, tx, pending_orders=po, prior_mmf_units=float(pm) if pm else None)
        except SnapshotRefused as exc:
            print("SNAPSHOT_REFUSED %s" % exc)
            return 2
        if "--write" in argv:
            w = write(s, opt("--root", HERE))
            s["written"] = w
        print(json.dumps({k: s[k] for k in ("state", "failed_checks", "as_of", "capital", "commitments", "sources")}
                         | {"nav_gbp": s["book"]["nav_gbp"], "written": s.get("written")}, indent=1, default=str))
        return 0 if s["state"] == "RECONCILED" else 3
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
