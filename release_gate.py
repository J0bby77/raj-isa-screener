#!/usr/bin/env python3
"""
release_gate.py — §18 TRUSTED BUILD / CANDIDATE / RELEASE, §19 BUILDSPEC, R7.8–R7.10, R4.15.

Authority: ISA_Engineering_Rules.md §18 (R18.1–R18.5), §19 (R19.1–R19.4), §7 (R7.8–R7.10),
§4 (R4.15, R4.16), §5 (R5.12), §14 (R14.5). Raised as ISA-0629 under ISA-0623 / ISA-0467.
Canonical stores: `Dashboard/state/trusted_build.json` (the current receipt) and
`Dashboard/state/trusted_receipts/` (every prior one, R18.4's recoverable window).

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS — the hole ISA-0467 named and did not close
═══════════════════════════════════════════════════════════════════════════════════════════
ISA-0467's systemic cause, written 27-Aug-2026: *"EVERY rule in the standard is an obligation
on a participant AT THE MOMENT OF BUILDING, and nothing fires when nobody is building."*
Phase 0 answered the first half — `framework_integrity` measures execution and declaration.
Nothing answered the second half, and it shows in one measurement:

  **`framework_atlas.check()` — the function R15.4 says must FAIL a build on drift — returns
  False against the delivered tree today (declared `cfd81d2f72bb`, 150 modules, as_of
  2026-08-12; current `2b253f07acee`, 209 modules) and has ZERO call sites outside its own
  `__main__` and its own test.** Fifty-nine modules exist that the Trusted map has never seen,
  including `framework_integrity`, `rule_audit` and `capital_destination`. The check was built.
  Nothing calls it. That is FC-E in the enforcement layer itself.

A control nobody invokes is not a control, so §18 moves the obligation off the participant and
onto the artefact: **LIVE must match a signed receipt, and a scheduled capital run refuses to
proceed when it does not** (R18.5). That is a state a check can read at any time, including
when nobody is building — which is precisely the risk surface ISA-0467 said nothing covered.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHAT THIS MODULE REFUSES TO DO
═══════════════════════════════════════════════════════════════════════════════════════════
It does not report a gate as PASS on evidence it could not gather. Every gate returns one of
GREEN / RED / **ENVIRONMENT_UNKNOWN** / **UNKNOWN**, and only GREEN counts (R4.3, R5.12).
`ENVIRONMENT_UNKNOWN` exists because a missing cache or an unreachable Windows path is a fact
about the sandbox, not about the framework (R2.9) — and because "could not look" and "looked
and found nothing" must never render the same (R2.10).

⚑ AN OBSERVER MAY NOT MEASURE ITSELF (A12/R10). This module is excluded from the negative-
control census it runs over the tree, and `_selftest` proves the exclusion is load-bearing.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["release_gate"] = False` — `certify()` and
`verify_live()` return `DISABLED`, which every caller reads as UNKNOWN and never as PASS.
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_MODULE = "release_gate"
STATE_REL = os.path.join("Dashboard", "state")
RECEIPT_FILE = "trusted_build.json"
RECEIPT_DIR = "trusted_receipts"

GREEN, RED, ENV_UNKNOWN, UNKNOWN = "GREEN", "RED", "ENVIRONMENT_UNKNOWN", "UNKNOWN"

# §19 R19.2 — the nineteen categories a BuildSpec must cover. `N/A` requires a reason and
# silence is not `N/A`. Restated NOWHERE else: this tuple is the one home (R4.4), and
# `consistency_check.pair_buildspec_categories` reads it rather than repeating it.
BUILDSPEC_CATEGORIES = (
    "authority_baseline", "problem_and_consequence", "current_evidence",
    "economic_objective", "target_state_and_options", "scope_and_exact_change",
    "data_contract", "dependencies_and_orchestration", "run_surfaces",
    "decision_capability_contract", "learning_and_ml", "observability_dashboard",
    "unintended_consequences", "economic_risk_acceptance", "testing_and_monitoring",
    "candidate_shadow_release_rollback", "register_impact", "unknowns_and_decisions",
    "definition_of_live_done",
)

# R4.15 — the run surfaces every build must disposition. Each is UPDATE_REQUIRED or
# VERIFIED_NO_CHANGE with evidence; omission is not a verdict (R14.5).
RUN_SURFACES = (
    "executed_skill", "skill_mirror", "scheduled_task_instructions", "run_context",
    "orchestrator_entry_point", "data_acquisition_stage", "consumer_output_surfaces",
    "email_reporting", "mandatory_post_run_state",
)

# R7.10 — supersession is a first-class disposition, not WONTFIX.
REVALIDATION_DISPOSITIONS = (
    "STILL_VALID", "PARTLY_VALID", "SUPERSEDED", "DUPLICATE", "NO_LONGER_A_DEFECT",
    "CORRECTIVE_ACTION_STALE",
)

SOURCE_SUFFIXES = (".py",)
EXCLUDE_PARTS = ("__pycache__", "archive", "_bak", "_baseline", ".git", "node_modules",
                 "calibration_pathc_jul2026", "_to_delete", "_candidate_evidence")


class ReleaseRefused(RuntimeError):
    """Raised rather than returning a green receipt. A build that cannot be certified is not
    certified — the refusal is the product (R14.2: refusal is the strongest control)."""


def _today() -> str:
    return datetime.date.today().isoformat()


def _flag() -> bool:
    try:
        import isa_policy as pol
        return bool(pol.flag("release_gate"))
    except Exception:                                                   # noqa: BLE001
        return True


def state_dir(root: str = HERE) -> str:
    return os.path.join(root, STATE_REL)


def receipt_path(root: str = HERE) -> str:
    return os.path.join(state_dir(root), RECEIPT_FILE)


# ────────────────────────────────────────────────────────────────────────────────────────
# FINGERPRINTS — what "LIVE matches the receipt" actually means
# ────────────────────────────────────────────────────────────────────────────────────────

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _iter_source(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not any(d == x or d.startswith(x) for x in EXCLUDE_PARTS)]
        for fn in sorted(filenames):
            if fn.endswith(SOURCE_SUFFIXES):
                yield os.path.join(dirpath, fn)


def _fingerprint_files(paths) -> Dict[str, str]:
    out = {}
    for p in sorted(paths):
        try:
            with open(p, "rb") as fh:
                out[os.path.relpath(p)] = _sha(fh.read())
        except OSError:
            continue
    return out


def _roll(d: Dict[str, str]) -> str:
    return _sha(json.dumps(d, sort_keys=True).encode("utf-8"))


def source_fingerprint(root: str = HERE) -> dict:
    files = _fingerprint_files(_iter_source(root))
    return {"roll": _roll(files), "n_files": len(files), "files": files}


def rules_fingerprint(root: str = HERE) -> dict:
    p = os.path.join(root, "ISA_Engineering_Rules.md")
    if not os.path.exists(p):
        return {"roll": None, "state": RED,
                "why": "ISA_Engineering_Rules.md is not on disk — the standard the build is "
                       "certified against does not exist"}
    with open(p, "rb") as fh:
        return {"roll": _sha(fh.read()), "path": "ISA_Engineering_Rules.md", "state": GREEN}


def run_surface_fingerprint(root: str = HERE) -> dict:
    """Over the surfaces `framework_atlas` enumerates — the EXECUTED contract where reachable,
    the mirror where it is not, and which was used is recorded (ISA-0211, R2.10)."""
    try:
        sys.path.insert(0, root)
        import framework_atlas as fa
        from pathlib import Path as _P
        wb = fa.run_surface_texts(_P(root), with_basis=True)
        digests = {k: _sha(v[0].encode("utf-8")) for k, v in wb.items()}
        bases = {k: v[1] for k, v in wb.items()}
        n_exec = sum(1 for b in bases.values() if b == "executed")
        return {"roll": _roll(digests), "n_surfaces": len(digests),
                "n_from_executed_contract": n_exec, "basis": bases, "digests": digests,
                "state": GREEN if digests else ENV_UNKNOWN,
                "why": None if digests else "no run surfaces were enumerable from this host"}
    except Exception as exc:                                            # noqa: BLE001
        return {"roll": None, "state": ENV_UNKNOWN,
                "why": "run-surface enumeration unavailable (%s) — a fact about this host, not "
                       "about the framework (R2.9)" % exc}


def config_fingerprint(root: str = HERE) -> dict:
    names = ("isa_policy.py", "scoring_config.py", "target_state.json", "target_weights.json",
             "threshold_register.json", "quantity_register.json", "negative_claims.json",
             "degradation_bands.json")
    present = [os.path.join(root, n) for n in names if os.path.exists(os.path.join(root, n))]
    missing = [n for n in names if not os.path.exists(os.path.join(root, n))]
    files = _fingerprint_files(present)
    return {"roll": _roll(files), "n_files": len(files), "missing": missing, "files": files,
            "state": GREEN if not missing else ENV_UNKNOWN,
            "why": None if not missing else
                   "config files absent from this host: %s (R5.12: a missing file is an "
                   "environment fact, never evidence about correctness)" % ", ".join(missing)}


def atlas_fingerprint(root: str = HERE) -> dict:
    p = os.path.join(state_dir(root), "framework_atlas_manifest.json")
    if not os.path.exists(p):
        return {"roll": None, "state": RED,
                "why": "no atlas manifest — the map has never been declared (R15.4)"}
    with open(p, encoding="utf-8") as fh:
        m = json.load(fh)
    return {"roll": m.get("fingerprint"), "run_id": m.get("run_id"), "as_of": m.get("as_of"),
            "module_count": m.get("module_count"), "state": GREEN}


def capability_fingerprint(root: str = HERE) -> dict:
    p = os.path.join(state_dir(root), "capability_registry.json")
    if not os.path.exists(p):
        return {"roll": None, "state": RED,
                "why": "no capability registry — R15.6 requires one machine-readable "
                       "Decision/Capability Registry beside the Atlas"}
    with open(p, "rb") as fh:
        return {"roll": _sha(fh.read()), "state": GREEN}


def live_fingerprints(root: str = HERE) -> dict:
    return {
        "source": source_fingerprint(root),
        "rules": rules_fingerprint(root),
        "run_surfaces": run_surface_fingerprint(root),
        "config": config_fingerprint(root),
        "atlas": atlas_fingerprint(root),
        "capabilities": capability_fingerprint(root),
    }


# ────────────────────────────────────────────────────────────────────────────────────────
# R18.5 — a scheduled run refuses an unsigned LIVE state
# ────────────────────────────────────────────────────────────────────────────────────────

def load_receipt(root: str = HERE) -> Optional[dict]:
    p = receipt_path(root)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def verify_live(root: str = HERE, receipt: Optional[dict] = None) -> dict:
    """R18.5 / KR10. Does LIVE match the latest Trusted Build receipt?

    ⚑ THIS IS THE MECHANICAL MEANING OF "THE RULES ACTUALLY RAN". Every other control in this
    framework fires while someone is building. This one fires while nobody is — which is the
    exact gap ISA-0467 named: *"nothing fires when nobody is building, so the checking
    apparatus maps where someone recently worked, not the risk surface."*
    """
    if not _flag():
        return {"state": "DISABLED",
                "why": "isa_policy.V2_FLAGS['release_gate'] is False. DISABLED reads as UNKNOWN "
                       "to every caller and never as PASS (R4.3)"}
    rec = receipt if receipt is not None else load_receipt(root)
    if rec is None:
        return {"state": "NO_RECEIPT",
                "why": ("no Trusted Build receipt exists at %s. Under R18.5 a capital-decision "
                        "run cannot certify that what is about to execute is what was "
                        "certified. This is UNTRUSTED, not clean." % receipt_path(root)),
                "blocks_capital_run": True}
    now = live_fingerprints(root)
    declared = rec.get("fingerprints") or {}
    diffs, unknowns = [], []
    for key in ("source", "rules", "run_surfaces", "config", "atlas", "capabilities"):
        cur, dec = now.get(key) or {}, declared.get(key) or {}
        if cur.get("state") in (ENV_UNKNOWN, UNKNOWN):
            unknowns.append({"surface": key, "why": cur.get("why")})
            continue
        if cur.get("roll") != dec.get("roll"):
            entry = {"surface": key, "declared": dec.get("roll"), "live": cur.get("roll")}
            if key == "source":
                d_files, c_files = dec.get("files") or {}, cur.get("files") or {}
                changed = sorted(f for f in set(d_files) | set(c_files)
                                 if d_files.get(f) != c_files.get(f))
                entry["changed_files"] = changed[:40]
                entry["n_changed"] = len(changed)
            diffs.append(entry)
    if diffs:
        return {"state": "UNTRUSTED_LIVE_STATE", "build_id": rec.get("build_id"),
                "diffs": diffs, "unknowns": unknowns, "blocks_capital_run": True,
                "why": ("LIVE differs from Trusted Build %s on %d surface(s). R18.5: preflight "
                        "reports UNTRUSTED_LIVE_STATE and blocks a capital-decision run until "
                        "reconciled." % (rec.get("build_id"), len(diffs)))}
    if unknowns:
        return {"state": ENV_UNKNOWN, "build_id": rec.get("build_id"), "unknowns": unknowns,
                "blocks_capital_run": True,
                "why": ("%d surface(s) could not be fingerprinted from this host. 'Could not "
                        "look' is not 'matches' (R2.10) — this blocks rather than passes."
                        % len(unknowns))}
    return {"state": "TRUSTED", "build_id": rec.get("build_id"),
            "promoted_on": rec.get("promoted_on"), "blocks_capital_run": False,
            "why": "LIVE matches Trusted Build %s on every fingerprinted surface"
                   % rec.get("build_id")}


# ────────────────────────────────────────────────────────────────────────────────────────
# §19 — the BuildSpec / Change Contract
# ────────────────────────────────────────────────────────────────────────────────────────

def buildspec_gaps(spec: dict) -> List[dict]:
    """R19.2 / KR17. Which mandatory categories are absent, and which claim `N/A` with no reason.

    ⚑ SILENCE IS NOT `N/A` — the rule says so in those words. A category present but empty, and
    a category set to "N/A" with no reason, are both gaps; only "N/A" WITH a reason is a
    disposition (R14.5: VERIFIED NO CHANGE is evidence, not absence of thought)."""
    out = []
    for cat in BUILDSPEC_CATEGORIES:
        v = spec.get(cat)
        if v in (None, "", []):
            out.append({"category": cat, "state": "ABSENT",
                        "why": "mandatory under R19.2; silence is not N/A"})
            continue
        if isinstance(v, str) and v.strip().upper() in ("N/A", "NA", "NOT APPLICABLE"):
            out.append({"category": cat, "state": "NA_WITHOUT_REASON",
                        "why": "R19.2: `N/A` requires a reason"})
    return out


def spec_currency(spec: dict, build_id: Optional[str]) -> dict:
    """R19.3 — a spec validated against an older Trusted Build is REVALIDATION_REQUIRED."""
    against = spec.get("validated_against_build_id")
    if not against:
        return {"state": RED, "why": "the BuildSpec names no `validated_against_build_id` "
                                     "(R19.1: it starts from a spec validated against the "
                                     "current Trusted Build)"}
    if build_id and against != build_id:
        return {"state": RED, "why": ("BuildSpec validated against %s; the current Trusted Build "
                                      "is %s. R19.3: the spec is REVALIDATION_REQUIRED."
                                      % (against, build_id))}
    return {"state": GREEN, "why": "BuildSpec validated against the current Trusted Build %s"
                                   % against}


# ────────────────────────────────────────────────────────────────────────────────────────
# R7.8 / R7.9 / R7.10 — item currency and supersession
# ────────────────────────────────────────────────────────────────────────────────────────

def item_currency(root: str = HERE, build_id: Optional[str] = None,
                  items: Optional[list] = None) -> dict:
    """KR13 / R7.8. Which actionable items are not validated against the current Trusted Build.

    ⚑ THIS IS THE RULE ISA-0467 ITSELF FAILED. It was raised 27-Aug-2026 and stood BUILD_READY
    and unmodified while the builds of 28-Aug, 29-Aug, 02-Sep, 03-Sep, 05-Sep and 06-Sep shipped
    most of its corrective action — so on 09-Sep a CRITICAL P0 item's stated remedy was to build
    two modules that had existed for twelve days. An item is authority only while the
    architecture it was written against still stands."""
    if items is None:
        try:
            sys.path.insert(0, root)
            import isa_register as R
            items = R.read_all()
        except Exception as exc:                                        # noqa: BLE001
            return {"state": ENV_UNKNOWN,
                    "why": "register unreadable from this host (%s) — R2.9" % exc}
    actionable = [i for i in items
                  if i.get("state") in ("OPEN", "IN_PROGRESS")
                  and i.get("build_readiness") == "BUILD_READY"]
    # ⚑⚑ A NULL BUILD ID IS NOT A CLEAN BILL (R4.3, FC-A). The first version compared
    #    `validated_against_build_id != build_id` with build_id None, so an item whose field was
    #    also None compared EQUAL and read as current — and with no receipt on disk that meant
    #    every actionable item read current. A control fed a null returns UNKNOWN and BLOCKS; it
    #    never returns PASS. Found by KR13 reporting 0 of 42 on a tree with no receipt at all.
    if not build_id:
        return {"state": RED, "current_build_id": None,
                "n_actionable": len(actionable), "n_stale": len(actionable),
                "stale": [{"id": i["id"], "criticality": i.get("criticality"),
                           "validated_against_build_id": i.get("validated_against_build_id"),
                           "title": (i.get("title") or "")[:110]} for i in actionable],
                "why": ("no Trusted Build receipt exists, so NO item can be validated against "
                        "the current build. Every BUILD_READY item is REVALIDATION_REQUIRED "
                        "until one does (R7.8, R4.3 — a null never returns PASS).")}
    stale = [{"id": i["id"], "criticality": i.get("criticality"),
              "validated_against_build_id": i.get("validated_against_build_id"),
              "title": (i.get("title") or "")[:110]}
             for i in actionable
             if i.get("validated_against_build_id") != build_id]
    stale.sort(key=lambda r: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
               .get(r["criticality"], 4))
    return {"state": GREEN if not stale else RED,
            "current_build_id": build_id,
            "n_actionable": len(actionable), "n_stale": len(stale), "stale": stale,
            "why": ("every BUILD_READY item is validated against the current Trusted Build"
                    if not stale else
                    "%d BUILD_READY item(s) are not validated against Trusted Build %s. Under "
                    "R7.8 they are REVALIDATION_REQUIRED, not BUILD_READY (KR13)."
                    % (len(stale), build_id))}


def invalidate_touched(items: list, footprint: dict) -> List[str]:
    """R7.9 — every OPEN/BUILD_READY item whose footprint intersects a changed capability,
    module, contract, orchestration path, decision state or run surface becomes
    REVALIDATION_REQUIRED. Returns the ids to move; the caller writes them, so this stays a
    pure function that a test can drive."""
    touched = set()
    for k in ("modules", "capabilities", "run_surfaces", "data_contracts", "decision_states"):
        touched |= {str(x).lower() for x in (footprint.get(k) or [])}
    if not touched:
        return []
    hit = []
    for i in items:
        if i.get("state") not in ("OPEN", "IN_PROGRESS"):
            continue
        blob = json.dumps(i, sort_keys=True).lower()
        if any(t and t in blob for t in touched):
            hit.append(i["id"])
    return sorted(hit)


# ────────────────────────────────────────────────────────────────────────────────────────
# R4.15 — run-surface impact
# ────────────────────────────────────────────────────────────────────────────────────────

def run_surface_dispositions(declared: dict) -> dict:
    """KR14 / R4.15 / R14.5. Every affected surface is UPDATE_REQUIRED or VERIFIED_NO_CHANGE
    WITH EVIDENCE. An unlisted surface cannot be inferred to need no change."""
    missing, unevidenced = [], []
    for s in RUN_SURFACES:
        d = declared.get(s)
        if d in (None, "", {}):
            missing.append(s)
            continue
        verdict = d.get("verdict") if isinstance(d, dict) else str(d)
        if verdict not in ("UPDATE_REQUIRED", "VERIFIED_NO_CHANGE", "NOT_APPLICABLE"):
            missing.append(s)
        elif verdict in ("VERIFIED_NO_CHANGE", "NOT_APPLICABLE") and not (
                isinstance(d, dict) and d.get("evidence")):
            unevidenced.append(s)
    return {"state": GREEN if not missing and not unevidenced else RED,
            "undispositioned": missing, "unevidenced": unevidenced,
            "why": ("every run surface carries a disposition with evidence"
                    if not missing and not unevidenced else
                    "R4.15: %d surface(s) undispositioned and %d claiming no change without "
                    "evidence. Omission is not a verdict (R14.5)."
                    % (len(missing), len(unevidenced)))}


# ────────────────────────────────────────────────────────────────────────────────────────
# negative-control census — R5.5, and the reason this gate is not self-congratulatory
# ────────────────────────────────────────────────────────────────────────────────────────

_NEG_MARKERS = ("negative control", "must fail", "must not", "positive control",
                "control is not vacuous", "reproduced on demand")


def negative_control_census(modules: List[str], root: str = HERE) -> dict:
    """R5.5 — does each changed module's selftest actually carry a control that FAILS on a
    deliberately broken input?

    ⚑ COUNTED FROM THE SELFTEST'S OWN BODY BY AST, never from a claim. A module whose selftest
    is emptied by a refactor must FAIL here rather than pass with zero assertions — the vacuous
    pass this project keeps rediscovering (ISA-0348, ISA-0513)."""
    rows = []
    for m in modules:
        if m == SELF_MODULE:
            continue                                    # A12: the observer is not the subject
        p = os.path.join(root, m if m.endswith(".py") else m + ".py")
        if not os.path.exists(p):
            rows.append({"module": m, "state": ENV_UNKNOWN, "why": "not on disk from this host"})
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
        except Exception as exc:                                        # noqa: BLE001
            rows.append({"module": m, "state": ENV_UNKNOWN, "why": str(exc)[:120]})
            continue
        st = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name in ("_selftest", "selftest")), None)
        if st is None:
            rows.append({"module": m, "state": RED, "n_assertions": 0, "n_negative": 0,
                         "why": "no selftest — R5.5 has nothing to be satisfied by"})
            continue
        lits = [n.value for n in ast.walk(st)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        n_neg = sum(1 for l in lits if any(k in l.lower() for k in _NEG_MARKERS))
        # ⚑ ISA-0682 — THE VOCABULARY IS DERIVED, NOT HAND-LISTED. This counted only
        #   ("ok", "check", "assert_") and FIVE modules define and use `ck(name, cond)`
        #   instead — capital_destination, forward_record, fund_expected_return,
        #   fund_exposure_vectors, fund_returns — so the R5.5 gate reported RED with
        #   n_assertions = 0 on selftests that assert throughout. That is Q4's own failure
        #   class inverted: a filter keyed on three values while the producers emit a fourth.
        #   The docstring's claim that it counts "by AST, never from a claim" was true and
        #   still wrong, because it counted the wrong tokens. A gate that cries wolf is the
        #   one that gets waived.
        #   ⚑ Any single-argument-or-more helper DEFINED INSIDE the selftest is now treated
        #     as an assertion helper, so a SIXTH idiom is counted rather than silently missed.
        _inner = {f.name for f in ast.walk(st)
                  if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f is not st}
        _vocab = {"ok", "check", "assert_", "ck"} | _inner
        n_ass = sum(1 for n in ast.walk(st)
                    if isinstance(n, ast.Assert)
                    or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in _vocab))
        rows.append({"module": m, "state": GREEN if (n_neg and n_ass) else RED,
                     "n_assertions": n_ass, "n_negative": n_neg,
                     "why": None if (n_neg and n_ass) else
                            "R5.5: a selftest with %d assertion(s) and %d labelled negative "
                            "control(s) does not prove a broken input still fails"
                            % (n_ass, n_neg)})
    bad = [r for r in rows if r["state"] != GREEN]
    return {"state": GREEN if not bad else RED, "rows": rows, "n_failing": len(bad),
            "why": None if not bad else
                   "%d module(s) changed by this build carry no proven negative control"
                   % len(bad)}


# ────────────────────────────────────────────────────────────────────────────────────────
# R18.3 — the one canonical release gate
# ────────────────────────────────────────────────────────────────────────────────────────

def _gate(name, state, why, **extra):
    d = {"gate": name, "state": state, "why": why}
    d.update(extra)
    return d


def certify(root: str = HERE, *, build_id: str, items: List[str],
            changed_modules: List[str], spec: Optional[dict] = None,
            run_surfaces: Optional[dict] = None,
            waivers: Optional[List[dict]] = None) -> dict:
    """R18.3 — the only supported route to LIVE. Returns a receipt-shaped certification.

    Every gate is GREEN / RED / ENVIRONMENT_UNKNOWN / UNKNOWN. **Only GREEN counts**, and a
    waived gate is recorded as waived rather than as green (§11: an unrecorded waiver is a
    defect)."""
    if not _flag():
        return {"state": "DISABLED", "build_id": build_id,
                "why": "isa_policy.V2_FLAGS['release_gate'] is False"}
    sys.path.insert(0, root)
    gates: List[dict] = []
    waivers = list(waivers or [])
    waived = {w["gate"] for w in waivers}

    # 1 — R15.4: the Candidate Atlas reconciles with the declared manifest.
    try:
        import framework_atlas as fa
        from pathlib import Path as _P
        ok, msg = fa.check(_P(root))
        gates.append(_gate("atlas_current", GREEN if ok else RED, msg))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("atlas_current", ENV_UNKNOWN,
                           "framework_atlas unavailable (%s) — R2.9" % exc))

    # 2/3 — §17: every rule classified, and no rule claimed ASSERTED that cannot fail.
    try:
        import rule_audit as ra
        doc = ra.audit()
        unclass = doc["coverage"]["unclassified"]
        gates.append(_gate("rules_all_classified", GREEN if not unclass else RED,
                           "R15.4 item 3: %d rule(s) defined and unclassified%s"
                           % (len(unclass), (": " + ", ".join(unclass)) if unclass else ""),
                           unclassified=unclass))
        unt = doc["traceable"]["asserted_untraceable"]
        gates.append(_gate("no_false_asserted", GREEN if not unt else RED,
                           "§17: %d rule(s) claimed ASSERTED with no check that FAILS%s"
                           % (len(unt), (": " + ", ".join(unt)) if unt else ""),
                           asserted_untraceable=unt,
                           traceable=doc["traceable"]["n_traceable"],
                           claimed=doc["traceable"]["n_claimed_asserted"]))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("rules_all_classified", ENV_UNKNOWN, "rule_audit failed (%s)" % exc))
        gates.append(_gate("no_false_asserted", ENV_UNKNOWN, "rule_audit failed (%s)" % exc))

    # 4 — R4.4: the documented rule still matches the code.
    try:
        import consistency_check as cc
        recs = cc.check_all(tagged=True)
        errs = [r for r in recs if r.get("severity") == getattr(cc, "ERROR", "ERROR")]
        gates.append(_gate("consistency_pairs", GREEN if not errs else RED,
                           "%d ERROR-severity mismatch(es)" % len(errs),
                           errors=[e["message"][:200] for e in errs[:12]],
                           n_errors=len(errs), n_records=len(recs)))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("consistency_pairs", ENV_UNKNOWN,
                           "consistency_check could not run (%s) — R2.9/R5.12" % exc))

    # 5 — Phase 0: declaration and execution integrity.
    try:
        import framework_integrity as fi
        pf = fi.preflight(root)
        gates.append(_gate("framework_integrity", GREEN if pf.get("state") == "PASS" else RED,
                           "framework_integrity.preflight: %s" % pf.get("state"),
                           errors=pf.get("errors")))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("framework_integrity", ENV_UNKNOWN,
                           "framework_integrity could not run (%s)" % exc))

    # 6 — R4.14 / R15.6: produced -> executed -> consumed -> decision-effective.
    try:
        import capability_registry as cr
        rec = cr.reconcile(root)
        if rec.get("state") == "DISABLED":
            gates.append(_gate("capability_chain", UNKNOWN, rec["why"]))
        else:
            gates.append(_gate("capability_chain",
                               GREEN if rec["state"] == "PASS" else RED,
                               "R4.14: %d of %d capabilities LIVE; GBP %.2f of declared "
                               "exposure not proven decision-effective"
                               % (rec["n_live"], rec["n_capabilities"],
                                  rec["gbp_exposure_not_live"]),
                               blocked_at=rec["blocked_at"]))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("capability_chain", ENV_UNKNOWN,
                           "capability_registry could not run (%s)" % exc))

    # 7 — R5.5: the changed modules carry controls that can fail.
    ncc = negative_control_census(changed_modules, root)
    gates.append(_gate("negative_controls", ncc["state"], ncc["why"] or
                       "every changed module's selftest carries a labelled negative control",
                       rows=[r for r in ncc["rows"] if r["state"] != GREEN]))

    # 8 — R4.15: run surfaces dispositioned.
    if run_surfaces is None:
        gates.append(_gate("run_surface_dispositions", RED,
                           "R4.15: no run-surface dispositions were supplied. Omission is not "
                           "a verdict (R14.5, KR14)"))
    else:
        rsd = run_surface_dispositions(run_surfaces)
        gates.append(_gate("run_surface_dispositions", rsd["state"], rsd["why"],
                           undispositioned=rsd["undispositioned"],
                           unevidenced=rsd["unevidenced"]))

    # 9 — R7.8: the items this build claims are validated against the CURRENT Trusted Build.
    prev = load_receipt(root)
    # ⚑⚑ THE BOOTSTRAP, AND IT IS DECLARED RATHER THAN PAPERED OVER. R7.8 asks whether an item
    #    was validated against the CURRENT Trusted Build. Before the first receipt exists there
    #    is no current Trusted Build, so the honest answer for the very first certification is
    #    that the items were validated against the build BEING certified — recorded as
    #    `bootstrap: true` on the gate so a reader can see this receipt is the origin of the
    #    chain and not a link in it. Every subsequent build compares against the real predecessor.
    bootstrap = prev is None
    ic = item_currency(root, build_id=(prev or {}).get("build_id") or build_id)
    claimed_stale = [s for s in (ic.get("stale") or []) if s["id"] in set(items)]
    gates.append(_gate("item_currency",
                       GREEN if not claimed_stale else RED,
                       "R7.8: %d of the %d item(s) this build claims are not validated against "
                       "the current Trusted Build" % (len(claimed_stale), len(items)),
                       stale_claimed=claimed_stale, bootstrap=bootstrap,
                       bootstrap_note=(None if not bootstrap else
                                       "no prior Trusted Build receipt existed; the items this "
                                       "build claims are validated against the build being "
                                       "certified. This receipt is the origin of the chain."),
                       n_stale_backlog_wide=ic.get("n_stale")))

    # 10 — R19: the BuildSpec contract.
    if spec is None:
        gates.append(_gate("buildspec", RED,
                           "R19.1: no material build starts from chat prose or an old "
                           "corrective action alone (KR17)"))
    else:
        gaps = buildspec_gaps(spec)
        cur = spec_currency(spec, (prev or {}).get("build_id"))
        gates.append(_gate("buildspec",
                           GREEN if not gaps and cur["state"] == GREEN else RED,
                           "R19.2: %d category gap(s); currency: %s"
                           % (len(gaps), cur["why"]), gaps=gaps))

    # 11 — R5.12: the Candidate could reproduce the declared baseline at all.
    fps = live_fingerprints(root)
    envs = {k: v.get("why") for k, v in fps.items()
            if v.get("state") in (ENV_UNKNOWN, UNKNOWN)}
    gates.append(_gate("candidate_parity", GREEN if not envs else ENV_UNKNOWN,
                       "R5.12: %d surface(s) could not be fingerprinted from this host"
                       % len(envs) if envs else
                       "every certifiable surface fingerprinted from this host",
                       unknowns=envs))

    for g in gates:
        if g["gate"] in waived and g["state"] != GREEN:
            g["waived"] = next(w for w in waivers if w["gate"] == g["gate"])
    blocking = [g for g in gates if g["state"] != GREEN and "waived" not in g]
    return {
        "build_id": build_id,
        "as_of": _today(),
        "certified": not blocking,
        "state": "CERTIFIED" if not blocking else "BLOCKED",
        "gates": gates,
        "blocking": [{"gate": g["gate"], "state": g["state"], "why": g["why"]} for g in blocking],
        "waivers": waivers,
        "items": items,
        "changed_modules": sorted(set(changed_modules)),
        "previous_build_id": (prev or {}).get("build_id"),
        "fingerprints": fps,
        "basis": ("R18.3 — one canonical release gate is the only supported route to LIVE. Only "
                  "GREEN counts; ENVIRONMENT_UNKNOWN blocks rather than passes (R4.3, R5.12); a "
                  "waived gate is recorded as waived, never as green (§11)."),
    }


def promote(certification: dict, root: str = HERE, *, promoted_by: str,
            rollback_to: Optional[str] = None, force: bool = False) -> dict:
    """R18.4 — atomic promotion. REFUSES an uncertified candidate unless explicitly forced with
    a recorded waiver, and keeps the previous receipt recoverable."""
    if not certification.get("certified") and not force:
        raise ReleaseRefused(
            "promotion refused: %d gate(s) blocking — %s. R18.3: one canonical release gate is "
            "the only supported route to LIVE, and a blocked gate is not a slow gate."
            % (len(certification.get("blocking") or []),
               ", ".join(g["gate"] for g in (certification.get("blocking") or []))))
    prev = load_receipt(root)
    sd = state_dir(root)
    os.makedirs(os.path.join(sd, RECEIPT_DIR), exist_ok=True)
    receipt = dict(certification)
    receipt.update({
        "promoted_on": _today(),
        "promoted_by": promoted_by,
        "rollback_to": rollback_to or (prev or {}).get("build_id"),
        "forced": bool(force and not certification.get("certified")),
        "receipt_kind": "TRUSTED_BUILD_RECEIPT",
    })
    if prev:
        with open(os.path.join(sd, RECEIPT_DIR, "%s.json" % prev.get("build_id", "previous")),
                  "w", encoding="utf-8") as fh:
            json.dump(prev, fh, indent=1, sort_keys=True)
    tmp = receipt_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=1, sort_keys=True)
    os.replace(tmp, receipt_path(root))                  # atomic (R18.4)
    with open(os.path.join(sd, RECEIPT_DIR, "%s.json" % receipt["build_id"]),
              "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=1, sort_keys=True)
    return receipt


def post_promotion_verify(root: str = HERE) -> dict:
    """R18.4/R5.9 — the delivered-location identity check, run AFTER promotion."""
    v = verify_live(root)
    return {"state": v["state"], "why": v["why"], "build_id": v.get("build_id"),
            "basis": ("R5.9: candidate certification happens in the Candidate; the exact "
                      "certified artefact is then promoted and identity-checked in the "
                      "delivered LIVE location. A sandbox-only pass is not production evidence.")}


# ────────────────────────────────────────────────────────────────────────────────────────
# selftest
# ────────────────────────────────────────────────────────────────────────────────────────

_ASSERTS = [0]


def _selftest(verbose: bool = True) -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        _ASSERTS[0] += 1
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:240]) if not cond else ""))
        if not cond:
            fails.append(name)

    # ── R18.5: no receipt, a drifted receipt, and a matching one ────────────────────
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, STATE_REL))
    with open(os.path.join(tmp, "m.py"), "w", encoding="utf-8") as fh:
        fh.write("def f():\n    return 1\n")
    with open(os.path.join(tmp, "ISA_Engineering_Rules.md"), "w", encoding="utf-8") as fh:
        fh.write("# rules\n")
    # ⚑ R5.12 IN THE FIXTURE ITSELF. The first version of this control built a temp tree with no
    #   config files, and `verify_live` correctly refused it as ENVIRONMENT_UNKNOWN — which read
    #   as the control failing when it was the FIXTURE that was incomplete. A candidate that
    #   cannot reproduce the declared surfaces is not evidence about correctness, and that
    #   applies to a test's own candidate too.
    for _cfg in ("isa_policy.py", "scoring_config.py", "target_state.json",
                 "target_weights.json", "threshold_register.json", "quantity_register.json",
                 "negative_claims.json", "degradation_bands.json"):
        with open(os.path.join(tmp, _cfg), "w", encoding="utf-8") as fh:
            fh.write("{}\n" if _cfg.endswith(".json") else "# stub\n")
    # ⚑ AND A RUN SURFACE. `run_surface_fingerprint` was passing the REAL repository root while
    #   fingerprinting a temp candidate — so the receipt would have carried the live tree's
    #   surfaces under a different tree's build id, and the control would have gone green on
    #   the wrong evidence. Fixed to honour `root`; the fixture now has to supply one, which is
    #   the fix proving itself.
    os.makedirs(os.path.join(tmp, "Skills_to_Edit", "a-task"), exist_ok=True)
    with open(os.path.join(tmp, "Skills_to_Edit", "a-task", "SKILL.md"), "w",
              encoding="utf-8") as fh:
        fh.write("# a task\n\nSee ISA_Engineering_Rules.md.\n")
    v = verify_live(tmp)
    ok("⚑ NO RECEIPT is UNTRUSTED and BLOCKS a capital run - 'never signed' must not read as "
       "'signed and unchanged' (R18.5, R2.10)",
       v["state"] == "NO_RECEIPT" and v["blocks_capital_run"] is True, v)

    fp = live_fingerprints(tmp)
    rec = {"build_id": "TB-TEST-01", "fingerprints": fp}
    with open(receipt_path(tmp), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    v = verify_live(tmp)
    ok("a LIVE state identical to the receipt reads TRUSTED and does not block",
       v["state"] == "TRUSTED" and v["blocks_capital_run"] is False, v)
    ok("⚑ ...and a surface that could not be fingerprinted at all would have BLOCKED rather "
       "than passed, so the TRUSTED above is a match and not an absence of looking (R5.12, "
       "R2.10)",
       verify_live(tmp, {"build_id": "TB-X", "fingerprints": dict(
           fp, config={"state": ENV_UNKNOWN, "why": "control"})}
       )["state"] in ("UNTRUSTED_LIVE_STATE", ENV_UNKNOWN))

    with open(os.path.join(tmp, "m.py"), "a", encoding="utf-8") as fh:
        fh.write("def g():\n    return 2\n")
    v = verify_live(tmp)
    ok("⚑ NEGATIVE CONTROL: ONE unsigned line of source flips LIVE to UNTRUSTED_LIVE_STATE and "
       "blocks the capital run - this is the mechanical meaning of 'the rules actually ran' "
       "(R18.5, KR10)",
       v["state"] == "UNTRUSTED_LIVE_STATE" and v["blocks_capital_run"] is True, v)
    ok("...and it NAMES the file that changed rather than reporting a bare hash mismatch",
       any("m.py" in f for d in v["diffs"] if d["surface"] == "source"
           for f in d.get("changed_files", [])), v["diffs"])

    with open(os.path.join(tmp, "ISA_Engineering_Rules.md"), "w", encoding="utf-8") as fh:
        fh.write("# rules\n\nan edit nobody signed\n")
    with open(receipt_path(tmp), "w", encoding="utf-8") as fh:
        json.dump({"build_id": "TB-TEST-01", "fingerprints": fp}, fh)
    v = verify_live(tmp)
    ok("⚑ the STANDARD ITSELF is a fingerprinted surface: editing ISA_Engineering_Rules.md "
       "without a receipt is UNTRUSTED, so the file that governs every build cannot drift "
       "outside §11's DECISION route",
       v["state"] == "UNTRUSTED_LIVE_STATE"
       and any(d["surface"] == "rules" for d in v["diffs"]), v)

    # ── §19 — silence is not N/A ────────────────────────────────────────────────────
    full = {c: "stated" for c in BUILDSPEC_CATEGORIES}
    ok("a complete BuildSpec reports no gaps", buildspec_gaps(full) == [])
    part = dict(full); part.pop("data_contract")
    ok("⚑ NEGATIVE CONTROL: a missing category is ABSENT, not inferred",
       [g["category"] for g in buildspec_gaps(part)] == ["data_contract"])
    na = dict(full); na["learning_and_ml"] = "N/A"
    ok("⚑ NEGATIVE CONTROL: `N/A` with no reason is a GAP - R19.2 says silence is not N/A",
       [g["state"] for g in buildspec_gaps(na)] == ["NA_WITHOUT_REASON"])
    na2 = dict(full); na2["learning_and_ml"] = "N/A — no data exhaust; the change is a parser fix"
    ok("...and `N/A` WITH a reason is a disposition, not a gap (R14.5)",
       buildspec_gaps(na2) == [])
    ok("R19.3: a spec validated against an older build is REVALIDATION_REQUIRED",
       spec_currency({"validated_against_build_id": "TB-OLD"}, "TB-NEW")["state"] == RED)
    ok("...and one validated against the current build passes - the control is not vacuous",
       spec_currency({"validated_against_build_id": "TB-NEW"}, "TB-NEW")["state"] == GREEN)

    # ── R4.15 — an unlisted surface is not a silent pass ────────────────────────────
    good = {s: {"verdict": "VERIFIED_NO_CHANGE", "evidence": "diffed, unchanged"}
            for s in RUN_SURFACES}
    ok("every surface dispositioned WITH evidence is green",
       run_surface_dispositions(good)["state"] == GREEN)
    miss = dict(good); miss.pop("skill_mirror")
    ok("⚑ NEGATIVE CONTROL: an unlisted run surface FAILS - 'nobody mentioned it' cannot be "
       "read as 'it needs no change' (R4.15, R14.5, KR14)",
       run_surface_dispositions(miss)["undispositioned"] == ["skill_mirror"])
    noev = dict(good); noev["run_context"] = {"verdict": "VERIFIED_NO_CHANGE"}
    ok("⚑ NEGATIVE CONTROL: VERIFIED_NO_CHANGE without evidence FAILS - the verdict is the "
       "claim, the evidence is the proof (R14.5)",
       run_surface_dispositions(noev)["unevidenced"] == ["run_context"])

    # ── R7.8 / R7.9 ─────────────────────────────────────────────────────────────────
    items = [{"id": "ISA-0001", "state": "OPEN", "build_readiness": "BUILD_READY",
              "criticality": "CRITICAL", "title": "x", "validated_against_build_id": "TB-OLD"},
             {"id": "ISA-0002", "state": "OPEN", "build_readiness": "BUILD_READY",
              "criticality": "LOW", "title": "y", "validated_against_build_id": "TB-NEW"},
             {"id": "ISA-0003", "state": "CLOSED_FIXED", "build_readiness": "BUILD_READY",
              "criticality": "CRITICAL", "title": "z"}]
    ic = item_currency(HERE, build_id="TB-NEW", items=items)
    ok("⚑ R7.8: a BUILD_READY item validated against an older build is STALE authority (KR13) - "
       "the rule ISA-0467 itself failed for twelve days",
       ic["state"] == RED and [s["id"] for s in ic["stale"]] == ["ISA-0001"], ic)
    ok("...and a CLOSED item is not dragged in - the gate is about actionable authority",
       all(s["id"] != "ISA-0003" for s in ic["stale"]))
    ok("⚑ NEGATIVE CONTROL (FC-A): with NO Trusted Build id, every actionable item is stale - "
       "the first version compared None to None, read them EQUAL, and reported 0 of 42 current "
       "on a tree with no receipt at all (R4.3: a control fed a null BLOCKS)",
       item_currency(HERE, build_id=None, items=items)["n_stale"] == 2,
       item_currency(HERE, build_id=None, items=items)["n_stale"])
    ok("R7.9: an item whose footprint intersects a changed module is returned for revalidation",
       invalidate_touched(
           [{"id": "ISA-0009", "state": "OPEN", "title": "fix capital_destination.sleeve_split"}],
           {"modules": ["capital_destination"]}) == ["ISA-0009"])
    ok("⚑ NEGATIVE CONTROL: an EMPTY footprint invalidates nothing - a build that touches "
       "nothing must not re-baseline the whole backlog",
       invalidate_touched([{"id": "ISA-0009", "state": "OPEN", "title": "x"}], {}) == [])

    # ── R5.5 census, and A12 ────────────────────────────────────────────────────────
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "hollow.py"), "w", encoding="utf-8") as fh:
        fh.write("def _selftest():\n    return 0\n")
    with open(os.path.join(d, "real.py"), "w", encoding="utf-8") as fh:
        fh.write('def _selftest():\n'
                 '    ok("NEGATIVE CONTROL: a broken input must fail", True)\n'
                 '    return 0\n')
    cen = negative_control_census(["hollow", "real"], d)
    ok("⚑ NEGATIVE CONTROL: a selftest with no assertions and no labelled control FAILS the "
       "census - the vacuous pass (ISA-0348, ISA-0513)",
       [r["module"] for r in cen["rows"] if r["state"] == RED] == ["hollow"], cen["rows"])
    ok("...and a selftest that carries one PASSES, so the failure above means 'hollow', not "
       "'this census rejects everything'",
       any(r["module"] == "real" and r["state"] == GREEN for r in cen["rows"]))
    ok("⚑ SELF-EXCLUSION (A12/R10): release_gate is not a subject of its own census",
       all(r["module"] != SELF_MODULE
           for r in negative_control_census([SELF_MODULE, "real"], d)["rows"]))

    # ── promotion refuses an uncertified candidate ──────────────────────────────────
    raised = False
    try:
        promote({"certified": False, "blocking": [{"gate": "atlas_current"}], "build_id": "X"},
                tmp, promoted_by="control")
    except ReleaseRefused:
        raised = True
    ok("⚑ REFUSAL IS THE PRODUCT: promote() RAISES on an uncertified candidate rather than "
       "writing a receipt with a caveat in it (R14.2, R18.3)", raised)
    r = promote({"certified": True, "build_id": "TB-TEST-02", "gates": [], "fingerprints": {}},
                tmp, promoted_by="control")
    ok("...and a certified candidate promotes, writing a receipt that names its rollback "
       "target (R18.4)", r["build_id"] == "TB-TEST-02" and "rollback_to" in r)
    ok("the previous receipt is retained, so the preceding Trusted Build stays recoverable "
       "through the declared stability window (R18.4)",
       os.path.exists(os.path.join(tmp, STATE_REL, RECEIPT_DIR, "TB-TEST-01.json")))

    if verbose:
        print("\nrelease_gate selftest: %d assertion(s), %d FAIL(s)%s"
              % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    if "--verify-live" in argv:
        v = verify_live()
        print(json.dumps(v, indent=2))
        return 0 if v["state"] == "TRUSTED" else 1
    if "--fingerprint" in argv:
        fp = live_fingerprints()
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "files"
                              and kk != "digests"} for k, v in fp.items()}, indent=2))
        return 0
    print(json.dumps(verify_live(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
