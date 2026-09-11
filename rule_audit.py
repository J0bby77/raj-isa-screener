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


def defined_rules(text: str) -> dict:
    """{section:int -> [rule ids]} for every rule the standard's BODY defines.

    ⚑⚑ ISA-0624. Until 09-Sep-2026 nothing in the tree compared the rules the standard DEFINES
    to the rules §17 CLASSIFIES, so a rule could be written into the body and never appear in
    the only instrument that reads the standard. §17's own R15.4 item 3 — "a new rule ships
    unclassified" fails the build — had nothing to fire on. A rule is DEFINED where the body
    opens a paragraph with `**Rn.m`; the §17 table's own rows start with `|` and are excluded
    by the anchor, so the table cannot define the rules it is measuring.
    """
    out: dict = {}
    for m in re.finditer(r"^\*\*R(\d+)\.(\d+)\b", text, re.M):
        sec, n = int(m.group(1)), int(m.group(2))
        rid = "R%d.%d" % (sec, n)
        bucket = out.setdefault(sec, [])
        if rid not in bucket:
            bucket.append(rid)
    for sec in out:
        out[sec].sort(key=lambda r: int(r.split(".")[1]))
    return out


def _expand(cell: str, defined_by_section: dict = None) -> list:
    """Expand one table cell's rule list. Handles 'R4.1–R4.13' ranges, bare ids, and '§4'
    section references.

    ⚑ A SECTION REFERENCE IS EXPANDED ONLY WHEN THE CELL NAMES NO RULE OF THAT SECTION.
    This module's docstring claimed §-expansion from 02-Sep-2026 and did not implement it
    (ISA-0624), and the naive implementation is wrong in a way that matters: §17's ASSERTED row
    reads "§4 (R4.1–R4.13)", where the `§4` is a heading for the explicit range beside it.
    Expanding it would silently promote R4.14–R4.16 — three rules the standard deliberately
    classifies JUDGEMENT — into ASSERTED. So `§N` expands only where it stands ALONE, which is
    exactly the "§18 · §19 · §20" shape that motivated the rule.

    ⚑ AND AN EMPTY EXPANSION RAISES. "§18 expanded to nothing" and "§18 has no rules" must not
    render the same (R2.10); a section reference that yields no rules is the silent-partial
    shape this file exists to measure (R4.9, FC-I).
    """
    out, seen = [], set()

    def _add(rid):
        if rid not in seen:
            seen.add(rid); out.append(rid)

    # ranges first: R4.1-R4.13 / R4.1–R4.13
    for m in re.finditer(r"R(\d+)\.(\d+)\s*[–-]\s*R(\d+)\.(\d+)", cell):
        a_sec, a_n, b_sec, b_n = (int(x) for x in m.groups())
        if a_sec == b_sec:
            for n in range(a_n, b_n + 1):
                _add("R%d.%d" % (a_sec, n))
    cell_wo = re.sub(r"R\d+\.\d+\s*[–-]\s*R\d+\.\d+", " ", cell)
    for m in _RULE_RE.finditer(cell_wo):
        _add("R%s.%s" % m.groups())

    if defined_by_section is not None:
        sections_named = {int(m.group(1)) for m in _SECTION_RE.finditer(cell)}
        for sec in sorted(sections_named):
            if any(int(r[1:].split(".")[0]) == sec for r in out):
                continue                      # decorative heading beside an explicit list
            rules = defined_by_section.get(sec)
            if not rules:
                raise StandardUnreadable(
                    "§17 references §%d as a whole and the standard's body defines no rule in "
                    "§%d — the reference expands to nothing, which reads as 'that section has "
                    "no rules' rather than 'the parser could not find them'. BLIND, not clean "
                    "(ISA-0624)." % (sec, sec))
            for rid in rules:
                _add(rid)
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
    defined = defined_rules(text)
    out = {c: [] for c in CLASSES}
    for line in block.group(0).split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        label = cells[0].replace("*", "").strip().upper()
        if label in CLASSES:
            out[label] = _expand(cells[2], defined)
    empty = [c for c in CLASSES if not out[c]]
    if empty:
        raise StandardUnreadable(
            "§17's table parsed with no rules for %s — the table shape changed and this parser "
            "did not. BLIND, not clean." % ", ".join(empty))
    return out


def unclassified(standard_text: str = None) -> list:
    """Rules the body DEFINES that §17 does not classify (ISA-0624).

    R15.4 item 3 says a new rule shipping unclassified fails the build. This is the function
    that makes that sentence fireable; `consistency_check.pair_rules_all_classified` is the
    check that fires."""
    text = standard_text if standard_text is not None else _read(STANDARD)
    if not text:
        raise StandardUnreadable("ISA_Engineering_Rules.md not readable at %s" % STANDARD)
    defined = defined_rules(text)
    flat = [r for sec in sorted(defined) for r in defined[sec]]
    classified = set(sum(classification(text).values(), []))
    return [r for r in flat if r not in classified]


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


_CHECK_FN_RE = re.compile(
    r"^(pair_|test_|check|verify|validate|assert|selftest$|_selftest$|q\d_|.*_report$|"
    r".*_regressions$|.*_gaps$|.*_equivalence$|.*_control$|.*_audit$|unclassified$)")
_ERR_NAME_RE = re.compile(
    r"^_?(errs?|errors?|fails?|failures?|findings?|problems?|issues?|warns?|warnings?|"
    r"mismatch(es)?|drift|breaches|violations?|refusals?)$")
_FAILURE_CALLS = frozenset((
    "ok", "fail", "failed", "refuse", "raise_", "assert_", "expect", "require",
    "_fail", "report_failure", "add_error", "add_finding", "flag"))
_DIAG_KEYS = frozenset(("note", "detail", "message", "msg", "reason",
                        "error", "finding", "why", "verdict"))
_STRICT_CACHE = {}


def _failing_strings(text: str) -> list:
    """Every string literal that can reach a FAILURE SIGNAL, not merely a function body.

    ⚑⚑ ISA-0627. `_enforcement_strings` below counts any string literal inside a function body.
    That was the right correction to grepping the file text (ISA-0526) and it is still too
    generous in one specific, measurable way: a rule id inside a sentence a RENDERER prints is
    a string literal inside a function body, so documentation delivered at runtime scores as
    enforcement. Measured 09-Sep-2026: R13.2's only hit in the whole tree is
    `isa_register_render` emitting the sentence "Under R13.2 analysis precedes design", and
    R7.5's are that renderer plus a selftest's own descriptive label. Neither is a check that
    fails when the rule is broken, which is what §17 says ASSERTED means.

    ⚑ So this is the STRICTER test, and it is structural rather than textual. A literal counts
    when it can actually carry a failure:
      · it sits inside a `raise` or an `assert`;
      · it is appended, extended or `+=`'d onto a name in the error vocabulary
        (`errs`, `errors`, `findings`, `failures`, ...);
      · it is an argument to a call in the failure vocabulary (`ok(...)`, `fail(...)`, ...);
      · it is returned — directly or inside a list/tuple/set — from a function whose name is in
        the check vocabulary (`pair_*`, `check*`, `verify*`, `q1_*`, `*_report`, `_selftest`).
    Both measurements are published. The loose one is kept because dropping it would hide the
    delta, and the delta is the finding (R6.2 — disagreement is published, never blended).
    """
    key = ("strict", hash(text))
    hit = _STRICT_CACHE.get(key)
    if hit is not None:
        return hit
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        _STRICT_CACHE[key] = out
        return out

    def _literals(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield n.value

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(fn, clean=False)
        checkish = bool(_CHECK_FN_RE.match(fn.name))
        for node in ast.walk(fn):
            carriers = []
            if isinstance(node, (ast.Raise, ast.Assert)):
                carriers.append(node)
            elif isinstance(node, ast.Call):
                f = node.func
                name = (f.id if isinstance(f, ast.Name) else
                        f.attr if isinstance(f, ast.Attribute) else None)
                if name and (name in _FAILURE_CALLS or _CHECK_FN_RE.match(name)):
                    carriers.append(node)          # check("...R4.11..."), ok(...), pair_x(...)
                elif name in ("append", "extend") and isinstance(f, ast.Attribute):
                    tgt = f.value.id if isinstance(f.value, ast.Name) else ""
                    if _ERR_NAME_RE.match(tgt):
                        carriers.extend(node.args)
                    else:
                        # ⚑ THE FINDING-RECORD SHAPE. `duplicate_orchestration.append({... "note":
                        #   "...(R4.5)"})` in framework_atlas is a diagnostic emitted into a
                        #   findings list whose name is the finding's subject, not the word
                        #   "errors". A dict carrying a diagnostic key IS the failure signal, and
                        #   excluding it would report the Atlas's own duplicate-orchestration
                        #   finding as prose (measured 09-Sep-2026: this is the whole of R4.5's
                        #   enforcement in the tree).
                        for a in node.args:
                            if isinstance(a, ast.Dict) and any(
                                    isinstance(k, ast.Constant) and k.value in _DIAG_KEYS
                                    for k in a.keys):
                                carriers.append(a)
            elif (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
                  and _ERR_NAME_RE.match(node.target.id)):
                carriers.append(node.value)
            elif isinstance(node, ast.Return) and checkish and node.value is not None:
                carriers.append(node.value)
            for c in carriers:
                for lit in _literals(c):
                    if doc is not None and lit == doc:
                        continue
                    out.append(lit)
    _STRICT_CACHE[key] = out
    return out


def _named_by_failing_check(text: str, rid: str) -> bool:
    """Does this source name `rid` on a path that can EMIT A FAILURE? (§17's actual definition.)"""
    pat = re.compile(r"(?<![\w.])" + re.escape(rid) + r"(?![\w.])")
    return any(pat.search(lit) for lit in _failing_strings(text))


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
        failing = sorted(l for l, t in corpus.items() if _named_by_failing_check(t, rid))
        enforced = sorted(l for l, t in corpus.items() if _named_by_enforcement(t, rid))
        mentioned = sorted(l for l, t in corpus.items()
                           if pat.search(t) and l not in enforced)
        out[rid] = {"fails_on_break_in": failing,
                    "enforced_in": enforced, "mentioned_in": mentioned}
    return out


def audit(standard_text: str | None = None, corpus: dict | None = None) -> dict:
    cls = classification(standard_text)
    total = sum(len(v) for v in cls.values())
    trace = traceability(cls, corpus)
    strict = [r for r, v in trace.items() if v["fails_on_break_in"]]
    loose = [r for r, v in trace.items() if v["enforced_in"]]
    untraceable = [r for r, v in trace.items() if not v["fails_on_break_in"]]
    soft_only = sorted(set(loose) - set(strict))
    prose_only = {r: trace[r]["mentioned_in"] for r in untraceable if trace[r]["mentioned_in"]}
    text = standard_text if standard_text is not None else _read(STANDARD)
    unclass = unclassified(text)
    defined = defined_rules(text)
    n_defined = sum(len(v) for v in defined.values())
    doc = {
        "as_of": datetime.date.today().isoformat(),
        "source": os.path.basename(STANDARD),
        "one_home": "the classification is PARSED from §17, never restated here (R4.4)",
        "total_rules_classified": total,
        # ── ISA-0624: what §17 does NOT cover is now reported, not merely absent ──────────
        "coverage": {
            "basis": ("R15.4 item 3 says a new rule shipping unclassified fails the build. Until "
                      "09-Sep-2026 nothing compared the rules the BODY defines to the rules §17 "
                      "classifies, so that sentence had nothing to fire on."),
            "rules_defined_in_body": n_defined,
            "rules_classified_in_S17": total,
            "unclassified": unclass,
            "n_unclassified": len(unclass),
            "complete": not unclass,
        },
        "claimed": {c: {"n": len(cls[c]),
                        "pct": round(100.0 * len(cls[c]) / total, 1) if total else None,
                        "rules": cls[c]} for c in CLASSES},
        "traceable": {
            "basis": ("of the rules §17 CLAIMS are ASSERTED, how many are named on a path that "
                      "can EMIT A FAILURE - a raise, an assert, a check call, an appended "
                      "finding, or a returned diagnostic - in consistency_check / "
                      "framework_integrity / the register modules / the test suites. Prose in a "
                      "spec or a build record does not count, and neither does a sentence a "
                      "RENDERER prints (ISA-0627)."),
            "n_claimed_asserted": len(cls["ASSERTED"]),
            "n_traceable": len(strict),
            "pct_of_claimed": (round(100.0 * len(strict) / len(cls["ASSERTED"]), 1)
                               if cls["ASSERTED"] else None),
            "pct_of_all_rules": (round(100.0 * len(strict) / total, 1) if total else None),
            "asserted_untraceable": untraceable,
            "enforced_by": {r: trace[r]["fails_on_break_in"] for r in cls["ASSERTED"]},
            "mentioned_only": prose_only,
            # ⚑ BOTH MEASUREMENTS ARE PUBLISHED, NEVER BLENDED (R6.2). The delta is the finding.
            "n_traceable_loose": len(loose),
            "loose_but_not_failing": {r: trace[r]["enforced_in"] for r in soft_only},
            "loose_vs_strict_note": (
                "a rule in `loose_but_not_failing` is NAMED inside a function body but on no path "
                "that can fail: measured 09-Sep-2026 these were R5.7 (a print in run_tests.py "
                "saying red suites are 'report-only' - which is the rule NOT being enforced, "
                "stated out loud) and R7.1 (a banner the register renderers print into the view "
                "they generate). Documentation delivered at runtime is still documentation."),
            "prose_is_not_enforcement": (
                "a rule id in a comment, a module docstring or a function's own docstring is "
                "prose ABOUT the rule (ISA-0526). A rule id in a string a renderer prints is "
                "prose DELIVERED BY the code (ISA-0627). Only a literal that can reach a failure "
                "signal counts."),
        },
        "reading": None,
    }
    doc["reading"] = (
        "§17 CLAIMS %d of %d rules ASSERTED (%.1f%%). %d of those %d are named by a check that "
        "can FAIL, so the TRACEABLE asserted ratio is %.1f%% of all rules. %s %s"
        % (len(cls["ASSERTED"]), total, doc["claimed"]["ASSERTED"]["pct"],
           len(strict), len(cls["ASSERTED"]), doc["traceable"]["pct_of_all_rules"] or 0.0,
           ("Every claimed-ASSERTED rule is traceable to a failing check."
            if not untraceable else
            "%d rule(s) are claimed ASSERTED with NO check that FAILS when they are broken: %s. "
            "A rule whose check cannot be found is a JUDGEMENT rule wearing an ASSERTED label.%s"
            % (len(untraceable), ", ".join(untraceable),
               ("" if not prose_only else
                " %d of them are MENTIONED in prose without being enforced (%s)."
                % (len(prose_only), ", ".join(sorted(prose_only)))))),
           ("§17 classifies every rule the body defines."
            if not unclass else
            "⚑ §17 does NOT classify %d rule(s) the body defines: %s. R15.4 item 3 says that "
            "fails the build (ISA-0624)." % (len(unclass), ", ".join(unclass)))))
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
    # ⚑⚑ ISA-0626 — THIS CONTROL USED TO NAME R8.2 AND BROKE THE DAY THE STANDARD WAS HONEST.
    #    `enforced_by` is computed only for rules §17 CLAIMS ASSERTED, so when ISA-0623's
    #    adoption moved R8.2 to JUDGEMENT - correctly, because nothing enforces it - this
    #    control read None and went red for a reason that had nothing to do with the mechanism
    #    it exists to prove. A control that borrows a rule's CLASSIFICATION as a hidden input is
    #    FC-C. It now asks the question against the property instead: whichever rule §17
    #    currently claims ASSERTED and this file happens to name, forcing this file into the
    #    corpus must make that rule read enforced BY THIS FILE.
    _probe = next((r for r in cls["ASSERTED"] if _named_by_failing_check(_self_src, r)), None)
    ok("⚑ CONTROL PROVING THE EXCLUSION IS LOAD-BEARING: this file names at least one "
       "currently-ASSERTED rule inside a failing-check string, so without the exclusion it "
       "WOULD have been counted - the exclusion is doing work, not decorating",
       _probe is not None, "no claimed-ASSERTED rule is named in rule_audit.py's own checks")
    ok("...and with rule_audit.py forced into a corpus, that rule reads as enforced BY "
       "rule_audit.py - the failure mode is reproduced on demand rather than described",
       _probe is not None
       and audit(corpus={SELF: _self_src})["traceable"]["enforced_by"].get(_probe) == [SELF],
       _probe)

    # ── ISA-0624 — §-SHORTHAND, AND THE TRAP THAT MAKES THE NAIVE VERSION WRONG ───────────
    _defined = defined_rules(_read(STANDARD))
    ok("§18 is expanded to the rules the BODY defines, not counted as zero - the shorthand the "
       "docstring claimed for a week and did not implement",
       set(_expand("§18", _defined)) == set(_defined.get(18, [])) and len(_defined.get(18, [])) > 0,
       _defined.get(18))
    ok("⚑ NEGATIVE CONTROL (the trap): '§4 (R4.1–R4.13)' is a HEADING beside an explicit range, "
       "so §4 must NOT expand - naive expansion would silently promote R4.14–R4.16 from "
       "JUDGEMENT to ASSERTED",
       "R4.14" not in _expand("§4 (R4.1–R4.13)", _defined)
       and "R4.13" in _expand("§4 (R4.1–R4.13)", _defined))
    _empty_raised = False
    try:
        _expand("§99", _defined)
    except StandardUnreadable:
        _empty_raised = True
    ok("⚑ NEGATIVE CONTROL: a section reference the body defines no rules for RAISES rather "
       "than expanding to nothing - 'that section has no rules' and 'the parser could not find "
       "them' must not render the same (R2.10, R4.9)", _empty_raised)

    # ── ISA-0624 — COVERAGE: what §17 does NOT classify is now measurable, and can FAIL ────
    ok("every rule the standard's body defines is classified in §17 - R15.4 item 3 made "
       "fireable rather than aspirational", not unclassified(), unclassified())
    _injected = _read(STANDARD) + (
        "\n\n**R99.1 — a rule written into the body and into no row of §17.**\n")
    ok("⚑ NEGATIVE CONTROL: a rule added to the body and to no §17 row is REPORTED unclassified "
       "- so the green above means 'covered', not 'the coverage test never finds anything'",
       unclassified(_injected) == ["R99.1"], unclassified(_injected))

    # ── ISA-0627 — PROSE DELIVERED BY THE CODE IS STILL PROSE ────────────────────────────
    _rendered = ("def render_banner(rows):\n"
                 '    rows.append("This file is a VIEW. Edit the store instead (R9.9).")\n'
                 "    return rows\n")
    ok("⚑ NEGATIVE CONTROL: a rule id inside a sentence a RENDERER prints into its own output "
       "is documentation delivered at runtime, not a check that fails - the shape that made "
       "R7.1 and R13.2 read as enforced (ISA-0627)",
       _named_by_enforcement(_rendered, "R9.9")
       and not _named_by_failing_check(_rendered, "R9.9"))
    _raises_it = ("def load(x):\n"
                  "    if x is None:\n"
                  '        raise ValueError("R9.9: refused - a null control never returns PASS")\n'
                  "    return x\n")
    ok("⚑ POSITIVE CONTROL: the same rule id inside a `raise` DOES count - so the negative "
       "above means 'cannot fail', not 'the strict scanner never finds anything'",
       _named_by_failing_check(_raises_it, "R9.9"))
    _checked = ("def test_thing():\n"
                '    check("D3/liveness: the write path refreshes the stores itself (R9.9)", True)\n')
    ok("⚑ POSITIVE CONTROL: a rule id inside a `check(...)` label in a suite counts - the "
       "shape carrying R4.11 and R5.6, which a raise/assert-only test would have wrongly "
       "rejected", _named_by_failing_check(_checked, "R9.9"))
    _finding = ("def build(dups):\n"
                '    dups.append({"function": "f", "note": "hand-maintained copy (R9.9)"})\n'
                "    return dups\n")
    ok("⚑ POSITIVE CONTROL: a diagnostic dict appended to a findings list counts even though "
       "the list is not called 'errors' - the shape carrying the Atlas's whole enforcement of "
       "R4.5", _named_by_failing_check(_finding, "R9.9"))
    _doc2 = audit()
    ok("the STRICT traceable set is a subset of the loose one - the stricter test can only "
       "remove, never invent",
       _doc2["traceable"]["n_traceable"] <= _doc2["traceable"]["n_traceable_loose"])
    ok("both measurements are published rather than blended (R6.2), and the delta is named",
       "loose_but_not_failing" in _doc2["traceable"]
       and "coverage" in _doc2)
    _both = trace = traceability()
    ok("the audit separates FAILS_ON_BREAK_IN from ENFORCED_IN from MENTIONED_IN, so a rule "
       "that is named but cannot fail is VISIBLE rather than absent (R6.2: the disagreement "
       "between the two measurements is published, never blended)",
       all(set(v) == {"fails_on_break_in", "enforced_in", "mentioned_in"}
           for v in _both.values()))

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
