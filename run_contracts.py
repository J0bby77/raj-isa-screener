#!/usr/bin/env python3
"""
run_contracts.py — ISA-0830 (under umbrella ISA-0826, 04-Oct-2026): the ONE home for the RUN-TIME
CONTRACTS the release gate certifies and the orchestrators consume.

Authority: ISA_Engineering_Rules.md R4.17 (artefact ownership/mutability), R4.18 (typed failure
scope), R5.12 (scheduled runtime envelope), R5.14 (temporal boundary matrix), R6.6 (external input
identity) and R4.15 (executed run-surface identity). Declaration: the SIGNED file
`Dashboard/state/artefact_contracts.json` (release_gate.CONFIG_FILES) plus `runtime_envelope` blocks in
the signed `Dashboard/state/task_contracts.json`. This module DECLARES NOTHING ITSELF beyond the
vocabularies; release_gate only CONSUMES these functions (R4.4 - one home per rule).

WHY (measured 30-Sep-2026 production parallel; 04-Oct-2026 on TB-2026-10-03-04):
  * ISA-0775 - the certified pre-run REWROTE signed target_state.json; LIVE went UNTRUSTED after one pass.
    CONFIG_RUNTIME_KEYS fixed that ONE file. No declaration said which persistent artefacts a workflow
    may write, so the next one is unprotected (framework_atlas `writes` sees 1 target in the whole tree,
    because paths are built with os.path.join - static writer detection cannot be the basis).
  * ISA-0778 - one authority error in errors[] skipped Steps 1.5/3/4/6/9. `_data_errors` fixed ONE
    prefix in ONE orchestrator. A failure did not say what it blocks.
  * ISA-0779 - a SIPP export was accepted as the ISA. The account check covers the PORTFOLIO export
    only; the X-Ray header carries ACB8G2I and is never compared (MEASURED 04-Oct).
  * ISA-0780/0786 - correctness that cannot finish inside the host ceiling is not production evidence.
  * ISA-0776/0777/0788/0789 - date-dependent controls passed ordinary tests and failed at T+1 / on repeat.

FAIL-CLOSED (R4.3): an unreadable declaration, an undeclared artefact, an undeclared failure prefix, a
missing identity check, an unmeasured runtime or a missing temporal point is a FINDING, never a pass.

ROLLBACK (R4.13): release_gate's contract gates read `isa_policy.V2_FLAGS['run_contract_gates']`
(default True); False records the gates as NOT_ENFORCED (never GREEN). The orchestrator scope rules fall
back to the pre-ISA-0830 `_data_errors` reading when the declaration is unreadable - and say so.
"""
from __future__ import annotations

import ast
import datetime
import fnmatch
import json
import os
import re
import sys
from typing import Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_MODULE = "run_contracts"
CONTRACT_REL = os.path.join("Dashboard", "state", "artefact_contracts.json")
TASKS_REL = os.path.join("Dashboard", "state", "task_contracts.json")
SCHEMA_VERSION = "1.0.0"

# R4.17 — the artefact classes. ONE home; artefact_contracts.json may use only these.
ARTEFACT_CLASSES = (
    "SIGNED_IMMUTABLE_CONFIG",          # only a Trusted promotion writes it
    "SIGNED_WITH_RUNTIME_PROJECTION",   # signed declared keys + declared runtime keys (CONFIG_RUNTIME_KEYS)
    "MUTABLE_RUNTIME_STATE",            # rewritten by its declared writer workflows
    "APPEND_ONLY_HISTORY",              # appended by its declared writer workflows; never truncated
    "RUN_OUTPUT",                       # one occurrence's output (month/date keyed)
    "CACHE",                            # re-derivable; loss costs time, not truth
    "EXTERNAL_INPUT",                   # supplied by Raj/the broker; identity keys mandatory (R6.6)
    "GENERATED_VIEW",                   # rendered from a canonical store; never hand-edited (R14.3)
    "SIGNED_SOURCE",                    # code, Rules, run surfaces: fingerprinted; only a promotion writes it
)
SIGNED_CLASSES = ("SIGNED_IMMUTABLE_CONFIG", "SIGNED_WITH_RUNTIME_PROJECTION", "SIGNED_SOURCE")
PROMOTION_WRITER = "release_promotion"

# R4.18 — typed failure scope. ONE home.
FAILURE_SCOPES = ("BLOCK_RUN", "BLOCK_NEW_CAPITAL", "BLOCK_COMPONENT", "DEGRADE_ONLY", "INFORMATIONAL")
UNDECLARED_SCOPE = "UNDECLARED"
CAPITAL_COMPONENT = "new_capital"

# R5.14 — the temporal boundary matrix. ONE home.
TEMPORAL_POINTS = ("T-1", "T", "T+1", "repeat_same_occurrence", "next_occurrence")
_DATE_ATTRS = {"today", "now", "utcnow"}
_DATE_NAMES = {"_today", "_clock_today"}

GREEN, RED, UNKNOWN = "GREEN", "RED", "UNKNOWN"
MIN_REASON = 20


class ContractUnreadable(RuntimeError):
    """The declaration could not be read. Callers treat this as RED/UNKNOWN, never as an empty contract."""


def _isa_root(root: str) -> str:
    return os.path.dirname(os.path.abspath(root))


def _inv_prefix(root: str) -> str:
    return os.path.basename(os.path.abspath(root))


def load_contract(root: str = HERE) -> dict:
    p = os.path.join(root, CONTRACT_REL)
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise ContractUnreadable("no artefact contract at %s (R4.17: an undeclared artefact surface is "
                                 "UNKNOWN, never clean)" % p)
    except Exception as exc:                                            # noqa: BLE001
        raise ContractUnreadable("artefact contract unreadable (%s: %s)" % (type(exc).__name__, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("artefacts"), list):
        raise ContractUnreadable("artefact contract has no `artefacts` list")
    return doc


def _reason_ok(s) -> bool:
    return isinstance(s, str) and len(s.strip()) >= MIN_REASON


# ─────────────────────────────────────────────────────────────────────────────────────────
# R4.17 — artefact ownership / mutability
# ─────────────────────────────────────────────────────────────────────────────────────────

def _entry_pattern(e: dict) -> str:
    return str(e.get("path") or e.get("glob") or "")


def classify_path(rel: str, contract: dict) -> Optional[dict]:
    """The declared entry for an ISA-root-relative path: an exact `path` wins over a `glob`; among
    globs the longest (most specific) pattern wins. None = UNDECLARED."""
    rel = rel.replace(os.sep, "/")
    exact = [e for e in contract.get("artefacts") or [] if e.get("path") and e["path"] == rel]
    if exact:
        return exact[0]
    globs = [e for e in contract.get("artefacts") or [] if e.get("glob") and fnmatch.fnmatchcase(rel, e["glob"])]
    if not globs:
        return None
    return sorted(globs, key=lambda e: -len(e["glob"]))[0]


def contract_errors(root: str = HERE, contract: Optional[dict] = None) -> List[str]:
    """Static validation of the declaration (R4.17/R6.6/R4.18). [] = consistent. Read by the release
    gate and by consistency_check, so a malformed contract fails the build, not the next run."""
    errs: List[str] = []
    try:
        doc = contract if contract is not None else load_contract(root)
    except ContractUnreadable as exc:
        return ["R4.17: %s" % exc]
    inv = _inv_prefix(root)
    for i, e in enumerate(doc.get("artefacts") or []):
        pat = _entry_pattern(e)
        if not pat:
            errs.append("R4.17: artefact entry %d declares neither path nor glob" % i)
            continue
        cls = e.get("class")
        if cls not in ARTEFACT_CLASSES:
            errs.append("R4.17: %s class %r is not one of %s" % (pat, cls, ARTEFACT_CLASSES))
        w = e.get("writers")
        if not isinstance(w, list):
            errs.append("R4.17: %s declares no `writers` list (silence is not 'no writer')" % pat)
            continue
        if cls in ("SIGNED_IMMUTABLE_CONFIG", "SIGNED_SOURCE") and set(w) - {PROMOTION_WRITER}:
            errs.append("R4.17: %s is SIGNED_IMMUTABLE_CONFIG but declares runtime writer(s) %s - only "
                        "a Trusted promotion may write it (ISA-0775 class)" % (pat, sorted(set(w) - {PROMOTION_WRITER})))
        if cls == "EXTERNAL_INPUT":
            keys = e.get("identity_keys")
            if not isinstance(keys, dict) or not keys:
                errs.append("R6.6: EXTERNAL_INPUT %s declares no identity_keys" % pat)
            else:
                for k, spec in keys.items():
                    if not isinstance(spec, dict):
                        errs.append("R6.6: %s identity key %s has no disposition" % (pat, k))
                    elif "check" in spec:
                        if not spec.get("check") or not spec.get("must_fire"):
                            errs.append("R6.6: %s identity key %s check needs `check` (qname) and `must_fire` (selftest marker)" % (pat, k))
                    elif "not_provable" in spec:
                        if not _reason_ok(spec.get("not_provable")) or not re.match(r"^ISA-\d{4}$", str(spec.get("owner") or "")):
                            errs.append("R6.6: %s identity key %s NOT_PROVABLE needs a reason (>= %d chars) and an owner item" % (pat, k, MIN_REASON))
                    else:
                        errs.append("R6.6: %s identity key %s is neither checked nor declared not_provable" % (pat, k))
    # one home for the signed list: release_gate.CONFIG_FILES. Every signed file must be classified
    # SIGNED_*, and every SIGNED_* entry must be in CONFIG_FILES; SIGNED_WITH_RUNTIME_PROJECTION <=> CONFIG_RUNTIME_KEYS.
    try:
        sys.path.insert(0, root)
        import release_gate as _rg
        cfg = {inv + "/" + c.replace(os.sep, "/") for c in _rg.CONFIG_FILES}
        rtk = {inv + "/" + c.replace(os.sep, "/") for c in _rg.CONFIG_RUNTIME_KEYS}
        signed = {inv + "/" + c.replace(os.sep, "/") for c in _rg.signed_paths(root)}
    except Exception as exc:                                            # noqa: BLE001
        errs.append("R4.17: release_gate unreadable (%s) - signed list UNKNOWN" % exc)
        cfg, rtk, signed = set(), set(), set()
    for c in sorted(cfg):
        e = classify_path(c, doc)
        if e is None or e.get("class") not in SIGNED_CLASSES:
            errs.append("R4.17: signed config %s (release_gate.CONFIG_FILES) is not classified SIGNED_* in the "
                        "artefact contract (got %s)" % (c, (e or {}).get("class")))
        elif (c in rtk) != (e.get("class") == "SIGNED_WITH_RUNTIME_PROJECTION"):
            errs.append("R4.17: %s runtime projection disagrees: CONFIG_RUNTIME_KEYS=%s, contract class=%s"
                        % (c, c in rtk, e.get("class")))
    for e in doc.get("artefacts") or []:
        if e.get("class") in SIGNED_CLASSES and e.get("path") and signed and e["path"] not in signed:
            errs.append("R4.17: %s is declared %s but no Trusted receipt fingerprints it (release_gate.signed_paths) "
                        "- a signed class with no signature" % (e["path"], e["class"]))
    # R4.18 declarations
    for i, r in enumerate(doc.get("failure_scope_rules") or []):
        if r.get("scope") not in FAILURE_SCOPES:
            errs.append("R4.18: failure rule %d (%s) scope %r not in %s" % (i, r.get("prefix"), r.get("scope"), FAILURE_SCOPES))
        if r.get("scope") == "BLOCK_COMPONENT" and not r.get("component"):
            errs.append("R4.18: failure rule %s is BLOCK_COMPONENT with no component" % r.get("prefix"))
        if not r.get("orchestrator") or not r.get("prefix"):
            errs.append("R4.18: failure rule %d lacks orchestrator/prefix" % i)
    return errs


def classify_writes(changed: Dict[str, str], writer: str, root: str = HERE,
                    contract: Optional[dict] = None, before_dir: Optional[str] = None,
                    after_dir: Optional[str] = None) -> dict:
    """R4.17 at the point of WRITING. `changed` = {isa-root-relative path: NEW|CHANGED} produced by
    `writer` (a declared workflow). Returns {"state": GREEN|RED|UNKNOWN, "violations": [...],
    "undeclared": [...], "classified": {...}}.
      SIGNED_IMMUTABLE_WRITE  - a runtime write to SIGNED_IMMUTABLE_CONFIG (always a violation)
      PROJECTION_BROKEN       - a SIGNED_WITH_RUNTIME_PROJECTION file whose DECLARED projection changed
      UNDECLARED_WRITER       - a declared artefact written by a workflow not in its `writers`
      UNDECLARED_ARTEFACT     - a persistent path no entry declares"""
    try:
        doc = contract if contract is not None else load_contract(root)
    except ContractUnreadable as exc:
        return {"state": UNKNOWN, "violations": [{"kind": "CONTRACT_UNREADABLE", "why": str(exc)}],
                "undeclared": [], "classified": {}}
    viol, undeclared, classified = [], [], {}
    for rel, kind in sorted(changed.items()):
        e = classify_path(rel, doc)
        if e is None:
            undeclared.append(rel)
            viol.append({"kind": "UNDECLARED_ARTEFACT", "path": rel,
                         "why": "R4.17: %s wrote %s, which no artefact-contract entry declares" % (writer, rel)})
            continue
        classified[rel] = e.get("class")
        if e.get("class") in ("SIGNED_IMMUTABLE_CONFIG", "SIGNED_SOURCE"):
            viol.append({"kind": "SIGNED_IMMUTABLE_WRITE", "path": rel,
                         "why": "R4.17: %s wrote SIGNED_IMMUTABLE_CONFIG %s at runtime (ISA-0775 class)" % (writer, rel)})
            continue
        if writer not in (e.get("writers") or []):
            viol.append({"kind": "UNDECLARED_WRITER", "path": rel,
                         "why": "R4.17: %s is not a declared writer of %s (declared: %s)" % (writer, rel, e.get("writers"))})
        if e.get("class") == "SIGNED_WITH_RUNTIME_PROJECTION" and before_dir and after_dir and kind == "CHANGED":
            try:
                import release_gate as _rg
                inv_rel = rel.split("/", 1)[1] if "/" in rel else rel
                b = _rg._config_file_digest(os.path.join(before_dir, rel), inv_rel)
                a = _rg._config_file_digest(os.path.join(after_dir, rel), inv_rel)
                if a != b:
                    viol.append({"kind": "PROJECTION_BROKEN", "path": rel,
                                 "why": "R4.17: %s changed the DECLARED (signed) projection of %s - only runtime keys may change" % (writer, rel)})
            except Exception as exc:                                    # noqa: BLE001
                viol.append({"kind": "PROJECTION_UNVERIFIABLE", "path": rel, "why": "R4.17: %s" % exc})
    return {"state": GREEN if not viol else RED, "violations": viol, "undeclared": undeclared,
            "classified": classified, "writer": writer, "n_changed": len(changed)}


def blocking_write_violations(result: dict) -> List[dict]:
    """The violations a RUNTIME commit must refuse on: integrity breaches of signed state. An undeclared
    artefact is a certification finding (the release gate refuses it) and a DEGRADE_ONLY runtime warning."""
    return [v for v in result.get("violations") or []
            if v["kind"] in ("SIGNED_IMMUTABLE_WRITE", "PROJECTION_BROKEN", "PROJECTION_UNVERIFIABLE", "CONTRACT_UNREADABLE")]


# ─────────────────────────────────────────────────────────────────────────────────────────
# R4.18 — typed failure scope
# ─────────────────────────────────────────────────────────────────────────────────────────

def failure_rules(orchestrator: str, contract: dict) -> List[dict]:
    return [r for r in contract.get("failure_scope_rules") or [] if r.get("orchestrator") == orchestrator]


def classify_failure(orchestrator: str, message: str, contract: dict) -> dict:
    """The declared scope of one structured failure; the LONGEST matching prefix wins. No match =
    UNDECLARED, which every consumer treats as BLOCK_RUN (fail-closed, R4.3)."""
    msg = str(message)
    hits = [r for r in failure_rules(orchestrator, contract) if msg.startswith(r["prefix"])]
    if not hits:
        return {"scope": UNDECLARED_SCOPE, "component": None, "prefix": None,
                "why": "R4.18: failure has no declared scope - treated as BLOCK_RUN"}
    r = sorted(hits, key=lambda x: -len(x["prefix"]))[0]
    return {"scope": r["scope"], "component": r.get("component"), "prefix": r["prefix"]}


def blocks(scope_rec: dict, needs) -> bool:
    """Does a classified failure block a stage that needs the components `needs`?"""
    s, needs = scope_rec.get("scope"), set(needs or ())
    if s in ("BLOCK_RUN", UNDECLARED_SCOPE):
        return True
    if s == "BLOCK_COMPONENT":
        return scope_rec.get("component") in needs
    if s == "BLOCK_NEW_CAPITAL":
        return CAPITAL_COMPONENT in needs
    return False


def blocking_failures(orchestrator: str, errors, needs, contract: Optional[dict] = None,
                      root: str = HERE) -> List[str]:
    """The failures in `errors` that block a stage needing `needs`. An unreadable contract returns
    EVERY error (fail-closed: the pre-ISA-0830 reading), never none."""
    try:
        doc = contract if contract is not None else load_contract(root)
    except ContractUnreadable:
        return [str(e) for e in (errors or [])]
    return [str(e) for e in (errors or []) if blocks(classify_failure(orchestrator, e, doc), needs)]


def emitted_failure_prefixes(module_path: str, list_names=("errors",)) -> List[dict]:
    """Static scan: the literal leading text of every `<list>.append(...)` in a module. f-strings and
    `"..." + x` contribute their leading literal; a call with NO leading literal is reported with
    prefix None (it cannot be scoped - finding)."""
    src = open(module_path, encoding="utf-8").read()
    tree = ast.parse(src)
    consts = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            consts[n.targets[0].id] = n.value.value

    def lead(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = ""
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out += v.value
                else:
                    break
            return out or None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return lead(node.left)
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return lead(node.func.value)
        return None
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append" \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in list_names and n.args:
            out.append({"line": n.lineno, "prefix": lead(n.args[0])})
    return out


def failure_scope_gaps(root: str = HERE, contract: Optional[dict] = None) -> List[str]:
    """R4.18 at build time: every failure an ENROLLED orchestrator can emit carries a declared scope."""
    try:
        doc = contract if contract is not None else load_contract(root)
    except ContractUnreadable as exc:
        return ["R4.18: %s" % exc]
    gaps = []
    for orch in doc.get("failure_scope_orchestrators") or []:
        p = os.path.join(root, orch + ".py")
        if not os.path.exists(p):
            gaps.append("R4.18: enrolled orchestrator %s.py is absent" % orch)
            continue
        for em in emitted_failure_prefixes(p):
            if not em["prefix"] or not em["prefix"].strip():
                gaps.append("R4.18: %s.py:%d appends a failure with no literal prefix - it cannot carry a declared scope"
                            % (orch, em["line"]))
                continue
            c = classify_failure(orch, em["prefix"], doc)
            if c["scope"] == UNDECLARED_SCOPE:
                gaps.append("R4.18: %s.py:%d emits %r with NO declared failure scope" % (orch, em["line"], em["prefix"][:60]))
    return gaps


# ─────────────────────────────────────────────────────────────────────────────────────────
# R6.6 — external input identity
# ─────────────────────────────────────────────────────────────────────────────────────────

def _function_defined(root: str, qname: str) -> bool:
    mod, _, fn = qname.partition(".")
    p = os.path.join(root, mod + ".py")
    if not fn or not os.path.exists(p):
        return False
    try:
        tree = ast.parse(open(p, encoding="utf-8").read())
    except SyntaxError:
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn for n in tree.body)


def input_identity_gaps(root: str = HERE, contract: Optional[dict] = None) -> List[str]:
    try:
        doc = contract if contract is not None else load_contract(root)
    except ContractUnreadable as exc:
        return ["R6.6: %s" % exc]
    gaps = []
    for e in doc.get("artefacts") or []:
        if e.get("class") != "EXTERNAL_INPUT":
            continue
        for k, spec in (e.get("identity_keys") or {}).items():
            if not isinstance(spec, dict):
                continue
            if "check" in spec:
                q = spec.get("check") or ""
                if not _function_defined(root, q):
                    gaps.append("R6.6: %s identity key %s names check %s, which is not defined on disk" % (_entry_pattern(e), k, q))
                    continue
                src_mod = os.path.join(root, (spec.get("must_fire_in") or q.split(".")[0]) + ".py")
                txt = open(src_mod, encoding="utf-8").read() if os.path.exists(src_mod) else ""
                if not spec.get("must_fire") or spec["must_fire"] not in txt:
                    gaps.append("R6.6: %s identity key %s: must-fire marker %r is absent from %s - the boundary "
                                "refusal is not tested" % (_entry_pattern(e), k, spec.get("must_fire"), os.path.basename(src_mod)))
    return gaps


# ─────────────────────────────────────────────────────────────────────────────────────────
# R5.12 — scheduled runtime envelope
# ─────────────────────────────────────────────────────────────────────────────────────────

def runtime_envelope(task_key: str, root: str = HERE, ledger_rows: Optional[list] = None) -> dict:
    """WITHIN | EXCEEDS | UNMEASURED | UNDECLARED for one scheduled task. The envelope lives on the task's
    signed contract: {host_ceiling_s, headroom_pct, stages: [..], evidence_ledger, continuation}.
    WITHIN requires the MAX measured duration of every declared stage over the last `window`
    measurements to fit under ceiling x (1 - headroom). A stage that does not fit is acceptable ONLY if
    the declared continuation re-runs it to completion (COMPLETE after INCOMPLETE is evidenced)."""
    try:
        tc = json.load(open(os.path.join(root, TASKS_REL), encoding="utf-8"))
    except Exception as exc:                                            # noqa: BLE001
        return {"state": UNKNOWN, "why": "task contracts unreadable (%s)" % exc}
    t = (tc.get("tasks") or {}).get(task_key)
    if not t:
        return {"state": "UNDECLARED", "why": "no task contract %s" % task_key}
    env = t.get("runtime_envelope")
    if not isinstance(env, dict):
        return {"state": "UNDECLARED", "why": "R5.12: task %s declares no runtime_envelope" % task_key}
    ceiling = float(env.get("host_ceiling_s") or 0)
    head = float(env.get("headroom_pct") or 0)
    budget = ceiling * (1 - head / 100.0)
    window = int(env.get("window") or 5)
    rows = ledger_rows
    if rows is None:
        p = os.path.join(root, env.get("evidence_ledger") or "")
        rows = []
        if os.path.isfile(p):
            for ln in open(p, encoding="utf-8"):
                try:
                    rows.append(json.loads(ln))
                except Exception:                                       # noqa: BLE001
                    continue
    per = {}

    def _ts(x):
        try:
            return datetime.datetime.fromisoformat(str(x).replace("Z", ""))
        except Exception:                                               # noqa: BLE001
            return None
    for st in env.get("stages") or []:
        xs = []
        started = {}
        for r in rows:
            if r.get("stage") != st:
                continue
            if r.get("status") == "STARTED":
                started[r.get("occurrence")] = _ts(r.get("at"))
            elif str(r.get("status", "")).startswith("COMPLETE"):
                if r.get("secs") is not None:
                    xs.append(float(r["secs"]))
                else:                                                   # a COMPLETE row with no secs: STARTED->COMPLETE stamps
                    a, b = started.get(r.get("occurrence")), _ts(r.get("at"))
                    if a and b and b >= a:
                        xs.append((b - a).total_seconds())
        per[st] = xs[-window:]
    unmeasured = [s for s, xs in per.items() if not xs]
    over = {s: max(xs) for s, xs in per.items() if xs and max(xs) > budget}
    cont = env.get("continuation") or {}
    if unmeasured:
        state = "UNMEASURED"
    elif over and not (cont.get("module") and cont.get("resumable_stages") and set(over) <= set(cont["resumable_stages"])):
        state = "EXCEEDS"
    else:
        state = "WITHIN"
    return {"state": state, "task": task_key, "budget_s": round(budget, 1), "ceiling_s": ceiling,
            "headroom_pct": head, "max_by_stage": {s: (max(x) if x else None) for s, x in per.items()},
            "over_budget": over, "unmeasured": unmeasured, "continuation": cont or None,
            "why": ("every declared stage fits inside the envelope" if state == "WITHIN" and not over else
                    "stages over budget %s are re-run to completion by the declared continuation %s" % (sorted(over), cont.get("module"))
                    if state == "WITHIN" else
                    "R5.12: no measured duration for %s" % unmeasured if state == "UNMEASURED" else
                    "R5.12: %s exceed the %.0fs budget with no supported continuation" % (over, budget))}


# ─────────────────────────────────────────────────────────────────────────────────────────
# R4.15 — executed run-surface identity
# ─────────────────────────────────────────────────────────────────────────────────────────

EXECUTED_BASES = ("executed", "canonical_loaded", "launcher_receipt")


def executed_identity(disposition: dict, surface_bases: Dict[str, str]) -> dict:
    """VERIFIED_NO_CHANGE on an executed surface needs EXECUTED identity: every task label it names
    must have been read from the executed contract (or proven loaded by the thin launcher). A mirror
    basis is UNKNOWN - it blocks unless an owning item waives the finding."""
    labels = disposition.get("tasks") if isinstance(disposition, dict) else None
    if not labels:
        return {"state": UNKNOWN, "unknown": [], "why": "R4.15: the disposition names no task surface, so executed identity cannot be established"}
    unknown = [l for l in labels if surface_bases.get(l) not in EXECUTED_BASES]
    return {"state": GREEN if not unknown else UNKNOWN, "unknown": unknown,
            "bases": {l: surface_bases.get(l) for l in labels},
            "why": ("every named surface was read from its executed contract" if not unknown else
                    "R4.15: %s read from the MIRROR only - the installed surface is UNKNOWN" % unknown)}


# ─────────────────────────────────────────────────────────────────────────────────────────
# R5.14 — temporal boundary matrix
# ─────────────────────────────────────────────────────────────────────────────────────────

def date_dependent(module_path: str) -> bool:
    """True if the module reads the clock for a DECISION (date.today / datetime.now / utcnow / the
    framework's _today/_clock_today). time.time() (durations) is not a date dependency."""
    try:
        tree = ast.parse(open(module_path, encoding="utf-8").read())
    except Exception:                                                   # noqa: BLE001
        return True                                                     # unknown -> treat as dependent (fail-closed)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and (f.attr in _DATE_ATTRS or f.attr in _DATE_NAMES):
                return True
            if isinstance(f, ast.Name) and f.id in _DATE_NAMES:
                return True
    return False


def temporal_matrix(evaluate: Callable[[datetime.date], object], boundary: datetime.date,
                    next_boundary: Optional[datetime.date] = None, repeat: Optional[Callable[[datetime.date], object]] = None) -> dict:
    """Evaluate a date-dependent capability at the five R5.14 points. `evaluate(d)` must be pure in d
    (inject the clock); `repeat(d)` re-runs the SAME occurrence (default: evaluate again) so the caller can
    assert idempotence; `next_boundary` defaults to boundary + 1 day after T+1 for the next occurrence."""
    one = datetime.timedelta(days=1)
    nb = next_boundary or (boundary + 2 * one)
    out = {"T-1": evaluate(boundary - one), "T": evaluate(boundary), "T+1": evaluate(boundary + one)}
    out["repeat_same_occurrence"] = (repeat or evaluate)(boundary)
    out["next_occurrence"] = evaluate(nb)
    return out


def temporal_gaps(spec_temporal: Optional[dict], changed_modules: List[str], root: str = HERE) -> List[str]:
    """R5.14 at build time: each changed date-dependent module declares its five-point matrix (each
    point a PASS marker present in its selftest or a named test file) or an N/A with a reason."""
    gaps = []
    spec_temporal = spec_temporal if isinstance(spec_temporal, dict) else {}
    for m in sorted(set(x.replace(".py", "") for x in changed_modules)):
        p = os.path.join(root, m + ".py")
        if not os.path.exists(p) or not date_dependent(p):
            continue
        d = spec_temporal.get(m)
        if d is None:
            gaps.append("R5.14: %s reads the clock and the change declares no temporal matrix (T-1/T/T+1/repeat/next) and no N/A reason" % m)
            continue
        if isinstance(d, dict) and "na" in d:
            if not _reason_ok(d.get("na")):
                gaps.append("R5.14: %s temporal N/A needs a reason (>= %d chars)" % (m, MIN_REASON))
            continue
        checks = (d or {}).get("checks") if isinstance(d, dict) else None
        if not isinstance(checks, dict):
            gaps.append("R5.14: %s temporal entry has neither `checks` nor `na`" % m)
            continue
        src_file = os.path.join(root, d.get("in") or (m + ".py"))
        txt = open(src_file, encoding="utf-8").read() if os.path.exists(src_file) else ""
        for pt in TEMPORAL_POINTS:
            mk = checks.get(pt)
            if not mk:
                gaps.append("R5.14: %s temporal matrix is missing point %s" % (m, pt))
            elif mk not in txt:
                gaps.append("R5.14: %s temporal point %s marker %r is not in %s" % (m, pt, mk, os.path.basename(src_file)))
    return gaps


# ─────────────────────────────────────────────────────────────────────────────────────────
# selftest
# ─────────────────────────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)[:300]) if (detail and not cond) else ""))
        if not cond:
            fails.append(name)

    c = {"artefacts": [
        {"path": "Investment Analysis/isa_policy.py", "class": "SIGNED_IMMUTABLE_CONFIG", "writers": [PROMOTION_WRITER]},
        {"path": "Investment Analysis/target_state.json", "class": "SIGNED_WITH_RUNTIME_PROJECTION", "writers": [PROMOTION_WRITER, "monthly_prerun"]},
        {"glob": "Investment Analysis/run_context_*.json", "class": "RUN_OUTPUT", "writers": ["monthly_prerun"]},
        {"glob": "Investment Analysis/*.json", "class": "MUTABLE_RUNTIME_STATE", "writers": ["vci_run"]},
        {"path": "Investment Analysis/score_panel.csv", "class": "APPEND_ONLY_HISTORY", "writers": ["monthly_prerun", "weekly_screen"]}],
        "failure_scope_rules": [
            {"orchestrator": "o", "prefix": "Step 1 ", "scope": "BLOCK_RUN"},
            {"orchestrator": "o", "prefix": "Step 3 ", "scope": "BLOCK_COMPONENT", "component": "analytics"},
            {"orchestrator": "o", "prefix": "R18.5 CAPITAL AUTHORITY ", "scope": "BLOCK_NEW_CAPITAL"},
            {"orchestrator": "o", "prefix": "WARN ", "scope": "DEGRADE_ONLY"}]}
    # R4.17
    r = classify_writes({"Investment Analysis/isa_policy.py": "CHANGED"}, "monthly_prerun", contract=c)
    ok("R4.17 MUST-FIRE: a runtime write to SIGNED_IMMUTABLE_CONFIG is a violation",
       r["state"] == RED and blocking_write_violations(r) and r["violations"][0]["kind"] == "SIGNED_IMMUTABLE_WRITE", r)
    r = classify_writes({"Investment Analysis/new_store.jsonl": "NEW"}, "monthly_prerun", contract=c)
    ok("R4.17 MUST-FIRE: an undeclared persistent artefact is a violation (and NOT a runtime commit blocker)",
       r["state"] == RED and r["undeclared"] == ["Investment Analysis/new_store.jsonl"] and not blocking_write_violations(r), r)
    r = classify_writes({"Investment Analysis/foo.json": "CHANGED"}, "monthly_prerun", contract=c)
    ok("R4.17 MUST-FIRE: a declared artefact written by an undeclared writer is UNDECLARED_WRITER",
       r["violations"] and r["violations"][0]["kind"] == "UNDECLARED_WRITER", r)
    r = classify_writes({"Investment Analysis/run_context_oct_2026.json": "NEW",
                         "Investment Analysis/score_panel.csv": "CHANGED"}, "monthly_prerun", contract=c)
    ok("R4.17 NEGATIVE CONTROL: declared writes by a declared writer are GREEN; exact/longest-glob precedence holds",
       r["state"] == GREEN and r["classified"]["Investment Analysis/run_context_oct_2026.json"] == "RUN_OUTPUT", r)
    ok("R4.17 contract_errors: SIGNED_IMMUTABLE with a runtime writer is refused",
       any("SIGNED_IMMUTABLE_CONFIG but declares runtime writer" in e for e in contract_errors(
           HERE, {"artefacts": [{"path": "x/isa_policy.py", "class": "SIGNED_IMMUTABLE_CONFIG", "writers": ["monthly_prerun"]}]})))
    ok("R6.6 contract_errors: EXTERNAL_INPUT without identity keys is refused",
       any("declares no identity_keys" in e for e in contract_errors(
           HERE, {"artefacts": [{"glob": "x/*.xlsx", "class": "EXTERNAL_INPUT", "writers": []}]})))
    ok("R6.6 contract_errors: a NOT_PROVABLE key with no owner/reason is refused",
       any("NOT_PROVABLE needs a reason" in e for e in contract_errors(
           HERE, {"artefacts": [{"glob": "x/*.xlsx", "class": "EXTERNAL_INPUT", "writers": [],
                                 "identity_keys": {"account_id": {"not_provable": "no"}}}]})))
    # R4.18
    ok("R4.18 MUST-FIRE: an undeclared failure scope blocks every stage (fail-closed)",
       blocking_failures("o", ["Something new broke"], ["portfolio"], c) == ["Something new broke"])
    ok("R4.18 MUST-FIRE: BLOCK_COMPONENT(analytics) does NOT block a stage that needs only the portfolio (no cascade)",
       blocking_failures("o", ["Step 3 (analytics): boom"], ["portfolio"], c) == [])
    ok("R4.18 NEGATIVE CONTROL: BLOCK_COMPONENT(analytics) DOES block a stage that needs analytics",
       blocking_failures("o", ["Step 3 (analytics): boom"], ["analytics"], c) == ["Step 3 (analytics): boom"])
    ok("R4.18: a capital-authority status blocks only stages that need new capital",
       blocking_failures("o", ["R18.5 CAPITAL AUTHORITY REFUSED"], ["portfolio"], c) == [] and
       blocking_failures("o", ["R18.5 CAPITAL AUTHORITY REFUSED"], [CAPITAL_COMPONENT], c) != [])
    ok("R4.18: BLOCK_RUN blocks everything; DEGRADE_ONLY blocks nothing",
       blocking_failures("o", ["Step 1 sanity: low"], ["x"], c) and not blocking_failures("o", ["WARN thin"], ["x", CAPITAL_COMPONENT], c))
    ok("R4.18 fail-closed: an unreadable contract returns EVERY error as blocking",
       blocking_failures("o", ["Step 3 (analytics): x"], ["portfolio"], None, root="/nonexistent") == ["Step 3 (analytics): x"])
    d = tempfile.mkdtemp(prefix="rc_st_")
    with open(os.path.join(d, "orch.py"), "w") as fh:
        fh.write("P = 'R18.5 CAPITAL AUTHORITY '\nerrors=[]\nerrors.append(f'Step 1 (x): {1}')\nerrors.append(P + 'y')\n"
                 "errors.append('Brand new failure ' + str(2))\nerrors.append(msg)\n")
    ems = emitted_failure_prefixes(os.path.join(d, "orch.py"))
    ok("R4.18 scanner reads f-string / constant+concat / bare-name prefixes",
       [e["prefix"] for e in ems] == ["Step 1 (x): ", "R18.5 CAPITAL AUTHORITY ", "Brand new failure ", None], ems)
    os.makedirs(os.path.join(d, "Dashboard", "state"))
    c2 = dict(c, failure_scope_orchestrators=["orch"])
    c2["failure_scope_rules"] = [dict(r, orchestrator="orch") for r in c["failure_scope_rules"]]
    g = failure_scope_gaps(d, c2)
    ok("R4.18 MUST-FIRE: an emitted failure with no declared scope (and a non-literal one) fails the build",
       len(g) == 2 and any("Brand new failure" in x for x in g) and any("no literal prefix" in x for x in g), g)
    # R5.12
    rows = [{"stage": "run", "status": "COMPLETE", "secs": 100}, {"stage": "commit", "status": "COMPLETE", "secs": 23}]
    tasks = {"tasks": {"t": {"runtime_envelope": {"host_ceiling_s": 180, "headroom_pct": 10, "stages": ["run", "commit"], "window": 5}}}}
    os.makedirs(os.path.join(d, "Dashboard", "state"), exist_ok=True)
    json.dump(tasks, open(os.path.join(d, TASKS_REL), "w"))
    ok("R5.12 NEGATIVE CONTROL: stages inside 180s x 0.9 are WITHIN", runtime_envelope("t", d, rows)["state"] == "WITHIN")
    rows2 = rows + [{"stage": "run", "status": "COMPLETE", "secs": 170}]
    ok("R5.12 MUST-FIRE: a stage over budget with NO continuation EXCEEDS", runtime_envelope("t", d, rows2)["state"] == "EXCEEDS")
    tasks["tasks"]["t"]["runtime_envelope"]["continuation"] = {"module": "prerun_runner", "resumable_stages": ["run"]}
    json.dump(tasks, open(os.path.join(d, TASKS_REL), "w"))
    ok("R5.12: the same overrun with a declared resumable continuation is WITHIN", runtime_envelope("t", d, rows2)["state"] == "WITHIN")
    ok("R5.12 MUST-FIRE: a declared stage with no measurement is UNMEASURED",
       runtime_envelope("t", d, [{"stage": "run", "status": "COMPLETE", "secs": 10}])["state"] == "UNMEASURED")
    ok("R5.12 MUST-FIRE: a task with no envelope is UNDECLARED",
       runtime_envelope("nope", d, rows)["state"] == "UNDECLARED")
    # R4.15
    ok("R4.15 MUST-FIRE: a mirror-only task surface is UNKNOWN, not VERIFIED",
       executed_identity({"tasks": ["isa-monthly-prerun"]}, {"isa-monthly-prerun": "mirror"})["state"] == UNKNOWN)
    ok("R4.15 NEGATIVE CONTROL: a canonical_loaded surface establishes executed identity",
       executed_identity({"tasks": ["vci"]}, {"vci": "canonical_loaded"})["state"] == GREEN)
    ok("R4.15: a disposition naming no task cannot establish identity",
       executed_identity({"verdict": "VERIFIED_NO_CHANGE"}, {})["state"] == UNKNOWN)
    # R5.14
    with open(os.path.join(d, "clocky.py"), "w") as fh:
        fh.write("import datetime\ndef f():\n    return datetime.date.today()\n# PASS T-1 x\n")
    with open(os.path.join(d, "viaregister.py"), "w") as fh:
        fh.write("import isa_register as R\ndef f():\n    return R._today()\n")
    with open(os.path.join(d, "durations.py"), "w") as fh:
        fh.write("import time\ndef f():\n    return time.time()\n")
    ok("R5.14 detector: date.today() is date-dependent; time.time() is not",
       date_dependent(os.path.join(d, "clocky.py")) and not date_dependent(os.path.join(d, "durations.py")))
    ok("R5.14 detector MUST-FIRE (found 04-Oct): a clock read through another module's _today() is date-dependent",
       date_dependent(os.path.join(d, "viaregister.py")))
    g = temporal_gaps({}, ["clocky", "durations"], d)
    ok("R5.14 MUST-FIRE: a changed date-dependent module with no matrix fails; a non-dependent one is not asked",
       len(g) == 1 and "clocky" in g[0], g)
    g = temporal_gaps({"clocky": {"checks": {"T-1": "PASS T-1 x"}}}, ["clocky"], d)
    ok("R5.14 MUST-FIRE: a partial matrix (only T-1) fails on the four missing points", len(g) == 4, g)
    g = temporal_gaps({"clocky": {"na": "stamps as_of only; no decision depends on the date"}}, ["clocky"], d)
    ok("R5.14 NEGATIVE CONTROL: an N/A with a real reason passes", g == [], g)
    seen = []
    m = temporal_matrix(lambda dd: (seen.append(dd), dd >= datetime.date(2026, 9, 30))[1], datetime.date(2026, 9, 30))
    ok("R5.14 harness: T-1 False, T True, T+1 True, repeat equals T, next evaluated",
       m["T-1"] is False and m["T"] is True and m["T+1"] is True and m["repeat_same_occurrence"] == m["T"]
       and len(seen) == 5, m)
    print("run_contracts selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--check" in argv:
        errs = contract_errors(HERE) + failure_scope_gaps(HERE) + input_identity_gaps(HERE)
        for e in errs:
            print("FINDING " + e)
        print("run_contracts check: %d finding(s)" % len(errs))
        return 1 if errs else 0
    if "--envelope" in argv:
        print(json.dumps(runtime_envelope(argv[argv.index("--envelope") + 1], HERE), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
