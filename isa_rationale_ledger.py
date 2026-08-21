"""P2.5 - the Rationale Ledger: why every capital-gating constant is what it is.

Standard: ISA_Engineering_Rules.md R12.3.  Spec: ISA_BuildSpec_ItemRegister_4C_09Aug2026.md s11A.

THE QUESTION IT ANSWERS
----------------------
R12.3: "every constant that gates capital carries its provenance - who_set_it, evidence_basis,
set_on, last_validated, revalidate_by, what_would_falsify_it." The rules file records that this
is currently UNANSWERABLE for SOURCE_WEIGHTS, the 60/75 conviction bands, the 65 fresh-capital
bar, the +-10pp E[r] cap ("never calibrated"), the drawdown ladder and the 12-1m vs
PRICE_MOM_BLEND overlap. A framework that cannot say why a number is what it is cannot defend
it, and H5 cannot resolve.

NO SECOND REGISTER (R7.1/P8). A rationale is a `RATIONALE` record on the SAME store, so it is
ranked, rendered, archived and contract-checked by the same machinery.

THE HONEST DEFAULT
------------------
`NO_RECORDED_RATIONALE` is a permitted and important answer, and R12.3 says it AUTO-RAISES an
item. So the ledger does exactly that: a capital-gating constant with no provenance produces a
RATIONALE record whose `evidence_basis` is NO_RECORDED_RATIONALE and which is OPEN until someone
answers it. Filling the ledger with plausible-sounding justifications I invented today would be
worse than leaving it empty - it would make an unanswered question look answered.

CLI:
  python3 isa_rationale_ledger.py --scan        # constants found, and which have provenance
  python3 isa_rationale_ledger.py --seed        # create the RATIONALE records
  python3 isa_rationale_ledger.py --gaps        # constants still with NO_RECORDED_RATIONALE
  python3 isa_rationale_ledger.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import isa_register as R

LEDGER_VERSION = "1.0.0"
DECLARATIONS = "isa_rationale_declarations.json"

# ⚑ DECLARED, not discovered. "Which constants gate capital" is a judgement about the framework,
# so it is written down and reviewable rather than inferred from a name pattern - a regex over
# ALL_CAPS would sweep in formatting widths and call it a capital control (R4.8).
CAPITAL_GATING = {
    "GATE4_CONCENTRATION_LIFT": ("screener_core.py", "The exclusion-rate lift at or above which a Gate-4 sector concentration warning fires: a sector cut at twice the frame's own base rate. Replaced the raw-share measure on 19-Aug-2026 (ISA-0375); the warning shapes how a screen's exclusions are read before names reach capital."),
    "GATE4_CONCENTRATION_THRESHOLD": ("screener_core.py", "RETIRED AS A TRIGGER 19-Aug-2026 (ISA-0375) and retained only as a published comparison statistic. It decides nothing; it is kept in this list so its retirement stays on the record rather than disappearing with it."),
    "REGIME_COVERAGE_MULTIPLIER": ("capital_destination.py", "Fraction of the sleeve median max drawdown a destination must have LIVED THROUGH; below it the fund is REFUSED as a destination for new capital (ISA-0154 / D-19)."),
    "SN_RATIO_ADMISSIBLE": ("capital_destination.py", "Signal/noise variance ratio above which trailing return may ORDER destinations rather than merely veto them (ISA-0153 / D-18)."),
    "SOURCE_WEIGHTS": ("scoring_config.py", "Selects SUMMARY, which is the set stock capital is deployed from."),
    "APS_FRESH_CAPITAL_BAR": ("scoring_config.py", "Source Score at or above the bar makes a name eligible for FRESH CAPITAL."),
    "ER_DEPLOY_FLOOR": ("scoring_config.py", "Hard floor on expected return; below it, no deployment."),
    "ER_RERATE_CAP": ("scoring_config.py", "Clamps the re-rate term inside E[r]; fires on 189 of 312 names (ISA-0029)."),
    "ER_RERATE_NEUTRAL_BAND": ("scoring_config.py", "Decides which names get zero re-rate credit rather than a penalty."),
    "EVIDENCE_ER_CONF_MIN": ("scoring_config.py", "er_confidence floor for the fundamentals evidence route."),
    "MIN_HOLD_DAYS": ("scoring_config.py", "Anti-churn rule; blocks an exit inside the window. Not paused with the rest of compliance."),
    "DRAWDOWN_TRANCHES": ("scoring_config.py", "The B1 ladder: when reserve capital is deployed."),
    "DRAWDOWN_LOOKBACK": ("scoring_config.py", "Window defining the trailing high the ladder measures against."),
    "DRAWDOWN_BUFFER_GBP": ("scoring_config.py", "Cash excluded from the deployable reserve."),
    "PRICE_MOM_BLEND": ("scoring_config.py", "Reintroduces the 21 days the 12-1m window deliberately skips (ISA-0146 / BL-6)."),
    "PRICE_MOM_PCTL_CUTS": ("scoring_config.py", "Cross-sectional cuts converting momentum into the score that selects SUMMARY."),
    "PRICE_MOM_LOOKBACK": ("scoring_config.py", "Momentum measurement window."),
    "PRICE_MOM_SKIP": ("scoring_config.py", "Days skipped to exclude short-term reversal."),
    "CONVICTION_FRACTIONS": ("scoring_config.py", "Conviction bands; gates deployment at Step 9."),
    "VCI_SOURCE_WEIGHTS": ("scoring_config.py", "Ranks the VCI sleeve, from which real starters are bought."),
    "FX_RATE_FRACTION": ("extract_cash_statement.py", "Every USD trade cost estimate in the framework."),
}

# Provenance is recognised ONLY in these forms. A trailing comment is where this framework
# actually records its reasons, so it counts - but the SHAPE is declared, never guessed.
_DATE = r"(\d{1,2}-[A-Za-z]{3}-\d{2,4}|\d{4}-\d{2}-\d{2})"

# ⚑ WIDENED 15-Aug-2026 (ISA-0345). The item-reference vocabulary was NARROWER THAN THE PROJECT'S
# OWN ITEM NAMING, so it under-reported provenance that was sitting in the comment: the drawdown
# constants cite "B1/D10", "B1/D9", "B1/D11" and VCI_SOURCE_WEIGHTS cites "v2 (E8/E6)", none of
# which the old pattern could match because it required a hyphen (`D-\d+`) and knew only the
# C/H/WP/ISA families. That is FC-H - a verifier whose coverage is narrower than its subject - and
# it makes the ledger under-count rather than over-count, which is the direction that hides work.
# The families are DECLARED here, never inferred, so widening it is reviewable (R4.8).
_ITEM_FAMILIES = r"(?:D|C|H|A|B|E|V|M|O|L|G|F|N|T|P|R|S|WP|BL|MB|FRS|WPM|CAP|TR)"
_PROVENANCE_MARKS = (
    (re.compile(_DATE), "dated"),
    (re.compile(r"\b(backtest|backtested|study|measured|calibrat)\w*", re.I), "evidence named"),
    (re.compile(r"\b(" + _ITEM_FAMILIES + r"-?\d{1,3}|ISA-\d{4}|WP-[A-Z0-9]+)\b"), "item referenced"),
)



def provenance_marks(comment: str) -> list:
    """-> the provenance labels a comment carries. THE one home for what `_PROVENANCE_MARKS` means.

    ISA-0363. Before this existed the pattern set was applied inline at its single call site, so a
    control could only be written by RE-IMPLEMENTING the match — and a control that re-implements
    the thing it controls tests the copy, not the recogniser. `register_callsites` now calls this.
    """
    return [label for rx, label in _PROVENANCE_MARKS if rx.search(comment or "")]


# R13.1 bases, plus the two honest non-answers. Anything else is a typo wearing a verdict.
VALID_EVIDENCE_BASIS = ("BACKTESTED", "MEASURED", "DECLARED", "REFUSED_FOR_POWER",
                        "PARTIAL_FROM_CODE_COMMENT", "NO_RECORDED_RATIONALE")

# A ledger entry is COMPLETE only with all four. Falsifiability alone is not provenance, and a
# revalidate date alone is not either: R12.3 asks who chose the number, on what evidence, what
# would disprove it, and when we look again.
REQUIRED_DECLARATION_FIELDS = ("who_set_it", "evidence_basis", "what_would_falsify_it",
                               "revalidate_by")


def root_dir(root=None) -> Path:
    return Path(root).resolve() if root else Path(__file__).resolve().parent


def _assignments(path: Path) -> dict:
    """{NAME: (lineno, trailing comment or '')} for module-level assignments."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tree = ast.parse(text)
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                end = getattr(node, "end_lineno", node.lineno)
                comment = ""
                for ln in lines[node.lineno - 1:end]:
                    if "#" in ln:
                        comment += " " + ln.split("#", 1)[1].strip()
                # a comment block immediately ABOVE the assignment counts too
                i = node.lineno - 2
                above = []
                while i >= 0 and lines[i].strip().startswith("#"):
                    above.insert(0, lines[i].strip().lstrip("# "))
                    i -= 1
                out[t.id] = (node.lineno, (" ".join(above) + " " + comment).strip())
    return out


def scan(root=None) -> dict:
    root = root_dir(root)
    found, missing = {}, []
    cache = {}
    for name, (module, why) in sorted(CAPITAL_GATING.items()):
        p = root / module
        if not p.exists():
            missing.append(f"{name}: {module} not on disk")
            continue
        if module not in cache:
            cache[module] = _assignments(p)
        entry = cache[module].get(name)
        if entry is None:
            missing.append(f"{name}: declared capital-gating but not assigned in {module}")
            continue
        lineno, comment = entry
        marks = provenance_marks(comment)
        found[name] = {"module": module, "line": lineno, "why_it_gates_capital": why,
                       "comment": comment[:400], "provenance_marks": marks,
                       "has_provenance": bool(marks)}
    return {"constants": found, "unresolved": missing,
            "with_provenance": sum(1 for v in found.values() if v["has_provenance"]),
            "total": len(found)}


def _declarations(root=None) -> dict:
    p = R.store_dir() / DECLARATIONS
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def seed(root=None, dry_run=False) -> dict:
    """One RATIONALE record per capital-gating constant. Same store, no second register."""
    res = scan(root)
    if res["unresolved"]:
        raise ValueError("rationale ledger refused - a declared capital-gating constant could not "
                         "be located, and a ledger with holes is worse than none:\n  - "
                         + "\n  - ".join(res["unresolved"]))
    declared = _declarations(root)
    existing = {a for it in R.read_all() for a in it.get("aliases", [])}
    written, gaps = [], []
    for name, info in res["constants"].items():
        alias = f"RATIONALE:{name}"
        if alias in existing:
            continue
        d = declared.get(name, {})
        recorded = d or (info["comment"] if info["has_provenance"] else "")
        rec = {
            "title": f"[{name}] why is this constant what it is? ({info['module']})",
            "aliases": [alias],
            "record_type": "RATIONALE",
            "criticality": "HIGH" if not info["has_provenance"] else "MEDIUM",
            # Closed only when the provenance is actually COMPLETE. Falsifiability alone is not
            # provenance: knowing what would disprove a number says nothing about who chose it
            # or on what evidence.
            "state": ("CLOSED_NOT_A_DEFECT"
                      if (d.get("who_set_it") and d.get("evidence_basis")
                          and d.get("what_would_falsify_it"))
                      else "OPEN"),
            "domain": "analysis",
            "intake_trigger": "build_discovery",
            "detected_by": "AUTOMATED_BATTERY",
            "detected_on": R._today(),
            "introduced_basis": "unknown",
            "provenance": "captured_live",
            "capital_link": info["why_it_gates_capital"],
            "context": (f"{name} is assigned at {info['module']}:{info['line']}. "
                        f"{info['why_it_gates_capital']}"),
            # ⚑ ISA-0347. The five Cs are mandatory on every record (ISA-0337, 13-Aug-2026) and
            # seed() did not supply them, so it could not write at all and its selftest was RED
            # for two days. These are NOT invented history (R7.5): each states a fact about the
            # LEDGER's own mechanics - why a number arrived without provenance, and what now
            # stops the next one - never a reconstructed reason for the number itself.
            "cause_proximate": (
                f"{name} is assigned at {info['module']}:{info['line']} with "
                + ("its reason recorded only in an adjacent code comment, which nothing asserts "
                   "and no consumer reads." if info["has_provenance"] else
                   "no adjacent record of who chose the value or on what evidence.")),
            "cause_systemic": (
                "R12.3 was adopted 09-Aug-2026, after most capital-gating constants were already "
                "set, so carrying provenance was never a precondition of shipping one. Nothing "
                "failed a build when a number arrived without a reason, which made the omission "
                "free and therefore universal."),
            "consequence": (
                f"UNQUANTIFIED at intake - a constant whose basis is unrecorded cannot be "
                f"defended, revalidated or falsified, so H5 cannot resolve for it. "
                f"Capital exposure: {info['why_it_gates_capital']}"),
            "corrective_action": (
                f"A declaration for {name} in {DECLARATIONS} carrying the six R12.3 fields, "
                "applied to this record by refresh(), asserted by declaration_errors() and "
                "re-opened automatically by stale_revalidations() when revalidate_by passes."),
            "rationale": {
                "constant": name,
                "home": f"{info['module']}:{info['line']}",
                "who_set_it": d.get("who_set_it"),
                "evidence_basis": d.get("evidence_basis") or (
                    "PARTIAL_FROM_CODE_COMMENT" if info["has_provenance"] else "NO_RECORDED_RATIONALE"),
                "set_on": d.get("set_on"),
                "last_validated": d.get("last_validated"),
                "revalidate_by": d.get("revalidate_by"),
                "what_would_falsify_it": d.get("what_would_falsify_it"),
            },
            "narrative": (f"Recorded provenance found in code: {recorded!r}" if recorded else
                          "NO recorded rationale. R12.3: this is a permitted and important answer, "
                          "and it auto-raises this item. It is NOT filled with a plausible "
                          "justification written today - that would make an unanswered question "
                          "look answered, which is the exact failure this register exists to catch."),
            "learning": {"learnable": False,
                         "reason_none": "provenance is recalled or reconstructed, not learned from data"},
            "source_doc": f"{info['module']} line {info['line']} (as_of {R._today()})",
            "is_fix": False,
        }
        if not info["has_provenance"]:
            gaps.append(name)
        if not dry_run:
            rec["id"] = R.next_id()
            R.write(rec)
            R.register_alias(alias, rec["id"], source="rationale_ledger")
        written.append(name)
        existing.add(alias)
    # ⚑ Two DIFFERENT quantities, named apart on purpose (R6.2): `no_provenance_in_code` counts
    # constants whose code comment says nothing, `gaps()` counts ledger entries with no
    # evidence_basis. A declaration can answer the second without changing the first, and
    # reporting one number for both would conceal exactly that.
    return {"seeded": len(written), "constants": written,
            "no_provenance_in_code": sorted(gaps), "dry_run": dry_run}


def declaration_errors(root=None) -> list:
    """Contract on the declarations file itself (R5.1 - assert at the artefact boundary)."""
    errs = []
    decl = _declarations(root)
    for name in sorted(CAPITAL_GATING):
        d = decl.get(name)
        if not d:
            errs.append(f"{name}: declared capital-gating with no entry in {DECLARATIONS}")
            continue
        for f in REQUIRED_DECLARATION_FIELDS:
            if not d.get(f):
                errs.append(f"{name}: declaration missing `{f}` (R12.3)")
        eb = d.get("evidence_basis")
        if eb and eb not in VALID_EVIDENCE_BASIS:
            errs.append(f"{name}: evidence_basis {eb!r} is not one of {VALID_EVIDENCE_BASIS} (R13.1)")
        rb = d.get("revalidate_by")
        if rb and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(rb)):
            errs.append(f"{name}: revalidate_by {rb!r} is not an ISO date (R4.2)")
    for name in sorted(decl):
        if name not in CAPITAL_GATING:
            errs.append(f"{name}: declaration present for a constant NOT declared capital-gating - "
                        "either add it to CAPITAL_GATING or remove the declaration (R4.4)")
    return errs


def stale_revalidations(today=None, root=None) -> list:
    """Entries whose revalidate_by has passed.

    ⚑ THIS IS THE RECURRENCE TEST. Provenance is not a thing you record once: a DECLARED constant
    is a promise to look again on a stated date, and a promise with no alarm is R14.1's "anything
    that depends on someone remembering". A passed date RE-OPENS the item - closure is temporary
    by construction, which is the honest shape for a number nobody has yet calibrated.
    """
    from datetime import date
    today = today or date.fromisoformat(R._today())
    out = []
    for name, d in sorted(_declarations(root).items()):
        rb = d.get("revalidate_by")
        if not rb:
            continue
        try:
            due = date.fromisoformat(str(rb))
        except ValueError:
            continue
        if due < today:
            out.append(f"{name}: revalidate_by {rb} has passed ({(today - due).days}d) - "
                       f"basis {d.get('evidence_basis')} must be re-established (R12.3)")
    return out


def refresh(root=None, today=None, dry_run=False) -> dict:
    """Apply the declarations to the RATIONALE items already on the register.

    ⚑ WHY THIS EXISTS (ISA-0346). `seed()` skips any constant whose alias is already on the
    register, so once an item was seeded the declarations file could never reach it again. The
    ledger was WRITE-ONCE: answering a question in the declarations changed nothing anybody read.
    That is FC-E - an absent execution reporting success - on the exact artefact built to record
    that a question had been answered. refresh() is the missing edge, and it is idempotent.
    """
    from datetime import date
    today = today or date.fromisoformat(R._today())
    errs = declaration_errors(root)
    if errs:
        raise ValueError("rationale ledger refused - the declarations file breaches its own "
                         "contract, and a ledger with holes is worse than none:\n  - "
                         + "\n  - ".join(errs))
    decl = _declarations(root)
    stale = {s.split(":")[0] for s in stale_revalidations(today, root)}
    closed, reopened, unchanged = [], [], []
    for it in R.read_all():
        if it.get("record_type") != "RATIONALE":
            continue
        name = (it.get("rationale") or {}).get("constant")
        d = decl.get(name)
        if not d:
            continue
        block = dict(it.get("rationale") or {})
        block.update({k: d.get(k) for k in ("who_set_it", "evidence_basis", "set_on",
                                            "last_validated", "revalidate_by",
                                            "what_would_falsify_it")})
        # ⚑ The rationale block is EXACTLY R12.3's six fields plus constant/home, and the schema
        # enforces that (additionalProperties: false). Supporting detail - effective N,
        # survivorship, the falsifier's current status, a corrected capital_link - is prose and
        # belongs in `narrative`, not smuggled into a structured block as ad-hoc keys. One shape
        # per record; a schema that quietly grows a key per author is FC-D wearing a dictionary.
        supporting = []
        for extra, label in (("source", "Source"),
                             ("evidence_n", "Effective N"),
                             ("survivorship", "Survivorship"),
                             ("evidence_basis_caveat", "Basis caveat"),
                             ("measured_consequence", "Measured consequence"),
                             ("falsifier_status", "Falsifier status"),
                             ("revalidate_basis", "Revalidation basis"),
                             ("capital_link_correction", "CORRECTION to capital_link"),
                             ("provenance_adjacency_defect", "Provenance adjacency")):
            if d.get(extra):
                supporting.append(f"**{label}.** {d[extra]}")
        complete = all(d.get(f) for f in REQUIRED_DECLARATION_FIELDS)
        want_open = (not complete) or (name in stale)
        target = "OPEN" if want_open else "CLOSED_NOT_A_DEFECT"
        narrative = ("Provenance RECOVERED from the documentary record, not invented (R7.5). "
                     + " ".join(supporting)) if supporting else it.get("narrative")
        if it.get("state") == target and (it.get("rationale") or {}) == block \
                and it.get("narrative") == narrative:
            unchanged.append(name)
            continue
        if dry_run:
            (reopened if want_open else closed).append(name)
            continue
        if target == "CLOSED_NOT_A_DEFECT":
            R.close(
                it["id"],
                state="CLOSED_NOT_A_DEFECT",
                rationale=block,
                narrative=narrative,
                corrective_action=(
                    f"Provenance for {name} RECOVERED from the documentary record and recorded in "
                    f"{DECLARATIONS}: who_set_it, evidence_basis ({d.get('evidence_basis')}), "
                    f"set_on, last_validated, revalidate_by ({d.get('revalidate_by')}) and "
                    "what_would_falsify_it. Nothing was invented (R7.5): where the setting decision "
                    "predates the record, set_on carries an explicit NOT_RECORDED with the first "
                    "attestation. Closure is TEMPORARY - stale_revalidations() re-opens the item "
                    "the day revalidate_by passes."),
                cause_systemic=(
                    "R12.3 was adopted after most of these constants were set, so provenance was "
                    "never a precondition of shipping one. The defence is not this backfill but "
                    "the assertion that now fails a build when a capital-gating constant carries "
                    "no complete declaration, and re-opens it when its revalidate date passes."),
                verification={
                    "test_id": "test_rationale_ledger_provenance.py",
                    "liveness_ref": "test_rationale_ledger_provenance.py::"
                                    "test_every_capital_gating_constant_has_complete_provenance"
                                    " (+ ::test_incomplete_declaration_is_refused, the negative "
                                    "control: strip who_set_it and the close must be refused; "
                                    "back-date revalidate_by and the item must re-open)",
                    "assertion_count": 0,
                    "green_on": R._today()},
                size_actual="XS")
            closed.append(name)
        else:
            item = dict(it)
            item["state"] = "OPEN"
            item["rationale"] = block
            item["narrative"] = (
                f"{name}: revalidate_by {d.get('revalidate_by')} has passed. The recorded basis "
                f"({d.get('evidence_basis')}) must be re-established before this closes again."
                if name in stale else item.get("narrative"))
            R.write(item, allow_update=True)
            reopened.append(name)
    return {"closed": sorted(closed), "reopened": sorted(reopened),
            "unchanged": sorted(unchanged), "stale": sorted(stale), "dry_run": dry_run}


def verify(today=None, root=None) -> list:
    """Everything the routine battery asserts about the ledger, in one call."""
    return declaration_errors(root) + coverage() + gaps() + stale_revalidations(today, root)


def gaps() -> list:
    """Capital-gating constants whose ledger entry is still NO_RECORDED_RATIONALE."""
    out = []
    for it in R.read_all():
        if it.get("record_type") != "RATIONALE":
            continue
        r = it.get("rationale") or {}
        if r.get("evidence_basis") in ("NO_RECORDED_RATIONALE", None):
            out.append(f"{r.get('constant')} ({r.get('home')}): {it['id']} - no recorded rationale (R12.3)")
    return sorted(out)


def coverage() -> list:
    """Declared capital-gating constants with NO ledger record at all.

    Different from gaps(): 'nobody wrote down why' and 'nobody even asked' are different
    failures, and collapsing them would hide the second (R2.10).
    """
    have = {(it.get("rationale") or {}).get("constant")
            for it in R.read_all() if it.get("record_type") == "RATIONALE"}
    return [f"{n}: declared capital-gating with no ledger record (R12.3)"
            for n in sorted(CAPITAL_GATING) if n not in have]


def selftest(verbose=True) -> int:
    import os, shutil, tempfile
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    tmp = Path(tempfile.mkdtemp(prefix="isa_ratl_"))
    (tmp / "scoring_config.py").write_text(
        "# WP-M (29-Jul-26): re-weighted after the momentum backtest, see D-24\n"
        "SOURCE_WEIGHTS = {'forward': 0.6}\n"
        "APS_FRESH_CAPITAL_BAR = 65.0\n"
        "ER_DEPLOY_FLOOR = 15.8   # derived from the anchor\n", encoding="utf-8")

    a = _assignments(tmp / "scoring_config.py")
    ok(set(a) == {"SOURCE_WEIGHTS", "APS_FRESH_CAPITAL_BAR", "ER_DEPLOY_FLOOR"},
       f"module-level assignments not found: {sorted(a)}")
    ok("29-Jul-26" in a["SOURCE_WEIGHTS"][1],
       "a comment block ABOVE the assignment must be read as provenance")

    saved = dict(CAPITAL_GATING)
    CAPITAL_GATING.clear()
    CAPITAL_GATING.update({
        "SOURCE_WEIGHTS": ("scoring_config.py", "selects SUMMARY"),
        "APS_FRESH_CAPITAL_BAR": ("scoring_config.py", "fresh capital eligibility"),
        "ER_DEPLOY_FLOOR": ("scoring_config.py", "deployment floor")})
    try:
        res = scan(tmp)
        ok(res["total"] == 3 and not res["unresolved"], f"scan: {res}")
        ok(res["constants"]["SOURCE_WEIGHTS"]["has_provenance"],
           "a dated comment naming a backtest counts as provenance")
        ok(not res["constants"]["APS_FRESH_CAPITAL_BAR"]["has_provenance"],
           "a bare number has NO provenance and must be reported as such")
        ok(not res["constants"]["ER_DEPLOY_FLOOR"]["has_provenance"],
           "an undated, unsourced comment is not provenance")

        CAPITAL_GATING["NOT_THERE"] = ("scoring_config.py", "phantom")
        raised = False
        try:
            seed(tmp, dry_run=True)
        except ValueError as e:
            raised = "could not be located" in str(e)
        ok(raised, "a declared constant that is not assigned must REFUSE the whole seed (R4.9)")
        del CAPITAL_GATING["NOT_THERE"]

        store = tmp / "state"
        store.mkdir()
        schema = R.store_dir() / R.SCHEMA_FILE
        if not schema.exists():
            schema = Path(R.__file__).parent / R.SCHEMA_FILE
        (store / R.SCHEMA_FILE).write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
        old = os.environ.get("ISA_REGISTER_STORE")
        os.environ["ISA_REGISTER_STORE"] = str(store)
        R._schema_cache.clear()
        try:
            ok(len(coverage()) == 3, "before seeding, every declared constant is uncovered")
            r = seed(tmp)
            ok(r["seeded"] == 3, f"seed wrote {r['seeded']}")
            ok(set(r["no_provenance_in_code"]) == {"APS_FRESH_CAPITAL_BAR", "ER_DEPLOY_FLOOR"},
               f"code-comment provenance mis-identified: {r['no_provenance_in_code']}")
            ok(not coverage(), "after seeding, coverage is clean")
            ok(len(gaps()) == 2,
               f"two constants must remain NO_RECORDED_RATIONALE and stay OPEN, got {gaps()}")
            ok(seed(tmp)["seeded"] == 0, "seeding twice must not duplicate (R7.6)")
            recs = [i for i in R.read_all() if i["record_type"] == "RATIONALE"]
            ok(all(i.get("rationale", {}).get("constant") for i in recs),
               "every RATIONALE record carries its rationale block (schema, R12.3)")
            ok(all(i["capital_link"] for i in recs),
               "every RATIONALE record states WHY the constant gates capital (R16.1)")
            ok(all("plausible justification" not in (i.get("rationale", {}).get("what_would_falsify_it") or "")
                   for i in recs), "the ledger never invents a justification")
        finally:
            if old:
                os.environ["ISA_REGISTER_STORE"] = old
            else:
                os.environ.pop("ISA_REGISTER_STORE", None)
            R._schema_cache.clear()
    finally:
        CAPITAL_GATING.clear()
        CAPITAL_GATING.update(saved)
    shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print(f"isa_rationale_ledger selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="R12.3 Rationale Ledger")
    ap.add_argument("--root", default=None)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--gaps", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.scan:
        r = scan(a.root)
        print(f"{r['with_provenance']}/{r['total']} capital-gating constants carry provenance")
        for k, v in sorted(r["constants"].items()):
            mark = ",".join(v["provenance_marks"]) or "NO RECORDED RATIONALE"
            print(f"  {k:<28} {v['module']}:{v['line']:<5} {mark}")
        for u in r["unresolved"]:
            print(f"  UNRESOLVED  {u}")
        return 0
    if a.seed:
        print(json.dumps(seed(a.root, a.dry_run), indent=2))
        return 0
    if a.refresh:
        print(json.dumps(refresh(a.root, dry_run=a.dry_run), indent=2))
        return 0
    if a.verify:
        v = verify(root=a.root)
        print("\n".join(v) if v else "rationale ledger: clean - every capital-gating constant "
              "carries a complete, in-date declaration")
        return 1 if v else 0
    if a.gaps:
        g = gaps() + coverage()
        print("\n".join(g) if g else "every capital-gating constant has a recorded rationale")
        return 1 if g else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
