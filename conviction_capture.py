#!/usr/bin/env python3
"""
conviction_capture.py — Capture Layer Item 3 / Dashboard Spec §7.6.2. 02-Aug-2026.

THE GAP THIS CLOSES
-------------------
Step 8 is durable (`action_stack_*.json`). Step 10's outcome is durable (email, ledger, trades
log). **Step 9 is not durable at all.** Dimensions 8/9/10 — macro resilience, portfolio fit,
execution practicality — conviction totals, classifications, T2 entry reviews, T3 thesis-health
calls and VCI tier assessments are session judgements written ONLY as prose in the email. The
reasoning behind the most consequential monthly decision is unreconstructable.

WHAT THIS IS
------------
The schema is specified verbatim in dashboard spec §7.6.2 and is implemented as written. This
module does three things and deliberately no more:

  prefill()   builds the skeleton from what the pre-run ALREADY computed (tier, route,
              sector_type, D1-D7 total, VCI hurdle slots), so the review session fills in
              judgement and rationale rather than re-typing machine output. Per §7.6.2 this
              "adds reasoning discipline, not work".
  validate()  refuses the incomplete cases the spec names: a null dimension rationale FAILS,
              and `sector_type_source: "session_override"` without a reason FAILS.
  write()     emits step9_conviction_[mmm_yyyy].json before the email sends.

`not_progressed[]` IS NOT OPTIONAL. The spec states plainly that this is where
missed-opportunity evidence comes from, and it is the input Book B needs. A name scoring below
45 is recorded WITH ITS REASON, never dropped — the whole point of MOA is that false negatives
are invisible unless someone writes them down at the time.

This module records judgement. It never forms one: no score here feeds a gate, a weight or a
ranking (build hazard H7).

CLI:
  python3 conviction_capture.py --prefill --month aug_2026
  python3 conviction_capture.py --validate step9_conviction_aug_2026.json
  python3 conviction_capture.py --selftest

Stdlib only.
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1

TIERS = {"T1", "T2", "T3", "T1-A", "T2-A", "T3-A"}
ROUTES = {"main", "vci"}
CLASSIFICATIONS = {"High", "Medium", "Watch", "No Action"}
THESIS_DIRECTIONS = {"Strengthening", "Unchanged", "Weakening", "Degrading"}
JUDGEMENT_DIMS = ("d8_macro_resilience", "d9_portfolio_fit", "d10_execution_practicality")
VCI_HURDLE_KEYS = ("bottleneck_intact", "fv_asymmetry", "floor", "nvidia_signals",
                   "catalyst_within_18m", "sizing_ok", "nvidia_class_exception")

# Spec §7.6.2: "names scoring < 45 are recorded with their reason, not dropped".
NOT_PROGRESSED_SCORE_FLOOR = 45


# ── what each tier is ACTUALLY asked for (Run_Context Step 9B / 9C) ──────────────────
# 02-Aug-2026. The original validate() demanded D8+D9+D10, a conviction_total and a
# classification for EVERY name. That contract is unsatisfiable, and this is the real
# reason step9_conviction_aug_2026.json sat with 53 null convictions: write() could never
# succeed, so nothing was ever written back.
#
#   T1  — `strategic_conviction_score` (D1-D7) exists. Step 9B asks for all three
#         judgement dimensions. conviction_total = D1-7 + D8 + D9 + D10, out of 100.
#   T2  — NO D1-7 base. Instead `t2_score` carries four computed sub-scores plus
#         `portfolio_fit: {"score": null, "basis": "[Step 9B]"}` — the slot D9 fills.
#         Step 9C asks for Portfolio Fit ONLY. Total is the 5 sub-scores, out of 50.
#   T3  — below the progression bar; no judgement is requested at all.
# VCI routes answer the hurdle questions instead, at every tier.
#
# Scoring a T2 name on the T1 scale, or demanding D8/D10 for a T3, does not make the
# record more complete — it makes it wrong.
TIER_SCOPE = {
    "T1":   {"dims": ("d8_macro_resilience", "d9_portfolio_fit",
                      "d10_execution_practicality"), "basis": "d1_7_plus_judgements", "max": 100},
    "T1-A": {"dims": ("d8_macro_resilience", "d9_portfolio_fit",
                      "d10_execution_practicality"), "basis": "d1_7_plus_judgements", "max": 100},
    "T2":   {"dims": ("d9_portfolio_fit",), "basis": "t2_five_dimension", "max": 50},
    "T2-A": {"dims": ("d9_portfolio_fit",), "basis": "t2_five_dimension", "max": 50},
    "T3":   {"dims": (), "basis": None, "max": None},
    "T3-A": {"dims": (), "basis": None, "max": None},
}

# Run_Context Step 9B bands, on the /100 scale.
BANDS = ((75, "High"), (60, "Medium"), (45, "Watch"))


def required_dims(tier):
    return TIER_SCOPE.get(_norm_tier(tier), TIER_SCOPE["T3"])["dims"]


def classify(normalised_100):
    """Classify on the /100 scale ONLY. A T2 total is out of 50 and is normalised before
    it reaches here, so a T2 and a T1 conviction are never compared on different rulers."""
    if normalised_100 is None:
        return None
    for cut, label in BANDS:
        if normalised_100 >= cut:
            return label
    return "No Action"


def compute_total(name):
    """Return (total, scale_max, normalised_100, basis) for one name, or (None,)*4 when the
    tier is not scored. Never invents a number the tier cannot support."""
    tier = _norm_tier(name.get("tier"))
    scope = TIER_SCOPE.get(tier, TIER_SCOPE["T3"])
    if scope["basis"] is None:
        return None, None, None, None
    dims = name.get("dimensions") or {}
    if scope["basis"] == "d1_7_plus_judgements":
        base = dims.get("d1_7_prerun_total")
        if base is None:
            return None, None, None, None
        parts = [base]
        for dk in scope["dims"]:
            v = (dims.get(dk) or {}).get("score")
            if v is None:
                return None, None, None, None
            parts.append(v)
        total = round(sum(float(x) for x in parts), 1)
    else:                                   # t2_five_dimension
        t2 = name.get("t2_score") or {}
        subs = []
        for k in ("valuation", "growth_durability", "moat", "risk_reward"):
            v = (t2.get(k) or {}).get("score")
            if v is None:
                return None, None, None, None
            subs.append(float(v))
        pf = (dims.get("d9_portfolio_fit") or {}).get("score")
        if pf is None:
            return None, None, None, None
        subs.append(float(pf))
        total = round(sum(subs), 1)
    norm = round(total / scope["max"] * 100.0, 1)
    return total, scope["max"], norm, scope["basis"]


def apply_judgements(doc, judgements, compliance_mod=None):
    """Fill the session's judgements and derive everything derivable.

    `judgements` = {ticker: {"d8": (score, rationale), "d9": (...), "d10": (...),
                             "d10_capital": x, "d10_compliance": y,
                             "thesis_direction": "...", "vci_hurdle": {...}}}

    D10 is ALWAYS routed through compliance.score_d10() — scoring it by hand hands every
    candidate the compliance points the paused regime is meant to withhold, which silently
    lowers the >=60 and >=75 bars for everyone.
    """
    if compliance_mod is None:
        try:
            import compliance as compliance_mod
        except Exception:
            compliance_mod = None
    applied, skipped = [], []
    for n in doc.get("names") or []:
        j = judgements.get(n.get("ticker"))
        if not j:
            skipped.append(n.get("ticker"))
            continue
        dims = n.setdefault("dimensions", {})
        need = required_dims(n.get("tier"))
        for key, dk in (("d8", "d8_macro_resilience"), ("d9", "d9_portfolio_fit"),
                        ("d10", "d10_execution_practicality")):
            if dk not in need:
                continue
            if key == "d10" and "d10_capital" in j:
                if compliance_mod is None:
                    raise RuntimeError("compliance module unavailable — refusing to score D10 "
                                       "by hand (Run_Context: MANDATORY compliance.score_d10)")
                score = compliance_mod.score_d10(j["d10_capital"], j.get("d10_compliance"))
                rationale = j.get("d10_rationale") or (j.get(key) or (None, ""))[1]
            else:
                val = j.get(key)
                if val is None:
                    continue
                score, rationale = val
            dims.setdefault(dk, {})
            dims[dk]["score"] = score
            dims[dk]["rationale"] = rationale
        if j.get("thesis_direction"):
            n["thesis_direction"] = j["thesis_direction"]
        if j.get("vci_hurdle"):
            n.setdefault("vci_hurdle", {}).update(j["vci_hurdle"])
        for o in (j.get("overrides") or []):
            n.setdefault("overrides", []).append(o)
        total, scale, norm, basis = compute_total(n)
        n["conviction_total"] = total
        n["conviction_scale_max"] = scale
        n["conviction_normalised_100"] = norm
        n["conviction_basis"] = basis
        n["classification"] = classify(norm)
        applied.append(n.get("ticker"))
    return {"applied": applied, "not_supplied": skipped}


def gate(doc):
    """Hard gate for the run: returns [] when the month may send, else the blocking errors.
    Wire this immediately before the email build so a month cannot be reported with its
    judgements unrecorded — the failure §7.6.2 exists to close."""
    return validate(doc, strict_judgement=True)


# ── prefill ──────────────────────────────────────────────────────────────────────────────

def _entries(step9_pre):
    """Every scored name the pre-run produced, with its route, flattened."""
    out = []
    for block, route in (("main_watchlist", "main"), ("candidate_pool", "main"),
                         ("vci_watchlist", "vci")):
        node = step9_pre.get(block) or {}
        for tier, lst in node.items():
            for e in (lst or []):
                if isinstance(e, dict) and e.get("ticker"):
                    out.append((tier, route, block, e))
    return out


# Fields that reveal the Source Score ranking. Measured 02-Aug-2026: the MECHANICAL D1-D7 base
# correlates 0.033 with the Source Score, but the JUDGED 10-dimension total correlates 0.505 —
# the session dimensions re-import the ranking the mechanical ones ignore, so the conviction
# floor gets cleared by judgement rather than by measurement. Blinding removes the anchor.
# `tier` is deliberately NOT blinded: it determines which dimensions are in scope (Step 9B/9C),
# so hiding it would make the record unscoreable. That residual leak is stated, not hidden.
RANKING_REVEALS = ("source_score", "vci_source_score", "normalised_score", "rank")


def prefill(month_label, here=None, step9_pre=None, action_stack=None, regime=None,
            blind=False):
    """Build the conviction skeleton from pre-run output.

    Every machine-computed field is filled. Every JUDGEMENT field is left explicitly null with
    an empty rationale, so validate() will refuse the document until the review session has
    actually made and recorded the call. A prefill that quietly defaulted D8/D9/D10 to a
    plausible number would be worse than no capture at all — it would look like reasoning.
    """
    here = here or HERE
    if step9_pre is None:
        p = os.path.join(here, f"step9_pre_{month_label}.json")
        if not os.path.exists(p):
            p = os.path.join(here, "archive", "decision_capture", f"step9_pre_{month_label}.json")
        with open(p, encoding="utf-8") as f:
            step9_pre = json.load(f)
    if action_stack is None:
        p = os.path.join(here, f"action_stack_{month_label}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                action_stack = json.load(f)
        else:
            action_stack = {}

    stack = action_stack.get("stack", action_stack if isinstance(action_stack, list) else []) or []
    stack_by_ticker = {r.get("ticker"): r for r in stack if isinstance(r, dict)}

    names, not_progressed = [], []
    seen = set()
    for tier, route, block, e in _entries(step9_pre):
        tk = e.get("ticker")
        if tk in seen:
            continue
        seen.add(tk)
        src = e.get("source_score")
        vci_src = e.get("vci_source_score")
        score = src if src is not None else vci_src
        d1_7 = e.get("strategic_conviction_score")

        # Below the floor: recorded WITH ITS REASON in not_progressed, never dropped.
        if score is not None and score < NOT_PROGRESSED_SCORE_FLOOR:
            not_progressed.append({
                "ticker": tk,
                "reason": (f"score {score} below the {NOT_PROGRESSED_SCORE_FLOOR} progression "
                           f"floor (tier {tier}, {block})"),
            })
            continue

        names.append({
            "ticker": tk,
            "tier": _norm_tier(tier),
            "route": route,
            "sector_type": e.get("sector_type"),
            # §7.6.2 defines exactly two values: it answers "did the SESSION change this?".
            # step9_pre carries a finer provenance of its own ("inferred", "mapped", ...),
            # which is preserved under _prefill rather than smuggled into a field whose
            # vocabulary means something else.
            "sector_type_source": ("session_override"
                                   if e.get("sector_type_source") == "session_override"
                                   else "step9_pre"),
            # T2 names carry no D1-D7 base; their total is built from the four computed
            # t2_score sub-scores plus the portfolio_fit slot D9 fills. Carry it through or
            # compute_total() cannot score a T2 name at all.
            "t2_score": e.get("t2_score"),
            "dimensions": {
                "d1_7_prerun_total": d1_7,
                # LEFT NULL ON PURPOSE — these are the session judgements the whole file exists
                # to capture. validate() fails while any rationale is empty.
                "d8_macro_resilience": {"score": None, "rationale": ""},
                "d9_portfolio_fit": {"score": None, "rationale": ""},
                "d10_execution_practicality": {"score": None, "rationale": ""},
            },
            "conviction_total": None,
            "classification": None,
            "action": (stack_by_ticker.get(tk) or {}).get("action"),
            "thesis_direction": None,
            "vci_hurdle": {k: (False if k == "nvidia_class_exception" else None)
                           for k in VCI_HURDLE_KEYS},
            "overrides": [],
            # --- provenance, so a later reader can tell machine input from human judgement ---
            "_prefill": {
                "source_score": src, "vci_source_score": vci_src,
                "normalised_score": e.get("normalised_score"),
                "t1_qualified": e.get("t1_qualified"),
                "est_rev_direction": (e.get("t1_gate_detail", {}) or {})
                                     .get("evidence", {}).get("basis", {})
                                     .get("rev_30d_direction"),
                "block": block, "rank": e.get("rank") or e.get("deployment_rank"),
                "sector_type_source_prerun": e.get("sector_type_source"),
                "tier_raw": tier,
            },
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "month": _month_iso(month_label),
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "regime": regime,
        "names": names,
        "not_progressed": not_progressed,
    }


def seal(doc, blind=True):
    """Move (or restore) the ranking signals so D8/D9/D10 can be scored without seeing them.

    Sealing is REVERSIBLE and lossless: values move to `_prefill_sealed`, they are never
    deleted. `unseal()` puts them back once the judgements are recorded, so the audit trail is
    complete and the dashboard still gets its numbers.
    """
    for n in doc.get("names") or []:
        pre = n.get("_prefill")
        if not isinstance(pre, dict):
            continue
        if blind:
            sealed = n.setdefault("_prefill_sealed", {})
            for k in RANKING_REVEALS:
                if k in pre:
                    sealed[k] = pre.pop(k)
            pre["_blinded"] = sorted(sealed.keys())
        else:
            sealed = n.pop("_prefill_sealed", {}) or {}
            pre.update(sealed)
            pre.pop("_blinded", None)
    doc["judgement_blinding"] = ("SEALED — D8/D9/D10 are to be scored without the Source Score, "
                                 "normalised score or rank visible. tier remains visible because "
                                 "it determines dimension scope." if blind else
                                 "unsealed — ranking signals restored after judgement")
    return doc


def _norm_tier(tier):
    """step9_pre writes VCI tiers as T1_A/T2_A/T3_A; §7.6.2's vocabulary is T1-A/T2-A/T3-A.
    Normalise at the boundary rather than widening the schema — the schema is the contract the
    dashboard reads, and two spellings of one tier is how join keys quietly stop matching."""
    t = str(tier).strip().replace("_", "-").upper()
    return t if t in TIERS else str(tier)


def _month_iso(month_label):
    """'aug_2026' -> '2026-08'."""
    months = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
    try:
        mmm, yyyy = month_label.lower().split("_")
        return f"{int(yyyy):04d}-{months[mmm]:02d}"
    except Exception:
        return month_label


# ── validation ───────────────────────────────────────────────────────────────────────────

def validate(doc, strict_judgement=True):
    """Return a list of validation errors. Empty list = the document may be written.

    `strict_judgement=False` allows a PREFILL skeleton (judgement fields still null) to pass
    the structural checks — used at pre-run to prove the skeleton is well-formed. The real
    gate before the email sends is strict.
    """
    errs = []
    if not isinstance(doc, dict):
        return ["conviction: document is not an object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"conviction: schema_version must be {SCHEMA_VERSION}, "
                    f"got {doc.get('schema_version')!r}")
    for k in ("month", "run_date", "names"):
        if doc.get(k) in (None, ""):
            errs.append(f"conviction: missing required field '{k}'")
    if "not_progressed" not in doc:
        # NOT an oversight to tolerate: this is the missed-opportunity evidence feed and the
        # input Book B needs. An absent list is different from an empty one and must be stated.
        errs.append("conviction: 'not_progressed' is absent. It is where missed-opportunity "
                    "evidence comes from and is Book B's input — an empty list is acceptable, "
                    "omitting the key is not.")
    names = doc.get("names") or []
    if not isinstance(names, list):
        return errs + ["conviction: 'names' must be a list"]
    if strict_judgement and not names:
        errs.append("conviction: 'names' is empty — a review that progressed nothing must say "
                    "so via not_progressed, not by emitting an empty names list")

    seen = set()
    for i, n in enumerate(names):
        tag = f"conviction[{n.get('ticker') or i}]"
        tk = n.get("ticker")
        if not tk:
            errs.append(f"{tag}: missing ticker")
        elif tk in seen:
            errs.append(f"{tag}: duplicate ticker")
        else:
            seen.add(tk)
        if n.get("tier") not in TIERS:
            errs.append(f"{tag}: tier {n.get('tier')!r} not in {sorted(TIERS)}")
        if n.get("route") not in ROUTES:
            errs.append(f"{tag}: route {n.get('route')!r} not in {sorted(ROUTES)}")

        # §7.6.2: session_override REQUIRES a reason.
        sts = n.get("sector_type_source")
        if sts not in ("step9_pre", "session_override"):
            errs.append(f"{tag}: sector_type_source {sts!r} must be 'step9_pre' or "
                        f"'session_override'")
        if sts == "session_override":
            ovs = n.get("overrides") or []
            has = any(o.get("field") == "sector_type" and str(o.get("reason", "")).strip()
                      for o in ovs if isinstance(o, dict))
            if not has:
                errs.append(f"{tag}: sector_type_source='session_override' with no overrides[] "
                            f"entry carrying a reason — an override without a stated reason is "
                            f"indistinguishable from a mistake")

        dims = n.get("dimensions") or {}
        if "d1_7_prerun_total" not in dims:
            errs.append(f"{tag}: dimensions.d1_7_prerun_total missing")
        _need = required_dims(n.get("tier"))
        for dk in JUDGEMENT_DIMS:
            d = dims.get(dk)
            if not isinstance(d, dict):
                errs.append(f"{tag}: dimensions.{dk} missing or malformed")
                continue
            if not strict_judgement or dk not in _need:
                # Not asked for at this tier (Step 9C gives T2 Portfolio Fit only, and T3
                # is below the progression bar). Demanding it would make the record wrong,
                # not more complete.
                continue
            # §7.6.2: "every dimension score carries the one-sentence rationale Step 9B already
            # mandates — a null rationale fails validation."
            if d.get("score") is None:
                errs.append(f"{tag}: {dk}.score is null — Step 9B requires a score")
            rat = str(d.get("rationale") or "").strip()
            if not rat:
                errs.append(f"{tag}: {dk}.rationale is empty — Step 9B mandates a one-sentence "
                            f"rationale per dimension; a score without one cannot be audited")
            elif len(rat) < 15:
                errs.append(f"{tag}: {dk}.rationale is {len(rat)} chars — too short to be the "
                            f"one-sentence rationale Step 9B requires")

        if strict_judgement and required_dims(n.get("tier")):
            # Only tiers that are actually scored must carry a total and a classification.
            if n.get("classification") not in CLASSIFICATIONS:
                errs.append(f"{tag}: classification {n.get('classification')!r} not in "
                            f"{sorted(CLASSIFICATIONS)}")
            if n.get("conviction_total") is None:
                errs.append(f"{tag}: conviction_total is null")
            elif n.get("conviction_basis") is None:
                errs.append(f"{tag}: conviction_total present but conviction_basis is null — "
                            f"a T1 /100 total and a T2 /50 total are not the same number and "
                            f"must say which they are")
            td = n.get("thesis_direction")
            if td is not None and td not in THESIS_DIRECTIONS:
                errs.append(f"{tag}: thesis_direction {td!r} not in {sorted(THESIS_DIRECTIONS)}")

        vh = n.get("vci_hurdle")
        if not isinstance(vh, dict):
            errs.append(f"{tag}: vci_hurdle block missing")
        else:
            missing = [k for k in VCI_HURDLE_KEYS if k not in vh]
            if missing:
                errs.append(f"{tag}: vci_hurdle missing keys {missing}")
            if strict_judgement and n.get("route") == "vci":
                unanswered = [k for k in VCI_HURDLE_KEYS
                              if k != "nvidia_class_exception" and vh.get(k) is None]
                if unanswered:
                    errs.append(f"{tag}: route='vci' but hurdle questions {unanswered} are "
                                f"unanswered — the VCI hurdle is the whole admission test")

        for j, o in enumerate(n.get("overrides") or []):
            if not isinstance(o, dict) or not str(o.get("reason", "")).strip():
                errs.append(f"{tag}: overrides[{j}] has no reason")

    for i, npd in enumerate(doc.get("not_progressed") or []):
        if not isinstance(npd, dict) or not npd.get("ticker"):
            errs.append(f"conviction.not_progressed[{i}]: missing ticker")
        elif not str(npd.get("reason", "")).strip():
            errs.append(f"conviction.not_progressed[{npd.get('ticker')}]: no reason. A name "
                        f"recorded without a reason carries no missed-opportunity evidence, "
                        f"which is the only purpose of this list.")
    return errs


def write(doc, month_label, here=None, strict_judgement=True):
    """Validate then write. A failing document is NOT written — a partially-captured
    judgement record that looks complete is worse than an absent one."""
    errs = validate(doc, strict_judgement=strict_judgement)
    if errs:
        raise ValueError("conviction capture failed validation:\n  - " + "\n  - ".join(errs))
    here = here or HERE
    path = os.path.join(here, f"step9_conviction_{month_label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return path


# ── self-test ────────────────────────────────────────────────────────────────────────────

def _selftest():
    fails = []

    def ok(label, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(label)

    step9 = {"main_watchlist": {
        "T1": [{"ticker": "AAA", "source_score": 70.8, "strategic_conviction_score": 31,
                "sector_type": "quality_compounder_saas", "sector_type_source": "inferred",
                "normalised_score": 78.0, "t1_qualified": True}],
        "T3": [{"ticker": "LOW", "source_score": 22.0, "strategic_conviction_score": 12,
                "normalised_score": 61.0}]},
        "vci_watchlist": {"T1_A": [{"ticker": "VVV", "vci_source_score": 66.0,
                                    "strategic_conviction_score": 28}]}}
    stack = {"stack": [{"ticker": "AAA", "action": "BUY"}]}

    doc = prefill("aug_2026", step9_pre=step9, action_stack=stack, regime="Slowdown")
    ok("U-CC1 prefill picks up progressable names", len(doc["names"]) == 2)
    ok("U-CC2 sub-45 name goes to not_progressed WITH a reason, not dropped",
       len(doc["not_progressed"]) == 1
       and doc["not_progressed"][0]["ticker"] == "LOW"
       and "22.0" in doc["not_progressed"][0]["reason"])
    ok("U-CC3 machine-computed fields are filled",
       doc["names"][0]["dimensions"]["d1_7_prerun_total"] == 31)
    ok("U-CC4 judgement fields are left NULL, never defaulted",
       all(doc["names"][0]["dimensions"][d]["score"] is None for d in JUDGEMENT_DIMS))
    ok("U-CC5 action carried from the action stack", doc["names"][0]["action"] == "BUY")
    ok("U-CC6 vci route detected", any(n["route"] == "vci" for n in doc["names"]))
    ok("U-CC7 month normalised to ISO", doc["month"] == "2026-08")
    ok("U-CC8 unknown sector_type_source normalised to step9_pre",
       doc["names"][0]["sector_type_source"] == "step9_pre")

    ok("U-CC9 prefill passes STRUCTURAL validation", not validate(doc, strict_judgement=False),
       str(validate(doc, strict_judgement=False))[:120])
    ok("U-CC10 prefill FAILS strict validation (judgement not yet made)",
       bool(validate(doc)))

    # complete it as a review session would
    for n in doc["names"]:
        for d in JUDGEMENT_DIMS:
            n["dimensions"][d] = {"score": 3, "rationale": "Reasoned one-sentence basis here."}
        n["conviction_total"] = 40
        n["conviction_basis"] = "d1_7_plus_judgements"
        n["conviction_scale_max"] = 100
        n["classification"] = "Medium"
        n["thesis_direction"] = "Unchanged"
        if n["route"] == "vci":
            n["vci_hurdle"].update({k: True for k in VCI_HURDLE_KEYS
                                    if k != "nvidia_class_exception"})
    ok("U-CC11 completed document passes strict validation", not validate(doc),
       str(validate(doc))[:160])

    # THE failure the spec names #1: a null rationale.
    bad = json.loads(json.dumps(doc))
    bad["names"][0]["dimensions"]["d8_macro_resilience"]["rationale"] = ""
    ok("U-CC12 null rationale FAILS validation",
       any("rationale is empty" in e for e in validate(bad)))
    bad2 = json.loads(json.dumps(doc))
    bad2["names"][0]["dimensions"]["d9_portfolio_fit"]["rationale"] = "ok"
    ok("U-CC13 a token rationale ('ok') also fails",
       any("too short" in e for e in validate(bad2)))

    # THE failure the spec names #2: session_override without a reason.
    bad3 = json.loads(json.dumps(doc))
    bad3["names"][0]["sector_type_source"] = "session_override"
    ok("U-CC14 session_override with no reason FAILS",
       any("session_override" in e for e in validate(bad3)))
    bad3["names"][0]["overrides"] = [{"field": "sector_type", "from": "a", "to": "b",
                                      "reason": "Reclassified after the Q2 disclosure."}]
    ok("U-CC15 session_override WITH a reason passes", not validate(bad3),
       str(validate(bad3))[:120])

    # not_progressed must exist as a key, and its entries must carry reasons.
    bad4 = json.loads(json.dumps(doc)); bad4.pop("not_progressed")
    ok("U-CC16 omitting not_progressed FAILS", any("not_progressed" in e for e in validate(bad4)))
    bad5 = json.loads(json.dumps(doc))
    bad5["not_progressed"] = [{"ticker": "ZZZ", "reason": ""}]
    ok("U-CC17 a not_progressed entry with no reason FAILS",
       any("no reason" in e for e in validate(bad5)))
    ok("U-CC18 an EMPTY not_progressed list is acceptable",
       not validate({**doc, "not_progressed": []}))

    # a vci name whose hurdle is unanswered
    bad6 = json.loads(json.dumps(doc))
    for n in bad6["names"]:
        if n["route"] == "vci":
            n["vci_hurdle"]["fv_asymmetry"] = None
    ok("U-CC19 unanswered VCI hurdle FAILS", any("hurdle questions" in e for e in validate(bad6)))

    # write() must refuse to persist an invalid document
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        raised = False
        try:
            write(bad, "aug_2026", here=td)
        except ValueError:
            raised = True
        ok("U-CC20 write() refuses an invalid document", raised)
        ok("U-CC20b ...and nothing was written",
           not os.path.exists(os.path.join(td, "step9_conviction_aug_2026.json")))
        p = write(doc, "aug_2026", here=td)
        ok("U-CC21 write() persists a valid document", os.path.exists(p))
        back = json.load(open(p, encoding="utf-8"))
        ok("U-CC22 round-trips", back["names"][0]["ticker"] == "AAA")

    # ── H6: judgement blinding ────────────────────────────────────────────────────────
    try:
        d3 = prefill("aug_2026", here=HERE)
    except Exception:
        d3 = {"names": []}
    if d3.get("names"):
        seal(d3, blind=True)
        pre = d3["names"][0].get("_prefill") or {}
        ok("U-CC23 sealing hides the ranking signals",
           not any(k in pre for k in RANKING_REVEALS))
        ok("U-CC23b tier stays visible (it sets dimension scope)",
           d3["names"][0].get("tier") is not None)
        sealed_keys = set((d3["names"][0].get("_prefill_sealed") or {}).keys())
        ok("U-CC23c sealing is LOSSLESS — values are moved, not deleted", bool(sealed_keys))
        seal(d3, blind=False)
        pre2 = d3["names"][0].get("_prefill") or {}
        ok("U-CC23d unsealing restores every sealed value",
           sealed_keys.issubset(pre2.keys()) and "_prefill_sealed" not in d3["names"][0])

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)}) {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefill", action="store_true")
    ap.add_argument("--month")
    ap.add_argument("--regime")
    ap.add_argument("--blind", action="store_true",
                    help="seal the Source Score / normalised score / rank so D8-D10 are scored "
                         "blind to the ranking (H6). Reversible via --unseal.")
    ap.add_argument("--unseal", metavar="DOC_JSON",
                    help="restore the sealed ranking signals after judgements are recorded")
    ap.add_argument("--validate")
    ap.add_argument("--lenient", action="store_true",
                    help="structural validation only (a prefill skeleton)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", metavar="JUDGEMENTS_JSON",
                    help="apply session judgements to the month's conviction doc and derive "
                         "conviction_total / classification")
    ap.add_argument("--gate", metavar="DOC_JSON",
                    help="hard gate: exit non-zero if the month may not send")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.gate:
        with open(a.gate, encoding="utf-8") as f:
            doc = json.load(f)
        errs = gate(doc)
        for e in errs:
            print("BLOCK: " + e)
        print("CONVICTION GATE PASS — month may send" if not errs
              else f"CONVICTION GATE FAIL — {len(errs)} blocking issue(s); DO NOT SEND")
        return 1 if errs else 0
    if a.apply:
        if not a.month:
            ap.error("--month required with --apply")
        path = os.path.join(HERE, f"step9_conviction_{a.month}.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        with open(a.apply, encoding="utf-8") as f:
            raw = json.load(f)
        j = {k: {kk: (tuple(vv) if isinstance(vv, list) else vv) for kk, vv in v.items()}
             for k, v in raw.items()}
        res = apply_judgements(doc, j)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        print(f"CONVICTION_APPLY month={a.month} applied={len(res['applied'])} "
              f"({', '.join(res['applied'][:10])})")
        errs = validate(doc, strict_judgement=True)
        print("  gate: " + ("PASS" if not errs else f"{len(errs)} outstanding"))
        for e in errs[:8]:
            print("    - " + e)
        return 0
    if a.unseal:
        with open(a.unseal, encoding="utf-8") as f:
            doc = json.load(f)
        seal(doc, blind=False)
        with open(a.unseal, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        print(f"UNSEALED {a.unseal} — ranking signals restored")
        return 0
    if a.validate:
        with open(a.validate, encoding="utf-8") as f:
            doc = json.load(f)
        errs = validate(doc, strict_judgement=not a.lenient)
        for e in errs:
            print("FAIL: " + e)
        print("CONVICTION VALID" if not errs else f"{len(errs)} VALIDATION ERROR(S)")
        return 1 if errs else 0
    if a.prefill:
        if not a.month:
            ap.error("--month required")
            doc = prefill(a.month, regime=a.regime)
        if a.blind:
            doc = seal(doc, blind=True)
        path = os.path.join(HERE, f"step9_conviction_{a.month}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        errs = validate(doc, strict_judgement=False)
        print(f"CONVICTION_PREFILL month={a.month} names={len(doc['names'])} "
              f"not_progressed={len(doc['not_progressed'])} -> {path}")
        print("  structural: " + ("OK" if not errs else f"{len(errs)} error(s)"))
        print("  NOTE: judgement fields are intentionally null. The review session must fill "
              "D8/D9/D10 score + rationale, conviction_total and classification before the "
              "email sends; write() will refuse the file until it does.")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
