"""Framework Atlas - the map of the ISA framework, generated from disk.

Standard: ISA_Engineering_Rules.md s15.

Why this exists (R15.1): every Change Footprint (R12.1) must be built from what is
ON DISK, not from my recollection - and my recollection is the thing that has been
wrong. expected_return.py's docstring claimed two consumers; disk had nine.
classify_security_type had zero callers, which is why an exclusion the Run_Context
mandated had never once run. screener_local was a silently diverging copy of
run_scheduled's orchestration.

R15.4: refresh() runs as part of shipping ANY change, and --check FAILS the build
when the regenerated graph differs from the declared manifest.

DISCIPLINE: every finding below carries a confidence. Structural facts (imports,
definitions, call sites) are VERIFIED - they are read from the AST. Anything
inferred (duplicate orchestration, artefact direction) is a CANDIDATE and says so.
An Atlas that reports a confident falsehood is worse than no Atlas (R2.5, FC-B).
"""
from __future__ import annotations

import ast
import hashlib
import re
import json
import os
import sys
from datetime import date
from pathlib import Path

ATLAS_VERSION = "1.0.0"

EXCLUDE_DIR_PARTS = ("__pycache__", "archive", "_bak", "_baseline", ".git", "node_modules",
                     "calibration_pathc_jul2026")
ARTEFACT_SUFFIXES = (".json", ".csv", ".jsonl", ".parquet", ".md", ".xlsx")

CAPITAL_CONST_HINTS = ("FLOOR", "MIN", "MAX", "CAP", "WEIGHT", "THRESHOLD", "BAR", "PCT",
                       "RATE", "BAND", "LIMIT", "TOLERANCE", "BUDGET", "TARGET", "HURDLE")

WRITE_FUNCS = {"write_text", "write_bytes", "to_csv", "to_json", "to_parquet", "to_excel", "dump", "savefig"}
READ_FUNCS = {"read_text", "read_bytes", "read_csv", "read_json", "read_parquet", "read_excel", "load"}

SKIP_ZERO_CALLER = {"main", "selftest", "__init__", "__repr__", "__str__", "__enter__", "__exit__"}


def repo_root() -> Path:
    env = os.environ.get("ISA_ATLAS_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent


def state_dir() -> Path:
    env = os.environ.get("ISA_REGISTER_STORE")
    if env:
        return Path(env)
    return repo_root() / "Dashboard" / "state"


def _iter_py(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part.startswith(x) or part == x for part in rel.parts for x in EXCLUDE_DIR_PARTS):
            continue
        yield p


# ---------------------------------------------------------------- module scan

class ModuleScan(ast.NodeVisitor):
    def __init__(self, modname: str):
        self.mod = modname
        self.functions = {}        # name -> {"lineno", "args", "calls": set, "asserts": int, "raises": int}
        self.classes = []
        self.imports = set()       # imported module names
        self.import_froms = {}     # module -> [names]
        self.calls = set()         # every called name, module-wide
        self.attr_calls = set()    # obj.attr form
        self.artefacts = set()
        self.writes = set()
        self.reads = set()
        self.constants = {}        # NAME -> repr(value)
        self.string_names = set()  # bare identifiers appearing as string literals (dispatch tables)
        self.assert_count = 0
        self.raise_count = 0
        self._fn_stack = []

    # -- helpers
    def _record_literal(self, s: str):
        if any(s.endswith(suf) for suf in ARTEFACT_SUFFIXES) and "/" not in s[:1]:
            self.artefacts.add(s)
        if s.isidentifier() and 2 < len(s) < 60:
            self.string_names.add(s)

    def _cur(self):
        return self.functions[self._fn_stack[-1]] if self._fn_stack else None

    # -- visits
    def visit_Import(self, node):
        for a in node.names:
            self.imports.add(a.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.level == 0:
            base = node.module.split(".")[0]
            self.imports.add(base)
            self.import_froms.setdefault(base, []).extend(a.name for a in node.names)
        self.generic_visit(node)

    def _visit_fn(self, node):
        qual = ".".join([*[f for f in self._fn_stack], node.name]) if self._fn_stack else node.name
        self.functions[qual] = {
            "lineno": node.lineno,
            "args": [a.arg for a in node.args.args],
            "calls": set(),
            "asserts": 0,
            "raises": 0,
            "doc": bool(ast.get_docstring(node)),
        }
        self._fn_stack.append(qual)
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_FunctionDef = _visit_fn
    visit_AsyncFunctionDef = _visit_fn

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        self.generic_visit(node)

    def visit_Assert(self, node):
        self.assert_count += 1
        cur = self._cur()
        if cur:
            cur["asserts"] += 1
        self.generic_visit(node)

    def visit_Raise(self, node):
        self.raise_count += 1
        cur = self._cur()
        if cur:
            cur["raises"] += 1
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self._record_literal(node.value)
        self.generic_visit(node)

    def visit_Assign(self, node):
        if not self._fn_stack:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 2:
                    try:
                        self.constants[t.id] = ast.unparse(node.value)[:200]
                    except Exception:
                        self.constants[t.id] = "<unparseable>"
        self.generic_visit(node)

    def visit_Call(self, node):
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
            self.attr_calls.add(fname)
        if fname:
            self.calls.add(fname)
            cur = self._cur()
            if cur:
                cur["calls"].add(fname)
            # artefact direction, best effort and labelled as such
            lits = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            arte = [s for s in lits if any(s.endswith(x) for x in ARTEFACT_SUFFIXES)]
            if fname == "open":
                mode = ""
                for a in node.args[1:]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        mode = a.value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                target = self.writes if ("w" in mode or "a" in mode) else self.reads
                for s in arte:
                    target.add(s)
            elif fname in WRITE_FUNCS:
                for s in arte:
                    self.writes.add(s)
            elif fname in READ_FUNCS:
                for s in arte:
                    self.reads.add(s)
        self.generic_visit(node)


def scan_module(path: Path, root: Path):
    rel = path.relative_to(root).as_posix()
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    sc = ModuleScan(path.stem)
    sc.visit(tree)
    return {
        "module": path.stem,
        "path": rel,
        "lines": src.count("\n") + 1,
        "functions": {k: {**v, "calls": sorted(v["calls"])} for k, v in sc.functions.items()},
        "classes": sc.classes,
        "imports": sorted(sc.imports),
        "calls": sorted(sc.calls),
        "artefacts": sorted(sc.artefacts),
        "writes": sorted(sc.writes),
        "reads": sorted(sc.reads),
        "constants": sc.constants,
        "string_names": sorted(sc.string_names),
        "assert_count": sc.assert_count,
        "raise_count": sc.raise_count,
        "has_selftest": any(k == "selftest" or k.startswith("selftest") for k in sc.functions),
    }


# ISA-0211 (12-Aug-2026). Skills_to_Edit/*/SKILL.md inside the ISA folder is a MIRROR. The
# scheduler executes the copies under Claude/Scheduled/. A guard pointed at the mirror verifies
# the mirror: it raised four false alarms about a "Step 12" the live prompt had already
# corrected, and - the dangerous direction - it would report GREEN on a correct mirror while the
# executed prompt was wrong. So: read the EXECUTED contract when it is reachable, and when it is
# not, say which basis was used rather than letting a mirror pass as the real thing (R6.4).
SCHEDULED_SKILLS_ENV = "ISA_SCHEDULED_SKILLS_DIR"
SCHEDULED_SKILLS_DEFAULT = r"C:\Users\rjoba\OneDrive\Documents\Claude\Scheduled"


def scheduled_skills_dir():
    """The directory the scheduler actually runs from, or None if unreachable from here."""
    import os
    cand = os.environ.get(SCHEDULED_SKILLS_ENV) or SCHEDULED_SKILLS_DEFAULT
    p = Path(cand)
    return p if p.is_dir() else None


def run_surface_texts(root: Path = None, with_basis: bool = False):
    """{label: raw text} for every run surface. THE single enumeration of run surfaces,
    consumed by consistency_check so its guards cover all of them rather than one document.

    Each SKILL surface is read from the EXECUTED location when that is reachable and from the
    ISA-folder mirror otherwise. `with_basis=True` returns {label: (text, basis)} where basis is
    `executed` | `mirror` - never absent, because a check that cannot say what it read is not
    evidence (R4.2)."""
    root = root or repo_root()
    live = scheduled_skills_dir()
    out, basis, seen = {}, {}, set()
    for pattern in RUN_SURFACE_GLOBS:
        for f in sorted(root.glob(pattern)):
            if f in seen or any(x in f.as_posix() for x in ("archive", "_bak", "_baseline")):
                continue
            if f.suffix.lower() not in (".md",):
                continue
            seen.add(f)
            label = f.parent.name if f.name == "SKILL.md" else f.stem
            src, why = f, "mirror" if f.name == "SKILL.md" else "executed"
            if f.name == "SKILL.md" and live is not None:
                cand = live / label / "SKILL.md"
                if cand.is_file():
                    src, why = cand, "executed"
            out[label] = src.read_text(encoding="utf-8", errors="replace")
            basis[label] = why
    if with_basis:
        return {k: (v, basis[k]) for k, v in out.items()}
    return out


def run_surface_mirror_drift(root: Path = None) -> list:
    """Labels whose ISA-folder mirror differs from the executed prompt.

    Returns [] when the live directory is unreachable - and the CALLER is told that separately,
    because "no drift found" and "could not look" must never render the same (R2.10)."""
    root = root or repo_root()
    live = scheduled_skills_dir()
    if live is None:
        return []
    drift = []
    for f in sorted(root.glob("Skills_to_Edit/*/SKILL.md")):
        label = f.parent.name
        cand = live / label / "SKILL.md"
        if not cand.is_file():
            drift.append(f"{label}: no executed prompt at {cand}")
        elif cand.read_text(encoding="utf-8", errors="replace") != \
                f.read_text(encoding="utf-8", errors="replace"):
            drift.append(f"{label}: mirror differs from the executed prompt")
    return drift


TRIAGE_FILE = "atlas_triage.json"


def _finding_key(kind: str, finding: dict) -> str:
    subj = (finding.get("constant") or finding.get("function") or finding.get("artefact")
            or finding.get("surface") or finding.get("module") or "?")
    return f"{kind}::{subj}"


def _finding_fingerprint(finding: dict) -> str:
    """What was TRUE when the finding was accepted. An acceptance is conditional on this
    not changing - otherwise a triage decision becomes a permanent blindfold, which is the
    class where an uninformed choice hardens into something invisible (R4.8)."""
    if "homes" in finding:
        payload = sorted((h["module"], h.get("normalised", h.get("value", "")))
                         for h in finding["homes"])
    elif "defined_in" in finding:
        payload = sorted(finding["defined_in"])
    elif "written_by" in finding:
        payload = [sorted(finding.get("written_by", [])), sorted(finding.get("read_by", []))]
    else:
        payload = sorted((k, str(v)) for k, v in finding.items() if k != "confidence")
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def load_triage() -> dict:
    path = state_dir() / TRIAGE_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def accept_finding(kind, finding, *, reason, accepted_by, register_item, expires_on=None):
    """Record a triage decision so the same finding does not resurface every run.

    Raj, 12-Aug-2026: "to avoid the same finding coming up again". A list that reports ten
    already-judged items every month teaches the reader to skip it, and the eleventh - the
    real one - goes with them (R14.1).

    The acceptance is NOT unconditional: it is bound to a fingerprint of what was true when
    it was made. Change one of the values and the finding RETURNS, saying it was previously
    accepted and what has moved since.
    """
    if not reason or not accepted_by or not register_item:
        raise ValueError("an acceptance needs a reason, an owner and a register item id. "
                         "An unattributed suppression is indistinguishable from a bug (R7.7)")
    state_dir().mkdir(parents=True, exist_ok=True)
    data = load_triage()
    data[_finding_key(kind, finding)] = {
        "fingerprint": _finding_fingerprint(finding),
        "reason": reason, "accepted_by": accepted_by, "register_item": register_item,
        "accepted_on": date.today().isoformat(), "expires_on": expires_on,
    }
    (state_dir() / TRIAGE_FILE).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def apply_triage(findings: dict, triage: dict = None, today: date = None) -> dict:
    """Split each finding list into active / accepted / reopened. Nothing is ever deleted."""
    triage = load_triage() if triage is None else triage
    today = today or date.today()
    active, accepted, reopened = {}, [], []
    for kind, items in findings.items():
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            active[kind] = items
            continue
        keep = []
        for f in items:
            rec = triage.get(_finding_key(kind, f))
            if not rec:
                keep.append(f)
                continue
            if rec.get("expires_on") and str(rec["expires_on"]) < today.isoformat():
                reopened.append({**f, "kind": kind, "reopened_because": "acceptance expired",
                                 "accepted_on": rec["accepted_on"], "register_item": rec["register_item"]})
                keep.append(f)
            elif rec["fingerprint"] != _finding_fingerprint(f):
                reopened.append({**f, "kind": kind,
                                 "reopened_because": "the values have CHANGED since this was accepted",
                                 "accepted_on": rec["accepted_on"], "register_item": rec["register_item"]})
                keep.append(f)
            else:
                accepted.append({**f, "kind": kind, "reason": rec["reason"],
                                 "register_item": rec["register_item"], "accepted_on": rec["accepted_on"]})
        active[kind] = keep
    active["_accepted_previously_triaged"] = accepted
    active["_reopened_acceptance_no_longer_holds"] = reopened
    return active


# ---------------------------------------------------------------- build

RUN_SURFACE_GLOBS = ("Run_Context*.md", "Skills_to_Edit/*/SKILL.md", "*/SKILL.md",
                     "*.bat", "*.ps1", "*.sh")

_MOD_REF = re.compile(r"\b([a-z][a-z0-9_]{2,})\.py\b")
_MOD_DASH = re.compile(r"python3?\s+(?:-m\s+)?([a-z][a-z0-9_]{2,})\b")
# A run surface can also invoke a module by IMPORTING it in an instructed code block.
# Omitting these forms made the scanner report vci_run_capture as unreachable when
# Run_Context_VCI_Task.md says `import vci_run_capture as VRC` three times. A detector
# that misses a whole invocation form produces confident false negatives - FC-I.
_MOD_IMPORT = re.compile(r"(?:^|\n|`|\s)import\s+([a-z][a-z0-9_]{2,})", re.M)
_MOD_FROM = re.compile(r"(?:^|\n|`|\s)from\s+([a-z][a-z0-9_]{2,})\s+import", re.M)
# Prose noise that the .py pattern picks up from inline examples and code fragments.
_REF_NOISE = {"def", "ticker", "tickers", "p_div", "raw_yield", "avail_years", "py_compile",
              "tests_jul2026", "self", "print", "return", "true", "false", "none"}
# A run surface importing json or yfinance is not naming a missing framework module.
# Reporting those as broken references would train the reader to ignore the finding,
# which is the same as not having it (R14.1 - a signal nobody reads is not a control).
_THIRD_PARTY = {"yfinance", "pandas", "numpy", "requests", "bs4", "lxml", "openpyxl",
                "curl_cffi", "matplotlib", "scipy", "dateutil", "pytz", "fastapi",
                "uvicorn", "pydantic", "jinja2", "defaultdict", "np", "pd"}


def _is_external(name: str) -> bool:
    return name in _THIRD_PARTY or name in getattr(sys, "stdlib_module_names", frozenset())


def _live_lines(text: str):
    """Operative lines only. Delegates to consistency_check, the single home for the
    historical-marker vocabulary (R4.4). Refuses to guess if that module is unavailable."""
    try:
        from consistency_check import _live_lines as cc_live
    except Exception as exc:  # pragma: no cover - environment, not logic
        raise RuntimeError(
            "framework_atlas needs consistency_check._live_lines to tell operative prose from "
            "a retirement note. Refusing to scan without it rather than reporting every "
            "struck-through line as a live reference (R4.4, R4.3)") from exc
    return cc_live(text)


def scan_run_surfaces(root: Path, known_modules: set) -> dict:
    """Map every RUN SURFACE (screeners, VCI, intramonth, pre-run, main run, EPS,
    intelligence brief) to the modules it invokes.

    Raj, 09-Aug-2026: "this should cover everything end to end". A module graph alone
    cannot answer that - the runs are defined in SKILL.md and Run_Context prose, and
    a run that names a module which does not exist is exactly FC-E (an absent
    execution reporting success). This is the check that would have caught it.
    """
    surfaces = {}
    seen = set()
    for pattern in RUN_SURFACE_GLOBS:
        for f in sorted(root.glob(pattern)):
            if f in seen or any(x in f.as_posix() for x in ("archive", "_bak", "_baseline")):
                continue
            seen.add(f)
            text = f.read_text(encoding="utf-8", errors="replace")
            # Only OPERATIVE prose counts. A line that marks itself RETIRED/SUPERSEDED may
            # legitimately quote the script it retired - the monthly Run_Context does exactly
            # that for energy_screener.py, and reporting it was my own false positive.
            # The filter has ONE home, in consistency_check (R4.4); this imports it rather
            # than restating it, and RAISES if it is gone rather than silently degrading.
            text = "\n".join(_live_lines(text))
            refs = (set(_MOD_REF.findall(text)) | set(_MOD_DASH.findall(text))
                    | set(_MOD_IMPORT.findall(text)) | set(_MOD_FROM.findall(text)))
            refs = {r for r in refs if not r.startswith("test_") and r not in _REF_NOISE}
            resolved = sorted(r for r in refs if r in known_modules)
            missing = sorted(r for r in refs if r not in known_modules and not _is_external(r))
            name = f.parent.name if f.name == "SKILL.md" else f.stem
            surfaces[name] = {
                "definition": f.relative_to(root).as_posix(),
                "kind": "skill" if f.name == "SKILL.md" else "run_context",
                "modules_referenced": resolved,
                "references_not_found_on_disk": missing,
            }
    return surfaces


def build(root: Path = None) -> dict:
    root = root or repo_root()
    mods, unparseable = {}, []
    for p in _iter_py(root):
        try:
            m = scan_module(p, root)
        except SyntaxError as exc:
            # R4.9: a reader that cannot parse a file COUNTS it. Never a silent partial.
            unparseable.append({"path": p.relative_to(root).as_posix(), "error": str(exc)})
            continue
        if m["module"] in mods:
            mods[f"{m['module']}@{m['path']}"] = m
        else:
            mods[m["module"]] = m

    local_names = {m["module"] for m in mods.values()}

    # import graph, restricted to project-local modules (VERIFIED - read from AST)
    importers = {n: set() for n in local_names}
    for name, m in mods.items():
        for imp in m["imports"]:
            if imp in local_names and imp != m["module"]:
                importers[imp].add(m["module"])

    # every defined function, and every module that calls that name (VERIFIED)
    definitions = {}
    for name, m in mods.items():
        for fn in m["functions"]:
            definitions.setdefault(fn.split(".")[-1], []).append({"module": m["module"], "path": m["path"]})
    callers = {}
    for name, m in mods.items():
        for c in m["calls"]:
            if c in definitions:
                callers.setdefault(c, set()).add(m["module"])

    is_test = lambda mod: mod.startswith("test_") or "tests" in (mods.get(mod, {}).get("path", ""))

    string_literals = set()
    for _n, _m in mods.items():
        string_literals.update(_m.get("string_names", []))

    findings = {}

    # KR2 / FC-E: zero-caller functions - defined once, never called anywhere
    zero_caller = []
    for fn, defs in sorted(definitions.items()):
        if fn in SKIP_ZERO_CALLER or fn.startswith("_") or fn.startswith("test_"):
            continue
        called_by = callers.get(fn, set())
        # A function called only inside its own module is NOT dead. Counting those was
        # noise that would have buried the real finding - the signal has to be usable
        # or it will be ignored, which is the same as not having it.
        if not called_by:
            dyn = fn in string_literals
            zero_caller.append({"function": fn, "defined_in": [d["module"] for d in defs],
                                "confidence": "WEAK" if dyn else "CANDIDATE",
                                "dispatch_suspected": dyn,
                                "note": ("name appears as a string literal - probably dispatched via a "
                                         "registry or CLI arg; NOT asserted as dead")
                                        if dyn else
                                        "no call site and no string reference found anywhere in project source"})

    # KR6: the same constant NAME defined in more than one module (duplicate home)
    def _norm_value(v: str) -> str:
        """Compare MEANING, not source text. Two false-positive classes were burying the
        one real finding among 24: (1) `X = _mod.X` is a REFERENCE to the single home, not
        a second home; (2) a set/dict literal differing only in element order is the same
        value. A detector that cries wolf 23 times out of 24 gets ignored, which is the
        same as not having it (R14.1)."""
        v = v.strip()
        if re.fullmatch(r"_?[A-Za-z_][\w]*\.[A-Z_][\w]*", v):
            return "@REFERENCE:" + v.split(".")[-1]
        if (v.startswith("{") and v.endswith("}")) or (v.startswith("[") and v.endswith("]")):
            inner = v[1:-1]
            if "for " not in inner and "(" not in inner:
                parts = sorted(x.strip() for x in inner.split(",") if x.strip())
                return v[0] + ", ".join(parts) + v[-1]
        return v

    const_homes = {}
    for name, m in mods.items():
        for c, v in m["constants"].items():
            const_homes.setdefault(c, []).append({"module": m["module"], "value": v,
                                                  "normalised": _norm_value(v)})
    _PATHISH = re.compile(r"os\.path\.|os\.environ|Path\(|__file__|tempfile\.|dirname|abspath")

    def _classify_dup(name, homes):
        """RULE_DUPLICATE is the only class KR6 counts. Everything else is reported but does
        not fail a build - a per-module HERE = os.path.dirname(__file__) is correct code, not
        a rule with two homes, and 20 of the 24 first-run findings were exactly that."""
        vals = [h["value"] for h in homes]
        if all(_PATHISH.search(v) for v in vals):
            return "LOCAL_SCAFFOLDING"
        if all(re.fullmatch(r"[\'\"]?[\d.]+[\'\"]?", v.strip()) for v in vals) and name.endswith("VERSION"):
            return "LOCAL_SCAFFOLDING"
        if all(re.fullmatch(r"(0|\[\]|\{\}|\(\)|None|True|False)", v.strip()) for v in vals):
            return "LOCAL_SCAFFOLDING"
        return "NAME_COLLISION_NEEDS_TRIAGE"

    duplicate_homes = []
    for c, homes in sorted(const_homes.items()):
        if len(homes) > 1:
            vals = {h["normalised"] for h in homes}
            refs = [h for h in homes if h["normalised"].startswith("@REFERENCE:")]
            if refs and len(refs) < len(homes) and len(vals) <= 2:
                # one real definition plus modules that alias it = ONE home, correctly used
                non_ref = {h["normalised"] for h in homes if not h["normalised"].startswith("@REFERENCE:")}
                if len(non_ref) == 1:
                    continue
            klass = "RULE_DUPLICATE" if len(vals) == 1 else _classify_dup(c, homes)
            duplicate_homes.append({
                "constant": c,
                "classification": klass,
                "counts_for_kr6": klass == "RULE_DUPLICATE",
                "homes": homes,
                "values_agree": len(vals) == 1,
                "confidence": "VERIFIED" if len(vals) > 1 else "CANDIDATE",
                "note": "values DISAGREE across homes - R4.4 breach" if len(vals) > 1
                        else "same value in both homes; still two homes (R4.4)",
            })

    # capital-gating constants (for the Rationale Ledger, R12.3)
    capital_constants = []
    for name, m in sorted(mods.items()):
        if is_test(m["module"]):
            continue
        for c, v in sorted(m["constants"].items()):
            if any(h in c for h in CAPITAL_CONST_HINTS):
                capital_constants.append({"constant": c, "home": m["module"], "value": v,
                                          "has_recorded_rationale": False})

    # KR1: a module that WRITES an artefact but asserts nothing
    unasserted_writers = []
    for name, m in sorted(mods.items()):
        if is_test(m["module"]):
            continue
        if m["writes"] and m["assert_count"] == 0 and m["raise_count"] == 0:
            unasserted_writers.append({"module": m["module"], "writes": m["writes"],
                                       "confidence": "VERIFIED",
                                       "note": "writes an artefact with no assert and no raise anywhere in the module (R5.1)"})

    # orphan artefacts: written but never read, or read but never written
    all_writes, all_reads = {}, {}
    for name, m in mods.items():
        if is_test(m["module"]):
            continue
        for a in m["writes"]:
            all_writes.setdefault(a, []).append(m["module"])
        for a in m["reads"]:
            all_reads.setdefault(a, []).append(m["module"])
    orphan_artefacts = []
    for a in sorted(set(all_writes) | set(all_reads)):
        w, r = all_writes.get(a, []), all_reads.get(a, [])
        if w and not r:
            orphan_artefacts.append({"artefact": a, "written_by": w, "read_by": [],
                                     "kind": "written_never_read", "confidence": "CANDIDATE"})
        elif r and not w:
            orphan_artefacts.append({"artefact": a, "written_by": [], "read_by": r,
                                     "kind": "read_never_written", "confidence": "CANDIDATE",
                                     "note": "may be produced outside python (broker export, manual save)"})

    # KR2: duplicate orchestration - same function name in 2+ modules with similar call sets
    duplicate_orchestration = []
    by_name = {}
    for name, m in mods.items():
        if is_test(m["module"]):
            continue
        for fn, meta in m["functions"].items():
            by_name.setdefault(fn.split(".")[-1], []).append((m["module"], set(meta["calls"])))
    for fn, occurrences in sorted(by_name.items()):
        if len(occurrences) < 2 or fn.startswith("_"):
            continue
        for i in range(len(occurrences)):
            for j in range(i + 1, len(occurrences)):
                m1, c1 = occurrences[i]
                m2, c2 = occurrences[j]
                if len(c1) < 5 or len(c2) < 5:
                    continue
                jac = len(c1 & c2) / max(1, len(c1 | c2))
                if jac >= 0.5:
                    duplicate_orchestration.append({
                        "function": fn, "modules": [m1, m2], "call_overlap": round(jac, 3),
                        "confidence": "CANDIDATE",
                        "note": "same name, overlapping call set - inspect for a hand-maintained copy (R4.5)"})

    # test coverage per module (VERIFIED by name reference, not by execution)
    test_mods = {n: m for n, m in mods.items() if is_test(m["module"])}
    covered = set()
    for n, tm in test_mods.items():
        for imp in tm["imports"]:
            if imp in local_names:
                covered.add(imp)
    untested = sorted(m["module"] for n, m in mods.items()
                      if not is_test(m["module"]) and m["module"] not in covered)

    # ---- run-surface coverage: the end-to-end question
    surfaces = scan_run_surfaces(root, local_names)
    covered_by_surface = set()
    for sname, sv in surfaces.items():
        for m in sv["modules_referenced"]:
            covered_by_surface.add(m)
    # transitive: a module imported by a covered module is also reachable from that run
    changed = True
    while changed:
        changed = False
        for name, m in mods.items():
            if m["module"] in covered_by_surface:
                for imp in m["imports"]:
                    if imp in local_names and imp not in covered_by_surface:
                        covered_by_surface.add(imp)
                        changed = True
    surface_broken_refs = [
        {"surface": k, "definition": v["definition"], "missing": v["references_not_found_on_disk"],
         "confidence": "VERIFIED",
         "note": "a run surface names a module that is not on disk - the run cannot do what it says (FC-E)"}
        for k, v in sorted(surfaces.items()) if v["references_not_found_on_disk"]]
    unreachable = sorted(m["module"] for n, m in mods.items()
                         if not is_test(m["module"]) and m["module"] not in covered_by_surface)

    # ---- artefacts whose only producer is unreachable from every run surface.
    # The MOA class (ISA-0003): missed_opportunity_*.json is ARCHIVED by the monthly
    # Run_Context, contracted by the dashboard and served by an API endpoint - and produced
    # by a module no run invokes. The framework was archiving an artefact nothing makes.
    # Point-fixing MOA would leave the class; this finds every instance (R9.6).
    unreachable_producers = []
    for a in sorted(set(all_writes)):
        producers = all_writes[a]
        if producers and all(m not in covered_by_surface for m in producers):
            unreachable_producers.append({
                "artefact": a, "written_by": producers,
                "read_by": all_reads.get(a, []),
                "confidence": "VERIFIED",
                "note": "every producer of this artefact is unreachable from all run surfaces - "
                        "consumers will read a file that no run creates (FC-E)"})

    findings = {
        "artefacts_with_unreachable_producer": unreachable_producers,
        "run_surface_broken_references": surface_broken_refs,
        "modules_unreachable_from_any_run_surface": unreachable,
        "zero_caller_functions": zero_caller,
        "duplicate_constant_homes": duplicate_homes,
        "unasserted_writers": unasserted_writers,
        "orphan_artefacts": orphan_artefacts,
        "duplicate_orchestration_candidates": duplicate_orchestration,
        "modules_no_test_imports_them": untested,
        "unparseable_files": unparseable,
        "capital_gating_constants": capital_constants,
    }

    atlas = {
        "run_surfaces": surfaces,
        "run_surface_count": len(surfaces),
        "modules_reachable_from_a_run_surface": len(covered_by_surface),
        "atlas_version": ATLAS_VERSION,
        "as_of": date.today().isoformat(),
        "root": str(root),
        "module_count": len(mods),
        "test_module_count": len(test_mods),
        "function_count": sum(len(m["functions"]) for m in mods.values()),
        "modules": {n: {k: v for k, v in m.items() if k != "functions"} for n, m in mods.items()},
        "functions_by_module": {n: sorted(m["functions"]) for n, m in mods.items()},
        "importers": {k: sorted(v) for k, v in importers.items() if v},
        "callers_of": {k: sorted(v) for k, v in sorted(callers.items())},
        "findings": findings,
    }
    atlas["findings"] = apply_triage(atlas["findings"])
    atlas["fingerprint"] = fingerprint(atlas)
    atlas["run_id"] = f"atlas-{atlas['as_of']}-{atlas['fingerprint'][:8]}"
    return atlas


def fingerprint(atlas: dict) -> str:
    """Structure only. Changes when the graph changes; stable across a re-run on the same tree."""
    payload = {
        "modules": {n: {"imports": m["imports"], "writes": m["writes"], "reads": m["reads"],
                        "constants": sorted(m["constants"])}
                    for n, m in sorted(atlas["modules"].items())},
        "functions": {n: v for n, v in sorted(atlas["functions_by_module"].items())},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- persist / drift (R15.4)

ATLAS_FILE = "framework_atlas.json"
MANIFEST_FILE = "framework_atlas_manifest.json"


def refresh(root: Path = None, *, write_manifest: bool = True) -> dict:
    """Regenerate and persist. Called automatically on every build (R15.4)."""
    atlas = build(root)
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    (sd / ATLAS_FILE).write_text(json.dumps(atlas, indent=1, sort_keys=True), encoding="utf-8")
    if write_manifest:
        (sd / MANIFEST_FILE).write_text(json.dumps({
            "fingerprint": atlas["fingerprint"], "run_id": atlas["run_id"],
            "as_of": atlas["as_of"], "module_count": atlas["module_count"],
            "function_count": atlas["function_count"], "atlas_version": ATLAS_VERSION,
        }, indent=2), encoding="utf-8")
    return atlas


def check(root: Path = None) -> tuple:
    """R15.4: regenerate and compare against the declared manifest.

    Returns (ok, message). The build FAILS when this returns False.
    """
    mpath = state_dir() / MANIFEST_FILE
    if not mpath.exists():
        return False, ("no atlas manifest - the map has never been declared. "
                       "Run `framework_atlas.py --refresh` (R15.4)")
    declared = json.loads(mpath.read_text(encoding="utf-8"))
    current = build(root)
    if declared.get("fingerprint") != current["fingerprint"]:
        d_mods = set(json.loads((state_dir() / ATLAS_FILE).read_text(encoding="utf-8"))["modules"]) \
            if (state_dir() / ATLAS_FILE).exists() else set()
        c_mods = set(current["modules"])
        added, removed = sorted(c_mods - d_mods), sorted(d_mods - c_mods)
        return False, (f"ATLAS DRIFT: declared {declared.get('fingerprint','?')[:12]} vs current "
                       f"{current['fingerprint'][:12]}. modules +{len(added)} -{len(removed)}"
                       + (f" added={added[:8]}" if added else "")
                       + (f" removed={removed[:8]}" if removed else "")
                       + ". The map no longer matches the code (R15.4) - refresh and review.")
    return True, f"atlas current: {current['module_count']} modules, fingerprint {current['fingerprint'][:12]}"


# ---------------------------------------------------------------- footprint (R12.1)

def footprint(subject: str, root: Path = None) -> dict:
    """Build the mechanical half of a Change Footprint for a module (R12.1 fields 1,4,5,6,7,8)."""
    atlas = build(root)
    m = atlas["modules"].get(subject)
    if m is None:
        raise KeyError(f"{subject} is not in the Atlas. Modules: {len(atlas['modules'])}")
    fns = atlas["functions_by_module"].get(subject, [])
    inbound = atlas["importers"].get(subject, [])
    outbound = [i for i in m["imports"] if i in atlas["modules"]]
    call_sites = {fn: atlas["callers_of"].get(fn.split(".")[-1], []) for fn in fns}
    dup = [d for d in atlas["findings"]["duplicate_constant_homes"]
           if any(h["module"] == subject for h in d["homes"])]
    orch = [d for d in atlas["findings"]["duplicate_orchestration_candidates"] if subject in d["modules"]]
    dead = [z for z in atlas["findings"]["zero_caller_functions"] if subject in z["defined_in"]]
    return {
        "subject": subject,
        "atlas_run_id": atlas["run_id"],
        "atlas_as_of": atlas["as_of"],
        "what_is_there": {"path": m["path"], "lines": m["lines"], "functions": len(fns),
                          "classes": m["classes"], "constants": sorted(m["constants"]),
                          "has_selftest": m["has_selftest"]},
        "integration_path": {"reads": m["reads"], "writes": m["writes"]},
        "dependencies_in_out": {"inbound_importers": inbound, "outbound_local_imports": outbound,
                                "call_sites_by_function": {k: v for k, v in call_sites.items() if v}},
        "test_surface": {"asserts_in_module": m["assert_count"], "raises_in_module": m["raise_count"],
                         "tested_by_import": subject not in atlas["findings"]["modules_no_test_imports_them"]},
        "degradation_duplication": {"duplicate_constant_homes": dup,
                                    "duplicate_orchestration_candidates": orch},
        "refactor_candidates": {"zero_caller_functions": dead},
        "what_is_not_there": "UNKNOWN - requires the Rationale Ledger and register (R12.3); not derivable from the AST",
        "why_it_is_there": "UNKNOWN - Rationale Ledger (R12.3)",
    }


# ---------------------------------------------------------------- selftest

def selftest(verbose: bool = True) -> int:
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="atlas_"))
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    (tmp / "alpha.py").write_text(
        "import beta\nTHRESH_MIN = 0.5\n\n"
        "def used_fn(x):\n    assert x is not None\n    return beta.helper(x)\n\n"
        "def never_called_fn():\n    return 1\n\n"
        "def writer():\n    open('out_thing.json','w').write('{}')\n", encoding="utf-8")
    (tmp / "beta.py").write_text(
        "THRESH_MIN = 0.9\n\ndef helper(x):\n    return x\n\n"
        "def reader():\n    return open('out_thing.json').read()\n", encoding="utf-8")
    (tmp / "gamma.py").write_text(
        "def lonely():\n    open('never_read.json','w').write('{}')\n", encoding="utf-8")

    a = build(tmp)
    ok(a["module_count"] == 3, f"3 modules, got {a['module_count']}")

    ok("alpha" in a["importers"].get("beta", []), "beta's importer alpha is recorded")

    zc = {z["function"] for z in a["findings"]["zero_caller_functions"]}
    ok("never_called_fn" in zc, "a never-called function is found (FC-E)")
    ok("helper" not in zc, "a called function is NOT reported as dead - negative control (R5.5)")

    dh = {d["constant"]: d for d in a["findings"]["duplicate_constant_homes"]}
    ok("THRESH_MIN" in dh, "the same constant in two modules is a duplicate home (KR6)")
    ok(dh["THRESH_MIN"]["values_agree"] is False, "disagreeing values are reported as disagreeing")
    ok(dh["THRESH_MIN"]["confidence"] == "VERIFIED", "a value disagreement is VERIFIED, not a candidate")
    # negative controls for the two false-positive classes that buried the real finding
    (tmp / "delta2.py").write_text("import alpha\nTHRESH_MIN = alpha.THRESH_MIN\nSET_A = {'b', 'a'}\n", encoding="utf-8")
    (tmp / "eps2.py").write_text("SET_A = {'a', 'b'}\n", encoding="utf-8")
    a_fp = build(tmp)
    dh2 = {d["constant"]: d for d in a_fp["findings"]["duplicate_constant_homes"]}
    ok("SET_A" not in dh2 or dh2["SET_A"]["values_agree"],
       "a set literal differing only in element order is NOT a disagreement")
    (tmp / "scaf1.py").write_text("import os\nHERE = os.path.dirname(os.path.abspath(__file__))\n", encoding="utf-8")
    (tmp / "scaf2.py").write_text("import os\nHERE = os.path.dirname(__file__)\n", encoding="utf-8")
    a_sc = build(tmp)
    dh3 = {d["constant"]: d for d in a_sc["findings"]["duplicate_constant_homes"]}
    ok(dh3["HERE"]["classification"] == "LOCAL_SCAFFOLDING",
       "a per-module path constant is scaffolding, not a rule with two homes")
    ok(dh3["HERE"]["counts_for_kr6"] is False, "scaffolding does not count for KR6")
    ok(dh3["THRESH_MIN"]["counts_for_kr6"] is False and
       dh3["THRESH_MIN"]["classification"] == "NAME_COLLISION_NEEDS_TRIAGE",
       "two numeric thresholds sharing a name need human triage - the tool must not decide")
    (tmp / "scaf1.py").unlink(); (tmp / "scaf2.py").unlink()

    # ---- triage ledger: an accepted finding must not resurface, and must return if it moves
    os.environ["ISA_REGISTER_STORE"] = str(tmp / "state")
    a_t = build(tmp)
    tm = next(d for d in a_t["findings"]["duplicate_constant_homes"] if d["constant"] == "THRESH_MIN")
    raised = False
    try:
        accept_finding("duplicate_constant_homes", tm, reason="", accepted_by="", register_item="")
    except ValueError:
        raised = True
    ok(raised, "an acceptance without reason/owner/register item is REFUSED (R7.7)")
    accept_finding("duplicate_constant_homes", tm, reason="different domains, verified",
                   accepted_by="raj", register_item="ISA-0099")
    a_t2 = build(tmp)
    ok(not any(d["constant"] == "THRESH_MIN" for d in a_t2["findings"]["duplicate_constant_homes"]),
       "an accepted finding does not resurface in the active list")
    ok(any(d.get("constant") == "THRESH_MIN" for d in a_t2["findings"]["_accepted_previously_triaged"]),
       "it is still visible under accepted - suppression is never deletion")
    # change one of the values: the acceptance must no longer hold
    (tmp / "beta.py").write_text("THRESH_MIN = 0.7\n\ndef helper(x):\n    return x\n\n"
                                 "def reader():\n    return open('out_thing.json').read()\n", encoding="utf-8")
    a_t3 = build(tmp)
    ok(any(d.get("constant") == "THRESH_MIN"
           for d in a_t3["findings"]["_reopened_acceptance_no_longer_holds"]),
       "changing a value REOPENS the accepted finding - an acceptance is never a permanent blindfold")
    ok(any(d["constant"] == "THRESH_MIN" for d in a_t3["findings"]["duplicate_constant_homes"]),
       "a reopened finding is back in the active list")
    (tmp / "beta.py").write_text("THRESH_MIN = 0.9\n\ndef helper(x):\n    return x\n\n"
                                 "def reader():\n    return open('out_thing.json').read()\n", encoding="utf-8")
    os.environ.pop("ISA_REGISTER_STORE", None)
    (tmp / "delta2.py").unlink(); (tmp / "eps2.py").unlink()

    orph = {o["artefact"]: o for o in a["findings"]["orphan_artefacts"]}
    ok("never_read.json" in orph, "an artefact written and never read is an orphan")
    ok(orph["never_read.json"]["kind"] == "written_never_read", "orphan direction is recorded")
    ok("out_thing.json" not in orph, "an artefact both written and read is NOT an orphan - negative control")

    ua = {u["module"] for u in a["findings"]["unasserted_writers"]}
    ok("gamma" in ua, "a writer with no assert and no raise is flagged (KR1)")
    ok("alpha" not in ua, "a writer that asserts is not flagged - negative control")

    ok(a["findings"]["capital_gating_constants"], "MIN/THRESH constants surface for the Rationale Ledger")

    # ---- run surfaces: the end-to-end coverage question
    (tmp / "Run_Context_Fake_Run.md").write_text(
        "Step 1: run alpha.py\nStep 2: then run zeta.py which does the thing\n", encoding="utf-8")
    sk = tmp / "Skills_to_Edit" / "fake-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("Invoke beta.py for the helper.\n", encoding="utf-8")
    a2 = build(tmp)
    ok(a2["run_surface_count"] == 2, f"both a Run_Context and a SKILL.md are run surfaces, got {a2['run_surface_count']}")
    surf = a2["run_surfaces"]
    ok("alpha" in surf["Run_Context_Fake_Run"]["modules_referenced"], "a referenced module resolves")
    ok("zeta" in surf["Run_Context_Fake_Run"]["references_not_found_on_disk"],
       "a run surface naming a module that is not on disk is caught (FC-E)")
    (tmp / "Run_Context_Import_Form.md").write_text(
        "Step 1: `import gamma as G` then call G.lonely()\n", encoding="utf-8")
    a_imp = build(tmp)
    ok("gamma" in a_imp["run_surfaces"]["Run_Context_Import_Form"]["modules_referenced"],
       "a module invoked by `import X` in a run surface is reachable - the form that made "
       "vci_run_capture a false positive")
    ok("gamma" not in a_imp["findings"]["modules_unreachable_from_any_run_surface"],
       "import-form invocation removes the module from the unreachable list")
    (tmp / "Run_Context_Import_Form.md").unlink()
    br = a2["findings"]["run_surface_broken_references"]
    ok(any(b["surface"] == "Run_Context_Fake_Run" for b in br), "broken reference becomes a finding")
    ok(br[0]["confidence"] == "VERIFIED", "a missing file is VERIFIED, not a candidate")
    (tmp / "Run_Context_Stdlib.md").write_text("Step 1: `import json` and `import yfinance`\n", encoding="utf-8")
    a_std = build(tmp)
    ok(not a_std["run_surfaces"]["Run_Context_Stdlib"]["references_not_found_on_disk"],
       "stdlib and third-party imports are NOT reported as missing framework modules")
    (tmp / "Run_Context_Stdlib.md").unlink()
    ok("beta" not in a2["findings"]["modules_unreachable_from_any_run_surface"],
       "a module named by a SKILL.md is reachable - negative control")
    ok("gamma" in a2["findings"]["modules_unreachable_from_any_run_surface"],
       "a module no run surface reaches is reported as unreachable")
    up = {u["artefact"] for u in a2["findings"]["artefacts_with_unreachable_producer"]}
    ok("never_read.json" in up,
       "an artefact whose only producer is unreachable from every run surface is caught (the MOA class)")
    ok("out_thing.json" not in up,
       "an artefact produced by a run-surface-reachable module is NOT flagged - negative control")
    # transitive reachability: alpha imports beta, so beta is reachable even via import only
    (tmp / "Run_Context_Fake_Run.md").write_text("Step 1: run alpha.py\n", encoding="utf-8")
    (sk / "SKILL.md").write_text("nothing here\n", encoding="utf-8")
    a3 = build(tmp)
    ok("beta" not in a3["findings"]["modules_unreachable_from_any_run_surface"],
       "a module reached only by import from a run-surface module is still reachable (transitive)")

    # fingerprint is stable across a re-run, and moves when the tree changes
    fp1 = build(tmp)["fingerprint"]
    ok(fp1 == a["fingerprint"], "fingerprint is stable on an unchanged tree")
    (tmp / "delta.py").write_text("def brand_new():\n    return 2\n", encoding="utf-8")
    ok(build(tmp)["fingerprint"] != fp1, "fingerprint moves when a module is added")

    # R15.4 drift check
    os.environ["ISA_REGISTER_STORE"] = str(tmp / "state")
    refresh(tmp)
    good, msg = check(tmp)
    ok(good, f"check() passes immediately after refresh: {msg}")
    (tmp / "epsilon.py").write_text("def another():\n    return 3\n", encoding="utf-8")
    bad, msg2 = check(tmp)
    ok(not bad, "check() FAILS when the tree changed and the manifest did not (R15.4)")
    ok("ATLAS DRIFT" in msg2, "the drift message names the drift")

    # unparseable files are counted, never silently skipped (R4.9)
    (tmp / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    b = build(tmp)
    ok(len(b["findings"]["unparseable_files"]) == 1, "an unparseable file is COUNTED, not silently dropped (R4.9)")

    # footprint
    fpt = footprint("alpha", tmp)
    ok(fpt["dependencies_in_out"]["outbound_local_imports"] == ["beta"], "footprint records outbound deps")
    ok(fpt["atlas_run_id"], "footprint carries an atlas run id (R15.3)")

    os.environ.pop("ISA_REGISTER_STORE", None)
    shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print(f"framework_atlas selftest: {n} assertions, 0 failed")
    return n


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--check" in args:
        good, msg = check()
        print(("OK: " if good else "FAIL: ") + msg)
        sys.exit(0 if good else 1)
    elif "--refresh" in args:
        a = refresh()
        print(f"atlas refreshed: {a['module_count']} modules, {a['function_count']} functions, "
              f"run_id {a['run_id']}")
    elif "--footprint" in args:
        print(json.dumps(footprint(args[args.index("--footprint") + 1]), indent=2))
    else:
        a = build()
        f = a["findings"]
        print(f"Framework Atlas {a['run_id']}  ({a['module_count']} modules, {a['function_count']} functions)")
        for k, v in f.items():
            print(f"  {k}: {len(v)}")
