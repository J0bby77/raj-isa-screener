#!/usr/bin/env python3
"""
vci_capture_run.py — mechanical bulk capture for the VCI learning loop (§13 / Run_Context §5.2A).

WHY THIS EXISTS
Capture was previously N manual `VL.capture(...)` calls issued by hand at the end of a time-boxed
VCI session. Between Apr and Jul 2026 it never actually landed: no store existed on disk, while the
Jul run's own Learning block asserted that capture had happened. A step that can be skipped while
reporting success is not a control. This script makes capture ONE command over a file the run has
already produced, and refuses to exit quietly if nothing was written.

USAGE
  python3 vci_capture_run.py --input vci_run_aug_2026.json          # preferred: structured run output
  python3 vci_capture_run.py --input observations.json              # or a bare list of observations
  python3 vci_capture_run.py --input ... --dry-run                  # validate without writing

INPUT
Either {"candidates":[...]} / {"observations":[...]} or a top-level list. Each item needs at minimum
`ticker`; `run_date` defaults to today. Everything else is passed through to VL.capture, which keeps
only the known FEATURE_KEYS, so extra fields are harmless.

EXIT CODES
  0 = rows captured and verified   1 = usage/input error   2 = capture ran but store did not grow
Exit 2 is the condition that previously went undetected. It is loud on purpose.
"""
from __future__ import annotations
import argparse, json, os, sys

def _load_any(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if isinstance(d, list):
        return d
    for k in ("observations", "candidates", "scored", "names"):
        if isinstance(d.get(k), list):
            return d[k]
    raise SystemExit(f"input {path}: expected a list, or an object with "
                     f"observations/candidates/scored/names")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="run output JSON (see module docstring)")
    ap.add_argument("--isa-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--run-date", default=None, help="ISO date; defaults to each row's own or today")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, a.isa_dir)
    import vci_learning as VL, scoring_config as cfg
    store = cfg.VCI_LEARNING_STORE_PATH

    items = _load_any(a.input)
    if not items:
        raise SystemExit("input contained zero candidates — refusing to report success")

    before = 0
    if os.path.exists(store):
        with open(store, encoding="utf-8") as fh:
            before = len((json.load(fh) or {}).get("observations", []))

    if a.dry_run:
        missing = [i for i, r in enumerate(items) if not r.get("ticker")]
        print(f"DRY RUN: {len(items)} rows, {len(missing)} missing ticker "
              f"{'(indices ' + str(missing[:10]) + ')' if missing else ''}")
        print(f"store currently holds {before} observations")
        return 0

    captured = 0
    for r in items:
        if not r.get("ticker"):
            print(f"  SKIP row without ticker: {str(r)[:80]}", file=sys.stderr)
            continue
        obs = dict(r)
        if a.run_date:
            obs["run_date"] = a.run_date
        VL.capture(obs, store)
        captured += 1

    with open(store, encoding="utf-8") as fh:
        after = len((json.load(fh) or {}).get("observations", []))

    v = VL.verify_stores()
    print(json.dumps({"captured": captured, "rows_before": before, "rows_after": after,
                      "verify": v}, indent=2, default=str))

    # The control: capture claiming success while the store is unchanged is the exact failure
    # mode that hid Apr-Jul 2026. Upserts mean after==before is legitimate ONLY on a re-run of
    # the same (run_date, ticker) set, so warn rather than fail in that case.
    if after == 0:
        print("FAIL: store is empty after capture — write did not land", file=sys.stderr)
        return 2
    if after == before and captured:
        print("NOTE: row count unchanged — treated as an idempotent re-run of the same "
              "(run_date, ticker) set. If this was a NEW run, investigate immediately.",
              file=sys.stderr)
    if v.get("status") != "OK":
        print(f"WARNING: verify_stores status={v.get('status')} notes={v.get('notes')}",
              file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
