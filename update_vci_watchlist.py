#!/usr/bin/env python3
"""
update_vci_watchlist.py — the ONE writer of the VCI asymmetric watchlist (ISA-0796, 10-Oct-2026).

The watchlist's canonical home is `Dashboard/state/vci_watchlist.json` (vci_watchlist_store). This script
applies a VCI run's JSON update to it, appends the history line and re-renders the generated view
`project_isa_vci_watchlist.md` beside the code. It NEVER reads or writes an assistant-memory file.

Usage (VCI run, post-run update):
    python update_vci_watchlist.py --update update_vci_watchlist_<mmm>_<yyyy>.json [--run-date YYYY-MM-DD]
    python update_vci_watchlist.py --update <json> --dry-run          (print the view; write nothing)
    python update_vci_watchlist.py --render-only                       (re-render the view from the store)
    python update_vci_watchlist.py --bootstrap-from <legacy.md> --run-date YYYY-MM-DD
                                                                       (ONE-TIME seed; refuses if a store exists)

JSON update schema: update_vci_watchlist_TEMPLATE.json. Required: `watchlist` (list, max 5 - a sixth is
trimmed by ACS and the trim is recorded) and `change_log_entry` {month, additions, removals, acs_changes,
notes}. Optional (KEPT when omitted, never blanked): rescore_calendar, excluded, amba_monitor,
nvidia_class_note, narrative_sections [{heading, body_md}]. Row fields: rank, ticker, company, exchange,
acs (int, or null = UNMEASURED - never 0), source_score, asym, asym_p25, floor, 3yr_pos_pct, nvidia_signals,
deploy, classification, entry_level, entry_trigger, last_scored, status, note.

⚑ --watchlist (the retired memory-file path) is REFUSED with exit 2: the old behaviour on a missing memory
file was to "generate from scratch", silently dropping the excluded list, re-score calendar and change log.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import vci_watchlist_store as S                                         # noqa: E402

RETIRED_FLAG_MSG = ("ISA-0796: --watchlist (the memory-file path) is RETIRED. The VCI watchlist lives in "
                    "Investment Analysis/Dashboard/state/vci_watchlist.json; run "
                    "`python update_vci_watchlist.py --update <update_json>` (no --watchlist). "
                    "The generated view is Investment Analysis/project_isa_vci_watchlist.md.")


def run(argv=None, root: str = HERE) -> int:
    ap = argparse.ArgumentParser(description="Apply a VCI run's update to the canonical VCI watchlist store (ISA-0796).")
    ap.add_argument("--update", metavar="PATH")
    ap.add_argument("--run-date", metavar="YYYY-MM-DD")
    ap.add_argument("--root", default=root, help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--bootstrap-from", metavar="LEGACY_MD")
    ap.add_argument("--watchlist", metavar="RETIRED", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.watchlist:
        print("REFUSED " + RETIRED_FLAG_MSG)
        return 2
    rt = a.root
    if a.render_only:
        doc = S.load(rt)
        with open(S.view_path(rt), "w", encoding="utf-8") as fh:
            fh.write(S.render_markdown(doc))
        print("VIEW_RENDERED %s (store revision %s)" % (S.view_path(rt), doc.get("revision")))
        return 0
    if a.bootstrap_from:
        if os.path.exists(S.store_path(rt)):
            print("REFUSED ISA-0796: a store already exists at %s - bootstrap is one-time; apply an --update instead"
                  % S.store_path(rt))
            return 2
        doc = S.parse_legacy_markdown(open(a.bootstrap_from, encoding="utf-8").read())
        if a.run_date:
            doc["as_of"] = a.run_date
        doc["revision"] = 1
        doc["provenance"] = {"bootstrap_from": os.path.basename(a.bootstrap_from),
                             "bootstrapped_at": datetime.datetime.now().isoformat(timespec="seconds")}
        doc["change_log"] = [{"month": "store", "additions": "—", "removals": "—", "acs_changes": "—",
                              "notes": "ISA-0796: store bootstrapped from %s (legacy memory-file content, last VCI "
                                       "run %s); no membership change" % (os.path.basename(a.bootstrap_from), doc.get("as_of"))}
                             ] + list(doc.get("change_log") or [])
        if a.dry_run:
            print(S.render_markdown(doc))
            return 0
        line = S.write(rt, doc, writer="bootstrap")
        print("STORE_BOOTSTRAPPED revision %s members %s" % (line["revision"], line["members"]))
        return 0
    if not a.update:
        ap.error("--update is required (or --render-only / --bootstrap-from)")
    try:
        with open(a.update, encoding="utf-8") as fh:
            upd = json.load(fh)
    except Exception as exc:                                            # noqa: BLE001
        print("REFUSED ISA-0796: update JSON unreadable (%s)" % exc)
        return 1
    run_date = a.run_date or upd.get("run_date") or datetime.date.today().isoformat()
    try:
        prev = S.load(rt)
        if S.is_repeat(prev, upd, run_date):
            print("STORE_UNCHANGED identical update already applied for %s (revision %s) - nothing written (R5.14)"
                  % (run_date, prev.get("revision")))
            return 0
        doc = S.apply_update(prev, upd, run_date)
    except S.StoreError as exc:
        print("REFUSED %s" % exc)
        return 1
    if a.dry_run:
        print(S.render_markdown(doc))
        return 0
    line = S.write(rt, doc, prev=prev)
    print("STORE_UPDATED revision %s as_of %s members %s added %s removed %s%s"
          % (line["revision"], doc["as_of"], line["members"], line["added"], line["removed"],
             (" MAX5_TRIMMED %s" % line["max5_dropped"]) if line.get("max5_dropped") else ""))
    print("VIEW %s" % S.view_path(rt))
    return 0


def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    import io
    import contextlib
    fails = []

    def ok(name, cond, detail=""):
        if verbose or not cond:
            print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:300]) if not cond else ""))
        if not cond:
            fails.append(name)

    root = tempfile.mkdtemp(prefix="uvw_")
    try:
        legacy = os.path.join(root, "legacy.md")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write(S._LEGACY_FIXTURE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run(["--watchlist", legacy, "--update", legacy], root=root)
        ok("MUST-FIRE: the retired --watchlist memory path is REFUSED (exit 2) and names the canonical store",
           rc == 2 and "vci_watchlist.json" in out.getvalue(), out.getvalue())
        upd = os.path.join(root, "u.json")
        with open(upd, "w") as fh:
            json.dump({"run_month": "October 2026", "watchlist": [{"ticker": "INFQ", "acs": 70}],
                       "change_log_entry": {"month": "Oct-26"}}, fh)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(["--update", upd], root=root)
        ok("NEGATIVE CONTROL (the 11-Oct failure mode): an update with NO store is REFUSED - never 'generated "
           "from scratch' with the excluded list, calendar and change log dropped",
           rc == 1 and not os.path.exists(S.store_path(root)) and not os.path.exists(S.view_path(root)))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(["--bootstrap-from", legacy], root=root)
        ok("bootstrap seeds the store and the view once", rc == 0 and S.members(root) == ["INFQ", "IONQ"])
        with contextlib.redirect_stdout(io.StringIO()):
            rc2 = run(["--bootstrap-from", legacy], root=root)
        ok("NEGATIVE CONTROL: a second bootstrap over an existing store is REFUSED", rc2 == 2)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(["--update", upd, "--run-date", "2026-10-11"], root=root)
        d = S.load(root)
        ok("MUST-FIRE (end to end): the VCI update lands in the store, the view and the history",
           rc == 0 and S.members(root) == ["INFQ"] and d["as_of"] == "2026-10-11" and d["excluded"]
           and "lastUpdated: 2026-10-11" in open(S.view_path(root), encoding="utf-8").read()
           and len(open(S.history_path(root)).readlines()) == 2, d.get("last_write"))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(["--update", upd, "--run-date", "2026-10-11"], root=root)
        ok("MUST-FIRE (R5.14 repeat): re-running the identical post-run update writes nothing (no 3rd history line)",
           rc == 0 and len(open(S.history_path(root)).readlines()) == 2 and S.load(root)["revision"] == 2)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(["--update", upd, "--run-date", "2026-10-12", "--dry-run"], root=root)
        ok("NEGATIVE CONTROL: --dry-run writes nothing", rc == 0 and S.load(root)["as_of"] == "2026-10-11")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if verbose:
        print("update_vci_watchlist selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    sys.exit(run())
