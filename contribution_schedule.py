"""contribution_schedule.py - THE single reader of the ISA contribution schedule (ISA-0851, B01-R01/R02/R05).

target_state.contribution_schedule is the canonical declaration of what Raj intends to pay in (amount and
effective date of each segment). Before 11-Oct-2026 the cash side kept a SECOND home -
extract_cash_statement.STANDING_ORDER / STANDING_ORDER_ACTIVE, hand-flipped constants - and an
INFERENCE rule in extract_portfolio that turned an expected payment into effective cash whenever a broker
file was dated within three working days of the 1st. Both are retired here:

  * every consumer asks this module whether a contribution stream is in force, how much, since when and
    what the next scheduled change is (6-Apr-2027 GBP 1,250 restart at the time of writing);
  * a schedule entry - its amount, its date, a resumption - is an EXPECTATION. It never proves that cash
    was credited. Credits are OBSERVED only from the AJ Bell cash statement (the golden source), so an
    expected contribution can never become deployable cash (credit_evidence = NOT_PROVEN, always).

A missing or unreadable schedule is UNKNOWN (never "paused", never "active"): consumers state it and price
nothing from it (R2.10, R4.3).

Stdlib only.
"""
import datetime as _dt
import json
import os
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = "target_state.json"
ACTIVE, PAUSED, UNKNOWN = "ACTIVE", "PAUSED", "UNKNOWN"
CREDIT_EVIDENCE = ("NOT_PROVEN - a schedule entry, its effective date or a resumption is an expectation; a cash "
                   "credit is observed only from the AJ Bell cash statement (B01-R01)")


def _today() -> _dt.date:
    return _dt.date.today()


def _as_date(v) -> Optional[_dt.date]:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def load(root: str = HERE, target_state: Optional[dict] = None):
    """-> (schedule list | None, why). A schedule is a non-empty list of {from, monthly_gbp}."""
    st = target_state
    if st is None:
        try:
            with open(os.path.join(root, STATE_FILE), encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception as exc:                                        # noqa: BLE001
            return None, "target_state.json unreadable (%s: %s)" % (type(exc).__name__, exc)
    sch = (st or {}).get("contribution_schedule")
    if not isinstance(sch, list) or not sch:
        return None, "target_state.contribution_schedule absent or empty"
    out = []
    for seg in sch:
        d = _as_date((seg or {}).get("from"))
        try:
            amt = float((seg or {}).get("monthly_gbp"))
        except (TypeError, ValueError):
            amt = None
        if d is None or amt is None or amt != amt or amt < 0:
            return None, "contribution_schedule segment %r is not {from: ISO date, monthly_gbp: >=0}" % (seg,)
        out.append({"from": d, "monthly_gbp": amt, "note": (seg or {}).get("note")})
    return sorted(out, key=lambda s: s["from"]), None


def state(as_of=None, root: str = HERE, target_state: Optional[dict] = None) -> dict:
    """The one reading every consumer uses. `monthly_gbp` is the amount IN FORCE on `as_of` (0 while paused)."""
    as_of = _as_date(as_of) or _today()
    sch, why = load(root, target_state)
    base = {"source": "target_state.contribution_schedule", "as_of": as_of.isoformat(),
            "credit_evidence": CREDIT_EVIDENCE}
    if sch is None:
        return dict(base, state=UNKNOWN, monthly_gbp=None, in_force_from=None, paused_since=None,
                    next_change=None, restart_monthly_gbp=None, why=why)
    cur = None
    for seg in sch:
        if seg["from"] <= as_of:
            cur = seg
    nxt = next((s for s in sch if s["from"] > as_of), None)
    restart = next((s for s in sch if s["from"] > as_of and s["monthly_gbp"] > 0), None)
    if cur is None:
        return dict(base, state=UNKNOWN, monthly_gbp=None, in_force_from=None, paused_since=None,
                    next_change=({"from": nxt["from"].isoformat(), "monthly_gbp": nxt["monthly_gbp"]} if nxt else None),
                    restart_monthly_gbp=(restart["monthly_gbp"] if restart else None),
                    why="no schedule segment is in force on %s (first segment starts %s)" % (as_of, sch[0]["from"]))
    st = ACTIVE if cur["monthly_gbp"] > 0 else PAUSED
    return dict(base, state=st, monthly_gbp=cur["monthly_gbp"], in_force_from=cur["from"].isoformat(),
                paused_since=(cur["from"].isoformat() if st == PAUSED else None),
                next_change=({"from": nxt["from"].isoformat(), "monthly_gbp": nxt["monthly_gbp"]} if nxt else None),
                restart_monthly_gbp=(restart["monthly_gbp"] if (st == PAUSED and restart) else None), why=None)


def describe(s: dict) -> str:
    """One sentence for reports - the SAME words in the email, run_context and Run_Context references."""
    if s.get("state") == ACTIVE:
        txt = "S/O ACTIVE GBP %s/month since %s" % (format(s["monthly_gbp"], ",.0f"), s["in_force_from"])
    elif s.get("state") == PAUSED:
        txt = "S/O PAUSED since %s (GBP 0/month)" % s["paused_since"]
    else:
        return "S/O state UNKNOWN - %s" % (s.get("why") or "schedule unreadable")
    nc = s.get("next_change")
    if nc:
        txt += "; next scheduled change %s: GBP %s/month" % (nc["from"], format(nc["monthly_gbp"], ",.0f"))
    return txt + " (schedule = expectation; credits only from the cash statement)"


def observed_credits(contributions_detail, ) -> dict:
    """B01-R02: external credits OBSERVED in the golden source, de-duplicated by flow identity
    (date, transaction, amount, reference). Identical identities are counted once and REPORTED."""
    rows, seen, dups = [], set(), []
    for r in contributions_detail or []:
        if not isinstance(r, dict) or r.get("type") == "PARSE_ERROR":
            continue
        try:
            amt = round(float(r.get("amount_gbp")), 2)
        except (TypeError, ValueError):
            continue
        key = (str(r.get("date") or ""), str(r.get("transaction") or "").strip().lower(), amt,
               str(r.get("reference") or "").strip())
        if key in seen:
            dups.append({"date": key[0], "transaction": r.get("transaction"), "amount_gbp": amt})
            continue
        seen.add(key)
        rows.append({"date": key[0], "transaction": r.get("transaction"), "amount_gbp": amt,
                     "reference": r.get("reference")})
    return {"n": len(rows), "total_gbp": round(sum(x["amount_gbp"] for x in rows), 2), "rows": rows,
            "duplicates_removed": dups,
            "identity": "(date, transaction, amount_gbp, reference) - an identical identity is one flow"}


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=None):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond or detail is None else " :: %r" % (detail,)))

    ts = {"contribution_schedule": [{"from": "2026-07-01", "monthly_gbp": 0}, {"from": "2027-04-06", "monthly_gbp": 1250}]}
    s = state("2026-11-01", target_state=ts)
    ok("ISA-0851 MUST-FIRE: Nov-2026 reads PAUSED, GBP 0 in force, restart 2027-04-06 GBP 1,250",
       s["state"] == PAUSED and s["monthly_gbp"] == 0 and s["paused_since"] == "2026-07-01"
       and s["next_change"] == {"from": "2027-04-06", "monthly_gbp": 1250.0} and s["restart_monthly_gbp"] == 1250.0, s)
    ok("ISA-0851 R5.14 T-1: the day before the restart is still PAUSED", state("2027-04-05", target_state=ts)["state"] == PAUSED)
    s2 = state("2027-04-06", target_state=ts)
    ok("ISA-0851 R5.14 T: the restart day reads ACTIVE GBP 1,250 (an expectation, never a credit)",
       s2["state"] == ACTIVE and s2["monthly_gbp"] == 1250.0 and s2["credit_evidence"].startswith("NOT_PROVEN"), s2)
    ok("ISA-0851 R5.14 T+1: the day after the restart is ACTIVE with no further change",
       state("2027-04-07", target_state=ts)["state"] == ACTIVE and state("2027-04-07", target_state=ts)["next_change"] is None)
    ok("ISA-0851 R5.14 repeat_same_occurrence: the same date twice gives the same reading",
       state("2026-11-01", target_state=ts) == state("2026-11-01", target_state=ts))
    ok("ISA-0851 R5.14 next_occurrence: the next monthly occurrence (Dec-2026) is still PAUSED with the same restart",
       state("2026-12-06", target_state=ts)["next_change"] == {"from": "2027-04-06", "monthly_gbp": 1250.0})
    ok("ISA-0851 MUST-FIRE: an absent schedule is UNKNOWN, never PAUSED or ACTIVE",
       state("2026-11-01", target_state={})["state"] == UNKNOWN and state("2026-11-01", target_state={})["monthly_gbp"] is None)
    ok("ISA-0851 MUST-FIRE: a malformed segment is UNKNOWN",
       state("2026-11-01", target_state={"contribution_schedule": [{"from": "x", "monthly_gbp": 5}]})["state"] == UNKNOWN)
    ok("ISA-0851 MUST-FIRE: a date before the first segment is UNKNOWN (no assumed stream)",
       state("2026-01-01", target_state=ts)["state"] == UNKNOWN)
    oc = observed_credits([{"date": "2026-04-06", "transaction": "Faster Payment In", "amount_gbp": 5000.0, "reference": "-"},
                           {"date": "2026-04-06", "transaction": "Faster Payment In", "amount_gbp": 5000.0, "reference": "-"},
                           {"date": "2026-05-01", "transaction": "Debit Card Payment", "amount_gbp": 1250.0, "reference": "-"},
                           {"date": "2026-05-05", "transaction": "Direct debit payment", "amount_gbp": 1250.0, "reference": "-"}])
    ok("ISA-0851 MUST-FIRE (B01-R02): a duplicated credit identity is counted once and reported",
       oc["n"] == 3 and oc["total_gbp"] == 7500.0 and len(oc["duplicates_removed"]) == 1, oc)
    ok("ISA-0851 NEGATIVE CONTROL: two distinct same-amount credits on different dates both count",
       sum(1 for r in oc["rows"] if r["amount_gbp"] == 1250.0) == 2)
    ok("ISA-0851: describe() states the restart and that a schedule is not a credit",
       "PAUSED since 2026-07-01" in describe(s) and "2027-04-06" in describe(s) and "cash statement" in describe(s))
    live = state()
    ok("ISA-0851 NEGATIVE CONTROL: the delivered target_state reads a definite state today",
       live["state"] in (ACTIVE, PAUSED), live)
    print("contribution_schedule selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(json.dumps(state(sys.argv[1] if len(sys.argv) > 1 else None), indent=1))
