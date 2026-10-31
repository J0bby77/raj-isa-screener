#!/usr/bin/env python3
"""run_cycle.py - ISA-0910 (28-Oct-2026): the ONE home for a run's CYCLE identity.

THE RULE (Raj, ISA-0795, 30-Sep-2026): a monthly run belongs to the month of the REVIEW SUNDAY it feeds -
the Sunday on or after the run date. The pre-run on Sat 03-Oct-2026 feeds Sun 04-Oct -> `oct_2026`; the
pre-run on Sat 31-Oct-2026 feeds Sun 01-Nov -> `nov_2026`; the review itself (a Sunday) is its own cycle.
For every date whose following Sunday is in the same month this is identical to the calendar month, so it
only changes behaviour in the last days of a month whose 1st Sunday is the 1st-6th of the next.

WHY IT EXISTS. ISA-0795 implemented the rule for the email subject only. Every other cycle identity
(run label, artefact names, receipt month, screen cycle filter, archive month, cycles_seen month) read the
wall clock. MEASURED 28-Oct-2026 in the production parallel at a simulated Sat 31-Oct (the first month-wrap
pre-run): the November run labelled itself `oct_2026`, took the candidate pool from SEPTEMBER screens,
took September as the prior book, and refused (`SHADOW LEDGER IMMUTABILITY: oct_2026 is already frozen`),
so the 1-Nov review would have had no November run context.

WHAT STAYS ON THE REAL DATE. As-of stamps, freshness, ledger entry dates and tax-year arithmetic describe
WHEN something happened and keep `date.today()`. Only "which monthly cycle is this run" comes from here.
consistency_check.pair_cycle_identity_one_home refuses a wall-clock month label anywhere else (R4.4).

Stdlib only. ROLLBACK (R4.13): restore the pre-ISA-0910 files from the build backup; there is no flag,
because a flag that re-enables the wall-clock label re-enables the defect.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional, Union

DateLike = Union[None, str, _dt.date, _dt.datetime]


def _as_date(d: DateLike) -> _dt.date:
    if d is None:
        return _dt.date.today()
    if isinstance(d, _dt.datetime):
        return d.date()
    if isinstance(d, _dt.date):
        return d
    return _dt.date.fromisoformat(str(d)[:10])


def review_sunday(run_date: DateLike = None) -> _dt.date:
    """The review Sunday a run on `run_date` feeds: the Sunday on or after it (ISA-0795)."""
    d = _as_date(run_date)
    return d + _dt.timedelta(days=(6 - d.weekday()) % 7)


def cycle_date(run_date: DateLike = None) -> _dt.date:
    """The date that names the run's monthly cycle (its review Sunday)."""
    return review_sunday(run_date)


def cycle_label(run_date: DateLike = None) -> str:
    """'nov_2026' - the [mmm_yyyy] every month-keyed artefact of the run carries."""
    return cycle_date(run_date).strftime("%b_%Y").lower()


def cycle_run_month(run_date: DateLike = None) -> str:
    """'Nov 2026' - the display form of the cycle."""
    return cycle_date(run_date).strftime("%b %Y")


def cycle_ym(run_date: DateLike = None, sep: str = "-") -> str:
    """'2026-11' (or '2026_11' with sep='_')."""
    c = cycle_date(run_date)
    return "%04d%s%02d" % (c.year, sep, c.month)


def previous_cycle_ym(run_date: DateLike = None, sep: str = "-") -> str:
    """The calendar month before the cycle month - the screen month a pre-run analyses ('2026-10' for nov_2026)."""
    c = cycle_date(run_date)
    y, m = (c.year - 1, 12) if c.month == 1 else (c.year, c.month - 1)
    return "%04d%s%02d" % (y, sep, m)


# ── ISA-0910 class kill: the scanner consistency_check.pair_cycle_identity_one_home reads ────────────────────
import re as _re
import os as _os

# A MONTH LABEL derived straight from the wall clock: date.today()/datetime.now()/utcnow() (optionally wrapped,
# e.g. `(today or date.today())`) formatted as a cycle label ('%b_%Y', '%b %Y', '%b-%Y', '%Y_%m'), or the ISO
# month sliced off today. Real-date stamps ('%Y-%m-%d', '%d %b %Y', isoformat()) are NOT cycle identity.
WALL_CLOCK_LABEL = _re.compile(
    r"(?:today|now|utcnow)\(\)\)?\s*\.strftime\(\s*['\"](?:%b_%Y|%b %Y|%b-%Y|%Y_%m)['\"]"
    r"|(?:today|now|utcnow)\(\)\s*\.isoformat\(\)\s*\[\s*:\s*7\s*\]")
# Deliberate real-date uses that match the shape. Each entry names WHY it is a date, not a cycle.
ALLOWED: dict = {}


def wall_clock_cycle_derivations(root: str):
    """[(file, line_no, text)] for every top-level framework module line that derives a cycle label from the
    wall clock. Selftest/assert lines and this module are exempt (a test may construct the retired shape)."""
    out = []
    for fn in sorted(_os.listdir(root)):
        if not fn.endswith(".py") or fn == "run_cycle.py" or fn.startswith("test_"):
            continue
        try:
            lines = open(_os.path.join(root, fn), encoding="utf-8").read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(lines, 1):
            t = ln.strip()
            if t.startswith("#") or t.startswith("assert ") or not WALL_CLOCK_LABEL.search(ln):
                continue
            if (fn, i) in ALLOWED or fn in ALLOWED:
                continue
            out.append((fn, i, t[:160]))
    return out


def _selftest() -> int:
    D = _dt.date
    # MUST-FIRE (the measured defect): the month-wrap pre-run is November's.
    assert cycle_label(D(2026, 10, 31)) == "nov_2026", "ISA-0910 MUST-FIRE: Sat 31-Oct-2026 feeds Sun 01-Nov"
    assert previous_cycle_ym(D(2026, 10, 31)) == "2026-10", "ISA-0910 MUST-FIRE: the Nov cycle analyses OCTOBER screens"
    assert cycle_ym(D(2026, 10, 31), "_") == "2026_11"
    assert cycle_run_month(D(2026, 10, 31)) == "Nov 2026"
    # Unchanged behaviour: in-month pre-runs and the review Sunday itself.
    assert cycle_label(D(2026, 10, 3)) == "oct_2026", "Sat 03-Oct feeds Sun 04-Oct"
    assert cycle_label(D(2026, 8, 1)) == "aug_2026", "Sat 01-Aug feeds Sun 02-Aug"
    assert cycle_label(D(2026, 9, 5)) == "sep_2026"
    assert cycle_label(D(2026, 11, 1)) == "nov_2026", "a Sunday is its own cycle"
    assert cycle_label(D(2026, 10, 14)) == "oct_2026", "mid-month is the calendar month"
    assert previous_cycle_ym(D(2026, 10, 3)) == "2026-09"
    # Year wrap: Sat 31-Dec-2022 feeds Sun 01-Jan-2023; Jan cycle analyses December.
    assert cycle_label(D(2022, 12, 31)) == "jan_2023" and previous_cycle_ym(D(2022, 12, 31)) == "2022-12"
    assert previous_cycle_ym(D(2027, 1, 3)) == "2026-12"
    # Next month-wrap pre-run after Oct-2026: Sat 31-Jul-2027 -> Aug cycle.
    assert cycle_label(D(2027, 7, 31)) == "aug_2027"
    # Types.
    assert cycle_label("2026-10-31") == "nov_2026" and cycle_label(_dt.datetime(2026, 10, 31, 9, 30)) == "nov_2026"
    # NEGATIVE CONTROL: the retired wall-clock rule disagrees on exactly the month-wrap date (the test can fail).
    assert D(2026, 10, 31).strftime("%b_%Y").lower() != cycle_label(D(2026, 10, 31)), \
        "NEGATIVE CONTROL: the wall-clock label of Sat 31-Oct (oct_2026) must NOT be accepted as its cycle"
    assert D(2026, 10, 3).strftime("%b_%Y").lower() == cycle_label(D(2026, 10, 3)), \
        "NEGATIVE CONTROL: on an in-month date the two rules agree - the fix changes nothing else"
    # R5.14 five-point matrix around the first month-wrap pre-run occurrence (Sat 31-Oct-2026).
    assert cycle_label(D(2026, 10, 30)) == "nov_2026", "R5.14 T-1: Fri 30-Oct-2026 -> nov_2026 (its Sunday is 01-Nov)"
    assert cycle_label(D(2026, 10, 31)) == "nov_2026", "R5.14 T: Sat 31-Oct-2026 is the NOVEMBER cycle"
    assert cycle_label(D(2026, 11, 1)) == "nov_2026", "R5.14 T+1: Sun 01-Nov-2026 is its own cycle"
    assert cycle_label(D(2026, 10, 31)) == cycle_label(D(2026, 10, 31)) == cycle_label("2026-10-31"), \
        "R5.14 repeat_same_occurrence: a re-run of the 31-Oct occurrence gets the same label"
    assert cycle_label(D(2026, 12, 5)) == "dec_2026" and cycle_label(D(2027, 7, 31)) == "aug_2027", \
        "R5.14 next_occurrence: Sat 05-Dec-2026 -> dec_2026; next month-wrap Sat 31-Jul-2027 -> aug_2027"
    # Scanner: must-fire on the retired shape, clean on the cycle form (the pair is only as good as this).
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        open(_os.path.join(td, "m1.py"), "w").write('x = date.today().strftime("%b_%Y").lower()\n')
        open(_os.path.join(td, "m2.py"), "w").write('y = "a_%s" % dt.date.today().strftime("%Y_%m")\n'
                                                    'z = (today or _d.date.today()).strftime("%b %Y")\n')
        open(_os.path.join(td, "m3.py"), "w").write('a = run_cycle.cycle_label()\nb = date.today().isoformat()\n'
                                                    'c = date.today().strftime("%Y-%m-%d")\n# date.today().strftime("%b_%Y")\n')
        f = wall_clock_cycle_derivations(td)
        assert [(x[0], x[1]) for x in f] == [("m1.py", 1), ("m2.py", 1), ("m2.py", 2)], f
    print("run_cycle selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(cycle_label())
