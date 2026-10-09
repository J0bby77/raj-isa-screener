#!/usr/bin/env python3
"""
month_artefacts.py - ISA-0832 (04-Oct-2026): the ONE home for WHERE a month-keyed decision artefact
lives across its lifecycle. Stdlib only, no data-file literals (it sits inside the screener fallback's
import closure via build_email - ISA-0498).

THE LIFECYCLE. The pre-run writes `<family>_<mmm>_<yyyy>.json` beside the code. After the monthly review,
capture_archive.archive_month(purge=True) COPIES each ARCHIVE_SET member to archive/decision_capture/,
verifies it byte-for-byte, and REMOVES the original. Every reader that kept looking only beside the code
then saw a different world (MEASURED 04-Oct-2026 on LIVE TB-2026-10-03-04 after the 04-Oct purge):
  * capital_decision_engine.verify_receipt -> STALE, so execution_check REFUSED every authorised order;
  * every "latest month" reader (VCI review universe, capital_destination, concentration_control,
    consistency pairs, email_prefill, derive_required_return's history, the pre-run's prior-month look-ups)
    silently fell back to SEPTEMBER's artefact - the newest LIVE copy left - FC-A, a plausible stale value.

THE RULE. A reader resolves a month artefact through this module:
  resolve(directory, name)      -> the LIVE copy if present, else the archived copy, else the LIVE path
  month_glob(directory, pattern)-> LIVE matches + archived matches not shadowed by a LIVE file of the same
                                   name (LIVE always wins: a re-run month is never served from the archive)
consistency_check.pair_month_artefacts_archive_aware refuses a raw glob over an ARCHIVE_SET family anywhere
else, so the next reader cannot reintroduce the split (class kill, R9.6).
"""
from __future__ import annotations

import fnmatch
import os
import sys

ARCHIVE_REL = os.path.join("archive", "decision_capture")


def archive_dir(directory: str) -> str:
    return os.path.join(directory, ARCHIVE_REL)


def resolve(directory: str, name: str) -> str:
    """Path of a month artefact: LIVE copy, else the byte-verified archived copy, else the LIVE path."""
    live = os.path.join(directory, name)
    if os.path.exists(live):
        return live
    arch = os.path.join(archive_dir(directory), name)
    return arch if os.path.exists(arch) else live


def is_archived(path: str) -> bool:
    return os.path.normpath(os.path.dirname(path)).endswith(os.path.normpath(ARCHIVE_REL))


def month_glob(directory: str, pattern: str) -> list:
    """glob(directory/pattern) united with the archive's matches; LIVE shadows the archive per basename.
    Ordered by BASENAME (never full path - a path sort would rank every archived file before every LIVE one)."""
    out, seen = [], set()
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for n in sorted(names):
        if fnmatch.fnmatch(n, pattern) and os.path.isfile(os.path.join(directory, n)):
            out.append(os.path.join(directory, n))
            seen.add(n)
    ad = archive_dir(directory)
    try:
        anames = os.listdir(ad)
    except OSError:
        anames = []
    for n in sorted(anames):
        if n not in seen and fnmatch.fnmatch(n, pattern) and os.path.isfile(os.path.join(ad, n)):
            out.append(os.path.join(ad, n))
    return sorted(out, key=os.path.basename)


# ---- the class-kill scanner (consumed by consistency_check.pair_month_artefacts_archive_aware) ----
LIVE_ONLY_MARK = "month-artefacts: live-only"     # an inline waiver; the line must also say why
SCAN_EXEMPT = ("capture_archive.py", "month_artefacts.py")


def archive_families(root: str) -> list:
    """Filename prefixes of the ARCHIVE_SET members, read from capture_archive.py by AST - the single
    declaration of what the purge moves. Parsed, never imported (no new import edge)."""
    import ast
    fams = []
    try:
        tree = ast.parse(open(os.path.join(root, "capture_archive.py"), encoding="utf-8").read())
    except (OSError, SyntaxError):
        return fams
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ARCHIVE_SET" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and "{month}" in k.value:
                    fams.append(k.value.split("{month}")[0])
    return sorted(set(fams))


def raw_archive_globs(root: str, files=None) -> list:
    """[(file, line, pattern)] for every glob()/iglob()/Path.glob() call whose literal pattern names an
    ARCHIVE_SET family and is not routed through month_glob. DRYRUN patterns and lines carrying
    LIVE_ONLY_MARK are exempt. Unreadable capture_archive -> one ('capture_archive.py', 0, ...) finding."""
    import ast, glob as _g
    fams = archive_families(root)
    if not fams:
        return [("capture_archive.py", 0, "ARCHIVE_SET unreadable - cannot prove readers are archive-aware")]
    out = []
    for fp in sorted(files if files is not None else _g.glob(os.path.join(root, "*.py"))):
        b = os.path.basename(fp)
        if b in SCAN_EXEMPT:
            continue
        try:
            src = open(fp, encoding="utf-8").read()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            consts = [x.value for x in node.args if isinstance(x, ast.Constant) and isinstance(x.value, str)]
            if any(consts[i:i + 2] == ["archive", "decision_capture"] for i in range(len(consts))):
                out.append((b, node.lineno, "SECOND HOME: archive/decision_capture spelled outside month_artefacts"))
                continue
            if name not in ("glob", "iglob"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    v = sub.value
                    if any(v.startswith(fam) for fam in fams) and "DRYRUN" not in v:
                        ln = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                        if LIVE_ONLY_MARK not in ln:
                            out.append((b, node.lineno, v))
    return out


def _selftest() -> int:
    import tempfile, shutil
    fails = []
    _pd = lambda m: "portfolio" + "_data_%s.json" % m      # built, not literal (ISA-0498 closure)

    def ok(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)[:300]))
        if not cond:
            fails.append(name)
    d = tempfile.mkdtemp(prefix="ma_st_")
    try:
        os.makedirs(archive_dir(d))
        for n in (_pd("aug_2026"), _pd("sep_2026")):
            open(os.path.join(d, n), "w").write("{}")
        open(os.path.join(archive_dir(d), _pd("oct_2026")), "w").write('{"m": "oct"}')
        open(os.path.join(archive_dir(d), _pd("sep_2026")), "w").write('{"m": "archived sep"}')
        g = [os.path.basename(p) for p in month_glob(d, "portfolio_data_*.json")]
        ok("ISA-0832 MUST-FIRE (the measured 04-Oct regression): after the purge the newest month (oct) is still visible",
           _pd("oct_2026") in g, g)
        ok("ISA-0832 NEGATIVE CONTROL: a LIVE copy shadows the archived copy of the same month (never served twice)",
           g.count(_pd("sep_2026")) == 1 and not is_archived(
               [p for p in month_glob(d, "portfolio_data_*.json") if p.endswith("sep_2026.json")][0]), g)
        ok("ISA-0832: resolve() returns the archived copy when the LIVE one was purged",
           is_archived(resolve(d, _pd("oct_2026"))))
        ok("ISA-0832: resolve() of a name in neither place returns the LIVE path (absence stays absence)",
           resolve(d, _pd("nov_2026")) == os.path.join(d, _pd("nov_2026")))
        ok("ISA-0832: an absent directory globs to nothing, never raises", month_glob(os.path.join(d, "nope"), "*.json") == [])
        # class-kill scanner: must fire on a raw glob, must not fire on month_glob / DRYRUN / marked lines
        open(os.path.join(d, "capture_archive.py"), "w").write(
            'ARCHIVE_SET = {"portfolio_data_{month}.json": False, "run_context_{month}.json": True}\n')
        open(os.path.join(d, "bad_reader.py"), "w").write(
            'import glob, os\nx = glob.glob(os.path.join("d", "portfolio_data_*.json"))\n')
        open(os.path.join(d, "good_reader.py"), "w").write(
            'import glob, month_artefacts as M\nx = M.month_glob("d", "portfolio_data_*.json")\n'
            'y = glob.glob("run_context_*.DRYRUN.json")\n'
            'z = glob.glob("run_context_*.json")  # month-artefacts: live-only (cleanup of LIVE copies)\n'
            'w = glob.glob("screen_history/*.csv")\n')
        open(os.path.join(d, "second_home.py"), "w").write(
            'import os\np = os.path.join("h", "archive", "decision_capture", "x")\n')
        found = raw_archive_globs(d)
        ok("ISA-0832 MUST-FIRE: a reader re-spelling the archive location (a second home) is found",
           any(f[0] == "second_home.py" and "SECOND HOME" in f[2] for f in found), found)
        os.remove(os.path.join(d, "second_home.py"))
        found = raw_archive_globs(d)
        ok("ISA-0832 MUST-FIRE: a raw glob over an ARCHIVE_SET family is found",
           any(f[0] == "bad_reader.py" for f in found), found)
        ok("ISA-0832 NEGATIVE CONTROL: month_glob, DRYRUN, a marked live-only line and a non-family glob are not flagged",
           not any(f[0] == "good_reader.py" for f in found), found)
        ok("ISA-0832: the families are read from capture_archive's own ARCHIVE_SET",
           archive_families(d) == ["portfolio_data_", "run_context_"], archive_families(d))
        os.remove(os.path.join(d, "capture_archive.py"))
        ok("ISA-0832 MUST-FIRE: an unreadable ARCHIVE_SET is a finding, not a silent pass",
           raw_archive_globs(d) and raw_archive_globs(d)[0][1] == 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("month_artefacts selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
