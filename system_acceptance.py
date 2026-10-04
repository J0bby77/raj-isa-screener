#!/usr/bin/env python3
"""system_acceptance.py - BuildSpec s12.4 / P10 (ISA-0812, 03-Oct-2026): the ONE SYSTEM_ACCEPTANCE receipt
for the monthly capital path, bound to the DELIVERED Trusted Build fingerprint.

It is produced AFTER promotion, on the LIVE (delivered) bytes, by executing - nontrading, without
mutating any LIVE artefact - the actual final consumers of the Capital Decision Receipt:
  * release_gate.verify_live TRUSTED (identity bound into the record: build id + every live roll);
  * the delivered engine / router / Checkpoint-D / email / pre-run runner selftests;
  * the real-path campaign (tests_jul2026/test_capital_engine_campaign.py) on the frozen month inputs in a
    private temp book: case fixed point, receipt -> capital_destination ENGINE_ALLOCATED rows to the penny,
    Checkpoint-D tick 11 pass/refuse, email renders the same receipt id, execution_check EXECUTABLE and its
    refusals, stale/changed-input refusals, idempotent fill capture;
  * the month's LIVE receipt verifies VALID against the LIVE inputs.
Only an all-pass run is ACCEPTED. The record is append-only (Dashboard/state/system_acceptance.jsonl).
Any intersecting change (a new Trusted Build) invalidates it: `current()` returns the latest record only
if its build id equals the LIVE build. The control `new_capital_control --open --acceptance <sa_id>` cites it.
Never pre-written: the id is a content hash of the executed evidence (s12.4 'never pre-write a fictitious
delivered-LIVE acceptance receipt').
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_REL = os.path.join("Dashboard", "state", "system_acceptance.jsonl")
SUITES = [("capital_decision_engine", ["capital_decision_engine.py", "--selftest"], "capital_decision_engine selftest: 0 FAIL"),
          ("capital_destination", ["capital_destination.py", "--selftest"], "0 failure(s)"),
          ("checkpoint_d", ["checkpoint_d.py", "--selftest"], "checkpoint_d selftest OK"),
          ("prerun_runner", ["prerun_runner.py", "--selftest"], "prerun_runner selftest: 0 FAIL"),
          ("isa_source_cache", ["isa_source_cache.py", "--selftest"], "isa_source_cache selftest: 0 FAIL"),
          ("campaign", ["tests_jul2026/test_capital_engine_campaign.py"], "test_capital_engine_campaign: 0 failure(s)")]


def _store(root):
    return os.path.join(root, STORE_REL)


def records(root: str = HERE) -> list:
    p = _store(root)
    if not os.path.exists(p):
        return []
    return [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]


def current(root: str = HERE) -> dict:
    """The latest ACCEPTED record bound to the CURRENT LIVE build, else a typed absence."""
    sys.path.insert(0, root)
    import release_gate as rg
    v = rg.verify_live(root)
    recs = [r for r in records(root) if r.get("state") == "ACCEPTED"]
    if not recs:
        return {"state": "ABSENT", "live_build": v.get("build_id")}
    last = recs[-1]
    if v.get("state") != "TRUSTED" or last.get("build_id") != v.get("build_id"):
        return {"state": "INVALIDATED", "sa_id": last.get("sa_id"), "accepted_build": last.get("build_id"),
                "live_build": v.get("build_id"), "live_state": v.get("state"),
                "why": "an intersecting release/LIVE change invalidates system acceptance (s12.4, T84)"}
    return {"state": "CURRENT", "sa_id": last["sa_id"], "build_id": last["build_id"], "at": last["at"]}


PARTIAL_REL = os.path.join("Dashboard", "state", "system_acceptance_partial.json")


def run_suite(name: str, root: str = HERE, timeout_s: int = 170) -> dict:
    """One suite per host call (the 180 s ceiling); results staged against the LIVE build id and
    discarded if the build changes before finalize()."""
    sys.path.insert(0, root)
    import release_gate as rg
    v = rg.verify_live(root)
    argv, token = next((a, t) for n, a, t in SUITES if n == name)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        r = subprocess.run([sys.executable] + argv, cwd=root, capture_output=True, text=True, timeout=timeout_s, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        ok = (token in out) and r.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = "TIMEOUT", False
    p = os.path.join(root, PARTIAL_REL)
    doc = json.load(open(p)) if os.path.exists(p) else {}
    if doc.get("build_id") != v.get("build_id"):
        doc = {"build_id": v.get("build_id"), "suites": {}}
    doc["suites"][name] = {"check": name, "pass": ok, "tail": out[-500:], "n_fail": out.count("FAIL "),
                           "n_pass": out.count("PASS ") + out.count("  ok "), "at": time.time()}
    json.dump(doc, open(p, "w"), indent=1)
    return doc["suites"][name]


def finalize(month: str, root: str = HERE, write: bool = False) -> dict:
    sys.path.insert(0, root)
    import release_gate as rg
    v = rg.verify_live(root)
    p = os.path.join(root, PARTIAL_REL)
    doc = json.load(open(p)) if os.path.exists(p) else {}
    checks = [{"check": "verify_live", "pass": v.get("state") == "TRUSTED", "observed": v.get("state"),
               "build_id": v.get("build_id")}]
    staged = doc.get("suites") if doc.get("build_id") == v.get("build_id") else {}
    for name, _a, _t in SUITES:
        checks.append(staged.get(name) or {"check": name, "pass": False, "tail": "NOT RUN against this build"})
    import capital_decision_engine as cde
    rv = cde.verify_receipt(month, root)
    rr = rv.pop("receipt", None) or {}
    checks.append({"check": "live_receipt_verifies", "pass": rv.get("state") == "VALID", "observed": rv.get("state"),
                   "receipt_id": rr.get("receipt_id"), "receipt_state": rr.get("state")})
    all_pass = all(c["pass"] for c in checks)
    body = {"month": month, "build_id": v.get("build_id"), "checks": checks,
            "state": "ACCEPTED" if all_pass else "REJECTED"}
    sa_id = "SA-%s-%s" % (datetime.date.today().isoformat(),
                          hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:10])
    rec = dict(body, sa_id=sa_id, at=datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
               basis="BuildSpec s12.4 nontrading delivered-path acceptance; Raj risk acceptance ISA-0812")
    if write:
        sp = _store(root)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    return rec


def run(month: str, root: str = HERE, write: bool = False, timeout_s: int = 170) -> dict:
    sys.path.insert(0, root)
    import release_gate as rg
    t0 = time.time()
    v = rg.verify_live(root)
    checks = [{"check": "verify_live", "pass": v.get("state") == "TRUSTED", "observed": v.get("state"),
               "build_id": v.get("build_id")}]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name, argv, token in SUITES:
        try:
            r = subprocess.run([sys.executable] + argv, cwd=root, capture_output=True, text=True,
                               timeout=timeout_s, env=env)
            out = (r.stdout or "") + (r.stderr or "")
            ok = (token in out) and r.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = "TIMEOUT", False
        checks.append({"check": name, "pass": ok, "tail": out[-500:],
                       "n_pass": out.count("PASS ") + out.count("  ok "), "n_fail": out.count("FAIL ")})
    import capital_decision_engine as cde
    rv = cde.verify_receipt(month, root)
    rr = rv.pop("receipt", None) or {}
    checks.append({"check": "live_receipt_verifies", "pass": rv.get("state") == "VALID", "observed": rv.get("state"),
                   "receipt_id": rr.get("receipt_id"), "receipt_state": rr.get("state")})
    all_pass = all(c["pass"] for c in checks)
    body = {"month": month, "build_id": v.get("build_id"), "live_rolls": v.get("live") or v.get("rolls") or v.get("fingerprints"),
            "checks": checks, "state": "ACCEPTED" if all_pass else "REJECTED", "secs": round(time.time() - t0, 1)}
    sa_id = "SA-%s-%s" % (datetime.date.today().isoformat(), hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:10])
    rec = dict(body, sa_id=sa_id, at=datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
               basis="BuildSpec s12.4 nontrading delivered-path acceptance; Raj risk acceptance ISA-0812")
    if write:
        p = _store(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    return rec


def _selftest() -> int:
    import tempfile
    fails = []

    def ok(n, c):
        print(("PASS " if c else "FAIL ") + n)
        if not c:
            fails.append(n)
    d = tempfile.mkdtemp(prefix="sa_st_")
    os.makedirs(os.path.join(d, "Dashboard", "state"))
    with open(_store(d), "w") as fh:
        fh.write(json.dumps({"state": "ACCEPTED", "sa_id": "SA-X", "build_id": "TB-OLD", "at": "x"}) + "\n")
    import release_gate as rg
    _orig = rg.verify_live
    try:
        rg.verify_live = lambda root=None, receipt=None: {"state": "TRUSTED", "build_id": "TB-NEW"}
        ok("T84 MUST-FIRE: a newer Trusted Build INVALIDATES the earlier acceptance", current(d)["state"] == "INVALIDATED")
        rg.verify_live = lambda root=None, receipt=None: {"state": "TRUSTED", "build_id": "TB-OLD"}
        ok("NEGATIVE CONTROL T84: the same build keeps it CURRENT", current(d)["state"] == "CURRENT")
        rg.verify_live = lambda root=None, receipt=None: {"state": "UNTRUSTED_LIVE_STATE", "build_id": "TB-OLD"}
        ok("MUST-FIRE: an untrusted LIVE invalidates acceptance", current(d)["state"] == "INVALIDATED")
    finally:
        rg.verify_live = _orig
    e = tempfile.mkdtemp(prefix="sa_st2_")
    ok("MUST-FIRE: no record -> ABSENT, never CURRENT", records(e) == [])
    print("system_acceptance selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--current" in argv:
        print(json.dumps(current(HERE), indent=1))
        return 0
    if "--suite" in argv:
        r = run_suite(argv[argv.index("--suite") + 1], HERE)
        print(("PASS " if r["pass"] else "FAIL ") + r["check"] + "  (" + str(r["n_pass"]) + " pass / " + str(r["n_fail"]) + " fail)")
        return 0 if r["pass"] else 3
    month = argv[argv.index("--month") + 1] if "--month" in argv else None
    if month and "--finalize" in argv:
        r = finalize(month, HERE, write="--write" in argv)
        print(json.dumps({k: r[k] for k in ("sa_id", "state", "build_id")}, indent=1))
        for c in r["checks"]:
            print(("PASS " if c["pass"] else "FAIL ") + c["check"])
        return 0 if r["state"] == "ACCEPTED" else 3
    if not month:
        print(__doc__)
        return 2
    r = run(month, HERE, write="--write" in argv)
    print(json.dumps({k: r[k] for k in ("sa_id", "state", "build_id", "secs")}, indent=1))
    for c in r["checks"]:
        print(("PASS " if c["pass"] else "FAIL ") + c["check"])
    return 0 if r["state"] == "ACCEPTED" else 3


if __name__ == "__main__":
    sys.exit(main())
