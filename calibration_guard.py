#!/usr/bin/env python3
"""
calibration_guard.py — WP-G (29-Jul-2026). Two guards against SILENT calibration drift.

WHY THIS EXISTS
---------------
On 29-Jul-2026 FORWARD_AXIS_BUCKET_WEIGHTS changed from {margin .30, price .70} to thirds. The
monthly pre-run (01-Aug) consumes the SUMMARY tab of screener workbooks produced under the OLD
config, and the next screen was not until 07-Aug. Nothing in the framework could detect that. The
same change also moved the candidate pool 84 -> 46 because the 70 floor had been implicitly
calibrated against a score distribution that no longer existed (the old axis manufactured 174 names
at exactly 100.0; the new one produces 5). Both problems reached the investor rather than the
framework catching them.

GUARD 1 — CONFIG FINGERPRINT. Every screener output is stamped with a hash of the calibration
parameters that produced it. The pre-run compares that stamp to live config and refuses to treat a
mismatched workbook as current.

GUARD 2 — POOL DRIFT. The SUMMARY pool size per screener is recorded each run. A move beyond
POOL_DRIFT_TOLERANCE against the trailing median raises a warning naming the parameters that
changed, so a composition shift surfaces instead of hiding.

DOCTRINE: both guards WARN and annotate. Neither blocks a run or alters a score — an over-eager
guard that halts the monthly review is worse than the drift it detects.
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, os, sys

SCHEMA_VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
POOL_STORE_DEFAULT = "calibration_pool_history.json"

# The parameters that materially determine WHICH names reach SUMMARY. Adding one here is how a new
# calibration lever becomes drift-detectable — if it changes ranking, it belongs in this list.
FINGERPRINT_KEYS = [
    "SOURCE_WEIGHTS", "FORWARD_AXIS_BUCKET_WEIGHTS", "SUMMARY_SOURCE_FLOOR",
    "SUMMARY_STAGE_EXCLUDE", "SUMMARY_PART_B_FLOOR", "FORWARD_ELIG_PART_A_FLOOR",
    "PRICE_MOM_LOOKBACK", "PRICE_MOM_SKIP", "PRICE_MOM_SHORT_LOOKBACK", "PRICE_MOM_SHORT_SKIP",
    "PRICE_MOM_BLEND", "PRICE_MOM_SCORING", "PRICE_MOM_PCTL_CUTS", "PRICE_MOM_THRESHOLDS",
    "EPS_TREND_MOM_THRESHOLDS", "REV_EST_FWD_THRESHOLDS", "REVISION_RUNWAY_CAP",
    "MOMENTUM_STATE_GATES_SUMMARY", "DOOR_QUALITY_PART_A_MIN", "DOOR_QUALITY_FCF_YEARS_MIN",
    "DOOR_INFLECTION_PART_A_MIN", "DOOR_INFLECTION_OFF_HIGH_MIN_PCT", "REGIME_DOORS_ACTIVE",
]
POOL_DRIFT_TOLERANCE = 0.30      # +/- vs trailing median before a warning
POOL_DRIFT_MIN_HISTORY = 2       # runs of history needed before drift can be judged


def _canon(v):
    if isinstance(v, dict):
        return {str(k): _canon(v[k]) for k in sorted(v)}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, float):
        return round(v, 6)
    return v


def config_fingerprint(cfg=None):
    """{'hash': 'ab12cd34', 'params': {...}, 'schema_version': ...} for the live calibration."""
    if cfg is None:
        import scoring_config as cfg
    params = {}
    for k in FINGERPRINT_KEYS:
        if hasattr(cfg, k):
            params[k] = _canon(getattr(cfg, k))
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return {"schema_version": SCHEMA_VERSION,
            "hash": hashlib.sha256(blob.encode()).hexdigest()[:12],
            "params": params,
            "stamped_at": datetime.datetime.now().isoformat(timespec="seconds")}


def compare_fingerprint(stamped, live=None):
    """Verdict + the exact parameters that differ. Never raises."""
    live = live or config_fingerprint()
    if not stamped or not isinstance(stamped, dict) or not stamped.get("hash"):
        return {"verdict": "UNSTAMPED", "changed": [],
                "message": ("This output predates the calibration fingerprint (WP-G, 29-Jul-2026). "
                            "It cannot be proven current — treat its SUMMARY as UNVERIFIED and "
                            "restamp via restamp_screener_outputs.py before consuming it.")}
    if stamped.get("hash") == live["hash"]:
        return {"verdict": "MATCH", "changed": [], "message": "Calibration matches live config."}
    sp, lp = stamped.get("params") or {}, live["params"]
    changed = []
    for k in sorted(set(sp) | set(lp)):
        if sp.get(k) != lp.get(k):
            changed.append({"param": k, "in_file": sp.get(k), "live": lp.get(k)})
    return {"verdict": "STALE", "changed": changed,
            "message": ("Output was produced under a DIFFERENT calibration (%s vs live %s). %d "
                        "parameter(s) changed. Its SUMMARY ranking is NOT current — restamp before "
                        "the pre-run consumes it." % (stamped.get("hash"), live["hash"], len(changed)))}


STAMP_STORE_DEFAULT = "calibration_stamp_history.json"


def exclude_from_pool(run_date, group, *, reason, approved_by, store=STAMP_STORE_DEFAULT):
    """Exclude one screen from candidate-pool merging, with a reason and an owner.

    The alternative - deleting the artefacts - destroys captured history to solve a selection
    problem. An exclusion says "retained as evidence, inadmissible for selection", which is the
    same pattern as regime_history's `backfilled_not_pit`. An unattributed exclusion would be
    indistinguishable from a bug, so reason and approved_by are mandatory (R7.7).
    """
    import os
    if not reason or not approved_by:
        raise ValueError("a pool exclusion requires a reason and an owner (R7.7)")
    path = store if os.path.isabs(store) else os.path.join(HERE, store)
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    data.setdefault("_pool_exclusions", {})[f"{_norm_date(run_date)}|{group}"] = {
        "reason": reason, "approved_by": approved_by,
        "excluded_on": datetime.date.today().isoformat()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return data["_pool_exclusions"]


def pool_exclusions(store=STAMP_STORE_DEFAULT):
    import os
    path = store if os.path.isabs(store) else os.path.join(HERE, store)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("_pool_exclusions", {})


def contributors_in_window(year, month, store=STAMP_STORE_DEFAULT):
    """Every stamped screen whose run_date falls in the given calendar month.

    The pool window is a month, and a group is screened once per cycle, so the stamp store
    is the authority on what contributed - no filename parsing, and therefore no second home
    for the group vocabulary (R4.4).
    """
    import os
    path = store if os.path.isabs(store) else os.path.join(HERE, store)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    excl = data.get("_pool_exclusions", {})
    out = []
    for key, rec in data.items():
        if key.startswith("_") or not isinstance(rec, dict):
            continue
        if key in excl:
            continue
        rd = rec.get("run_date", "")
        if len(rd) >= 7 and rd[:4].isdigit():
            if int(rd[:4]) == int(year) and int(rd[5:7]) == int(month):
                out.append((rd, rec.get("group")))
    return sorted(out)


def record_calibration_stamp(run_date, group, fingerprint=None, store=STAMP_STORE_DEFAULT):
    """Record the calibration fingerprint IN FORCE when a screen was scored.

    D-14 (12-Aug-2026). update_watchlist merges the candidate pool by HIGHEST SCORE across
    every contributing workbook, not by newest - so a frame scored under a retired basis can
    outrank a correctly scored one and enter the pool on a number that today's config would
    never produce. That happened on 07-Aug-2026: the SP500 screen ran on the retired momentum
    bands because screener_local never called run_scheduled.

    Deleting that workbook fixes one month. This stamp plus assert_one_calibration_stamp()
    is what makes the NEXT basis change fail loudly instead of silently winning a merge.
    """
    import os
    fp_obj = fingerprint if isinstance(fingerprint, dict) else config_fingerprint()
    fp = fp_obj.get('hash') if isinstance(fp_obj, dict) else str(fp_obj)
    path = store if os.path.isabs(store) else os.path.join(HERE, store)
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    key = f"{_norm_date(run_date)}|{group}"
    prior = data.get(key)
    if prior and prior.get("fingerprint") != fp:
        # A screen's basis cannot change after the fact. Overwriting would erase the very
        # evidence this store exists to hold (R6.4: relabel, never rewrite).
        data.setdefault("_conflicts", []).append(
            {"key": key, "was": prior.get("fingerprint"), "now": fp,
             "observed_at": datetime.date.today().isoformat()})
    else:
        data[key] = {"fingerprint": fp, "group": group,
                     "schema_version": fp_obj.get("schema_version") if isinstance(fp_obj, dict) else None,
                     "run_date": _norm_date(run_date),
                     "stamped_at": datetime.date.today().isoformat()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return fp


def _norm_date(v):
    s = str(v or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def assert_one_calibration_stamp(contributors, store=STAMP_STORE_DEFAULT):
    """contributors = [(run_date, group), ...] feeding one candidate pool.

    Returns (ok, verdict). ok=False means the pool spans more than one calibration basis and
    MUST NOT be merged - scores from different bases are not comparable, and the merge keeps
    the highest.

    Unknown stamps are REPORTED, never assumed to agree (R4.1). A pool that is entirely
    unstamped is pre-stamp history and passes with a warning; a pool mixing a KNOWN stamp
    with an unknown one cannot be shown to be single-basis, so it fails. The check therefore
    hardens by itself as stamps accumulate, instead of needing a migration first.
    """
    import os
    path = store if os.path.isabs(store) else os.path.join(HERE, store)
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    excl = data.get("_pool_exclusions", {})
    known, unknown, excluded = {}, [], []
    for rd, grp in contributors:
        _k = f"{_norm_date(rd)}|{grp}"
        if _k in excl:
            excluded.append(f"{_norm_date(rd)} {grp} ({excl[_k]['reason'][:60]})")
            continue
        rec = data.get(_k)
        if rec:
            known.setdefault(rec["fingerprint"], []).append(f"{_norm_date(rd)} {grp}")
        else:
            unknown.append(f"{_norm_date(rd)} {grp}")
    if len(known) > 1:
        return False, {"status": "MIXED_CALIBRATION", "stamps": known, "unstamped": unknown, "excluded": excluded,
                       "message": ("The candidate pool spans %d calibration bases: %s. The merge "
                                   "keeps the HIGHEST score, so a frame scored under a retired "
                                   "basis can win. Re-run the stale group or exclude it."
                                   % (len(known), {k[:12]: v for k, v in known.items()}))}
    if known and unknown:
        return False, {"status": "PARTIALLY_UNSTAMPED", "stamps": known, "unstamped": unknown, "excluded": excluded,
                       "message": ("%d contributor(s) carry no calibration stamp and cannot be "
                                   "shown to share the basis of the %d that do: %s. Unknown is "
                                   "not agreement." % (len(unknown), sum(len(v) for v in known.values()), unknown))}
    if not known and unknown:
        return True, {"status": "ALL_UNSTAMPED_PRE_STAMP_HISTORY", "unstamped": unknown,
                      "excluded": excluded,
                      "message": "No contributor carries a stamp (pre-12-Aug-2026 history). "
                                 "Passing with a warning; this hardens as stamps accumulate."}
    return True, {"status": "SINGLE_BASIS", "stamps": known, "excluded": excluded}


def record_pool(group, run_date, pool_size, eligible_size=None, store=POOL_STORE_DEFAULT,
                fingerprint_hash=None):
    """Append one (group, run_date) pool observation. Idempotent on that key."""
    data = {"schema_version": SCHEMA_VERSION, "runs": []}
    if os.path.exists(store):
        try:
            data = json.load(open(store, encoding="utf-8")) or data
        except (ValueError, OSError):
            pass
    runs = [r for r in data.get("runs", [])
            if not (r.get("group") == group and r.get("run_date") == run_date)]
    runs.append({"group": group, "run_date": run_date, "pool_size": int(pool_size),
                 "eligible_size": (int(eligible_size) if eligible_size is not None else None),
                 "config_hash": fingerprint_hash})
    runs.sort(key=lambda r: (r.get("group") or "", r.get("run_date") or ""))
    data["runs"] = runs
    try:
        json.dump(data, open(store, "w", encoding="utf-8"), indent=1)
    except OSError:
        pass
    return len(runs)


def check_pool_drift(group, pool_size, store=POOL_STORE_DEFAULT, tolerance=None,
                     exclude_run_date=None):
    """Compare pool_size to the trailing median for this screener. WARN only, never blocks."""
    tol = POOL_DRIFT_TOLERANCE if tolerance is None else tolerance
    hist = []
    if os.path.exists(store):
        try:
            data = json.load(open(store, encoding="utf-8")) or {}
            hist = [r["pool_size"] for r in data.get("runs", [])
                    if r.get("group") == group and r.get("pool_size") is not None
                    and r.get("run_date") != exclude_run_date]
        except (ValueError, OSError, KeyError):
            hist = []
    if len(hist) < POOL_DRIFT_MIN_HISTORY:
        return {"verdict": "INSUFFICIENT_HISTORY", "n_prior": len(hist), "median": None,
                "pool_size": pool_size,
                "message": "Fewer than %d prior runs for %s — drift not assessable yet."
                           % (POOL_DRIFT_MIN_HISTORY, group)}
    s = sorted(hist)
    med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0
    if med <= 0:
        return {"verdict": "INSUFFICIENT_HISTORY", "n_prior": len(hist), "median": med,
                "pool_size": pool_size, "message": "Prior median is zero — not assessable."}
    delta = (pool_size - med) / med
    verdict = "OK" if abs(delta) <= tol else ("POOL_EXPANDED" if delta > 0 else "POOL_CONTRACTED")
    msg = ("%s SUMMARY pool %d vs trailing median %.1f (%+.0f%%)." % (group, pool_size, med, delta * 100))
    if verdict != "OK":
        msg += (" EXCEEDS the +/-%.0f%% tolerance. A pool move this size is normally a CALIBRATION "
                "change, not a change in the market — check the config fingerprint diff before "
                "treating this candidate set as comparable to prior months." % (tol * 100))
    return {"verdict": verdict, "n_prior": len(hist), "median": med, "pool_size": pool_size,
            "delta_pct": round(delta * 100, 1), "message": msg}


def preflight(group, pool_size, stamped_fingerprint=None, store=POOL_STORE_DEFAULT):
    """One call for the pre-run: fingerprint verdict + pool-drift verdict + a single headline."""
    fp = compare_fingerprint(stamped_fingerprint)
    pd_ = check_pool_drift(group, pool_size, store=store)
    blocking = fp["verdict"] in ("STALE", "UNSTAMPED") or pd_["verdict"] in ("POOL_EXPANDED", "POOL_CONTRACTED")
    return {"group": group, "fingerprint": fp, "pool_drift": pd_,
            "attention_required": blocking,
            "headline": ("CALIBRATION ATTENTION — " + fp["message"] + " " + pd_["message"])
                        if blocking else "Calibration and pool size consistent with prior runs."}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true", help="print the live fingerprint and exit")
    ap.add_argument("--group"); ap.add_argument("--run-date", dest="run_date")
    ap.add_argument("--pool-size", dest="pool_size", type=int)
    ap.add_argument("--stamped", help="JSON file holding a previously stamped fingerprint")
    ap.add_argument("--store", default=POOL_STORE_DEFAULT)
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if a.emit:
        print(json.dumps(config_fingerprint(), indent=1)); return 0
    stamped = None
    if a.stamped and os.path.exists(a.stamped):
        try:
            blob = json.load(open(a.stamped, encoding="utf-8"))
            stamped = blob.get("calibration_fingerprint", blob)
        except (ValueError, OSError):
            stamped = None
    if a.group is None or a.pool_size is None:
        print("need --group and --pool-size (or --emit)"); return 2
    res = preflight(a.group, a.pool_size, stamped, store=a.store)
    print(json.dumps(res, indent=1))
    if a.record and a.run_date:
        record_pool(a.group, a.run_date, a.pool_size, store=a.store,
                    fingerprint_hash=config_fingerprint()["hash"])
    return 1 if res["attention_required"] else 0


if __name__ == "__main__":
    sys.exit(main())
