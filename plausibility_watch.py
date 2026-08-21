#!/usr/bin/env python3
"""
plausibility_watch.py — closes ISA-0130.  Built 16-Aug-2026.

⚑ THE CORRECTION THAT INVERTS THE ITEM. BL-25's recorded action was "act only if the frequency
rises". MEASURED 15-Aug-2026: THE FREQUENCY IS NOT RETAINED ANYWHERE. `gate_variables.assert_ranges`
raises or warns per run; the counts survive only in transient run logs and in retrospective prose
(six on 08-Aug per the F250-SPI retrospective, one on 14-Aug for SKHY). There is no store, so
"frequency rises" was never a condition anything could evaluate — a rule that depends on someone
remembering is a defect (R14.1).

R6.5 — retain first, analyse later. The governing test: if we don't write it now, what question
becomes permanently unanswerable? Here: every question about SKHY (ISA-0341). The value that
breached is gone; the anomaly cannot be diagnosed after the fact, only re-observed.

TWO PIECES, IN ORDER
  1. IRREVERSIBLE CAPTURE. Every plausibility WARN is appended to `plausibility_warns.jsonl` with
     ticker, group, run_date, column, THE VALUE ITSELF and the range it breached. The value is the
     part that was being thrown away and the only part that makes a diagnosis possible.
  2. A BAND ON THE STORE. `warn_rate()` turns "frequency rises" into a threshold a machine tests:
     warns per 1,000 rows, per group, against a floor derived from the store's own history —
     never hand-set (R14.3).

ROLLBACK (R4.13): `CAPTURE = False` — warns are still raised, simply not retained.
"""
from __future__ import annotations
import datetime as dt, json, math, os, statistics as st, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

CAPTURE = True
STORE = HERE / "plausibility_warns.jsonl"
K_SIGMA = 3.0
MIN_RUNS_FOR_BAND = 6          # below this the SE is wider than any band worth declaring
SCHEMA_VERSION = "1.0.0"


def record(*, ticker, group, run_date, column, value, lo, hi, unit, rows_scanned,
           store: Path = None) -> dict:
    """Append ONE warn. The VALUE is the field that was being discarded (R6.5)."""
    row = {"ticker": ticker, "group": group, "run_date": run_date, "column": column,
           "value": value, "range_low": lo, "range_high": hi, "unit": unit,
           "rows_scanned": rows_scanned, "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
           "schema_version": SCHEMA_VERSION}
    if CAPTURE:
        p = store or STORE
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    return row


def capture_violations(violations, *, group, run_date, rows_scanned, records=None,
                       store: Path = None) -> int:
    """Parse `gate_variables.assert_ranges` violation strings into retained rows.

    ⚑ R4.9 — a line that cannot be parsed is COUNTED and retained VERBATIM, never dropped. A
    recogniser that silently skips what it does not understand is the exact shape ISA-0344 is
    about, and this parser is a hand-written recogniser like any other.
    """
    n = 0
    for v in violations or []:
        parsed = {"ticker": None, "column": None, "value": None, "range_low": None,
                  "range_high": None, "unit": None}
        try:
            head, rest = v.split(": ", 1)
            parts = head.split("/")
            parsed["ticker"] = parts[0]
            expr, tail = rest.split(" outside plausible range ", 1)
            parsed["column"], raw = expr.split("=", 1)
            parsed["value"] = float(raw)
            rng, unit = tail.split(" (", 1)
            lo, hi = rng.strip("[] ").split(", ")
            parsed["range_low"], parsed["range_high"] = float(lo), float(hi)
            parsed["unit"] = unit.split(")")[0]
        except Exception:                                            # noqa: BLE001
            parsed["column"] = "UNPARSED"
            parsed["unit"] = "UNPARSED"
        row = record(ticker=parsed["ticker"], group=group, run_date=run_date,
                     column=parsed["column"], value=parsed["value"],
                     lo=parsed["range_low"], hi=parsed["range_high"], unit=parsed["unit"],
                     rows_scanned=rows_scanned, store=store)
        row["raw"] = v
        n += 1
    return n


def load(store: Path = None) -> list:
    p = store or STORE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def warn_rate(store: Path = None) -> dict:
    """Warns per 1,000 rows scanned, per group, with a band DERIVED from the store's own history.

    R4.3 — with fewer than MIN_RUNS_FOR_BAND runs this returns UNKNOWN and BLOCKS. It does not
    return "within band" on two observations; an unmeasurable rate and an acceptable rate are
    opposite facts.
    """
    rows = load(store)
    if not rows:
        return {"state": "NO_OBSERVATIONS",
                "note": "the store exists and is empty - which is itself the answer BL-25 never "
                        "had: zero retained warns, not an unknown number of them"}
    by = {}
    for r in rows:
        by.setdefault((r["group"], r["run_date"]), {"warns": 0, "rows": r.get("rows_scanned") or 0})
        by[(r["group"], r["run_date"])]["warns"] += 1
    per_group = {}
    for (g, d), v in by.items():
        rate = (v["warns"] / v["rows"] * 1000.0) if v["rows"] else None
        per_group.setdefault(g, []).append({"run_date": d, "warns": v["warns"],
                                            "rows": v["rows"], "per_1000": rate})
    out = {}
    for g, runs in per_group.items():
        rates = [r["per_1000"] for r in runs if r["per_1000"] is not None]
        if len(rates) < MIN_RUNS_FOR_BAND:
            out[g] = {"state": "UNKNOWN_INSUFFICIENT_HISTORY", "n_runs": len(rates),
                      "min_runs_required": MIN_RUNS_FOR_BAND,
                      "runs": sorted(runs, key=lambda r: r["run_date"]),
                      "verdict": "BLOCKS - no band may be declared on this many runs (R4.3)"}
            continue
        mu, sd = st.mean(rates), (st.pstdev(rates) or 0.0)
        ceiling = mu + K_SIGMA * sd
        latest = sorted(runs, key=lambda r: r["run_date"])[-1]
        out[g] = {"state": "BANDED", "n_runs": len(rates), "mean_per_1000": round(mu, 3),
                  "sd": round(sd, 3), "ceiling_per_1000": round(ceiling, 3),
                  "latest": latest,
                  "verdict": ("BREACH" if (latest["per_1000"] or 0) > ceiling else "WITHIN_BAND"),
                  "basis": f"mean + {K_SIGMA}sd of the store's own {len(rates)} retained runs"}
    return {"state": "MEASURED", "groups": out, "total_warns": len(rows), "as_of": dt.date.today().isoformat()}


def selftest(verbose=True) -> int:
    import tempfile
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    tmp = Path(tempfile.mktemp(suffix=".jsonl"))
    ck("an absent store reports NO_OBSERVATIONS, never a rate",
       warn_rate(tmp)["state"] == "NO_OBSERVATIONS")

    # the SKHY line, verbatim in the shape gate_variables emits
    v = ["SKHY/NASDAQ/2026-08-14: revenue_latest=9.71e+13 outside plausible range "
         "[-10000000000000.0, 10000000000000.0] (reporting currency absolute). If this is a "
         "distance-below-threshold, it is in the wrong column - gate_variables stores LEVELS ONLY."]
    n = capture_violations(v, group="NASDAQ", run_date="2026-08-14", rows_scanned=439, store=tmp)
    rows = load(tmp)
    ck("the violation is retained", n == 1 and len(rows) == 1)
    ck("THE VALUE ITSELF is retained - the field ISA-0341 needed and did not have",
       rows[0]["value"] == 9.71e13)
    ck("ticker, column and the breached range are retained",
       rows[0]["ticker"] == "SKHY" and rows[0]["column"] == "revenue_latest"
       and rows[0]["range_high"] == 1e13)
    ck("rows_scanned is retained so a RATE is computable, not just a count",
       rows[0]["rows_scanned"] == 439)

    # NEGATIVE CONTROL — an unparseable line must be COUNTED, not dropped
    capture_violations(["complete gibberish with no structure"], group="NASDAQ",
                       run_date="2026-08-14", rows_scanned=439, store=tmp)
    r2 = load(tmp)
    ck("NEGATIVE CONTROL: an unparseable violation is retained and marked UNPARSED",
       len(r2) == 2 and r2[1]["column"] == "UNPARSED")

    wr = warn_rate(tmp)
    ck("one run does not produce a band - it BLOCKS",
       wr["groups"]["NASDAQ"]["state"] == "UNKNOWN_INSUFFICIENT_HISTORY")
    for i in range(MIN_RUNS_FOR_BAND):
        record(ticker="X", group="SP500", run_date=f"2026-0{i+1}-01", column="mkt_cap",
               value=1.0, lo=0.0, hi=1e14, unit="USD", rows_scanned=500, store=tmp)
    wr2 = warn_rate(tmp)
    ck("enough runs produce a band derived from the store, not hand-set",
       wr2["groups"]["SP500"]["state"] == "BANDED"
       and "retained runs" in wr2["groups"]["SP500"]["basis"])
    ck("the band yields a verdict",
       wr2["groups"]["SP500"]["verdict"] in ("WITHIN_BAND", "BREACH"))

    global CAPTURE
    CAPTURE = False
    before = len(load(tmp))
    record(ticker="Y", group="SP500", run_date="2026-07-01", column="mkt_cap", value=1.0,
           lo=0.0, hi=1e14, unit="USD", rows_scanned=1, store=tmp)
    ck("rollback constant stops capture without stopping the warn", len(load(tmp)) == before)
    CAPTURE = True
    tmp.unlink(missing_ok=True)
    print(f"\nplausibility_watch selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 10 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(json.dumps(warn_rate(), indent=2))
