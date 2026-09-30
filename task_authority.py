"""
task_authority.py — ISA-0537: SCHEDULED-TASK RUN-SURFACE AUTHORITY (thin launcher + canonical task
contract + invocation receipt).

Authority: ChatGPT Astra 6 Audit/ISA_BuildSpec_ISA0537_RunSurfaceAuthority_27Sep2026.md (Raj-issued
27-Sep-2026), validated against TB-2026-09-27-02. Binding standard: ISA_Engineering_Rules.md (R4.4 one
home, R4.15 run surfaces, R14.1 nothing depends on someone remembering, R18.5 untrusted LIVE refuses).

WHY. Every Cowork scheduled task used to carry a FULL COPY of its workflow in its installed description.
That copy lives in the Cowork desktop task store, which no session and no check can read (ISA-0537,
re-measured 27-Sep-2026: the Scheduled folder exists but cannot be granted to a session). So every
run-surface check validated Skills_to_Edit/<task>/SKILL.md - a copy that did not run - while the copy
that fired was unobservable. Two homes for one rule, one of them invisible.

THE FIX. The installed description becomes a STABLE THIN LAUNCHER: task key + launcher protocol and
nothing else (no rule, threshold, stage, build id or hash - so it never needs editing after a build).
At invocation it runs `task_authority.py launch`, which:
    verifies LIVE against the current Trusted receipt (release_gate.verify_live)
 -> resolves the canonical task contract (Dashboard/state/task_contracts.json, SIGNED config)
 -> hashes the canonical workflow (SKILL) and Run_Context and compares them with the Trusted receipt
 -> checks the occurrence window (schedule authority = SCHEDULED_TASKS_SETUP.md, read, never copied)
 -> writes an append-only invocation receipt (Dashboard/state/task_invocations.jsonl)
 -> prints the ONE canonical workflow file the session must execute.
Skills_to_Edit/<task>/SKILL.md therefore stops being "a mirror that may or may not run" and becomes the
canonical LOADED source for every migrated task.

SAFE MIGRATION (BuildSpec §8, critique §2). The contract's `enforcement_state` is the AUTHORISED CEILING:
  LEGACY_OBSERVED  (initial, all tasks) - nothing refuses that did not refuse before;
  THIN_LAUNCHER_PROBATION is DERIVED, never stored: a LEGACY_OBSERVED task whose ledger holds a matching
                    launcher receipt (right key + protocol) is in probation - mismatches WARN, never block;
  ENFORCED         - set only by a SIGNED contract edit through release_gate, after retained receipt
                    evidence; a missing/mismatched launcher, untrusted LIVE or canonical-surface mismatch
                    then REFUSES before any capital logic.
Mutable decision/runtime data (receipts) never lives in the signed contract, so capturing a receipt never
forces a new Trusted build.

CLI (run from the Investment Analysis folder):
  python3 task_authority.py launch   --task-key K --protocol ISA-TL-1 [--dry-run]
  python3 task_authority.py observe  --run-context Run_Context_X.md [--task-key K]   (legacy hook; never blocks)
  python3 task_authority.py complete --receipt-id R --outcome COMPLETED|FAILED|NO_OP [--ref TEXT]
  python3 task_authority.py health   [--json]
  python3 task_authority.py launchers [--write]      (render the thin launchers; --write refreshes files)
  python3 task_authority.py --selftest
Learning paradigm: NONE (operational evidence only). Rollback: restore the previous Trusted build; the
ledger is append-only evidence and is never deleted.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                                            # noqa: D103
        return None

PROTOCOL = "ISA-TL-1"
CONTRACT_SCHEMA = "1.0.0"
RECEIPT_SCHEMA = "1.0.0"
STATE_REL = os.path.join("Dashboard", "state")
CONTRACTS_REL = os.path.join(STATE_REL, "task_contracts.json")
LEDGER_REL = os.path.join(STATE_REL, "task_invocations.jsonl")
LAUNCHER_FILE = "LAUNCHER.md"
TZ_NAME = "Europe/London"
ISA_WINDOWS_ROOT = (r"C:\Users\rjoba\OneDrive\Documents\OneDrive\Documents\Raj\Finance"
                    r"\Financial Planning\ISA\Investment Analysis")

# enforcement ceilings (stored) and the derived probation state
LEGACY_OBSERVED, PROBATION, ENFORCED = "LEGACY_OBSERVED", "THIN_LAUNCHER_PROBATION", "ENFORCED"
STORED_STATES = (LEGACY_OBSERVED, ENFORCED)
# capability decision states (BuildSpec §10)
LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
PROBATION_MATCH = "PROBATION_MATCH"
AUTHORITY_VERIFIED = "AUTHORITY_VERIFIED"
REFUSED_LAUNCHER_IDENTITY = "REFUSED_LAUNCHER_IDENTITY"
REFUSED_UNTRUSTED_LIVE = "REFUSED_UNTRUSTED_LIVE"
REFUSED_CANONICAL_SURFACE_MISMATCH = "REFUSED_CANONICAL_SURFACE_MISMATCH"
REFUSED_OUTSIDE_OCCURRENCE = "REFUSED_OUTSIDE_OCCURRENCE"
MISSED_EXPECTED_OCCURRENCE = "MISSED_EXPECTED_OCCURRENCE"
CAPABILITY_STATES = (LEGACY_UNVERIFIED, PROBATION_MATCH, AUTHORITY_VERIFIED, REFUSED_LAUNCHER_IDENTITY,
                     REFUSED_UNTRUSTED_LIVE, REFUSED_CANONICAL_SURFACE_MISMATCH,
                     REFUSED_OUTSIDE_OCCURRENCE, MISSED_EXPECTED_OCCURRENCE)
# operational verdicts the launcher acts on
PROCEED, NO_OP_OUTSIDE, NO_OP_DUPLICATE, REFUSED, OBSERVED = (
    "PROCEED", "NO_OP_OUTSIDE_OCCURRENCE", "NO_OP_DUPLICATE_OCCURRENCE", "REFUSED", "OBSERVED")
OUTCOMES = ("COMPLETED", "FAILED", "NO_OP")
SCHEDULE_ON_DEMAND = "ON_DEMAND"


class TaskAuthorityError(ValueError):
    """A contract/ledger breach. Raised, never swallowed into a pass (R4.3)."""


# ─────────────────────────────────────────────────────────────── time / io helpers

def _now(now=None) -> datetime.datetime:
    if now is not None:
        return now
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:                                                   # noqa: BLE001
        return datetime.datetime.now(datetime.timezone.utc)


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_text(t: str) -> str:
    return _sha_bytes(t.encode("utf-8"))


def _read_text(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────── contract store

def load_contracts(root: str = HERE) -> dict:
    """The canonical task contract store. RAISES when absent or malformed - a launcher cannot know
    what to run without it, and 'no contract' must never read as 'nothing to check' (R4.3)."""
    p = os.path.join(root, CONTRACTS_REL)
    txt = _read_text(p)
    if txt is None:
        raise TaskAuthorityError("CONTRACT_UNAVAILABLE: %s absent/unreadable" % CONTRACTS_REL)
    doc = json.loads(txt)
    errs = validate_contracts(doc, root)
    if errs:
        raise TaskAuthorityError("CONTRACT_INVALID: " + "; ".join(errs))
    return doc


_REQUIRED_TASK_FIELDS = ("capital_relevant", "canonical_workflow", "run_context", "schedule",
                         "completion_contract", "enforcement_state", "observation_start")


def validate_contracts(doc: dict, root: str = HERE) -> list:
    errs = []
    if not isinstance(doc, dict) or doc.get("schema_version") != CONTRACT_SCHEMA:
        return ["schema_version must be %s" % CONTRACT_SCHEMA]
    if doc.get("launcher_protocol") != PROTOCOL:
        errs.append("launcher_protocol %r != code protocol %s" % (doc.get("launcher_protocol"), PROTOCOL))
    tasks = doc.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        return errs + ["tasks empty"]
    for k, t in tasks.items():
        for f in _REQUIRED_TASK_FIELDS:
            if f not in t:
                errs.append("%s: missing %s" % (k, f))
        if t.get("enforcement_state") not in STORED_STATES:
            errs.append("%s: enforcement_state %r not in %s (THIN_LAUNCHER_PROBATION is derived, "
                        "never stored)" % (k, t.get("enforcement_state"), STORED_STATES))
        if t.get("enforcement_state") == ENFORCED and not t.get("enforced_on_evidence"):
            errs.append("%s: ENFORCED without enforced_on_evidence (a receipt id) - BuildSpec §16" % k)
        # business logic must never be copied into the contract (BuildSpec §5)
        for bad in ("rules", "thresholds", "steps", "workflow_text", "cron"):
            if bad in t:
                errs.append("%s: field %r duplicates workflow/schedule authority (R4.4)" % (k, bad))
    return errs


def task_contract(task_key: str, root: str = HERE, doc: dict = None) -> dict:
    doc = doc if doc is not None else load_contracts(root)
    return (doc.get("tasks") or {}).get(task_key)


# ─────────────────────────────────────────────────────────────── schedule (read, never copied)

_ORD = {"1st": (1, 7), "2nd": (8, 14), "3rd": (15, 21), "4th": (22, 28)}
_WD = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def schedule_rule(task_key: str, root: str = HERE, setup_text: str = None) -> dict:
    """The occurrence rule for a task, parsed from its ONE home (SCHEDULED_TASKS_SETUP.md, via
    consistency_check.declared_tasks). -> {kind: NTH|RELATIVE_SAT_BEFORE_FIRST_SUN|WEEKLY|UNDECLARED}."""
    if setup_text is None:
        setup_text = _read_text(os.path.join(root, "SCHEDULED_TASKS_SETUP.md")) or ""
    try:
        import consistency_check as _cc
        rows = _cc.declared_tasks(setup_text)
    except Exception as exc:                                            # noqa: BLE001
        return {"kind": "UNDECLARED", "why": "schedule authority unreadable (%s)" % exc}
    spec = rows.get(task_key)
    if not spec:
        return {"kind": "UNDECLARED", "why": "%s has no row in SCHEDULED_TASKS_SETUP.md" % task_key}
    if spec.get("relative"):
        return {"kind": "RELATIVE_SAT_BEFORE_FIRST_SUN", "row": spec["row"]}
    if spec.get("ordinal") and spec.get("weekday"):
        return {"kind": "NTH", "window": list(spec["ordinal"]), "weekday": spec["weekday"],
                "row": spec["row"]}
    m = re.search(r"\b(" + "|".join(_WD) + r")s\b", spec.get("row") or "")
    if m:
        return {"kind": "WEEKLY", "weekday": m.group(1), "row": spec["row"]}
    return {"kind": "UNDECLARED", "why": "row not parseable: %r" % spec.get("row")}


def occurrence(task: dict, task_key: str, day: datetime.date, root: str = HERE,
               setup_text: str = None) -> dict:
    """Is `day` an occurrence of this task? -> {in_window, occurrence_id, rule}."""
    if task.get("schedule") == SCHEDULE_ON_DEMAND:
        return {"in_window": True, "occurrence_id": "%s@%s" % (task_key, day.isoformat()),
                "rule": {"kind": SCHEDULE_ON_DEMAND}}
    rule = schedule_rule(task_key, root, setup_text)
    k = rule["kind"]
    wd = _WD[day.weekday()]
    if k == "NTH":
        lo, hi = rule["window"]
        ok = lo <= day.day <= hi and wd == rule["weekday"]
    elif k == "RELATIVE_SAT_BEFORE_FIRST_SUN":
        nxt = day + datetime.timedelta(days=1)
        ok = wd == "Saturday" and 1 <= nxt.day <= 7
    elif k == "WEEKLY":
        ok = wd == rule["weekday"]
    else:
        return {"in_window": None, "occurrence_id": None, "rule": rule}
    return {"in_window": ok, "occurrence_id": "%s@%s" % (task_key, day.isoformat()) if ok else None,
            "rule": rule}


def expected_occurrences(task: dict, task_key: str, start: datetime.date, end: datetime.date,
                         root: str = HERE, setup_text: str = None) -> list:
    if task.get("schedule") == SCHEDULE_ON_DEMAND:
        return []
    out, d = [], start
    while d <= end:
        o = occurrence(task, task_key, d, root, setup_text)
        if o["in_window"]:
            out.append(o["occurrence_id"])
        d += datetime.timedelta(days=1)
    return out


# ─────────────────────────────────────────────────────────────── ledger

def read_ledger(root: str = HERE) -> list:
    p = os.path.join(root, LEDGER_REL)
    out = []
    txt = _read_text(p)
    if not txt:
        return out
    for i, line in enumerate(txt.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"kind": "CORRUPT_LINE", "line": i})
    return out


def _append(rec: dict, root: str = HERE) -> None:
    p = os.path.join(root, LEDGER_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")


def effective_state(task_key: str, task: dict = None, ledger: list = None, root: str = HERE) -> str:
    """Stored ceiling + ledger evidence. PROBATION is DERIVED: never stored, so capturing a receipt
    never changes a signed file."""
    task = task if task is not None else (task_contract(task_key, root) or {})
    if task.get("enforcement_state") == ENFORCED:
        return ENFORCED
    ledger = ledger if ledger is not None else read_ledger(root)
    for r in ledger:
        if (r.get("kind") == "LAUNCH" and r.get("task_key") == task_key
                and r.get("launcher_protocol") == PROTOCOL and not r.get("dry_run")):
            return PROBATION
    return LEGACY_OBSERVED


def canonical_loaded_labels(root: str = HERE) -> set:
    """Task keys whose Skills_to_Edit/<task>/SKILL.md is PROVEN to be the executed workflow (a launcher
    receipt exists: PROBATION or ENFORCED). framework_atlas reports those surfaces as `canonical_loaded`
    instead of `mirror` - the mirror stops being ambiguous (BuildSpec §9). Never raises: an unreadable
    contract/ledger leaves every surface a mirror, the conservative reading."""
    try:
        doc = load_contracts(root)
        led = read_ledger(root)
        return {k for k, t in doc["tasks"].items() if effective_state(k, t, led) in (PROBATION, ENFORCED)}
    except Exception:                                                   # noqa: BLE001
        return set()


# ─────────────────────────────────────────────────────────────── trusted identity

def _trusted_identity(root: str = HERE) -> dict:
    try:
        import release_gate as rg
        v = rg.verify_live(root)
        rec = rg.load_receipt(root) or {}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNKNOWN", "why": "release_gate unavailable (%s)" % exc, "build_id": None,
                "digests": {}, "config_files": {}}
    fps = rec.get("fingerprints") or {}
    return {"state": v.get("state"), "why": v.get("why"), "build_id": v.get("build_id"),
            "digests": (fps.get("run_surfaces") or {}).get("digests") or {},
            "config_files": (fps.get("config") or {}).get("files") or {}}


def _label_of(rel: str) -> str:
    rel = rel.replace("\\", "/")
    base = os.path.basename(rel)
    return rel.split("/")[-2] if base == "SKILL.md" else os.path.splitext(base)[0]


def surface_identity(rel: str, root: str, trusted_digests: dict) -> dict:
    """Hash one canonical surface EXACTLY as release_gate.run_surface_fingerprint does (utf-8 text),
    and compare with the Trusted receipt."""
    txt = _read_text(os.path.join(root, rel)) if rel else None
    if txt is None:
        return {"path": rel, "state": "ABSENT", "sha256": None, "trusted_sha256": None}
    sha = _sha_text(txt)
    label = _label_of(rel)
    trusted = trusted_digests.get(label)
    state = "MATCH" if trusted == sha else ("UNSIGNED" if trusted is None else "MISMATCH")
    return {"path": rel, "label": label, "state": state, "sha256": sha, "trusted_sha256": trusted}


# ─────────────────────────────────────────────────────────────── launch / observe / complete

def _new_receipt_id(task_key: str, now: datetime.datetime) -> str:
    return "TIR-%s-%s-%s" % (now.strftime("%Y%m%dT%H%M%S"), (task_key or "unresolved")[:40],
                             uuid.uuid4().hex[:6])


def launch(task_key: str, protocol: str, *, root: str = HERE, now=None, dry_run: bool = False,
           trusted: dict = None, contracts: dict = None, ledger: list = None,
           setup_text: str = None, host: str = None) -> dict:
    """The thin launcher's one call. -> receipt dict with `verdict` (PROCEED | NO_OP_* | REFUSED),
    `capability_state`, `canonical_workflow` and a human `message`. Never raises for a task-level
    refusal: the refusal IS the answer and is written to the ledger (R2.10)."""
    _fi_mark("task_authority", "launch")
    now = _now(now)
    warnings, reasons = [], []
    rec = {"kind": "LAUNCH", "receipt_schema": RECEIPT_SCHEMA, "task_key": task_key,
           "launcher_protocol": protocol, "invoked_at": now.isoformat(timespec="seconds"),
           "timezone": str(now.tzinfo), "dry_run": bool(dry_run),
           "host": host or _host_identity()}
    rec["receipt_id"] = _new_receipt_id(task_key, now)
    # 1 contract
    try:
        doc = contracts if contracts is not None else load_contracts(root)
    except (TaskAuthorityError, ValueError) as exc:
        return _finish(rec, REFUSED, REFUSED_CANONICAL_SURFACE_MISMATCH, ["%s" % exc], warnings, root)
    rec["task_contract_schema_version"] = doc.get("schema_version")
    rec["task_contract_sha256"] = _sha_text(json.dumps(doc, sort_keys=True))
    task = (doc.get("tasks") or {}).get(task_key)
    if task is None:
        return _finish(rec, REFUSED, REFUSED_LAUNCHER_IDENTITY,
                       ["unknown task_key %r - no canonical contract (a launcher with a wrong key "
                        "cannot know what to run)" % task_key], warnings, root)
    ledger = ledger if ledger is not None else read_ledger(root)
    stored = task["enforcement_state"]
    enforced = stored == ENFORCED
    rec["enforcement_state"] = ENFORCED if enforced else (
        PROBATION if protocol == PROTOCOL else LEGACY_OBSERVED)
    rec["canonical_workflow"] = task["canonical_workflow"]
    rec["canonical_workflow_windows"] = ISA_WINDOWS_ROOT + "\\" + task["canonical_workflow"].replace("/", "\\")
    # 2 protocol
    if protocol != PROTOCOL:
        msg = "launcher protocol %r != %s (stale or edited launcher)" % (protocol, PROTOCOL)
        if enforced:
            return _finish(rec, REFUSED, REFUSED_LAUNCHER_IDENTITY, [msg], warnings, root)
        warnings.append(msg)
    # 3 trusted LIVE
    trusted = trusted if trusted is not None else _trusted_identity(root)
    rec["trusted_build_id"] = trusted.get("build_id")
    rec["trusted_state"] = trusted.get("state")
    if trusted.get("state") != "TRUSTED":
        msg = "LIVE is %s (%s) - R18.5" % (trusted.get("state"), trusted.get("why"))
        if enforced:
            return _finish(rec, REFUSED, REFUSED_UNTRUSTED_LIVE, [msg], warnings, root)
        warnings.append(msg)
    # 4 canonical surfaces
    digests = trusted.get("digests") or {}
    wf = surface_identity(task["canonical_workflow"], root, digests)
    rc = surface_identity(task["run_context"], root, digests) if task.get("run_context") else \
        {"path": None, "state": "NOT_DECLARED"}
    rec["canonical_workflow_identity"] = wf
    rec["run_context_identity"] = rc
    if wf["state"] == "ABSENT" or rc["state"] == "ABSENT":
        return _finish(rec, REFUSED, REFUSED_CANONICAL_SURFACE_MISMATCH,
                       ["canonical surface absent/unreadable: %s" % (
                           wf["path"] if wf["state"] == "ABSENT" else rc["path"])], warnings, root)
    bad = [s for s in (wf, rc) if s["state"] in ("MISMATCH", "UNSIGNED")]
    if bad:
        msg = "canonical surface differs from Trusted %s: %s" % (
            trusted.get("build_id"), ", ".join("%s=%s" % (s["path"], s["state"]) for s in bad))
        if enforced:
            return _finish(rec, REFUSED, REFUSED_CANONICAL_SURFACE_MISMATCH, [msg], warnings, root)
        warnings.append(msg)
    # 5 occurrence
    occ = occurrence(task, task_key, now.date(), root, setup_text)
    rec["occurrence_id"] = occ["occurrence_id"]
    rec["occurrence_rule"] = occ["rule"]
    if occ["in_window"] is None:
        msg = "occurrence rule UNDECLARED (%s)" % occ["rule"].get("why")
        if enforced:
            return _finish(rec, REFUSED, REFUSED_OUTSIDE_OCCURRENCE, [msg], warnings, root)
        warnings.append(msg)
    elif not occ["in_window"]:
        return _finish(rec, NO_OP_OUTSIDE, REFUSED_OUTSIDE_OCCURRENCE,
                       ["[%s] %s is not an occurrence of this task (%s) - no-op" % (
                           task_key, now.date().isoformat(), occ["rule"].get("row") or occ["rule"]["kind"])],
                       warnings, root)
    # 6 duplicate occurrence (idempotent)
    if occ.get("occurrence_id") and task.get("schedule") != SCHEDULE_ON_DEMAND:
        prior = [r for r in ledger if r.get("kind") == "LAUNCH" and r.get("verdict") == PROCEED
                 and r.get("occurrence_id") == occ["occurrence_id"] and not r.get("dry_run")]
        done = {r.get("receipt_id") for r in ledger
                if r.get("kind") == "COMPLETE" and r.get("outcome") == "COMPLETED"}
        if any(p.get("receipt_id") in done for p in prior):
            return _finish(rec, NO_OP_DUPLICATE, rec["enforcement_state"] == ENFORCED and
                           AUTHORITY_VERIFIED or PROBATION_MATCH,
                           ["occurrence %s already COMPLETED under %s - duplicate firing is a no-op"
                            % (occ["occurrence_id"], [p["receipt_id"] for p in prior])], warnings, root)
        if prior:
            rec["prior_incomplete_receipts"] = [p["receipt_id"] for p in prior]
            warnings.append("re-launch of %s after an incomplete run %s" % (
                occ["occurrence_id"], rec["prior_incomplete_receipts"]))
    cap = (AUTHORITY_VERIFIED if enforced else
           PROBATION_MATCH if protocol == PROTOCOL else LEGACY_UNVERIFIED)
    return _finish(rec, PROCEED, cap, [], warnings, root)


def _finish(rec, verdict, cap_state, reasons, warnings, root):
    rec["verdict"] = verdict
    rec["capability_state"] = cap_state
    rec["refusal_reasons"] = reasons if verdict == REFUSED else []
    rec["notes"] = reasons if verdict != REFUSED else []
    rec["warnings"] = warnings
    rec["preflight_verdict"] = verdict
    if verdict == PROCEED:
        rec["message"] = ("PROCEED: open %s and execute it IN FULL as this task's instructions. "
                          "Quote receipt %s in the run output; at the end run `python3 task_authority.py "
                          "complete --receipt-id %s --outcome COMPLETED` (or FAILED/NO_OP)."
                          % (rec.get("canonical_workflow_windows") or rec.get("canonical_workflow"),
                             rec["receipt_id"], rec["receipt_id"]))
    elif verdict == REFUSED:
        rec["message"] = "REFUSED (%s): %s. Do nothing else; end the run." % (cap_state, "; ".join(reasons))
    else:
        rec["message"] = "%s: %s. End the run." % (verdict, "; ".join(reasons))
    if not rec.get("dry_run"):
        _append(rec, root)
    return rec


def observe(run_context: str = None, task_key: str = None, *, root: str = HERE, now=None,
            contracts: dict = None, ledger: list = None, setup_text: str = None) -> dict:
    """LEGACY HOOK (called from the top of each Run_Context). A legacy installed task that still reads
    its Run_Context gets an invocation receipt WITHOUT any change to the installed text. NEVER refuses
    and never raises: it can only add evidence (BuildSpec §13 critical containment)."""
    now = _now(now)
    rec = {"kind": "OBSERVE", "receipt_schema": RECEIPT_SCHEMA, "via": "run_context_legacy_hook",
           "run_context": run_context, "declared_task_key": task_key,
           "invoked_at": now.isoformat(timespec="seconds"), "timezone": str(now.tzinfo),
           "launcher_protocol": None, "host": _host_identity()}
    rec["receipt_id"] = _new_receipt_id(task_key or "observe", now)
    try:
        doc = contracts if contracts is not None else load_contracts(root)
        tasks = doc.get("tasks") or {}
        cands = [k for k, t in tasks.items() if (not run_context or t.get("run_context") == run_context)]
        inwin = [k for k in cands
                 if occurrence(tasks[k], k, now.date(), root, setup_text)["in_window"]]
        if task_key:
            resolved = task_key if task_key in tasks else None
            rec["resolution"] = "DECLARED" if resolved else "UNKNOWN_TASK_KEY"
            if resolved and inwin and resolved not in inwin:
                rec["resolution"] = "DECLARED_CONFLICTS_WITH_SCHEDULE"
        elif len(inwin) == 1:
            resolved, rec["resolution"] = inwin[0], "INFERRED_FROM_SCHEDULE"
        elif len(inwin) > 1:
            resolved, rec["resolution"] = None, "AMBIGUOUS"
            rec["candidates"] = inwin
        else:
            on_demand = [k for k in cands if tasks[k].get("schedule") == SCHEDULE_ON_DEMAND]
            resolved = on_demand[0] if len(on_demand) == 1 else None
            rec["resolution"] = "ON_DEMAND" if resolved else "OUTSIDE_EVERY_WINDOW"
        rec["task_key"] = resolved
        if resolved:
            o = occurrence(tasks[resolved], resolved, now.date(), root, setup_text)
            rec["occurrence_id"] = o["occurrence_id"]
            led = ledger if ledger is not None else read_ledger(root)
            if any(r.get("kind") == "LAUNCH" and r.get("occurrence_id") == o["occurrence_id"]
                   and r.get("verdict") == PROCEED for r in led if o["occurrence_id"]):
                rec["verdict"] = "OBSERVED_VIA_LAUNCHER"
                return rec                                   # already receipted by the launcher
            rec["enforcement_state"] = effective_state(resolved, tasks[resolved], led)
            trusted = _trusted_identity(root)
            rec["trusted_build_id"], rec["trusted_state"] = trusted.get("build_id"), trusted.get("state")
            rec["run_context_identity"] = surface_identity(tasks[resolved].get("run_context"), root,
                                                           trusted.get("digests") or {})
        rec["capability_state"] = LEGACY_UNVERIFIED
        rec["verdict"] = OBSERVED
        rec["message"] = ("OBSERVED (legacy hook, receipt %s): the installed task text is still "
                          "unverified (ISA-0537). Continue the run normally." % rec["receipt_id"])
        _append(rec, root)
    except Exception as exc:                                            # noqa: BLE001
        rec["verdict"] = "OBSERVE_FAILED"
        rec["message"] = "observe failed (%s) - continue the run normally; this hook never blocks" % exc
    return rec


def complete(receipt_id: str, outcome: str, ref: str = None, *, root: str = HERE, now=None) -> dict:
    """Terminal record for a PROCEED receipt. RAISES on an unknown receipt or outcome (R4.7)."""
    if outcome not in OUTCOMES:
        raise TaskAuthorityError("outcome %r not in %s" % (outcome, OUTCOMES))
    led = read_ledger(root)
    if not any(r.get("receipt_id") == receipt_id and r.get("kind") in ("LAUNCH", "OBSERVE") for r in led):
        raise TaskAuthorityError("no invocation receipt %r in %s" % (receipt_id, LEDGER_REL))
    now = _now(now)
    rec = {"kind": "COMPLETE", "receipt_schema": RECEIPT_SCHEMA, "receipt_id": receipt_id,
           "outcome": outcome, "completion_ref": ref,
           "completed_at": now.isoformat(timespec="seconds")}
    _append(rec, root)
    return rec


def _host_identity() -> dict:
    return {"platform": sys.platform, "python": sys.version.split()[0],
            "root_basename": os.path.basename(HERE)}


# ─────────────────────────────────────────────────────────────── health (missed occurrences)

def health(root: str = HERE, now=None, contracts: dict = None, ledger: list = None,
           setup_text: str = None, grace_days: int = 1) -> dict:
    """Per task: effective state, expected occurrences since observation_start (minus a grace day),
    which have a receipt, and which are MISSED. A task that never fired cannot report itself - this is
    the downstream detector (BuildSpec §8). Missing contracts -> state UNKNOWN, never clean."""
    now = _now(now)
    try:
        doc = contracts if contracts is not None else load_contracts(root)
    except (TaskAuthorityError, ValueError) as exc:
        return {"state": "UNKNOWN", "why": str(exc), "tasks": {}}
    led = ledger if ledger is not None else read_ledger(root)
    seen = {r.get("occurrence_id") for r in led if r.get("kind") in ("LAUNCH", "OBSERVE")
            and not r.get("dry_run") and r.get("occurrence_id")}
    end = now.date() - datetime.timedelta(days=grace_days)
    out = {}
    for k, t in sorted((doc.get("tasks") or {}).items()):
        eff = effective_state(k, t, led)
        try:
            start = datetime.date.fromisoformat(t["observation_start"])
        except Exception:                                               # noqa: BLE001
            start = end + datetime.timedelta(days=1)
        exp = expected_occurrences(t, k, start, end, root, setup_text) if start <= end else []
        missed = [o for o in exp if o not in seen]
        if eff == ENFORCED and missed:
            st = MISSED_EXPECTED_OCCURRENCE
        elif missed and eff == PROBATION:
            st = MISSED_EXPECTED_OCCURRENCE
        elif missed:
            st = "UNOBSERVED_OCCURRENCE"      # legacy: missed OR ran without the hook - not separable
        else:
            st = {ENFORCED: AUTHORITY_VERIFIED, PROBATION: PROBATION_MATCH}.get(eff, LEGACY_UNVERIFIED)
        out[k] = {"effective_state": eff, "capital_relevant": bool(t.get("capital_relevant")),
                  "expected": exp, "missed": missed, "state": st}
    n_enf_missed = sum(1 for v in out.values() if v["effective_state"] == ENFORCED and v["missed"])
    return {"state": "RED" if n_enf_missed else "OK", "as_of": now.isoformat(timespec="seconds"),
            "tasks": out,
            "n_legacy": sum(1 for v in out.values() if v["effective_state"] == LEGACY_OBSERVED),
            "n_probation": sum(1 for v in out.values() if v["effective_state"] == PROBATION),
            "n_enforced": sum(1 for v in out.values() if v["effective_state"] == ENFORCED)}


# ─────────────────────────────────────────────────────────────── thin launcher (the ONE template)

LAUNCHER_TEMPLATE = """THIN LAUNCHER - ISA scheduled task `{task_key}` - launcher protocol {protocol}.
This text deliberately carries only a task key and a launcher protocol. Everything else lives in the ISA framework and is loaded at run time - do not add anything to it.

1. Using the shell on Raj's computer, in the connected ISA folder `Investment Analysis`, run exactly:
   python3 task_authority.py launch --task-key {task_key} --protocol {protocol}
2. Read the JSON it prints.
   - verdict PROCEED: open the file named in `canonical_workflow` and execute it IN FULL as this task's instructions, from its first step. Quote `receipt_id` in the run's output. When the run ends, run: python3 task_authority.py complete --receipt-id <receipt_id> --outcome COMPLETED (use FAILED or NO_OP if that is what happened).
   - verdict NO_OP_OUTSIDE_OCCURRENCE or NO_OP_DUPLICATE_OCCURRENCE: print `message` and end the run.
   - verdict REFUSED: print `message` (it names the reason) and end the run. Do nothing else.
3. If the command cannot be run at all, end the run and report LAUNCHER_UNAVAILABLE. Never fall back to a remembered or older copy of these instructions.
"""


def render_launcher(task_key: str, protocol: str = PROTOCOL) -> str:
    return LAUNCHER_TEMPLATE.format(task_key=task_key, protocol=protocol)


# single-home lint: what a launcher must NEVER carry (BuildSpec §15 test 1)
_LINT = (
    (r"TB-\d{4}-\d{2}-\d{2}", "a Trusted Build id (forces a manual edit every build)"),
    (r"\b[0-9a-f]{12,}\b", "a hash literal"),
    (r"\d+(\.\d+)?\s*%|£|GBP|\bUSD\b", "a percentage or money amount (business rule)"),
    (r"\b(ACS|QMS|E\[r\]|ISA-\d{3,4}|Step\s*\d|threshold|floor|cap\b|buy|sell|trim|top-?up)\b",
     "workflow/business vocabulary"),
    (r"\b\d{1,2}\s*[-\u2013]\s*\d{1,2}\b|\b(1st|2nd|3rd|4th)\b|\b(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day",
     "schedule/occurrence rule (schedule authority is SCHEDULED_TASKS_SETUP.md)"),
    (r"Run_Context|SKILL\.md|\.xlsx|\.pdf", "a canonical file path (resolved at run time by the contract)"),
)


def launcher_lint(text: str, task_key: str) -> list:
    """[] when the launcher is a pure launcher. The task key and protocol are removed first."""
    body = text.replace(task_key, "<TASK>").replace(PROTOCOL, "<PROTO>")
    out = []
    for pat, why in _LINT:
        m = re.search(pat, body, re.I)
        if m:
            out.append("%s: launcher carries %s (%r)" % (task_key, why, m.group(0)))
    n_lines = len([ln for ln in text.splitlines() if ln.strip()])
    if n_lines > 12:
        out.append("%s: launcher has %d non-blank lines (> 12) - substantive prose is creeping in"
                   % (task_key, n_lines))
    if text.count("--task-key %s --protocol %s" % (task_key, PROTOCOL)) != 1:
        out.append("%s: launcher must invoke the launch command with its own key+protocol exactly once"
                   % task_key)
    return out


def launcher_path(task_key: str, root: str = HERE) -> str:
    return os.path.join(root, "Skills_to_Edit", task_key, LAUNCHER_FILE)


def write_launchers(root: str = HERE, doc: dict = None) -> list:
    doc = doc if doc is not None else load_contracts(root)
    out = []
    for k in sorted(doc["tasks"]):
        p = launcher_path(k, root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_launcher(k))
        out.append(p)
    return out


# ─────────────────────────────────────────────────────────────── integrity (consumed by consistency_check)

def integrity_findings(root: str = HERE, setup_text: str = None, doc: dict = None) -> list:
    """ERROR-grade contract findings: every SETUP-declared task has a contract; every contract names
    existing canonical surfaces and a parseable schedule; every generated LAUNCHER.md equals the
    generator; every rendered launcher passes the single-home lint."""
    errs = []
    try:
        doc = doc if doc is not None else load_contracts(root)
    except (TaskAuthorityError, ValueError) as exc:
        return ["ISA-0537: %s" % exc]
    tasks = doc["tasks"]
    if setup_text is None:
        setup_text = _read_text(os.path.join(root, "SCHEDULED_TASKS_SETUP.md")) or ""
    try:
        import consistency_check as _cc
        declared = set(_cc.declared_tasks(setup_text))
    except Exception as exc:                                            # noqa: BLE001
        return ["ISA-0537: schedule authority unreadable (%s) - UNMEASURED, not clean" % exc]
    for k in sorted(declared - set(tasks)):
        errs.append("ISA-0537: %s is declared in SCHEDULED_TASKS_SETUP.md but has no task contract" % k)
    for k, t in sorted(tasks.items()):
        if t.get("schedule") != SCHEDULE_ON_DEMAND and k not in declared:
            errs.append("ISA-0537: contract %s claims a schedule but SCHEDULED_TASKS_SETUP.md declares none" % k)
        for f in ("canonical_workflow", "run_context"):
            if t.get(f) and not os.path.isfile(os.path.join(root, t[f])):
                errs.append("ISA-0537: %s.%s -> %s does not exist" % (k, f, t[f]))
        if t.get("schedule") != SCHEDULE_ON_DEMAND and schedule_rule(k, root, setup_text)["kind"] == "UNDECLARED":
            errs.append("ISA-0537: %s occurrence rule UNDECLARED" % k)
        errs += ["ISA-0537: " + x for x in launcher_lint(render_launcher(k), k)]
        lp = launcher_path(k, root)
        on_disk = _read_text(lp)
        if on_disk is None:
            errs.append("ISA-0537: %s has no generated %s (run task_authority.py launchers --write)" % (k, LAUNCHER_FILE))
        elif on_disk != render_launcher(k):
            errs.append("ISA-0537: %s/%s differs from the generator - the launcher has ONE home "
                        "(task_authority.LAUNCHER_TEMPLATE); regenerate, never hand-edit" % (k, LAUNCHER_FILE))
    return errs


# ─────────────────────────────────────────────────────────────── capability checks (typed evidence)

def check_scheduled_task_authority_states() -> bool:
    """CAP-scheduled_task_authority must-fire through the production call (task_authority.launch):
    ENFORCED + matching identities -> AUTHORITY_VERIFIED/PROCEED; wrong protocol -> REFUSED_LAUNCHER_IDENTITY;
    untrusted LIVE -> REFUSED_UNTRUSTED_LIVE; canonical hash mismatch -> REFUSED_CANONICAL_SURFACE_MISMATCH;
    out of window -> REFUSED_OUTSIDE_OCCURRENCE; legacy protocol-less -> LEGACY_UNVERIFIED (never blocks).
    RAISES AssertionError on any miss (a check that cannot fail is not evidence)."""
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="ta_cap_")
    try:
        wf = "Skills_to_Edit/isa-nasdaq-fri2/SKILL.md"
        rc = "Run_Context_ISA_Growth_Stock_Analysis.md"
        for rel, txt in ((wf, "wf"), (rc, "rc")):
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)) or tmp, exist_ok=True)
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
                fh.write(txt)
        setup = "| isa-nasdaq-fri2 | NASDAQ | x | `0 9 8-14 * 5` | 2nd Friday |\n"
        task = {"capital_relevant": True, "canonical_workflow": wf, "run_context": rc, "schedule": "SETUP",
                "completion_contract": "x", "enforcement_state": ENFORCED, "enforced_on_evidence": "TIR-x",
                "observation_start": "2026-09-28"}
        doc = {"schema_version": CONTRACT_SCHEMA, "launcher_protocol": PROTOCOL,
               "tasks": {"isa-nasdaq-fri2": task}}
        tr = {"state": "TRUSTED", "build_id": "TB-X", "digests": {"isa-nasdaq-fri2": _sha_text("wf"),
                                                                  _label_of(rc): _sha_text("rc")}}
        day, off = datetime.datetime(2026, 10, 9, 9), datetime.datetime(2026, 10, 2, 9)
        kw = dict(root=tmp, contracts=doc, setup_text=setup, dry_run=True, ledger=[])
        got = {
            AUTHORITY_VERIFIED: launch("isa-nasdaq-fri2", PROTOCOL, now=day, trusted=tr, **kw),
            REFUSED_LAUNCHER_IDENTITY: launch("isa-nasdaq-fri2", "ISA-TL-0", now=day, trusted=tr, **kw),
            REFUSED_UNTRUSTED_LIVE: launch("isa-nasdaq-fri2", PROTOCOL, now=day,
                                           trusted=dict(tr, state="UNTRUSTED_LIVE_STATE"), **kw),
            REFUSED_CANONICAL_SURFACE_MISMATCH: launch("isa-nasdaq-fri2", PROTOCOL, now=day, trusted=dict(
                tr, digests=dict(tr["digests"], **{"isa-nasdaq-fri2": "0" * 64})), **kw),
            REFUSED_OUTSIDE_OCCURRENCE: launch("isa-nasdaq-fri2", PROTOCOL, now=off, trusted=tr, **kw),
        }
        for st, r in got.items():
            assert r["capability_state"] == st, (st, r["capability_state"], r.get("refusal_reasons"))
        assert got[AUTHORITY_VERIFIED]["verdict"] == PROCEED
        assert all(got[s]["verdict"] == REFUSED for s in (REFUSED_LAUNCHER_IDENTITY, REFUSED_UNTRUSTED_LIVE,
                                                          REFUSED_CANONICAL_SURFACE_MISMATCH))
        doc["tasks"]["isa-nasdaq-fri2"] = dict(task, enforcement_state=LEGACY_OBSERVED)
        leg = launch("isa-nasdaq-fri2", "ISA-TL-0", now=day, trusted=dict(tr, state="UNTRUSTED_LIVE_STATE"), **kw)
        assert leg["verdict"] == PROCEED and leg["capability_state"] == LEGACY_UNVERIFIED, leg
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────────────────────── selftest

def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    n = [0]

    def ok(cond, msg):
        assert cond, msg
        n[0] += 1

    setup = ("| Task ID | Group | Indices | Cron | Schedule |\n|---|---|---|---|---|\n"
             "| isa-nasdaq-fri2 | NASDAQ | x | `0 9 8-14 * 5` | 2nd Friday |\n"
             "| isa-monthly-prerun | Monthly pre-run | `30 9 1-7 * 6` | Saturday before the 1st Sunday, 09:30 | yes |\n"
             "| isa-weekly-eps-snapshot | Silent eps | `0 8 * * 1` | Mondays 08:00 | yes |\n")
    tmp = tempfile.mkdtemp(prefix="ta_selftest_")
    try:
        os.makedirs(os.path.join(tmp, STATE_REL))
        for rel, txt in (("Skills_to_Edit/isa-nasdaq-fri2/SKILL.md", "nasdaq workflow"),
                         ("Skills_to_Edit/isa-monthly-prerun/SKILL.md", "prerun workflow"),
                         ("Skills_to_Edit/isa-weekly-eps-snapshot/SKILL.md", "eps workflow"),
                         ("Skills_to_Edit/intramonth-stock-review/SKILL.md", "intramonth workflow"),
                         ("Run_Context_ISA_Growth_Stock_Analysis.md", "growth ctx"),
                         ("Run_Context_Monthly_ISA_Review.md", "monthly ctx"),
                         ("Run_Context_Intramonth_Stock_Review.md", "intra ctx")):
            p = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
        with open(os.path.join(tmp, "SCHEDULED_TASKS_SETUP.md"), "w", encoding="utf-8") as fh:
            fh.write(setup)

        def T(wf, rc, sched="SETUP", state=LEGACY_OBSERVED, **kw):
            d = {"capital_relevant": True, "canonical_workflow": wf, "run_context": rc,
                 "schedule": sched, "completion_contract": "fixture", "enforcement_state": state,
                 "observation_start": "2026-09-27"}
            d.update(kw)
            return d
        doc = {"schema_version": CONTRACT_SCHEMA, "launcher_protocol": PROTOCOL, "tasks": {
            "isa-nasdaq-fri2": T("Skills_to_Edit/isa-nasdaq-fri2/SKILL.md", "Run_Context_ISA_Growth_Stock_Analysis.md"),
            "isa-monthly-prerun": T("Skills_to_Edit/isa-monthly-prerun/SKILL.md", "Run_Context_Monthly_ISA_Review.md"),
            "isa-weekly-eps-snapshot": T("Skills_to_Edit/isa-weekly-eps-snapshot/SKILL.md", None, capital_relevant=False),
            "intramonth-stock-review": T("Skills_to_Edit/intramonth-stock-review/SKILL.md",
                                         "Run_Context_Intramonth_Stock_Review.md", sched=SCHEDULE_ON_DEMAND)}}
        with open(os.path.join(tmp, CONTRACTS_REL), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

        def digests():
            return {_label_of(r): _sha_text(_read_text(os.path.join(tmp, r))) for r in (
                "Skills_to_Edit/isa-nasdaq-fri2/SKILL.md", "Skills_to_Edit/isa-monthly-prerun/SKILL.md",
                "Skills_to_Edit/isa-weekly-eps-snapshot/SKILL.md", "Skills_to_Edit/intramonth-stock-review/SKILL.md",
                "Run_Context_ISA_Growth_Stock_Analysis.md", "Run_Context_Monthly_ISA_Review.md",
                "Run_Context_Intramonth_Stock_Review.md")}
        TR = {"state": "TRUSTED", "build_id": "TB-FIXTURE-1", "why": "fixture", "digests": digests()}
        UNTR = dict(TR, state="UNTRUSTED_LIVE_STATE")
        fri2 = datetime.datetime(2026, 10, 9, 9, 0)          # 2nd Friday of Oct-2026
        wrongfri = datetime.datetime(2026, 10, 2, 9, 0)      # 1st Friday

        # schedule parsing (from the ONE home)
        ok(schedule_rule("isa-nasdaq-fri2", tmp)["kind"] == "NTH", "S1 NTH parse")
        ok(schedule_rule("isa-monthly-prerun", tmp)["kind"] == "RELATIVE_SAT_BEFORE_FIRST_SUN", "S2 relative parse")
        ok(schedule_rule("isa-weekly-eps-snapshot", tmp)["kind"] == "WEEKLY", "S3 weekly parse")
        ok(occurrence(doc["tasks"]["isa-monthly-prerun"], "isa-monthly-prerun", datetime.date(2026, 10, 3), tmp)["in_window"],
           "S4 Sat 03-Oct-2026 precedes Sun 04-Oct")
        ok(occurrence(doc["tasks"]["isa-monthly-prerun"], "isa-monthly-prerun", datetime.date(2026, 10, 31), tmp)["in_window"],
           "S5 Sat 31-Oct-2026 precedes Sun 01-Nov (month-crossing edge)")
        ok(not occurrence(doc["tasks"]["isa-monthly-prerun"], "isa-monthly-prerun", datetime.date(2026, 10, 10), tmp)["in_window"],
           "S6 Sat 10-Oct not an occurrence")

        # T1 single-home lint + NEGATIVE CONTROLS
        for k in doc["tasks"]:
            ok(launcher_lint(render_launcher(k), k) == [], "T1 generated launcher is lint-clean: %s" % k)
        for inj in ("Only proceed if ACS >= 75.", "Build TB-2026-09-27-02 is current.",
                    "Run on the 2nd Friday (8-14).", "Read Run_Context_VCI_Task.md first.",
                    "Deploy 0.75% of the ISA.", "sha 0123456789abcdef0123"):
            ok(launcher_lint(render_launcher("isa-nasdaq-fri2") + inj + "\n", "isa-nasdaq-fri2") != [],
               "T1-NC lint must FIRE on injected business prose: %r" % inj)

        # T2 legacy compatibility: observe never refuses, records LEGACY_UNVERIFIED
        o = observe("Run_Context_ISA_Growth_Stock_Analysis.md", root=tmp, now=fri2, setup_text=setup)
        ok(o["verdict"] == OBSERVED and o["task_key"] == "isa-nasdaq-fri2"
           and o["resolution"] == "INFERRED_FROM_SCHEDULE" and o["capability_state"] == LEGACY_UNVERIFIED,
           "T2 legacy observe resolves by schedule and never blocks: %s" % o)
        o2 = observe("Run_Context_Monthly_ISA_Review.md", root=tmp, now=datetime.datetime(2026, 10, 4, 9),
                     setup_text=setup)
        ok(o2["verdict"] == OBSERVED and o2["resolution"] == "OUTSIDE_EVERY_WINDOW",
           "T2b a Sunday review reading the monthly Run_Context outside the pre-run window is recorded, not refused")
        ok(effective_state("isa-nasdaq-fri2", root=tmp) == LEGACY_OBSERVED,
           "T2c an OBSERVE receipt never promotes a task to probation (the launcher is not installed)")

        # T3 correct thin launcher -> receipt + PROCEED + probation (derived)
        r = launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TR, setup_text=setup)
        ok(r["verdict"] == PROCEED and r["capability_state"] == PROBATION_MATCH
           and r["canonical_workflow"] == "Skills_to_Edit/isa-nasdaq-fri2/SKILL.md"
           and r["trusted_build_id"] == "TB-FIXTURE-1" and r["occurrence_id"] == "isa-nasdaq-fri2@2026-10-09",
           "T3 correct launcher proceeds with a receipt: %s" % r)
        ok(effective_state("isa-nasdaq-fri2", root=tmp) == PROBATION, "T3b probation is DERIVED from the receipt")
        ok(canonical_loaded_labels(tmp) == {"isa-nasdaq-fri2"},
           "T3e only the launched task's SKILL becomes canonical_loaded; the rest stay mirrors")
        ok(any(x.get("receipt_id") == r["receipt_id"] for x in read_ledger(tmp)), "T3c receipt appended")
        ok(observe("Run_Context_ISA_Growth_Stock_Analysis.md", root=tmp, now=fri2, setup_text=setup)["verdict"]
           == "OBSERVED_VIA_LAUNCHER", "T3d the legacy hook does not double-count a launched occurrence")

        # T9 duplicate occurrence: incomplete -> re-launch allowed + flagged; completed -> NO_OP
        r2 = launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TR, setup_text=setup)
        ok(r2["verdict"] == PROCEED and r2.get("prior_incomplete_receipts") == [r["receipt_id"]],
           "T9a relaunch after an incomplete run proceeds and names the prior receipt")
        complete(r2["receipt_id"], "COMPLETED", "fixture", root=tmp)
        r3 = launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TR, setup_text=setup)
        ok(r3["verdict"] == NO_OP_DUPLICATE, "T9b a duplicate firing after COMPLETED is an idempotent no-op")
        try:
            complete("TIR-NOPE", "COMPLETED", root=tmp)
            ok(False, "T9c unknown receipt must raise")
        except TaskAuthorityError:
            ok(True, "T9c")

        # T7 out of window
        r4 = launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=wrongfri, trusted=TR, setup_text=setup)
        ok(r4["verdict"] == NO_OP_OUTSIDE and r4["capability_state"] == REFUSED_OUTSIDE_OCCURRENCE,
           "T7 out-of-window launch is a typed no-op")

        # T4/T5/T6 in PROBATION: mismatches WARN, never block (critical containment)
        r5 = launch("isa-nasdaq-fri2", "ISA-TL-0", root=tmp, now=datetime.datetime(2026, 11, 13, 9), trusted=UNTR, setup_text=setup, dry_run=True)
        ok(r5["verdict"] == PROCEED and len(r5["warnings"]) >= 2 and r5["capability_state"] == LEGACY_UNVERIFIED,
           "T4-6 legacy/probation: wrong protocol + untrusted LIVE WARN and proceed: %s" % r5["warnings"])
        # unknown task key refuses in any state (nothing to load)
        ok(launch("isa-nope", PROTOCOL, root=tmp, now=fri2, trusted=TR, setup_text=setup, dry_run=True)
           ["capability_state"] == REFUSED_LAUNCHER_IDENTITY, "T4b unknown task key refuses")

        # ENFORCED: every mismatch refuses BEFORE capital logic
        docE = json.loads(json.dumps(doc))
        docE["tasks"]["isa-nasdaq-fri2"]["enforcement_state"] = ENFORCED
        docE["tasks"]["isa-nasdaq-fri2"]["enforced_on_evidence"] = r["receipt_id"]
        ok(launch("isa-nasdaq-fri2", "ISA-TL-0", root=tmp, now=fri2, trusted=TR, contracts=docE,
                  setup_text=setup, dry_run=True)["capability_state"] == REFUSED_LAUNCHER_IDENTITY,
           "T4 ENFORCED wrong protocol refuses")
        ok(launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=UNTR, contracts=docE,
                  setup_text=setup, dry_run=True)["capability_state"] == REFUSED_UNTRUSTED_LIVE,
           "T5 ENFORCED untrusted LIVE refuses")
        TRBAD = dict(TR, digests=dict(TR["digests"], **{"isa-nasdaq-fri2": "0" * 64}))
        ok(launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TRBAD, contracts=docE,
                  setup_text=setup, dry_run=True)["capability_state"] == REFUSED_CANONICAL_SURFACE_MISMATCH,
           "T6 ENFORCED canonical SKILL hash mismatch refuses")
        e_ok = launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TR, contracts=docE,
                      setup_text=setup, dry_run=True)
        ok(e_ok["capability_state"] == AUTHORITY_VERIFIED, "T4c ENFORCED + all identities match -> AUTHORITY_VERIFIED")
        # T10 build turnover: a new Trusted build id with the same launcher still resolves
        TR2 = dict(TR, build_id="TB-FIXTURE-2")
        ok(launch("isa-nasdaq-fri2", PROTOCOL, root=tmp, now=fri2, trusted=TR2, contracts=docE,
                  setup_text=setup, dry_run=True)["trusted_build_id"] == "TB-FIXTURE-2",
           "T10 build turnover needs no scheduler edit")
        # ENFORCED without evidence is an invalid contract
        docBad = json.loads(json.dumps(docE))
        docBad["tasks"]["isa-nasdaq-fri2"].pop("enforced_on_evidence")
        ok(any("enforced_on_evidence" in e for e in validate_contracts(docBad)), "C1 ENFORCED needs evidence")
        docBad2 = json.loads(json.dumps(doc))
        docBad2["tasks"]["isa-nasdaq-fri2"]["enforcement_state"] = PROBATION
        ok(validate_contracts(docBad2), "C2 PROBATION may never be STORED")
        docBad3 = json.loads(json.dumps(doc))
        docBad3["tasks"]["isa-nasdaq-fri2"]["cron"] = "0 9 8-14 * 5"
        ok(validate_contracts(docBad3), "C3 NEGATIVE CONTROL: a copied cron in the contract fires (R4.4)")
        # canonical surface absent refuses in ANY state (nothing to execute)
        os.remove(os.path.join(tmp, "Skills_to_Edit/isa-weekly-eps-snapshot/SKILL.md"))
        ok(launch("isa-weekly-eps-snapshot", PROTOCOL, root=tmp, now=datetime.datetime(2026, 10, 5, 8),
                  trusted=TR, setup_text=setup, dry_run=True)["capability_state"] == REFUSED_CANONICAL_SURFACE_MISMATCH,
           "T11 absent canonical workflow refuses - a mirror/advisory copy cannot stand in")
        with open(os.path.join(tmp, "Skills_to_Edit/isa-weekly-eps-snapshot/SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("eps workflow")
        # T8 missed occurrence: health reports without inventing a run
        h = health(tmp, now=datetime.datetime(2026, 10, 20, 12), setup_text=setup)
        ok(h["tasks"]["isa-nasdaq-fri2"]["missed"] == [] and h["tasks"]["isa-nasdaq-fri2"]["effective_state"] == PROBATION,
           "T8a a receipted occurrence is not missed")
        ok("isa-monthly-prerun@2026-10-03" in h["tasks"]["isa-monthly-prerun"]["missed"]
           and h["tasks"]["isa-monthly-prerun"]["state"] == "UNOBSERVED_OCCURRENCE",
           "T8b a legacy task with no receipt is UNOBSERVED (missed OR unhooked - never invented)")
        ok(h["tasks"]["intramonth-stock-review"]["expected"] == [], "T8c on-demand tasks have no expected occurrence")
        hE = health(tmp, now=datetime.datetime(2026, 10, 20, 12), setup_text=setup,
                    contracts=dict(docE, tasks=dict(docE["tasks"], **{"isa-monthly-prerun": dict(
                        docE["tasks"]["isa-monthly-prerun"], enforcement_state=ENFORCED, enforced_on_evidence="x")})))
        ok(hE["state"] == "RED" and hE["tasks"]["isa-monthly-prerun"]["state"] == MISSED_EXPECTED_OCCURRENCE,
           "T8d an ENFORCED task that never fired is MISSED and RED")
        # integrity + generated launcher drift
        ok(any("no generated" in e for e in integrity_findings(tmp, setup)), "I1 missing LAUNCHER.md fires")
        write_launchers(tmp)
        ok(integrity_findings(tmp, setup) == [], "I2 clean after generation: %s" % integrity_findings(tmp, setup))
        with open(launcher_path("isa-nasdaq-fri2", tmp), "a", encoding="utf-8") as fh:
            fh.write("Also check the 2nd Friday.\n")
        ok(any("differs from the generator" in e for e in integrity_findings(tmp, setup)),
           "I3 NEGATIVE CONTROL: a hand-edited launcher fires")
        # corrupt ledger line is typed, not fatal
        with open(os.path.join(tmp, LEDGER_REL), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        ok(any(x.get("kind") == "CORRUPT_LINE" for x in read_ledger(tmp)), "L1 corrupt ledger line typed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok(check_scheduled_task_authority_states(), "CAP must-fire: every decision state reached through launch()")
    if verbose:
        print("task_authority selftest: %d checks PASS" % n[0])
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    if "--selftest" in argv:
        return _selftest()
    cmd = argv[0] if argv else ""
    if cmd == "launch":
        r = launch(opt("--task-key"), opt("--protocol"), dry_run="--dry-run" in argv)
        print(json.dumps(r, indent=1, default=str))
        return 0 if r["verdict"] in (PROCEED, NO_OP_OUTSIDE, NO_OP_DUPLICATE) else 2
    if cmd == "observe":
        r = observe(opt("--run-context"), opt("--task-key"))
        print(json.dumps(r, indent=1, default=str))
        return 0                                              # never blocks
    if cmd == "complete":
        r = complete(opt("--receipt-id"), opt("--outcome"), opt("--ref"))
        print(json.dumps(r, indent=1))
        return 0
    if cmd == "health":
        r = health()
        print(json.dumps(r, indent=1, default=str))
        return 0 if r["state"] != "RED" else 3
    if cmd == "launchers":
        if "--write" in argv:
            for p in write_launchers():
                print("wrote", p)
        else:
            for k in sorted(load_contracts()["tasks"]):
                print("=" * 20, k, "=" * 20)
                print(render_launcher(k))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
