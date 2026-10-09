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
⚑ ISA-0828 (R18.6, 04-Oct-2026): acceptance is PATH-LEVEL. Each record carries the declared path id and a
PATH FINGERPRINT over exactly what the path executes (the static import closure of its declared entry
modules, every signed config file, its run surfaces, the capability rows its modules produce). `current()`
is CURRENT iff LIVE is TRUSTED AND that fingerprint is unchanged: an INTERSECTING Trusted change invalidates
it (naming the changed members); a NON-intersecting change (a screener-only module, a test, a VCI-only
store) does not. Before 04-Oct any new build id invalidated acceptance (blanket) and new_capital_control
never re-read it, so new stock capital stayed OPEN on a stale acceptance. Legacy records (no fingerprint)
keep the old build-id rule. The control `new_capital_control --open --acceptance <sa_id>` cites it.
Never pre-written: the id is a content hash of the executed evidence (s12.4 'never pre-write a fictitious
delivered-LIVE acceptance receipt').
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_REL = os.path.join("Dashboard", "state", "system_acceptance.jsonl")
DEFAULT_PATH = "monthly_capital"
REGISTRY_REL = os.path.join("Dashboard", "state", "capability_registry.json")
SUITES = [("capital_decision_engine", ["capital_decision_engine.py", "--selftest"], "capital_decision_engine selftest: 0 FAIL"),
          ("capital_destination", ["capital_destination.py", "--selftest"], "0 failure(s)"),
          ("checkpoint_d", ["checkpoint_d.py", "--selftest"], "checkpoint_d selftest OK"),
          ("prerun_runner", ["prerun_runner.py", "--selftest"], "prerun_runner selftest: 0 FAIL"),
          ("isa_source_cache", ["isa_source_cache.py", "--selftest"], "isa_source_cache selftest: 0 FAIL"),
          ("campaign", ["tests_jul2026/test_capital_engine_campaign.py"], "test_capital_engine_campaign: 0 failure(s)")]


def _store(root):
    return os.path.join(root, STORE_REL)


# ─────────────────────────────────────────────────────────────────────────────────────────
# ISA-0828 / R18.6 — the PATH and its fingerprint
# ─────────────────────────────────────────────────────────────────────────────────────────

def path_declaration(root: str = HERE, path_id: str = DEFAULT_PATH) -> dict:
    """The declared path (capability_registry.json `paths`) - signed with the registry. Absent = refuse."""
    try:
        doc = json.load(open(os.path.join(root, REGISTRY_REL), encoding="utf-8"))
    except Exception as exc:                                            # noqa: BLE001
        raise RuntimeError("capability registry unreadable (%s) - path %s UNKNOWN" % (exc, path_id))
    p = (doc.get("paths") or {}).get(path_id)
    if not isinstance(p, dict) or not p.get("entry_modules"):
        raise RuntimeError("R18.6: path %r is not declared in capability_registry.json `paths`" % path_id)
    return dict(p, _capabilities=doc.get("capabilities") or [])


def _imports_of(path: str) -> set:
    import ast
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception:                                                   # noqa: BLE001
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and ".py" in n.value:
            # a script the path runs as a SUBPROCESS (run_script("extract_xray.py") etc.) executes on
            # the path as surely as an import does. Over-inclusion only widens what intersects (safe).
            _m = re.fullmatch(r"(?:.*[/\\])?([A-Za-z_][A-Za-z0-9_]*)\.py", n.value.strip())
            if _m:
                out.add(_m.group(1))
    return out


def import_closure(root: str, entry_modules) -> list:
    """Static closure of framework modules reachable by import (module-level AND lazy imports) from the
    entry modules. A module absent from disk is reported, never silently dropped."""
    avail = {}
    for fn in os.listdir(root):
        if fn.endswith(".py"):
            avail[fn[:-3]] = os.path.join(root, fn)
    seen, todo = set(), [m for m in entry_modules]
    while todo:
        m = todo.pop()
        if m in seen or m not in avail:
            continue
        seen.add(m)
        todo.extend(sorted(_imports_of(avail[m]) - seen))
    return sorted(seen)


def path_fingerprint(root: str = HERE, path_id: str = DEFAULT_PATH, fps: dict = None) -> dict:
    """-> {"path_id", "fingerprint", "members": {rel: digest}, "n": {...}, "missing_entry_modules": [...]}"""
    sys.path.insert(0, root)
    import release_gate as rg
    decl = path_declaration(root, path_id)
    fps = fps if fps is not None else rg.live_fingerprints(root)
    mods = import_closure(root, decl["entry_modules"])
    src = (fps.get("source") or {}).get("files") or {}
    members = {}
    for m in mods:
        rel = m + ".py"
        members["src:" + rel] = src.get(rel) or src.get(os.path.join(".", rel)) or "ABSENT_FROM_SOURCE_ROLL"
    for rel, dg in sorted(((fps.get("config") or {}).get("files") or {}).items()):
        members["cfg:" + rel.replace(os.sep, "/")] = dg
    rs = (fps.get("run_surfaces") or {}).get("digests") or {}
    for lab in decl.get("run_surfaces") or []:
        members["surface:" + lab] = rs.get(lab) or "UNAVAILABLE"
    modset = set(mods)
    for cap in decl["_capabilities"]:
        prod = str(cap.get("producer") or "")
        if prod.split(".", 1)[0] in modset:
            members["cap:" + cap["id"]] = hashlib.sha256(json.dumps(cap, sort_keys=True).encode()).hexdigest()
    roll = hashlib.sha256(json.dumps(sorted(members.items())).encode()).hexdigest()
    return {"path_id": path_id, "fingerprint": roll, "members": members,
            "n": {"modules": len(mods), "config": sum(1 for k in members if k.startswith("cfg:")),
                  "run_surfaces": sum(1 for k in members if k.startswith("surface:")),
                  "capabilities": sum(1 for k in members if k.startswith("cap:"))},
            "missing_entry_modules": [m for m in decl["entry_modules"] if m not in modset]}


def changed_members(a: dict, b: dict) -> list:
    return sorted(k for k in set(a or {}) | set(b or {}) if (a or {}).get(k) != (b or {}).get(k))


def records(root: str = HERE) -> list:
    p = _store(root)
    if not os.path.exists(p):
        return []
    return [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]


def current(root: str = HERE, path_id: str = DEFAULT_PATH, _fp: dict = None) -> dict:
    """R18.6 - the latest ACCEPTED record for `path_id`, CURRENT iff LIVE is TRUSTED and the path
    fingerprint it accepted is the path fingerprint now. Else a typed INVALIDATED/ABSENT naming why."""
    sys.path.insert(0, root)
    import release_gate as rg
    v = rg.verify_live(root)
    recs = [r for r in records(root) if r.get("state") == "ACCEPTED" and (r.get("path_id") or DEFAULT_PATH) == path_id]
    if not recs:
        return {"state": "ABSENT", "path_id": path_id, "live_build": v.get("build_id")}
    last = recs[-1]
    base = {"sa_id": last.get("sa_id"), "path_id": path_id, "accepted_build": last.get("build_id"),
            "live_build": v.get("build_id"), "live_state": v.get("state")}
    if v.get("state") != "TRUSTED":
        return dict(base, state="INVALIDATED", why="LIVE is %s - no acceptance survives an untrusted LIVE (R18.5)" % v.get("state"))
    if not last.get("path_fingerprint"):
        if last.get("build_id") != v.get("build_id"):
            return dict(base, state="INVALIDATED", basis="LEGACY_BUILD_ID",
                        why="legacy acceptance (no path fingerprint) is bound to its build id; a new build invalidates it (s12.4, T84)")
        return dict(base, state="CURRENT", basis="LEGACY_BUILD_ID", build_id=last["build_id"], at=last.get("at"))
    try:
        fp = _fp if _fp is not None else path_fingerprint(root, path_id)
    except Exception as exc:                                            # noqa: BLE001
        return dict(base, state="INVALIDATED", why="R18.6: path fingerprint not computable (%s) - UNKNOWN never CURRENT" % exc)
    if fp["fingerprint"] != last["path_fingerprint"]:
        ch = changed_members(last.get("path_members"), fp["members"])
        return dict(base, state="INVALIDATED", basis="PATH_FINGERPRINT", changed_members=ch[:40], n_changed=len(ch),
                    why="R18.6: an INTERSECTING change altered %d member(s) of path %s since acceptance (%s)"
                        % (len(ch), path_id, ", ".join(ch[:6])))
    return dict(base, state="CURRENT", basis="PATH_FINGERPRINT", build_id=v.get("build_id"), at=last.get("at"),
                path_fingerprint=fp["fingerprint"],
                why=("accepted on %s; LIVE %s leaves path %s byte-identical" % (last.get("build_id"), v.get("build_id"), path_id)))


def record(sa_id: str, root: str = HERE) -> dict:
    for r in records(root):
        if r.get("sa_id") == sa_id:
            return r
    return {}


def acceptance_currency(ref, root: str = HERE, path_id: str = DEFAULT_PATH) -> dict:
    """ISA-0828: is the acceptance an OPEN new-capital control cites still CURRENT for its path?
    CURRENT | STALE | ROLLBACK (a governed rollback reference) | UNGOVERNED_ROOT (a fixture book with no
    Trusted receipt - capital authority refuses such a tree upstream: NO_RECEIPT)."""
    ref = str(ref or "").strip()
    if ref.startswith("ROLLBACK:"):
        return {"state": "ROLLBACK", "ref": ref, "why": "a governed rollback reference (R4.13)"}
    sys.path.insert(0, root)
    import release_gate as rg
    if rg.load_receipt(root) is None:
        return {"state": "UNGOVERNED_ROOT", "ref": ref,
                "why": "no Trusted receipt in this root (fixture book); capital_run_authority REFUSES NO_RECEIPT upstream"}
    cur = current(root, path_id)
    if cur.get("state") != "CURRENT":
        return {"state": "STALE", "ref": ref, "current": cur,
                "why": "R18.6: the path acceptance is %s - %s" % (cur.get("state"), cur.get("why"))}
    if cur.get("sa_id") == ref:
        return {"state": "CURRENT", "ref": ref, "current": cur, "why": cur.get("why")}
    cited = record(ref, root)
    if cited and cited.get("path_fingerprint") and cited.get("path_fingerprint") == cur.get("path_fingerprint"):
        return {"state": "CURRENT", "ref": ref, "current": cur, "why": "the cited acceptance accepted the identical path"}
    return {"state": "STALE", "ref": ref, "current": cur,
            "why": "R18.6: the control cites %s but the CURRENT acceptance of path %s is %s (renewed after an intersecting change): re-open citing it"
                   % (ref, path_id, cur.get("sa_id"))}


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
    try:
        _pf = path_fingerprint(root, DEFAULT_PATH)
    except Exception as _exc:                                           # noqa: BLE001
        _pf = None
        checks.append({"check": "path_fingerprint", "pass": False, "observed": str(_exc)})
    all_pass = all(c["pass"] for c in checks)
    body = {"month": month, "build_id": v.get("build_id"), "checks": checks,
            "state": "ACCEPTED" if all_pass else "REJECTED", "path_id": DEFAULT_PATH,
            "path_fingerprint": (_pf or {}).get("fingerprint"), "path_members": (_pf or {}).get("members"),
            "path_n": (_pf or {}).get("n")}
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
    try:
        _pf = path_fingerprint(root, DEFAULT_PATH)
    except Exception as _exc:                                           # noqa: BLE001
        _pf = None
        checks.append({"check": "path_fingerprint", "pass": False, "observed": str(_exc)})
    all_pass = all(c["pass"] for c in checks)
    body = {"month": month, "build_id": v.get("build_id"), "live_rolls": v.get("live_rolls"),
            "checks": checks, "state": "ACCEPTED" if all_pass else "REJECTED", "secs": round(time.time() - t0, 1),
            "path_id": DEFAULT_PATH, "path_fingerprint": (_pf or {}).get("fingerprint"),
            "path_members": (_pf or {}).get("members"), "path_n": (_pf or {}).get("n")}
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
    # ── ISA-0828 / R18.6: PATH-LEVEL acceptance ────────────────────────────────────────────────
    t = tempfile.mkdtemp(prefix="sa_path_")
    os.makedirs(os.path.join(t, "Dashboard", "state"))
    for nm, body in (("entry.py", "import dep\ndef f():\n    import lazydep\n    return 1\n"),
                     ("dep.py", "X = 1\n"), ("lazydep.py", "Y = 1\n"), ("unrelated.py", "Z = 1\n")):
        open(os.path.join(t, nm), "w").write(body)
    json.dump({"capabilities": [], "paths": {DEFAULT_PATH: {"entry_modules": ["entry"], "run_surfaces": []}}},
              open(os.path.join(t, REGISTRY_REL), "w"))
    ok("R18.6 closure: entry + module-level + LAZY imports, and nothing unrelated",
       import_closure(t, ["entry"]) == ["dep", "entry", "lazydep"])
    fp0 = path_fingerprint(t)
    with open(_store(t), "w") as fh:
        fh.write(json.dumps({"state": "ACCEPTED", "sa_id": "SA-P1", "build_id": "TB-A", "at": "x", "path_id": DEFAULT_PATH,
                             "path_fingerprint": fp0["fingerprint"], "path_members": fp0["members"]}) + "\n")
    _orig = rg.verify_live
    try:
        rg.verify_live = lambda root=None, receipt=None: {"state": "TRUSTED", "build_id": "TB-A"}
        ok("R18.6 NEGATIVE CONTROL: the accepted tree is CURRENT", current(t)["state"] == "CURRENT")
        open(os.path.join(t, "unrelated.py"), "a").write("W = 2\n")
        rg.verify_live = lambda root=None, receipt=None: {"state": "TRUSTED", "build_id": "TB-B"}
        _c = current(t)
        ok("R18.6 MUST-FIRE (handoff case 17): a NON-intersecting Trusted change does NOT invalidate the path",
           _c["state"] == "CURRENT" and _c["basis"] == "PATH_FINGERPRINT")
        open(os.path.join(t, "lazydep.py"), "a").write("Y = 2\n")
        _c = current(t)
        ok("R18.6 MUST-FIRE (handoff case 16): an INTERSECTING change (a lazily imported dependency) invalidates "
           "it and names the member", _c["state"] == "INVALIDATED" and "src:lazydep.py" in _c.get("changed_members", []))
        open(os.path.join(t, "trusted_build_marker"), "w").write("x")
        json.dump({"build_id": "TB-B"}, open(os.path.join(t, "Dashboard", "state", "trusted_build.json"), "w"))
        ok("ISA-0828 MUST-FIRE: a control citing the pre-change acceptance is STALE",
           acceptance_currency("SA-P1", t)["state"] == "STALE")
        fp1 = path_fingerprint(t)
        with open(_store(t), "a") as fh:
            fh.write(json.dumps({"state": "ACCEPTED", "sa_id": "SA-P2", "build_id": "TB-B", "at": "y", "path_id": DEFAULT_PATH,
                                 "path_fingerprint": fp1["fingerprint"], "path_members": fp1["members"]}) + "\n")
        ok("ISA-0828 (handoff case 18): a renewal on the delivered tree is CURRENT and the control citing it is CURRENT",
           current(t)["state"] == "CURRENT" and acceptance_currency("SA-P2", t)["state"] == "CURRENT")
        ok("ISA-0828: after renewal the OLD citation is still STALE (re-open must cite the renewal)",
           acceptance_currency("SA-P1", t)["state"] == "STALE")
        ok("ISA-0828: a governed ROLLBACK reference is recognised, not treated as an acceptance",
           acceptance_currency("ROLLBACK: drill", t)["state"] == "ROLLBACK")
        rg.verify_live = lambda root=None, receipt=None: {"state": "UNTRUSTED_LIVE_STATE", "build_id": "TB-B"}
        ok("R18.6 MUST-FIRE: an untrusted LIVE invalidates even a fingerprint-identical acceptance",
           current(t)["state"] == "INVALIDATED")
    finally:
        rg.verify_live = _orig
    ok("ISA-0828: a root with NO Trusted receipt is UNGOVERNED_ROOT (fixture book), never CURRENT",
       acceptance_currency("SA-X", e)["state"] == "UNGOVERNED_ROOT")
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
