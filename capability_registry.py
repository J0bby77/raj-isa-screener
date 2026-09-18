#!/usr/bin/env python3
"""
capability_registry.py — R15.6, the SEMANTIC map the Atlas cannot infer.

Authority: ISA_Engineering_Rules.md §15 (R15.5, R15.6), §4 (R4.14), §5 (R5.10), §20 (R20.1).
Raised as ISA-0628 under ISA-0623. Canonical store: `Dashboard/state/capability_registry.json`
— chosen once here and thereafter REFERENCED, never restated (R15.6, R4.4).

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS — and why the Atlas and the integrity layer both fail without it
═══════════════════════════════════════════════════════════════════════════════════════════
R15.5 says it in as many words: *"the Atlas is a structural analyser, not a certification
system"*. It answers "is this function reachable". `framework_integrity` answers "did this
function run, and from where". Neither can answer the three questions a capital decision
actually turns on:

    · what DECISION is this capability supposed to improve?
    · which STATES can that decision take, and can each of them actually be reached?
    · did the output reach a CONSUMER that could change the decision?

`position_sizing.stock_max` was reachable (the Atlas said so), instrumented (the ledger said
so), and had never run on the live capital path — because its only caller was a synthetic probe
that self-labels *"not a size anyone should act on"*. GBP 10,702.06 routed on the wrong
function while every structural instrument read green (ISA-0454). R4.14 is the rule that
closes it: a capability is LIVE only on **PRODUCED → EXECUTED → CONSUMED → DECISION-EFFECTIVE**,
and this module is where that chain is declared and measured.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ NOTHING HERE IS INVENTED (R7.5, R4.8)
═══════════════════════════════════════════════════════════════════════════════════════════
The registry is SEEDED from declarations that already exist — `quantity_register.json`'s
twelve quantities and their declared computers, surfaces, assertions and GBP exposure — and
every semantic field the framework has never recorded is written `UNDECLARED`, which the
reconciler reports as a gap. A plausible-looking consumer written where the truth is "nobody
has looked" would launder a backlog into a reassuring category, which is the mistake
`quantity_register.json` refused to make with `computer: null` (ISA-0448). This module makes
the same refusal.

⚑ AN OBSERVER MAY NOT MEASURE ITSELF (A12/R10). `capability_registry` is excluded from its own
consumer scan; `_selftest` carries the negative control proving the exclusion is load-bearing.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["capability_registry"] = False` — `reconcile()` returns
state `DISABLED` and every caller treats it as UNKNOWN rather than PASS (R4.3).
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_MODULE = "capability_registry"
STORE_REL = os.path.join("Dashboard", "state", "capability_registry.json")
SCHEMA_VERSION = "1.0.0"

# R4.14's four stages, in order. The order is load-bearing: a later stage cannot be green while
# an earlier one is red, and `live_state` reports the FIRST stage that fails rather than a
# blended score, because "it produces but nothing reads it" and "nothing produces it" are
# different facts with different fixes (R2.10).
STAGES = ("PRODUCED", "EXECUTED", "CONSUMED", "DECISION_EFFECTIVE")

PRODUCTION_STATES = ("TRUSTED", "CANDIDATE", "SHADOW", "NOT_LIVE")
UNDECLARED = "UNDECLARED"


class CapabilityRefused(RuntimeError):
    """Raised rather than returning an empty registry. A missing store is not a clean map."""


# ─────────────────────────────────────────────────────────────────────────────────────────
# ISA-0663 — SUBJECT AREAS. A subject that is not a module, declared and never inferred.
# ─────────────────────────────────────────────────────────────────────────────────────────
# R12.1 field 5 asks how a subject links together END TO END, which is inherently a
# cross-module question, while the only mechanical resolver — framework_atlas.footprint —
# takes a single module NAME. Every material subject in this framework spans modules, so
# until today the preflight could only orient on the subjects that least needed it, and a
# cross-module subject came back RED as "not in the Atlas" — a fact about the resolver
# wearing the appearance of a fact about the framework.
#
# ⚑ MEMBERSHIP IS DECLARED, NEVER DERIVED FROM THE NAME. A `vci*` prefix rule would have
#   silently dropped `position_sizing`, which holds vci_size_pct, budget_available,
#   binary_commitment, min_hold_ok and CATALYST_STATUSES — i.e. the entire sizing half of
#   the subject. R2.7 forbids joining records on inferred similarity and R4.8 refuses an
#   uninformed choice; an area whose membership is guessed is worse than no area, because
#   the omission is invisible in the receipt.

SUBJECT_AREAS_REL = os.path.join("Dashboard", "state", "subject_areas.json")


def subject_areas(root: str = HERE) -> dict:
    """The declared area map. Absent file = no areas, never an inferred one (R4.3)."""
    doc = _read_json(os.path.join(root, SUBJECT_AREAS_REL))
    return (doc or {}).get("areas") or {}


def resolve_subject(subject: str, root: str = HERE, modules=None) -> dict:
    """Resolve a preflight subject to a module set.

    Returns kind `module` (the subject IS a module), `area` (a DECLARED set), or
    `UNKNOWN`. UNKNOWN is the honest answer for a name nobody has declared and must stay
    that way: a subject the map does not contain cannot be discussed from the map (R2.8),
    and inventing a membership here would make an unoriented answer look oriented."""
    if modules is not None and subject in modules:
        return {"kind": "module", "subject": subject, "modules": [subject],
                "declared_by": "framework_atlas (module inventory on disk)"}
    areas = subject_areas(root)
    a = areas.get(subject)
    if a:
        known = [m for m in a.get("modules", []) if modules is None or m in modules]
        missing = [m for m in a.get("modules", []) if modules is not None and m not in modules]
        return {"kind": "area", "subject": subject, "modules": known,
                "declared_missing_from_disk": missing,
                "label": a.get("label"), "decision_path": a.get("decision_path"),
                "declared_by": a.get("declared_by"),
                "excluded_with_reason": a.get("excluded_with_reason") or {}}
    return {"kind": "UNKNOWN", "subject": subject, "modules": [],
            "why": ("%r is neither a module on disk nor a declared subject area. R4.8: an "
                    "uninformed resolution is REFUSED, not guessed — a name-prefix match "
                    "would have dropped position_sizing from the `vci` area, which is where "
                    "half that subject's capital logic lives." % subject)}


def store_path(root: str = HERE) -> str:
    return os.path.join(root, STORE_REL)


def _today() -> str:
    return datetime.date.today().isoformat()


def _flag() -> bool:
    try:
        import isa_policy as pol
        return bool(pol.flag("capability_registry"))
    except Exception:                                                   # noqa: BLE001
        return True


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None
    except json.JSONDecodeError as exc:
        raise CapabilityRefused(
            "%s is unreadable (%s). An unreadable registry is UNKNOWN, never empty (R4.3)."
            % (os.path.basename(path), exc))


# ────────────────────────────────────────────────────────────────────────────────────────
# the store
# ────────────────────────────────────────────────────────────────────────────────────────

def load(root: str = HERE) -> dict:
    """The registry, or a refusal. Never a silently empty map (R4.3, R2.10)."""
    doc = _read_json(store_path(root))
    if doc is None:
        raise CapabilityRefused(
            "capability registry absent at %s. R15.6 requires one machine-readable "
            "Decision/Capability Registry beside the Atlas; run "
            "`python3 capability_registry.py --seed` to build it from the declarations that "
            "already exist." % store_path(root))
    if "capabilities" not in doc:
        raise CapabilityRefused(
            "capability registry at %s has no `capabilities` key — the shape changed and this "
            "reader did not. BLIND, not clean." % store_path(root))
    return doc


def save(doc: dict, root: str = HERE) -> str:
    p = store_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    return p


# ────────────────────────────────────────────────────────────────────────────────────────
# seeding — from declarations that exist, never from imagination
# ────────────────────────────────────────────────────────────────────────────────────────

def _decision_states() -> List[str]:
    """The canonical action vocabulary, read from its one home (R4.4)."""
    try:
        sys.path.insert(0, HERE)
        import action_language as al
        states = list(getattr(al, "CANONICAL_ACTIONS", []))
        if states:
            return states
    except Exception:                                                   # noqa: BLE001
        pass
    return []


def seed_from_declarations(root: str = HERE) -> dict:
    """Build the registry from `quantity_register.json`, marking every unrecorded field
    `UNDECLARED`.

    ⚑ The quantity register already declares, per quantity: the ONE function that computes it,
    the surfaces it renders on, the assertion that guards it, and its GBP exposure. Those are
    facts on disk. What it does not carry — the decision the quantity improves, the states that
    decision can take, the module that CONSUMES it, and the fixture that proves a state can
    fire — is exactly what R15.6 adds, and exactly what must not be guessed.
    """
    qr = _read_json(os.path.join(root, "quantity_register.json"))
    if not qr or "quantities" not in qr:
        raise CapabilityRefused(
            "quantity_register.json is absent or has no `quantities` — the registry cannot be "
            "seeded from declarations, and seeding it from anything else would be invention "
            "(R7.5).")
    states = _decision_states()
    caps = []
    for q in qr["quantities"]:
        computer = q.get("computer")
        caps.append({
            "id": "CAP-" + q["name"],
            "name": q["name"],
            "purpose": q.get("note") or UNDECLARED,
            "economic_decision": UNDECLARED,
            "inputs": UNDECLARED,
            "producer": computer or UNDECLARED,
            "outputs": list(q.get("surface") or []),
            "consumers": UNDECLARED,
            "orchestrators": UNDECLARED,
            "decision_states": states if states else UNDECLARED,
            "must_fire": UNDECLARED,
            "liveness_ref": q.get("assertion") or UNDECLARED,
            "consumption_ref": UNDECLARED,
            "decision_effective_ref": UNDECLARED,
            "constraints": UNDECLARED,
            "production_status": "NOT_LIVE" if not computer else UNDECLARED,
            "gbp_exposure": q.get("gbp_exposure"),
            "units": q.get("units") or UNDECLARED,
            "standard_refs": ["R15.6", "R4.14", "R5.10"],
            "seeded_from": "quantity_register.json",
            "as_of": _today(),
        })
    return {
        "_what": ("R15.6 Decision/Capability Registry — the semantic map beside the Atlas. For "
                  "each material capability: what decision it improves, which states that "
                  "decision can take, who consumes its output, and the fixture that proves a "
                  "state can actually fire."),
        "_authority": "ISA_Engineering_Rules.md R15.6 / R4.14 / R5.10 (adopted 09-Sep-2026, ISA-0623)",
        "_raised": "ISA-0628",
        "_rule_undeclared": ("UNDECLARED means nobody has recorded it. It is a GAP the reconciler "
                             "reports, never a value. Writing a plausible consumer where the "
                             "truth is 'nobody has looked' would launder a backlog into a "
                             "reassuring category (ISA-0448's refusal, applied here)."),
        "_rule_chain": ("R4.14: LIVE requires PRODUCED -> EXECUTED -> CONSUMED -> "
                        "DECISION-EFFECTIVE through the real path. Static reachability alone may "
                        "never close FC-E or FC-K (R15.5)."),
        "schema_version": SCHEMA_VERSION,
        "as_of": _today(),
        "capabilities": caps,
    }


# ────────────────────────────────────────────────────────────────────────────────────────
# the R4.14 evidence chain
# ────────────────────────────────────────────────────────────────────────────────────────

def _functions_on_disk(root: str = HERE) -> set:
    """`module.function` for every function in the tree, excluding this module (A12)."""
    out = set()
    try:
        import framework_integrity as fi
        for path in fi.source_files(root):
            _, tree = fi.parsed(path)
            if tree is None:
                continue
            m = fi.modname(path)
            if m == SELF_MODULE:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add("%s.%s" % (m, node.name))
    except Exception:                                                   # noqa: BLE001
        return set()
    return out


def _live_calls(ledger: Optional[dict] = None) -> Dict[str, int]:
    """`module.function` -> count of calls the execution ledger attributes to a LIVE run."""
    try:
        import framework_integrity as fi
        led = ledger if ledger is not None else fi.load_ledger()
    except Exception:                                                   # noqa: BLE001
        return {}
    out = {}
    for qname, rec in (led.get("records") or {}).items():
        kinds = rec.get("kinds") or {}
        n = int(kinds.get("live_run", 0))
        if n:
            out[qname] = n
    return out


def _readers_of(key: str, root: str = HERE) -> List[str]:
    """Modules that NAME `key` as a string literal in a function body — a candidate consumer.

    ⚑ CANDIDATE, NOT PROOF, AND THE DIFFERENCE IS DECLARED. Naming a key is evidence that a
    module could read it; only a declared `consumption_ref` proves the read happens on the live
    path. This function exists so that `consumers: UNDECLARED` can be reported alongside "and
    here are the modules that look like they read it", which is the difference between a gap a
    person can close in a minute and one they have to go looking for.
    """
    out = []
    try:
        import framework_integrity as fi
        pat = re.compile(r"(?<![\w.])" + re.escape(key) + r"(?![\w.])")
        for path in fi.source_files(root):
            m = fi.modname(path)
            if m == SELF_MODULE:
                continue
            src, tree = fi.parsed(path)
            if tree is None:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                doc = ast.get_docstring(fn, clean=False)
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and n.value != doc and pat.search(n.value)):
                        out.append(m)
                        break
                else:
                    continue
                break
    except Exception:                                                   # noqa: BLE001
        return []
    return sorted(set(out))


# ─────────────────────────────────────────────────────────────────────────────────────────
# ISA-0628 (16-Sep-2026) — EVIDENCE IS A TYPED REFERENCE, NEVER A SENTENCE
# ─────────────────────────────────────────────────────────────────────────────────────────
# ⚑ THE FALSE-GREEN THIS CLOSES, MEASURED ON TB-2026-09-16-01. The chain tested
#   `ref in (None, "", UNDECLARED)`, so ANY other string counted as proof. Five CANDIDATE
#   capabilities scored DECISION_EFFECTIVE = GREEN on the prose "UNDECLARED — no live ... has yet
#   ..." and CONSUMED = GREEN on "PROVEN 12-Sep-2026: ..."; vci_size_pct's `consumers` was the
#   prose "UNDECLARED — this is the gap ISA-0646 records", read as a declared consumer. Only
#   EXECUTED being red first kept them from reading LIVE. A sentence ABOUT evidence is not
#   evidence ([[prose is not a mechanism]]).
# ⚑ THE CONTRACT. A reference is either UNDECLARED (None, "", or any string beginning
#   "UNDECLARED") or a dict {"kind": "check", "ref": "module.function"}: the function must exist on
#   disk AND its module's suite must be GREEN in the census recorded against THIS source
#   (consistency_check.record_suite_status / pair_red_suite_is_an_incident). Anything else is RED
#   `PROSE_NOT_EVIDENCE` — recorded, readable, and never a pass.
EVIDENCE_KINDS = ("check",)
NOT_APPLICABLE_KIND = "not_applicable"
SEMANTIC_FIELDS = ("economic_decision", "inputs", "consumers", "orchestrators", "must_fire",
                   "consumption_ref", "decision_effective_ref", "constraints", "production_status")
# ⚑ ISA-0699 (b/c), BS-0699-BC §13: a RENDERER prints a decision; it never makes one. A check that
#   lives in, or traverses only, a renderer cannot prove DECISION-EFFECTIVE.
RENDERER_MODULES = ("email_prefill", "build_monthly_isa_email", "build_email", "build_excel",
                    "isa_register_render", "isa_register_export")


def _module_ast(mod: str, root: str = HERE):
    try:
        import framework_integrity as fi
        for path in fi.source_files(root):
            if fi.modname(path) == mod:
                return fi.parsed(path)[1]
    except Exception:                                                   # noqa: BLE001
        return None
    return None


def _import_aliases(tree) -> Dict[str, str]:
    """alias -> module (import m as a / from m import f as g -> g: 'm.f')."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                out[a.asname or a.name] = "%s.%s" % (n.module, a.name)
    return out


def traverses(check_qname: str, via_qname: str, root: str = HERE, depth: int = 4) -> bool:
    """AST: does `check_qname` call `via_qname`, directly or through same-module helpers (<= depth)?
    A check that resolves but never enters the real orchestrator proves the helper, not the path
    (BS-0699-BC §13). Calls are resolved through the module's import aliases, so `_cd.sleeve_split`
    counts for capital_destination.sleeve_split and a same-named function elsewhere does not."""
    cmod, _, cfn = check_qname.rpartition(".")
    vmod, _, vfn = via_qname.rpartition(".")
    tree = _module_ast(cmod, root)
    if tree is None:
        return False
    fns = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    aliases = _import_aliases(tree)
    seen, frontier = set(), [cfn]
    for _ in range(depth + 1):
        nxt = []
        for name in frontier:
            if name in seen or name not in fns:
                continue
            seen.add(name)
            for n in ast.walk(fns[name]):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    if f.attr == vfn and aliases.get(f.value.id, f.value.id) == vmod:
                        return True
                elif isinstance(f, ast.Name):
                    if cmod == vmod and f.id == vfn:
                        return True
                    if aliases.get(f.id) == via_qname:
                        return True
                    nxt.append(f.id)
        frontier = nxt
    return False


def referenced_in_module(check_qname: str, root: str = HERE) -> bool:
    """The check's name is used somewhere in its module other than its own def (a selftest call or a
    registry the selftest runs). An unreferenced check is never executed by its suite, so a GREEN
    suite says nothing about it."""
    mod, _, fn = check_qname.rpartition(".")
    tree = _module_ast(mod, root)
    if tree is None:
        return False
    return any((isinstance(n, ast.Name) and n.id == fn) or
               (isinstance(n, ast.Attribute) and n.attr == fn) for n in ast.walk(tree))


def _is_undeclared(v) -> bool:
    if v is None or v == "" or v == [] or v == {}:
        return True
    return isinstance(v, str) and v.strip().upper().startswith(UNDECLARED)


def _suite_states(root: str = HERE) -> Dict[str, str]:
    """{module: state} from the suite census, ONLY if it was recorded against this source."""
    try:
        import consistency_check as _cc
        doc = _read_json(os.path.join(root, _cc.SUITE_STATUS_REL))
        if not doc or doc.get("source_roll") is None or \
                doc.get("source_roll") != _cc._source_roll(root):
            return {}
        return {r.get("module"): r.get("state") for r in doc.get("rows", [])}
    except Exception:                                                   # noqa: BLE001
        return {}


def ref_evidence(ref, on_disk: set, suites: Dict[str, str], *, cap_id: Optional[str] = None,
                 stage: Optional[str] = None, root: str = HERE, check_ast: bool = True) -> dict:
    """-> {"state": GREEN|RED|UNDECLARED|NOT_APPLICABLE, "why": ...} for one evidence reference."""
    if _is_undeclared(ref):
        return {"state": "UNDECLARED", "why": "no evidence reference is recorded"}
    if not isinstance(ref, dict):
        return {"state": "RED", "kind": "PROSE_NOT_EVIDENCE",
                "why": ("the reference is prose (%r) — a sentence about evidence is not evidence. "
                        "Record {'kind': 'check', 'ref': 'module.function'} naming a check whose "
                        "suite is GREEN on this source" % str(ref)[:90])}
    if ref.get("kind") == NOT_APPLICABLE_KIND:
        if stage != "DECISION_EFFECTIVE":
            return {"state": "RED", "why": "not_applicable is only a DECISION-EFFECTIVE disposition"}
        if not ref.get("why") or not re.match(r"^ISA-\d{4}$", str(ref.get("item") or "")):
            return {"state": "RED", "why": ("a not_applicable disposition must name why AND an ISA item "
                                            "(an unowned 'not applicable' is a silent gap)")}
        _nev = ref.get("evidence")
        if _nev is not None:
            _e = ref_evidence(_nev, on_disk, suites, cap_id=cap_id, stage="NEGATIVE_SCOPE",
                              root=root, check_ast=check_ast)
            if _e["state"] != "GREEN":
                return {"state": "RED", "why": "not_applicable evidence check: " + _e["why"]}
        return {"state": "NOT_APPLICABLE", "item": ref.get("item"),
                "why": "declared not decision-effective (%s): %s" % (ref.get("item"), ref.get("why"))}
    if ref.get("kind") not in EVIDENCE_KINDS:
        return {"state": "RED", "why": "evidence kind %r is not one of %s"
                                       % (ref.get("kind"), list(EVIDENCE_KINDS))}
    fn = str(ref.get("ref") or "")
    if fn not in on_disk:
        return {"state": "RED", "why": "check %s is not a function in the tree" % fn}
    mod = fn.split(".", 1)[0]
    _caps = ref.get("capability")
    if _caps is not None and cap_id is not None:
        if cap_id not in (_caps if isinstance(_caps, list) else [_caps]):
            return {"state": "RED", "kind": "CAPABILITY_MISMATCH",
                    "why": ("check %s is declared for %s, not %s — evidence reused across capabilities "
                            "it was not written for (BS-0699-BC §13)" % (fn, _caps, cap_id))}
    via = ref.get("via")
    if stage == "DECISION_EFFECTIVE":
        if not via:
            return {"state": "RED", "kind": "VIA_REQUIRED",
                    "why": ("decision_effective_ref names no `via` orchestrator — a helper call is not "
                            "the production path (BS-0699-BC §8)")}
        if mod in RENDERER_MODULES or str(via).split(".", 1)[0] in RENDERER_MODULES:
            return {"state": "RED", "kind": "RENDERER_NOT_DECISION",
                    "why": "a renderer (%s) prints decisions; it cannot prove one was made" % (fn if mod in RENDERER_MODULES else via)}
    if via:
        if via not in on_disk:
            return {"state": "RED", "why": "via orchestrator %s is not a function in the tree" % via}
        if check_ast and not traverses(fn, via, root):
            return {"state": "RED", "kind": "DOES_NOT_TRAVERSE_ORCHESTRATOR",
                    "why": "check %s never calls %s (AST, through same-module helpers)" % (fn, via)}
    if check_ast and not referenced_in_module(fn, root):
        return {"state": "RED", "kind": "CHECK_NEVER_RUN",
                "why": "check %s is not referenced in its module, so its suite never runs it" % fn}
    st = suites.get(mod)
    if st != "GREEN":
        return {"state": "RED",
                "why": ("check %s exists but its suite is %s in the census recorded against this "
                        "source — an unrun or red check proves nothing (R5.4, R5.7)"
                        % (fn, st or "NOT RECORDED"))}
    return {"state": "GREEN", "why": "check %s%s exists and its suite is GREEN on this source"
                                     % (fn, (" via " + via) if via else "")}


def evidence_chain(cap: dict, on_disk: set, live: Dict[str, int],
                   root: str = HERE, suites: Optional[Dict[str, str]] = None,
                   check_ast: bool = True, bound: Optional[Dict[str, dict]] = None) -> dict:
    """R4.14 for one capability. Each stage is GREEN / RED / UNDECLARED with its reason."""
    producer = cap.get("producer")
    chain = {}
    suites = _suite_states(root) if suites is None else suites

    # PRODUCED — the producer exists on disk and is named.
    if not producer or producer == UNDECLARED:
        chain["PRODUCED"] = {"state": "UNDECLARED",
                             "why": "no producer is declared for this capability"}
    elif producer in on_disk:
        chain["PRODUCED"] = {"state": "GREEN", "why": "%s exists on disk" % producer}
    else:
        chain["PRODUCED"] = {"state": "RED",
                             "why": ("declared producer %s is not a function in the tree — the "
                                     "registry names something that does not exist" % producer)}

    # EXECUTED — the execution ledger attributes a LIVE call to it. A test caller is not a
    # live caller; that distinction is the whole of ISA-0454.
    n = live.get(producer or "", 0)
    _b = None if bound is None else (bound.get(producer or "") or {})
    if chain["PRODUCED"]["state"] != "GREEN":
        chain["EXECUTED"] = {"state": "BLOCKED",
                             "why": "PRODUCED is not green; execution is not asked"}
    elif _b is not None and _b.get("calls"):
        chain["EXECUTED"] = {"state": "GREEN",
                             "why": "%d live_run call(s) in %d AUTHORISED non-dry-run capital run(s): %s"
                                    % (_b["calls"], len(_b.get("runs") or []), ", ".join((_b.get("runs") or [])[:3]))}
    elif _b is not None and n:
        # ⚑ ISA-0699 (17-Sep-2026): the monthly ledger counts live_run calls with no run identity, so a
        #   rehearsal or a pre-ISA-0704 dry run reads exactly like the scheduled run. Unbound calls are
        #   information, not EXECUTED evidence (BS-0699-BC §13 stale/foreign evidence).
        chain["EXECUTED"] = {"state": "RED", "kind": "EXECUTION_NOT_RUN_BOUND", "unbound_live_run_calls": n,
                             "why": ("%d live_run call(s) recorded from the production orchestrator, but none "
                                     "bound to an AUTHORISED non-dry-run capital run (framework_integrity."
                                     "bind_run). Real-run observation is ISA-0699 leg (d)" % n)}
    elif n:
        chain["EXECUTED"] = {"state": "GREEN",
                             "why": "%d live_run call(s) in the execution ledger" % n}
    else:
        chain["EXECUTED"] = {"state": "RED",
                             "why": ("no live_run call recorded. REACHABLE_NOT_LIVE is the "
                                     "shape that routed GBP 10,702.06 on the wrong function "
                                     "(ISA-0454) — a test or probe caller is not a live caller")}

    # CONSUMED — a declared consumer, not an inferred one.
    cons = cap.get("consumers")
    if _is_undeclared(cons):
        cand = []
        for out_key in (cap.get("outputs") or []):
            cand += _readers_of(out_key, root)
        chain["CONSUMED"] = {
            "state": "UNDECLARED",
            "why": ("no consumer is declared. R15.6 asks who reads the output; the Atlas cannot "
                    "infer it and this module refuses to guess it"),
            "candidates": sorted(set(cand)) or None}
    elif _is_undeclared(cap.get("consumption_ref")):
        chain["CONSUMED"] = {"state": "RED",
                             "why": ("consumers are named (%s) but no `consumption_ref` proves "
                                     "the read happens on the live path — a declared reader is "
                                     "an intention until a check names it (R5.4)"
                                     % ", ".join(cons if isinstance(cons, list) else [str(cons)]))}
    else:
        _ev = ref_evidence(cap["consumption_ref"], on_disk, suites, cap_id=cap.get("id"),
                           stage="CONSUMED", root=root, check_ast=check_ast)
        chain["CONSUMED"] = {"state": "GREEN" if _ev["state"] == "GREEN" else "RED",
                             "why": "consumption_ref: " + _ev["why"]}

    # DECISION-EFFECTIVE — a must-fire fixture (R5.10) that actually reached the state.
    mf = cap.get("must_fire")
    der = cap.get("decision_effective_ref")
    if isinstance(der, dict) and der.get("kind") == NOT_APPLICABLE_KIND:
        _ev = ref_evidence(der, on_disk, suites, cap_id=cap.get("id"), stage="DECISION_EFFECTIVE",
                           root=root, check_ast=check_ast)
        chain["DECISION_EFFECTIVE"] = {"state": _ev["state"], "why": "decision_effective_ref: " + _ev["why"]}
    elif _is_undeclared(mf):
        chain["DECISION_EFFECTIVE"] = {
            "state": "UNDECLARED",
            "why": ("R5.10: every decision state the framework claims it can make needs at "
                    "least one controlled fixture in which that state MUST be reached through "
                    "the real orchestration. None is declared, so the claim is untested")}
    elif _is_undeclared(der):
        chain["DECISION_EFFECTIVE"] = {
            "state": "RED",
            "why": ("must-fire fixtures are declared but no `decision_effective_ref` records "
                    "one firing. A fixture that has never made the state fire does not prove "
                    "the state can be reached (R5.10)")}
    else:
        _ev = ref_evidence(der, on_disk, suites, cap_id=cap.get("id"), stage="DECISION_EFFECTIVE",
                           root=root, check_ast=check_ast)
        chain["DECISION_EFFECTIVE"] = {"state": "GREEN" if _ev["state"] == "GREEN" else "RED",
                                       "why": "decision_effective_ref: " + _ev["why"]}
    # ⚑ ISA-0465/0700 (16-Sep-2026): a SHADOW capability moves no capital BY DESIGN. However
    #   good its evidence, it cannot be DECISION-EFFECTIVE until promoted out of SHADOW; a green
    #   here would be a false LIVE claim manufactured from a comparison run (R4.14, R18.2).
    if cap.get("production_status") == "SHADOW" and chain["DECISION_EFFECTIVE"]["state"] == "GREEN":
        chain["DECISION_EFFECTIVE"] = {"state": "RED", "kind": "SHADOW_NOT_DECISION_EFFECTIVE",
                                       "why": ("production_status SHADOW: the capability runs without "
                                               "moving capital, so it is not decision-effective until a "
                                               "LIVE decision promotes it (R18.2)")}
    return chain


def _open_item_ids(root: str = HERE):
    """Ids of non-terminal register items, or None when the register cannot be read."""
    try:
        sys.path.insert(0, root)
        import isa_register as _R
        return {i["id"] for i in _R.read_all()
                if i.get("state") in ("OPEN", "IN_PROGRESS", "BLOCKED_ON_RAJ", "DEFERRED")}
    except Exception:                                                   # noqa: BLE001
        return None


def _not_live_disposition(cap: dict, ls: dict, open_items) -> dict:
    """C7: a non-LIVE capability names {"item": "ISA-xxxx", "since": "YYYY-MM-DD", "why": ...}."""
    if ls.get("live"):
        return {"state": "LIVE"}
    r = cap.get("not_live_reason")
    if not isinstance(r, dict) or not r.get("item") or not r.get("since"):
        return {"state": "UNDISPOSITIONED",
                "why": "no dated not_live_reason naming a register item (ISA-0467 C7)"}
    if open_items is None:
        return {"state": "UNKNOWN", "item": r.get("item"), "why": "register unreadable"}
    if r["item"] not in open_items:
        return {"state": "STALE", "item": r["item"],
                "why": "the named item is not OPEN - the reason no longer owns the gap"}
    return {"state": "REGISTERED", "item": r["item"], "since": r["since"], "why": r.get("why")}


def live_state(cap: dict, chain: dict) -> dict:
    """LIVE only on four greens. Otherwise the FIRST stage that is not green, and why."""
    for st in STAGES:
        s = chain[st]["state"]
        if s != "GREEN":
            return {"live": False, "blocked_at": st, "state": s, "why": chain[st]["why"]}
    return {"live": True, "blocked_at": None, "state": "GREEN",
            "why": "PRODUCED -> EXECUTED -> CONSUMED -> DECISION-EFFECTIVE all green (R4.14)"}


def reconcile(root: str = HERE, ledger: Optional[dict] = None,
              registry: Optional[dict] = None) -> dict:
    """Every capability's R4.14 chain, ranked by GBP exposure. The KR12 input."""
    if not _flag():
        return {"state": "DISABLED",
                "why": ("isa_policy.V2_FLAGS['capability_registry'] is False. DISABLED is "
                        "reported as UNKNOWN by every caller and never as PASS (R4.3)")}
    doc = registry if registry is not None else load(root)
    on_disk = _functions_on_disk(root)
    live = _live_calls(ledger)
    try:
        import framework_integrity as _fib
        bound = _fib.bound_live_calls(ledger)
    except Exception:                                                   # noqa: BLE001
        bound = {}
    suites = _suite_states(root)
    open_items = _open_item_ids(root)
    rows = []
    for cap in doc["capabilities"]:
        chain = evidence_chain(cap, on_disk, live, root, suites=suites, bound=bound)
        ls = live_state(cap, chain)
        rows.append({"id": cap["id"], "name": cap["name"],
                     "gbp_exposure": cap.get("gbp_exposure"),
                     "producer": cap.get("producer"),
                     "production_status": cap.get("production_status"),
                     "not_live_reason": _not_live_disposition(cap, ls, open_items),
                     "chain": chain, "live": ls})
    rows.sort(key=lambda r: -(r["gbp_exposure"] or 0.0))
    n_live = sum(1 for r in rows if r["live"]["live"])
    by_stage = {}
    for r in rows:
        if not r["live"]["live"]:
            by_stage.setdefault(r["live"]["blocked_at"], []).append(r["name"])
    exposure_not_live = sum((r["gbp_exposure"] or 0.0) for r in rows if not r["live"]["live"])
    # ⚑ ISA-0467 C7 — "every capability carrying declared GBP exposure is either LIVE on the R4.14
    #   chain or has a dated, registered reason it is not", measured rather than asserted.
    _c7_missing = [r["name"] for r in rows if not r["live"]["live"] and (r["gbp_exposure"] or 0) > 0
                   and r["not_live_reason"]["state"] != "REGISTERED"]
    c7 = {"state": ("UNKNOWN" if open_items is None else ("MET" if not _c7_missing else "NOT_MET")),
          "undispositioned": _c7_missing,
          "why": ("register unreadable - C7 cannot be measured (R4.3)" if open_items is None else
                  "%d exposure-carrying capability(ies) are neither LIVE nor carry a dated reason "
                  "naming an OPEN register item" % len(_c7_missing))}
    _unobs = unobserved_producers(root, doc)
    _unobs_ids = {r["id"] for r in _unobs.get("rows") or []}
    _decl = declarations(root, doc)
    _undecl_ids = {r["id"] for r in _decl["unexplained_undeclared"]}
    _all_undecl = {r["id"] for r in undeclared_fields(root, doc)}
    # ⚑ BS-0699-BC §12 — denominators BY STATE; no roll-up hides the failed stage.
    denominators = {
        "total": len(rows),
        "fully_declared": sum(1 for r in rows if r["id"] not in _all_undecl),
        "declared_or_explicitly_unresolved": sum(1 for r in rows if r["id"] not in _undecl_ids),
        "instrumented": sum(1 for r in rows if r["id"] not in _unobs_ids and r["producer"]
                            and not _is_undeclared(r["producer"])),
        "consumed_check_passing": sum(1 for r in rows if r["chain"]["CONSUMED"]["state"] == "GREEN"),
        "decision_check_passing": sum(1 for r in rows if r["chain"]["DECISION_EFFECTIVE"]["state"] == "GREEN"),
        "decision_not_applicable": sum(1 for r in rows
                                       if r["chain"]["DECISION_EFFECTIVE"]["state"] == "NOT_APPLICABLE"),
        "real_run_observed": sum(1 for r in rows if r["chain"]["EXECUTED"]["state"] == "GREEN"),
        "production_orchestrator_calls_unbound": sum(1 for r in rows
                                                     if r["chain"]["EXECUTED"].get("kind") == "EXECUTION_NOT_RUN_BOUND"),
        "live": n_live,
        "registered_non_live": sum(1 for r in rows if not r["live"]["live"]
                                   and r["not_live_reason"]["state"] == "REGISTERED"),
        "by_stage_state": {st: {k: sum(1 for r in rows if r["chain"][st]["state"] == k)
                                for k in sorted({r["chain"][st]["state"] for r in rows})}
                           for st in STAGES},
    }
    return {
        "c7": c7,
        "declarations": _decl,
        "denominators": denominators,
        "unobserved_producers": _unobs,
        "state": "PASS" if n_live == len(rows) and rows else "FAIL",
        "as_of": _today(),
        "n_capabilities": len(rows),
        "n_live": n_live,
        "n_not_live": len(rows) - n_live,
        "gbp_exposure_not_live": round(exposure_not_live, 2),
        "blocked_at": by_stage,
        "rows": rows,
        "basis": ("R4.14. A capability is LIVE only when evidence proves PRODUCED -> EXECUTED -> "
                  "CONSUMED -> DECISION-EFFECTIVE through the real intended path. The Atlas "
                  "answers only the first (R15.5)."),
    }


def unobserved_producers(root: str = HERE, registry: Optional[dict] = None) -> dict:
    """ISA-0699 — a declared producer the execution ledger can never observe (absent from
    framework_integrity.CAPITAL_PATH_MANIFEST, or with no `_mark` site) reports EXECUTED RED for ever,
    which reads as 'did not run' when the truth is 'not instrumented' (R2.10). -> {state, rows}."""
    doc = registry if registry is not None else load(root)
    try:
        import framework_integrity as _fi
        man = {"%s.%s" % (m, f) for m, f, _, _ in _fi.CAPITAL_PATH_MANIFEST}
        sites = _fi.mark_sites(root)
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNKNOWN", "why": "framework_integrity unavailable: %s" % exc, "rows": []}
    rows = []
    for cap in doc["capabilities"]:
        p = cap.get("producer")
        if _is_undeclared(p):
            continue
        if p not in man or not sites.get(p):
            rows.append({"id": cap["id"], "producer": p, "in_manifest": p in man,
                         "marked": bool(sites.get(p))})
    return {"state": "PASS" if not rows else "FAIL", "rows": rows}


def must_fire_gaps(root: str = HERE, registry: Optional[dict] = None) -> List[dict]:
    """KR12 / R5.10 — capabilities claiming decision states with no must-fire fixture."""
    doc = registry if registry is not None else load(root)
    out = []
    for cap in doc["capabilities"]:
        states = cap.get("decision_states")
        if not states or states == UNDECLARED:
            continue
        mf = cap.get("must_fire")
        declared = set()
        if isinstance(mf, list):
            declared = {m.get("state") for m in mf if isinstance(m, dict)}
        missing = [s for s in states if s not in declared]
        if missing:
            out.append({"id": cap["id"], "name": cap["name"],
                        "gbp_exposure": cap.get("gbp_exposure"),
                        "states_without_a_must_fire_fixture": missing,
                        "why": ("R5.10: if no fixture can make the state fire, the capability is "
                                "not live regardless of code coverage")})
    out.sort(key=lambda r: -(r["gbp_exposure"] or 0.0))
    return out


def _field_undeclared(cap: dict, f: str) -> bool:
    v = cap.get(f)
    return v is None or v == "" or v == [] or (isinstance(v, str) and v.strip().upper().startswith(UNDECLARED))


def undeclared_fields(root: str = HERE, registry: Optional[dict] = None) -> List[dict]:
    """Every UNDECLARED semantic field, ranked by exposure — the honest backlog of this build.
    A field listed in the capability's `unresolved` map is still reported (it IS undeclared) but
    carries its recorded reason, so 'nobody looked' and 'traced, cannot be declared' differ."""
    doc = registry if registry is not None else load(root)
    out = []
    for cap in doc["capabilities"]:
        gaps = [f for f in SEMANTIC_FIELDS if _field_undeclared(cap, f)]
        if gaps:
            unr = cap.get("unresolved") or {}
            out.append({"id": cap["id"], "name": cap["name"],
                        "gbp_exposure": cap.get("gbp_exposure"), "undeclared": gaps,
                        "unexplained": [f for f in gaps if not _explained(unr.get(f))],
                        "unresolved": {f: unr.get(f) for f in gaps if _explained(unr.get(f))}})
    out.sort(key=lambda r: -(r["gbp_exposure"] or 0.0))
    return out


def _explained(reason) -> bool:
    return isinstance(reason, str) and len(reason.strip()) >= 20 and bool(re.search(r"ISA-\d{4}", reason))


def declarations(root: str = HERE, registry: Optional[dict] = None) -> dict:
    """ISA-0699 leg (b), BS-0699-BC §15: every capability is TRULY declared or EXPLICITLY unresolved.

    FAIL when (i) any semantic field is UNDECLARED without an `unresolved` reason naming an ISA item —
    so a newly added capability with blank semantics fails reconciliation instead of silently joining
    the denominator — or (ii) a capability id in the registry's `_denominator_ids` floor is missing,
    so the denominator cannot shrink by deletion."""
    doc = registry if registry is not None else load(root)
    rows = [r for r in undeclared_fields(root, doc) if r["unexplained"]]
    ids = {c["id"] for c in doc["capabilities"]}
    floor = doc.get("_denominator_ids")
    missing = sorted(set(floor or []) - ids)
    no_floor = not isinstance(floor, list) or not floor
    state = "FAIL" if (rows or missing or no_floor) else "PASS"
    return {"state": state, "n_capabilities": len(ids),
            "unexplained_undeclared": [{"id": r["id"], "fields": r["unexplained"]} for r in rows],
            "denominator_floor_missing": missing,
            "why": ("no _denominator_ids floor is recorded" if no_floor else
                    "%d capability(ies) carry UNDECLARED fields with no recorded reason; %d floor id(s) "
                    "missing" % (len(rows), len(missing)))}


def report(root: str = HERE, ledger: Optional[dict] = None, cap: int = 10) -> dict:
    rec = reconcile(root, ledger)
    if rec.get("state") == "DISABLED":
        return rec
    gaps = undeclared_fields(root)
    mf = must_fire_gaps(root)
    return {
        "as_of": _today(),
        "state": rec["state"],
        "n_capabilities": rec["n_capabilities"],
        "n_live": rec["n_live"],
        "n_not_live": rec["n_not_live"],
        "gbp_exposure_not_live": rec["gbp_exposure_not_live"],
        "blocked_at": rec["blocked_at"],
        "top": rec["rows"][:cap],
        "suppressed": {"count": max(0, len(rec["rows"]) - cap),
                       "line": "%d further capability row(s) suppressed"
                               % max(0, len(rec["rows"]) - cap)},
        "must_fire_gaps": mf[:cap],
        "n_must_fire_gaps": len(mf),
        "undeclared_fields": gaps[:cap],
        "n_with_undeclared_fields": len(gaps),
        "declarations": rec.get("declarations"),
        "denominators": rec.get("denominators"),
        "renders_in": "email §11 framework health / the deferred dashboard (§20) — never §2",
    }


# ────────────────────────────────────────────────────────────────────────────────────────
# selftest
# ────────────────────────────────────────────────────────────────────────────────────────

_ASSERTS = [0]


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        _ASSERTS[0] += 1
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:220]) if not cond else ""))
        if not cond:
            fails.append(name)

    on_disk = {"m.produces", "m.consumes"}

    def _ec(*a, **k):   # synthetic 'm' module: no source to AST-verify, so AST checks are off here
        return evidence_chain(*a, check_ast=False, **k)

    def cap(**kw):
        base = {"id": "CAP-x", "name": "x_gbp", "producer": "m.produces",
                "outputs": ["email.s2.x"], "consumers": UNDECLARED,
                "consumption_ref": UNDECLARED, "must_fire": UNDECLARED,
                "decision_effective_ref": UNDECLARED, "decision_states": ["BUY", "HOLD"],
                "gbp_exposure": 1000.0}
        base.update(kw)
        return base

    # ── the chain refuses at the FIRST red, and names which stage ─────────────────────
    c = _ec(cap(producer="m.missing"), on_disk, {"m.missing": 5}, HERE)
    ok("a producer that is not on disk is PRODUCED:RED, not a silent pass",
       c["PRODUCED"]["state"] == "RED", c["PRODUCED"])
    ok("...and EXECUTED is BLOCKED rather than green off a ledger entry - a ledger row for a "
       "function that does not exist must not manufacture evidence",
       c["EXECUTED"]["state"] == "BLOCKED", c["EXECUTED"])

    c = _ec(cap(), on_disk, {}, HERE)
    ok("⚑ THE ISA-0454 SHAPE: on disk, declared, and never run from a live caller is "
       "EXECUTED:RED - reachable is not live (R15.5)",
       c["PRODUCED"]["state"] == "GREEN" and c["EXECUTED"]["state"] == "RED", c)
    ls = live_state(cap(), c)
    ok("...and live_state reports EXECUTED as the blocking stage, not a blended score",
       ls["live"] is False and ls["blocked_at"] == "EXECUTED", ls)

    c = _ec(cap(), on_disk, {"m.produces": 3}, HERE)
    ok("a live call makes EXECUTED green", c["EXECUTED"]["state"] == "GREEN", c["EXECUTED"])
    ok("⚑ but CONSUMED is UNDECLARED, not green - 'nobody has recorded a consumer' and 'there "
       "is no consumer' must not render the same (R2.10)",
       c["CONSUMED"]["state"] == "UNDECLARED", c["CONSUMED"])
    ok("...and the UNDECLARED consumer report still offers CANDIDATE readers, so the gap is one "
       "a person can close rather than one they must go looking for",
       "candidates" in c["CONSUMED"])

    c = _ec(cap(consumers=["email_prefill"]), on_disk, {"m.produces": 3}, HERE)
    ok("⚑ a NAMED consumer with no consumption_ref is RED, not green - a declared reader is an "
       "intention until a check names it (R5.4, D-22's class)",
       c["CONSUMED"]["state"] == "RED", c["CONSUMED"])

    _suites = {"m": "GREEN"}
    full = cap(consumers=["email_prefill"], consumption_ref={"kind": "check", "ref": "m.consumes"},
               must_fire=[{"state": "BUY", "fixture": "f1"}, {"state": "HOLD", "fixture": "f2"}],
               decision_effective_ref={"kind": "check", "ref": "m.consumes", "via": "m.produces"})
    c = _ec(full, on_disk, {"m.produces": 3}, HERE, suites=_suites)
    ls = live_state(full, c)
    ok("⚑ POSITIVE CONTROL: with all four stages evidenced the capability IS live - so every "
       "red above means 'this stage is missing', not 'the chain never goes green'",
       ls["live"] is True, ls)

    # ── ISA-0628: prose is not evidence ──────────────────────────────────────────────
    prose = dict(full, consumption_ref="PROVEN 12-Sep-2026: run_context carries it",
                 decision_effective_ref="UNDECLARED — no live decision has yet turned on it")
    c = _ec(prose, on_disk, {"m.produces": 3}, HERE, suites=_suites)
    ok("⚑ ISA-0628 NEGATIVE CONTROL: a PROSE consumption_ref is CONSUMED:RED (PROSE_NOT_EVIDENCE), "
       "never green", c["CONSUMED"]["state"] == "RED" and "prose" in c["CONSUMED"]["why"], c["CONSUMED"])
    ok("⚑ ISA-0628 NEGATIVE CONTROL: 'UNDECLARED — <sentence>' is UNDECLARED, not a declared "
       "reference (the measured false GREEN on five capabilities)",
       c["DECISION_EFFECTIVE"]["state"] != "GREEN"
       and "no `decision_effective_ref`" in c["DECISION_EFFECTIVE"]["why"], c["DECISION_EFFECTIVE"])
    ok("ISA-0628 NEGATIVE CONTROL: a consumers field of 'UNDECLARED — ...' is not a named consumer",
       _ec(cap(consumers="UNDECLARED — the gap ISA-0646 records"), on_disk,
                      {"m.produces": 3}, HERE, suites=_suites)["CONSUMED"]["state"] == "UNDECLARED")
    ok("ISA-0628 NEGATIVE CONTROL: a typed check whose suite is not GREEN on this source is RED",
       _ec(full, on_disk, {"m.produces": 3}, HERE, suites={})["CONSUMED"]["state"] == "RED")
    ok("ISA-0628 NEGATIVE CONTROL: a typed check naming a function not on disk is RED",
       ref_evidence({"kind": "check", "ref": "m.ghost"}, on_disk, _suites, check_ast=False)["state"] == "RED")
    # ── ISA-0467 C7 disposition ──
    _nl = live_state(cap(), _ec(cap(), on_disk, {}, HERE, suites=_suites))
    ok("C7 NEGATIVE CONTROL: a non-LIVE capability with no dated reason is UNDISPOSITIONED",
       _not_live_disposition(cap(), _nl, {"ISA-0001"})["state"] == "UNDISPOSITIONED")
    ok("C7 NEGATIVE CONTROL: a reason naming a CLOSED item is STALE",
       _not_live_disposition(cap(not_live_reason={"item": "ISA-0002", "since": "2026-09-16"}),
                             _nl, {"ISA-0001"})["state"] == "STALE")
    ok("C7 POSITIVE CONTROL: a dated reason naming an OPEN item is REGISTERED",
       _not_live_disposition(cap(not_live_reason={"item": "ISA-0001", "since": "2026-09-16"}),
                             _nl, {"ISA-0001"})["state"] == "REGISTERED")

    # ── R5.10 must-fire coverage, and the negative control that it can fail ──────────
    doc = {"capabilities": [full, cap(name="y_gbp", id="CAP-y")]}
    gaps = must_fire_gaps(HERE, doc)
    ok("a capability claiming decision states with no must-fire fixture is a GAP (R5.10, KR12)",
       [g["name"] for g in gaps] == ["y_gbp"], gaps)
    ok("⚑ NEGATIVE CONTROL: the fully covered capability is NOT reported - so the finding above "
       "means 'uncovered', not 'this check flags everything'",
       all(g["name"] != "x_gbp" for g in gaps), gaps)
    partial = dict(full)
    partial["must_fire"] = [{"state": "BUY", "fixture": "f1"}]
    gaps2 = must_fire_gaps(HERE, {"capabilities": [partial]})
    ok("⚑ a PARTIALLY covered capability names the states with no fixture rather than passing - "
       "coverage that shrinks as states are added is FC-H",
       gaps2 and gaps2[0]["states_without_a_must_fire_fixture"] == ["HOLD"], gaps2)

    _sh = dict(full, production_status="SHADOW")
    _csh = _ec(_sh, on_disk, {"m.produces": 3}, HERE, suites=_suites)
    ok("ISA-0465/0700 NEGATIVE CONTROL: the SAME fully-evidenced capability in SHADOW is NOT live - "
       "DECISION_EFFECTIVE RED (SHADOW_NOT_DECISION_EFFECTIVE); a comparison run cannot manufacture LIVE",
       live_state(_sh, _csh)["live"] is False
       and _csh["DECISION_EFFECTIVE"].get("kind") == "SHADOW_NOT_DECISION_EFFECTIVE", _csh["DECISION_EFFECTIVE"])

    _uo = unobserved_producers(HERE, {"capabilities": [cap(producer="nonexistent_mod.never_marked")]})
    ok("ISA-0699 NEGATIVE CONTROL: a producer outside the manifest with no _mark site is reported unobserved",
       _uo["state"] == "FAIL" and _uo["rows"][0]["in_manifest"] is False, _uo)
    if os.path.exists(store_path(HERE)):
        _ur = unobserved_producers(HERE)
        ok("ISA-0699 MUST-FIRE on the real registry: every declared producer is manifest-listed AND marked",
           _ur["state"] == "PASS", _ur)

    _cb = _ec(full, on_disk, {"m.produces": 3}, HERE, suites=_suites, bound={})
    ok("ISA-0699 NEGATIVE CONTROL: live_run calls NOT bound to an AUTHORISED real run are EXECUTED:RED "
       "(EXECUTION_NOT_RUN_BOUND) - a rehearsal cannot make a capability LIVE",
       _cb["EXECUTED"].get("kind") == "EXECUTION_NOT_RUN_BOUND" and live_state(full, _cb)["live"] is False, _cb["EXECUTED"])
    _cg = _ec(full, on_disk, {"m.produces": 3}, HERE, suites=_suites,
              bound={"m.produces": {"calls": 3, "runs": ["TB-X@t"]}})
    ok("ISA-0699 POSITIVE CONTROL: the same capability with run-bound calls is EXECUTED GREEN and LIVE",
       _cg["EXECUTED"]["state"] == "GREEN" and live_state(full, _cg)["live"] is True, _cg["EXECUTED"])

    # ── ISA-0699 legs (b)/(c) — BS-0699-BC §15 contract controls ─────────────────────
    ok("BS-0699 NEGATIVE CONTROL: a decision_effective_ref with no `via` orchestrator is RED (VIA_REQUIRED)",
       ref_evidence({"kind": "check", "ref": "m.consumes"}, on_disk, _suites, stage="DECISION_EFFECTIVE",
                    check_ast=False).get("kind") == "VIA_REQUIRED")
    _rd = set(on_disk) | {"email_prefill.concentration_line"}
    ok("BS-0699 NEGATIVE CONTROL: renderer-only evidence cannot satisfy DECISION-EFFECTIVE",
       ref_evidence({"kind": "check", "ref": "email_prefill.concentration_line", "via": "m.produces"},
                    _rd, {"email_prefill": "GREEN", "m": "GREEN"}, stage="DECISION_EFFECTIVE",
                    check_ast=False).get("kind") == "RENDERER_NOT_DECISION")
    ok("BS-0699 NEGATIVE CONTROL: evidence declared for another capability is RED (CAPABILITY_MISMATCH)",
       ref_evidence({"kind": "check", "ref": "m.consumes", "via": "m.produces", "capability": "CAP-y"},
                    on_disk, _suites, cap_id="CAP-x", stage="DECISION_EFFECTIVE",
                    check_ast=False).get("kind") == "CAPABILITY_MISMATCH")
    _real = {"capability_checks.check_stock_max_gbp", "capability_checks.check_mctr",
             "capital_destination.sleeve_split", "risk_contribution.evaluate"}
    _rs = {"capability_checks": "GREEN"}
    if os.path.exists(os.path.join(HERE, "capability_checks.py")):
        _pos = ref_evidence({"kind": "check", "ref": "capability_checks.check_stock_max_gbp",
                             "via": "capital_destination.sleeve_split"}, _real, _rs, cap_id="CAP-x",
                            stage="DECISION_EFFECTIVE")
        ok("BS-0699 POSITIVE CONTROL (real AST): check_stock_max_gbp traverses sleeve_split through its helper",
           _pos["state"] == "GREEN", _pos)
        _neg = ref_evidence({"kind": "check", "ref": "capability_checks.check_mctr",
                             "via": "capital_destination.sleeve_split"}, _real, _rs,
                            stage="DECISION_EFFECTIVE")
        ok("BS-0699 NEGATIVE CONTROL (real AST): a check that never enters the named orchestrator is RED",
           _neg.get("kind") == "DOES_NOT_TRAVERSE_ORCHESTRATOR", _neg)
        ok("BS-0699 NEGATIVE CONTROL: same check against a CONSUMED stage still refuses a false via",
           ref_evidence({"kind": "check", "ref": "capability_checks.check_mctr",
                         "via": "capital_destination.sleeve_split"}, _real, _rs,
                        stage="CONSUMED")["state"] == "RED")
        ok("BS-0699 NEGATIVE CONTROL: a stale/mismatched source roll (suite not recorded on this source) is RED",
           ref_evidence({"kind": "check", "ref": "capability_checks.check_stock_max_gbp",
                         "via": "capital_destination.sleeve_split"}, _real, {}, stage="DECISION_EFFECTIVE")["state"] == "RED")
    _na = dict(full, decision_effective_ref={"kind": "not_applicable", "item": "ISA-0001",
                                             "why": "renderer-only consumer"})
    _cna = _ec(_na, on_disk, {"m.produces": 3}, HERE, suites=_suites)
    ok("BS-0699: a not_applicable disposition is NOT_APPLICABLE and never LIVE",
       _cna["DECISION_EFFECTIVE"]["state"] == "NOT_APPLICABLE" and live_state(_na, _cna)["live"] is False, _cna)
    ok("BS-0699 NEGATIVE CONTROL: not_applicable without an owning ISA item is RED",
       _ec(dict(full, decision_effective_ref={"kind": "not_applicable", "why": "x"}), on_disk,
           {"m.produces": 3}, HERE, suites=_suites)["DECISION_EFFECTIVE"]["state"] == "RED")
    _decl_ok = dict(full, economic_decision="d", inputs=["i"], orchestrators=["o"], constraints=["c"],
                    production_status="CANDIDATE")
    _dd = {"_denominator_ids": ["CAP-x"], "capabilities": [_decl_ok]}
    ok("BS-0699 POSITIVE CONTROL: a fully declared registry with its floor PASSES declarations",
       declarations(HERE, _dd)["state"] == "PASS", declarations(HERE, _dd))
    _new = cap(id="CAP-new", name="new_gbp")
    ok("BS-0699 MUST-FIRE: a newly added capability with blank semantics FAILS reconciliation rather "
       "than joining the denominator silently",
       declarations(HERE, {"_denominator_ids": ["CAP-x"], "capabilities": [_decl_ok, _new]})["state"] == "FAIL")
    _expl = dict(_decl_ok, id="CAP-z", decision_effective_ref=UNDECLARED,
                 unresolved={"decision_effective_ref": "traced 17-Sep: branch unreachable, owned by ISA-0706"})
    ok("BS-0699: an UNDECLARED field with a recorded reason naming an item is explicitly unresolved (PASS)",
       declarations(HERE, {"_denominator_ids": ["CAP-z"], "capabilities": [_expl]})["state"] == "PASS")
    ok("BS-0699 NEGATIVE CONTROL: a reason with no ISA item does not explain the gap",
       declarations(HERE, {"_denominator_ids": ["CAP-z"], "capabilities": [dict(_expl, unresolved={
           "decision_effective_ref": "we will get to this later on"})]})["state"] == "FAIL")
    ok("BS-0699 NEGATIVE CONTROL: deleting a capability below the _denominator_ids floor FAILS",
       declarations(HERE, {"_denominator_ids": ["CAP-x", "CAP-gone"], "capabilities": [_decl_ok]})
       ["denominator_floor_missing"] == ["CAP-gone"])
    if os.path.exists(store_path(HERE)):
        _dr = declarations(HERE)
        ok("ISA-0699 MUST-FIRE on the real registry: every capability truly declared or explicitly unresolved",
           _dr["state"] == "PASS", _dr)

    # ── refusals: a missing or reshaped store is UNKNOWN, never empty ────────────────
    raised = False
    try:
        load("/nonexistent-root-for-the-control")
    except CapabilityRefused:
        raised = True
    ok("⚑ NEGATIVE CONTROL: an absent registry RAISES rather than returning zero capabilities - "
       "'no capabilities' would read as 'nothing to check' (R2.10, R4.3)", raised)

    # ── seeding does not invent ──────────────────────────────────────────────────────
    if os.path.exists(os.path.join(HERE, "quantity_register.json")):
        seeded = seed_from_declarations(HERE)
        ok("seeding produces one capability per declared quantity, from disk",
           len(seeded["capabilities"]) > 0)
        ok("⚑ every semantic field the framework has never recorded is UNDECLARED, not a "
           "plausible guess (R7.5, ISA-0448's refusal)",
           all(c0["economic_decision"] == UNDECLARED for c0 in seeded["capabilities"]))
        ok("...and the declared facts ARE carried over rather than blanked - producer and GBP "
           "exposure come from quantity_register.json",
           any(c0["producer"] != UNDECLARED and c0["gbp_exposure"]
               for c0 in seeded["capabilities"]))

    # ── A12: the observer is not in its own consumer scan ────────────────────────────
    ok("⚑ SELF-EXCLUSION (A12/R10): capability_registry is not a candidate consumer of its own "
       "keys - a check whose first finding is itself gets deleted rather than fixed",
       SELF_MODULE not in _readers_of("email.s2.capital_router", HERE))

    if verbose:
        print("\ncapability_registry selftest: %d assertion(s), %d FAIL(s)%s"
              % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    if "--seed" in argv:
        doc = seed_from_declarations()
        p = save(doc)
        print("capability registry seeded: %d capabilities -> %s"
              % (len(doc["capabilities"]), p))
        return 0
    print(json.dumps(report(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
