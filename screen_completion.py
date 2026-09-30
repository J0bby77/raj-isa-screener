#!/usr/bin/env python3
"""
screen_completion.py - ISA-0755 / ISA-0483 (26-Sep-2026): THE END-OF-RUN COMPLETION RECEIPT for a
weekly Growth screen. Authority: ISA_PreOct_Production_Readiness_BuildSpec_Handoff_FINAL_26Sep2026.md
Package B1; ISA_Engineering_Rules.md R4.11 (capture is a property of producing the artefact), R2.10
(absent is not empty), R4.12 (no swallowed failures), R20.2 (the email is a renderer).

WHY. On 26-Sep the STOXX600 screen skipped register capture (16b-2) and the source-performance
append, and the email still said "File saved". On 25-Sep the NASDAQ fallback never reconciled what it
wrote, and the whole observation was lost when the sandbox recycled. Completion was a property of
someone reaching the end of a prose list. It is now a RECEIPT, evaluated from the CANONICAL stores
(never from a sandbox/outputs copy), and build_email refuses a normal success email without one.

WHAT IT CHECKS - existing mechanisms only, no parallel store:
  workbook             the canonical OneDrive copy (Investment Analysis), never an outputs/ copy
  screen_history       screen_history/<YYYY-MM-DD>_<GROUP>_full_data.csv (capture_screen_artefacts)
  score_panel          score_panel.csv rows for (run_date, group) == scored rows
  constituents         constituents_history.csv rows for (run_date, group)
  regime               regime_history.csv row for run_date, stamp_basis 'live' (else DEGRADED)
  capture_status       screen_capture_status.json for this run
  fallback             NOT_USED | RECONCILED | UNRECONCILED (canonical rows present and counts agree)
  retrospective        isa_retrospective_intake.run_findings: CAPTURED | NO_FINDINGS_CAPTURED | MISSING
  source_performance   source_performance_writer.run_logged (runs[] entry for this run)
  population           reconciled waterfall: universe = scored + gate-rejected; scored = sum(status)

OVERALL: FAILED (no core screen output) | INCOMPLETE (a mandatory component missing) | DEGRADED (only a
declared ancillary component - regime PIT stamp, capture-status record - short) | COMPLETE.
`email_permission` is NORMAL only for COMPLETE; anything else is INCIDENT (the email still sends, with the
state unmissable - suppressing it would hide the incident).

STORE: screen_completion_receipts.json {run_key: receipt} - re-evaluating a run REPLACES its receipt
(idempotent); nothing is appended twice.

CLI:
  python3 screen_completion.py --group STOXX600 --run-date 2026-09-26 [--route local_primary]
                               [--workbook "<path>"] [--write]
  python3 screen_completion.py --show --group STOXX600 --run-date 2026-09-26
  python3 screen_completion.py --selftest
ROLLBACK (R4.13): build_email --allow-missing-receipt is NOT provided; rollback is the prior Trusted Build.
"""
from __future__ import annotations

import csv
import datetime as _dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = "screen_completion_receipts.json"
RECEIPT_VERSION = "1.0"
STATES = ("COMPLETE", "DEGRADED", "INCOMPLETE", "FAILED")
MANDATORY = ("workbook", "screen_history", "score_panel", "constituents", "fallback",
             "retrospective", "source_performance")
ANCILLARY = ("regime", "capture_status")
PRIMARY_ROUTES = ("local_primary",)
FALLBACK_ROUTES = ("composio_fallback", "constituent_hybrid")
# Workbook filename tokens per group (the workbook names drifted: 'Stoxx 600', 'Nasdaq', 'F250 & SPI').
GROUP_WORKBOOK_TOKENS = {
    "STOXX600": ("STOXX600", "Stoxx 600", "STOXX 600"), "NASDAQ": ("NASDAQ", "Nasdaq"),
    "SP500": ("SP500", "S&P 500"), "MIDCAP400": ("MIDCAP400", "MidCap400", "MIDCAP 400"),
    "F250-SPI": ("F250-SPI", "F250 & SPI", "F250 and SPI"),
}


def _iso(d: str) -> str:
    d = str(d).strip()
    if len(d) == 8 and d.isdigit():
        return "%s-%s-%s" % (d[:4], d[4:6], d[6:])
    return _dt.date.fromisoformat(d[:10]).isoformat()


def run_key(run_date: str, group: str) -> str:
    return "%s_%s" % (_iso(run_date).replace("-", ""), str(group).strip().upper())


def _rows(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _count(path, pred):
    if not os.path.exists(path):
        return None
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if pred(r):
                n += 1
    return n


def _comp(state, why, **kw):
    return dict({"state": state, "why": why}, **kw)


def _workbook(root, iso, group, workbook=None):
    if workbook:
        p = os.path.realpath(workbook)
        inside = os.path.commonpath([p, os.path.realpath(root)]) == os.path.realpath(root)
        if not inside:
            return _comp("MISSING", "workbook path is outside the canonical Investment Analysis folder "
                         "(a sandbox/outputs copy is not persistence)", path=workbook)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return _comp("OK", "canonical workbook present", path=os.path.relpath(p, root),
                         bytes=os.path.getsize(p))
        return _comp("MISSING", "named workbook absent from the canonical folder", path=workbook)
    dd = _dt.date.fromisoformat(iso).strftime("%d-%b-%y")
    toks = GROUP_WORKBOOK_TOKENS.get(group, (group,))
    hits = [p for p in glob.glob(os.path.join(root, "Growth Stock Analysis * W-e %s.xlsx" % dd))
            if any(t.lower() in os.path.basename(p).lower() for t in toks)]
    if hits:
        return _comp("OK", "canonical workbook present", path=os.path.basename(hits[0]),
                     bytes=os.path.getsize(hits[0]))
    return _comp("MISSING", "no canonical workbook 'Growth Stock Analysis <%s> W-e %s.xlsx'" % (group, dd))


def evaluate(group: str, run_date: str, *, root: str = None, route: str = None,
             workbook: str = None) -> dict:
    """Evaluate the completion receipt for one run from the CANONICAL stores. Never raises."""
    root = root or HERE
    group = str(group).strip().upper()
    iso = _iso(run_date)
    key = run_key(iso, group)
    comps = {}
    # core frame
    fd_path = os.path.join(root, "screen_history", "%s_%s_full_data.csv" % (iso, group))
    fd = _rows(fd_path)
    scored = len(fd) if fd else 0
    comps["screen_history"] = (_comp("OK", "retained frame present", rows=scored,
                                     path=os.path.relpath(fd_path, root))
                               if fd else _comp("MISSING", "no retained frame in screen_history/"))
    comps["workbook"] = _workbook(root, iso, group, workbook)
    # score panel
    sp = _count(os.path.join(root, "score_panel.csv"),
                lambda r: r.get("run_date", "")[:10] == iso and r.get("group", "").upper() == group)
    if sp is None:
        comps["score_panel"] = _comp("MISSING", "score_panel.csv absent")
    elif sp == 0:
        comps["score_panel"] = _comp("MISSING", "no score_panel rows for this run", rows=0)
    elif fd and sp != scored:
        comps["score_panel"] = _comp("MISMATCH", "score_panel rows %d != scored rows %d" % (sp, scored),
                                     rows=sp)
    else:
        comps["score_panel"] = _comp("OK", "score_panel appended", rows=sp)
    # constituents
    cons = _rows(os.path.join(root, "constituents_history.csv")) or []
    cons = [r for r in cons if r.get("run_date", "")[:10] == iso and r.get("group", "").upper() == group]
    comps["constituents"] = (_comp("OK", "PIT constituents captured", rows=len(cons)) if cons else
                             _comp("MISSING", "no constituents_history rows for this run"))
    # regime (ancillary)
    reg = [r for r in (_rows(os.path.join(root, "regime_history.csv")) or [])
           if r.get("run_date", "")[:10] == iso]
    if not reg:
        comps["regime"] = _comp("MISSING", "no regime_history row for this run date")
    else:
        sb = reg[-1].get("stamp_basis")
        comps["regime"] = _comp("OK" if sb == "live" else "NOT_PIT",
                                "regime stamp_basis=%s" % sb, stamp_basis=sb)
    # capture status (ancillary: it records only the LAST run)
    try:
        cs = json.load(open(os.path.join(root, "screen_capture_status.json"), encoding="utf-8"))
    except (OSError, ValueError):
        cs = None
    if cs and str(cs.get("run_date")) in (iso.replace("-", ""), iso) and str(cs.get("group", "")).upper() == group:
        comps["capture_status"] = _comp("OK" if cs.get("ok") else "FAILED",
                                        "capture %s (%s)" % (cs.get("action"), cs.get("reason")),
                                        columns=cs.get("columns"))
    else:
        comps["capture_status"] = _comp("NOT_THIS_RUN", "screen_capture_status.json records another run "
                                        "(it keeps only the last); the retained frame is the evidence")
    # source performance run ledger (ISA-0483)
    try:
        sys.path.insert(0, root)
        import source_performance_writer as _spw
        ent = _spw.run_logged(iso, group, path=os.path.join(root, "source_performance_log.json"))
    except Exception as exc:                                            # noqa: BLE001
        ent, _err = None, exc
    comps["source_performance"] = (_comp("OK", "runs[] entry present", basis=ent.get("basis"),
                                         run_mode=ent.get("run_mode")) if ent else
                                   _comp("MISSING", "source_performance_log.json runs[] has no entry "
                                         "for this run (ISA-0483)"))
    # route + fallback reconciliation
    rt = route or (ent or {}).get("run_mode") or "UNDECLARED"
    if rt in PRIMARY_ROUTES:
        comps["fallback"] = _comp("NOT_USED", "primary route")
    else:
        ok = (fd and comps["score_panel"]["state"] == "OK" and cons
              and comps["source_performance"]["state"] == "OK")
        comps["fallback"] = (_comp("RECONCILED", "every canonical store carries this run with agreeing counts",
                                   route=rt) if ok else
                             _comp("UNRECONCILED", "route %s: the canonical stores do not all carry this run "
                                   "(FALLBACK_WRITTEN not reconciled)" % rt, route=rt))
    # retrospective (register capture, ISA-0229/0755)
    try:
        import isa_retrospective_intake as _RI
        rf = _RI.run_findings(key)
    except Exception as exc:                                            # noqa: BLE001
        rf = {"state": "MISSING", "ledger": [], "why": "%s: %s" % (type(exc).__name__, exc)}
    comps["retrospective"] = _comp(
        "OK" if rf["state"] in ("CAPTURED", "NO_FINDINGS_CAPTURED") else "MISSING",
        rf["state"], capture_state=rf["state"], findings=len(rf.get("ledger") or []),
        promoted=sum(1 for x in rf.get("ledger") or [] if x.get("disposition") == "PROMOTED"),
        observations=sum(1 for x in rf.get("ledger") or [] if x.get("disposition") == "OBSERVATION"))
    # population waterfall
    pop = population(fd or [], cons)
    # overall
    missing = [c for c in MANDATORY if comps[c]["state"] not in ("OK", "NOT_USED", "RECONCILED")]
    anc = [c for c in ANCILLARY if comps[c]["state"] != "OK"]
    if not fd and comps["workbook"]["state"] != "OK":
        overall, why = "FAILED", "no retained frame and no workbook: the core screen did not complete"
    elif missing:
        overall, why = "INCOMPLETE", "mandatory component(s) not complete: " + ", ".join(missing)
    elif anc:
        overall, why = "DEGRADED", "core evidence persisted; ancillary short: " + ", ".join(anc)
    else:
        overall, why = "COMPLETE", "every mandatory and ancillary component complete"
    # The Trusted Build IN FORCE, read from the signed receipt file (not imported: release_gate would
    # pull the whole enforcement layer into the Composio fallback closure via build_email - ISA-0498).
    # This records which build produced the run; live-identity verification stays release_gate's job.
    try:
        rcpt = json.load(open(os.path.join(root, "Dashboard", "state", "trusted_build.json"), encoding="utf-8"))
        fp = rcpt.get("fingerprints") or {}
        build = {"build_id": rcpt.get("build_id"), "promoted_on": rcpt.get("promoted_on"),
                 "declared_rolls": {k: (v.get("roll") if isinstance(v, dict) else v) for k, v in fp.items()},
                 "basis": "declared by the Trusted receipt in force; not a live-identity verification"}
    except (OSError, ValueError) as exc:
        build = {"build_id": None, "why": "trusted_build.json unreadable: %s" % exc}
    return {"receipt_version": RECEIPT_VERSION, "run_key": key, "group": group, "run_date": iso,
            "route": rt, "trusted_build": build,
            "evaluated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "components": comps, "population": pop, "state": overall, "why": why,
            "missing_mandatory": missing, "ancillary_short": anc,
            "email_permission": "NORMAL" if overall == "COMPLETE" else "INCIDENT",
            "basis": "ISA-0755/0483: evaluated from canonical stores only; absence is never 'OK' (R2.10)"}


def population(frame_rows, cons_rows) -> dict:
    """The one arithmetic waterfall. RECONCILED only if every identity holds exactly."""
    by_status = {}
    for r in frame_rows:
        k = (r.get("final_status") or "UNKNOWN").strip() or "UNKNOWN"
        by_status[k] = by_status.get(k, 0) + 1
    rejected = {}
    for r in cons_rows:
        if str(r.get("scored")).lower() != "true":
            g = r.get("gate_code") or "UNSPECIFIED"
            rejected[g] = rejected.get(g, 0) + 1
    universe = len(cons_rows)
    scored = len(frame_rows)
    n_rej = sum(rejected.values())
    ids = {"universe == scored + gate_rejected": (universe == scored + n_rej) if cons_rows else None,
           "scored == sum(final_status)": scored == sum(by_status.values())}
    state = ("UNMEASURED" if not cons_rows or not frame_rows else
             "RECONCILED" if all(v for v in ids.values()) else "UNRECONCILED")
    return {"state": state, "universe": universe, "gate_rejected": n_rej,
            "gate_rejected_by_code": dict(sorted(rejected.items(), key=lambda x: -x[1])),
            "scored": scored, "scored_by_status": by_status, "identities": ids}


def load(root=None) -> dict:
    p = os.path.join(root or HERE, STORE)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def write(receipt: dict, root=None) -> str:
    root = root or HERE
    doc = load(root)
    doc[receipt["run_key"]] = receipt                       # idempotent: replace, never append
    p = os.path.join(root, STORE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


def get(group: str, run_date: str, root=None):
    return load(root).get(run_key(run_date, group))


# ───────────────────────────────────────────────────────────────────────── selftest
def _selftest(verbose: bool = True) -> int:
    import shutil, tempfile
    fails = []

    def ck(name, cond):
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    td = tempfile.mkdtemp(prefix="screen_completion_")
    os.makedirs(os.path.join(td, "screen_history"))
    os.makedirs(os.path.join(td, "Dashboard", "state"))
    old_store = os.environ.get("ISA_REGISTER_STORE")
    os.environ["ISA_REGISTER_STORE"] = os.path.join(td, "Dashboard", "state")
    try:
        # copy the register schema so the intake module can run hermetically
        import isa_register as _R
        src = os.path.join(HERE, "Dashboard", "state", _R.SCHEMA_FILE)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(td, "Dashboard", "state", _R.SCHEMA_FILE))
        _R._schema_cache.clear()
        import isa_retrospective_intake as _RI
        G, D = "STOXX600", "2026-09-26"

        def w(path, rows):
            with open(os.path.join(td, path), "w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)

        w("screen_history/%s_%s_full_data.csv" % (D, G),
          [{"ticker": "A", "final_status": "CANDIDATE_RANKABLE"},
           {"ticker": "B", "final_status": "MANDATORY_MINIMUM_FAIL"}])
        w("score_panel.csv", [{"run_date": D, "group": G, "ticker": "A"},
                              {"run_date": D, "group": G, "ticker": "B"}])
        w("constituents_history.csv", [
            {"run_date": D, "group": G, "ticker": "A", "scored": "True", "gate_code": "pass"},
            {"run_date": D, "group": G, "ticker": "B", "scored": "True", "gate_code": "pass"},
            {"run_date": D, "group": G, "ticker": "C", "scored": "False", "gate_code": "Gate 1"}])
        w("regime_history.csv", [{"run_date": D, "stamp_basis": "live"}])
        with open(os.path.join(td, "Growth Stock Analysis Stoxx 600 W-e 26-Sep-26.xlsx"), "wb") as fh:
            fh.write(b"xlsx")
        json.dump({"run_date": "20260926", "group": G, "ok": True, "action": "captured"},
                  open(os.path.join(td, "screen_capture_status.json"), "w"))
        json.dump({"indices": {}, "runs": []}, open(os.path.join(td, "source_performance_log.json"), "w"))

        # 1. the 26-Sep STOXX replay: no register capture, no source-performance append
        r = evaluate(G, D, root=td, route="local_primary")
        ck("STOXX 26-Sep replay: missing retrospective + source performance -> INCOMPLETE (not COMPLETE)",
           r["state"] == "INCOMPLETE" and "retrospective" in r["missing_mandatory"]
           and "source_performance" in r["missing_mandatory"])
        ck("NEGATIVE CONTROL: an INCOMPLETE receipt never grants a NORMAL email",
           r["email_permission"] == "INCIDENT")
        ck("the waterfall reconciles exactly (3 = 2 scored + 1 rejected)",
           r["population"]["state"] == "RECONCILED" and r["population"]["gate_rejected"] == 1)
        # 2. complete it through the REAL mechanisms
        _RI.record_no_findings(run_key(D, G), group=G)
        import source_performance_writer as _spw
        _spw_log = os.path.join(td, "source_performance_log.json")
        fdp = os.path.join(td, "screen_history", "%s_%s_full_data.csv" % (D, G))
        tmpf = os.path.join(td, "20260926_%s_full_data.csv" % G)
        shutil.copy(fdp, tmpf)
        ck("the ledger append is idempotent", _spw.append_run(tmpf, G, route="local_primary", path=_spw_log)["state"] == "APPENDED"
           and _spw.append_run(tmpf, G, path=_spw_log)["state"] == "ALREADY_LOGGED")
        r2 = evaluate(G, D, root=td, route="local_primary")
        ck("must-fire: every component present -> COMPLETE with NORMAL email permission",
           r2["state"] == "COMPLETE" and r2["email_permission"] == "NORMAL")
        # 3. the 25-Sep NASDAQ fallback shape: route fallback, nothing reconciled into canonical stores
        r3 = evaluate("NASDAQ", "2026-09-25", root=td, route="composio_fallback")
        ck("NASDAQ 25-Sep fallback replay: nothing canonical -> FAILED/INCOMPLETE, never COMPLETE",
           r3["state"] in ("FAILED", "INCOMPLETE") and r3["components"]["fallback"]["state"] == "UNRECONCILED")
        # 4. a fallback run whose rows DID reach the canonical stores is RECONCILED
        r4 = evaluate(G, D, root=td, route="composio_fallback")
        ck("a fallback run reconciled into canonical stores reads RECONCILED",
           r4["components"]["fallback"]["state"] == "RECONCILED" and r4["state"] == "COMPLETE")
        # 5. an outputs/ (sandbox) workbook is not persistence
        r5 = evaluate(G, D, root=td, route="local_primary", workbook="/tmp/outputs/x.xlsx")
        ck("NEGATIVE CONTROL: a workbook outside the canonical folder is MISSING",
           r5["components"]["workbook"]["state"] == "MISSING" and r5["state"] == "INCOMPLETE")
        # 6. a non-PIT regime row degrades, it does not fail
        w("regime_history.csv", [{"run_date": D, "stamp_basis": "live_stale_source"}])
        r6 = evaluate(G, D, root=td, route="local_primary")
        ck("an ancillary shortfall (regime not PIT) is DEGRADED, named, never COMPLETE",
           r6["state"] == "DEGRADED" and "regime" in r6["ancillary_short"])
        # 7. a panel/frame count disagreement is a MISMATCH, not OK
        w("score_panel.csv", [{"run_date": D, "group": G, "ticker": "A"}])
        r7 = evaluate(G, D, root=td, route="local_primary")
        ck("NEGATIVE CONTROL: score_panel rows != scored rows -> MISMATCH -> INCOMPLETE",
           r7["components"]["score_panel"]["state"] == "MISMATCH" and r7["state"] == "INCOMPLETE")
        # 8. the store is idempotent per run key
        write(r2, td); write(r7, td)
        ck("re-evaluating a run REPLACES its receipt (one key, latest state)",
           len(load(td)) == 1 and get(G, D, td)["state"] == "INCOMPLETE")
        # 9. an unreconciled waterfall is named
        pop = population([{"final_status": "X"}], [{"scored": "True"}, {"scored": "True"}])
        ck("NEGATIVE CONTROL: universe != scored + rejected -> UNRECONCILED", pop["state"] == "UNRECONCILED")
    finally:
        if old_store is None:
            os.environ.pop("ISA_REGISTER_STORE", None)
        else:
            os.environ["ISA_REGISTER_STORE"] = old_store
        try:
            import isa_register as _R2
            _R2._schema_cache.clear()
        except Exception:                                               # noqa: BLE001
            pass
        shutil.rmtree(td, ignore_errors=True)
    if verbose:
        print("screen_completion selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--group"); ap.add_argument("--run-date", dest="run_date")
    ap.add_argument("--route", default=None, help="local_primary | composio_fallback | constituent_hybrid")
    ap.add_argument("--workbook", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not (a.group and a.run_date):
        ap.error("--group and --run-date are required")
    if a.show:
        print(json.dumps(get(a.group, a.run_date), indent=1))
        return 0
    r = evaluate(a.group, a.run_date, route=a.route, workbook=a.workbook)
    if a.write:
        write(r)
    print("COMPLETION_RECEIPT state=%s run=%s route=%s email=%s missing=%s ancillary_short=%s population=%s"
          % (r["state"], r["run_key"], r["route"], r["email_permission"],
             ",".join(r["missing_mandatory"]) or "-", ",".join(r["ancillary_short"]) or "-",
             r["population"]["state"]))
    return 0 if r["state"] == "COMPLETE" else 2


if __name__ == "__main__":
    sys.exit(main())
