#!/usr/bin/env python3
"""
rule_audit.py — §17 of ISA_Engineering_Rules.md, made a number instead of a sentence.

⚑⚑ WHY (ISA-0467). §17 says in as many words: *"A standard nobody can prove ran is FC-E — an
absent execution reporting success."* It then declares a classification of every rule into
ASSERTED / PARTIAL / JUDGEMENT and an honest ratio of "roughly 40/25/35" — and the file that
was supposed to compute it did not exist, so the ratio itself was prose. §17's own adoption
condition (Raj, 09-Aug-2026) is that the rules are worth executing ONLY WITH §15 and §17.

⚑ ONE HOME (R4.4). The classification is NOT restated here. It is PARSED from §17's own table
in ISA_Engineering_Rules.md. Copying the rule lists into this file would create the second home
that §17 exists to prevent — and would let the code and the standard drift while both looked
authoritative.

⚑⚑ AND THE MEASUREMENT THAT MATTERS IS NOT THE COUNT. A rule is ASSERTED when "a named check
fails when the rule is broken". Counting the rules the table CLAIMS are asserted measures the
table, not the framework. So this module reports TWO ratios:

    CLAIMED     — what §17's table says
    TRACEABLE   — of the rules claimed ASSERTED, how many are actually NAMED by a check in the
                  tree (consistency_check, the test suites, the enforcement modules)

A rule claimed ASSERTED that no check names is reported as ASSERTED_UNTRACEABLE. That gap is
the honest finding; publishing only the claimed ratio would be the same failure one level up.
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STANDARD = os.path.join(HERE, "ISA_Engineering_Rules.md")
CLASSES = ("ASSERTED", "PARTIAL", "JUDGEMENT")

# Where a rule id may legitimately be NAMED by a check. Scoped deliberately (ISA-0493: the
# scope must be the claim) — a rule id appearing in a build record or a spec is documentation,
# not enforcement, and counting it would inflate the traceable ratio with prose.
CHECK_SURFACES = (
    "consistency_check.py", "framework_integrity.py", "isa_register.py",
    "isa_register_render.py", "isa_register_export.py", "framework_atlas.py",
    "register_callsites.py", "isa_register_metrics.py",
)
# ⚑⚑ AN OBSERVER MAY NOT MEASURE ITSELF (ISA-0382, and ISA-0526 one level down). `rule_audit.py`
#    is NOT in the corpus above. With it included, this module's own negative-control labels —
#    strings inside `_selftest` that NAME the rules being reported as unenforced — counted as
#    enforcement of those rules, and R8.2 flipped from UNENFORCED to enforced-by-rule_audit.
#    The structural fix (comments and docstrings excluded) was right and still insufficient:
#    a test's descriptive label is prose too when the test is the auditor's own. The exclusion
#    is asserted by a control below, not assumed.
SELF = "rule_audit.py"
CHECK_DIRS = ("tests_jul2026",)

_RULE_RE = re.compile(r"\bR(\d+)\.(\d+)\b")
_SECTION_RE = re.compile(r"§\s*(\d+)")


class StandardUnreadable(RuntimeError):
    """Raised rather than returning a zero ratio. A missing standard is not a clean audit."""


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _expand(cell: str) -> list:
    """Expand one table cell's rule list. Handles 'R4.1–R4.13' ranges, bare ids, and '§4'
    section references (which mean every rule in that section, per §17's own shorthand)."""
    out, seen = [], set()
    # ranges first: R4.1-R4.13 / R4.1–R4.13
    for m in re.finditer(r"R(\d+)\.(\d+)\s*[–-]\s*R(\d+)\.(\d+)", cell):
        a_sec, a_n, b_sec, b_n = (int(x) for x in m.groups())
        if a_sec == b_sec:
            for n in range(a_n, b_n + 1):
                rid = "R%d.%d" % (a_sec, n)
                if rid not in seen:
                    seen.add(rid); out.append(rid)
    cell_wo = re.sub(r"R\d+\.\d+\s*[–-]\s*R\d+\.\d+", " ", cell)
    for m in _RULE_RE.finditer(cell_wo):
        rid = "R%s.%s" % m.groups()
        if rid not in seen:
            seen.add(rid); out.append(rid)
    return out


def classification(standard_text: str | None = None) -> dict:
    """{class: [rule ids]} parsed from §17's table. RAISES if the section cannot be found —
    an audit that silently reports 0 rules is worse than no audit (R2.10)."""
    text = standard_text if standard_text is not None else _read(STANDARD)
    if not text:
        raise StandardUnreadable("ISA_Engineering_Rules.md not readable at %s" % STANDARD)
    block = re.search(r"^## 17\..*?(?=^## \d+\.)", text, re.S | re.M)
    if not block:
        raise StandardUnreadable("§17 not found in the standard — the audit is BLIND, not clean")
    out = {c: [] for c in CLASSES}
    for line in block.group(0).split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        label = cells[0].replace("*", "").strip().upper()
        if label in CLASSES:
            out[label] = _expand(cells[2])
    empty = [c for c in CLASSES if not out[c]]
    if empty:
        raise StandardUnreadable(
            "§17's table parsed with no rules for %s — the table shape changed and this parser "
            "did not. BLIND, not clean." % ", ".join(empty))
    return out


def _check_corpus() -> dict:
    """{label: source text} of every surface where a rule may be NAMED by a check."""
    corpus = {}
    for fn in CHECK_SURFACES:
        t = _read(os.path.join(HERE, fn))
        if t:
            corpus[fn] = t
    for d in CHECK_DIRS:
        p = os.path.join(HERE, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if fn.endswith(".py"):
                t = _read(os.path.join(p, fn))
                if t:
                    corpus["%s/%s" % (d, fn)] = t
    return corpus


_STRINGS_CACHE = {}


def _enforcement_strings(text: str) -> list:
    """Every string literal that lives INSIDE a function body, excluding each function's own
    docstring. Cached on the source text — the audit asks 34 rules × ~55 files, and re-parsing
    each file per rule turned a 2-second report into a 3-minute one."""
    key = hash(text)
    hit = _STRINGS_CACHE.get(key)
    if hit is not None:
        return hit
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        _STRINGS_CACHE[key] = out
        return out
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(fn, clean=False)
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if doc is not None and node.value == doc:
                    continue
                out.append(node.value)
    _STRINGS_CACHE[key] = out
    return out


def _named_by_enforcement(text: str, rid: str) -> bool:
    """Does this source NAME `rid` inside executable enforcement, rather than talk ABOUT it?

    ⚑⚑ ISA-0446's CLASS, AND IT FIRED ON MY OWN PROSE WITHIN THE HOUR (ISA-0526). The first
    version of this function grepped the file text. On 02-Sep-2026 it correctly reported R8.2
    as ASSERTED_UNTRACEABLE — and then, once `isa_register_metrics.py` was written with a
    comment EXPLAINING that R8.2 is unenforced, the same grep found the string and reported
    R8.2 traceable. **A sentence saying a rule is not enforced became the evidence that it is.**

    ⚑ So the test is structural, not textual. A rule id counts as NAMED only when it appears in
    a STRING LITERAL INSIDE A FUNCTION BODY — an error message, an assertion message, a
    refusal — and NOT when it appears in:
      · a comment (comments are not in the AST at all, which is the point of parsing);
      · a module-level docstring or a module-level assignment (prose and configuration);
      · a function's OWN docstring (documentation about the rule, not enforcement of it).
    """
    pat = re.compile(r"(?<![\w.])" + re.escape(rid) + r"(?![\w.])")
    return any(pat.search(lit) for lit in _enforcement_strings(text))


def traceability(cls: dict | None = None, corpus: dict | None = None) -> dict:
    """For every rule claimed ASSERTED, which check surfaces ENFORCE it (and, separately,
    which merely mention it). Returns {rid: {"enforced_in": [...], "mentioned_in": [...]}}."""
    cls = cls if cls is not None else classification()
    corpus = corpus if corpus is not None else _check_corpus()
    out = {}
    for rid in cls["ASSERTED"]:
        pat = re.compile(r"(?<![\w.])" + re.escape(rid) + r"(?![\w.])")
        enforced = sorted(l for l, t in corpus.items() if _named_by_enforcement(t, rid))
        mentioned = sorted(l for l, t in corpus.items()
                           if pat.search(t) and l not in enforced)
        out[rid] = {"enforced_in": enforced, "mentioned_in": mentioned}
    return out


def audit(standard_text: str | None = None, corpus: dict | None = None) -> dict:
    cls = classification(standard_text)
    total = sum(len(v) for v in cls.values())
    trace = traceability(cls, corpus)
    traceable = [r for r, v in trace.items() if v["enforced_in"]]
    untraceable = [r for r, v in trace.items() if not v["enforced_in"]]
    prose_only = {r: v["mentioned_in"] for r, v in trace.items()
                  if not v["enforced_in"] and v["mentioned_in"]}
    doc = {
        "as_of": datetime.date.today().isoformat(),
        "source": os.path.basename(STANDARD),
        "one_home": "the classification is PARSED from §17, never restated here (R4.4)",
        "total_rules_classified": total,
        "claimed": {c: {"n": len(cls[c]),
                        "pct": round(100.0 * len(cls[c]) / total, 1) if total else None,
                        "rules": cls[c]} for c in CLASSES},
        "traceable": {
            "basis": ("of the rules §17 CLAIMS are ASSERTED, how many are NAMED by a check in "
                      "consistency_check / framework_integrity / the register modules / the "
                      "test suites. Prose in a spec or a build record does not count."),
            "n_claimed_asserted": len(cls["ASSERTED"]),
            "n_traceable": len(traceable),
            "pct_of_claimed": (round(100.0 * len(traceable) / len(cls["ASSERTED"]), 1)
                               if cls["ASSERTED"] else None),
            "pct_of_all_rules": (round(100.0 * len(traceable) / total, 1) if total else None),
            "asserted_untraceable": untraceable,
            "enforced_by": {r: trace[r]["enforced_in"] for r in cls["ASSERTED"]},
            "mentioned_only": prose_only,
            "prose_is_not_enforcement": (
                "a rule id in a comment, a module docstring or a function's own docstring is "
                "prose ABOUT the rule. Only a string literal inside a function body - an error "
                "message, an assertion, a refusal - counts as enforcement (ISA-0526)."),
        },
        "reading": None,
    }
    doc["reading"] = (
        "§17 CLAIMS %d of %d rules ASSERTED (%.1f%%). %d of those %d are actually NAMED by a "
        "check in the tree, so the TRACEABLE asserted ratio is %.1f%% of all rules. %s"
        % (len(cls["ASSERTED"]), total, doc["claimed"]["ASSERTED"]["pct"],
           len(traceable), len(cls["ASSERTED"]), doc["traceable"]["pct_of_all_rules"] or 0.0,
           ("Every claimed-ASSERTED rule is traceable to a check."
            if not untraceable else
            "%d rule(s) are claimed ASSERTED with NO check ENFORCING them: %s. A rule whose check "
            "cannot be found is a JUDGEMENT rule wearing an ASSERTED label.%s"
            % (len(untraceable), ", ".join(untraceable),
               ("" if not prose_only else
                " %d of them are MENTIONED in prose without being enforced (%s) - a sentence "
                "saying a rule is unenforced is not evidence that it is enforced (ISA-0526)."
                % (len(prose_only), ", ".join(sorted(prose_only))))))))
    return doc


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (("   " + str(detail)) if not cond else ""))
        if not cond:
            fails.append(name)

    # ── the parser reproduces §17 rather than restating it ────────────────────────────────
    cls = classification()
    ok("§17 parses into all three classes with rules in each",
       all(cls[c] for c in CLASSES), {c: len(cls[c]) for c in CLASSES})
    ok("the R4.1–R4.13 range is EXPANDED, not counted as one rule",
       "R4.7" in cls["ASSERTED"] and "R4.13" in cls["ASSERTED"], cls["ASSERTED"][:6])
    ok("no rule is classified twice - the classes partition the rules",
       len(set(cls["ASSERTED"]) & set(cls["PARTIAL"])) == 0
       and len(set(cls["ASSERTED"]) & set(cls["JUDGEMENT"])) == 0
       and len(set(cls["PARTIAL"]) & set(cls["JUDGEMENT"])) == 0)

    # ── NEGATIVE CONTROL: a standard whose §17 is gone must RAISE, never report 0% cleanly ─
    broken = _read(STANDARD).replace("## 17. RULE AUDIT", "## 17x. RULE AUDIT", 1)
    ok("NEGATIVE CONTROL: the mutation was applied to the real standard's text",
       broken != _read(STANDARD))
    raised = False
    try:
        classification(broken)
    except StandardUnreadable:
        raised = True
    ok("NEGATIVE CONTROL: a missing §17 RAISES StandardUnreadable - a zero ratio would read "
       "as 'no rules are asserted', which is a fact, not an error", raised)

    # ── NEGATIVE CONTROL: a table whose shape changed must be BLIND, not silently empty ────
    reshaped = re.sub(r"\|\s*\*\*ASSERTED\*\*\s*\|", "| **ASSERTED_v2** |", _read(STANDARD), count=1)
    ok("NEGATIVE CONTROL: the row-label mutation was applied", reshaped != _read(STANDARD))
    raised2 = False
    try:
        classification(reshaped)
    except StandardUnreadable:
        raised2 = True
    ok("NEGATIVE CONTROL: a renamed class row RAISES rather than reporting ASSERTED = 0",
       raised2)

    # ── the traceability measurement is real, and BOTH verdicts are exercised ──────────────
    doc = audit()
    ok("the audit reports a CLAIMED ratio and a TRACEABLE one, and they are different numbers "
       "measured different ways",
       doc["claimed"]["ASSERTED"]["pct"] is not None
       and doc["traceable"]["pct_of_all_rules"] is not None)
    fake = {"nothing.py": "# a corpus that names no rule at all\n"}
    empty_trace = audit(corpus=fake)
    ok("NEGATIVE CONTROL: against a corpus naming NO rule, every claimed-ASSERTED rule is "
       "reported UNTRACEABLE - so a non-empty result above means 'found', not 'the scanner is "
       "broken'",
       len(empty_trace["traceable"]["asserted_untraceable"])
       == empty_trace["traceable"]["n_claimed_asserted"]
       and empty_trace["traceable"]["n_traceable"] == 0)
    ok("...and against the real tree at least one rule IS traceable, so the scanner finds "
       "things too", doc["traceable"]["n_traceable"] > 0, doc["traceable"]["n_traceable"])
    ok("⚑ prose does not count as enforcement: no build record or build spec is in the corpus",
       not any(("BuildRecord" in k or "BuildSpec" in k) for k in _check_corpus()))

    # ── ISA-0526 — PROSE ABOUT A RULE MUST NOT COUNT AS ENFORCEMENT OF IT ─────────────────
    # ⚑⚑ This control exists because the FIRST version of this module failed it. It grepped
    #    the file text, correctly reported R8.2 untraceable, and then went green the moment a
    #    COMMENT was written explaining that R8.2 is unenforced. ISA-0446's class, within the
    #    hour, in the module built to measure enforcement.
    _comment_only = ("# R9.9 is unenforced and this comment says so\n"
                     "def pair_x():\n"
                     "    return []\n")
    ok("NEGATIVE CONTROL: a rule id in a COMMENT is NOT enforcement - comments are not in the "
       "AST, which is the point of parsing rather than grepping",
       not _named_by_enforcement(_comment_only, "R9.9"))
    _moduledoc_only = ('"""This module is about R9.9 and does not enforce it."""\n'
                       "def pair_x():\n"
                       "    return []\n")
    ok("NEGATIVE CONTROL: a rule id in a MODULE DOCSTRING is not enforcement",
       not _named_by_enforcement(_moduledoc_only, "R9.9"))
    _fndoc_only = ("def pair_x():\n"
                   '    """Nothing here checks R9.9."""\n'
                   "    return []\n")
    ok("NEGATIVE CONTROL: a rule id in a FUNCTION'S OWN DOCSTRING is documentation about the "
       "rule, not enforcement of it",
       not _named_by_enforcement(_fndoc_only, "R9.9"))
    _assign_only = 'NOTES = {"R9.9": "blocked, see the register"}\n'
    ok("NEGATIVE CONTROL: a rule id in a MODULE-LEVEL ASSIGNMENT is configuration or prose - "
       "this is the exact shape that made R8.2 read as traceable on 02-Sep-2026",
       not _named_by_enforcement(_assign_only, "R9.9"))
    _enforced = ("def pair_x(rows):\n"
                 "    if not rows:\n"
                 '        return ["R9.9: refused on an empty population"]\n'
                 "    return []\n")
    ok("⚑ POSITIVE CONTROL: a rule id in a string literal INSIDE a function body - an error "
       "message a check actually emits - DOES count, so the negatives above mean 'not "
       "enforcement', not 'the scanner never finds anything'",
       _named_by_enforcement(_enforced, "R9.9"))
    ok("⚑ SELF-EXCLUSION (ISA-0382): rule_audit.py is not in its own corpus - with it in, this "
       "selftest's own control labels counted as enforcement of the rules they name, and R8.2 "
       "flipped to enforced-by-rule_audit",
       SELF not in _check_corpus() and SELF not in CHECK_SURFACES)
    _self_src = _read(os.path.join(HERE, SELF))
    ok("⚑ CONTROL PROVING THE EXCLUSION IS LOAD-BEARING: this file DOES contain R8.2 inside a "
       "function-body string, so without the exclusion it WOULD have been counted - the "
       "exclusion is doing work, not decorating",
       _named_by_enforcement(_self_src, "R8.2"))
    ok("...and with rule_audit.py forced into a corpus, R8.2 reads as enforced - the failure "
       "mode is reproduced on demand rather than described",
       _named_by_enforcement(_self_src, "R8.2")
       and bool(audit(corpus={SELF: _self_src})["traceable"]["enforced_by"].get("R8.2")))
    _both = trace = traceability()
    ok("the audit separates ENFORCED_IN from MENTIONED_IN so a prose-only rule is visible "
       "rather than absent", all(set(v) == {"enforced_in", "mentioned_in"} for v in _both.values()))

    print("\nrule_audit._selftest: %d assertion(s) failed" % len(fails))
    return len(fails)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    doc = audit()
    if "--json" in argv:
        print(json.dumps(doc, indent=2))
    else:
        print(doc["reading"])
        print("\nCLAIMED: " + " · ".join(
            "%s %d (%.1f%%)" % (c, doc["claimed"][c]["n"], doc["claimed"][c]["pct"])
            for c in CLASSES))
        if doc["traceable"]["asserted_untraceable"]:
            print("\nASSERTED_UNTRACEABLE (%d): %s"
                  % (len(doc["traceable"]["asserted_untraceable"]),
                     ", ".join(doc["traceable"]["asserted_untraceable"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
