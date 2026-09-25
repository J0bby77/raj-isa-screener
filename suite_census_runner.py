#!/usr/bin/env python3
"""
suite_census_runner.py — ISA-0696: a fresh, isolated suite census between builds.

Authority: BS-0696 (ChatGPT Astra 6 Audit/ISA_BuildSpecs_AnalysisFirst_and_Taxonomy_Resolution_16Sep2026.md §8)
as amended by ISA_BuildSpec_ISA-0696_Amendment_A1_DataIdentityAndScheduler_17Sep2026.md. R5.7, KR5, R18.1, R18.5.

WHY. The census (consistency_check.record_suite_status) was recorded only when a build certified, so
between builds suite_status.json aged past KR5's 8 days and data-dependent selftests could turn RED with
nothing observing them. Selftests may not run inside LIVE (ISA-0704), so this runner:

  1. PREPARES a sandbox copy of LIVE (same exclusions as a Candidate), refusing when LIVE is not TRUSTED
     or the copy's source/config rolls differ from LIVE's. Any suite status inside the copy is removed,
     so a merged "resume" can never re-publish an old census as fresh.
  2. STEPS the census in the sandbox in budgeted chunks (a host call is ~180 s), every suite in its own
     guarded subprocess; LIVE and the ISA folder are protected roots (ISA_SUITE_PROTECT).
  3. PUBLISHES the completed census to LIVE `Dashboard/state/suite_status.json` (+ one line of
     `suite_census_history.jsonl`) only if LIVE's source/config are still what the census ran on, with
     provenance, duration and a LIVE before/after snapshot as isolation evidence.

The capital boundary reads the result through consistency_check.suite_status_state (FRESH_GREEN only),
inside release_gate.capital_run_authority. A census is ASSURANCE, never a capital signal.

CLI:  python3 suite_census_runner.py --step [--budget 150] [--kind scheduled|manual]
      -> CENSUS_PROGRESS n/m | CENSUS_PUBLISHED state=... | CENSUS_REFUSED why=...
      python3 suite_census_runner.py --status        (the preflight reading of LIVE, JSON)
      python3 suite_census_runner.py --ensure [--budget 150]   (run-time: CENSUS_FRESH, or steps until published)
      python3 suite_census_runner.py --lead          (scheduled lead census: LEAD_NOT_DUE or steps; A2)
      python3 suite_census_runner.py --cadence       (lead-window coverage of past key runs)
ISA-0696 A2 (Raj, 17-Sep-2026): the census is EVENT-DRIVEN - fresh at every capital run (ensure) plus one lead
census in the 5..1-day window before each monthly pre-run and VCI run; no weekly cadence.
ROLLBACK (R4.13): isa_policy.V2_FLAGS["census_gates_capital"] = False stops the capital gate consuming it;
this runner then only records.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

STATE_REL = os.path.join("Dashboard", "state")
STATUS_REL = os.path.join(STATE_REL, "suite_status.json")
HISTORY_REL = os.path.join(STATE_REL, "suite_census_history.jsonl")
META = "census_runner_meta.json"
DEFAULT_SANDBOX = os.path.join(os.path.expanduser("~"), ".isa_census_sandbox")
IGNORE_PREFIXES = ("archive", "_bak", "_to_delete", "__pycache__", "_baseline", "register_archive",
                   "_dryrun_outputs", "_candidate_evidence", ".git")
PREPARE_MAX_AGE_H = 24


def _now():
    return datetime.datetime.now()


def _tree(sandbox: str) -> str:
    return os.path.join(sandbox, "Investment Analysis")


def _read(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                                   # noqa: BLE001
        return default


def _write_atomic(p: str, doc) -> None:
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, default=str)
    os.replace(tmp, p)


def _rolls(root: str) -> dict:
    import release_gate as rg
    return {"source_roll": rg.source_fingerprint(root).get("roll"),
            "config_roll": rg.config_fingerprint(root).get("roll"),
            "census_roll": rg.census_roll(root).get("roll")}          # ISA-0746


def prepare(live: str, sandbox: str = DEFAULT_SANDBOX) -> dict:
    """Fresh sandbox copy of LIVE. -> {"state": "PREPARED"|"REFUSED", ...}"""
    import release_gate as rg
    import isa_write_guard as wg
    v = rg.verify_live(live)
    if v.get("state") != "TRUSTED":
        return {"state": "REFUSED", "why": "LIVE is %s against %s - a census of unsigned code cannot "
                                           "vouch for a Trusted build (R18.5)" % (v.get("state"), v.get("build_id"))}
    if os.path.isdir(sandbox):
        shutil.rmtree(sandbox)
    os.makedirs(sandbox)
    tree = _tree(sandbox)

    def ignore(d, names):
        return [n for n in names if any(n == x or n.startswith(x) for x in IGNORE_PREFIXES)]
    shutil.copytree(live, tree, ignore=ignore, symlinks=True)
    # ⚑ ISA-0729 (23-Sep-2026) — R5.12 CANDIDATE PARITY. Several release scripts read the monthly
    #   inputs Raj saves in the PARENT folder (Cash Statement, AJ Bell portfolio/X-Ray, Transaction
    #   History; extract_cash_statement.parse defaults to dirname(HERE)). A sandbox holding only the
    #   tree read those as ABSENT and turned a data read into a false RED. The parent's top-level
    #   FILES (not its folders) are copied beside the sandbox tree, read-only inputs.
    _parent = os.path.dirname(os.path.realpath(live))
    _n_parent = 0
    for _fn in sorted(os.listdir(_parent)):
        _src = os.path.join(_parent, _fn)
        if os.path.isfile(_src) and not os.path.islink(_src):
            shutil.copy2(_src, os.path.join(sandbox, _fn))
            _n_parent += 1
    for stale in (STATUS_REL, HISTORY_REL):
        if os.path.exists(os.path.join(tree, stale)):
            os.remove(os.path.join(tree, stale))
    lr, sr = _rolls(live), _rolls(tree)
    if lr != sr or None in lr.values():
        return {"state": "REFUSED", "why": "sandbox rolls %s differ from LIVE %s - the copy is not the Trusted "
                                           "tree" % (sr, lr)}
    meta = {"prepared_at": _now().isoformat(timespec="seconds"), "live_root": os.path.realpath(live),
            "build_id": v.get("build_id"), "parent_inputs_copied": _n_parent, **lr}
    _write_atomic(os.path.join(sandbox, META), meta)
    _write_atomic(os.path.join(sandbox, "live_snapshot_before.json"), wg.snapshot(live))
    return dict(meta, state="PREPARED")


def _needs_prepare(live: str, sandbox: str) -> bool:
    meta = _read(os.path.join(sandbox, META))
    if not meta or not os.path.isdir(_tree(sandbox)):
        return True
    if meta.get("live_root") != os.path.realpath(live) or _rolls(live) != {
            "source_roll": meta.get("source_roll"), "config_roll": meta.get("config_roll"),
            "census_roll": meta.get("census_roll")}:
        return True
    try:
        age_h = (_now() - datetime.datetime.fromisoformat(meta["prepared_at"])).total_seconds() / 3600
    except Exception:                                                   # noqa: BLE001
        return True
    return age_h > PREPARE_MAX_AGE_H


def step(live: str = HERE, sandbox: str = DEFAULT_SANDBOX, budget_s: float = 150.0,
         kind: str = "manual", timeout_s: int = 120) -> dict:
    """One budgeted chunk. Prepares when needed, runs suites, publishes when the census is complete."""
    import consistency_check as cc
    import isa_write_guard as wg
    live = os.path.realpath(live)
    t0 = _now()
    if _needs_prepare(live, sandbox):
        p = prepare(live, sandbox)
        if p["state"] != "PREPARED":
            return {"state": "REFUSED", "why": p["why"]}
    tree = _tree(sandbox)
    spent = (_now() - t0).total_seconds()
    env_old = {k: os.environ.get(k) for k in ("ISA_SUITE_PROTECT", "ISA_CENSUS_KIND")}
    os.environ["ISA_SUITE_PROTECT"] = os.pathsep.join([live, os.path.dirname(live)])
    os.environ["ISA_CENSUS_KIND"] = kind
    # an already-installed guard (e.g. this selftest running inside the census) is left as it is: installing
    # would widen it and uninstalling would remove the OUTER guard (the ISA-0704 lesson).
    outer = bool(wg._STATE.get("installed"))
    if not outer:
        wg.install([live, os.path.dirname(live)],
                   [sandbox, os.path.realpath(sandbox), __import__("tempfile").gettempdir()])
    try:
        doc = cc.record_suite_status(root=tree, merge=True, budget_s=max(budget_s - spent, 1.0),
                                     timeout_s=timeout_s)
    finally:
        blocked = wg.manifest().get("n_blocked", 0) if wg._STATE.get("installed") else 0
        if not outer:
            wg.uninstall()
        for k, val in env_old.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    c = doc.get("census") or {}
    if not c.get("complete") or any(r.get("state") == "TIMEOUT" for r in doc.get("rows", [])):
        timeouts = sum(1 for r in doc.get("rows", []) if r.get("state") == "TIMEOUT")
        return {"state": "PROGRESS", "recorded": c.get("n_recorded"), "on_disk": c.get("n_on_disk"),
                "timeouts": timeouts, "runner_write_escapes": blocked}
    return publish(live, sandbox, runner_write_escapes=blocked)


def publish(live: str, sandbox: str, runner_write_escapes: int = 0) -> dict:
    import consistency_check as cc
    import isa_write_guard as wg
    tree = _tree(sandbox)
    meta = _read(os.path.join(sandbox, META)) or {}
    doc = _read(os.path.join(tree, STATUS_REL))
    if not doc or not (doc.get("census") or {}).get("complete"):
        return {"state": "REFUSED", "why": "no complete census in the sandbox"}
    lr = _rolls(live)
    if (doc.get("source_roll") != lr["source_roll"] or doc.get("config_roll") != lr["config_roll"]
            or doc.get("census_roll") != lr["census_roll"]):
        return {"state": "REFUSED", "why": "LIVE source/config/signed surfaces changed while the census ran - not published; "
                                           "the next --step re-prepares"}
    before = _read(os.path.join(sandbox, "live_snapshot_before.json"), {}) or {}
    after = wg.snapshot(live)
    changed = wg.diff({k: tuple(v) for k, v in before.items()}, after)
    changed = {k: [x for x in v if not x.startswith((STATUS_REL, HISTORY_REL))] for k, v in changed.items() if k != "n"}
    n_changed = sum(len(v) for v in changed.values())
    published_at = _now()
    try:
        dur = (published_at - datetime.datetime.fromisoformat(meta["prepared_at"])).total_seconds()
    except Exception:                                                   # noqa: BLE001
        dur = None
    doc["provenance"] = {"runner": "suite_census_runner", "census_kind": doc.get("census_kind"),
                         "prepared_at": meta.get("prepared_at"), "published_at": published_at.isoformat(timespec="seconds"),
                         "duration_s": dur, "sandbox": tree, "build_id_at_prepare": meta.get("build_id"),
                         "runner_write_escapes_blocked": runner_write_escapes,
                         "live_changed_during_census": {"n": n_changed, "files": {k: v[:20] for k, v in changed.items() if v},
                                                        "attribution": ("not the census: suites ran in the sandbox under a "
                                                                        "guard protecting LIVE" if not runner_write_escapes
                                                                        else "UNKNOWN - the runner blocked write escapes")}}
    doc["build_id"] = meta.get("build_id") or doc.get("build_id")
    os.makedirs(os.path.join(live, STATE_REL), exist_ok=True)
    _write_atomic(os.path.join(live, STATUS_REL), doc)
    st = cc.suite_status_state(live)
    line = {"published_at": doc["provenance"]["published_at"], "as_of_ts": doc.get("as_of_ts"),
            "state": st.get("state"), "census_kind": doc.get("census_kind"), "build_id": doc.get("build_id"),
            "duration_s": dur, "counts": doc.get("counts"), "suite_secs_total": doc.get("suite_secs_total"),
            "non_green": [r.get("module") for r in doc.get("rows", []) if r.get("state") != "GREEN"],
            "live_changed_during_census": n_changed}
    with open(os.path.join(live, HISTORY_REL), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    return {"state": "PUBLISHED", "census_state": st.get("state"), "why": st.get("why"),
            "duration_s": dur, "live_changed_during_census": n_changed}


LEAD_WINDOW_DAYS = 5


def _first_sunday(y: int, m: int) -> datetime.date:
    d = datetime.date(y, m, 1)
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def key_runs(today: datetime.date, months_ahead: int = 2) -> list:
    """Scheduled capital-decision runs a lead census must precede (A2): the monthly pre-run (the Saturday
    before the first Sunday, isa-monthly-prerun occurrence guard) and the VCI run (the 2nd Sunday,
    vci-monthly-value-chain-intelligence occurrence guard). Intramonth is on demand -> ensure() only."""
    out = []
    y, m = today.year, today.month
    for i in range(-1, months_ahead + 1):
        yy, mm = (y + (m - 1 + i) // 12, (m - 1 + i) % 12 + 1)
        fs = _first_sunday(yy, mm)
        out.append({"run": "monthly_prerun", "date": fs - datetime.timedelta(days=1)})
        out.append({"run": "vci_run", "date": fs + datetime.timedelta(days=7)})
    return sorted(out, key=lambda r: r["date"])


def lead_due(live: str = HERE, today=None, state=None) -> dict:
    """-> {"due": bool, "key_run", "window", "why"}. Due inside [K-5, K-1] of a key run K when no FRESH_GREEN
    census has been recorded since the window opened (a red census stays due, so a fix is re-verified)."""
    import consistency_check as cc
    today = today or _now().date()
    st = state if state is not None else cc.suite_status_state(live)
    for k in key_runs(today):
        start, end = k["date"] - datetime.timedelta(days=LEAD_WINDOW_DAYS), k["date"] - datetime.timedelta(days=1)
        if start <= today <= end:
            try:
                recorded = datetime.datetime.fromisoformat(st.get("as_of_ts")).date() if st.get("as_of_ts") else None
            except Exception:                                           # noqa: BLE001
                recorded = None
            win = {"key_run": k["run"], "key_date": k["date"].isoformat(), "window": [start.isoformat(), end.isoformat()]}
            if st.get("state") == "FRESH_GREEN" and recorded is not None and recorded >= start:
                return dict(win, due=False, why="a FRESH_GREEN census was already recorded in this window (%s)" % st.get("as_of_ts"))
            return dict(win, due=True, why="census is %s, recorded %s - the lead census for %s %s is due"
                                           % (st.get("state"), st.get("as_of_ts"), k["run"], k["date"].isoformat()))
    nxt = next((k for k in key_runs(today) if k["date"] > today), None)
    return {"due": False, "key_run": None, "window": None,
            "why": "outside every lead window; next key run %s on %s" % ((nxt or {}).get("run"), (nxt or {}).get("date"))}


def ensure(live: str = HERE, sandbox: str = DEFAULT_SANDBOX, budget_s: float = 150.0, kind: str = "run_time") -> dict:
    """A2 run-time self-healing: FRESH_GREEN -> no-op; otherwise step until published or the budget is spent.
    Never raises into a capital run; the capital gate still decides from the resulting state."""
    import consistency_check as cc
    t0 = _now()
    st = cc.suite_status_state(live)
    if st.get("state") == "FRESH_GREEN":
        return {"state": "FRESH", "census_state": "FRESH_GREEN", "why": st.get("why")}
    last = {"state": "PROGRESS"}
    while True:
        left = budget_s - (_now() - t0).total_seconds()
        if left < 5:
            break
        last = step(live, sandbox, budget_s=left, kind=kind)
        if last["state"] in ("PUBLISHED", "REFUSED"):
            return dict(last, before=st.get("state"))
    return dict(last, state="INCOMPLETE_BUDGET", before=st.get("state"),
                why="census not finished within %.0f s; progress is kept - the next ensure/--step resumes" % budget_s)


def cadence(live: str = HERE, now=None, history=None) -> dict:
    """A2 observability: for each past key run, was a SCHEDULED census published inside its lead window?"""
    now = now or _now()
    if history is None:
        history = []
        try:
            with open(os.path.join(live, HISTORY_REL), encoding="utf-8") as fh:
                history = [json.loads(x) for x in fh if x.strip()]
        except OSError:
            history = []
    sched = [(datetime.datetime.fromisoformat(h["published_at"]).date(), h.get("state")) for h in history
             if h.get("census_kind") == "scheduled" and h.get("published_at")]
    past = [k for k in key_runs(now.date()) if k["date"] <= now.date()][-3:]
    rows = []
    for k in past:
        start = k["date"] - datetime.timedelta(days=LEAD_WINDOW_DAYS)
        inwin = [st for d, st in sched if start <= d < k["date"]]
        rows.append({"key_run": k["run"], "date": k["date"].isoformat(), "lead_published": bool(inwin),
                     "lead_green": "FRESH_GREEN" in inwin})
    if not sched:
        return {"state": "NO_SCHEDULED_OCCURRENCE", "rows": rows,
                "why": "no scheduled lead census has ever published - the lead surface is not yet evidenced"}
    missed = [r for r in rows if not r["lead_published"]]
    return {"state": "MISSED" if missed else "ON_CADENCE", "rows": rows, "n_scheduled": len(sched),
            "missed": [r["key_run"] + "@" + r["date"] for r in missed]}


def _selftest(verbose: bool = True) -> int:
    import tempfile
    import consistency_check as cc
    import release_gate as rg
    fails = []

    def ok(name, cond, detail=""):
        if verbose or not cond:
            print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:300]) if not cond else ""))
        if not cond:
            fails.append(name)

    saved_core = cc.BATTERY_SELFTEST_MODULES
    cc.BATTERY_SELFTEST_MODULES = ()                     # the mini tree carries no core battery modules
    base = tempfile.mkdtemp(prefix="census_runner_selftest_")
    live = os.path.join(base, "ISA", "Investment Analysis")
    sandbox = os.path.join(base, "sandbox")
    try:
        os.makedirs(os.path.join(live, STATE_REL))
        os.makedirs(os.path.join(live, "Skills_to_Edit", "a-task"))
        with open(os.path.join(live, "Skills_to_Edit", "a-task", "SKILL.md"), "w") as fh:
            fh.write("# a task\n")
        with open(os.path.join(live, "s_green.py"), "w") as fh:
            fh.write("def _selftest():\n    return 0\n")
        with open(os.path.join(live, "data.json"), "w") as fh:
            fh.write("{}")
        for cf in rg.CONFIG_FILES:                       # placeholders: config is fingerprinted, not interpreted
            os.makedirs(os.path.dirname(os.path.join(live, cf)) or live, exist_ok=True)
            with open(os.path.join(live, cf), "w") as fh:
                fh.write("V2_FLAGS = {}\n" if cf.endswith(".py") else "{}\n")

        def sign():
            with open(rg.receipt_path(live), "w") as fh:
                json.dump({"build_id": "TB-SELFTEST", "fingerprints": rg.live_fingerprints(live)}, fh)
        sign()
        # a planted OLD all-GREEN status with the same rolls: a resume must never re-publish it
        _planted = {"as_of": "2026-01-01", "as_of_ts": "2026-01-01T00:00:00", **_rolls(live),
                    "rows": [{"module": "s_green", "state": "GREEN", "write_isolation": "CLEAN"}],
                    "census": {"complete": True, "n_on_disk": 1, "n_recorded": 1}, "isolation": {"all_clean": True}}
        _write_atomic(os.path.join(live, STATUS_REL), _planted)
        sign()
        ok("fixture LIVE is TRUSTED", rg.verify_live(live)["state"] == "TRUSTED", rg.verify_live(live))
        ok("NEGATIVE CONTROL: before the runner the planted 2026-01-01 census is STALE and capital is REFUSED",
           cc.suite_status_state(live)["state"] == "STALE" and rg.capital_run_authority("t", live)["authority"] == "REFUSED")
        snap0 = __import__("isa_write_guard").snapshot(live)
        r, n = {"state": "PROGRESS"}, 0
        while r["state"] == "PROGRESS" and n < 10:
            r = step(live, sandbox, budget_s=60, kind="scheduled")
            n += 1
        ok("MUST-FIRE: the runner publishes a complete census (state PUBLISHED) through prepare -> step -> publish",
           r["state"] == "PUBLISHED", r)
        pub = _read(os.path.join(live, STATUS_REL)) or {}
        ok("NEGATIVE CONTROL: the published census is a NEW run, not the planted status (timestamp + provenance)",
           pub.get("as_of_ts", "").startswith(datetime.date.today().isoformat()) and pub.get("provenance", {}).get("runner")
           == "suite_census_runner", pub.get("as_of_ts"))
        ok("MUST-FIRE (delivered preflight consumes it): FRESH_GREEN and capital authority AUTHORISED on the same tree",
           cc.suite_status_state(live)["state"] == "FRESH_GREEN"
           and rg.capital_run_authority("t", live)["authority"] == "AUTHORISED", rg.capital_run_authority("t", live))
        d = __import__("isa_write_guard").diff(snap0, __import__("isa_write_guard").snapshot(live))
        touched = set(d["modified"]) | set(d["created"]) | set(d["deleted"])
        ok("ISOLATION: the only LIVE files the census touched are suite_status.json and the history line",
           touched <= {STATUS_REL, HISTORY_REL}, d)
        ok("A2 ensure(): a FRESH_GREEN census is a no-op (state FRESH, nothing re-run)",
           ensure(live, sandbox, budget_s=30)["state"] == "FRESH")
        # a data-dependent RED suite -> FRESH_RED -> REFUSED
        with open(os.path.join(live, "s_red.py"), "w") as fh:
            fh.write("import json\ndef _selftest():\n    d = json.load(open('data.json'))\n"
                     "    assert d.get('anchor') == 13.9, 'data moved'\n    return 0\n")
        sign()
        r, n = {"state": "PROGRESS"}, 0
        while r["state"] == "PROGRESS" and n < 10:
            r = step(live, sandbox, budget_s=60, kind="scheduled")
            n += 1
        ok("MUST-FIRE: a failing data-dependent suite publishes FRESH_RED and capital authority is REFUSED",
           r.get("census_state") == "FRESH_RED" and rg.capital_run_authority("t", live)["authority"] == "REFUSED", r)
        # an unsigned LIVE is refused before anything is copied or published
        with open(os.path.join(live, "s_green.py"), "a") as fh:
            fh.write("# unsigned\n")
        _pre = _read(os.path.join(live, STATUS_REL))
        r = step(live, sandbox, budget_s=30, kind="scheduled")
        ok("NEGATIVE CONTROL: an UNTRUSTED LIVE is CENSUS_REFUSED and the published status is untouched",
           r["state"] == "REFUSED" and _read(os.path.join(live, STATUS_REL)) == _pre, r)
        # A2 key runs: Oct-2026 pre-run Sat 03-Oct (first Sunday 04-Oct), VCI Sun 11-Oct
        _kr = {(k["run"], k["date"].isoformat()) for k in key_runs(datetime.date(2026, 9, 17))}
        ok("A2 key_runs: the Oct-2026 pre-run is Sat 03-Oct and the VCI run Sun 11-Oct; Nov pre-run Sat 31-Oct",
           {("monthly_prerun", "2026-10-03"), ("vci_run", "2026-10-11"), ("monthly_prerun", "2026-10-31")} <= _kr, sorted(_kr))
        _g = {"state": "FRESH_GREEN", "as_of_ts": "2026-09-17T08:12:45"}
        ok("A2 NEGATIVE CONTROL: 17-Sep is outside every lead window -> not due",
           lead_due(live, today=datetime.date(2026, 9, 17), state=_g)["due"] is False)
        ok("A2 MUST-FIRE: 29-Sep is inside the 03-Oct pre-run window and the last census predates it -> due",
           lead_due(live, today=datetime.date(2026, 9, 29), state=_g)["due"] is True)
        ok("A2: a FRESH_GREEN census already recorded inside the window -> not due again",
           lead_due(live, today=datetime.date(2026, 9, 30),
                    state={"state": "FRESH_GREEN", "as_of_ts": "2026-09-29T07:05:00"})["due"] is False)
        ok("A2 MUST-FIRE: a FRESH_RED census inside the window stays due, so the fix is re-verified",
           lead_due(live, today=datetime.date(2026, 9, 30),
                    state={"state": "FRESH_RED", "as_of_ts": "2026-09-29T07:05:00"})["due"] is True)
        ok("A2: the VCI window (06..10-Oct) is due even after the pre-run window's green census",
           lead_due(live, today=datetime.date(2026, 10, 7),
                    state={"state": "FRESH_GREEN", "as_of_ts": "2026-09-29T07:05:00"})["due"] is True)
        _now0 = datetime.datetime(2026, 10, 12, 9, 0)
        hh = [{"census_kind": "scheduled", "published_at": "2026-09-29T07:10:00", "state": "FRESH_GREEN"},
              {"census_kind": "manual", "published_at": "2026-10-08T08:00:00", "state": "FRESH_GREEN"}]
        ok("A2 MUST-FIRE (missed lead window): VCI 11-Oct had only a MANUAL census in its window -> MISSED",
           cadence(live, now=_now0, history=hh)["state"] == "MISSED" and "vci_run@2026-10-11" in cadence(live, now=_now0, history=hh)["missed"],
           cadence(live, now=_now0, history=hh))
        ok("cadence comparator: scheduled lead censuses before both key runs -> ON_CADENCE",
           cadence(live, now=_now0, history=hh + [{"census_kind": "scheduled", "published_at": "2026-10-07T07:10:00",
                                                  "state": "FRESH_GREEN"},
                                                 {"census_kind": "scheduled", "published_at": "2026-09-03T07:10:00",
                                                  "state": "FRESH_GREEN"},
                                                 {"census_kind": "scheduled", "published_at": "2026-09-10T07:10:00",
                                                  "state": "FRESH_GREEN"}])["state"] == "ON_CADENCE")
        ok("NEGATIVE CONTROL: no scheduled publication at all is NO_SCHEDULED_OCCURRENCE, never ON_CADENCE",
           cadence(live, now=_now0, history=[])["state"] == "NO_SCHEDULED_OCCURRENCE")
        # wiring: the pre-run self-heals BEFORE it asks for capital authority
        _pr = os.path.join(HERE, "monthly_isa_prerun.py")
        if os.path.exists(_pr):
            import ast as _ast
            _t = _ast.parse(open(_pr, encoding="utf-8").read())
            _main = next((x for x in _t.body if isinstance(x, _ast.FunctionDef) and x.name == "main"), None)
            _ln = {}
            for _n in (_ast.walk(_main) if _main else []):
                if isinstance(_n, _ast.Call):
                    _nm = getattr(_n.func, "attr", getattr(_n.func, "id", None))
                    if _nm in ("ensure", "_capital_authority_step"):
                        _ln.setdefault(_nm, _n.lineno)
            ok("A2 WIRING (AST): monthly_isa_prerun.main calls suite_census_runner.ensure before _capital_authority_step",
               len(_ln) == 2 and _ln["ensure"] < _ln["_capital_authority_step"], _ln)
    finally:
        cc.BATTERY_SELFTEST_MODULES = saved_core
        shutil.rmtree(base, ignore_errors=True)
    if verbose:
        print("suite_census_runner selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    live = opt("--live", HERE)
    sandbox = opt("--sandbox", DEFAULT_SANDBOX)
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--status" in argv:
        import consistency_check as cc
        print(json.dumps(cc.suite_status_state(live), indent=1, default=str))
        return 0
    if "--ensure" in argv:
        r = ensure(live, sandbox, budget_s=float(opt("--budget", 150)), kind=opt("--kind", "run_time"))
        if r["state"] == "FRESH":
            print("CENSUS_FRESH | %s" % r.get("why"))
            return 0
        if r["state"] == "PUBLISHED":
            print("CENSUS_PUBLISHED state=%s | %s" % (r.get("census_state"), r.get("why")))
            return 0 if r.get("census_state") == "FRESH_GREEN" else 3
        if r["state"] == "INCOMPLETE_BUDGET":
            print("CENSUS_PROGRESS (budget spent) - run --ensure again")
            return 0
        print("CENSUS_REFUSED why=%s" % r.get("why"))
        return 2
    if "--lead" in argv:
        d = lead_due(live)
        if not d["due"]:
            print("LEAD_NOT_DUE | %s" % d["why"])
            return 0
        r = step(live, sandbox, budget_s=float(opt("--budget", 150)), kind="scheduled")
        if r["state"] == "PROGRESS":
            print("CENSUS_PROGRESS %s/%s for %s %s - run --lead again" % (r.get("recorded"), r.get("on_disk"), d["key_run"], d["key_date"]))
            return 0
        if r["state"] == "PUBLISHED":
            import consistency_check as cc
            st = cc.suite_status_state(live)
            if r.get("census_state") != "FRESH_GREEN":
                print("CENSUS_RED state=%s failing=%s | lead census for %s %s - FIX BEFORE THE RUN or it will be REFUSED"
                      % (r.get("census_state"), ",".join(x["module"] for x in st.get("non_green") or []) or st.get("why"),
                         d["key_run"], d["key_date"]))
                return 3
            print("CENSUS_PUBLISHED state=FRESH_GREEN for %s %s | %s" % (d["key_run"], d["key_date"], r.get("why")))
            return 0
        print("CENSUS_REFUSED why=%s" % r.get("why"))
        return 2
    if "--cadence" in argv:
        print(json.dumps(cadence(live), indent=1, default=str))
        return 0
    if "--step" in argv:
        r = step(live, sandbox, budget_s=float(opt("--budget", 150)), kind=opt("--kind", "manual"))
        if r["state"] == "PROGRESS":
            print("CENSUS_PROGRESS %s/%s timeouts=%s" % (r.get("recorded"), r.get("on_disk"), r.get("timeouts")))
        elif r["state"] == "PUBLISHED":
            print("CENSUS_PUBLISHED state=%s duration_s=%s live_changed_during_census=%s | %s"
                  % (r.get("census_state"), r.get("duration_s"), r.get("live_changed_during_census"), r.get("why")))
        else:
            print("CENSUS_REFUSED why=%s" % r.get("why"))
        return 0 if r["state"] in ("PROGRESS", "PUBLISHED") else 2
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
