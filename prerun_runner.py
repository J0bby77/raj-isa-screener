#!/usr/bin/env python3
"""prerun_runner.py - ISA-0801 (BuildSpec P1, 03-Oct-2026): the CHECKPOINTED CONTINUATION that runs the
monthly pre-run inside the host's per-call ceiling.

WHY. A single monthly_isa_prerun.py pass on the OneDrive (Plan9/virtiofs) mount needs > 300 s because
every file open/stat costs 2-8 ms and a pass touches the whole codebase several times; the device shell
allows <= 180 s per call. The same pass on a local workspace takes ~105 s (measured 03-Oct 13:5xZ and
18:11Z). So the pass runs on a byte-exact LOCAL WORKSPACE copy and its outputs are committed back to
LIVE under verification. Nothing is cached across runs: every assurance check still runs, every run,
on the actual bytes (no static-receipt shortcut that could conceal a stale input - BuildSpec s12.1/T71/T72).

STAGES (each one call, each < 180 s, each idempotent and resumable):
  --stage sync    lock the occurrence; rsync LIVE -> workspace; snapshot (path -> size, mtime_ns, sha256)
  --stage run     run monthly_isa_prerun.py in the workspace (re-run unchanged until the completion
                  token appears; the scheduled task's own re-run protocol, ISA-0780)
  --stage fetch   (optional) the Step 2 metrics fetch, in the workspace
  --stage commit  copy every file the run CREATED or CHANGED back to LIVE: refuses if the same LIVE file
                  changed since the sync (conflict), backs up every overwritten LIVE file first, rewrites
                  the workspace root to the LIVE root in text outputs, replaces atomically, then VERIFIES
                  on LIVE (release_gate.verify_live TRUSTED on the build the run used; the Capital Decision
                  Receipt verifies VALID). Any verification failure ROLLS BACK every committed file.
  --status        the occurrence's stage ledger
Ledger: Dashboard/state/prerun_stage_ledger.jsonl (APPEND_ONLY_HISTORY); lock: Dashboard/state/prerun_lock.json.
Failure scopes: SYNC/RUN failure = BLOCK_RUN (LIVE untouched); COMMIT verification failure = BLOCK_RUN +
automatic rollback; a cut-off RUN = INCOMPLETE (re-run the stage).

ROLLBACK (R4.13): run monthly_isa_prerun.py directly (the pre-ISA-0801 path) - unchanged by this module.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
METHOD_VERSION = "PRR-1.0"
LOCK_REL = os.path.join("Dashboard", "state", "prerun_lock.json")
LEDGER_REL = os.path.join("Dashboard", "state", "prerun_stage_ledger.jsonl")
LOCK_STALE_S = 3 * 3600
EXCLUDES = ("_bak*", "__pycache__", "_capital_engine_*", "ChatGPT Astra 6 Audit", "_bak_prerun_commit",
            "_candidate_evidence")
TEXT_SUFFIXES = (".json", ".jsonl", ".md", ".csv", ".txt", ".html", ".log")
COMPLETION_TOKEN = "Review task reads:"
ERROR_TOKEN = "ERRORS -- review task will be blocked"


class RunnerRefused(Exception):
    pass


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def isa_root_of(inv_dir: str) -> str:
    return os.path.dirname(os.path.abspath(inv_dir))


def workspace_for(occurrence: str, base: Optional[str] = None) -> str:
    return os.path.join(base or os.environ.get("ISA_PRERUN_WORKSPACE", "/tmp/isa_prerun_ws"), occurrence)


def default_occurrence(today: Optional[datetime.date] = None) -> str:
    d = today or datetime.date.today()
    return "prerun-%s" % d.isoformat()


def _ledger(live_inv: str, row: dict) -> None:
    p = os.path.join(live_inv, LEDGER_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = dict(row, method_version=METHOD_VERSION, at=_now())
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def ledger_rows(live_inv: str, occurrence: Optional[str] = None) -> List[dict]:
    p = os.path.join(live_inv, LEDGER_REL)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                r = json.loads(ln)
                if occurrence is None or r.get("occurrence") == occurrence:
                    out.append(r)
    return out


def last_state(live_inv: str, occurrence: str, stage: str) -> Optional[dict]:
    rows = [r for r in ledger_rows(live_inv, occurrence) if r.get("stage") == stage]
    return rows[-1] if rows else None


# ── lock (T76: concurrent invocation / repeated occurrence) ──────────────────────────────────────
def acquire_lock(live_inv: str, occurrence: str, holder: str) -> dict:
    p = os.path.join(live_inv, LOCK_REL)
    if os.path.exists(p):
        try:
            cur = json.load(open(p, encoding="utf-8"))
        except Exception:                                               # noqa: BLE001
            cur = {}
        age = time.time() - float(cur.get("acquired_epoch") or 0)
        if cur.get("occurrence") and cur.get("occurrence") != occurrence and age < LOCK_STALE_S \
                and not cur.get("released"):
            raise RunnerRefused("LOCKED: occurrence %s holds the pre-run lock (%.0f s old) - a concurrent run "
                                "is refused (T76)" % (cur.get("occurrence"), age))
        if cur.get("occurrence") == occurrence and not cur.get("released") and cur.get("holder") != holder \
                and age < LOCK_STALE_S:
            raise RunnerRefused("LOCKED: occurrence %s is held by %s - a second invoker is refused (T76)"
                                % (occurrence, cur.get("holder")))
    rec = {"occurrence": occurrence, "holder": holder, "acquired_at": _now(), "acquired_epoch": time.time(),
           "released": False}
    tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(rec, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, p)
    return rec


def release_lock(live_inv: str, occurrence: str) -> None:
    p = os.path.join(live_inv, LOCK_REL)
    try:
        cur = json.load(open(p, encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        return
    if cur.get("occurrence") == occurrence:
        cur.update(released=True, released_at=_now())
        tmp = p + ".tmp"
        json.dump(cur, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, p)


# ── snapshot ────────────────────────────────────────────────────────────────────────────────────
def _excluded(rel: str) -> bool:
    import fnmatch
    parts = rel.split(os.sep)
    return any(fnmatch.fnmatch(p, pat) for p in parts for pat in EXCLUDES)


def snapshot(root: str, with_sha: bool = True) -> Dict[str, list]:
    out = {}
    for dp, dns, fns in os.walk(root):
        rel_dp = os.path.relpath(dp, root)
        dns[:] = [d for d in dns if not _excluded(os.path.normpath(os.path.join(rel_dp, d)))]
        for f in fns:
            rel = os.path.normpath(os.path.join(rel_dp, f))
            if _excluded(rel):
                continue
            fp = os.path.join(dp, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out[rel] = [st.st_size, st.st_mtime_ns, _sha(fp) if with_sha else None]
    return out


# ── stages ──────────────────────────────────────────────────────────────────────────────────────
def stage_sync(live_inv: str, occurrence: str, ws_base: Optional[str] = None, holder: str = "runner") -> dict:
    acquire_lock(live_inv, occurrence, holder)
    live_isa = isa_root_of(live_inv)
    ws = workspace_for(occurrence, ws_base)
    os.makedirs(ws, exist_ok=True)
    _ledger(live_inv, {"occurrence": occurrence, "stage": "sync", "status": "STARTED", "workspace": ws})
    t0 = time.time()
    cmd = ["rsync", "-a", "--delete"] + sum([["--exclude", e] for e in EXCLUDES], []) + \
          [live_isa.rstrip("/") + "/", os.path.join(ws, "ISA") + "/"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _ledger(live_inv, {"occurrence": occurrence, "stage": "sync", "status": "FAILED", "failure_scope": "BLOCK_RUN",
                           "why": r.stderr[-800:]})
        raise RunnerRefused("SYNC_FAILED: %s" % r.stderr[-400:])
    ws_isa = os.path.join(ws, "ISA")
    snap = snapshot(ws_isa, with_sha=True)
    json.dump({"occurrence": occurrence, "live_isa": live_isa, "ws_isa": ws_isa, "taken_at": _now(), "files": snap},
              open(os.path.join(ws, "sync_manifest.json"), "w", encoding="utf-8"))
    row = {"occurrence": occurrence, "stage": "sync", "status": "COMPLETE", "workspace": ws, "n_files": len(snap),
           "secs": round(time.time() - t0, 1)}
    _ledger(live_inv, row)
    return row


def stage_run(live_inv: str, occurrence: str, ws_base: Optional[str] = None, timeout_s: int = 170,
              cmd: Optional[List[str]] = None) -> dict:
    ws = workspace_for(occurrence, ws_base)
    if not os.path.exists(os.path.join(ws, "sync_manifest.json")):
        raise RunnerRefused("NOT_SYNCED: run --stage sync first (resume begins at the earliest invalid stage, T74)")
    ws_isa = os.path.join(ws, "ISA")
    ws_inv = os.path.join(ws_isa, os.path.basename(live_inv))
    cmd = cmd or [sys.executable, os.path.join(ws_inv, "monthly_isa_prerun.py"), "--isa-folder", ws_isa]
    _ledger(live_inv, {"occurrence": occurrence, "stage": "run", "status": "STARTED"})
    t0 = time.time()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        r = subprocess.run(cmd, cwd=ws_inv, capture_output=True, text=True, timeout=timeout_s, env=env)
        out, rc, cut = (r.stdout or "") + (r.stderr or ""), r.returncode, False
    except subprocess.TimeoutExpired as exc:
        out, rc, cut = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes)
                        else (exc.stdout or "")), None, True
    log = os.path.join(ws, "run_%s.log" % datetime.datetime.utcnow().strftime("%H%M%S"))
    open(log, "w", encoding="utf-8").write(out)
    if cut or (COMPLETION_TOKEN not in out and ERROR_TOKEN not in out):
        row = {"occurrence": occurrence, "stage": "run", "status": "INCOMPLETE", "failure_scope": "RERUN_STAGE",
               "secs": round(time.time() - t0, 1), "log": log,
               "why": "no completion token (cut off or crashed) - re-run --stage run unchanged (ISA-0780)"}
    elif ERROR_TOKEN in out:
        row = {"occurrence": occurrence, "stage": "run", "status": "COMPLETE_WITH_ERRORS", "failure_scope": "BLOCK_RUN",
               "secs": round(time.time() - t0, 1), "log": log, "rc": rc}
    else:
        row = {"occurrence": occurrence, "stage": "run", "status": "COMPLETE", "secs": round(time.time() - t0, 1),
               "log": log, "rc": rc}
    _ledger(live_inv, row)
    return row


def stage_fetch(live_inv: str, occurrence: str, month: str, ws_base: Optional[str] = None,
                timeout_s: int = 170, pylibs: Optional[str] = None, cmd: Optional[List[str]] = None) -> dict:
    """The scheduled task's Step 2 metrics fetch, IN THE WORKSPACE (yfinance from the tmpfs pylibs).
    Terminal token ALL_DONE; anything else is INCOMPLETE (re-run the stage)."""
    ws = workspace_for(occurrence, ws_base)
    if not os.path.exists(os.path.join(ws, "sync_manifest.json")):
        raise RunnerRefused("NOT_SYNCED: run --stage sync first")
    ws_inv = os.path.join(ws, "ISA", os.path.basename(live_inv))
    cmd = cmd or [sys.executable, "fetch_watchlist_metrics.py", "--watchlist", "watchlist_tickers.json",
                  "--out", "watchlist_metrics_%s.json" % month, "--month-label", month]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if pylibs:
        env["PYTHONPATH"] = pylibs + os.pathsep + env.get("PYTHONPATH", "")
    _ledger(live_inv, {"occurrence": occurrence, "stage": "fetch", "status": "STARTED"})
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=ws_inv, capture_output=True, text=True, timeout=timeout_s, env=env)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    status = "COMPLETE" if "ALL_DONE" in out else "INCOMPLETE"
    row = {"occurrence": occurrence, "stage": "fetch", "status": status, "secs": round(time.time() - t0, 1),
           "tail": out[-600:]}
    _ledger(live_inv, row)
    return row


def _changed_files(ws: str) -> Dict[str, str]:
    man = json.load(open(os.path.join(ws, "sync_manifest.json"), encoding="utf-8"))
    before = man["files"]
    now = snapshot(man["ws_isa"], with_sha=False)
    changed = {}
    for rel, (size, mt, _sh) in now.items():
        b = before.get(rel)
        if b is None:
            changed[rel] = "NEW"
        elif b[0] != size or b[1] != mt:
            fp = os.path.join(man["ws_isa"], rel)
            if _sha(fp) != b[2]:
                changed[rel] = "CHANGED"
    return changed


def _rewrite(data: bytes, ws_isa: str, live_isa: str) -> bytes:
    data = data.replace(ws_isa.encode(), live_isa.encode())
    ws_base = os.path.dirname(ws_isa)
    return data.replace(ws_base.encode(), os.path.dirname(live_isa).encode())


def stage_commit(live_inv: str, occurrence: str, ws_base: Optional[str] = None, verify=None) -> dict:
    ws = workspace_for(occurrence, ws_base)
    run = last_state(live_inv, occurrence, "run")
    if not run or run.get("status") != "COMPLETE":
        raise RunnerRefused("RUN_NOT_COMPLETE: the run stage is %s - nothing is committed from a partial or "
                            "errored pass (T74)" % (run or {}).get("status"))
    man = json.load(open(os.path.join(ws, "sync_manifest.json"), encoding="utf-8"))
    ws_isa, live_isa = man["ws_isa"], man["live_isa"]
    _t0_commit = time.time()                                            # ISA-0830 R5.12: commit duration is runtime evidence
    changed = _changed_files(ws)
    _ledger(live_inv, {"occurrence": occurrence, "stage": "commit", "status": "STARTED", "n_changed": len(changed)})
    conflicts = []
    for rel in changed:
        lp = os.path.join(live_isa, rel)
        b = man["files"].get(rel)
        if os.path.exists(lp):
            if b is None:
                conflicts.append(rel + " (appeared on LIVE after the sync)")
            elif os.path.getsize(lp) != b[0] or _sha(lp) != b[2]:
                conflicts.append(rel + " (changed on LIVE after the sync)")
    if conflicts:
        _ledger(live_inv, {"occurrence": occurrence, "stage": "commit", "status": "REFUSED_CONFLICT",
                           "failure_scope": "BLOCK_RUN", "conflicts": conflicts[:50]})
        raise RunnerRefused("COMMIT_CONFLICT: LIVE changed after the sync for %s - re-sync and re-run (T75)"
                            % conflicts[:5])
    # ⚑ ISA-0830 (R4.17, 04-Oct-2026): the run may write only what the signed artefact contract lets the
    #   monthly pre-run write. A write to SIGNED (immutable) state, or a change to the DECLARED projection of
    #   target_state.json, REFUSES the commit before LIVE is touched (ISA-0775 class: one certified pass made
    #   LIVE UNTRUSTED). An artefact no entry declares is a DEGRADE_ONLY warning here (recorded) and a
    #   certification RED at the release gate. An unreadable contract refuses (R4.3 - fail-closed).
    try:
        import run_contracts as _rc830
        aw = _rc830.classify_writes(changed, "monthly_prerun", root=live_inv, before_dir=live_isa, after_dir=ws_isa)
        blocking = _rc830.blocking_write_violations(aw)
    except Exception as exc:                                            # noqa: BLE001
        aw = {"state": "UNKNOWN", "violations": [{"kind": "CONTRACT_UNREADABLE", "why": "%s: %s" % (type(exc).__name__, exc)}],
              "undeclared": []}
        blocking = aw["violations"]
    if blocking:
        _ledger(live_inv, {"occurrence": occurrence, "stage": "commit", "status": "REFUSED_ARTEFACT_CONTRACT",
                           "failure_scope": "BLOCK_RUN", "violations": blocking[:20]})
        raise RunnerRefused("ARTEFACT_CONTRACT: the run wrote signed/immutable state %s - nothing committed (R4.17)"
                            % [v.get("path") for v in blocking][:5])
    bdir = os.path.join(live_inv, "_bak_prerun_commit", occurrence + "_" + datetime.datetime.utcnow().strftime("%H%M%S"))
    done = []
    try:
        for rel, kind in sorted(changed.items()):
            src, dst = os.path.join(ws_isa, rel), os.path.join(live_isa, rel)
            if os.path.exists(dst):
                bp = os.path.join(bdir, rel)
                os.makedirs(os.path.dirname(bp), exist_ok=True)
                shutil.copy2(dst, bp)
            data = open(src, "rb").read()
            if rel.endswith(TEXT_SUFFIXES):
                data = _rewrite(data, ws_isa, live_isa)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".prr_tmp"
            open(tmp, "wb").write(data)
            os.replace(tmp, dst)
            done.append((rel, kind))
        ver = (verify or verify_live_outputs)(live_inv)
        if ver.get("state") != "VERIFIED":
            raise RunnerRefused("POST_COMMIT_VERIFICATION_FAILED: %s" % ver)
    except Exception as exc:                                            # noqa: BLE001
        for rel, kind in done:
            dst = os.path.join(live_isa, rel)
            bp = os.path.join(bdir, rel)
            if kind == "CHANGED" and os.path.exists(bp):
                shutil.copy2(bp, dst)
            elif kind == "NEW":
                try:
                    os.replace(dst, dst + ".rolled_back")
                except OSError:
                    pass
        _ledger(live_inv, {"occurrence": occurrence, "stage": "commit", "status": "ROLLED_BACK",
                           "failure_scope": "BLOCK_RUN", "why": str(exc)[:800], "n_reverted": len(done)})
        raise
    row = {"occurrence": occurrence, "stage": "commit", "status": "COMPLETE", "n_committed": len(done),
           "secs": round(time.time() - _t0_commit, 1),
           "backup_dir": bdir, "verification": ver,
           "artefact_contract": {"state": aw.get("state"), "undeclared": (aw.get("undeclared") or [])[:30],
                                 "failure_scope": "DEGRADE_ONLY" if aw.get("undeclared") else None}}
    _ledger(live_inv, row)
    release_lock(live_inv, occurrence)
    return row


def verify_live_outputs(live_inv: str) -> dict:
    """Post-commit acceptance ON LIVE: the run's build is the TRUSTED LIVE build, and the Capital Decision
    Receipt of the run month verifies against the committed inputs (a path rewrite or partial copy that
    touched a hash-bound input is caught here and rolls the commit back)."""
    sys.path.insert(0, live_inv)
    import release_gate as rg
    v = rg.verify_live(live_inv)
    rc_path = sorted((f for f in os.listdir(live_inv) if f.startswith("run_context_") and f.endswith(".json")
                      and "DRYRUN" not in f), key=lambda f: os.path.getmtime(os.path.join(live_inv, f)))
    month = None
    if rc_path:
        try:
            month = json.load(open(os.path.join(live_inv, rc_path[-1]), encoding="utf-8"))["_meta"]["month_label"]
        except Exception:                                               # noqa: BLE001
            month = None
    rcv = {"state": "NOT_APPLICABLE"}
    if month:
        import capital_decision_engine as cde
        rcv = cde.verify_receipt(month, live_inv)
        rcv.pop("receipt", None)
    ok = v.get("state") == "TRUSTED" and rcv.get("state") in ("VALID", "NOT_APPLICABLE", "ABSENT")
    return {"state": "VERIFIED" if ok else "FAILED", "live": v.get("state"), "build_id": v.get("build_id"),
            "receipt": rcv.get("state"), "month": month}


# ── selftest (hermetic: temp LIVE tree + fake pre-run) ───────────────────────────────────────────
def _selftest() -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=None):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
        if not cond:
            fails.append(name)
    base = tempfile.mkdtemp(prefix="prr_st_")
    live_isa = os.path.join(base, "LIVE", "ISA")
    live_inv = os.path.join(live_isa, "Investment Analysis")
    os.makedirs(os.path.join(live_inv, "Dashboard", "state"))
    open(os.path.join(live_inv, "a.json"), "w").write('{"x": 1}')
    open(os.path.join(live_inv, "keep.json"), "w").write('{"k": 1}')
    open(os.path.join(live_inv, "signed_cfg.json"), "w").write('{"s": 1}')
    json.dump({"artefacts": [{"path": "Investment Analysis/a.json", "class": "MUTABLE_RUNTIME_STATE", "writers": ["monthly_prerun"]},
                             {"path": "Investment Analysis/new_out.json", "class": "RUN_OUTPUT", "writers": ["monthly_prerun"]},
                             {"path": "Investment Analysis/signed_cfg.json", "class": "SIGNED_IMMUTABLE_CONFIG", "writers": ["release_promotion"]}]},
              open(os.path.join(live_inv, "Dashboard", "state", "artefact_contracts.json"), "w"))
    fake = os.path.join(base, "fake_prerun.py")
    open(fake, "w").write(
        "import sys,os,json\nws=os.getcwd()\n"
        "json.dump({'path': ws, 'v': 2}, open(os.path.join(ws,'a.json'),'w'))\n"
        "open(os.path.join(ws,'new_out.json'),'w').write('{}')\n"
        "print('Review task reads: ' + os.path.join(ws,'run_context_x.json'))\n")
    wsb = os.path.join(base, "WS")
    occ = "prerun-test"
    good = lambda inv: {"state": "VERIFIED"}                                        # noqa: E731
    bad = lambda inv: {"state": "FAILED", "why": "fixture"}                         # noqa: E731
    stage_sync(live_inv, occ, wsb, holder="A")
    try:
        stage_sync(live_inv, occ, wsb, holder="B")
        ok("T76 MUST-FIRE: a second invoker of the same occurrence is refused by the lock", False)
    except RunnerRefused as e:
        ok("T76 MUST-FIRE: a second invoker of the same occurrence is refused by the lock", "LOCKED" in str(e))
    try:
        stage_sync(live_inv, "prerun-other", wsb, holder="C")
        ok("T76 MUST-FIRE: a different occurrence cannot run while one is live", False)
    except RunnerRefused as e:
        ok("T76 MUST-FIRE: a different occurrence cannot run while one is live", "LOCKED" in str(e))
    try:
        stage_commit(live_inv, occ, wsb, verify=good)
        ok("T74 MUST-FIRE: commit before a COMPLETE run is refused", False)
    except RunnerRefused as e:
        ok("T74 MUST-FIRE: commit before a COMPLETE run is refused", "RUN_NOT_COMPLETE" in str(e))
    r = stage_run(live_inv, occ, wsb, cmd=[sys.executable, "-c", "import time; print('partial'); time.sleep(5)"], timeout_s=1)
    ok("T74: a cut-off pass is INCOMPLETE (never a success)", r["status"] == "INCOMPLETE", r)
    try:
        stage_commit(live_inv, occ, wsb, verify=good)
        ok("T74 MUST-FIRE: commit after an INCOMPLETE run is refused", False)
    except RunnerRefused:
        ok("T74 MUST-FIRE: commit after an INCOMPLETE run is refused", True)
    r = stage_run(live_inv, occ, wsb, cmd=[sys.executable, fake])
    ok("NEGATIVE CONTROL T74: the unchanged re-run completes", r["status"] == "COMPLETE", r)
    open(os.path.join(live_inv, "a.json"), "w").write('{"x": 99}')            # LIVE drift after sync
    try:
        stage_commit(live_inv, occ, wsb, verify=good)
        ok("T75 MUST-FIRE: a LIVE file changed after the sync refuses the commit", False)
    except RunnerRefused as e:
        ok("T75 MUST-FIRE: a LIVE file changed after the sync refuses the commit", "COMMIT_CONFLICT" in str(e))
    open(os.path.join(live_inv, "a.json"), "w").write('{"x": 1}')             # restore the bytes
    try:
        stage_commit(live_inv, occ, wsb, verify=bad)
        ok("MUST-FIRE: failed post-commit verification ROLLS BACK every committed file", False)
    except Exception:                                                   # noqa: BLE001
        ok("MUST-FIRE: failed post-commit verification ROLLS BACK every committed file",
           open(os.path.join(live_inv, "a.json")).read() == '{"x": 1}'
           and not os.path.exists(os.path.join(live_inv, "new_out.json")))
    # ── ISA-0830 (R4.17): a run that writes SIGNED state cannot commit ─────────────────────────
    _ws_inv = os.path.join(workspace_for(occ, wsb), "ISA", "Investment Analysis")
    open(os.path.join(_ws_inv, "signed_cfg.json"), "w").write('{"s": 2}')
    try:
        stage_commit(live_inv, occ, wsb, verify=good)
        ok("ISA-0830 MUST-FIRE: a run that rewrote SIGNED_IMMUTABLE_CONFIG is REFUSED and LIVE is untouched", False)
    except RunnerRefused as e:
        ok("ISA-0830 MUST-FIRE: a run that rewrote SIGNED_IMMUTABLE_CONFIG is REFUSED and LIVE is untouched",
           "ARTEFACT_CONTRACT" in str(e) and open(os.path.join(live_inv, "signed_cfg.json")).read() == '{"s": 1}'
           and open(os.path.join(live_inv, "a.json")).read() == '{"x": 1}', str(e))
    open(os.path.join(_ws_inv, "signed_cfg.json"), "w").write('{"s": 1}')
    os.utime(os.path.join(_ws_inv, "signed_cfg.json"), ns=(os.stat(os.path.join(live_inv, "signed_cfg.json")).st_mtime_ns,) * 2)
    open(os.path.join(_ws_inv, "undeclared_store.json"), "w").write('{}')
    c = stage_commit(live_inv, occ, wsb, verify=good)
    ok("ISA-0830 NEGATIVE CONTROL: an UNDECLARED artefact does not block the commit; it is recorded DEGRADE_ONLY",
       c["status"] == "COMPLETE" and "Investment Analysis/undeclared_store.json" in c["artefact_contract"]["undeclared"]
       and c["artefact_contract"]["failure_scope"] == "DEGRADE_ONLY", c.get("artefact_contract"))
    a = json.load(open(os.path.join(live_inv, "a.json")))
    ok("NEGATIVE CONTROL: a verified commit copies changed + new outputs and rewrites the workspace path",
       c["status"] == "COMPLETE" and a["v"] == 2 and a["path"] == live_inv
       and os.path.exists(os.path.join(live_inv, "new_out.json")), (c, a))
    ok("unchanged files are not touched", open(os.path.join(live_inv, "keep.json")).read() == '{"k": 1}')
    ok("the lock is released after a verified commit",
       json.load(open(os.path.join(live_inv, LOCK_REL)))["released"] is True)
    st = [(x["stage"], x["status"]) for x in ledger_rows(live_inv, occ)]
    ok("the stage ledger is append-only and records every transition",
       ("run", "INCOMPLETE") in st and ("commit", "ROLLED_BACK") in st and st[-1] == ("commit", "COMPLETE"), st)
    stage_sync(live_inv, "prerun-other", wsb, holder="C")
    ok("NEGATIVE CONTROL T76: after release the next occurrence may run", True)
    shutil.rmtree(base, ignore_errors=True)
    print("prerun_runner selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv and argv.index(n) + 1 < len(argv) else d
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    live_inv = os.path.abspath(opt("--inv-dir", HERE))
    occ = opt("--occurrence") or default_occurrence()
    ws_base = opt("--workspace")
    try:
        if "--status" in argv:
            print(json.dumps(ledger_rows(live_inv, occ), indent=1))
            return 0
        st = opt("--stage")
        if st == "sync":
            r = stage_sync(live_inv, occ, ws_base, holder=opt("--holder", "scheduled-prerun"))
        elif st == "run":
            r = stage_run(live_inv, occ, ws_base, timeout_s=int(opt("--timeout", "170")))
        elif st == "fetch":
            r = stage_fetch(live_inv, occ, opt("--month"), ws_base, pylibs=opt("--pylibs"))
        elif st == "commit":
            r = stage_commit(live_inv, occ, ws_base)
        else:
            print(__doc__)
            return 2
    except RunnerRefused as exc:
        print("PRERUN_RUNNER_REFUSED %s" % exc)
        return 3
    print(json.dumps(r, indent=1, default=str))
    print("PRERUN_RUNNER_STAGE %s %s" % (r.get("stage"), r.get("status")))
    return 0 if r.get("status") == "COMPLETE" else 4


if __name__ == "__main__":
    sys.exit(main())
