#!/usr/bin/env python3
"""
isa_register_metrics.py — §10's KRIs, computed instead of estimated (ISA-0467, P0.7).

⚑⚑ WHY (ISA-0467, CRITICAL). §10 declares thirteen KRIs and nine warning lights, names this
file as the thing that computes them, and the file did not exist. The standard's own enforcement
layer was unbuilt AND unmonitored — a standard nobody can prove ran is FC-E, which is the
failure class the standard itself names first.

⚑⚑ THE PATH IN §10 IS WRONG AND IS CORRECTED HERE, NOT SILENTLY. §10 declares the store at
`registry/isa_items.jsonl` and the output at `registry/metrics_[yyyy_mm].json`. **`registry/`
does not exist and never has.** The register lives at `Dashboard/state/isa_items.jsonl`, and
this module writes beside it. The spec required that correction to be a register item rather
than a quiet re-point (ISA-0522) — a path fixed in code and left wrong in the standard is the
two-homes defect the standard exists to prevent.

⚑ MINIMUM VIABLE SET for 05-Sep (§P0.7): K6, K7, K12, KR3, KR6, plus §17's ratio from
`rule_audit`. The rest are declared UNIMPLEMENTED **by name** rather than omitted — an absent
metric and a metric reading zero are the same output and different facts (R2.10).

⚑ NOTHING HERE RETURNS A BARE NUMBER. Every metric is {value, basis, n, d} or
{value: None, unmeasured_reason: ...}. Rule 1 of the engineering standard: make "missing"
impossible to represent as a number.
"""
from __future__ import annotations

import datetime
import inspect
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STORE_DIR = os.path.join(HERE, "Dashboard", "state")
STORE = os.path.join(STORE_DIR, "isa_items.jsonl")

# ⚑ §10 declares this path as `registry/`. That directory does not exist. Corrected here and
#   recorded as ISA-0522 — see the module docstring.
DECLARED_IN_STANDARD = "registry/metrics_[yyyy_mm].json"
ACTUAL_OUTPUT_DIR = STORE_DIR

# "Found by the framework, not by a person" (K6). A detector is the framework when it runs
# WITHOUT a human deciding to look. CLAUDE_BUILD and CLAUDE_REVIEW are people looking, and
# counting them would be the count-gaming §10's own heading warns about.
FRAMEWORK_DETECTORS = ("AUTOMATED_BATTERY", "INVARIANT", "CONTRACT_ASSERT", "PARITY_CHECK",
                       "ATLAS")
PERSON_DETECTORS = ("RAJ", "CLAUDE_BUILD", "CLAUDE_REVIEW", "LIVE_RUN_SURPRISE",
                    "EXTERNAL_SOURCE")

UNIMPLEMENTED = {
    "K1": "fix points ÷ total points shipped — needs `points` populated; 0 items carry it",
    "K2": "needs the D-21 surprise register's VERIFIED transitions, not yet a field",
    "K3": "needs a 'materially wrong' flag on superseded entries",
    "K4": "computable (CORRECTION + detected_by RAJ + factual_error) but per-SESSION, and the "
          "store carries no session id",
    "K5": "needs HYPOTHESIS/TESTED/VERIFIED on causal claims — field does not exist",
    "K8": "needs `escaped` + `escape_surface` populated; sparsely present",
    "K9": "computable from failure_class once 'declared defence' is a field",
    "K10": "needs the follow-on link between a fix and the defect it caused",
    "K11": "computable from standard_refs + verification.test_id — deferred to the next build",
    "K13": "computable from state + criticality + created_on — deferred to the next build",
    "KR1": "needs the module→artefact boundary map (framework_atlas), not yet keyed that way",
    "KR2": "screener_local duplication — a standing known value, not derivable from the store",
    "KR4": "register_callsites.py holds this; not yet surfaced through here",
    "KR5": "tests_jul2026/run_tests.kr5_report() holds this (ISA-0514/ISA-0521)",
    "KR7": "calibration_stamp_history.json holds this; not yet surfaced through here",
    "KR8": "computable from state=BLOCKED_ON_RAJ + age — deferred to the next build",
    "KR9": "⚑ BLOCKED BY R8.2 BEING UNENFORCED (ISA-0523): learning tasks carry a task_id and "
           "no liveness assertion, so 'tasks that cannot prove they ran' is currently ALL of "
           "them and the metric would be true and useless until R8.2 is instrumented",
}


def _missing(reason):
    return {"value": None, "unmeasured_reason": reason}


# ⚑ Within-process memo for the three EXPENSIVE readings. KR3 calls every consistency_check
#   pair (7.8s), KR6 walks the tree by AST (17.2s), and the §17 audit parses ~55 files. The
#   selftest and the battery each ask for metrics() more than once; without this a single
#   suite run took over two minutes. Keyed on the arguments that change the answer, and
#   cleared by _reset_cache() so a test can force a fresh reading.
_CACHE = {}


def _reset_cache():
    _CACHE.clear()


def load_items(path: str | None = None) -> list:
    p = path or STORE
    if not os.path.exists(p):
        raise FileNotFoundError(
            "register store not found at %s. §10 declares `registry/isa_items.jsonl`, which "
            "does not exist — see ISA-0522." % p)
    out = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _window(items, since=None, until=None, ids=None):
    if ids is not None:
        want = set(ids)
        return [i for i in items if i.get("id") in want]
    sel = items
    if since:
        sel = [i for i in sel if (i.get("created_on") or "") >= since]
    if until:
        sel = [i for i in sel if (i.get("created_on") or "") <= until]
    return sel


# ── K6 — found by the framework, not by a person ──────────────────────────────────────────
def k6(items, since=None, until=None, ids=None) -> dict:
    sel = _window(items, since, until, ids)
    if not sel:
        return _missing("no items in the window — a ratio over an empty population is not 0%")
    found = [i["id"] for i in sel if i.get("detected_by") in FRAMEWORK_DETECTORS]
    unattributed = [i["id"] for i in sel if not i.get("detected_by")]
    return {"value": round(100.0 * len(found) / len(sel), 1),
            "n": len(found), "d": len(sel),
            "basis": "detected_by in %s ÷ all new items in the window" % (FRAMEWORK_DETECTORS,),
            "found_by_framework": found,
            "unattributed": unattributed,
            "unattributed_note": ("items with no detected_by count in the DENOMINATOR, never "
                                  "the numerator — an unattributed find is not a framework find")}


# ── K7 — days a fault ran undetected ──────────────────────────────────────────────────────
def k7(items, since=None, until=None, ids=None) -> dict:
    sel = _window(items, since, until, ids)
    lat = [i["latency_days"] for i in sel
           if isinstance(i.get("latency_days"), int) and i["latency_days"] >= 0]
    if not lat:
        return _missing("no item in the window carries latency_days")
    return {"value": float(statistics.median(lat)),
            "n": len(lat), "d": len(sel),
            "basis": "median latency_days (introduced_on → detected_on)",
            "coverage_pct": round(100.0 * len(lat) / len(sel), 1),
            "coverage_note": ("the median is over the %d of %d items that carry the field; a "
                              "median computed on 7%% of the population is a reading about "
                              "those items, not about the framework" % (len(lat), len(sel))),
            "min": min(lat), "max": max(lat)}


# ── K12 — "done" proven to actually run ───────────────────────────────────────────────────
def k12(items, since=None, until=None, ids=None) -> dict:
    sel = [i for i in _window(items, since, until, ids)
           if (i.get("state") or "").startswith("CLOSED_FIXED")]
    if not sel:
        return _missing("no CLOSED_FIXED items in the window")
    green, ref_only, none_at_all = [], [], []
    for i in sel:
        v = i.get("verification") or {}
        if v.get("liveness_ref") and v.get("green_on"):
            green.append(i["id"])
        elif v.get("liveness_ref"):
            ref_only.append(i["id"])
        else:
            none_at_all.append(i["id"])
    return {"value": round(100.0 * len(green) / len(sel), 1),
            "n": len(green), "d": len(sel),
            "basis": ("CLOSED_FIXED items whose verification carries BOTH a liveness_ref and a "
                      "green_on date ÷ all CLOSED_FIXED. A liveness_ref with no green_on names "
                      "a test; it does not say the test passed"),
            "ref_without_green_on": ref_only,
            "no_verification_at_all": none_at_all}


# ── KR3 — controls able to return PASS on a null input ────────────────────────────────────
def kr3(module=None) -> dict:
    """MEASURED, not declared: every `pair_*` in consistency_check is called with EMPTY inputs
    and asked whether it returns [] — a clean pass on nothing.

    ⚑ Empty, not None. Most pairs treat `None` as *"read the live tree"*, which is their
    documented default and not a null input at all; feeding None would measure the default
    path and report it as null-tolerance. The distinction is the whole measurement.

    ⚑ Raising is a PASS for the control (it refused). Returning [] is the KR3 violation."""
    if module is None and "kr3" in _CACHE:
        return _CACHE["kr3"]
    if module is None:
        try:
            import consistency_check as module                       # noqa: N813
        except Exception as e:                                       # noqa: BLE001
            return _missing("consistency_check not importable (%s) — KR3 is BLIND" % e)
    pairs = [(n, f) for n, f in vars(module).items()
             if n.startswith("pair_") and callable(f)]
    if not pairs:
        return _missing("no pair_* functions found in consistency_check — KR3 is BLIND, and a "
                        "BLIND scan must not report 0 violations")
    tolerant, refused, no_args = [], [], []
    for name, fn in pairs:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        params = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
        if not params:
            no_args.append(name)          # reads the live tree only; not a null-input surface
            continue
        empties = {}
        for p in params:
            n = p.name.lower()
            if n.endswith("texts") or n.endswith("surfaces") or n in ("module_texts", "rows"):
                empties[p.name] = {}
            elif n.endswith("items") or n.endswith("list"):
                empties[p.name] = []
            elif "text" in n or n.endswith("_src") or n.endswith("_doc"):
                empties[p.name] = ""
            else:
                empties[p.name] = {}
        try:
            out = fn(**empties)
        except Exception:                                            # noqa: BLE001
            refused.append(name)          # refusing on nothing is correct
            continue
        if isinstance(out, list) and not out:
            tolerant.append(name)         # ⚑ a clean PASS on nothing — the KR3 violation
        else:
            refused.append(name)
    checked = len(tolerant) + len(refused)
    _out = {"value": len(tolerant), "n": len(tolerant), "d": checked,
            "target": 0,
            "basis": ("consistency_check.pair_* called with EMPTY (not None) arguments; a "
                      "control returning [] on nothing is null-tolerant, one that raises or "
                      "reports errors has refused"),
            "null_tolerant": sorted(tolerant),
            "not_applicable_no_args": sorted(no_args),
            "note": ("%d pair(s) take no arguments and read the live tree only — they have no "
                     "null-input surface to test and are reported, not counted"
                     % len(no_args))}
    if module is None:
        _CACHE["kr3"] = _out
    return _out


# ── KR6 — rules living in more than one place ─────────────────────────────────────────────
def kr6() -> dict:
    """From P0.2's quantity register: a quantity with two computers is a rule with two homes."""
    if "kr6" in _CACHE:
        return _CACHE["kr6"]
    try:
        import framework_integrity as fi
    except Exception as e:                                           # noqa: BLE001
        return _missing("framework_integrity not importable (%s) — KR6 is BLIND" % e)
    try:
        rep = fi.q1_two_computers()
    except Exception as e:                                           # noqa: BLE001
        return _missing("q1_two_computers raised (%s) — KR6 is BLIND, not 0" % e)
    breaches = rep.get("breaches") or rep.get("violations") or []
    reg = fi.load_quantity_register()
    _out = {"value": len(breaches), "n": len(breaches), "d": len(reg), "target": 0,
            "basis": ("framework_integrity.q1_two_computers() over quantity_register.json — a "
                      "quantity with more than one computer is a rule with two homes (R4.4)"),
            "breaches": breaches,
            "coverage_note": ("measured over the %d quantities in the register. 202 "
                              "capital-gating constants are enumerable in the tree against 12 "
                              "registered thresholds (ISA-0495), so this is a reading about the "
                              "register's population, not about every rule in the framework"
                              % len(reg))}
    _CACHE["kr6"] = _out
    return _out


# ── §17 rule-audit ratio ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════════════
# KR10 – KR19 — the warning lights the adopted standard added (ISA-0623, 09-Sep-2026)
# ═══════════════════════════════════════════════════════════════════════════════════════
# ⚑ EACH ONE READS AN EXISTING INSTRUMENT RATHER THAN RE-DERIVING IT (R4.4, R4.5). KR10 asks
#   release_gate, KR12 asks capability_registry, KR13 asks release_gate.item_currency, KR17
#   asks release_gate.buildspec_gaps. A metric that re-implements the check it reports is the
#   second home this file exists to avoid, and it would drift the day the check changed.

def kr10() -> dict:
    """KR10 — unsigned/untrusted live changes. Must be 0."""
    try:
        import release_gate as rg
        v = rg.verify_live()
    except Exception as e:                                              # noqa: BLE001
        return _missing("release_gate unavailable (%s)" % e)
    n = 0 if v.get("state") == "TRUSTED" else len(v.get("diffs") or []) or 1
    return {"value": n, "trigger": "must be 0", "state": v.get("state"),
            "why": v.get("why"), "blocks_capital_run": v.get("blocks_capital_run")}


def kr11() -> dict:
    """KR11 — candidate parity unknown or failed."""
    try:
        import release_gate as rg
        fp = rg.live_fingerprints()
    except Exception as e:                                              # noqa: BLE001
        return _missing("release_gate unavailable (%s)" % e)
    unknown = {k: v.get("why") for k, v in fp.items()
               if v.get("state") in ("ENVIRONMENT_UNKNOWN", "UNKNOWN")}
    return {"value": len(unknown), "trigger": "any", "surfaces": unknown,
            "why": ("R5.12: a surface that cannot be fingerprinted is ENVIRONMENT_UNKNOWN and "
                    "blocks; it is never evidence about framework correctness")}


def kr12() -> dict:
    """KR12 — claimed capital decision states lacking the R4.14 chain. Must be 0."""
    try:
        import capability_registry as cr
        rec = cr.reconcile()
        if rec.get("state") == "DISABLED":
            return _missing(rec["why"])
        gaps = cr.must_fire_gaps()
    except Exception as e:                                              # noqa: BLE001
        return _missing("capability_registry unavailable (%s)" % e)
    return {"value": rec["n_not_live"], "trigger": "must be 0",
            "d": rec["n_capabilities"],
            "gbp_exposure_not_live": rec["gbp_exposure_not_live"],
            "blocked_at": rec["blocked_at"],
            "n_must_fire_gaps": len(gaps),
            "why": ("R4.14: LIVE requires produced -> executed -> consumed -> "
                    "decision-effective. Static reachability alone may never close FC-E "
                    "(R15.5)")}


def kr13() -> dict:
    """KR13 — actionable items not validated against the current Trusted Build. Must be 0."""
    try:
        import release_gate as rg
        prev = rg.load_receipt() or {}
        ic = rg.item_currency(build_id=prev.get("build_id"))
    except Exception as e:                                              # noqa: BLE001
        return _missing("release_gate unavailable (%s)" % e)
    if ic.get("state") == "ENVIRONMENT_UNKNOWN":
        return _missing(ic["why"])
    return {"value": ic["n_stale"], "d": ic["n_actionable"], "trigger": "must be 0",
            "current_build_id": ic.get("current_build_id"),
            "worst": [s["id"] for s in ic["stale"][:8]],
            "why": ("R7.8: an item not validated against the current Trusted Build is "
                    "REVALIDATION_REQUIRED, not BUILD_READY. ISA-0467 stood BUILD_READY for "
                    "twelve days while six builds shipped most of its corrective action")}


def kr14(items=None) -> dict:
    """KR14 — shipped items with an undispositioned run surface. Must be 0."""
    try:
        import release_gate as rg
        surfaces = rg.RUN_SURFACES
    except Exception as e:                                              # noqa: BLE001
        return _missing("release_gate unavailable (%s)" % e)
    items = items if items is not None else load_items()
    recent = [i for i in items
              if i.get("state") == "CLOSED_FIXED" and i.get("trusted_build_id")]
    bad = []
    for i in recent:
        d = i.get("run_surface_dispositions") or {}
        missing = [s for s in surfaces if s not in d]
        if missing:
            bad.append({"id": i["id"], "undispositioned": missing})
    return {"value": len(bad), "d": len(recent), "trigger": "must be 0", "worst": bad[:8],
            "why": ("R4.15/R14.5: each affected surface is UPDATE_REQUIRED or "
                    "VERIFIED_NO_CHANGE with evidence; omission is not a verdict")}


def kr15(items=None) -> dict:
    """KR15 — retained learning data with no feedback contract. Any is a trigger."""
    items = items if items is not None else load_items()
    learn = [i for i in items
             if (i.get("learning") or {}).get("learnable")
             and (i.get("learning") or {}).get("task_id")]
    open_loops = [i["id"] for i in learn if not i.get("learning_feedback_contract")]
    return {"value": len(open_loops), "d": len(learn), "trigger": "any",
            "worst": open_loops[:8],
            "why": ("R8.5: capture is not learning. A growing store with no analysis, no "
                    "consumer and no Raj-facing insight path is an open FC-E loop")}


def kr16(items=None) -> dict:
    """KR16 — material decision outputs that cannot reconstruct the decision trace."""
    items = items if items is not None else load_items()
    capital = [i for i in items
               if (i.get("capital_impact") or {}).get("has_impact")
               and i.get("state") == "CLOSED_FIXED"]
    opaque = [i["id"] for i in capital if not i.get("observability")]
    return {"value": len(opaque), "d": len(capital), "trigger": "any", "worst": opaque[:8],
            "why": ("R20.1: every material capital capability emits structured evidence "
                    "sufficient to reconstruct INPUT -> CALCULATION -> CONSTRAINT -> DECISION "
                    "-> CAPITAL CONSEQUENCE without reading Python")}


def kr17(items=None) -> dict:
    """KR17 — BuildSpec quality/currency failure. Must be 0."""
    items = items if items is not None else load_items()
    material = [i for i in items
               if i.get("size_est") in ("M", "L", "XL") and i.get("state") == "CLOSED_FIXED"
               and i.get("trusted_build_id")]
    nospec = [i["id"] for i in material if not i.get("buildspec_ref")]
    return {"value": len(nospec), "d": len(material), "trigger": "must be 0",
            "worst": nospec[:8],
            "why": ("R19.1: no material build starts from chat prose or an old corrective "
                    "action alone; it starts from a BuildSpec validated against the current "
                    "Trusted Build")}


def kr18(items=None) -> dict:
    """KR18 — items executed without a current model/reasoning route. Any is a trigger."""
    items = items if items is not None else load_items()
    worked = [i for i in items
              if i.get("state") in ("IN_PROGRESS", "CLOSED_FIXED")
              and i.get("trusted_build_id")]
    unrouted = [i["id"] for i in worked if not i.get("model_route")]
    return {"value": len(unrouted), "d": len(worked), "trigger": "any", "worst": unrouted[:8],
            "why": ("R9.7: route on reasoning complexity, uncertainty, architectural breadth, "
                    "novelty and capital consequence — not priority or size — and stamp the "
                    "resolution `as_of` rather than hard-coding a model")}


def kr19(items=None) -> dict:
    """KR19 — avoidable second-generation redesign. Any triggers a class review."""
    items = items if items is not None else load_items()
    fcm = [i["id"] for i in items if i.get("failure_class") == "FC-M"]
    return {"value": len(fcm), "trigger": "any; class review", "worst": fcm[:8],
            "why": ("FC-M: a change directionally better but never tested against the absolute "
                    "portfolio objective, risk budget or institutionally complete design. "
                    "R16.5: relative improvement does not establish absolute acceptability")}


def rule_audit_ratio() -> dict:
    if "s17" in _CACHE:
        return _CACHE["s17"]
    try:
        import rule_audit
        doc = rule_audit.audit()
    except Exception as e:                                           # noqa: BLE001
        return _missing("rule_audit unavailable (%s)" % e)
    _out = {"value": doc["traceable"]["pct_of_all_rules"],
            "claimed_asserted_pct": doc["claimed"]["ASSERTED"]["pct"],
            "claimed_partial_pct": doc["claimed"]["PARTIAL"]["pct"],
            "claimed_judgement_pct": doc["claimed"]["JUDGEMENT"]["pct"],
            "n": doc["traceable"]["n_traceable"], "d": doc["total_rules_classified"],
            "asserted_untraceable": doc["traceable"]["asserted_untraceable"],
            "basis": doc["traceable"]["basis"],
            "reading": doc["reading"]}
    _CACHE["s17"] = _out
    return _out


def metrics(month: str | None = None, items=None) -> dict:
    """month as 'yyyy_mm'; defaults to the current month."""
    today = datetime.date.today()
    month = month or "%04d_%02d" % (today.year, today.month)
    ck = ("metrics", month, items is None)
    if items is None and ck in _CACHE:
        return _CACHE[ck]
    items = items if items is not None else load_items()
    since = "%s-%s-01" % tuple(month.split("_"))
    doc = {
        "as_of": today.isoformat(),
        "month": month,
        "store": os.path.relpath(STORE, HERE),
        "path_correction": {
            "declared_in_standard_s10": DECLARED_IN_STANDARD,
            "actual": os.path.relpath(ACTUAL_OUTPUT_DIR, HERE),
            "item": "ISA-0522",
            "note": ("§10's `registry/` directory does not exist. Corrected in code AND raised "
                     "as a register item — a path fixed in one home and left wrong in the other "
                     "is the defect this file measures.")},
        "n_items_total": len(items),
        "K6_month": k6(items, since=since),
        "K6_all_time": k6(items),
        "K7_all_time": k7(items),
        "K12_all_time": k12(items),
        "KR3": kr3(),
        "KR6": kr6(),
        "S17_rule_audit": rule_audit_ratio(),
        # ── ISA-0623: the warning lights the adopted standard added ────────────────────
        "KR10": kr10(), "KR11": kr11(), "KR12": kr12(), "KR13": kr13(),
        "KR14": kr14(items), "KR15": kr15(items), "KR16": kr16(items),
        "KR17": kr17(items), "KR18": kr18(items), "KR19": kr19(items),
        "unimplemented": UNIMPLEMENTED,
        "unimplemented_note": ("named, never omitted. An absent metric and a metric reading "
                               "zero are the same output and different facts (R2.10)"),
    }
    if ck[2]:
        _CACHE[ck] = doc
    return doc


def write(month: str | None = None, out_dir: str | None = None) -> str:
    doc = metrics(month)
    d = out_dir or ACTUAL_OUTPUT_DIR
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "metrics_%s.json" % doc["month"])
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return p


# ── K-A1 — the metric must reproduce the KNOWN-BAD baseline before it is trusted ──────────
# §12 acceptance: "K6 on the 26/27-Aug session reproduces 0 of 12". The twelve findings of the
# spec's §1.1 are ISA-0454 … ISA-0465 (ISA-0466 onward are Raj's, the DECISION and the
# CORRECTIONs). Every one was found by a person reading code, not by a check.
KA1_POPULATION = ["ISA-%04d" % n for n in range(454, 466)]


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (("   " + str(detail)) if not cond else ""))
        if not cond:
            fails.append(name)

    items = load_items()

    # ── K-A1: reproduce the known-bad baseline ────────────────────────────────────────────
    ka1 = k6(items, ids=KA1_POPULATION)
    ok("K-A1: K6 over the twelve §1.1 findings (ISA-0454…0465) reproduces 0 of 12 — the metric "
       "reproduces the KNOWN-BAD baseline before it is trusted on a good one",
       ka1["n"] == 0 and ka1["d"] == 12 and ka1["value"] == 0.0, ka1)
    ok("⚑ ...and the population really is twelve items that exist, not twelve ids that do not — "
       "a ratio over an empty set would also read 0",
       len(_window(items, ids=KA1_POPULATION)) == 12,
       len(_window(items, ids=KA1_POPULATION)))
    ok("NEGATIVE CONTROL: K6 over a population that DOES contain framework finds is non-zero, "
       "so the 0 above means 'none', not 'the counter is broken'",
       (k6(items, since="2026-09-01")["n"] or 0) > 0, k6(items, since="2026-09-01"))
    ok("NEGATIVE CONTROL: K6 over an empty window refuses rather than returning 0%",
       k6(items, since="2099-01-01").get("value") is None)

    # ── K7 / K12 ──────────────────────────────────────────────────────────────────────────
    ok("K7 publishes its COVERAGE alongside the median — a median over a field 7% of items "
       "carry is a reading about those items", "coverage_pct" in k7(items))
    ok("K12 separates 'has a liveness_ref' from 'has a GREEN one' — naming a test is not the "
       "same as the test passing",
       "ref_without_green_on" in k12(items))
    ok("NEGATIVE CONTROL: K12 over a window with no CLOSED_FIXED items refuses",
       k12(items, since="2099-01-01").get("value") is None)

    # ── KR3 ───────────────────────────────────────────────────────────────────────────────
    r3 = kr3()
    ok("KR3 measures null-tolerance by CALLING the controls with empty inputs, not by reading "
       "a declaration", r3.get("d", 0) > 0, r3)

    class _Fake:
        @staticmethod
        def pair_always_passes(texts=None):
            return []                       # a control that passes on nothing

        @staticmethod
        def pair_refuses(texts=None):
            raise ValueError("refused on an empty corpus")

    fake = kr3(module=_Fake)
    ok("NEGATIVE CONTROL: a control that returns [] on an empty input is CAUGHT as null-tolerant",
       fake["null_tolerant"] == ["pair_always_passes"], fake)
    ok("NEGATIVE CONTROL: a control that RAISES on an empty input is NOT counted — refusing is "
       "the correct behaviour and must not be scored as a defect",
       "pair_refuses" not in fake["null_tolerant"])

    class _Empty:
        pass
    ok("⚑ a module with no pair_* functions reports BLIND, not zero violations",
       kr3(module=_Empty).get("value") is None)

    # ── KR6 and §17 ───────────────────────────────────────────────────────────────────────
    ok("KR6 reads P0.2's quantity register and publishes the register's SIZE with it, so the "
       "0 is bounded by what is registered", "coverage_note" in kr6())
    ok("§17's ratio is computed and both the CLAIMED and the TRACEABLE figures are published",
       rule_audit_ratio().get("value") is not None)

    # ── the unimplemented metrics are NAMED ───────────────────────────────────────────────
    declared = set(UNIMPLEMENTED)
    computed = {"K6", "K7", "K12", "KR3", "KR6"}
    all_k = {"K%d" % n for n in range(1, 14)} | {"KR%d" % n for n in range(1, 10)}
    ok("every §10 metric is either COMPUTED or NAMED as unimplemented — none is silently "
       "omitted (R2.10)", declared | computed == all_k, sorted(all_k - (declared | computed)))
    ok("...and no metric is in both lists", not (declared & computed), sorted(declared & computed))

    # ── the path correction is published, not silent ──────────────────────────────────────
    doc = metrics()
    ok("the output path correction to §10 is PUBLISHED in the artefact with its item id, not "
       "applied silently",
       doc["path_correction"]["item"] == "ISA-0522"
       and doc["path_correction"]["declared_in_standard_s10"] == DECLARED_IN_STANDARD)
    ok("⚑ and §10's declared `registry/` directory really is absent — the correction is a "
       "measurement, not an assumption",
       not os.path.isdir(os.path.join(HERE, "registry")))

    print("\nisa_register_metrics._selftest: %d assertion(s) failed" % len(fails))
    return len(fails)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    if "--write" in argv:
        print("wrote " + write())
        return 0
    print(json.dumps(metrics(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
