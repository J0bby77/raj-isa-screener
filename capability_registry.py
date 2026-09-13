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


def evidence_chain(cap: dict, on_disk: set, live: Dict[str, int],
                   root: str = HERE) -> dict:
    """R4.14 for one capability. Each stage is GREEN / RED / UNDECLARED with its reason."""
    producer = cap.get("producer")
    chain = {}

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
    if chain["PRODUCED"]["state"] != "GREEN":
        chain["EXECUTED"] = {"state": "BLOCKED",
                             "why": "PRODUCED is not green; execution is not asked"}
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
    if cons == UNDECLARED or not cons:
        cand = []
        for out_key in (cap.get("outputs") or []):
            cand += _readers_of(out_key, root)
        chain["CONSUMED"] = {
            "state": "UNDECLARED",
            "why": ("no consumer is declared. R15.6 asks who reads the output; the Atlas cannot "
                    "infer it and this module refuses to guess it"),
            "candidates": sorted(set(cand)) or None}
    elif cap.get("consumption_ref") in (None, "", UNDECLARED):
        chain["CONSUMED"] = {"state": "RED",
                             "why": ("consumers are named (%s) but no `consumption_ref` proves "
                                     "the read happens on the live path — a declared reader is "
                                     "an intention until a check names it (R5.4)"
                                     % ", ".join(cons if isinstance(cons, list) else [str(cons)]))}
    else:
        chain["CONSUMED"] = {"state": "GREEN",
                             "why": "consumption proved by %s" % cap["consumption_ref"]}

    # DECISION-EFFECTIVE — a must-fire fixture (R5.10) that actually reached the state.
    mf = cap.get("must_fire")
    der = cap.get("decision_effective_ref")
    if mf == UNDECLARED or not mf:
        chain["DECISION_EFFECTIVE"] = {
            "state": "UNDECLARED",
            "why": ("R5.10: every decision state the framework claims it can make needs at "
                    "least one controlled fixture in which that state MUST be reached through "
                    "the real orchestration. None is declared, so the claim is untested")}
    elif der in (None, "", UNDECLARED):
        chain["DECISION_EFFECTIVE"] = {
            "state": "RED",
            "why": ("must-fire fixtures are declared but no `decision_effective_ref` records "
                    "one firing. A fixture that has never made the state fire does not prove "
                    "the state can be reached (R5.10)")}
    else:
        chain["DECISION_EFFECTIVE"] = {"state": "GREEN",
                                       "why": "must-fire evidence: %s" % der}
    return chain


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
    rows = []
    for cap in doc["capabilities"]:
        chain = evidence_chain(cap, on_disk, live, root)
        ls = live_state(cap, chain)
        rows.append({"id": cap["id"], "name": cap["name"],
                     "gbp_exposure": cap.get("gbp_exposure"),
                     "producer": cap.get("producer"),
                     "production_status": cap.get("production_status"),
                     "chain": chain, "live": ls})
    rows.sort(key=lambda r: -(r["gbp_exposure"] or 0.0))
    n_live = sum(1 for r in rows if r["live"]["live"])
    by_stage = {}
    for r in rows:
        if not r["live"]["live"]:
            by_stage.setdefault(r["live"]["blocked_at"], []).append(r["name"])
    exposure_not_live = sum((r["gbp_exposure"] or 0.0) for r in rows if not r["live"]["live"])
    return {
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


def undeclared_fields(root: str = HERE, registry: Optional[dict] = None) -> List[dict]:
    """Every UNDECLARED semantic field, ranked by exposure — the honest backlog of this build."""
    doc = registry if registry is not None else load(root)
    semantic = ("economic_decision", "inputs", "consumers", "orchestrators", "must_fire",
                "consumption_ref", "decision_effective_ref", "constraints", "production_status")
    out = []
    for cap in doc["capabilities"]:
        gaps = [f for f in semantic if cap.get(f) in (None, "", UNDECLARED)]
        if gaps:
            out.append({"id": cap["id"], "name": cap["name"],
                        "gbp_exposure": cap.get("gbp_exposure"), "undeclared": gaps})
    out.sort(key=lambda r: -(r["gbp_exposure"] or 0.0))
    return out


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

    def cap(**kw):
        base = {"id": "CAP-x", "name": "x_gbp", "producer": "m.produces",
                "outputs": ["email.s2.x"], "consumers": UNDECLARED,
                "consumption_ref": UNDECLARED, "must_fire": UNDECLARED,
                "decision_effective_ref": UNDECLARED, "decision_states": ["BUY", "HOLD"],
                "gbp_exposure": 1000.0}
        base.update(kw)
        return base

    # ── the chain refuses at the FIRST red, and names which stage ─────────────────────
    c = evidence_chain(cap(producer="m.missing"), on_disk, {"m.missing": 5}, HERE)
    ok("a producer that is not on disk is PRODUCED:RED, not a silent pass",
       c["PRODUCED"]["state"] == "RED", c["PRODUCED"])
    ok("...and EXECUTED is BLOCKED rather than green off a ledger entry - a ledger row for a "
       "function that does not exist must not manufacture evidence",
       c["EXECUTED"]["state"] == "BLOCKED", c["EXECUTED"])

    c = evidence_chain(cap(), on_disk, {}, HERE)
    ok("⚑ THE ISA-0454 SHAPE: on disk, declared, and never run from a live caller is "
       "EXECUTED:RED - reachable is not live (R15.5)",
       c["PRODUCED"]["state"] == "GREEN" and c["EXECUTED"]["state"] == "RED", c)
    ls = live_state(cap(), c)
    ok("...and live_state reports EXECUTED as the blocking stage, not a blended score",
       ls["live"] is False and ls["blocked_at"] == "EXECUTED", ls)

    c = evidence_chain(cap(), on_disk, {"m.produces": 3}, HERE)
    ok("a live call makes EXECUTED green", c["EXECUTED"]["state"] == "GREEN", c["EXECUTED"])
    ok("⚑ but CONSUMED is UNDECLARED, not green - 'nobody has recorded a consumer' and 'there "
       "is no consumer' must not render the same (R2.10)",
       c["CONSUMED"]["state"] == "UNDECLARED", c["CONSUMED"])
    ok("...and the UNDECLARED consumer report still offers CANDIDATE readers, so the gap is one "
       "a person can close rather than one they must go looking for",
       "candidates" in c["CONSUMED"])

    c = evidence_chain(cap(consumers=["email_prefill"]), on_disk, {"m.produces": 3}, HERE)
    ok("⚑ a NAMED consumer with no consumption_ref is RED, not green - a declared reader is an "
       "intention until a check names it (R5.4, D-22's class)",
       c["CONSUMED"]["state"] == "RED", c["CONSUMED"])

    full = cap(consumers=["email_prefill"], consumption_ref="pair_capital_router_render",
               must_fire=[{"state": "BUY", "fixture": "f1"}, {"state": "HOLD", "fixture": "f2"}],
               decision_effective_ref="test_must_fire::BUY green 09-Sep-2026")
    c = evidence_chain(full, on_disk, {"m.produces": 3}, HERE)
    ls = live_state(full, c)
    ok("⚑ POSITIVE CONTROL: with all four stages evidenced the capability IS live - so every "
       "red above means 'this stage is missing', not 'the chain never goes green'",
       ls["live"] is True, ls)

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
