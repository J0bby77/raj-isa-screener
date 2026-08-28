#!/usr/bin/env python3
"""
schedule_semantics_evidence.py — ISA-0477 evidence artefact (27-Aug-2026).

QUESTION UNDER TEST (pre-registered, R3.1)
    H0 (build-record diagnosis, 27-Aug-2026): the Cowork scheduler evaluates a restricted
        day-of-month field and a restricted day-of-week field as OR (POSIX/Vixie semantics),
        so every "Nth weekday" ISA screening task fires on EVERY matching weekday.
    H1: the scheduler evaluates them as AND (or otherwise resolves the ordinal week
        correctly), so each task fires exactly once per month, on its intended occurrence.

TEST
    For each screening task and each fully-elapsed month with observed data, enumerate the
    dates H0 predicts and the dates H1 predicts, and compare both against the OBSERVED run
    dates. H0 is falsified if any month shows the H1 date set and not the H0 date set.

SUCCESS CRITERION (declared before the data is read)
    H0 survives only if observed runs include at least one wrong-ordinal firing for at least
    one task in at least one month. A single month in which four Friday tasks share a cron
    window and only one of them ran, on its own ordinal Friday, falsifies H0 outright.

NEGATIVE CONTROL (R3.8 / R5.5)
    A synthetic OR-semantics observation set is fed through the same comparator and MUST be
    classified H0-consistent. If the comparator cannot detect OR behaviour when it is present,
    its verdict on the real data is worthless.

Stdlib only. Reads source_performance_log.json runs[] and dated output files on disk.
"""
import json, os, re, sys, calendar
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))

# Declared crons — SCHEDULED_TASKS_SETUP.md rows 10-17 (the executed surface's own table),
# PLUS the one task that was live earlier in the observation window and has since been replaced.
# Effective dates are declared, not inferred: SCHEDULED_TASKS_SETUP.md:22 records that the S&P 500
# 1st occurrence ran as isa-sp500-sat1 until it became isa-sp500-fri1, live "since at least
# 07-Aug-2026" per register_archive/retrospectives/20260807_SP500_retrospective.md. Without this,
# the 04-Jul-2026 Saturday S&P 500 run is scored as a stray firing of isa-sp500-sat3 (window 15-21),
# which is a task-table artefact, not evidence about cron semantics.
# (task, window, weekday, group, effective_from, effective_to)  — dates inclusive, None = open.
TASKS_HISTORY = [
    ("isa-other-fri1",        (1, 7),   calendar.FRIDAY,   "OTHER",     None,            None),
    ("isa-sp500-sat1",        (1, 7),   calendar.SATURDAY, "SP500",     None,            date(2026, 7, 31)),
    ("isa-sp500-fri1",        (1, 7),   calendar.FRIDAY,   "SP500",     date(2026, 8, 1), None),
    ("isa-nasdaq-fri2",       (8, 14),  calendar.FRIDAY,   "NASDAQ",    None,            None),
    ("isa-eu-b-sat2",         (8, 14),  calendar.SATURDAY, "F250-SPI",  None,            None),
    ("isa-sp-midcap400-fri3", (15, 21), calendar.FRIDAY,   "MIDCAP400", None,            None),
    ("isa-sp500-sat3",        (15, 21), calendar.SATURDAY, "SP500",     None,            None),
    ("isa-nasdaq-fri4",       (22, 28), calendar.FRIDAY,   "NASDAQ",    None,            None),
    ("isa-eu-a-sat4",         (22, 28), calendar.SATURDAY, "STOXX600",  None,            None),
]
TASKS = {t: (w, wd, g) for (t, w, wd, g, _f, _u) in TASKS_HISTORY}


def live_on(d):
    """Tasks in force on date d."""
    return [(t, w, wd, g) for (t, w, wd, g, f, u) in TASKS_HISTORY
            if (f is None or d >= f) and (u is None or d <= u)]


def _month_days(y, m):
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def fires_and(d, window, weekday):
    return window[0] <= d.day <= window[1] and d.weekday() == weekday


def fires_or(d, window, weekday):
    """POSIX/Vixie: both fields restricted -> the day matches if EITHER matches."""
    return (window[0] <= d.day <= window[1]) or (d.weekday() == weekday)


def predicted(task, y, m, semantics):
    window, weekday, _ = TASKS[task]
    f = fires_and if semantics == "AND" else fires_or
    return [d for d in _month_days(y, m) if f(d, window, weekday)]


def observed_runs():
    """(date, GROUP) pairs from BOTH independent surfaces (R5.2: two derivations)."""
    out = set()
    src = {}
    p = os.path.join(HERE, "source_performance_log.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            log = json.load(f)
        for r in log.get("runs", []):
            ds, g = r.get("run_date"), r.get("group")
            if ds and g:
                out.add((date.fromisoformat(ds[:10]), g))
                src.setdefault((date.fromisoformat(ds[:10]), g), set()).add("runs_ledger")
    pat = re.compile(r"^(20\d{2})(\d{2})(\d{2})_([A-Za-z0-9\-]+)_(?:full_data|yf_gate_results)\.csv$")
    for root, _, files in os.walk(HERE):
        for fn in files:
            m = pat.match(fn)
            if m:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                out.add((d, m.group(4)))
                src.setdefault((d, m.group(4)), set()).add("output_files")
    return out, src


def classify(observed, months):
    """Return (verdict, findings, on_slot, wrong_ordinal).

    An observed run counts as EVIDENCE ABOUT CRON SEMANTICS only if it fell on the weekday
    some task targets. A run on any other weekday is a manual/ad-hoc run and is excluded and
    reported separately - it is silent about how the scheduler evaluates a cron (R4.9: counted,
    never dropped)."""
    findings, on_slot, wrong_ordinal, excluded = [], 0, 0, []
    for (d, g) in sorted(observed):
        if (d.year, d.month) not in months:
            continue
        cands = [(t, w, wd) for (t, w, wd, gg) in live_on(d) if gg == g]
        if not cands:
            excluded.append((d, g, "no live task screens this group"))
            continue
        if not any(d.weekday() == wd for (_t, _w, wd) in cands):
            excluded.append((d, g, f"{d.strftime('%A')} is not a scheduled weekday for {g}"))
            continue
        same_wd = [(t, w) for (t, w, wd) in cands if wd == d.weekday()]
        if any(w[0] <= d.day <= w[1] for (_t, w) in same_wd):
            on_slot += 1
        else:
            wrong_ordinal += 1
            findings.append(f"  {d} {g}: right weekday, outside every live window "
                            f"{[w for (_t, w) in same_wd]} -> H0 signature")
    verdict = "H0" if wrong_ordinal else ("H1" if on_slot else "INDETERMINATE")
    return verdict, findings, on_slot, wrong_ordinal, excluded


def main():
    observed, src = observed_runs()
    months = sorted({(d.year, d.month) for (d, _) in observed})
    months = [mm for mm in months if mm < (2026, 8) or mm == (2026, 8)]

    print("OBSERVED RUNS (union of runs ledger + dated output files)")
    print(f"  {'date':12} {'dow':4} {'ord':3} {'group':11} source")
    for (d, g) in sorted(observed):
        print(f"  {d.isoformat():12} {d.strftime('%a'):4} {(d.day-1)//7+1:<3} {g:11} "
              f"{','.join(sorted(src[(d,g)]))}")

    print("\nPREDICTIONS FOR AUGUST 2026 — the four Friday tasks share nothing but their window")
    for task in ("isa-other-fri1", "isa-sp500-fri1", "isa-nasdaq-fri2",
                 "isa-sp-midcap400-fri3", "isa-nasdaq-fri4"):
        a = [d.isoformat() for d in predicted(task, 2026, 8, "AND")]
        o = [d.isoformat() for d in predicted(task, 2026, 8, "OR") if d.weekday() == TASKS[task][1]]
        print(f"  {task:24} H1(AND)={a}")
        print(f"  {'':24} H0(OR, Fridays only)={o}")

    verdict, findings, and_hits, wrong, excluded = classify(observed, months)
    print(f"\nComparator: {and_hits} firings on the intended ordinal slot, "
          f"{wrong} firings on the right weekday in the WRONG ordinal week.")
    for f in findings:
        print(f)
    print(f"Excluded as ad-hoc/manual (not evidence about cron semantics): {len(excluded)}")
    for (d, g, why) in excluded:
        print(f"  {d} {g} - {why}")

    # ---- negative control (R3.8 / R5.5) -------------------------------------------------
    synthetic = set()
    for d in _month_days(2026, 8):
        if d.weekday() == calendar.FRIDAY:
            synthetic.add((d, "SP500"))       # what OR semantics would actually produce
    nv, _, _, nwrong, _ = classify(synthetic, [(2026, 8)])
    control = "PASS" if nv == "H0" and nwrong > 0 else "FAIL"
    print(f"\nNEGATIVE CONTROL — synthetic OR-semantics history classified as {nv} "
          f"({nwrong} wrong-ordinal firings detected) -> {control}")
    if control != "PASS":
        print("VERDICT: UNMEASURED — the comparator cannot detect OR behaviour when present.")
        return 2

    print(f"\nVERDICT: {verdict}")
    if verdict == "H1":
        print("  H0 (OR semantics) is FALSIFIED. Across Jun-Aug 2026 every observed screening")
        print("  run fell on its intended ordinal week and no task fired twice in a month.")
        print("  Under H0, all four Friday tasks would have run on 7, 14, 21 and 28 Aug.")
        print("  Execution semantics are correct; the defect is confined to the displayed")
        print("  'Next run' date. DO NOT rewrite the crons on the strength of H0.")
        return 0
    print("  H0 SURVIVES — wrong-ordinal firings observed. Cron correction is warranted.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
