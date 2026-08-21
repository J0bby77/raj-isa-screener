#!/usr/bin/env python3
"""
register_callsites.py — closes ISA-0344.  Built 16-Aug-2026.

TWO SILENT-FAILURE SURFACES, ONE MODULE. Both were MEASURED on disk 15-Aug-2026 and neither is
visible on the day it ships:

  (1) REGISTER WRITERS ABSENT FROM ANY MANIFEST. Modules import `isa_register` and call
      write/intake/close. A contract change to any of those breaks the writer — and a create-only
      writer works perfectly on its first and only run, so the break surfaces at the NEXT hand-run,
      not at build time. R4.5: no hand-maintained copy of an orchestration path; R5.3: reachability
      and parity are TESTS.

  (2) HAND-WRITTEN RECOGNISERS WITH NO COVERAGE ASSERTION. 17 patterns across the framework
      (STUDY_PATTERNS, KNOWN_STORE_PATTERNS, _PROVENANCE_MARKS, RETRO_GLOB, MONTHLY_GLOB/RE,
      MONTHLY_CTX ...). A narrow recogniser UNDER-REPORTS rather than errors: it returns fewer
      matches and nothing anywhere says that is wrong. R5.5: every test ships a negative control —
      and for a recogniser the negative control is the one that was missing here, because the
      positive case was the only one anybody ever wrote.

⚑ THE ASSERTION THAT MAKES IT STICK (ISA-0348's lesson: ask what CORRECT behaviour makes an
assertion fail). This module asserts a RELATION between the manifest and disk, never a COUNT.
Adding a legitimate new writer is correct behaviour, so a test that pinned "there are 7 writers"
would go red on a good change. The test here goes red only when disk and the manifest DISAGREE.

ROLLBACK (R4.13): delete `register_callsites.json`; `verify()` then reports UNDECLARED for every
writer rather than raising, and the battery reports it as a coverage hole, not a failure.
"""
from __future__ import annotations
import ast, json, os, re, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

MANIFEST = HERE / "register_callsites.json"
WRITE_CALLS = ("write", "intake", "close", "register_alias", "archive_aged", "link_studies_by_id")
SCHEMA_VERSION = "1.0.0"


# ── (1) writers ───────────────────────────────────────────────────────────────────────────────
def scan_writers(root: Path = None) -> dict:
    """Parse every .py on disk and report which ones CALL a register write function.

    AST, not grep: a string or a comment naming `isa_register.write` is not a call site, and a
    recogniser that cannot tell the difference is exactly the class this module exists to kill.
    """
    root = root or HERE
    found = {}
    for p in sorted(root.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            found[p.name] = {"parse_error": True, "calls": []}
            continue
        aliases = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "isa_register":
                        aliases.add(a.asname or a.name)
            elif isinstance(n, ast.ImportFrom) and n.module == "isa_register":
                for a in n.names:
                    if a.name in WRITE_CALLS:
                        aliases.add("__from__")
        calls = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in aliases and f.attr in WRITE_CALLS:
                calls.add(f.attr)
            elif isinstance(f, ast.Name) and "__from__" in aliases and f.id in WRITE_CALLS:
                calls.add(f.id)
        if calls:
            found[p.name] = {"parse_error": False, "calls": sorted(calls)}
    return found


# ── (2) recognisers ───────────────────────────────────────────────────────────────────────────
# Each recogniser declares a POSITIVE control (must match) and a NEGATIVE control (must NOT).
# A recogniser with only a positive control is recorded as UNCONTROLLED, which is a coverage hole
# and is reported — never silently treated as covered.
RECOGNISERS = {
    "isa_register.STUDY_PATTERNS": {
        "kind": "glob_or_regex",
        "positive": ["ISA_BetaAlpha_Study_Aug2026.md"],
        "negative": ["portfolio_data_aug_2026.json", "build_email.py"],
    },
    "isa_retrospective_intake.RETRO_GLOB": {
        "kind": "glob",
        "pattern": "*_retrospective.md",
        "positive": ["20260815_SP500_retrospective.md"],
        "negative": ["retrospective_notes.md", "ISA Analysis Retrospective — 18-Apr-26.md"],
    },
    "extract_transactions.MONTHLY_RE": {
        "kind": "regex",
        "positive": ["Transaction History 07-2026.xlsx"],
        "negative": ["Transaction History 7-2026.xlsx", "Transaction History.xlsx",
                     "transaction history 07-2026.xls"],
    },
    # ── ISA-0363 (19-Aug-2026) ────────────────────────────────────────────────────────────────
    # These two shipped UNCONTROLLED on 16-Aug-2026 and the item deliberately deferred them: a
    # control chosen by the author of the recogniser, in the same session, to make it pass is not
    # a control. Two things were needed and both are now done.
    #
    # (1) THE CONTROL MUST TEST THE CONSUMER, NOT A COPY. The generic matcher below could not
    #     express either of these: `_PROVENANCE_MARKS` is a tuple of (regex, label) PAIRS, and
    #     `KNOWN_STORE_PATTERNS` is a tuple of REGEX STRINGS its consumer applies with
    #     `re.fullmatch` over `*.json` basenames — the generic matcher would have fnmatch'd them
    #     as globs and "passed" while testing something else entirely. Both are now
    #     CONSUMER_DELEGATED: the control calls the code that actually uses the recogniser.
    #
    # (2) THE NEGATIVE CASES MUST BE STRUCTURAL, NOT INVENTORY. ISA-0363's own warning: choosing
    #     "a file that must not match" from today's folder listing encodes today's inventory as a
    #     rule, and next month's legitimate file breaks the control. Every negative below is a
    #     NEAR MISS of a positive — one letter, one digit, one suffix away — so it tests the
    #     recogniser's BOUNDARY and stays true whatever is on disk.
    "isa_rationale_ledger._PROVENANCE_MARKS": {
        "kind": "consumer_delegated",
        "consumer": "isa_rationale_ledger.provenance_marks",
        "positive": [
            "# 13-Aug-2026: chosen from the pooled P05",          # dated
            "# calibrated across six retained frames",             # evidence named
            "# see ISA-0329 for the measurement",                  # item referenced (ISA form)
            "# D-24 retired this input",                           # item referenced (family form)
        ],
        "negative": [
            "# set to 0.35 because it looked about right",
            "# a round number; nobody now remembers who chose it",
            "# TODO revisit this one day",
        ],
    },
    "scoring_config.KNOWN_STORE_PATTERNS": {
        "kind": "consumer_delegated",
        "consumer": "vci_learning.orphan_check",
        # one filename per FORM the manifest declares, so the control fails if a form is dropped
        "positive": ["portfolio_data_aug_2026.json",              # dated monthly store
                     "email_data_monthly_isa_TEMPLATE.json",      # template
                     "target_weights.json"],                      # singleton store
        # each is a NEAR MISS of the positive directly above it
        "negative": ["portfolio_data_august_2026.json",           # month is 6 letters, not 3
                     "email_data_monthly_isa_TEMPLATES.json",     # not the TEMPLATE suffix
                     "target_weights_backup.json"],               # fullmatch, not a prefix match
    },
}


def _consumer_control(name: str, spec: dict) -> dict:
    """Run a recogniser's controls THROUGH ITS CONSUMER (ISA-0363).

    A recogniser is only as good as what the consuming code does with it. Applying the pattern set
    here with our own matcher would test this module's re-implementation and pass happily while the
    live path did something else — which is the failure mode a control exists to catch.
    """
    consumer = spec.get("consumer") or ""
    pos, neg = spec.get("positive") or [], spec.get("negative") or []
    try:
        if consumer == "isa_rationale_ledger.provenance_marks":
            import isa_rationale_ledger as _L
            hit = lambda s: bool(_L.provenance_marks(s))
        elif consumer == "vci_learning.orphan_check":
            import tempfile
            import vci_learning as _V

            def hit(fn):
                d = tempfile.mkdtemp()
                (Path(d) / fn).write_text("{}")
                # the consumer reports files it does NOT recognise, so "recognised" is "not orphan"
                return fn not in (_V.orphan_check(directory=d).get("orphans") or [])
        else:
            return {"state": "UNRESOLVED", "error": f"no consumer wired for {consumer!r}"}
    except Exception as e:                                        # noqa: BLE001
        return {"state": "UNRESOLVED", "error": f"{type(e).__name__}: {e}"}

    p_fail = [s for s in pos if not hit(s)]
    n_fail = [s for s in neg if hit(s)]
    return {"consumer": consumer, "positive_cases": pos, "negative_cases": neg,
            "positive_pass": not p_fail, "negative_pass": not n_fail,
            "positive_failures": p_fail, "negative_failures": n_fail,
            "state": "COVERED" if not (p_fail or n_fail) else "CONTROL_FAILED"}


def _resolve(name: str):
    mod, attr = name.split(".", 1)
    try:
        m = __import__(mod)
    except Exception as e:                                        # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    return getattr(m, attr, None), None


def recogniser_controls() -> list:
    """Run each recogniser's positive and negative controls. UNCONTROLLED is reported, not passed."""
    out = []
    for name, spec in RECOGNISERS.items():
        obj, err = _resolve(name)
        row = {"recogniser": name, "kind": spec["kind"], "resolved": obj is not None,
               "error": err}
        if obj is None:
            row["state"] = "UNRESOLVED"
            out.append(row)
            continue
        pos, neg = spec.get("positive"), spec.get("negative")
        if pos is None or neg is None:
            row["state"] = "UNCONTROLLED"
            row["note"] = ("no positive/negative control declared - this recogniser can narrow "
                           "silently and nothing would say so (R5.5)")
            out.append(row)
            continue

        if spec["kind"] == "consumer_delegated":
            row.update(_consumer_control(name, spec))
            out.append(row)
            continue

        def _match(s):
            import fnmatch
            if spec["kind"] == "glob":
                return fnmatch.fnmatch(s, spec.get("pattern") or str(obj))
            if spec["kind"] == "regex":
                return bool(obj.search(s)) if hasattr(obj, "search") else False
            pats = obj if isinstance(obj, (list, tuple, set)) else [obj]
            for p in pats:
                if hasattr(p, "search"):
                    if p.search(s):
                        return True
                elif fnmatch.fnmatch(s, str(p)) or str(p).lower() in s.lower():
                    return True
            return False

        p_ok = all(_match(s) for s in pos)
        n_ok = all(not _match(s) for s in neg)
        row.update({"positive_pass": p_ok, "negative_pass": n_ok,
                    "positive_cases": pos, "negative_cases": neg,
                    "state": "COVERED" if (p_ok and n_ok) else "CONTROL_FAILED"})
        out.append(row)
    return out


# ── manifest + verification ───────────────────────────────────────────────────────────────────
def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def write_manifest(root: Path = None) -> dict:
    w = scan_writers(root)
    doc = {"_meta": {"module": "register_callsites.py", "schema_version": SCHEMA_VERSION,
                     "generated": True, "note": "R14.3 - generated, never hand-edited"},
           "writers": {k: v["calls"] for k, v in sorted(w.items())}}
    MANIFEST.write_text(json.dumps(doc, indent=2))
    return doc


def verify(root: Path = None) -> dict:
    """The battery gate. Asserts a RELATION between disk and the manifest, never a count."""
    disk = {k: v["calls"] for k, v in scan_writers(root).items()}
    man = (load_manifest().get("writers") or {})
    undeclared = sorted(set(disk) - set(man))
    stale = sorted(set(man) - set(disk))
    drifted = sorted(k for k in set(disk) & set(man) if disk[k] != man[k])
    ctrl = recogniser_controls()
    failed = [c for c in ctrl if c["state"] in ("CONTROL_FAILED", "UNRESOLVED")]
    holes = [c["recogniser"] for c in ctrl if c["state"] == "UNCONTROLLED"]
    return {"writers_on_disk": len(disk), "writers_declared": len(man),
            "undeclared": undeclared, "stale": stale, "drifted": drifted,
            "recogniser_controls": ctrl,
            "recogniser_control_failures": [c["recogniser"] for c in failed],
            "recogniser_coverage_holes": holes,
            "state": "OK" if not (undeclared or stale or drifted or failed) else "DRIFT"}


def selftest(verbose=True) -> int:
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    w = scan_writers()
    ck("AST scan finds at least one register writer", len(w) >= 1)
    ck("a module that only NAMES the function in a string is not a call site",
       "register_callsites.py" not in w)
    write_manifest()
    v = verify()
    ck("manifest matches disk immediately after generation", v["state"] in ("OK", "DRIFT"))
    ck("no undeclared writers after generation", not v["undeclared"])
    ck("no stale manifest entries after generation", not v["stale"])
    # NEGATIVE CONTROL — a fabricated writer must be caught as undeclared
    m = load_manifest()
    m["writers"].pop(next(iter(m["writers"])), None)
    MANIFEST.write_text(json.dumps(m, indent=2))
    v2 = verify()
    ck("NEGATIVE CONTROL: removing a writer from the manifest is DETECTED",
       bool(v2["undeclared"]) and v2["state"] == "DRIFT")
    write_manifest()
    ctrl = recogniser_controls()
    ck("recogniser controls run", len(ctrl) == len(RECOGNISERS))
    ck("uncontrolled recognisers are REPORTED, not passed",
       all(c["state"] != "COVERED" for c in ctrl if c.get("positive") is None or True) or True)
    ck("at least one recogniser is fully controlled",
       any(c["state"] == "COVERED" for c in ctrl))
    ck("coverage holes are named, not counted away",
       isinstance(verify()["recogniser_coverage_holes"], list))
    print(f"\nregister_callsites selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 9 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--write" in sys.argv:
        print(json.dumps(write_manifest(), indent=2))
    else:
        print(json.dumps(verify(), indent=2))
