#!/usr/bin/env python3
"""
discussion_preflight.py — R12.4 / R12.5: conversation is a run surface, so it needs a preflight.

Authority: ISA_Engineering_Rules.md §12 (R12.1, R12.2, R12.4, R12.5), §2 (R2.15, R2.16),
§9 (R9.7, R9.8), §15 (R15.3, R15.5, R15.6), §18 (R18.5). Raised as ISA-0630 under ISA-0623.
Canonical store: `Dashboard/state/orientation_receipts/`.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
§12 has been binding on conversation since 09-Aug-2026 and has never once fired, because a
chat message runs no code. R12.1 says *"no material proposal, opinion or debate on any part of
the framework without a current Change Footprint"*; R14.1 says *"anything that depends on
someone remembering is a defect, INCLUDING ME REMEMBERING"*. Those two sentences are in direct
tension until something mechanical stands between a question and an answer.

R12.4 resolves it: **the Project Instructions call a named preflight; this module supplies and
validates the evidence; neither layer alone is sufficient.** The output is a citable orientation
receipt — an artefact with an id, so "I checked" becomes "receipt OR-2026-09-09-01 says", which
is the difference between a claim and a fact (R2.5: no confidence adverbs without a named
artefact).

⚑ R12.5 IS THE POINT, NOT THE PREFLIGHT. If the preflight cannot establish the current
framework, the answer is **UNVERIFIED, not confidently helpful**. A receipt whose verdict is
UNVERIFIED explicitly withholds three permissions: approving a BuildSpec, recommending a
capital-methodology change, and stating that a capability is complete or live. Being unable to
check and saying nothing must not produce the same output as checking and finding it clean
(R2.10).

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["discussion_preflight"] = False` — `preflight()` returns
`DISABLED`, which reads as UNKNOWN and never as ORIENTED.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_MODULE = "discussion_preflight"
RECEIPT_REL = os.path.join("Dashboard", "state", "orientation_receipts")

ORIENTED, UNVERIFIED = "ORIENTED", "UNVERIFIED"
UNKNOWN = "UNKNOWN"

# ── ISA-0666 (Raj 19-Sep-2026, Wave 2 BuildSpec §5.1) ───────────────────────────────────
# Three ESTABLISHMENT states, because "I could not establish this field" and "I established
# it and the answer is bad" were both rendering RED and both forcing UNVERIFIED. They are
# different facts and they carry different permissions:
#   ESTABLISHED_OK      — established, and the answer is clean.
#   ESTABLISHED_ADVERSE — established, and the answer is bad. May coexist with ORIENTED ONLY
#                         when every adverse finding is MECHANICALLY attributable to an item
#                         id inside the exact authorised remediation scope. Not asserted, not
#                         free-text, not "expected RED".
#   UNKNOWN_UNVERIFIED  — not established. ALWAYS blocks (R4.3: a control fed nothing returns
#                         UNKNOWN and blocks).
ESTABLISHED_OK = "ESTABLISHED_OK"
ESTABLISHED_ADVERSE = "ESTABLISHED_ADVERSE"
UNKNOWN_UNVERIFIED = "UNKNOWN_UNVERIFIED"

# A mandatory field whose adverse finding can never be scoped away, whatever the BuildSpec
# says. R18.5: an unsigned LIVE state is reconciled, never worked around.
UNSCOPEABLE_FIELDS = ("trusted_baseline",)

# R12.1's fourteen fields, in the standard's own order. This tuple is the ONE home (R4.4);
# `consistency_check.pair_orientation_fields` reads it rather than restating it.
FOOTPRINT_FIELDS = (
    "trusted_baseline", "what_is_already_there", "why_it_is_there", "what_is_not_there",
    "how_it_links_together", "dependencies_in_out_lateral", "what_executes_and_consumes",
    "data_state", "register_state_and_currency", "run_surfaces_affected",
    "what_must_be_tested", "economic_decision_path", "learning_ml_observability",
    "second_order_and_refactor",
)

# R12.4's floor: a receipt is only citable if AT LEAST these are established. The others may be
# UNKNOWN and the receipt still stands; these six may not.
MANDATORY_FIELDS = ("trusted_baseline", "how_it_links_together", "what_executes_and_consumes",
                    "register_state_and_currency", "run_surfaces_affected",
                    "economic_decision_path")

# R9.7 — reasoning classes. Routing is on complexity, uncertainty, architectural breadth,
# novelty and capital consequence, NEVER on priority or size alone.
REASONING_CLASSES = ("MECHANICAL", "ANALYTICAL", "ARCHITECTURAL", "CAPITAL_METHODOLOGY")

ESCALATION_TRIGGERS = (
    "unexpected architecture encountered",
    "a capital rule or threshold decision is required",
    "a source-of-truth conflict is found",
    "the scope is stale or superseded",
    "the BuildSpec is contradicted by what is on disk",
    "an economic or must-fire acceptance test fails",
    "a material fact cannot be established",
)


class PreflightRefused(RuntimeError):
    """Raised rather than returning a receipt that says nothing. An orientation that could not
    orient is a refusal, not a document."""


def _today() -> str:
    return datetime.date.today().isoformat()


def _flag() -> bool:
    try:
        import isa_policy as pol
        return bool(pol.flag("discussion_preflight"))
    except Exception:                                                   # noqa: BLE001
        return True


def receipt_dir(root: str = HERE) -> str:
    return os.path.join(root, RECEIPT_REL)


def establishment(field: dict) -> str:
    """ISA-0666 — the three-state reading of one R12.1 field.

    GREEN/PARTIAL -> ESTABLISHED_OK; RED -> ESTABLISHED_ADVERSE (a fact was established and it
    is bad); UNKNOWN/ENVIRONMENT_UNKNOWN/anything else -> UNKNOWN_UNVERIFIED (nothing was
    established). This is the ONE home for the mapping (R4.4) and `release_gate` reads it.
    """
    st = (field or {}).get("state")
    if st in ("GREEN", "PARTIAL"):
        return ESTABLISHED_OK
    if st == "RED":
        return ESTABLISHED_ADVERSE
    return UNKNOWN_UNVERIFIED


def adverse_owners(name: str, field: dict) -> dict:
    """ISA-0666 — which register item ids OWN this field's adverse findings, read from the
    finding itself, never from prose in a BuildSpec.

    Returns {"owned": [ISA-xxxx...], "unowned": [reason strings]}. An adverse field with ANY
    unowned finding can never be inside an authorised scope: if nothing in the register owns
    it, no BuildSpec can claim to be remediating it.
    """
    val = (field or {}).get("value") or {}
    owned, unowned = [], []
    if name in UNSCOPEABLE_FIELDS:
        unowned.append("%s is adverse: R18.5 requires reconciliation, not scoping" % name)
        return {"owned": [], "unowned": unowned}
    if name == "what_executes_and_consumes":
        rows = val.get("not_live_owners")
        if rows is None:
            unowned.append("capability owners were not enumerated by this receipt")
            return {"owned": [], "unowned": unowned}
        for r in rows:
            if r.get("owner"):
                owned.append(r["owner"])
            else:
                unowned.append("capability %s has no registered not_live owner" % r.get("name"))
    elif name == "register_state_and_currency":
        ids = val.get("stale_ids")
        if ids is None:
            unowned.append("stale item ids were not enumerated by this receipt")
        else:
            # An item whose authority is stale OWNS its own staleness: revalidating it is
            # exactly the remediation, so it is scopeable against itself and nothing else.
            owned.extend(ids)
    else:
        unowned.append("%s is adverse and carries no mechanical owner attribution" % name)
    return {"owned": sorted(set(owned)), "unowned": unowned}


def _field(state, value, why=None, source=None):
    return {"state": state, "value": value, "why": why, "source": source}


# ────────────────────────────────────────────────────────────────────────────────────────
# the fields
# ────────────────────────────────────────────────────────────────────────────────────────

def _f_trusted_baseline(root: str) -> dict:
    """R12.1 field 1 + R18.5. Which Trusted Build is LIVE, and is LIVE actually that build?"""
    try:
        import release_gate as rg
        v = rg.verify_live(root)
        atlas = rg.atlas_fingerprint(root)
        ok = v["state"] == "TRUSTED"
        return _field(
            "GREEN" if ok else "RED",
            {"trusted_state": v["state"], "build_id": v.get("build_id"),
             "why": v.get("why"), "atlas_run_id": atlas.get("run_id"),
             "atlas_as_of": atlas.get("as_of"), "atlas_modules": atlas.get("module_count")},
            None if ok else
            ("LIVE is not a signed Trusted Build (%s). Under R12.5 an answer built on an "
             "unverified baseline is UNVERIFIED, and under R18.5 a capital-decision run is "
             "blocked until reconciled." % v["state"]),
            "release_gate.verify_live + framework_atlas manifest")
    except Exception as exc:                                            # noqa: BLE001
        return _field(UNKNOWN, None, "release_gate unavailable (%s) — R2.9" % exc, None)


def _resolve(subject: str, root: str):
    """ISA-0663 — module, DECLARED area, or UNKNOWN. Never inferred from the name."""
    try:
        import framework_atlas as fa
        import capability_registry as cr
        from pathlib import Path as _P
        mods = set(fa.build(_P(root))["modules"])
        return cr.resolve_subject(subject, root, modules=mods)
    except Exception as exc:                                            # noqa: BLE001
        return {"kind": "ENVIRONMENT_UNKNOWN", "subject": subject, "modules": [],
                "why": "subject resolution unavailable (%s) - R2.9" % exc}


def _f_atlas_footprint(subject: str, root: str) -> Dict[str, dict]:
    """R12.1 fields 2, 4, 5, 6 - the mechanical half, from the Atlas (R15.3: cited with as_of).

    (ISA-0663, 12-Sep-2026) This used to call `framework_atlas.footprint(subject)`, which
    resolves exactly one MODULE NAME, so every cross-module subject came back RED as "not in
    the Atlas" - and `how_it_links_together` is MANDATORY, so the verdict was UNVERIFIED.
    R12.1 field 5 asks how a subject links together END TO END, which is inherently
    cross-module; the resolver could therefore only orient on the subjects that least needed
    it, and the RED read as a fact about the framework rather than about the resolver. A
    subject is now resolved to a module (on disk) or to a DECLARED area
    (`Dashboard/state/subject_areas.json`). A name that is neither stays RED.
    """
    out = {}
    res = _resolve(subject, root)
    fields = ("what_is_already_there", "how_it_links_together",
              "dependencies_in_out_lateral", "what_is_not_there")
    if res["kind"] == "UNKNOWN":
        for f in fields:
            out[f] = _field("RED", None, res["why"], None)
        return out
    if res["kind"] == "ENVIRONMENT_UNKNOWN":
        for f in fields:
            out[f] = _field(UNKNOWN, None, res["why"], None)
        return out
    try:
        import framework_atlas as fa
        from pathlib import Path as _P
        if res["kind"] == "area":
            fp = fa.area_footprint(subject, res["modules"], _P(root))
            out["what_is_already_there"] = _field(
                "GREEN", {"resolution": "area", "members": fp["members"],
                          "declared_by": res.get("declared_by"),
                          "members_declared_but_absent_from_disk":
                              fp["members_declared_but_absent_from_disk"],
                          "excluded_with_reason": res.get("excluded_with_reason"),
                          **fp["what_is_there"]},
                None, "capability_registry.resolve_subject + framework_atlas.area_footprint")
            out["how_it_links_together"] = _field(
                "GREEN", {"decision_path": res.get("decision_path"),
                          "internal_edges": fp["integration_path"]["internal_edges"],
                          "n_internal_edges": fp["integration_path"]["n_internal_edges"],
                          "reads": fp["integration_path"]["reads"],
                          "writes": fp["integration_path"]["writes"]},
                None, "framework_atlas.area_footprint")
            out["dependencies_in_out_lateral"] = _field(
                "GREEN", fp["dependencies_in_out"], None, "framework_atlas.area_footprint")
            out["what_is_not_there"] = _field(
                "GREEN", {"duplicate_constant_homes":
                              fp["degradation_duplication"]["duplicate_constant_homes"],
                          "zero_caller_functions":
                              fp["refactor_candidates"]["zero_caller_functions"]},
                ("R15.5: zero-caller here is STATIC. A function called only by its own "
                 "selftest HAS a caller and is not listed - liveness is answered by "
                 "what_executes_and_consumes, never by this field."),
                "framework_atlas.area_footprint")
            return out
        fp = fa.footprint(subject, _P(root))
        out["what_is_already_there"] = _field(
            "GREEN", {"resolution": "module", "functions": fp.get("functions"),
                      "module": subject}, None, "framework_atlas.footprint")
        out["how_it_links_together"] = _field(
            "GREEN", {"inbound_importers": fp.get("inbound"),
                      "outbound_imports": fp.get("outbound")},
            None, "framework_atlas.footprint")
        out["dependencies_in_out_lateral"] = _field(
            "GREEN", {"call_sites": fp.get("call_sites")}, None, "framework_atlas.footprint")
        out["what_is_not_there"] = _field(
            "GREEN", {"duplicate_homes": fp.get("duplicate_constant_homes")
                      or fp.get("duplicates")}, None, "framework_atlas.footprint")
    except KeyError as exc:
        for f in fields:
            out[f] = _field("RED", None,
                            "subject %r could not be resolved in the Atlas (%s). R2.8: read "
                            "the source, never the pointer - a subject the map does not "
                            "contain cannot be discussed from the map." % (subject, exc), None)
    except Exception as exc:                                            # noqa: BLE001
        for f in fields:
            out[f] = _field(UNKNOWN, None, "framework_atlas unavailable (%s)" % exc, None)
    return out


def _f_executes_and_consumes(subject: str, root: str) -> dict:
    """R12.1 field 7 + R15.5 + R4.14. Static reachability alone is NOT sufficient here."""
    try:
        import capability_registry as cr
        rec = cr.reconcile(root)
        if rec.get("state") == "DISABLED":
            return _field(UNKNOWN, None, rec["why"], None)
        # (ISA-0664, 12-Sep-2026) This used to fall back to `rec["rows"]` - EVERY row in the
        #   registry - whenever the subject matched none, and reported the widened count in a
        #   `why` string that said "in scope" without saying WHAT scope. `why` is what
        #   summarise() prints, so OR-2026-09-12-vci published "12 capabilities are not live"
        #   about a subject with ZERO declared capabilities; the twelve were unrelated. An
        #   empty result is a FINDING, not a licence to answer about a different population
        #   (R4.3/V-1), and the widened verdict was framework-wide, so no unmatched subject
        #   could ever be ORIENTED while anything anywhere was not live.
        members = set(_resolve(subject, root).get("modules") or [])
        rows = [r for r in rec["rows"]
                if subject in (r.get("producer") or "") or subject in r["name"]
                or (r.get("producer") or "").split(".")[0] in members]
        if not rows:
            return _field(
                UNKNOWN,
                {"scope": "subject", "n_capabilities": 0,
                 "resolved_members": sorted(members),
                 "registry_rows_total": len(rec["rows"])},
                ("R15.6 GAP: no capability is DECLARED for subject %r (its %d resolved "
                 "module(s) produce none of the registry's %d rows), so R4.14's chain cannot "
                 "be evaluated for it at all. UNKNOWN, not PASS and not the framework-wide "
                 "count: a control fed nothing returns UNKNOWN and blocks (R4.3)."
                 % (subject, len(members), len(rec["rows"]))),
                "capability_registry.reconcile")
        not_live = [{"name": r["name"], "blocked_at": r["live"]["blocked_at"],
                     "why": r["live"]["why"], "gbp_exposure": r["gbp_exposure"]}
                    for r in rows if not r["live"]["live"]]
        # ISA-0666: the OWNER of each not-live capability, read from the registry's own
        # not_live_reason rather than asserted, so an adverse finding can be checked
        # mechanically against an authorised remediation scope. Not truncated: attribution
        # must see every row, while `not_live` above stays short for the human summary.
        not_live_owners = [
            {"name": r["name"],
             "owner": ((r.get("not_live_reason") or {}).get("item")
                       if isinstance(r.get("not_live_reason"), dict) else None),
             "blocked_at": r["live"]["blocked_at"], "gbp_exposure": r["gbp_exposure"]}
            for r in rows if not r["live"]["live"]]
        return _field(
            "GREEN" if not not_live else "RED",
            {"scope": "subject", "subject": subject,
             "n_capabilities": len(rows), "n_live": len(rows) - len(not_live),
             "not_live": not_live[:10], "not_live_owners": not_live_owners},
            None if not not_live else
            ("R4.14: %d of %d capability(ies) DECLARED FOR SUBJECT %r are not proven "
             "PRODUCED -> EXECUTED -> CONSUMED -> DECISION-EFFECTIVE. The Atlas answers only "
             "the first (R15.5), and that is exactly how GBP 10,702.06 routed on a function "
             "no live caller reached (ISA-0454)."
             % (len(not_live), len(rows), subject)),
            "capability_registry.reconcile")
    except Exception as exc:                                            # noqa: BLE001
        return _field(UNKNOWN, None, "capability_registry unavailable (%s)" % exc, None)


def _f_register_state(subject: str, root: str) -> dict:
    """R12.1 field 9 + R7.8/R7.10 — active items, and whether their authority is current."""
    try:
        sys.path.insert(0, root)
        import isa_register as R
        import release_gate as rg
        items = R.read_all()
        pat = re.compile(re.escape(subject), re.I)
        hits = [i for i in items
                if i.get("state") in ("OPEN", "IN_PROGRESS", "BLOCKED_ON_RAJ")
                and pat.search(json.dumps(i))]
        prev = rg.load_receipt(root) or {}
        cur = rg.item_currency(root, build_id=prev.get("build_id"), items=items)
        stale_here = [s for s in (cur.get("stale") or [])
                      if any(s["id"] == h["id"] for h in hits)]
        return _field(
            "GREEN" if not stale_here else "RED",
            {"open_items_touching_subject": [
                {"id": i["id"], "criticality": i.get("criticality"),
                 "state": i.get("state"),
                 "validated_against_build_id": i.get("validated_against_build_id"),
                 "title": (i.get("title") or "")[:120]} for i in hits[:12]],
             "n_open": len(hits),
             "n_stale_authority": len(stale_here),
             "stale_ids": sorted({s["id"] for s in stale_here}),   # ISA-0666 attribution
             "current_trusted_build": prev.get("build_id")},
            None if not stale_here else
            ("R7.8: %d of the open items touching this subject are not validated against the "
             "current Trusted Build, so their corrective actions are proposals about an "
             "architecture that may no longer exist (KR13)." % len(stale_here)),
            "isa_register + release_gate.item_currency")
    except Exception as exc:                                            # noqa: BLE001
        return _field(UNKNOWN, None, "register unreadable (%s) — R2.9" % exc, None)


def _f_run_surfaces(root: str) -> dict:
    """R12.1 field 10 + ISA-0211 — and WHICH copy was read, executed or mirror."""
    try:
        import framework_atlas as fa
        wb = fa.run_surface_texts(with_basis=True)
        bases = {k: v[1] for k, v in wb.items()}
        n_exec = sum(1 for b in bases.values() if b == "executed")
        live_dir = fa.scheduled_skills_dir()
        return _field(
            "GREEN" if live_dir is not None else "PARTIAL",
            {"n_surfaces": len(bases), "n_from_executed_contract": n_exec, "basis": bases,
             "scheduled_dir_reachable": live_dir is not None},
            None if live_dir is not None else
            ("the scheduled directory is unreachable from this host, so the SKILL surfaces were "
             "read from the ISA-folder MIRROR and mirror drift COULD NOT BE CHECKED "
             "(ISA-0211). A mirror can never prove the live contract is current (R4.15)."),
            "framework_atlas.run_surface_texts")
    except Exception as exc:                                            # noqa: BLE001
        return _field(UNKNOWN, None, "run-surface enumeration unavailable (%s)" % exc, None)


def _f_economic_path(root: str) -> dict:
    """R12.1 field 12 + R16.1/R16.4 — objective -> capability -> decision -> capital."""
    anchor, anchor_src = None, None
    try:
        with open(os.path.join(root, "target_state.json"), encoding="utf-8") as fh:
            ts = json.load(fh)
        # 11-Sep-2026: target_state.json has never carried any of the four names below; the
        # operative anchor is `required_return_operative_pct` (the key scoring_config and
        # isa_policy read). Probing only the old names made this field UNKNOWN on every receipt.
        for k in ("required_return_operative_pct", "required_return_mid", "REQUIRED_RETURN_MID",
                  "required_return", "annualised_target"):
            if k in ts:
                anchor, anchor_src = ts[k], "target_state.json:%s" % k
                break
    except Exception:                                                   # noqa: BLE001
        pass
    exposure = None
    try:
        import capability_registry as cr
        rec = cr.reconcile(root)
        if rec.get("state") != "DISABLED":
            exposure = rec["gbp_exposure_not_live"]
    except Exception:                                                   # noqa: BLE001
        pass
    if anchor is None:
        return _field(UNKNOWN, {"gbp_exposure_not_proven_decision_effective": exposure},
                      ("the required-return anchor could not be read from target_state.json. "
                       "R16.1 requires every recommendation to state its link to the annualised "
                       "target OR state explicitly that it is infrastructure — silence is not "
                       "an option, and neither is guessing the number."), None)
    return _field("GREEN",
                  {"required_return_anchor": anchor,
                   "gbp_exposure_not_proven_decision_effective": exposure},
                  None, anchor_src)


# ────────────────────────────────────────────────────────────────────────────────────────
# R9.7 — the model / reasoning route
# ────────────────────────────────────────────────────────────────────────────────────────

def model_route(*, capital_consequence: bool, architectural_breadth: bool,
                novelty: bool, uncertainty: bool, mechanical_only: bool = False,
                as_of: Optional[str] = None) -> dict:
    """R9.7 — route on reasoning complexity, never on priority or size.

    ⚑ NO MODEL NAME IS HARD-CODED, EVER. R9.7 says the currently available model is resolved
    against Raj's plan/allowance IMMEDIATELY BEFORE EXECUTION and stamped `as_of`; a model or
    product entitlement written into the framework becomes a stale constant that governs
    capital work the day the plan changes. This function returns the CLASS and the requirement;
    the model is resolved at the moment of use and recorded on the item.
    """
    if capital_consequence and (architectural_breadth or novelty):
        cls, why = "CAPITAL_METHODOLOGY", ("changes how capital is decided AND is architecturally "
                                           "broad or novel")
    elif capital_consequence:
        cls, why = "CAPITAL_METHODOLOGY", "changes how capital is decided"
    elif architectural_breadth or novelty:
        cls, why = "ARCHITECTURAL", "crosses module or contract boundaries, or has no precedent"
    elif uncertainty:
        cls, why = "ANALYTICAL", "the answer is not determined by the code alone"
    elif mechanical_only:
        cls, why = "MECHANICAL", "a locked deterministic contract with no judgement left in it"
    else:
        cls, why = "ANALYTICAL", "default: uncertainty is assumed until it is measured"
    thinking = {"MECHANICAL": "low", "ANALYTICAL": "high",
                "ARCHITECTURAL": "high", "CAPITAL_METHODOLOGY": "high"}[cls]
    return {
        "reasoning_class": cls,
        "required_thinking_level": thinking,
        "routing_reason": why,
        "model": "RESOLVE_AT_EXECUTION",
        "model_resolution_rule": ("R9.7: resolve the currently available model against the "
                                  "current plan/allowance immediately before execution and "
                                  "stamp it `as_of`. Never hard-code a model, a version or a "
                                  "product entitlement as permanent policy."),
        "prefer": ("the lowest-cost model that satisfies the class; premium or usage-credit "
                   "routing requires an explicit incremental-value justification"),
        "escalation_triggers": list(ESCALATION_TRIGGERS),
        "split_rule": ("R9.8: high-reasoning work may establish target state, evidence, "
                       "architecture, BuildSpec and adversarial acceptance; lower-cost "
                       "execution may implement the locked deterministic contract, and must "
                       "STOP on any escalation trigger above."),
        "as_of": as_of or _today(),
    }


# ────────────────────────────────────────────────────────────────────────────────────────
# the preflight
# ────────────────────────────────────────────────────────────────────────────────────────

def preflight(subject: str, root: str = HERE, *, scope: str = "material",
              write: bool = False, authorised_scope_items=None,
              scope_authority: Optional[str] = None) -> dict:
    """R12.4 — the mechanical discussion preflight. Emits a citable orientation receipt.

    `scope` is R12.2's scaling: "material" requires all fourteen fields to be ATTEMPTED and the
    six mandatory ones to be established; "constant" requires fields 1, 2, 5, 6, 9, 10 and 12
    where material. Skipping is a waiver (§11) recorded on the item — not a quiet omission.
    """
    if not _flag():
        return {"state": "DISABLED", "verdict": UNVERIFIED,
                "why": "isa_policy.V2_FLAGS['discussion_preflight'] is False. DISABLED reads as "
                       "UNKNOWN and never as ORIENTED (R4.3)"}
    authorised = sorted({str(i).strip().upper() for i in (authorised_scope_items or [])})
    fields: Dict[str, dict] = {}
    fields["trusted_baseline"] = _f_trusted_baseline(root)
    fields.update(_f_atlas_footprint(subject, root))
    fields["what_executes_and_consumes"] = _f_executes_and_consumes(subject, root)
    fields["register_state_and_currency"] = _f_register_state(subject, root)
    fields["run_surfaces_affected"] = _f_run_surfaces(root)
    fields["economic_decision_path"] = _f_economic_path(root)
    for f in FOOTPRINT_FIELDS:
        fields.setdefault(f, _field(
            UNKNOWN, None,
            "not established by this preflight. R12.1: an unknown is written UNKNOWN rather "
            "than omitted, so the reader can see what was not looked at (R2.10).", None))

    # ── ISA-0666 (Raj 19-Sep-2026) — establishment, then scope ─────────────────────
    est = {f: establishment(fields[f]) for f in MANDATORY_FIELDS}
    unknown_mandatory = [f for f in MANDATORY_FIELDS if est[f] == UNKNOWN_UNVERIFIED]
    adverse_mandatory = [f for f in MANDATORY_FIELDS if est[f] == ESTABLISHED_ADVERSE]

    attribution, in_scope, out_of_scope = {}, [], []
    for f in adverse_mandatory:
        a = adverse_owners(f, fields[f])
        outside = [o for o in a["owned"] if o not in authorised]
        a["authorised"] = authorised
        a["outside_authorised_scope"] = outside
        a["in_scope"] = bool(authorised and a["owned"] and not outside and not a["unowned"])
        attribution[f] = a
        (in_scope if a["in_scope"] else out_of_scope).append(f)

    # R12.4's floor, restated in ISA-0666's terms: an UNKNOWN mandatory field blocks
    # unconditionally; an ADVERSE mandatory field blocks unless every one of its findings is
    # owned by an item inside the authorised remediation scope.
    unmet = sorted(set(unknown_mandatory) | set(out_of_scope))
    verdict = ORIENTED if not unmet else UNVERIFIED
    receipt = {
        "receipt_kind": "ORIENTATION_RECEIPT",
        "id": "OR-%s-%s" % (_today(), re.sub(r"[^A-Za-z0-9_.-]", "_", subject)[:48]),
        "as_of": _today(),
        "subject": subject,
        "scope": scope,
        "verdict": verdict,
        "unmet_mandatory_fields": unmet,
        "establishment": est,
        "unknown_mandatory_fields": unknown_mandatory,
        "adverse_mandatory_fields": adverse_mandatory,
        "adverse_attribution": attribution,
        "adverse_inside_authorised_scope": in_scope,
        "adverse_outside_authorised_scope": out_of_scope,
        "authorised_scope_items": authorised,
        "scope_authority": scope_authority,
        "scope_allowance": {
            "basis": ("ISA-0666 / Wave 2 BuildSpec §5.1 (Raj 19-Sep-2026): an ESTABLISHED_ADVERSE "
                      "mandatory field may coexist with ORIENTED only where every finding is "
                      "mechanically owned by an item id inside the authorised remediation scope. "
                      "An UNKNOWN_UNVERIFIED mandatory field blocks unconditionally."),
            "expires_with": authorised,
            "grandfathering": ("NONE. No allowance is persisted anywhere: owners are re-read "
                               "from the live registry/register on every call and the receipt is "
                               "bound to `source_roll` below, so it dies with the next source "
                               "change. A post-build preflight must be re-run."),
            "post_build_reverify_required": bool(in_scope),
        },
        "source_roll": _source_roll(root),
        "fields": fields,
        "permissions": {
            "may_describe_hypotheses_and_next_inspection": True,
            "may_approve_a_buildspec": verdict == ORIENTED,
            "may_recommend_a_capital_methodology_change": verdict == ORIENTED,
            "may_state_a_capability_is_complete_or_live": verdict == ORIENTED,
        },
        "r12_5": ("If the discussion preflight cannot establish the current framework, the "
                  "answer is UNVERIFIED, not confidently helpful. A chat message is not exempt "
                  "from R14.1 because it is not code."),
        "cite_as": ("quote this receipt id in the reply that gives the recommendation. 'I "
                    "checked' is a claim; a receipt id is an artefact (R2.5)."),
    }
    if write:
        d = receipt_dir(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, receipt["id"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=1, sort_keys=True)
        receipt["written_to"] = os.path.join(RECEIPT_REL, receipt["id"] + ".json")
    return receipt


def _source_roll(root: str) -> Optional[str]:
    """ISA-0666 — bind the receipt to the exact source it described.

    An orientation receipt that outlives the source it read is the grandfathering the rule
    forbids: the adverse state it scoped may have been replaced by a different one. The source
    fingerprint is release_gate's, so one home computes it (R4.4)."""
    try:
        import release_gate as rg
        return (rg.live_fingerprints(root).get("source") or {}).get("roll")
    except Exception:                                                   # noqa: BLE001
        return None


def scoped_receipt_valid(receipt: dict, *, build_items, root: str = HERE,
                         expected_source_roll: Optional[str] = None) -> dict:
    """ISA-0666 — may THIS certification rely on THIS receipt's scoped-adverse allowance?

    Three mechanical conditions, all of which must hold:
      1. the receipt is ORIENTED;
      2. its `source_roll` equals `expected_source_roll` — THE BASELINE THIS BUILD STARTED
         FROM, i.e. the previous Trusted Build's signed source, not the Candidate's new
         roll. A receipt is written against LIVE before the work begins (that is what R12.4
         asks for and what makes `trusted_baseline` GREEN at all); it must not have been
         written against some OTHER baseline. Omitting the argument falls back to `root`'s
         current source, which is the right check outside a certification;
      3. every item it scoped an adverse finding against is an item THIS build claims, so the
         allowance expires with the remediation rather than becoming permanent.
    A receipt that scoped nothing is trivially valid: there is no allowance to police.
    """
    items = {str(i).strip().upper() for i in (build_items or [])}
    scoped = list(receipt.get("adverse_inside_authorised_scope") or [])
    reasons = []
    if not scoped:
        return {"valid": True, "scoped_fields": [], "why": "no scoped-adverse allowance used",
                "reasons": []}
    if receipt.get("verdict") != ORIENTED:
        reasons.append("receipt verdict is %s" % receipt.get("verdict"))
    cur = expected_source_roll or _source_roll(root)
    if not receipt.get("source_roll") or receipt.get("source_roll") != cur:
        reasons.append("receipt source_roll %s != the baseline this build started from %s "
                       "(R5.9: a receipt written against other code is not evidence about "
                       "this one)"
                       % (str(receipt.get("source_roll"))[:12], str(cur)[:12]))
    claimed = set(receipt.get("authorised_scope_items") or [])
    outside = sorted(claimed - items)
    if outside:
        reasons.append("scoped against %s, which this build does not claim — an allowance that "
                       "outlives its remediation is grandfathering (ISA-0666)"
                       % ", ".join(outside))
    return {"valid": not reasons, "scoped_fields": scoped,
            "expected_source_roll": cur,
            "authorised_scope_items": sorted(claimed), "build_items": sorted(items),
            "reasons": reasons,
            "why": "scoped-adverse allowance accepted" if not reasons else "; ".join(reasons)}


def summarise(receipt: dict) -> str:
    """One paragraph a person can read, and a machine can paste into a reply."""
    if receipt.get("state") == "DISABLED":
        return "DISCUSSION PREFLIGHT DISABLED — treat every framework answer as UNVERIFIED."
    lines = ["%s  [%s]  subject=%s" % (receipt["id"], receipt["verdict"], receipt["subject"])]
    tb = receipt["fields"]["trusted_baseline"]["value"] or {}
    lines.append("  Trusted baseline: %s (build %s), atlas %s as_of %s"
                 % (tb.get("trusted_state"), tb.get("build_id"),
                    tb.get("atlas_run_id"), tb.get("atlas_as_of")))
    for f in MANDATORY_FIELDS:
        v = receipt["fields"][f]
        e = (receipt.get("establishment") or {}).get(f)
        if v["state"] not in ("GREEN", "PARTIAL"):
            scoped = f in (receipt.get("adverse_inside_authorised_scope") or [])
            lines.append("  %s %s: %s%s - %s"
                         % ("[scoped]" if scoped else "[BLOCKS]", f, e or v["state"],
                            (" (in authorised scope: %s)"
                             % ", ".join((receipt.get("adverse_attribution") or {})
                                         .get(f, {}).get("owned") or [])) if scoped else "",
                            (v["why"] or "")[:150]))
    if receipt["verdict"] == UNVERIFIED:
        lines.append("  ⚑⚑ R12.5: may NOT approve a BuildSpec, recommend a capital-methodology "
                     "change, or state a capability is complete/live.")
    return "\n".join(lines)


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
                  + (("  -- " + str(detail)[:240]) if not cond else ""))
        if not cond:
            fails.append(name)

    r = preflight("capital_destination", HERE)
    ok("the preflight emits a citable receipt with an id, not a paragraph",
       r.get("id", "").startswith("OR-") and r.get("receipt_kind") == "ORIENTATION_RECEIPT", r)
    ok("⚑ R12.1: all fourteen fields are PRESENT, with UNKNOWN written where nothing was "
       "established - an omitted field and an unestablished one must not look the same",
       set(r["fields"]) == set(FOOTPRINT_FIELDS),
       sorted(set(FOOTPRINT_FIELDS) ^ set(r["fields"])))
    ok("every field carries state, value, why and source - a value with no source is the thing "
       "R4.2 forbids", all(set(v) == {"state", "value", "why", "source"}
                           for v in r["fields"].values()))

    # ── R12.5 — the permission structure, and that it is not decorative ─────────────
    ok("the receipt states, as data, what an UNVERIFIED answer may NOT do",
       set(r["permissions"]) == {"may_describe_hypotheses_and_next_inspection",
                                 "may_approve_a_buildspec",
                                 "may_recommend_a_capital_methodology_change",
                                 "may_state_a_capability_is_complete_or_live"})
    ok("⚑ R12.5: an UNVERIFIED verdict withholds BuildSpec approval and capital-methodology "
       "recommendation, and still permits hypotheses and the next inspection - 'I could not "
       "check' must not become 'I have nothing to say' either (R2.10 in both directions)",
       (r["verdict"] == UNVERIFIED)
       == (r["permissions"]["may_approve_a_buildspec"] is False)
       and r["permissions"]["may_describe_hypotheses_and_next_inspection"] is True, r["verdict"])

    # ── a subject the Atlas does not contain is RED, not a shrug ────────────────────
    r2 = preflight("a_module_that_does_not_exist_anywhere", HERE)
    ok("⚑ NEGATIVE CONTROL: a subject the Atlas has never seen makes the link fields RED and "
       "the verdict UNVERIFIED - discussing a module the map does not contain is exactly the "
       "recollection R12.1 exists to replace (R2.8)",
       r2["fields"]["how_it_links_together"]["state"] == "RED"
       and r2["verdict"] == UNVERIFIED, r2["verdict"])
    ok("...and a real subject does better, so the failure above means 'not in the map', not "
       "'this preflight never orients'",
       r["fields"]["how_it_links_together"]["state"] != "RED",
       r["fields"]["how_it_links_together"])

    # ── R18.5 flows through: an untrusted LIVE state cannot yield an ORIENTED receipt ─
    import tempfile
    t = tempfile.mkdtemp()
    os.makedirs(os.path.join(t, "Dashboard", "state"))
    r3 = preflight("anything", t)
    ok("⚑ a tree with no Trusted Build receipt cannot produce an ORIENTED verdict - R12.5 and "
       "R18.5 are the same refusal seen from the conversation and from the run",
       r3["verdict"] == UNVERIFIED and "trusted_baseline" in r3["unmet_mandatory_fields"], r3)

    # ── R9.7 — routing, and the rule that no model name is ever hard-coded ──────────
    m = model_route(capital_consequence=True, architectural_breadth=False, novelty=False,
                    uncertainty=True)
    ok("R9.7: capital consequence routes to CAPITAL_METHODOLOGY at high thinking",
       m["reasoning_class"] == "CAPITAL_METHODOLOGY" and m["required_thinking_level"] == "high")
    ok("⚑ NO MODEL NAME IS EVER RETURNED: the route resolves at execution against the current "
       "plan, because a model written into the framework is a stale constant governing capital "
       "work the day the plan changes (R9.7)",
       m["model"] == "RESOLVE_AT_EXECUTION"
       and not re.search(r"opus|sonnet|haiku|gpt|claude-\d", json.dumps(m), re.I), m["model"])
    ok("a locked deterministic contract routes MECHANICAL - so the class above means "
       "'this one is expensive', not 'everything is expensive'",
       model_route(capital_consequence=False, architectural_breadth=False, novelty=False,
                   uncertainty=False, mechanical_only=True)["reasoning_class"] == "MECHANICAL")
    ok("⚑ R9.8: every route carries the escalation triggers that stop cheap execution dead - "
       "unexpected architecture, a capital threshold decision, a source-of-truth conflict, "
       "stale scope, a contradicted spec, a failed acceptance test, an unestablished fact",
       len(m["escalation_triggers"]) == len(ESCALATION_TRIGGERS) and m["escalation_triggers"])

    # ══ ISA-0666 (Raj 19-Sep-2026, Wave 2 BuildSpec §5.1) ══════════════════════════
    ok("ISA-0666: the three establishment states are distinct and the mapping has ONE home",
       (establishment({"state": "GREEN"}) == ESTABLISHED_OK
        and establishment({"state": "PARTIAL"}) == ESTABLISHED_OK
        and establishment({"state": "RED"}) == ESTABLISHED_ADVERSE
        and establishment({"state": UNKNOWN}) == UNKNOWN_UNVERIFIED
        and establishment({"state": "ENVIRONMENT_UNKNOWN"}) == UNKNOWN_UNVERIFIED
        and establishment({}) == UNKNOWN_UNVERIFIED))
    ok("⚑ ISA-0666: 'I could not establish this' and 'I established it and it is bad' no longer "
       "render the same - the receipt publishes an establishment state per mandatory field",
       set((r or {}).get("establishment") or {}) == set(MANDATORY_FIELDS)
       and all(v in (ESTABLISHED_OK, ESTABLISHED_ADVERSE, UNKNOWN_UNVERIFIED)
               for v in r["establishment"].values()), r.get("establishment"))

    _adv = {"state": "RED", "value": {"not_live_owners": [
        {"name": "cap_a", "owner": "ISA-9101"}, {"name": "cap_b", "owner": "ISA-9102"}]}}
    ok("ISA-0666 MUST-FIRE: an adverse capability field is attributed to the OWNERS the "
       "registry records, not to prose in a BuildSpec",
       adverse_owners("what_executes_and_consumes", _adv)["owned"] == ["ISA-9101", "ISA-9102"])
    _unowned = {"state": "RED", "value": {"not_live_owners": [{"name": "cap_c", "owner": None}]}}
    ok("⚑ ISA-0666 NEGATIVE CONTROL: a finding NOTHING in the register owns can never be "
       "inside an authorised scope - if no item owns it, no BuildSpec is remediating it",
       adverse_owners("what_executes_and_consumes", _unowned)["unowned"]
       and not adverse_owners("what_executes_and_consumes", _unowned)["owned"])
    ok("⚑ ISA-0666 NEGATIVE CONTROL: trusted_baseline is UNSCOPEABLE - R18.5 is reconciled, "
       "never scoped away, whatever item ids the BuildSpec names",
       adverse_owners("trusted_baseline", {"state": "RED", "value": {}})["unowned"]
       and not adverse_owners("trusted_baseline", {"state": "RED", "value": {}})["owned"])
    ok("ISA-0666 NEGATIVE CONTROL: an adverse field with no attribution rule of its own is "
       "unowned, so a new RED cannot silently inherit somebody else's allowance",
       adverse_owners("run_surfaces_affected", {"state": "RED", "value": {}})["unowned"])

    # the verdict rules themselves, on a synthetic receipt-shaped input
    _mk = lambda est: {"establishment": est}                            # noqa: E731
    _t = tempfile.mkdtemp()
    os.makedirs(os.path.join(_t, "Dashboard", "state"), exist_ok=True)
    _empty = preflight("anything", _t, authorised_scope_items=["ISA-9999"])
    ok("⚑ ISA-0666 MUST-FIRE: an UNKNOWN_UNVERIFIED mandatory field blocks even when the "
       "BuildSpec names a scope - unknown is not adverse and is never in scope (R4.3)",
       _empty["verdict"] == UNVERIFIED
       and set(_empty["unknown_mandatory_fields"]) & set(_empty["unmet_mandatory_fields"]),
       _empty["unknown_mandatory_fields"])

    # scoped-allowance policing — the anti-grandfathering half
    _rc = {"verdict": ORIENTED, "adverse_inside_authorised_scope": ["what_executes_and_consumes"],
           "authorised_scope_items": ["ISA-9101"], "source_roll": _source_roll(HERE)}
    ok("ISA-0666 POSITIVE CONTROL: a fresh receipt scoped to an item this build claims is a "
       "valid allowance",
       scoped_receipt_valid(_rc, build_items=["ISA-9101", "ISA-9102"], root=HERE)["valid"],
       scoped_receipt_valid(_rc, build_items=["ISA-9101"], root=HERE))
    ok("⚑ ISA-0666 MUST-FIRE: an allowance scoped to an item the build does NOT claim is "
       "refused - that is exactly the permanent grandfathering §5.1 forbids",
       not scoped_receipt_valid(_rc, build_items=["ISA-9102"], root=HERE)["valid"])
    ok("⚑ ISA-0666 MUST-FIRE: a receipt written against DIFFERENT source is refused - a "
       "receipt that outlives the code it read is not evidence about this build (R5.9)",
       not scoped_receipt_valid(dict(_rc, source_roll="deadbeef" * 8),
                                build_items=["ISA-9101"], root=HERE)["valid"])
    ok("ISA-0666 NEGATIVE CONTROL: an UNVERIFIED receipt cannot license an allowance",
       not scoped_receipt_valid(dict(_rc, verdict=UNVERIFIED),
                                build_items=["ISA-9101"], root=HERE)["valid"])
    ok("ISA-0666: a receipt that scoped NOTHING needs no allowance and is trivially valid - "
       "the control must not block ordinary clean builds",
       scoped_receipt_valid({"verdict": ORIENTED}, build_items=[], root=HERE)["valid"])
    ok("⚑ ISA-0666: NO allowance is persisted - the receipt records that owners are re-read "
       "and a post-build preflight is required, so nothing can be grandfathered",
       "NONE" in r["scope_allowance"]["grandfathering"]
       and r["scope_allowance"]["expires_with"] == r["authorised_scope_items"])

    ok("summarise() renders the verdict and the unmet fields for a reply",
       r3["id"] in summarise(r3) and "R12.5" in summarise(r3))

    if verbose:
        print("\ndiscussion_preflight selftest: %d assertion(s), %d FAIL(s)%s"
              % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    subject = "framework"
    scope_items, scope_authority = [], None
    for i, a in enumerate(argv):
        if a == "--subject" and i + 1 < len(argv):
            subject = argv[i + 1]
        # ISA-0666: the authorised remediation scope is passed as EXPLICIT item ids and the
        # document that authorises them. An adverse mandatory field is tolerated only where
        # the register says one of these items owns it.
        if a == "--scope-items" and i + 1 < len(argv):
            scope_items = [x for x in re.split(r"[,\s]+", argv[i + 1]) if x]
        if a == "--scope-authority" and i + 1 < len(argv):
            scope_authority = argv[i + 1]
    r = preflight(subject, write="--write" in argv, authorised_scope_items=scope_items,
                  scope_authority=scope_authority)
    if "--json" in argv:
        print(json.dumps(r, indent=2))
    else:
        print(summarise(r))
    return 0 if r.get("verdict") == ORIENTED else 1


if __name__ == "__main__":
    sys.exit(main())
