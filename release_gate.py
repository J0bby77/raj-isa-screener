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
import re
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


def _fingerprint_files(paths, root: str) -> Dict[str, str]:
    """⚑ ISA-0697 (16-Sep-2026): keys are relative to the TREE, not to the process's working
    directory. `os.path.relpath(p)` made every fingerprint a function of `cwd`: the unchanged LIVE
    tree verified TRUSTED from inside the folder and UNTRUSTED_LIVE_STATE (428 'changed' files,
    keyed '../sessions/.../x.py') from anywhere else — and the scheduled pre-run invokes
    monthly_isa_prerun.py by absolute path with no declared cwd. `root` is now mandatory (R4.7:
    an un-updated caller fails rather than silently keeping the old keying)."""
    out = {}
    for p in sorted(paths):
        try:
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = _sha(fh.read())
        except OSError:
            continue
    return out


def _roll(d: Dict[str, str]) -> str:
    return _sha(json.dumps(d, sort_keys=True).encode("utf-8"))


def source_fingerprint(root: str = HERE) -> dict:
    files = _fingerprint_files(_iter_source(root), root)
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


# R18.5 config surface — ONE home (the selftest fixture reads it too, R4.4).
# ⚑ ISA-0465/0700 pre-LIVE close-out (16-Sep-2026): `concentration_theme_taxonomy.json` is the
#   declared golden source the theme cap refuses capital on. It was outside every fingerprint, so a
#   hand edit to a membership would have left LIVE reading TRUSTED (KR10 blind to a capital input).
CONFIG_FILES = ("isa_policy.py", "scoring_config.py", "target_state.json", "target_weights.json",
                "threshold_register.json", "quantity_register.json", "negative_claims.json",
                "degradation_bands.json", "concentration_theme_taxonomy.json",
                # ISA-0473: the write-once grandfather baseline of the deliverable intake gate is
                # signed - an unsigned edit could otherwise grandfather a violating deliverable.
                os.path.join("Dashboard", "state", "deliverable_intake_baseline.json"),
                # ISA-0607 (23-Sep-2026): Raj's broker venue declaration decides which venues may
                # receive automated new capital - an unsigned edit to it would move capital.
                "broker_venues.json",
                # ISA-0729 (23-Sep-2026): the declared release denominator of the script battery - an
                # unsigned edit could quietly move a failing release test out of the census.
                "script_suite_census.json")


def config_fingerprint(root: str = HERE) -> dict:
    names = CONFIG_FILES
    present = [os.path.join(root, n) for n in names if os.path.exists(os.path.join(root, n))]
    missing = [n for n in names if not os.path.exists(os.path.join(root, n))]
    files = _fingerprint_files(present, root)
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


# ISA-0746 (24-Sep-2026) — THE CENSUS IS BOUND TO EVERY SIGNED SURFACE, NOT TO SOURCE ALONE.
# A selftest reads more than .py: capability_registry.json (the real-registry must-fire), the
# config files, the Engineering Rules, the atlas manifest and the run surfaces. On 24-Sep-2026
# TB-24-02 changed ONLY capability_registry.json, every suite row was re-used GREEN because the
# source roll matched, and LIVE carried a failing must-fire. One home for the census identity;
# record_suite_status (reuse), pair_red_suite_is_an_incident (build gate), suite_status_state
# (capital preflight) and suite_census_runner (publish) all read it.
CENSUS_SURFACES = ("source", "config", "capabilities", "rules", "atlas", "run_surfaces")


def census_roll(root: str = HERE, fps: Optional[dict] = None) -> dict:
    """-> {"roll": sha over every CENSUS_SURFACES roll, "components": {surface: roll}}.
    ⚑ An UNAVAILABLE surface (e.g. run surfaces not enumerable on a host) enters the roll as the
      explicit token "UNAVAILABLE" rather than voiding it: the roll stays deterministic, a change in
      availability still changes it (so nothing is re-used across it), and a host that cannot
      enumerate one surface does not lose its census - which would REFUSE the capital run."""
    fps = fps if fps is not None else live_fingerprints(root)
    comps = {k: ((fps.get(k) or {}).get("roll") or "UNAVAILABLE") for k in CENSUS_SURFACES}
    return {"roll": _roll(comps), "components": comps,
            "unavailable": sorted(k for k, v in comps.items() if v == "UNAVAILABLE")}


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
            if key in ("source", "config"):
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
            "live_rolls": {k: (now.get(k) or {}).get("roll") for k in
                           ("source", "rules", "run_surfaces", "config", "atlas", "capabilities")},
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

# R18.5 — THE REFUSAL, AT THE RUN SURFACE (ISA-0629, 16-Sep-2026)
# ⚑ WHAT WAS MISSING. `verify_live()` answered R18.5's question and NOTHING THAT MOVES CAPITAL
#   ASKED IT: no call site in monthly_isa_prerun, the VCI run capture or any SKILL/Run_Context.
#   Its only live consumer was `consistency_check.pair_live_is_trusted`, which runs at pre-run
#   Step 9d — AFTER every capital figure is computed — and lands as one line in errors[]. An
#   unsigned tree would have produced a complete, plausible run_context with a red line underneath
#   it, and the artefact is what gets read. "Blocks a capital-decision run" was prose.
# ⚑ THE CONTRACT. One function, one vocabulary, stamped onto the artefact the decision is read
#   from (R4.11 — capture is a property of producing the artefact):
#     AUTHORISED    LIVE matches the Trusted receipt on every surface — capital decisions may proceed
#     REFUSED       UNTRUSTED_LIVE_STATE / NO_RECEIPT / ENVIRONMENT_UNKNOWN / DISABLED / UNKNOWN —
#                   no capital decision may be taken from this run's outputs until reconciled
#     NOT_ENFORCED  the rollback flag is off; the untrusted state is RECORDED and the decision is
#                   Raj's, never implied AUTHORISED
#   Anything else, including absence, is read as REFUSED by every consumer (R4.3).
CAPITAL_AUTHORITY_STATES = ("AUTHORISED", "REFUSED", "NOT_ENFORCED")


def _refuse_untrusted() -> bool:
    """Rollback constant (R4.13). Default TRUE — the refusal is the adopted rule (R18.5), so
    turning it off must be a deliberate act (V2_FLAGS['refuse_untrusted_capital_run'] = False),
    not the consequence of a missing key."""
    try:
        import isa_policy as _p
        if "refuse_untrusted_capital_run" in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS["refuse_untrusted_capital_run"])
    except Exception:                                                   # noqa: BLE001
        pass
    return True


def _census_gates_capital() -> bool:
    """Rollback constant (R4.13) for ISA-0696. Default TRUE: a capital run needs a FRESH_GREEN suite
    census as well as a TRUSTED tree. V2_FLAGS['census_gates_capital'] = False records NOT_ENFORCED."""
    try:
        import isa_policy as _p
        if "census_gates_capital" in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS["census_gates_capital"])
    except Exception:                                                   # noqa: BLE001
        pass
    return True


def capital_run_authority(surface: str, root: str = HERE) -> dict:
    """R18.5 — may a capital-decision run on `surface` proceed on this tree? Records the exact
    identity it checked (build id + every surface roll) so the run can say WHAT it ran.

    ⚑ ISA-0696 (17-Sep-2026): AUTHORISED additionally requires the suite census to read FRESH_GREEN
    through consistency_check.suite_status_state (complete, all GREEN, CLEAN isolation, <= 8 days,
    recorded against this exact source and config). A TRUSTED tree whose census is stale, red,
    incomplete or other-identity is REFUSED (NOT_ENFORCED under the census rollback), never AUTHORISED."""
    import datetime as _dt
    try:
        v = verify_live(root)
    except Exception as exc:                                            # noqa: BLE001
        v = {"state": UNKNOWN, "why": "verify_live raised %s: %s" % (type(exc).__name__, exc),
             "blocks_capital_run": True}
    trusted = v.get("state") == "TRUSTED" and v.get("blocks_capital_run") is False
    try:
        import consistency_check as _cc
        suite = _cc.suite_status_state(root)
    except Exception as exc:                                            # noqa: BLE001
        suite = {"state": "ABSENT", "why": "suite census could not be read (%s: %s) - UNKNOWN, never usable"
                                          % (type(exc).__name__, exc)}
    census_ok = suite.get("state") == "FRESH_GREEN"
    if trusted and census_ok:
        authority = "AUTHORISED"
    elif not trusted:
        authority = "REFUSED" if _refuse_untrusted() else "NOT_ENFORCED"
    else:
        authority = "REFUSED" if _census_gates_capital() else "NOT_ENFORCED"
    why = v.get("why")
    if trusted and not census_ok:
        why = ("LIVE is TRUSTED but the suite census is %s: %s (ISA-0696 - run "
               "`python3 suite_census_runner.py --step` until CENSUS_PUBLISHED)" % (suite.get("state"), suite.get("why")))
    return {
        "surface": surface,
        "authority": authority,
        "live_state": v.get("state"),
        "build_id": v.get("build_id"),
        "promoted_on": v.get("promoted_on"),
        "live_rolls": v.get("live_rolls"),
        "diffs": v.get("diffs"),
        "unknowns": v.get("unknowns"),
        "why": why,
        "suite_census": {k: suite.get(k) for k in ("state", "why", "as_of_ts", "age_hours", "age_days",
                                                  "refuses_after", "next_due", "denominator", "n_non_green",
                                                  "non_green", "identity_mismatch", "isolation_all_clean",
                                                  "data_drift", "census_kind", "build_id")},
        "census_gates_capital": _census_gates_capital(),
        "checked_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "basis": ("R18.5 — a scheduled capital run refuses unsigned changes. AUTHORISED only when "
                  "LIVE matches the Trusted receipt on every fingerprinted surface AND the suite census "
                  "is FRESH_GREEN for that exact source/config (ISA-0696); anything else is REFUSED (or "
                  "NOT_ENFORCED under the recorded rollback), never PASS (R4.3)."),
    }


def capital_authority_of(doc) -> str:
    """The ONE reader of an artefact's stamped authority. Absent/unknown -> REFUSED (R4.3)."""
    a = ((doc or {}).get("_meta") or {}).get("capital_authority")
    if a is None:
        a = ((doc or {}).get("trusted_build") or {}).get("authority")
    return a if a in CAPITAL_AUTHORITY_STATES else "REFUSED"


def _gate(name, state, why, **extra):
    d = {"gate": name, "state": state, "why": why}
    d.update(extra)
    return d


# ─────────────────────────────────────────────────────────────────────────────────────────
# ISA-0695 (BS-0695 §5D) — EXACT WAIVER OWNERSHIP
# ─────────────────────────────────────────────────────────────────────────────────────────
# ⚑ MEASURED FAILURE: a waiver was {gate, item, why}. On 17-Sep-2026 a consistency_pairs waiver written for
#   3 known errors absorbed 11 (8 stale register views appeared after it was written), and the
#   framework_integrity waiver cited an item (ISA-0683) that owned none of the gate's findings. A waiver now
#   owns FINDINGS, not gates: every current finding id of the waived gate must be listed with an owning
#   canonical item at its exact revision, the item must be open and must NAME the finding id, and any finding
#   not listed keeps the gate blocking. A partially owned RED stays RED and unwaived.
_TERMINAL_ITEM_STATES = ("CLOSED_FIXED", "CLOSED_WONTFIX", "CLOSED_NOT_A_DEFECT", "SUPERSEDED")


def _norm_finding_text(text: str) -> str:
    t = str(text)
    while True:                                   # nested parentheses, innermost first
        t2 = re.sub(r"\([^()]*\)", "", t)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"TB-\d{4}-\d{2}-\d{2}-\d{2}", "TB", t)
    t = re.sub(r"[0-9a-f]{8,}", "H", t)
    t = re.sub(r"\d+(\.\d+)?", "N", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def text_finding_id(prefix: str, text: str) -> str:
    """Stable id for a message-shaped finding: its rule prefix + a hash of the message with numbers,
    hashes, build ids and parenthesised lists removed (they change run to run; the defect does not)."""
    head = str(text).split(":", 1)[0][:40]
    return "%s-%s-%s" % (prefix, re.sub(r"[^A-Za-z0-9.]+", "_", head).strip("_"),
                         hashlib.sha1(_norm_finding_text(text).encode("utf-8")).hexdigest()[:10])


def waiver_check(gate: dict, waiver: dict, read_item=None) -> dict:
    """-> {"valid": bool, "unowned": [ids], "owner_errors": [...], "stale": [ids], "owned": {id: owner}}"""
    if read_item is None:
        import isa_register as _R

        def read_item(i):
            return _R.get(i)
    current = [f["id"] for f in (gate.get("findings") or [])]
    listed = {}
    errs = []
    if not isinstance(waiver.get("findings"), list) or not waiver["findings"]:
        return {"valid": False, "unowned": current, "owner_errors": [
            "waiver for %s lists no findings - a gate-level waiver cannot prove it owns what it hides "
            "(ISA-0695)" % gate.get("gate")], "stale": [], "owned": {}}
    for w in waiver["findings"]:
        fid, owner, rev = w.get("id"), w.get("owner"), w.get("owner_revision")
        if not fid or not re.match(r"^ISA-\d{4}$", str(owner or "")):
            errs.append("malformed waiver finding %r" % w)
            continue
        listed[fid] = owner
        try:
            item = read_item(owner)
        except Exception as exc:                                        # noqa: BLE001
            errs.append("%s: owner %s unreadable (%s)" % (fid, owner, exc))
            continue
        if item.get("state") in _TERMINAL_ITEM_STATES:
            errs.append("%s: owner %s is %s - a closed item cannot own a live finding" % (fid, owner, item.get("state")))
        if rev is None or int(item.get("revision") or 0) != int(rev):
            errs.append("%s: owner %s is at revision %s, waiver cites %s - re-read the owner" % (
                fid, owner, item.get("revision"), rev))
        if fid in current and fid not in json.dumps(item, ensure_ascii=False):
            errs.append("%s: owner %s does not name this finding id - citing an item is not owning a finding" % (fid, owner))
    unowned = [f for f in current if f not in listed]
    stale = [f for f in listed if f not in current]
    return {"valid": not unowned and not errs, "unowned": unowned, "owner_errors": errs, "stale": stale,
            "owned": {f: listed[f] for f in current if f in listed}}


def certify(root: str = HERE, *, build_id: str, items: List[str],
            changed_modules: List[str], spec: Optional[dict] = None,
            run_surfaces: Optional[dict] = None,
            waivers: Optional[List[dict]] = None,
            orientation_receipts: Optional[List[dict]] = None) -> dict:
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
                           findings=[{"id": text_finding_id("CP", e["message"]), "text": e["message"][:300]}
                                     for e in errs],
                           n_errors=len(errs), n_records=len(recs)))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("consistency_pairs", ENV_UNKNOWN,
                           "consistency_check could not run (%s) — R2.9/R5.12" % exc))

    # 4b — R5.7 / ISA-0683: EVERY suite on disk is recorded GREEN against THIS source. Read
    #      through the pair's one home (consistency_check.pair_red_suite_is_an_incident), so the
    #      gate and the battery cannot disagree about what "green" means (R4.4). Recording is
    #      the expensive half and is done beforehand in the Candidate
    #      (consistency_check.record_suite_status); an absent, partial, stale or other-source
    #      record is RED here, never a skip (R2.10).
    try:
        import consistency_check as _cc_s
        _se = _cc_s.pair_red_suite_is_an_incident(root=root)
        gates.append(_gate("suite_census", GREEN if not _se else RED,
                           "R5.7/ISA-0683: %d suite-census finding(s)" % len(_se),
                           errors=[e[:240] for e in _se[:12]],
                           findings=[{"id": text_finding_id("SC", e), "text": e[:300]} for e in _se]))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("suite_census", ENV_UNKNOWN,
                           "suite census could not be read (%s) — R2.9/R5.12" % exc))

    # 5 — Phase 0: declaration and execution integrity.
    try:
        import framework_integrity as fi
        pf = fi.preflight(root)
        # ⚑ ISA-0695: preflight returns OK/FAIL; this compared against "PASS", so the gate could never be GREEN.
        _red = [f for f in (pf.get("findings") or []) if f.get("severity") == "RED"]
        _fi_state = GREEN if (pf.get("state") in ("OK", "PASS") and pf.get("findings") is not None) else RED
        gates.append(_gate("framework_integrity", _fi_state,
                           "framework_integrity.preflight: %s (%d RED finding(s), counts %s)"
                           % (pf.get("state"), len(_red), (pf.get("counts") or {}).get("by_severity")),
                           errors=pf.get("errors"),
                           findings=([{"id": f["id"], "text": "%s %s: %s" % (f["check"], f["subject"], f["why"])[:300],
                                       "declared_owner": f.get("owner")} for f in _red]
                                     if pf.get("findings") is not None else
                                     [{"id": "FI-LEDGER-UNAVAILABLE", "text": "; ".join(pf.get("errors") or [])}]),
                           counts=pf.get("counts")))
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
                               blocked_at=rec["blocked_at"],
                               findings=[{"id": "CC-%s" % r["name"], "text": "%s not LIVE: blocked at %s (%s)"
                                          % (r["name"], r["live"]["blocked_at"], r["live"]["state"])}
                                         for r in rec["rows"] if not r["live"]["live"]]))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("capability_chain", ENV_UNKNOWN,
                           "capability_registry could not run (%s)" % exc))

    # 7 — R5.5: the changed modules carry controls that can fail.
    ncc = negative_control_census(changed_modules, root)
    gates.append(_gate("negative_controls", ncc["state"], ncc["why"] or
                       "every changed module's selftest carries a labelled negative control",
                       rows=[r for r in ncc["rows"] if r["state"] != GREEN],
                       findings=[{"id": "NC-%s" % r["module"], "text": str(r.get("why"))[:300]}
                                 for r in ncc["rows"] if r["state"] != GREEN]))

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

    # 11 — ISA-0666: any orientation receipt that leant on a SCOPED-ADVERSE allowance must
    #      still be valid at certification. Three mechanical conditions (see
    #      discussion_preflight.scoped_receipt_valid): the receipt is ORIENTED; it was written
    #      against THIS source; and every item it scoped against is an item this build claims.
    #      The third is the anti-grandfathering rule — an allowance expires with its
    #      remediation, so it can never become a standing exemption. A build that used no
    #      scoped allowance passes trivially; silence is not a pass, an absent receipt with a
    #      claimed allowance is.
    try:
        import discussion_preflight as dpf
        # The receipt was written against LIVE BEFORE the work started — that is what makes
        # its `trusted_baseline` field GREEN and the receipt citable at all. So the roll it
        # must match is the PREVIOUS Trusted Build's signed source, not the Candidate's new
        # one. Matching the Candidate would make every allowance unusable by construction,
        # which is a dead control, not a strict one.
        base_roll = ((prev or {}).get("fingerprints", {}).get("source") or {}).get("roll")
        checks = [dict(dpf.scoped_receipt_valid(rc, build_items=items, root=root,
                                                expected_source_roll=base_roll),
                       receipt=rc.get("id"), subject=rc.get("subject"))
                  for rc in (orientation_receipts or [])]
        bad = [c for c in checks if not c["valid"]]
        gates.append(_gate("preflight_scope", GREEN if not bad else RED,
                           ("ISA-0666: %d orientation receipt(s) checked, %d invalid "
                            "scoped-adverse allowance(s)" % (len(checks), len(bad))),
                           checks=checks,
                           **({"findings": [{"id": "PS-%s" % (c.get("receipt") or "?"),
                                             "text": c["why"][:300]} for c in bad]}
                              if bad else {})))
    except Exception as exc:                                            # noqa: BLE001
        gates.append(_gate("preflight_scope", ENV_UNKNOWN,
                           "discussion_preflight unavailable (%s) — R2.9" % exc))

    # 12 — R5.12: the Candidate could reproduce the declared baseline at all.
    fps = live_fingerprints(root)
    envs = {k: v.get("why") for k, v in fps.items()
            if v.get("state") in (ENV_UNKNOWN, UNKNOWN)}
    gates.append(_gate("candidate_parity", GREEN if not envs else ENV_UNKNOWN,
                       "R5.12: %d surface(s) could not be fingerprinted from this host"
                       % len(envs) if envs else
                       "every certifiable surface fingerprinted from this host",
                       unknowns=envs))

    for g in gates:
        if g["state"] != GREEN and not g.get("findings"):
            g["findings"] = [{"id": "G-%s" % g["gate"], "text": str(g.get("why"))[:300]}]
        if g["gate"] in waived and g["state"] != GREEN:
            w = next(w for w in waivers if w["gate"] == g["gate"])
            chk = waiver_check(g, w)
            g["waiver_validation"] = chk
            if chk["valid"]:
                g["waived"] = w
    blocking = [g for g in gates if g["state"] != GREEN and "waived" not in g]
    return {
        "build_id": build_id,
        "as_of": _today(),
        "certified": not blocking,
        "state": "CERTIFIED" if not blocking else "BLOCKED",
        "gates": gates,
        "blocking": [{"gate": g["gate"], "state": g["state"], "why": g["why"],
                      "waiver_validation": g.get("waiver_validation")} for g in blocking],
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


# ────────────────────────────────────────────────────────────────────────────────────────
# R18.4 — THE PROMOTION FOOTPRINT (ISA-0729 / ISA-0629, 23-Sep-2026)
# ────────────────────────────────────────────────────────────────────────────────────────
# ⚑ WHY. `promote()` signs a receipt; it never moved a file. Every promotion since TB-2026-09-09-01
#   copied files by hand from a diff of Candidate against LIVE, so anything a Candidate test had
#   rewritten looked like part of the build. TB-2026-09-23-04 promoted a Candidate-mutated
#   disagreement_log.json and implied_m_history.json (an M* PREDICTION appended by a replay, not by
#   a run), and the "restore" re-used a backup that already held the mutated copies. The footprint is
#   now COMPUTED: only fingerprinted (signed) surfaces move by default; any other changed file is
#   RUNTIME STATE and is REFUSED unless the build declares it by name with a reason.
PROMOTION_NEVER = ("suite_status_sandbox", ".isa_census_sandbox")


def signed_paths(root: str = HERE) -> set:
    """Every file a Trusted receipt fingerprints, tree-relative (R18.3 surfaces)."""
    out = set(source_fingerprint(root)["files"])
    if os.path.exists(os.path.join(root, "ISA_Engineering_Rules.md")):
        out.add("ISA_Engineering_Rules.md")
    out |= {n for n in CONFIG_FILES if os.path.exists(os.path.join(root, n))}
    for rel in (os.path.join(STATE_REL, "framework_atlas_manifest.json"),
                os.path.join(STATE_REL, "capability_registry.json")):
        if os.path.exists(os.path.join(root, rel)):
            out.add(rel)
    # run surfaces: the ONE enumeration (framework_atlas.RUN_SURFACE_GLOBS), resolved to tree files
    try:
        sys.path.insert(0, root)
        import framework_atlas as _fa
        from pathlib import Path as _P
        for _pat in _fa.RUN_SURFACE_GLOBS:
            for _f in _P(root).glob(_pat):
                _rel = os.path.relpath(str(_f), root)
                if _f.is_file() and not any(x in _f.as_posix() for x in ("archive", "_bak", "_baseline")):
                    out.add(os.path.normpath(_rel))
    except Exception:                                                   # noqa: BLE001
        pass
    return out


def _tree_shas(root: str) -> Dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not any(d == x or d.startswith(x) for x in EXCLUDE_PARTS)
                       and d not in PROMOTION_NEVER]
        for fn in filenames:
            if fn.endswith((".pyc", ".tmp")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = _sha(fh.read())
            except OSError:
                continue
    return out


def promotion_plan(candidate_root: str, live_root: str = HERE, declared_data: Optional[dict] = None) -> dict:
    """R18.4 — the exact file footprint a promotion may copy, computed, never hand-picked.

    -> {"state": "READY"|"REFUSED", "copy": [{path, kind, sha, reason?}], "refused_runtime_state": [...],
        "live_only": [...], ...}. kind is SIGNED (a fingerprinted surface) or DECLARED_DATA (named by the
    build with a reason). Anything else that differs is UNDECLARED RUNTIME STATE and REFUSES the plan:
    mutable Candidate state never reaches LIVE as an incidental consequence of a source promotion."""
    declared_data = {os.path.normpath(k): v for k, v in (declared_data or {}).items()}
    bad_decl = sorted(k for k, v in declared_data.items() if not str(v or "").strip())
    cand, live = _tree_shas(candidate_root), _tree_shas(live_root)
    signed = signed_paths(candidate_root) | signed_paths(live_root)
    copy, refused = [], []
    for rel in sorted(cand):
        if live.get(rel) == cand[rel]:
            continue
        if rel in signed:
            copy.append({"path": rel, "kind": "SIGNED", "sha": cand[rel], "new": rel not in live})
        elif rel in declared_data and rel not in bad_decl:
            copy.append({"path": rel, "kind": "DECLARED_DATA", "sha": cand[rel], "new": rel not in live,
                         "reason": declared_data[rel]})
        else:
            refused.append(rel)
    undeclared_missing = sorted(k for k in declared_data if k not in cand)
    state = "READY" if (copy and not refused and not bad_decl and not undeclared_missing) else "REFUSED"
    why = ("%d signed + %d declared data file(s) to copy" % (sum(1 for c in copy if c["kind"] == "SIGNED"),
                                                             sum(1 for c in copy if c["kind"] == "DECLARED_DATA"))
           if state == "READY" else
           "REFUSED: %s" % "; ".join(x for x in (
               ("%d undeclared runtime-state file(s) differ: %s" % (len(refused), ", ".join(refused[:8]))) if refused else "",
               ("declared data without a reason: %s" % bad_decl) if bad_decl else "",
               ("declared data not in the Candidate: %s" % undeclared_missing) if undeclared_missing else "",
               "nothing to promote" if not copy else "") if x))
    return {"state": state, "why": why, "copy": copy, "refused_runtime_state": refused,
            "live_only": sorted(k for k in live if k not in cand)[:50],
            "candidate_root": os.path.realpath(candidate_root), "live_root": os.path.realpath(live_root),
            "basis": "R18.4/ISA-0729: only SIGNED surfaces move by default; runtime state moves only when declared."}


def apply_promotion(plan: dict, backup_dir: str, *, candidate_root: Optional[str] = None,
                    live_root: Optional[str] = None) -> dict:
    """Copy EXACTLY the plan, backing up every LIVE file it overwrites FIRST, then sha-verify, then
    prove no other LIVE data file moved while it ran. Refuses a plan that is not READY (R18.3)."""
    if plan.get("state") != "READY":
        raise ReleaseRefused("promotion plan is %s - %s" % (plan.get("state"), plan.get("why")))
    cand = candidate_root or plan["candidate_root"]
    live = live_root or plan["live_root"]
    import shutil as _sh
    try:
        import consistency_check as _cc
        snap0 = _cc.data_snapshot(live)["files"]
    except Exception:                                                   # noqa: BLE001
        _cc, snap0 = None, None
    os.makedirs(backup_dir, exist_ok=True)
    done = []
    for c in plan["copy"]:
        src, dst = os.path.join(cand, c["path"]), os.path.join(live, c["path"])
        with open(src, "rb") as fh:
            if _sha(fh.read()) != c["sha"]:
                raise ReleaseRefused("%s changed in the Candidate after the plan was made" % c["path"])
        if os.path.exists(dst):
            b = os.path.join(backup_dir, c["path"])
            os.makedirs(os.path.dirname(b), exist_ok=True)
            _sh.copy2(dst, b)
        os.makedirs(os.path.dirname(dst) or live, exist_ok=True)
        tmp = dst + ".promote.tmp"
        _sh.copy2(src, tmp)
        os.replace(tmp, dst)
        with open(dst, "rb") as fh:
            ok_sha = _sha(fh.read()) == c["sha"]
        done.append(dict(c, verified=ok_sha))
    drift = None
    if _cc is not None:
        snap1 = _cc.data_snapshot(live)["files"]
        moved = {c["path"] for c in plan["copy"]}
        drift = sorted(k for k in set(snap0) | set(snap1) if snap0.get(k) != snap1.get(k) and k not in moved)
    manifest = {"applied_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "n_copied": len(done), "all_verified": all(d["verified"] for d in done),
                "copied": done, "backup_dir": os.path.realpath(backup_dir),
                "undeclared_live_data_drift": drift,
                "state": ("APPLIED" if all(d["verified"] for d in done) and not drift else
                          "APPLIED_WITH_DRIFT" if all(d["verified"] for d in done) else "VERIFY_FAILED"),
                "rollback": "copy every file under backup_dir back over LIVE (new files: delete) - R4.13"}
    with open(os.path.join(backup_dir, "_promotion_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest


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
    for _cfg in CONFIG_FILES:
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

    # ── ISA-0465/0700: the declared theme taxonomy is a fingerprinted capital input ─────────
    _tx = os.path.join(tmp, "concentration_theme_taxonomy.json")
    _tx_orig = open(_tx, "rb").read() if os.path.exists(_tx) else None
    with open(_tx, "w", encoding="utf-8") as fh:
        fh.write('{"memberships": {"HAND_EDIT": {}}}\n')
    _v_tx = verify_live(tmp)
    if _tx_orig is None:
        os.remove(_tx)
    else:
        with open(_tx, "wb") as fh:
            fh.write(_tx_orig)
    ok("ISA-0700 MUST-FIRE: a hand edit to concentration_theme_taxonomy.json is UNTRUSTED_LIVE_STATE "
       "naming the file (it gates capital, so it must not verify TRUSTED)",
       _v_tx["state"] == "UNTRUSTED_LIVE_STATE" and any(
           d.get("surface") == "config" and "concentration_theme_taxonomy.json" in d.get("changed_files", [])
           for d in _v_tx.get("diffs", [])), _v_tx)
    ok("ISA-0700 comparator: restoring the signed taxonomy bytes verifies TRUSTED again",
       verify_live(tmp)["state"] == "TRUSTED")

    # ── ISA-0697: the verdict must not depend on the process's working directory ────────────
    _cwd = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp())
        _v_elsewhere = verify_live(tmp)
    finally:
        os.chdir(_cwd)
    ok("ISA-0697 MUST-FIRE: the same signed tree verifies TRUSTED from an UNRELATED working "
       "directory - a fingerprint keyed on cwd made unchanged LIVE read UNTRUSTED from anywhere "
       "but inside the folder", _v_elsewhere["state"] == "TRUSTED", _v_elsewhere)
    ok("ISA-0697: fingerprint keys are tree-relative (no '..' segments)",
       all(not k.startswith("..") for k in source_fingerprint(tmp)["files"]))

    # ── ISA-0629: the refusal AT THE RUN SURFACE, on the same fixture ───────────────────
    # ISA-0696: the signed fixture carries a complete, CLEAN, same-identity census (its tree has no suites)
    import consistency_check as _ccf
    import datetime as _dtf
    _st_path = os.path.join(tmp, "Dashboard", "state", "suite_status.json")
    os.makedirs(os.path.dirname(_st_path), exist_ok=True)
    _fresh = {"as_of": _dtf.date.today().isoformat(),
              "as_of_ts": _dtf.datetime.now().isoformat(timespec="seconds"),
              "source_roll": source_fingerprint(tmp)["roll"], "config_roll": config_fingerprint(tmp)["roll"],
              "census_roll": census_roll(tmp)["roll"],
              "rows": [{"module": r["module"], "state": "GREEN", "write_isolation": "CLEAN"}
                       for r in _ccf.suite_census(tmp)],
              "isolation": {"all_clean": True}, "data_snapshot": _ccf.data_snapshot(tmp)}
    with open(_st_path, "w", encoding="utf-8") as fh:
        json.dump(_fresh, fh)
    _ca = capital_run_authority("fixture_run", tmp)
    ok("ISA-0629 POSITIVE CONTROL: a signed, unchanged tree gives capital authority AUTHORISED "
       "and records the build id and every surface roll it checked",
       _ca["authority"] == "AUTHORISED" and _ca["build_id"] == "TB-TEST-01"
       and set((_ca.get("live_rolls") or {})) >= {"source", "config", "run_surfaces"}, _ca)
    _stale = dict(_fresh, as_of_ts=(_dtf.datetime.now() - _dtf.timedelta(days=9)).isoformat(timespec="seconds"))
    with open(_st_path, "w", encoding="utf-8") as fh:
        json.dump(_stale, fh)
    _ca_st = capital_run_authority("fixture_run", tmp)
    ok("ISA-0696 MUST-FIRE: the SAME signed, TRUSTED tree with a 9-day-old census is REFUSED, naming STALE",
       _ca_st["authority"] == "REFUSED" and _ca_st["live_state"] == "TRUSTED"
       and _ca_st["suite_census"]["state"] == "STALE", _ca_st)
    _red = dict(_fresh, rows=_fresh["rows"] + [{"module": "data_dependent_suite", "state": "RED"}])
    with open(_st_path, "w", encoding="utf-8") as fh:
        json.dump(_red, fh)
    ok("ISA-0696 MUST-FIRE: a fresh census with a RED suite REFUSES capital authority (FRESH_RED)",
       capital_run_authority("fixture_run", tmp)["suite_census"]["state"] == "FRESH_RED"
       and capital_run_authority("fixture_run", tmp)["authority"] == "REFUSED")
    try:
        import isa_policy as _polc
        _hadc = "census_gates_capital" in _polc.V2_FLAGS
        _oldc = _polc.V2_FLAGS.get("census_gates_capital")
        _polc.V2_FLAGS["census_gates_capital"] = False
        _ca_nc = capital_run_authority("fixture_run", tmp)
        if _hadc:
            _polc.V2_FLAGS["census_gates_capital"] = _oldc
        else:
            _polc.V2_FLAGS.pop("census_gates_capital", None)
        ok("ISA-0696 ROLLBACK: census gate off -> NOT_ENFORCED (recorded), never AUTHORISED on a red census",
           _ca_nc["authority"] == "NOT_ENFORCED", _ca_nc)
    except ImportError:
        ok("ISA-0696 ROLLBACK control could not import isa_policy - UNKNOWN, not PASS", False)
    os.remove(_st_path)
    ok("ISA-0696 NEGATIVE CONTROL: no census at all on a TRUSTED tree is REFUSED (ABSENT)",
       capital_run_authority("fixture_run", tmp)["suite_census"]["state"] == "ABSENT"
       and capital_run_authority("fixture_run", tmp)["authority"] == "REFUSED")
    with open(_st_path, "w", encoding="utf-8") as fh:
        json.dump(_fresh, fh)
    with open(os.path.join(tmp, "m.py"), "a", encoding="utf-8") as fh:
        fh.write("def g():\n    return 2\n")
    _ca2 = capital_run_authority("fixture_run", tmp)
    ok("ISA-0629 MUST-FIRE: one unsigned line of source makes capital authority REFUSED",
       _ca2["authority"] == "REFUSED" and _ca2["live_state"] == "UNTRUSTED_LIVE_STATE", _ca2)
    try:
        import isa_policy as _pol
        _had = "refuse_untrusted_capital_run" in _pol.V2_FLAGS
        _old = _pol.V2_FLAGS.get("refuse_untrusted_capital_run")
        _pol.V2_FLAGS["refuse_untrusted_capital_run"] = False
        _ca3 = capital_run_authority("fixture_run", tmp)
        if _had:
            _pol.V2_FLAGS["refuse_untrusted_capital_run"] = _old
        else:
            _pol.V2_FLAGS.pop("refuse_untrusted_capital_run", None)
        ok("ISA-0629 ROLLBACK: with the flag off the same tree is NOT_ENFORCED - recorded, never "
           "AUTHORISED (R4.3, R4.13)", _ca3["authority"] == "NOT_ENFORCED", _ca3)
    except ImportError:
        ok("ISA-0629 ROLLBACK control could not import isa_policy - UNKNOWN, not PASS", False)
    ok("ISA-0629: an artefact with NO stamped authority reads REFUSED through the one reader",
       capital_authority_of({}) == "REFUSED"
       and capital_authority_of({"_meta": {"capital_authority": "AUTHORISED"}}) == "AUTHORISED"
       and capital_authority_of({"_meta": {"capital_authority": "MAYBE"}}) == "REFUSED")
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

    # ── ISA-0695: exact waiver ownership (BS-0695 §15) ────────────────────────────────────
    _items = {"ISA-9001": {"id": "ISA-9001", "state": "OPEN", "revision": 3,
                           "corrective_action": "owns FI-T-A and FI-Q3-B"},
              "ISA-9002": {"id": "ISA-9002", "state": "CLOSED_FIXED", "revision": 2, "corrective_action": "FI-T-A"},
              "ISA-9003": {"id": "ISA-9003", "state": "OPEN", "revision": 1,
                           "corrective_action": "mentions the framework_integrity gate but no finding id"}}
    _ri = lambda i: _items[i]                                                   # noqa: E731
    _g = {"gate": "framework_integrity", "findings": [{"id": "FI-T-A"}, {"id": "FI-Q3-B"}]}
    _full = {"gate": "framework_integrity", "findings": [{"id": "FI-T-A", "owner": "ISA-9001", "owner_revision": 3},
                                                         {"id": "FI-Q3-B", "owner": "ISA-9001", "owner_revision": 3}]}
    ok("ISA-0695 POSITIVE CONTROL: every current finding listed with an open owner at its exact revision that names it -> valid",
       waiver_check(_g, _full, _ri)["valid"] is True, waiver_check(_g, _full, _ri))
    _drop = dict(_full, findings=_full["findings"][:1])
    ok("ISA-0695 MUST-FIRE: removing one finding's owner from the waiver leaves it UNOWNED and the gate blocking",
       waiver_check(_g, _drop, _ri)["unowned"] == ["FI-Q3-B"] and not waiver_check(_g, _drop, _ri)["valid"])
    _new = {"gate": "framework_integrity", "findings": _g["findings"] + [{"id": "FI-Q1-NEW"}]}
    ok("ISA-0695 MUST-FIRE: a NEW finding under the same gate is not absorbed by the old waiver",
       waiver_check(_new, _full, _ri)["unowned"] == ["FI-Q1-NEW"])
    ok("ISA-0695 NEGATIVE CONTROL: an owner item that mentions the gate but not the finding id does not own it",
       not waiver_check({"gate": "g", "findings": [{"id": "FI-T-A"}]},
                        {"gate": "g", "findings": [{"id": "FI-T-A", "owner": "ISA-9003", "owner_revision": 1}]}, _ri)["valid"])
    ok("ISA-0695 NEGATIVE CONTROL: a CLOSED owner cannot own a live finding",
       not waiver_check({"gate": "g", "findings": [{"id": "FI-T-A"}]},
                        {"gate": "g", "findings": [{"id": "FI-T-A", "owner": "ISA-9002", "owner_revision": 2}]}, _ri)["valid"])
    ok("ISA-0695 NEGATIVE CONTROL: a stale owner revision is refused",
       not waiver_check(_g, dict(_full, findings=[dict(f, owner_revision=2) for f in _full["findings"]]), _ri)["valid"])
    ok("ISA-0695 NEGATIVE CONTROL: a legacy gate-level waiver (no findings) is refused",
       not waiver_check(_g, {"gate": "framework_integrity", "item": "ISA-9001", "why": "x"}, _ri)["valid"])
    ok("ISA-0695: message finding ids are stable across changing numbers, build ids and file lists",
       text_finding_id("CP", "R18.5/KR10: UNTRUSTED against TB-2026-09-17-03 - source (5 file(s): a.py, b.py); 12 errs")
       == text_finding_id("CP", "R18.5/KR10: UNTRUSTED against TB-2026-09-17-04 - source (2 file(s): c.py); 7 errs"))
    ok("ISA-0695 NEGATIVE CONTROL: different defects get different ids",
       text_finding_id("CP", "ISA-0596: key ABSENT from run_context") != text_finding_id("CP", "ISA-0229: retrospective has ZERO findings"))

    # ══ ISA-0666 — the scoped-adverse allowance is policed at the release gate ═══════
    import discussion_preflight as _dpf
    _roll = (live_fingerprints(HERE).get("source") or {}).get("roll")   # stands in for the baseline roll
    _good = {"id": "OR-TEST-A", "subject": "x", "verdict": "ORIENTED",
             "adverse_inside_authorised_scope": ["what_executes_and_consumes"],
             "authorised_scope_items": ["ISA-9101"], "source_roll": _roll}
    ok("ISA-0666 POSITIVE CONTROL: a build claiming ISA-9101 may rely on a fresh receipt "
       "scoped to ISA-9101",
       _dpf.scoped_receipt_valid(_good, build_items=["ISA-9101"], root=HERE)["valid"])
    ok("⚑ ISA-0666 MUST-FIRE at the RELEASE GATE: an allowance scoped to an item this build "
       "does not claim is refused - a scoped adverse state must expire with its remediation, "
       "not become a standing exemption (Wave 2 BuildSpec §5.1)",
       not _dpf.scoped_receipt_valid(_good, build_items=["ISA-0001"], root=HERE)["valid"])
    ok("⚑ ISA-0666 NEGATIVE CONTROL: a receipt written against other source cannot license "
       "this certification",
       not _dpf.scoped_receipt_valid(dict(_good, source_roll="0" * 64),
                                     build_items=["ISA-9101"], root=HERE)["valid"])
    ok("ISA-0666: a certification that used NO scoped allowance is not blocked by the new gate",
       _dpf.scoped_receipt_valid({"id": "OR-TEST-B", "verdict": "ORIENTED"},
                                 build_items=["ISA-9101"], root=HERE)["valid"])

    # ── ISA-0729 / R18.4: the computed promotion footprint ─────────────────────────────────
    import tempfile as _tfp, shutil as _shp
    _pl, _pc = _tfp.mkdtemp(prefix="rg_live_"), _tfp.mkdtemp(prefix="rg_cand_")
    for _r in (_pl, _pc):
        open(os.path.join(_r, "mod_a.py"), "w").write("X = 1\n")
        open(os.path.join(_r, "runtime_log.json"), "w").write('{"n": 1}')
        open(os.path.join(_r, "ISA_Engineering_Rules.md"), "w").write("# rules\n")
    open(os.path.join(_pc, "mod_a.py"), "w").write("X = 2\n")
    _p0 = promotion_plan(_pc, _pl)
    ok("ISA-0729 POSITIVE CONTROL: a source-only Candidate diff is READY and moves exactly the signed file",
       _p0["state"] == "READY" and [c["path"] for c in _p0["copy"]] == ["mod_a.py"]
       and _p0["copy"][0]["kind"] == "SIGNED", _p0)
    open(os.path.join(_pc, "runtime_log.json"), "w").write('{"n": 2}')
    _p1 = promotion_plan(_pc, _pl)
    ok("ISA-0729 MUST-FIRE / NEGATIVE CONTROL: a Candidate-mutated runtime state file REFUSES the plan "
       "(the TB-2026-09-23-04 leak), naming the file",
       _p1["state"] == "REFUSED" and _p1["refused_runtime_state"] == ["runtime_log.json"], _p1)
    _p2 = promotion_plan(_pc, _pl, declared_data={"runtime_log.json": ""})
    ok("ISA-0729 NEGATIVE CONTROL: a declaration with no reason is not a declaration",
       _p2["state"] == "REFUSED", _p2)
    _p3 = promotion_plan(_pc, _pl, declared_data={"runtime_log.json": "certification census, deliberately promoted"})
    ok("ISA-0729 POSITIVE CONTROL: a named, reasoned data file is carried as DECLARED_DATA",
       _p3["state"] == "READY" and {c["kind"] for c in _p3["copy"]} == {"SIGNED", "DECLARED_DATA"}, _p3)
    _bk = _tfp.mkdtemp(prefix="rg_bak_")
    _m = apply_promotion(_p0 if False else promotion_plan(_pc, _pl, declared_data={
        "runtime_log.json": "fixture"}), _bk)
    ok("ISA-0729: apply_promotion copies, sha-verifies and backs up the overwritten LIVE file FIRST",
       _m["all_verified"] and open(os.path.join(_pl, "mod_a.py")).read() == "X = 2\n"
       and open(os.path.join(_bk, "mod_a.py")).read() == "X = 1\n", _m)
    try:
        apply_promotion(_p1, _bk)
        _refused = False
    except ReleaseRefused:
        _refused = True
    ok("ISA-0729 NEGATIVE CONTROL: a REFUSED plan cannot be applied", _refused)
    for _r in (_pl, _pc, _bk):
        _shp.rmtree(_r, ignore_errors=True)

    if verbose:
        print("\nrelease_gate selftest: %d assertion(s), %d FAIL(s)%s"
              % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    if "--capital-authority" in argv:
        i = argv.index("--capital-authority")
        surface = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") else "manual"
        a = capital_run_authority(surface)
        print(json.dumps(a, indent=2, default=str))
        return 0 if a["authority"] == "AUTHORISED" else 1
    if "--verify-live" in argv:
        v = verify_live()
        print(json.dumps(v, indent=2))
        return 0 if v["state"] == "TRUSTED" else 1
    if "--promotion-plan" in argv:
        # python3 release_gate.py --promotion-plan <candidate_root> [--declare path=reason ...]
        i = argv.index("--promotion-plan")
        decl = {}
        for j, a in enumerate(argv):
            if a == "--declare" and j + 1 < len(argv) and "=" in argv[j + 1]:
                k, v = argv[j + 1].split("=", 1)
                decl[k] = v
        p = promotion_plan(argv[i + 1], HERE, declared_data=decl)
        print(json.dumps(p, indent=1))
        return 0 if p["state"] == "READY" else 1
    if "--fingerprint" in argv:
        fp = live_fingerprints()
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "files"
                              and kk != "digests"} for k, v in fp.items()}, indent=2))
        return 0
    print(json.dumps(verify_live(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
