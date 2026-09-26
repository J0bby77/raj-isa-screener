#!/usr/bin/env python3
"""
isa_tree_scope.py — ONE HOME for "which directories are not framework source" (ISA-0710, 25-Sep-2026).

WHY A MODULE OF ITS OWN. release_gate (the signed release fingerprint), framework_atlas (the R15.4 map)
and framework_integrity (the Phase-0 preflight) each declared a private exclusion list and they drifted:
framework_integrity scanned `_candidate_evidence/` and `_dryrun_outputs/`, so on LIVE it enumerated 234
.py files (5 evidence copies of registered computers, incl. a WIP capability_checks) while the certified
Candidate - built without those folders - had 229, and the LIVE preflight reported WARN 10 against the
Candidate's WARN 7. A predicate homed INSIDE release_gate would drag release_gate's lazy imports into the
Composio fallback's import closure (sync_repo_to_github.fallback_input_gaps measured +40 unclassified
inputs), so the rule lives here: stdlib-only, no imports, nothing to drag.

  EXCLUDE_PARTS    PREFIX-matched directory names (backups, archives, caches, evidence, dry runs).
  NON_SOURCE_DIRS  EXACT directory names that hold data, not framework code.
  excluded_dir()   the one predicate; release_gate._iter_source / _tree_shas, framework_atlas._iter_py
                   and framework_integrity.source_files all call it.
consistency_check.pair_excluded_dirs_single_home proves the three enumerations agree (fixture + real tree).
"""
from __future__ import annotations

EXCLUDE_PARTS = ("__pycache__", "archive", "_bak", "_baseline", ".git", "node_modules",
                 "calibration_pathc_jul2026", "_to_delete", "_candidate_evidence", "_dryrun_outputs")
NON_SOURCE_DIRS = ("register_archive", "backfill_source", "screen_history", "bench_cache", "nav_cache",
                   "web", "dist", "Skills_to_Edit")


def excluded_dir(name: str) -> bool:
    """True when a directory NAME is outside the framework source population."""
    return name in NON_SOURCE_DIRS or any(name == x or name.startswith(x) for x in EXCLUDE_PARTS)


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(label, cond):
        if verbose:
            print(("  PASS  " if cond else "  FAIL  ") + label)
        if not cond:
            fails.append(label)

    ok("MUST-FIRE: evidence, dry-run, backup and baseline folders are excluded",
       all(excluded_dir(d) for d in ("_candidate_evidence", "_dryrun_outputs", "_bak_isa0710_25sep2026",
                                     "_baseline_20260621", "archive", "__pycache__", "Skills_to_Edit")))
    ok("NEGATIVE CONTROL: ordinary source folders are NOT excluded (a prefix is not a wildcard; "
       "exact names stay exact)", not any(excluded_dir(d) for d in ("Dashboard", "server", "tests_jul2026",
                                                                    "webhooks", "distribution", "state")))
    if verbose:
        print("isa_tree_scope selftest: %s" % ("PASS" if not fails else "FAIL %s" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
