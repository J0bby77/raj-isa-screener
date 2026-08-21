#!/usr/bin/env python3
"""R15.2 DEGRADATION WATCH for source coverage — the first band instrument in the framework.

WHAT THIS EXISTS TO CATCH
-------------------------
R15.2: "every decision-grade field and every gate declares an expected coverage and fire-rate
band. A move outside the band raises an item automatically, before anyone notices an odd output."
That rule was adopted 09-Aug-2026 and nothing implemented it.

The concrete failure it is built against (ISA-0263 / ISA-0274 / ISA-0279): three separate weekly
retrospectives across 25-Jul, 08-Aug and 08-Aug reported yfinance `quoteSummary` HTTP 404s
knocking out overlays on a majority of the Top-8/Top-10 names in the EU/UK screens. Each was
filed, none was measured, and the third one closed the loop by declaring it "accepted as known
limitation" — an acceptance of a quantity nobody had a number for.

`source_performance_log.json` is the golden source for exactly this. Read on 15-Aug-2026 it says:

    STOXX Europe 600 · overlay_quotesummary · 146 attempts · 35.6% lifetime · 42.1% last run

...and it says NOTHING AT ALL for the other eleven indices, including FTSE 250 and SPI, whose
retrospectives are the ones reporting the 404s. So the instrument that exists to track source
coverage never recorded the failure the retrospectives kept raising. That is FC-I, a silent
partial: a reader that cannot match a row must COUNT it and fail (R4.9), and this one simply had
no row.

TWO CHECKS, NAMED APART ON PURPOSE (R2.10 / R6.2)
-------------------------------------------------
`band_breaches()`  — a rate we DID measure has fallen below its derived floor.
`coverage_holes()` — a metric measured for some indices is ABSENT for an index that has run since
                     that metric first appeared. "The coverage is bad" and "we never looked" are
                     different findings and must never render the same.

THE BAND IS DERIVED, NOT GUESSED (R12.3 / R3.2)
-----------------------------------------------
floor = lifetime_rate − k·SE, SE = sqrt(p(1−p)/n), k = 3, clamped to [0, 1].

A binomial 3-sigma lower bound on the rate we have actually observed. It answers "is this run
worse than this source has ever been, beyond sampling noise?" and nothing else — deliberately.
It is NOT a quality target: a source that has always been terrible gets a low floor and does not
fire, which is correct, because the finding there is the LEVEL and that is what `report()` prints
alongside. n < MIN_N_FOR_BAND yields no band at all rather than a band built on nothing (R4.10).

CLI:
  python3 degradation_bands.py --derive --write   # (re)derive bands from the retained log
  python3 degradation_bands.py --check            # breaches + holes; exit 1 if any
  python3 degradation_bands.py --report           # the full table, levels included
  python3 degradation_bands.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "source_performance_log.json"
BANDS_FILE = HERE / "degradation_bands.json"

BANDS_VERSION = "1.0.0"
K_SIGMA = 3.0
MIN_N_FOR_BAND = 30          # below this, SE is wider than any band worth declaring
STALE_INDEX_DAYS = 45        # an index not run in this long is dormant, not in breach


# ------------------------------------------------------------------ io

def load_log(path=None) -> dict:
    p = Path(path) if path else LOG_FILE
    return json.loads(p.read_text(encoding="utf-8"))


def load_bands(path=None):
    p = Path(path) if path else BANDS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ------------------------------------------------------------------ derivation

def _floor(successes: int, attempts: int) -> float:
    p = successes / attempts
    se = math.sqrt(max(p * (1.0 - p), 0.0) / attempts)
    return max(0.0, min(1.0, p - K_SIGMA * se))


def derive(log=None, as_of=None) -> dict:
    """Bands from the retained log. Pure function of the log — never hand-edited (R14.3)."""
    log = log or load_log()
    as_of = as_of or date.today().isoformat()
    bands, skipped = {}, []
    for index, metrics in (log.get("indices") or {}).items():
        for metric, s in metrics.items():
            n = int(s.get("attempts") or 0)
            k = int(s.get("successes") or 0)
            if n < MIN_N_FOR_BAND:
                skipped.append({"index": index, "metric": metric, "attempts": n,
                                "reason": f"n<{MIN_N_FOR_BAND} — a band on this would be noise, "
                                          f"and a band built on nothing is worse than none (R4.10)"})
                continue
            bands.setdefault(index, {})[metric] = {
                "lifetime_rate": round(k / n, 4),
                "attempts": n,
                "floor": round(_floor(k, n), 4),
                "basis": f"binomial {K_SIGMA:g}-sigma lower bound on {k}/{n} retained attempts",
            }
    return {
        "schema_version": BANDS_VERSION,
        "as_of": as_of,
        "source": "source_performance_log.json",
        "rule": (f"floor = lifetime_rate - {K_SIGMA:g}*sqrt(p(1-p)/n), clamped to [0,1]; no band "
                 f"below n={MIN_N_FOR_BAND}. Fires on 'worse than this source has ever been', "
                 f"never on 'worse than we would like' — the level is reported separately."),
        "bands": bands,
        "no_band": skipped,
    }


# ------------------------------------------------------------------ the two checks

def _index_last_run(metrics: dict):
    ds = [s.get("last_run") for s in metrics.values() if s.get("last_run")]
    return max(ds) if ds else None


def band_breaches(log=None, bands=None) -> list:
    """A rate we DID measure, below its derived floor on the most recent run."""
    log = log or load_log()
    bands = bands or load_bands()
    if not bands:
        return ["degradation bands have never been derived — run --derive --write (R15.2)"]
    out = []
    for index, metrics in (log.get("indices") or {}).items():
        for metric, s in metrics.items():
            b = ((bands.get("bands") or {}).get(index) or {}).get(metric)
            if not b:
                continue
            rate = s.get("last_run_rate")
            if rate is None:
                continue
            if float(rate) < b["floor"]:
                out.append(
                    f"BAND BREACH {index}/{metric}: last run {float(rate):.1%} on "
                    f"{s.get('last_run')} is below the {b['floor']:.1%} floor "
                    f"(lifetime {b['lifetime_rate']:.1%} over {b['attempts']} attempts)")
    return sorted(out)


def coverage_holes(log=None, today=None) -> list:
    """A metric measured somewhere and ABSENT here — 'never looked' is not 'looked and fine'.

    Restricted to indices that have actually run recently, so a dormant index does not generate
    a permanent hole for every metric (that would be noise, and noise gets switched off).
    """
    log = log or load_log()
    today = today or date.today()
    idx = log.get("indices") or {}
    everywhere = {}
    for index, metrics in idx.items():
        for metric in metrics:
            everywhere.setdefault(metric, set()).add(index)
    out = []
    for index, metrics in idx.items():
        last = _index_last_run(metrics)
        if not last:
            continue
        try:
            age = (today - date.fromisoformat(last)).days
        except ValueError:
            continue
        if age > STALE_INDEX_DAYS:
            continue
        for metric, seen_in in everywhere.items():
            if metric in metrics or len(seen_in) < 1:
                continue
            out.append(
                f"COVERAGE HOLE {index}/{metric}: measured for {len(seen_in)} index(es) "
                f"({', '.join(sorted(seen_in))}) but NEVER for {index}, which last ran {last}. "
                f"UNMEASURED is not PASS (R2.10, R4.9)")
    return sorted(out)


def check(log=None, bands=None, today=None) -> list:
    return band_breaches(log, bands) + coverage_holes(log, today)


def report(log=None, bands=None) -> str:
    log = log or load_log()
    bands = bands or load_bands() or derive(log)
    lines = [f"DEGRADATION BANDS  (bands as_of {bands.get('as_of')}, "
             f"source {bands.get('source')})", ""]
    lines.append("%-22s %-22s %9s %9s %9s %12s" %
                 ("INDEX", "METRIC", "lifetime", "floor", "last_run", "verdict"))
    for index in sorted(log.get("indices") or {}):
        for metric, s in sorted((log["indices"][index]).items()):
            b = ((bands.get("bands") or {}).get(index) or {}).get(metric)
            rate = s.get("last_run_rate")
            if not b:
                verdict = "NO BAND (n<%d)" % MIN_N_FOR_BAND
                lines.append("%-22s %-22s %9s %9s %9s %12s" %
                             (index[:22], metric[:22], "-", "-",
                              ("%.1f%%" % (100 * rate)) if rate is not None else "-", verdict))
                continue
            verdict = ("BREACH" if rate is not None and float(rate) < b["floor"] else "ok")
            lines.append("%-22s %-22s %8.1f%% %8.1f%% %8s %12s" %
                         (index[:22], metric[:22], 100 * b["lifetime_rate"], 100 * b["floor"],
                          ("%.1f%%" % (100 * rate)) if rate is not None else "-", verdict))
    holes = coverage_holes(log)
    lines += ["", "COVERAGE HOLES — measured somewhere, never here:"]
    lines += ["  - " + h for h in holes] if holes else ["  (none)"]
    return "\n".join(lines)


# ------------------------------------------------------------------ selftest

def selftest(verbose=True) -> int:
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    today = date(2026, 8, 15)
    recent = (today - timedelta(days=5)).isoformat()
    old = (today - timedelta(days=200)).isoformat()

    log = {"indices": {
        "GOOD": {"sector": {"attempts": 1000, "successes": 999, "last_run": recent,
                            "last_run_rate": 1.0},
                 "overlay_quotesummary": {"attempts": 200, "successes": 70, "last_run": recent,
                                          "last_run_rate": 0.36}},
        "HOLEY": {"sector": {"attempts": 1000, "successes": 998, "last_run": recent,
                             "last_run_rate": 1.0}},
        "DORMANT": {"sector": {"attempts": 1000, "successes": 998, "last_run": old,
                               "last_run_rate": 1.0}},
        "THIN": {"sector": {"attempts": 5, "successes": 5, "last_run": recent,
                            "last_run_rate": 1.0},
                 "overlay_quotesummary": {"attempts": 3, "successes": 1, "last_run": recent,
                                          "last_run_rate": 0.33}},
    }}

    b = derive(log, as_of=today.isoformat())
    ok("GOOD" in b["bands"] and "overlay_quotesummary" in b["bands"]["GOOD"],
       "a metric with enough attempts must get a band")
    ok("THIN" not in b["bands"],
       f"n<{MIN_N_FOR_BAND} must yield NO band, not a band built on nothing (R4.10)")
    ok(any(s["index"] == "THIN" for s in b["no_band"]),
       "a skipped metric must be COUNTED and named, never silently dropped (R4.9)")

    # a chronically-bad source at its usual level does NOT fire — the band is about degradation
    ok(not band_breaches(log, b),
       f"36% against a 35% lifetime rate is not degradation; it is the level. Got: "
       f"{band_breaches(log, b)}")

    # NEGATIVE CONTROL: genuinely degrade it and the band MUST fire
    bad = json.loads(json.dumps(log))
    bad["indices"]["GOOD"]["overlay_quotesummary"]["last_run_rate"] = 0.05
    br = band_breaches(bad, b)
    ok(len(br) == 1 and "GOOD/overlay_quotesummary" in br[0],
       f"a real collapse must breach the band, got {br}")

    # NEGATIVE CONTROL: a perfect source that fails outright must fire too
    bad2 = json.loads(json.dumps(log))
    bad2["indices"]["GOOD"]["sector"]["last_run_rate"] = 0.5
    ok(any("GOOD/sector" in x for x in band_breaches(bad2, b)),
       "a 99.9% source dropping to 50% must breach")

    holes = coverage_holes(log, today)
    ok(any("HOLEY/overlay_quotesummary" in h for h in holes),
       f"an index that never measured a metric others measure must be reported, got {holes}")
    ok(not any("DORMANT" in h for h in holes),
       "an index dormant beyond the staleness window must NOT generate holes — permanent noise "
       "is how a control gets switched off")
    ok(not any("GOOD/" in h for h in holes),
       "an index that measures everything must produce no hole")

    # the derivation is a pure function of the log
    ok(derive(log, as_of=today.isoformat()) == b, "derive() must be deterministic (R14.3)")

    if verbose:
        print(f"degradation_bands selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="R15.2 degradation watch — source coverage bands")
    ap.add_argument("--derive", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.derive:
        b = derive()
        if a.write:
            BANDS_FILE.write_text(json.dumps(b, indent=1, sort_keys=True), encoding="utf-8")
            print(f"wrote {BANDS_FILE.name}: {sum(len(v) for v in b['bands'].values())} bands, "
                  f"{len(b['no_band'])} metrics below n={MIN_N_FOR_BAND}")
        else:
            print(json.dumps(b, indent=1)[:4000])
        return 0
    if a.report:
        print(report())
        return 0
    if a.check:
        v = check()
        print("\n".join(v) if v else "degradation watch: no band breach, no coverage hole")
        return 1 if v else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
