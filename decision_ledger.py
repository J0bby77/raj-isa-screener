#!/usr/bin/env python3
"""
decision_ledger.py — the ISA decision ledger (redesign Part 1 §5 / CONTRACTS #7, H1/H3).

Logs EVERY monthly-review decision — buys, trims, sells, top-ups, holds, AND passes
(the road not taken is the signal) — capturing the scores / gates / flags AT decision
time, then appends monthly mark-to-market so per-signal information coefficient can be
measured at quarterly calibration. Append-only JSON; safe to import and call from the
review step. NOT yet wired into any scheduled run — pure additive new module.

Contract (CONTRACTS #7), per entry:
  date, ticker, route, decision{buy,trim,sell,top_up,PASS,hold},
  scores_at_decision{source_score,F,Q,V,native_score},
  gates_at_decision[], flags_at_decision[],
  thesis, catalyst|null, expected_review_date,
  mtm:[{date, price, return_pct, thesis_status}]
"""
from __future__ import annotations
import argparse, json, os, datetime

# Contract decision vocabulary. PASS is upper-case (the "road not taken"); the rest lower.
DECISIONS = {"buy", "trim", "sell", "top_up", "PASS", "hold"}
SCHEMA_VERSION = "1.0"


def _today() -> str:
    return datetime.date.today().isoformat()


def _norm_decision(decision: str) -> str:
    """Map any casing to the contract form (PASS upper, others lower). Raises on unknown."""
    d = str(decision).strip()
    norm = "PASS" if d.lower() == "pass" else d.lower()
    if norm not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}, got {decision!r}")
    return norm


def load_ledger(path: str) -> dict:
    """Return the ledger dict {schema_version, entries:[...]} — tolerant of a missing or
    legacy (bare-list) file so a first run never crashes."""
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    if isinstance(d, dict) and isinstance(d.get("entries"), list):
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d
    if isinstance(d, list):                       # legacy bare list
        return {"schema_version": SCHEMA_VERSION, "entries": d}
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def save_ledger(ledger: dict, path: str) -> None:
    """Atomic write (tmp + replace) so a crash mid-write never corrupts the ledger."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, default=str)
    os.replace(tmp, path)


def entry_id(date: str, ticker: str, decision: str) -> str:
    return f"{date}::{(ticker or '').upper()}::{decision}"


def log_decision(path, ticker, route, decision, scores=None, gates=None, flags=None,
                 thesis="", catalyst=None, expected_review_date=None, date=None,
                 dedupe=True) -> dict:
    """Append one decision entry and persist. Idempotent per (date, ticker, decision)
    when dedupe=True (re-running a review in the same month won't duplicate). Returns
    the entry dict (existing one if it was a dedupe hit)."""
    norm = _norm_decision(decision)
    date = date or _today()
    ledger = load_ledger(path)
    eid = entry_id(date, ticker, norm)
    if dedupe:
        for e in ledger["entries"]:
            if e.get("_id") == eid:
                return e
    s = scores or {}
    entry = {
        "_id": eid,
        "date": date,
        "ticker": ticker,
        "route": route,
        "decision": norm,
        # The ledger records a RECOMMENDATION, never an assumed trade. Execution is confirmed
        # retrospectively from the broker ISA PDF/Excel the FOLLOWING month (reconcile_executions).
        "execution_status": "recommended",
        "executed_confirmed_date": None,
        "scores_at_decision": {
            "source_score": s.get("source_score"),
            "F": s.get("F"),
            "Q": s.get("Q"),
            "V": s.get("V"),
            "native_score": s.get("native_score"),
        },
        "gates_at_decision": list(gates or []),
        "flags_at_decision": list(flags or []),
        "thesis": thesis or "",
        "catalyst": catalyst,
        "expected_review_date": expected_review_date,
        "mtm": [],
    }
    ledger["entries"].append(entry)
    save_ledger(ledger, path)
    return entry


def append_mtm(path, ticker, price, return_pct=None, thesis_status=None,
               date=None, decision=None) -> dict | None:
    """Append a monthly mark-to-market point to the latest matching entry for `ticker`
    (optionally filtered to a specific decision). Returns the updated entry, or None if
    no matching entry exists yet."""
    date = date or _today()
    ledger = load_ledger(path)
    cand = [e for e in ledger["entries"]
            if (e.get("ticker") or "").upper() == (ticker or "").upper()
            and (decision is None or e.get("decision") == decision)]
    if not cand:
        return None
    entry = sorted(cand, key=lambda e: e.get("date", ""))[-1]
    entry.setdefault("mtm", []).append({
        "date": date,
        "price": price,
        "return_pct": return_pct,
        "thesis_status": thesis_status,
    })
    save_ledger(ledger, path)
    return entry


BUY_LIKE = {"buy", "top_up", "etf_tactical"}   # B4/P3 (18-Jul-26): Category-8 action vocab
SELL_LIKE = {"sell", "trim"}


def _as_qty_map(holdings):
    """Accept {ticker: quantity} OR a bare [ticker] list (presence only -> quantity None)."""
    if isinstance(holdings, dict):
        return {str(k).strip().upper(): v for k, v in holdings.items()}
    return {str(t).strip().upper(): None for t in (holdings or [])}


def reconcile_executions(path, current_holdings, prior_holdings=None, date=None) -> dict:
    """Confirm — from BROKER TRUTH (next month's actual holdings) — which recommendations were taken.
    The system NEVER assumes execution. Pass `current_holdings` as {ticker: quantity} (or a bare ticker
    list = presence only). `prior_holdings` ({ticker: quantity}) lets top_up/trim be confirmed by a
    QUANTITY change; without it (or without quantities) top_up/trim are left `execution_unconfirmed`
    because presence alone cannot prove a size change. Reconciles entries still `recommended`:
      buy    : executed if the ticker is now held, else not_executed
      sell   : executed if the ticker is now NOT held, else not_executed
      top_up : executed if current qty > prior qty (needs both quantities), else not_executed / unconfirmed
      trim   : executed if current qty < prior qty (needs both quantities), else not_executed / unconfirmed
      PASS/hold : no_action_expected
    Returns counts by new status."""
    date = date or _today()
    cur = _as_qty_map(current_holdings)
    prior = _as_qty_map(prior_holdings) if prior_holdings is not None else None
    ledger = load_ledger(path)
    counts = {"confirmed_executed": 0, "not_executed": 0,
              "execution_unconfirmed": 0, "no_action_expected": 0}
    for e in ledger["entries"]:
        if e.get("execution_status") != "recommended":
            continue
        d = e.get("decision")
        t = str(e.get("ticker") or "").strip().upper()
        held_now = t in cur
        if d == "buy":
            status = "confirmed_executed" if held_now else "not_executed"
        elif d == "sell":
            status = "confirmed_executed" if not held_now else "not_executed"
        elif d in ("top_up", "trim"):
            cq = cur.get(t)
            pq = prior.get(t) if prior is not None else None
            if prior is None or cq is None or pq is None:
                status = "execution_unconfirmed"   # presence alone cannot prove a size change
            elif d == "top_up":
                status = "confirmed_executed" if cq > pq else "not_executed"
            else:  # trim
                status = "confirmed_executed" if cq < pq else "not_executed"
        else:  # PASS / hold — nothing to execute
            status = "no_action_expected"
        e["execution_status"] = status
        if status == "confirmed_executed":
            e["executed_confirmed_date"] = date
        counts[status] = counts.get(status, 0) + 1
    save_ledger(ledger, path)
    return counts


def default_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "decision_ledger.json")


def summary(path: str) -> dict:
    """Counts by decision + total entries — for a quick run-context / email line."""
    ledger = load_ledger(path)
    by = {}
    for e in ledger["entries"]:
        by[e.get("decision")] = by.get(e.get("decision"), 0) + 1
    return {"total": len(ledger["entries"]), "by_decision": by,
            "schema_version": ledger.get("schema_version")}


def main():
    ap = argparse.ArgumentParser(description="ISA decision ledger (log / mark-to-market / summary).")
    ap.add_argument("--path", required=True, help="decision_ledger.json path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="log a decision")
    lg.add_argument("--ticker", required=True)
    lg.add_argument("--route", default="growth")
    lg.add_argument("--decision", required=True, help="buy|trim|sell|top_up|PASS|hold")
    lg.add_argument("--thesis", default="")
    lg.add_argument("--catalyst", default=None)
    lg.add_argument("--review-date", default=None)

    mt = sub.add_parser("mtm", help="append a mark-to-market point")
    mt.add_argument("--ticker", required=True)
    mt.add_argument("--price", type=float, required=True)
    mt.add_argument("--return-pct", type=float, default=None)
    mt.add_argument("--thesis-status", default=None)

    sub.add_parser("summary", help="print ledger summary")

    a = ap.parse_args()
    if a.cmd == "log":
        e = log_decision(a.path, a.ticker, a.route, a.decision, thesis=a.thesis,
                         catalyst=a.catalyst, expected_review_date=a.review_date)
        print(f"LOGGED {e['_id']}")
    elif a.cmd == "mtm":
        e = append_mtm(a.path, a.ticker, a.price, return_pct=a.return_pct,
                       thesis_status=a.thesis_status)
        print(f"MTM_APPENDED {e['_id']}" if e else f"NO_ENTRY for {a.ticker}")
    elif a.cmd == "summary":
        print(json.dumps(summary(a.path), indent=2))


if __name__ == "__main__":
    main()
