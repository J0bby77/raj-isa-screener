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
_PROVENANCE_MARKS = (
    (re.compile(_DATE), "dated"),
    (re.compile(r"\b(backtest|backtested|study|measured|calibrat)\w*", re.I), "evidence named"),
    (re.compile(r"\b(D-\d+|C\d|H\d|WP-[A-Z0-9]+|ISA-\d{4})\b"), "item referenced"),
)


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
        marks = [label for rx, label in _PROVENANCE_MARKS if rx.search(comment)]
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
    if a.gaps:
        g = gaps() + coverage()
        print("\n".join(g) if g else "every capital-gating constant has a recorded rationale")
        return 1 if g else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
