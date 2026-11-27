#!/usr/bin/env python3
"""
sell_off_state.py - P8b (ISA-0907 / ISA-0964, 25-Nov-2026): Raj's standing constraint "Active sell-off - do not sell core fund
holdings to fund stock purchases", measured mechanically. The rule's numbers live in ONE home, isa_policy.SELL_OFF_RULE
(Raj 25-Nov-2026 09:50, Q1-A); this module measures and never restates them (R4.4).

THE RULE (Run_Context Standing Constraints; Raj Q1-A / Q2-A, 25-Nov-2026):
  OVERALL  the overall ISA benchmark (VUAG.L) closes >= overall_drop_pct below its highest daily close of the last
           window_trading_days sessions                                          -> ACTIVE for every donor
  LOCAL    a donor fund's own mandate comparator (benchmark_registry.comparator_for) closes >= local_drop_pct below
           its window high                                                       -> ACTIVE for that donor
  DISORDERLY  Raj has DECLARED disorderly markets (a dated, reasoned event in the risk-review store; restrict-only;
           the machine never infers it - Q2-A interim; mechanical version ISA-0966)  -> ACTIVE for every donor
  What it restricts: fund-trim FUNDING of stock purchases only. Cash-funded buys, fund-to-fund actions, reductions and
  the drawdown protocol are untouched.

FRESHNESS without a tunable number: a series is fresh when its last daily close is on or after the last COMPLETED
weekday before the check date. A holiday or a failed fetch therefore reads UNKNOWN, and UNKNOWN restricts exactly as
ACTIVE does (R4.3: a control fed a null never passes).

ROLLBACK (R4.13): SELL_OFF_RULE_ENFORCED = False -> state_for() returns NOT_ENFORCED (never restricts); the
measurement is still produced and published.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Callable, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SELL_OFF_RULE_ENFORCED = True        # ROLLBACK (R4.13): False -> NOT_ENFORCED, never restricts

try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                   # noqa: BLE001
    def _fi_mark(*_a, **_k):
        return None

ACTIVE, CLEAR, UNKNOWN, NOT_ENFORCED = "ACTIVE", "CLEAR", "UNKNOWN", "NOT_ENFORCED"
DECLARE, CLEAR_EVENT = "DISORDERLY_DECLARED", "DISORDERLY_CLEARED"


def rule() -> dict:
    """The ONE declared rule (isa_policy.SELL_OFF_RULE). Unreadable -> UNDECLARED (never a default number)."""
    try:
        import isa_policy as _p
        r = dict(getattr(_p, "SELL_OFF_RULE"))
        for k in ("overall_benchmark", "overall_drop_pct", "local_drop_pct", "window_trading_days"):
            if r.get(k) in (None, ""):
                return {"state": "UNDECLARED", "why": "isa_policy.SELL_OFF_RULE lacks %s" % k}
        return dict(r, state="DECLARED")
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "UNDECLARED", "why": "isa_policy.SELL_OFF_RULE unavailable: %s" % exc}


def last_completed_weekday(today: dt.date) -> dt.date:
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def series_state(closes: List[Tuple[dt.date, float]], today: dt.date, drop_pct: float, window: int) -> dict:
    """One series against its window high. closes = [(date, close)] ascending. Pure."""
    c = [(d, float(v)) for d, v in (closes or []) if v is not None and float(v) > 0]
    if len(c) < window:
        return {"state": UNKNOWN, "why": "only %d daily closes (< %d-session window)" % (len(c), window), "n_obs": len(c)}
    w = c[-window:]
    last_d, last = w[-1]
    hi_d, hi = max(w, key=lambda x: x[1])
    dd = 100.0 * (last / hi - 1.0)
    out = {"last_date": last_d.isoformat(), "last_close": round(last, 4), "high": round(hi, 4), "high_date": hi_d.isoformat(),
           "drawdown_pct": round(dd, 2), "threshold_pct": drop_pct, "window": window, "n_obs": len(c)}
    if last_d < last_completed_weekday(today):
        return dict(out, state=UNKNOWN, why="stale: last close %s is before the last completed weekday %s"
                    % (last_d, last_completed_weekday(today)))
    return dict(out, state=ACTIVE if round(-dd, 6) >= float(drop_pct) else CLEAR)   # rounded: 92/100-1 is -7.99999..


def yfinance_closes(ticker: str) -> List[Tuple[dt.date, float]]:
    """Daily closes (6 months) from yfinance - the source drawdown_monitor already uses for VUAG.L."""
    import yfinance as yf
    h = yf.Ticker(ticker).history(period="6mo", auto_adjust=False)
    return [(i.date(), float(v)) for i, v in h["Close"].dropna().items()]


def disorderly(root: str = HERE, today: Optional[dt.date] = None) -> dict:
    """Raj's declared disorderly-markets flag from the risk-review store: the latest event wins; a declaration with an
    `until` date lapses after it. Unreadable store -> UNKNOWN (restricts)."""
    today = today or dt.date.today()
    try:
        import risk_review_state as _rrs
        ev = _rrs.read_events(root)
    except Exception as exc:                                        # noqa: BLE001
        return {"state": UNKNOWN, "why": "risk-review store unreadable: %s" % exc}
    if ev["state"] == "CORRUPT":
        return {"state": UNKNOWN, "why": "risk-review store CORRUPT"}
    rel = [e for e in ev["events"] if e.get("event") in (DECLARE, CLEAR_EVENT)]
    if not rel or rel[-1]["event"] == CLEAR_EVENT:
        return {"state": CLEAR, "last_event": (rel[-1]["event_id"] if rel else None)}
    d = rel[-1]
    if d.get("until") and dt.date.fromisoformat(d["until"]) < today:
        return {"state": CLEAR, "why": "declaration %s lapsed %s" % (d["event_id"], d["until"]), "last_event": d["event_id"]}
    return {"state": ACTIVE, "declared_by": d.get("declared_by"), "declared_on": d.get("recorded_at"), "reason": d.get("reason"),
            "until": d.get("until"), "event_id": d["event_id"]}


def record_disorderly(declare: bool, by: str, reason: str, root: str = HERE, until: Optional[str] = None,
                      now: Optional[dt.datetime] = None) -> dict:
    """Raj's declaration/clearance (CLI only; the machine never calls this on its own)."""
    if not str(by or "").strip() or len(str(reason or "").strip()) < 10:
        raise ValueError("record_disorderly needs --by and a reason of at least 10 characters")
    import risk_review_state as _rrs
    t = now or dt.datetime.now(dt.timezone.utc)
    rec = {"event_id": "RRD-%s-%s" % (t.strftime("%Y%m%dT%H%M%S"), "declare" if declare else "clear"),
           "event": DECLARE if declare else CLEAR_EVENT, "constraint": "ACTIVE_SELL_OFF_DISORDERLY",
           "declared_by": by, "reason": reason, "until": until, "recorded_at": t.isoformat(timespec="seconds")}
    _rrs._write(root, rec)
    return rec


def measure(donors: Iterable[str] = (), today: Optional[dt.date] = None, root: str = HERE,
            fetch: Optional[Callable[[str], List[Tuple[dt.date, float]]]] = None,
            comparator: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """The sell-off state now: overall leg, each donor's local leg and Raj's disorderly flag."""
    _fi_mark("sell_off_state", "measure")
    today = today or dt.date.today()
    r = rule()
    out = {"as_of": today.isoformat(), "rule": {k: r.get(k) for k in ("state", "overall_benchmark", "overall_drop_pct",
                                                                       "local_drop_pct", "window_trading_days", "authority")},
           "source": "yfinance daily Close (period 6mo)", "series": {}, "donors": {}}
    if r["state"] != "DECLARED":
        out.update(overall={"state": UNKNOWN, "why": r.get("why")}, disorderly={"state": UNKNOWN})
        return out
    fetch = fetch or yfinance_closes
    if comparator is None:
        def comparator(sd):
            import benchmark_registry as _br
            return _br.comparator_for(sd)[0]          # -> (ticker, dict); RAISES when no comparator is declared
    W = int(r["window_trading_days"])

    def one(t, drop):
        if t in out["series"]:
            return out["series"][t]
        try:
            s = series_state(fetch(t), today, drop, W)
        except Exception as exc:                                    # noqa: BLE001
            s = {"state": UNKNOWN, "why": "fetch failed: %s: %s" % (type(exc).__name__, str(exc)[:120])}
        s["ticker"] = t
        out["series"][t] = s
        return s
    out["overall"] = one(r["overall_benchmark"], float(r["overall_drop_pct"]))
    out["disorderly"] = disorderly(root, today)
    for sd in donors:
        try:
            t = comparator(sd)
        except Exception as exc:                                    # noqa: BLE001
            t, why = None, str(exc)
        if not t:
            out["donors"][sd] = {"state": UNKNOWN, "why": "no mandate comparator declared for %s" % sd}
            continue
        loc = one(t, float(r["local_drop_pct"])) if t != r["overall_benchmark"] else \
            series_state(fetch(t), today, float(r["local_drop_pct"]), W)
        out["donors"][sd] = {"comparator": t, "state": loc["state"], "drawdown_pct": loc.get("drawdown_pct"),
                             "why": loc.get("why")}
    return out


def state_for(m: dict, donor: Optional[str] = None) -> dict:
    """ACTIVE / CLEAR / UNKNOWN (UNKNOWN restricts) for fund-trim funding from `donor` (None = any trim at all)."""
    _fi_mark("sell_off_state", "state_for")
    if not SELL_OFF_RULE_ENFORCED:
        return {"state": NOT_ENFORCED, "why": "SELL_OFF_RULE_ENFORCED False (rollback)"}
    ov = (m or {}).get("overall") or {}
    ds = (m or {}).get("disorderly") or {}
    if ov.get("state") != CLEAR:
        return {"state": ov.get("state") or UNKNOWN, "leg": "OVERALL", "why": ov.get("why") or
                "%s %s%% vs its %s-session high (threshold %s%%)" % (ov.get("ticker"), ov.get("drawdown_pct"),
                                                                   ov.get("window"), ov.get("threshold_pct"))}
    if ds.get("state") != CLEAR:
        return {"state": ds.get("state") or UNKNOWN, "leg": "DISORDERLY",
                "why": ds.get("why") or "Raj declared disorderly markets (%s): %s" % (ds.get("event_id"), ds.get("reason"))}
    if donor is not None:
        d = ((m or {}).get("donors") or {}).get(donor)
        if not d:
            return {"state": UNKNOWN, "leg": "LOCAL", "why": "donor %s was not measured" % donor}
        if d.get("state") != CLEAR:
            return {"state": d.get("state") or UNKNOWN, "leg": "LOCAL",
                    "why": d.get("why") or "%s %s%% vs its window high (local threshold)" % (d.get("comparator"), d.get("drawdown_pct"))}
    return {"state": CLEAR}


def _selftest(verbose: bool = True) -> int:
    import shutil
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %r" % (detail,)))
        if not cond:
            fails.append(name)
    fri = dt.date(2026, 11, 27)

    def path(last_day, last_close, n=70, peak=100.0):
        d0 = last_day - dt.timedelta(days=int(n * 1.5))
        days = [d for d in (d0 + dt.timedelta(days=i) for i in range(int(n * 1.5) + 1)) if d.weekday() < 5]
        days = [d for d in days if d <= last_day][-n:]
        return [(d, peak) for d in days[:-1]] + [(days[-1], last_close)]
    sat = dt.date(2026, 11, 28)
    a = series_state(path(fri, 91.0), sat, 8.0, 63)
    ok("MUST-FIRE: -9% vs the 63-session high at an 8% threshold -> ACTIVE", a["state"] == ACTIVE and a["drawdown_pct"] == -9.0, a)
    b = series_state(path(fri, 93.0), sat, 8.0, 63)
    ok("NEGATIVE CONTROL: -7% -> CLEAR", b["state"] == CLEAR, b)
    e = series_state(path(fri, 92.0), sat, 8.0, 63)
    ok("boundary: exactly -8% -> ACTIVE (>= threshold)", e["state"] == ACTIVE, e)
    s = series_state(path(dt.date(2026, 11, 25), 99.0), sat, 8.0, 63)
    ok("NEGATIVE CONTROL: a Wednesday close checked on Saturday is STALE -> UNKNOWN (restricts)", s["state"] == UNKNOWN, s)
    ok("too few closes -> UNKNOWN", series_state(path(fri, 99.0, n=20), sat, 8.0, 63)["state"] == UNKNOWN)
    # temporal matrix on the freshness boundary (check date vs the last close, Friday 27-Nov)
    ok("temporal T-1: checked Friday with Thursday's close -> fresh", series_state(path(dt.date(2026, 11, 26), 99.0), fri, 8.0, 63)["state"] == CLEAR)
    ok("temporal T: checked Saturday with Friday's close -> fresh", series_state(path(fri, 99.0), sat, 8.0, 63)["state"] == CLEAR)
    ok("temporal T+1: checked Monday with Friday's close -> fresh (no session missed)",
       series_state(path(fri, 99.0), dt.date(2026, 11, 30), 8.0, 63)["state"] == CLEAR)
    r1 = series_state(path(fri, 99.0), sat, 8.0, 63)
    ok("temporal repeat_same_occurrence: the same check twice -> identical", r1 == series_state(path(fri, 99.0), sat, 8.0, 63))
    ok("temporal next_occurrence: checked Tuesday with Friday's close -> stale UNKNOWN",
       series_state(path(fri, 99.0), dt.date(2026, 12, 1), 8.0, 63)["state"] == UNKNOWN)
    tmp = tempfile.mkdtemp(prefix="sos_selftest_")
    try:
        data = {"VUAG.L": path(fri, 95.0), "VJPN.L": path(fri, 88.0), "FTAL.L": path(fri, 96.0)}
        comp = {"B50MZ94": "VJPN.L", "B2PLJM6": "FTAL.L", "NOCOMP": None}.get
        m = measure(["B50MZ94", "B2PLJM6", "NOCOMP"], today=sat, root=tmp, fetch=lambda t: data[t], comparator=comp)
        ok("overall CLEAR at -5%", m["overall"]["state"] == CLEAR, m["overall"])
        ok("MUST-FIRE: a donor whose own benchmark is -12% (>= 10%) is ACTIVE for that donor only",
           state_for(m, "B50MZ94")["state"] == ACTIVE and state_for(m, "B2PLJM6")["state"] == CLEAR, m["donors"])
        ok("NEGATIVE CONTROL: a donor with no declared comparator reads UNKNOWN (restricts)", state_for(m, "NOCOMP")["state"] == UNKNOWN)
        ok("any-trim question with the overall leg CLEAR -> CLEAR", state_for(m)["state"] == CLEAR)
        data2 = dict(data, **{"VUAG.L": path(fri, 90.0)})
        m2 = measure(["B2PLJM6"], today=sat, root=tmp, fetch=lambda t: data2[t], comparator=comp)
        ok("MUST-FIRE: the overall leg ACTIVE restricts EVERY donor", state_for(m2, "B2PLJM6")["state"] == ACTIVE
           and state_for(m2)["leg"] == "OVERALL", state_for(m2))

        def boom(t):
            raise RuntimeError("network down")
        m3 = measure(["B2PLJM6"], today=sat, root=tmp, fetch=boom, comparator=comp)
        ok("NEGATIVE CONTROL: a failed fetch reads UNKNOWN and restricts (R4.3)", state_for(m3)["state"] == UNKNOWN, m3["overall"])
        try:
            record_disorderly(True, "", "x", root=tmp)
            refused = False
        except ValueError:
            refused = True
        ok("NEGATIVE CONTROL: a declaration without who/reason is refused", refused)
        record_disorderly(True, "Raj (fixture)", "gilt market seizure fixture", root=tmp, now=dt.datetime(2026, 11, 27, 10, 0))
        m4 = measure(["B2PLJM6"], today=sat, root=tmp, fetch=lambda t: data[t], comparator=comp)
        ok("MUST-FIRE: Raj's disorderly declaration restricts trims although every series is CLEAR",
           state_for(m4, "B2PLJM6")["state"] == ACTIVE and state_for(m4)["leg"] == "DISORDERLY", state_for(m4))
        record_disorderly(False, "Raj (fixture)", "markets orderly again fixture", root=tmp, now=dt.datetime(2026, 11, 27, 11, 0))
        m5 = measure(["B2PLJM6"], today=sat, root=tmp, fetch=lambda t: data[t], comparator=comp)
        ok("Raj's clearance lifts only his own flag", state_for(m5, "B2PLJM6")["state"] == CLEAR, m5["disorderly"])
        global SELL_OFF_RULE_ENFORCED
        SELL_OFF_RULE_ENFORCED = False
        ok("rollback: not enforced -> NOT_ENFORCED, measurement still produced", state_for(m2)["state"] == NOT_ENFORCED and m2["overall"]["state"] == ACTIVE)
        SELL_OFF_RULE_ENFORCED = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print("sell_off_state selftest: %d FAIL" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv and argv.index(n) + 1 < len(argv) else d
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--declare-disorderly" in argv or "--clear-disorderly" in argv:
        rec = record_disorderly("--declare-disorderly" in argv, opt("--by"), opt("--reason"), until=opt("--until"))
        print(json.dumps(rec, indent=1))
        return 0
    donors = [x for x in (opt("--donors") or "").split(",") if x]
    print(json.dumps(measure(donors), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
