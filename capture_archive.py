#!/usr/bin/env python3
"""
capture_archive.py — Capture Layer Item 0 (Dashboard Spec §7.6.1). 02-Aug-2026.

THE DEFECT THIS CLOSES
----------------------
Run_Context post-run cleanup item 8 deleted `step9_pre_[mmm_yyyy].json` after the email sent.
That file is the ONLY record of the Step 9A inputs, so every month the reasoning behind the
most consequential decision of the run became unreconstructable. Acceptance criterion #19 of
the dashboard spec ("history never recorded cannot be reconstructed") was unachievable
precisely where judgement matters most.

WHAT THIS DOES
--------------
Splits post-run housekeeping into two explicitly-named sets and makes the split MECHANICAL
rather than a prose instruction a run can skip:

  ARCHIVE_SET  — moved into archive/decision_capture/ and kept forever.
  PURGE_SET    — genuinely regenerable bulk; deleted, but ONLY after the archive has been
                 verified byte-for-byte on disk.

Ordering is not incidental: archive first, verify, purge second. A failed archive aborts the
purge, so the failure mode is "disk holds too much", never "disk holds nothing".

Idempotent. Re-running is a no-op that re-verifies. Never overwrites an existing archived
file with a different one — a collision is an ERROR, because that means a month was re-run
and the original decision inputs would otherwise be silently replaced.

CLI:
  python3 capture_archive.py --month aug_2026                # archive + verify (no purge)
  python3 capture_archive.py --month aug_2026 --purge        # archive, verify, then purge
  python3 capture_archive.py --month aug_2026 --dry_run
  python3 capture_archive.py --selftest

Library:
  from capture_archive import archive_month, verify_month
  res = archive_month("aug_2026", purge=True)

Stdlib only.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(HERE, "archive", "decision_capture")

# Files that ARE the decision record. Never deleted. {template: required?}
# required=True  -> absence is an ERROR (the run should have produced it)
# required=False -> absence is fine (capture layer item not yet live for that month)
ARCHIVE_SET = {
    "step9_pre_{month}.json":        True,   # §7.6.1 — Step 9A inputs
    "action_stack_{month}.json":     True,   # Step 8 output
    "run_context_{month}.json":      True,   # pre-run status/warnings/errors
    "entry_level_audit_{month}.json": True,  # §7.6A.5
    "step9_conviction_{month}.json": False,  # §7.6.2 — Item 3, live from Sep-2026
    "intelligence_{month}.json":     False,  # §7.7 — appended by both passes, live from Sep-2026
    "missed_opportunity_{month}.json": False,  # §7.2 — MOA, retrospective by construction
    "run_manifest_{month}.json":     False,  # §7.6A  — Item 2, live from Sep-2026
    "email_data_{month}.json":       False,  # what the email actually said
    "analytics_data_{month}.json":   False,
    "portfolio_data_{month}.json":   False,
    "xray_data_{month}.json":        False,
    "transactions_data_{month}.json": False,
    "calibration_report_{month}.md": False,
}

# Regenerable bulk. Purged only after ARCHIVE_SET is verified on disk.
PURGE_SET = (
    "watchlist_metrics_{month}.json",
    "watchlist_scored_{month}.json",
)

# Explicitly NEVER purged, whatever else changes. Guard against a future edit widening
# PURGE_SET by accident.
NEVER_PURGE = (
    "transaction_ledger.json", "decision_ledger.json", "score_panel.csv",
    "gate_variables.csv", "shadow_ledger.json", "watchlist_tickers.json",
    "calibration_registry.json", "vci_learning_store.json",
)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _paths(month, here=None, archive_dir=None):
    here = here or HERE
    archive_dir = archive_dir or os.path.join(here, "archive", "decision_capture")
    return here, archive_dir


def archive_month(month, purge=False, dry_run=False, here=None, archive_dir=None):
    """Archive the decision-capture set for `month` (e.g. 'aug_2026').

    Returns a dict: {archived[], already[], missing_required[], missing_optional[],
                     collisions[], purged[], purge_blocked, errors[]}
    """
    here, archive_dir = _paths(month, here, archive_dir)
    res = {"month": month, "archived": [], "already": [], "missing_required": [],
           "missing_optional": [], "collisions": [], "purged": [], "purge_blocked": False,
           "errors": []}
    if not dry_run:
        os.makedirs(archive_dir, exist_ok=True)

    for tmpl, required in ARCHIVE_SET.items():
        name = tmpl.format(month=month)
        src, dst = os.path.join(here, name), os.path.join(archive_dir, name)
        src_exists, dst_exists = os.path.exists(src), os.path.exists(dst)

        if not src_exists and not dst_exists:
            (res["missing_required"] if required else res["missing_optional"]).append(name)
            continue
        if not src_exists and dst_exists:
            res["already"].append(name)          # archived on a previous pass
            continue
        if dst_exists:
            # Collision. Identical content = benign idempotent re-run. Different = ERROR:
            # a re-run must never overwrite the original decision inputs of a sent month.
            if _sha256(src) == _sha256(dst):
                res["already"].append(name)
                continue
            res["collisions"].append(name)
            res["errors"].append(
                f"CAPTURE_ARCHIVE_COLLISION: {name} already archived with different content — "
                f"refusing to overwrite. Resolve by hand (suffix the new file) before purging.")
            continue
        if dry_run:
            res["archived"].append(name)
            continue
        # COPY then verify then remove the source — a move that fails mid-way must not lose it.
        shutil.copy2(src, dst)
        if _sha256(src) != _sha256(dst):
            res["errors"].append(f"CAPTURE_ARCHIVE_VERIFY_FAIL: {name} copied but hash differs")
            continue
        os.remove(src)
        res["archived"].append(name)

    # ---- purge gate -------------------------------------------------------------------
    blocking = res["missing_required"] or res["collisions"] or res["errors"]
    if purge and blocking:
        res["purge_blocked"] = True
        res["errors"].append(
            "PURGE_BLOCKED: archive incomplete — "
            f"missing_required={res['missing_required']} collisions={res['collisions']}. "
            "Nothing was deleted. This is the intended failure mode.")
    elif purge:
        for tmpl in PURGE_SET:
            name = tmpl.format(month=month)
            if name in NEVER_PURGE:
                res["errors"].append(f"PURGE_SET/NEVER_PURGE conflict on {name}")
                continue
            p = os.path.join(here, name)
            if os.path.exists(p):
                if not dry_run:
                    os.remove(p)
                res["purged"].append(name)
    return res


def verify_month(month, here=None, archive_dir=None):
    """Read-only: is this month's decision record complete in the archive?"""
    here, archive_dir = _paths(month, here, archive_dir)
    present, absent = [], []
    for tmpl, required in ARCHIVE_SET.items():
        if not required:
            continue
        name = tmpl.format(month=month)
        if os.path.exists(os.path.join(archive_dir, name)) or os.path.exists(os.path.join(here, name)):
            present.append(name)
        else:
            absent.append(name)
    return {"month": month, "complete": not absent, "present": present, "absent": absent}


def _selftest():
    """U-CA1..U-CA5: archive, idempotence, collision refusal, purge gate, never-purge."""
    fails = []

    def ok(label, cond):
        print(("  PASS  " if cond else "  FAIL  ") + label)
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        arc = os.path.join(td, "archive", "decision_capture")
        month = "sep_2026"

        def touch(name, body="x"):
            with open(os.path.join(td, name), "w") as f:
                f.write(body)

        for t, req in ARCHIVE_SET.items():
            if req:
                touch(t.format(month=month), "orig-" + t)
        for t in PURGE_SET:
            touch(t.format(month=month), "bulk")

        r = archive_month(month, purge=True, here=td, archive_dir=arc)
        ok("U-CA1 required files archived", len(r["archived"]) >= 4 and not r["errors"])
        ok("U-CA1b step9_pre archived, not deleted",
           os.path.exists(os.path.join(arc, f"step9_pre_{month}.json"))
           and not os.path.exists(os.path.join(td, f"step9_pre_{month}.json")))
        ok("U-CA2 purge ran once archive verified", len(r["purged"]) == len(PURGE_SET))

        r2 = archive_month(month, purge=True, here=td, archive_dir=arc)
        ok("U-CA3 idempotent re-run is a no-op", not r2["archived"] and not r2["errors"]
           and len(r2["already"]) >= 4)

        # collision: a re-run regenerates step9_pre with different content
        touch(f"step9_pre_{month}.json", "DIFFERENT")
        r3 = archive_month(month, purge=True, here=td, archive_dir=arc)
        ok("U-CA4 differing re-archive refused", bool(r3["collisions"]) and r3["purge_blocked"])
        ok("U-CA4b original archived copy untouched",
           open(os.path.join(arc, f"step9_pre_{month}.json")).read().startswith("orig-"))

        # purge gate: missing required file blocks the purge
        month2 = "oct_2026"
        touch(f"action_stack_{month2}.json")
        for t in PURGE_SET:
            touch(t.format(month=month2), "bulk")
        r4 = archive_month(month2, purge=True, here=td, archive_dir=arc)
        ok("U-CA5 missing required blocks purge", r4["purge_blocked"] and not r4["purged"]
           and os.path.exists(os.path.join(td, PURGE_SET[0].format(month=month2))))

        ok("U-CA6 no NEVER_PURGE name is in PURGE_SET",
           not (set(NEVER_PURGE) & {t.format(month="x") for t in PURGE_SET}))

    print(("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)})"))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="e.g. aug_2026")
    ap.add_argument("--purge", action="store_true",
                    help="delete the regenerable bulk set AFTER the archive verifies")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="read-only completeness check")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.month:
        ap.error("--month required")
    if a.verify:
        v = verify_month(a.month)
        print(json.dumps(v, indent=2))
        return 0 if v["complete"] else 1
    r = archive_month(a.month, purge=a.purge, dry_run=a.dry_run)
    print(json.dumps(r, indent=2))
    for e in r["errors"]:
        print("ERROR: " + e, file=sys.stderr)
    return 1 if r["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
