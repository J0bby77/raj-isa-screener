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


def _evidence_states():
    """The declared evidence-state vocabulary, read from its one home (evidence_state.STATE_TO_RUNG)."""
    import evidence_state as _es
    return set(_es.STATE_TO_RUNG)


def _single_authority() -> bool:
    """Is D21's single sizing authority live? Defaults TRUE when the flag is undeclared —
    the post-D21 world is the intended one, and a rollback must be a DELIBERATE act."""
    try:
        import isa_policy as _p
        if "single_sizing_authority" in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS["single_sizing_authority"])
    except Exception:                                                   # noqa: BLE001
        pass
    return True


def _thesis_states():
    """The declared states, READ from their one home (R4.4) — never restated here."""
    try:
        import thesis_state as _ts
        return tuple(_ts.STATES)
    except Exception:                                                   # noqa: BLE001
        # ⚑ A REFUSAL, NOT A FALLBACK LIST. If the module is unimportable the validator must
        # not invent the vocabulary it is validating against — a second copy of the state list
        # here is exactly the two-homes defect P7 exists to close.
        return ()
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


def _judgement_errors(doc, n):
    """The D21 judgement errors for ONE name: strict minus structural validation of a one-name
    document. Derived from validate() itself, so the rule has one home (R4.4)."""
    one = dict(doc, names=[n], not_progressed=doc.get("not_progressed") or [])
    structural = set(validate(one, strict_judgement=False))
    return [e for e in validate(one, strict_judgement=True) if e not in structural]


def gate_by_name(doc, scope):
    """ISA-0698 (Raj, 16-Sep-2026, Option B) — the §7.6.2 gate, per name, on the mechanically
    derived capital-precondition population (`capital_destination.judgement_scope`).

    Returns {blocking, scope_state, required, complete, refused_capital, non_blocking_missing,
    not_applicable, counts}. Only `blocking` (a structurally invalid record) stops the email.
    A REQUIRED name with missing/invalid judgement is REFUSED NEW CAPITAL by name; a name outside
    the population that lacks judgement is NON_BLOCKING_RECORD_MISSING; held names are judged in
    thesis_state / held review and are NOT_APPLICABLE here. An UNKNOWN scope refuses positive
    stock capital for every main/VCI candidate — never a run-wide pass and never a silent block.
    The scope is read, never computed here: nothing in the record can move a name into or out
    of it."""
    doc = doc if isinstance(doc, dict) else {}
    blocking = validate(doc, strict_judgement=False) if doc else ["conviction: record absent"]
    names = {n.get("ticker"): n for n in (doc.get("names") or []) if isinstance(n, dict)}
    out = {"blocking": blocking, "required": [], "complete": [], "refused_capital": {},
           "non_blocking_missing": [], "not_applicable": [],
           "basis": "ISA-0698 Option B — judgement mandatory only for the capital-precondition population"}
    if not isinstance(scope, dict) or scope.get("state") != "OK":
        out["scope_state"] = "UNKNOWN"
        out["refused_capital"] = "ALL_REQUIRED_UNKNOWN_SCOPE"
        out["why"] = ((scope or {}).get("why") if isinstance(scope, dict) else None) or \
            "no pre-judgement judgement_scope is available"
    else:
        out["scope_state"] = "OK"
        sn = scope.get("names") or {}
        for tk in sorted(sn):
            sc = sn[tk].get("scope")
            if sc == "REQUIRED_FOR_CAPITAL_DECISION":
                out["required"].append(tk)
                n = names.get(tk)
                if n is None:
                    out["refused_capital"][tk] = ["REQUIRED_FOR_CAPITAL_DECISION but absent from "
                                                  "the Step 9 record"]
                    continue
                errs = _judgement_errors(doc, n)
                if errs:
                    out["refused_capital"][tk] = errs
                else:
                    out["complete"].append(tk)
            elif sc == "NOT_APPLICABLE":
                out["not_applicable"].append(tk)
        for tk, n in names.items():
            if tk in sn and sn[tk].get("scope") != "NON_BLOCKING_RECORD":
                continue
            if required_dims(n.get("tier")) and _judgement_errors(doc, n):
                out["non_blocking_missing"].append(tk)
    out["counts"] = {"required": len(out["required"]), "complete": len(out["complete"]),
                     "refused_capital": (len(out["refused_capital"])
                                         if isinstance(out["refused_capital"], dict) else "ALL"),
                     "non_blocking_missing": len(out["non_blocking_missing"]),
                     "not_applicable": len(out["not_applicable"]), "blocking": len(blocking)}
    return out


def gate(doc):
    """LEGACY whole-record strict validation (every T1/T2 name). ⚑ ISA-0698: the email path no
    longer uses this as its gate — it uses gate_by_name() on the capital-precondition scope. Kept
    for the CLI's completeness report and the rollback path.
    Hard gate for the run: returns [] when the month may send, else the blocking errors.
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
            # ── P7.3 (D21, 28-Aug-2026) — WHERE JUDGEMENT NOW LIVES ────────────────────
            # LEFT NULL ON PURPOSE, exactly like d8/d9/d10 above: these are the session
            # judgements this file exists to capture, and validate() fails while either is
            # empty. ⚑ `INTACT` as a DEFAULT would make "nobody looked" and "the thesis is as
            # underwritten" render identically — which is R2.10's exact prohibition, and is
            # how the /100 came to stand at 2 of 53 populated while still gating the email.
            "thesis_state": None,
            "thesis_state_rationale": "",
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
        # ⚑ ISA-0466 (16-Sep-2026) — THE RETIRED /100 NO LONGER GATES THE EMAIL. D21 retired the
        #   conviction score as a decision input (P7.1) and P7.3 made the gate read thesis_state +
        #   evidence_state — but this loop still DEMANDED a D8/D9/D10 score and rationale per T1/T2
        #   name, and the block below still demanded a /100 `classification`. Measured on
        #   step9_conviction_sep_2026.json: 797 blocking errors over 107 T1/T2 names, most of them
        #   for inputs to a number that decides nothing — so every Sunday either hand-filled a
        #   display score or ran with --allow-unrecorded-conviction and the gate OFF.
        #   Under the single sizing authority a dimension is OPTIONAL; if a session DOES record a
        #   score it must still carry its rationale (an unexplained number is never admissible).
        #   The rollback path (V2_FLAGS["single_sizing_authority"] = False) keeps the old demand.
        _dims_gate = not _single_authority()
        for dk in JUDGEMENT_DIMS:
            d = dims.get(dk)
            if not isinstance(d, dict):
                errs.append(f"{tag}: dimensions.{dk} missing or malformed")
                continue
            if strict_judgement and dk in _need and not _dims_gate:
                if d.get("score") is not None and not str(d.get("rationale") or "").strip():
                    errs.append(f"{tag}: {dk}.rationale is empty while a score is recorded — an "
                                f"optional judgement, once made, must be auditable")
                elif d.get("score") is not None and len(str(d.get("rationale")).strip()) < 15:
                    errs.append(f"{tag}: {dk}.rationale is too short to be the one-sentence "
                                f"rationale Step 9B requires of a recorded score")
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
            # ⚑ ISA-0466: `classification` is the /100 band — required only on the rollback path;
            #   under the single authority it is display, and if present it must still be valid.
            if (not _single_authority() or n.get("classification") is not None) and \
                    n.get("classification") not in CLASSIFICATIONS:
                errs.append(f"{tag}: classification {n.get('classification')!r} not in "
                            f"{sorted(CLASSIFICATIONS)}")
            # ══════════════════════════════════════════════════════════════════════════════
            # P7.3 — THE §7.6.2 GATE NOW READS `thesis_state` + `evidence_state` (D21)
            # ══════════════════════════════════════════════════════════════════════════════
            # ⚑ WHY THE OLD GATE HAD TO GO: it required `conviction_total`, and
            # `step9_conviction_aug_2026.json` carries that field NULL for EVERY name, with the
            # /100 standing at 2 of 53 populated. A gate on a field nobody fills is not a gate
            # — it is a control that reports success while doing nothing, which is this
            # project's dominant failure class wearing the costume of a check.
            #
            # ⚑⚑ AND R11 IS WHY BOTH BRANCHES EXIST RATHER THAN ONE. Retiring the /100 removes
            # a control; if `thesis_state` were optional the net effect of P7 would be to
            # remove a control and add none (C8). So the NEW gate must be refusing BEFORE the
            # old one is gone, and the rollback path must still refuse on something. There is
            # never a window with neither.
            if _single_authority():
                _ts = n.get("thesis_state")
                if _ts is None:
                    errs.append(f"{tag}: thesis_state is null — D21 moved judgement out of the "
                                f"/100 and INTO thesis_state, which may BLOCK, DOWNSIZE or HOLD "
                                f"and never upsize. Declare one of "
                                f"{sorted(_thesis_states())}.")
                elif _ts not in _thesis_states():
                    errs.append(f"{tag}: thesis_state {_ts!r} is not declared. Declared: "
                                f"{sorted(_thesis_states())}.")
                _tr = str(n.get("thesis_state_rationale") or "").strip()
                if not _tr:
                    errs.append(f"{tag}: thesis_state has no rationale. A state without a "
                                f"reason cannot be challenged next month, which is the only "
                                f"thing that makes a judgement reviewable rather than a "
                                f"preference.")
                elif len(_tr) < 15:
                    errs.append(f"{tag}: thesis_state_rationale is {len(_tr)} chars — too "
                                f"short to be the one-sentence rationale §7.6.2 requires of "
                                f"every judgement field.")
                if n.get("evidence_state") is None:
                    errs.append(f"{tag}: evidence_state is null — the §7.6.2 gate reads "
                                f"thesis_state AND evidence_state, both non-null with "
                                f"rationales. Evidence sets the rung; thesis_state may only "
                                f"cap it.")
                elif n.get("evidence_state") not in _evidence_states():
                    # ⚑ ISA-0466: non-null was the whole test, so a typo passed the gate and would
                    #   have been read as a rung key downstream (R4.8 — never a guessed state).
                    errs.append(f"{tag}: evidence_state {n.get('evidence_state')!r} is not declared. "
                                f"Declared: {sorted(_evidence_states())}.")
            else:
                # ROLLBACK PATH (V2_FLAGS["single_sizing_authority"] = False): the pre-D21
                # gate, unchanged, so the flag restores the old behaviour exactly.
                if n.get("conviction_total") is None:
                    errs.append(f"{tag}: conviction_total is null")
                elif n.get("conviction_basis") is None:
                    errs.append(f"{tag}: conviction_total present but conviction_basis is null "
                                f"— a T1 /100 total and a T2 /50 total are not the same number "
                                f"and must say which they are")
            # ⚑ conviction_total remains COMPUTED AND DISPLAYED either way (P7.1 / A10). What
            # changed is that no GATE reads it — enforced by AST in
            # consistency_check.pair_no_gate_reads_conviction_score.
            if n.get("conviction_total") is not None and n.get("conviction_basis") is None:
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
    ok("U-CC10 NEGATIVE CONTROL: prefill FAILS strict validation (judgement not yet made)",
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
        # ⚑ ISA-0694 (16-Sep-2026): D21/P7.3 (ISA-0466 leg, 12-Sep-2026) made the strict gate read
        #   thesis_state + evidence_state; this "completed as a review session would" fixture
        #   was not moved with it, so U-CC11/U-CC15/U-CC21 went red on a correct gate. The
        #   fixture now completes the judgement the gate actually requires.
        n["thesis_state"] = "INTACT"
        n["thesis_state_rationale"] = "Thesis unchanged after the latest quarterly results."
        n["evidence_state"] = "THIN"
        if n["route"] == "vci":
            n["vci_hurdle"].update({k: True for k in VCI_HURDLE_KEYS
                                    if k != "nvidia_class_exception"})
    # ── ISA-0466 · the retired /100 inputs no longer gate under the single authority ──────
    _nodims = json.loads(json.dumps(doc))
    for n in _nodims["names"]:
        for d in JUDGEMENT_DIMS:
            n["dimensions"][d] = {"score": None, "rationale": ""}
        n["conviction_total"] = None
        n["classification"] = None
    ok("U-CC30 MUST-FIRE: thesis_state + evidence_state recorded, NO D8/D9/D10 and no /100 "
       "classification -> passes the strict gate under the single sizing authority",
       not validate(_nodims), str(validate(_nodims))[:160])
    import isa_policy as _pol_cc
    _had_sa = "single_sizing_authority" in _pol_cc.V2_FLAGS
    _old_sa = _pol_cc.V2_FLAGS.get("single_sizing_authority")
    _pol_cc.V2_FLAGS["single_sizing_authority"] = False
    try:
        ok("U-CC31 ROLLBACK NEGATIVE CONTROL: the same document FAILS with the single authority "
           "switched off (the old /100 demand is restored exactly)",
           any("d8_macro_resilience" in e or "classification" in e for e in validate(_nodims)))
    finally:
        if _had_sa:
            _pol_cc.V2_FLAGS["single_sizing_authority"] = _old_sa
        else:
            _pol_cc.V2_FLAGS.pop("single_sizing_authority", None)
    _bogus = json.loads(json.dumps(doc))
    _bogus["names"][0]["evidence_state"] = "CONFIRMD"
    ok("U-CC32 NEGATIVE CONTROL: an undeclared evidence_state (typo) FAILS rather than passing "
       "as non-null", any("evidence_state 'CONFIRMD' is not declared" in e for e in validate(_bogus)))
    _noth = json.loads(json.dumps(_nodims))
    _noth["names"][0]["thesis_state"] = None
    ok("U-CC33 NEGATIVE CONTROL: dropping the dims does NOT drop the D21 judgement - a null "
       "thesis_state still FAILS", any("thesis_state is null" in e for e in validate(_noth)))

    # ── ISA-0698 · per-name gate on the mechanical capital-precondition scope ─────────────
    _names = [n["ticker"] for n in doc["names"]]
    _req, _other = _names[0], _names[1:]
    _scope = {"state": "OK", "names": dict(
        {_req: {"scope": "REQUIRED_FOR_CAPITAL_DECISION"}},
        **{t: {"scope": "NON_BLOCKING_RECORD"} for t in _other},
        HELDX={"scope": "NOT_APPLICABLE"})}
    _g_ok = gate_by_name(doc, _scope)
    ok("U-CC40 POSITIVE CONTROL: required name with valid judgement -> complete, nothing refused, "
       "no run block", _g_ok["complete"] == [_req] and not _g_ok["refused_capital"]
       and not _g_ok["blocking"], str(_g_ok)[:200])
    _miss = json.loads(json.dumps(doc))
    for n in _miss["names"]:
        n["thesis_state"] = None
    _g = gate_by_name(_miss, _scope)
    ok("U-CC41 MUST-FIRE: required name with missing judgement -> REFUSED NEW CAPITAL for that "
       "name only", list(_g["refused_capital"]) == [_req] and not _g["blocking"], str(_g)[:200])
    ok("U-CC42 NON-BLOCKING: names outside the scope lacking judgement are recorded as "
       "non_blocking_missing and do not block the run",
       set(_g["non_blocking_missing"]) >= {t for t in _other
                                           if required_dims(next(n for n in _miss["names"] if n["ticker"] == t)["tier"])}
       and not _g["blocking"])
    ok("U-CC43 SCOPE INTEGRITY NC: changing judgement does not change who is required",
       _g["required"] == _g_ok["required"] == [_req])
    _gu = gate_by_name(doc, None)
    ok("U-CC44 NEGATIVE CONTROL: an UNKNOWN scope refuses positive capital for all required "
       "candidates (never a pass, never a run block)",
       _gu["scope_state"] == "UNKNOWN" and _gu["refused_capital"] == "ALL_REQUIRED_UNKNOWN_SCOPE"
       and not _gu["blocking"])
    _abs = gate_by_name(dict(doc, names=[n for n in doc["names"] if n["ticker"] != _req]), _scope)
    ok("U-CC45 NEGATIVE CONTROL: a REQUIRED name absent from the record is refused, not skipped",
       _req in _abs["refused_capital"])
    ok("U-CC46 HELD NAME is NOT_APPLICABLE here — its judgement home is thesis_state / held review",
       _g_ok["not_applicable"] == ["HELDX"] and "HELDX" not in _g_ok["refused_capital"])
    _bad_struct = json.loads(json.dumps(doc)); _bad_struct.pop("not_progressed")
    ok("U-CC47 NEGATIVE CONTROL: a STRUCTURALLY invalid record still blocks",
       bool(gate_by_name(_bad_struct, _scope)["blocking"]))

    ok("U-CC11 completed document passes strict validation", not validate(doc),
       str(validate(doc))[:160])

    # THE failure the spec names #1: a null rationale.
    bad = json.loads(json.dumps(doc))
    bad["names"][0]["dimensions"]["d8_macro_resilience"]["rationale"] = ""
    ok("U-CC12 NEGATIVE CONTROL: null rationale FAILS validation",
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
        # ISA-0698: per-name gate on capital_destination's PRE-JUDGEMENT scope (same file month)
        import re as _re
        _m = _re.search(r"step9_conviction_([a-z]{3}_\d{4})", os.path.basename(a.gate))
        _scope = None
        if _m:
            # one source for the router's scope: the run context (ISA-0447)
            _rcp = os.path.join(os.path.dirname(os.path.abspath(a.gate)),
                                "run_context_%s.json" % _m.group(1))
            if os.path.exists(_rcp):
                with open(_rcp, encoding="utf-8") as f:
                    _scope = (((json.load(f) or {}).get("summary") or {})
                              .get("capital_destination") or {}).get("judgement_scope")
        res = gate_by_name(doc, _scope)
        for e in res["blocking"]:
            print("BLOCK: " + e)
        _ref = res["refused_capital"]
        for tk in (sorted(_ref) if isinstance(_ref, dict) else []):
            print("REFUSED NEW CAPITAL: %s — %s" % (tk, _ref[tk][0][:160]))
        print("scope %s · counts %s" % (res["scope_state"], res["counts"]))
        print("CONVICTION RECORD VALID — the email may build; refused names receive no new capital"
              if not res["blocking"] else
              f"CONVICTION RECORD INVALID — {len(res['blocking'])} blocking issue(s); DO NOT SEND")
        return 1 if res["blocking"] else 0
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
