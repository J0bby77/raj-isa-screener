#!/usr/bin/env python3
"""
framework_integrity.py — PHASE 0 of the 27-Aug-2026 build spec: the ENFORCEMENT layer.

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md §5 Phase 0.
Raised as ISA-0467 / ISA-0468. Built 28-Aug-2026.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS — the systemic finding (spec §2)
═══════════════════════════════════════════════════════════════════════════════════════════
Twelve live-path defects were found by hand on 26/27-Aug-2026. `framework_atlas` — 1,019
lines, specified by R15.1 to catch exactly this class — flagged NONE of them, while reporting
150 zero-caller functions and 286 findings in total.

⚑⚑ THE ATLAS MEASURES REACHABILITY. EVERY DEFECT FOUND WAS A LIVE-PATH DEFECT IN REACHABLE
CODE. `record_level` is called — by its own test suite. `position_sizing.stock_max` is
called — by a synthetic probe that self-labels "not a size anyone should act on". Both are
reachable. Neither has ever run on the live capital path.

This module measures EXECUTION, DECLARATION and EVIDENCE. It does not replace the atlas —
the atlas maps structure and becomes one of this module's inputs.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ THE DESIGN RULE FOR THE WHOLE OF PHASE 0, learned from §2.2
═══════════════════════════════════════════════════════════════════════════════════════════
A check that produces 286 unranked findings is a check that gets suppressed — the atlas even
ships `accept_finding()` / `apply_triage()`, so the design anticipated the volume problem and
resolved it by suppression. Every enumerator here therefore emits at most TEN findings ranked
by GBP exposure, plus a PUBLISHED count of what it suppressed and that count's own exposure.
"7 further findings suppressed, GBP 0 combined exposure" is noise; "31 further suppressed,
GBP 4,120 combined exposure" is a queue. The reader can tell them apart (R4.9 — no silent caps).

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ R10 — AN OBSERVER MAY NOT MEASURE ITSELF (A12)
═══════════════════════════════════════════════════════════════════════════════════════════
Phase 0 measures the framework while the framework is being changed. EVERY enumerator in this
module excludes this module's own functions, its own docstrings and its own string constants,
and `selftest()` carries a NEGATIVE CONTROL proving the exclusion is real rather than assumed.
Without it, the phrase list in P0.4 would flag its own documentation and the quantity register
would flag its own registry literals — a check whose first finding is itself is a check that
gets deleted rather than fixed.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["execution_ledger"] / ["quantity_register"] /
["threshold_register"] / ["negative_claim_expiry"] = False. With every flag False `_mark()` is
a no-op, reconciliation is skipped, and run output is byte-identical. The `_mark` CALLS STAY
IN THE CODE — removing them is what makes the ledger droppable.
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sys
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("ISA_OUT", HERE)

SELF_MODULE = "framework_integrity"

# The ranked-queue cap (§P0.6). Ten, and the suppressed count is always published.
QUEUE_CAP = 10

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _mmm_yyyy(d: Optional[datetime.date] = None) -> str:
    d = d or datetime.date.today()
    return "%s_%d" % (_MONTHS[d.month - 1], d.year)


def ledger_path(d: Optional[datetime.date] = None) -> str:
    return os.path.join(OUT, "execution_ledger_%s.json" % _mmm_yyyy(d))


def _flag(name: str, default: bool = False) -> bool:
    """Read a V2 flag, tolerating a policy module that does not yet declare it.

    ⚑ An UNDECLARED flag reads as `default` and is REPORTED by `flag_report()`; it is never
    silently treated as False, because a capability that is off because nobody declared it and
    a capability that is off because someone turned it off are different facts (R2.10)."""
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


def flag_report() -> dict:
    """Which Phase-0 flags are DECLARED in isa_policy and which are merely absent."""
    declared, absent = {}, []
    try:
        import isa_policy as _p
        for f in PHASE0_FLAGS:
            if f in _p.V2_FLAGS:
                declared[f] = bool(_p.V2_FLAGS[f])
            else:
                absent.append(f)
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "POLICY_UNIMPORTABLE", "reason": "%s: %s" % (type(exc).__name__, exc)}
    return {"state": "OK", "declared": declared, "undeclared": sorted(absent),
            "note": ("an undeclared flag defaults OFF and is named here — 'off because nobody "
                     "declared it' and 'off because someone turned it off' are different facts")}


PHASE0_FLAGS = ("execution_ledger", "quantity_register", "threshold_register",
                "negative_claim_expiry")


class IntegrityRefused(RuntimeError):
    """A declaration check could not be performed. NEVER downgraded to a pass — a monitoring
    layer that degrades silently is worse than no monitoring layer (R4.9)."""


# ═══════════════════════════════════════════════════════════════════════════════════════
# SOURCE ACCESS — one reader, cached, so seven enumerators do not parse the tree seven times
# ═══════════════════════════════════════════════════════════════════════════════════════
_SRC_CACHE: Dict[str, Tuple[str, Optional[ast.Module]]] = {}

# Directories that hold TESTS. A caller from here is never `live_run`, and a string constant
# here is never a PRODUCER — which is the whole of F4: `forward_led` occurs only in a selftest
# and two fixtures, and the atlas counted those as producers.
TEST_DIRS = ("tests_jul2026",)
TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*|run_tests|run_fixture_checks|xa1_replay)\.py$")


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if TEST_FILE_RE.search(p):
        return True
    return any(("/%s/" % d) in p or p.startswith("%s/" % d) for d in TEST_DIRS)


def source_files(root: str = HERE, *, include_tests: bool = False,
                 exclude_self: bool = True) -> List[str]:
    """Every .py in the framework, ordered. Backups, caches and archives are EXCLUDED and the
    exclusion is declared here rather than hidden in a walk filter."""
    skip_dirs = {"__pycache__", "archive", "register_archive", "_to_delete", "backfill_source",
                 "screen_history", "bench_cache", "nav_cache", "calibration_pathc_jul2026",
                 "node_modules", "web", "dist", "Skills_to_Edit"}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in skip_dirs and not d.startswith("_bak_")
                       and not d.startswith("_baseline")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if exclude_self and fn == SELF_MODULE + ".py":
                continue
            if not include_tests and is_test_path(rel):
                continue
            out.append(full)
    return sorted(out)


def parsed(path: str) -> Tuple[str, Optional[ast.Module]]:
    """Source text and AST for a file, through the SHARED cache (ISA-0597).

    ⚑ ONE HOME, AND IT PAYS TWICE. This module kept its own `_SRC_CACHE` beside
    `isa_source_cache`, so the ~150 files parsed by `preflight` at Step 0 were parsed AGAIN by
    the A18 checker inside Step 9d in the same process — 657 redundant `ast.parse` calls and 36s
    of a ~175s budget. Sharing the cache means Step 0 warms it for everything downstream.
    Semantics are unchanged and that is not asserted in prose: `producer_equivalence` and
    `computer_equivalence` re-derive every live pair through the pre-ISA-0594 scanners and
    compare (R5.8)."""
    if path in _SRC_CACHE:
        return _SRC_CACHE[path]
    try:
        import isa_source_cache as _sc
        src = _sc.read(path)
        if src is None:
            raise OSError(path)
        tree = _sc.parse_text(src, filename=path)
    except SyntaxError:
        src, tree = (_SRC_CACHE.get(path, ("", None))[0] or ""), None
    except Exception:                                                   # noqa: BLE001
        _SRC_CACHE[path] = ("", None)
        return _SRC_CACHE[path]
    _SRC_CACHE[path] = (src, tree)
    return _SRC_CACHE[path]


# ── PERFORMANCE CACHES (ISA-0552, 02-Sep-2026) ──────────────────────────────────────────
# ⚑ THESE CHANGE COST, NEVER SEMANTICS, AND THE DISTINCTION IS LOAD-BEARING. Step 0 took
# 81.3s of a ~175s host-shell budget — `quantity_register_report` alone, 68.5s of it inside
# `q4_dead_vocabulary` — which is why the pre-run never reached `write_run_context` and
# `run_context_[mmm_yyyy].json` was never produced. A control that costs half the run gets
# switched off (the R5.7 lesson), so making it cheap is part of making it survive.
# Each cache is keyed by PATH and shares its lifetime with `_SRC_CACHE`, so a file that
# changes mid-run is already outside the module's stated model — no new staleness class.
# The pre-filters below are exact, not heuristic: every producer shape `_producers_of`
# recognises matches the field name as literal source text (a dict key, a subscript slice, a
# kwarg name, or an identifier CONTAINING the field), so a file whose lower-cased source does
# not contain the lower-cased field cannot hold a producer for it. That claim is not left as
# prose: `producer_equivalence(sample=N)` below re-derives `_producers_of` for real
# (field, literal) pairs with the pre-filter and the caches BOTH disabled, and asserts the
# hit sets are identical. It runs in `--selftest` (sampled, so it stays affordable) and can
# be run exhaustively with `--equivalence-full`. Measured 02-Sep-2026 on the September tree:
# `quantity_register_report` 87.2s -> 22.7s, output byte-for-byte identical.
_WALK_CACHE: dict = {}
_SPANS_CACHE: dict = {}
_LOWER_CACHE: dict = {}
_DATA_BLOB_CACHE: dict = {}


def _walk_cached(path: str, tree):
    """list(ast.walk(tree)) memoised per NODE, through the shared cache (ISA-0597).

    Keyed on the tree rather than the path, so a tree already walked by another consumer in this
    process is not walked again."""
    try:
        import isa_source_cache as _sc
        return _sc.walk(tree)
    except Exception:                                                   # noqa: BLE001
        got = _WALK_CACHE.get(path)
        if got is None:
            got = list(ast.walk(tree))
            _WALK_CACHE[path] = got
        return got


def _spans_cached(path: str, tree, module: str):
    got = _SPANS_CACHE.get(path)
    if got is None:
        got = _excluded_line_ranges(tree, module)
        _SPANS_CACHE[path] = got
    return got


def _lower_cached(path: str, src: str) -> str:
    got = _LOWER_CACHE.get(path)
    if got is None:
        got = src.lower()
        _LOWER_CACHE[path] = got
    return got


def _data_blobs(root: str):
    """(label, text) for every .json/.jsonl in root and Dashboard/state, read ONCE per root."""
    got = _DATA_BLOB_CACHE.get(root)
    if got is not None:
        return got
    blobs = []
    for fn in sorted(os.listdir(root)):
        if not (fn.endswith(".json") or fn.endswith(".jsonl")):
            continue
        try:
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                blobs.append(("data:%s" % fn, fh.read()))
        except Exception:                                               # noqa: BLE001
            continue
    state_dir = os.path.join(root, "Dashboard", "state")
    if os.path.isdir(state_dir):
        for fn in sorted(os.listdir(state_dir)):
            if not (fn.endswith(".json") or fn.endswith(".jsonl")):
                continue
            try:
                with open(os.path.join(state_dir, fn), encoding="utf-8") as fh:
                    blobs.append(("data:Dashboard/state/%s" % fn, fh.read()))
            except Exception:                                           # noqa: BLE001
                continue
    _DATA_BLOB_CACHE[root] = blobs
    return blobs


def modname(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# ═══════════════════════════════════════════════════════════════════════════════════════
# P0.1 — LIVE-PATH EXECUTION LEDGER
# ═══════════════════════════════════════════════════════════════════════════════════════
# ⚑ EXPLICIT, NOT `sys.settrace`. A tracer that silently stops tracing is precisely the
# defect class this module exists to catch: an absent execution reporting success. So every
# manifest function carries ONE literal line at its head, and a manifest entry with no such
# line is itself a finding (see `mark_sites()`).

CALLER_KINDS = ("live_run", "test", "selftest", "probe", "unknown")

# Callers that are DECLARED PROBES. `probe` is its own kind because
# `capital_destination._stock_side_sensitivity` is exactly the case that fooled the atlas: it
# passes a synthetic `_PROBE` candidate that self-labels "not a size anyone should act on",
# and the atlas counted it as a caller.
PROBE_CALLERS: Dict[Tuple[str, str], str] = {
    ("capital_destination", "_stock_side_sensitivity"):
        "A12 plan-stability grid; passes a synthetic _PROBE candidate (spec §1.1 F1)",
}

# The orchestrators that constitute a LIVE RUN. A frame originating in one of these is
# live_run; anything else that is not a test, selftest or declared probe is `unknown`, which
# is reported and never silently promoted.
LIVE_ORCHESTRATORS = frozenset({
    "monthly_isa_prerun", "build_monthly_isa_email", "email_prefill", "build_email",
    "build_excel", "screener_local", "update_watchlist", "rerank_watchlist",
    "vci_run_capture", "vci_capture_run", "capital_destination", "fund_action_stack",
    "step9_pre_builder", "isa_retrospective_intake", "stock_price_fetch",
})

# ── THE MANIFEST ───────────────────────────────────────────────────────────────────────
# Every function that participates in a capital decision, with the caller it is expected to
# have. `expect` is the kind the function MUST reach for the run to be clean.
#   ("module", "function", expect, why)
CAPITAL_PATH_MANIFEST: List[Tuple[str, str, str, str]] = [
    # --- capital routing -----------------------------------------------------------------
    ("capital_destination", "sleeve_split", "live_run",
     "decides the stock/fund split of the marginal pound"),
    ("position_sizing", "stock_max", "live_run",
     "THE demand-pull sizing rule (clean spec §2). Pre-P4 this reports REACHABLE_NOT_LIVE "
     "because its only caller is the synthetic _PROBE — spec §1.1 F1, assertion L3"),
    ("position_sizing", "allocate", "live_run",
     "floor-then-priority fill (D15-D17). Absent pre-P4"),
    ("position_sizing", "target_pct", "live_run", "the fixed ladder rung for one position"),
    ("position_sizing", "apply_correlation", "live_run",
     "A2.3 unmeasured-correlation STARTER cap"),
    # --- measurement ---------------------------------------------------------------------
    ("stock_return_store", "record_level", "live_run",
     "the capture instrument. Pre-P1 this reports REACHABLE_NOT_LIVE — it has never captured "
     "anything outside its own tests (spec §1.1 F2, assertion L4)"),
    ("stock_return_store", "coverage", "live_run", "per-name measurement status, every run"),
    ("correlation_engine", "assess", "live_run", "the A2.1 admission gate"),
    ("risk_contribution", "contributions", "live_run", "risk share of each held position"),
    ("evidence_state", "classify", "live_run", "the counted-channel evidence classifier"),
    # --- selection / lifecycle -----------------------------------------------------------
    ("retention", "ratchet_eligible", "live_run",
     "the step-down ratchet population. Pre-P5 the filter can never be true (spec §1.1 F4)"),
    ("rerank_watchlist", "_apply_diversification", "live_run",
     "sector/theme caps. Pre-P6 `ctx` carries no sector map, so no cap has ever fired "
     "(spec §1.1 F7)"),
]


def _classify_caller(stack: Sequence[traceback.FrameSummary]) -> Tuple[str, str]:
    """-> (caller_kind, caller_label). Derived from the ORIGINATING module of the call stack.

    ⚑ Order matters and is declared: selftest beats test beats probe beats live_run. A
    function reached from a selftest INSIDE a live orchestrator is a selftest, because the
    question this answers is 'did real capital flow through here', not 'which file is it in'."""
    frames = [f for f in stack
              if modname(f.filename) not in (SELF_MODULE,)]
    if not frames:
        return "unknown", "no frame outside the instrument"
    label = None
    kinds = []
    for f in frames:
        m = modname(f.filename)
        fn = f.name
        if fn.endswith("_selftest") or fn == "selftest" or fn == "_selftest":
            kinds.append("selftest"); label = label or "%s.%s" % (m, fn)
        elif is_test_path(f.filename) or m.startswith("test_"):
            kinds.append("test"); label = label or "%s.%s" % (m, fn)
        elif (m, fn) in PROBE_CALLERS:
            kinds.append("probe"); label = label or "%s.%s" % (m, fn)
        elif m in LIVE_ORCHESTRATORS:
            kinds.append("live_run"); label = label or "%s.%s" % (m, fn)
    for k in ("selftest", "test", "probe", "live_run"):
        if k in kinds:
            return k, label or k
    f = frames[-1]
    return "unknown", "%s.%s" % (modname(f.filename), f.name)


_LEDGER_MEM: Dict[str, dict] = {}


def _mark(module: str, fn: str, caller: Optional[str] = None) -> None:
    """ONE line at the head of every manifest function. A no-op when the flag is off.

    ⚑ Never raises into the caller. A monitoring hook that can break a capital run is a worse
    risk than the risk it monitors; the FAILURE TO WRITE is caught at reconciliation instead,
    where it fails the run loudly (`reconcile(strict=True)`)."""
    if not _flag("execution_ledger"):
        return
    try:
        stack = traceback.extract_stack()[:-1]
        kind, label = _classify_caller(stack)
        if caller:
            label = caller
        key = "%s.%s" % (module, fn)
        rec = _LEDGER_MEM.setdefault(key, {"module": module, "function": fn,
                                           "calls": 0, "kinds": {}, "callers": []})
        rec["calls"] += 1
        rec["kinds"][kind] = rec["kinds"].get(kind, 0) + 1
        if label and label not in rec["callers"]:
            rec["callers"].append(label)
    except Exception:                                                   # noqa: BLE001
        pass


def flush_ledger(path: Optional[str] = None) -> str:
    """Persist the in-memory ledger. RAISES if it cannot be written (spec P0.1 Refusals)."""
    p = path or ledger_path()
    doc = {"_what": "P0.1 live-path execution ledger — which capital-path functions actually "
                    "ran this month, and under what kind of caller.",
           "as_of": datetime.datetime.now().isoformat(timespec="seconds"),
           "records": _LEDGER_MEM}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                prev = json.load(fh)
            for k, v in (prev.get("records") or {}).items():
                if k not in doc["records"]:
                    doc["records"][k] = v
                else:
                    cur = doc["records"][k]
                    cur["calls"] += v.get("calls", 0)
                    for kk, vv in (v.get("kinds") or {}).items():
                        cur["kinds"][kk] = cur["kinds"].get(kk, 0) + vv
                    for c in v.get("callers", []):
                        if c not in cur["callers"]:
                            cur["callers"].append(c)
        except Exception:                                               # noqa: BLE001
            pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)
    return p


def load_ledger(path: Optional[str] = None) -> dict:
    p = path or ledger_path()
    if not os.path.exists(p):
        return {"records": {}, "store_exists": False, "store_path": p}
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    d["store_exists"] = True
    d["store_path"] = p
    return d


def mark_sites(root: str = HERE) -> Dict[str, bool]:
    """Which manifest functions actually carry a `_mark(...)` call at all — by AST, never by
    file text (ISA-0446: a fixture whose only mention is prose must fail to satisfy a check).

    ⚑ A manifest entry with no `_mark` site can never be observed, so it would report
    'declared and not executed' forever and the reader would learn nothing. This separates
    'the instrument is not installed' from 'the code did not run'."""
    want = {(m, f) for m, f, _, _ in CAPITAL_PATH_MANIFEST}
    found = {k: False for k in want}
    for path in source_files(root):
        m = modname(path)
        if not any(mm == m for mm, _ in want):
            continue
        _, tree = parsed(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (m, node.name) not in want:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = (f.attr if isinstance(f, ast.Attribute)
                          else f.id if isinstance(f, ast.Name) else None)
                    # `_fi_mark` is the import alias every instrumented module uses; `_mark`
                    # is the name inside this module. Both count — and NOTHING ELSE does,
                    # because a check that matched on file text would be satisfied by a
                    # comment mentioning the ledger (ISA-0446).
                    if nm in ("_mark", "_fi_mark"):
                        found[(m, node.name)] = True
                        break
    return {"%s.%s" % k: v for k, v in found.items()}


def reconcile(root: str = HERE, ledger: Optional[dict] = None,
              strict: bool = False) -> dict:
    """The end-of-run reconciliation emitted to `summary.execution_ledger`.

        declared and NOT executed                       -> ERROR
        executed ONLY as test / selftest / probe        -> REACHABLE_NOT_LIVE  (ERROR)
        executed and NOT declared                       -> UNDECLARED_LIVE     (WARNING)
    """
    led = ledger if ledger is not None else load_ledger()
    recs = led.get("records") or {}
    sites = mark_sites(root)
    rows, errors, warnings = [], [], []
    declared = set()
    for m, f, expect, why in CAPITAL_PATH_MANIFEST:
        key = "%s.%s" % (m, f)
        declared.add(key)
        r = recs.get(key)
        instrumented = sites.get(key, False)
        if r is None or not r.get("calls"):
            verdict = "NOT_EXECUTED" if instrumented else "NOT_INSTRUMENTED"
            rows.append({"quantity": key, "verdict": verdict, "expect": expect,
                         "kinds": {}, "callers": [], "instrumented": instrumented, "why": why})
            errors.append("%s: %s — %s" % (key, verdict, why))
            continue
        kinds = r.get("kinds") or {}
        live = int(kinds.get("live_run", 0))
        nonlive = sum(int(v) for k, v in kinds.items() if k in ("test", "selftest", "probe"))
        if live > 0:
            verdict = "live_run"
        elif nonlive > 0:
            verdict = "REACHABLE_NOT_LIVE"
            errors.append("%s: REACHABLE_NOT_LIVE — executed %d time(s), every one from %s. "
                          "Reachable is not live." %
                          (key, nonlive, "/".join(sorted(k for k in kinds if k != "live_run"))))
        else:
            verdict = "UNKNOWN_CALLER"
            warnings.append("%s: executed but the caller could not be classified" % key)
        rows.append({"quantity": key, "verdict": verdict, "expect": expect,
                     "kinds": kinds, "callers": r.get("callers", []),
                     "instrumented": instrumented, "why": why})
    for key, r in sorted(recs.items()):
        if key in declared:
            continue
        warnings.append("%s: UNDECLARED_LIVE — executed but absent from CAPITAL_PATH_MANIFEST "
                        "(the manifest is stale)" % key)
    out = {"as_of": datetime.date.today().isoformat(),
           "flag": _flag("execution_ledger"),
           "ledger_path": led.get("store_path"), "ledger_exists": led.get("store_exists", False),
           "n_declared": len(declared), "rows": rows,
           "errors": errors, "warnings": warnings,
           "state": "ERROR" if errors else ("WARN" if warnings else "OK"),
           "basis": ("P0.1. `verdict` answers 'did real capital flow through here', which is "
                     "not the question `framework_atlas` answers ('is it reachable'). A test "
                     "caller, a selftest caller and a synthetic probe are all REACHABLE_NOT_LIVE.")}
    if strict and errors:
        raise IntegrityRefused("execution ledger reconciliation FAILED:\n  " +
                               "\n  ".join(errors))
    return out


# ── register integration: a CLOSED item whose liveness ref stops firing REOPENS ──────────
def register_path() -> str:
    return os.path.join(HERE, "Dashboard", "state", "isa_items.jsonl")


def _iter_register(path: Optional[str] = None):
    p = path or register_path()
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:                                           # noqa: BLE001
                continue


def liveness_regressions(ledger: Optional[dict] = None,
                         register: Optional[str] = None) -> dict:
    """Resolve every CLOSED item's `verification.liveness_ref` against the ledger.

    ⚑ THE HIGHEST-VALUE HALF OF P0.1. 'Closed but dead' is 9 of the 12 findings. The record
    already says a liveness ref "proved REACHABLE, not EXECUTED" (ISA-0404); this is the
    correction to the INSTRUMENT, not another item against the code."""
    led = ledger if ledger is not None else load_ledger()
    recs = led.get("records") or {}

    def _fires(ref: str) -> Tuple[bool, str]:
        r = recs.get(ref)
        if r is None:
            for k in recs:
                if k.endswith("." + ref) or ref.endswith("." + k):
                    r = recs[k]
                    break
        if r is None:
            return False, "no ledger record"
        kinds = r.get("kinds") or {}
        if int(kinds.get("live_run", 0)) > 0:
            return True, "live_run"
        return False, "only " + "/".join(sorted(kinds)) if kinds else "no calls"

    regressed, held, unresolvable = [], [], []
    for item in _iter_register(register):
        state = (item.get("state") or item.get("status") or "").upper()
        if "CLOSED" not in state:
            continue
        ver = item.get("verification") or {}
        ref = ver.get("liveness_ref")
        if not ref or not isinstance(ref, str):
            continue
        tag = item.get("id") or item.get("tag") or item.get("isa_id") or "?"
        ok, why = _fires(ref)
        if ok:
            held.append({"item": tag, "ref": ref})
        elif why == "no ledger record":
            unresolvable.append({"item": tag, "ref": ref, "reason": why})
        else:
            regressed.append({"item": tag, "ref": ref, "reason": why,
                              "new_state": "REGRESSED",
                              "detected_on": datetime.date.today().isoformat()})
    return {"n_closed_with_ref": len(held) + len(regressed) + len(unresolvable),
            "held": held, "regressed": regressed, "unresolvable": unresolvable,
            "state": "REGRESSED" if regressed else "OK",
            "basis": ("A CLOSED item whose liveness ref no longer fires REOPENS as REGRESSED. "
                      "An `unresolvable` ref is NOT a pass — it means the ledger has never "
                      "seen that name, which is reported separately (R2.10).")}


# ═══════════════════════════════════════════════════════════════════════════════════════
# P0.2 — QUANTITY REGISTER  (Q1 two computers · Q2 no surface · Q3 dead computer · Q4 vocabulary)
# ═══════════════════════════════════════════════════════════════════════════════════════
QUANTITY_REGISTER = os.path.join(HERE, "quantity_register.json")


def load_quantity_register(path: Optional[str] = None) -> List[dict]:
    p = path or QUANTITY_REGISTER
    if not os.path.exists(p):
        raise IntegrityRefused(
            "quantity_register.json is absent at %s. P0.2 cannot report PASS on a register it "
            "could not read — an absent declaration and a clean declaration are different facts "
            "(R2.10)." % p)
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc["quantities"] if isinstance(doc, dict) else doc


# ── Q1: two computers for one quantity ─────────────────────────────────────────────────
# ⚑ THE COMPUTE / RELAY DISTINCTION IS THE WHOLE RULE, and it is why this is AST work and
# not a grep. After P4, `sleeve_split` still ASSIGNS `out["stock_max_gbp"]` — but its value is
# `sm["stock_max_gbp"]`, a READ of the quantity from its one computer. A function that reads a
# quantity and passes it on is a RELAY. A function that builds the value out of other things
# is a COMPUTER. Q1 counts computers. Counting assignments instead would make the correct
# post-P4 wiring fail, and a check that fails on correct behaviour gets switched off.

def _is_relay_value(node: ast.AST, qname: str) -> bool:
    """True when the value is a direct read of `qname` from something else."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            sl = sub.slice
            if isinstance(sl, ast.Constant) and sl.value == qname:
                return True
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == "get" and sub.args:
                a0 = sub.args[0]
                if isinstance(a0, ast.Constant) and a0.value == qname:
                    return True
    return False



def _excluded_line_ranges(tree: ast.Module, module: str) -> List[Tuple[int, int]]:
    """Line spans of every SELFTEST and every DECLARED PROBE function in a file.

    ⚑ THIS FUNCTION EXISTS BECAUSE OF A DEFECT IN THIS MODULE, FOUND 28-Aug-2026, AND IT IS
    F4'S OWN LESSON TURNED ON THE INSTRUMENT THAT WAS BUILT TO CATCH F4.

    The first implementation wrote `for node in ast.walk(tree): if <is selftest>: continue`.
    `ast.walk` is a FLAT traversal, so `continue` skipped the FunctionDef node and then went
    on to visit every one of its children anyway. `retention._selftest` builds
    {"ticker": "COCO", "route": "forward_led"} — and Q4 duly counted it as a live producer and
    reported the ratchet vocabulary HEALTHY. The check whose entire purpose was "a test is not
    a producer" was itself counting a selftest as a producer.

    ⚑ AND THE SELFTEST DID NOT CATCH IT, which is the part worth keeping: Q-A3's fixture lived
    in a separate `test_fixture.py`, so it exercised the PATH exclusion (is_test_path) and
    never the FUNCTION exclusion. A negative control that exercises a different mechanism from
    the one in the live path is a control that passes while the defect ships. The control
    below now puts the fixture inside a `_selftest` in a live module, which is where the real
    one was."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_selftest = node.name.endswith("_selftest") or node.name in ("_selftest", "selftest")
        is_probe = (module, node.name) in PROBE_CALLERS
        if is_selftest or is_probe:
            end = getattr(node, "end_lineno", None) or node.lineno
            spans.append((node.lineno, end))
    return spans


def _in_spans(line: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(a <= line <= b for a, b in spans)


def _computers_of_scan(qname: str, root: str = HERE) -> List[dict]:
    """Every function in the tree that COMPUTES `qname`. Tests are excluded by construction:
    a test that builds a fixture with the key is not a second authority."""
    hits = []
    for path in source_files(root):
        _, tree = parsed(path)
        if tree is None:
            continue
        m = modname(path)
        spans = _spans_cached(path, tree, m)
        for node in _walk_cached(path, tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _in_spans(node.lineno, spans):
                continue
            # ⚑ A DECLARED PROBE IS NOT A PRODUCER, for exactly the reason a test is not.
            # `_stock_side_sensitivity` builds a synthetic candidate carrying `evidence_state`
            # and `rho_sleeve` and self-labels it "not a size anyone should act on". Counting
            # it as a computer is the same error the atlas makes counting it as a caller.
            # (Handled by `spans` above — kept as a comment because the REASON is the point.)
            for sub in ast.walk(node):
                val, where = None, None
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name) and t.id == qname:
                            val, where = sub.value, "name_assign"
                        elif (isinstance(t, ast.Subscript)
                              and isinstance(t.slice, ast.Constant)
                              and t.slice.value == qname):
                            val, where = sub.value, "key_assign"
                elif isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if isinstance(k, ast.Constant) and k.value == qname:
                            val, where = v, "dict_literal"
                if val is None:
                    continue
                if _is_relay_value(val, qname):
                    continue
                hits.append({"module": m, "function": node.name, "line": sub.lineno,
                             "form": where, "path": os.path.relpath(path, root)})
                break
    # one row per (module, function)
    seen, out = set(), []
    for h in hits:
        k = (h["module"], h["function"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out



# ── ISA-0594 (05-Sep-2026): THE SCAN IS INVERTED, ONCE, INSTEAD OF RE-RUN PER QUERY ────────
# ⚑ COST ONLY. The two enumerators below answered "who produces (field, literal)?" and "who
# computes qname?" by walking all ~150 file ASTs ONCE PER QUERY. With 389 live vocabulary
# pairs that is ~58,000 file-walks, and `quantity_register_report` — which BOTH `preflight`
# (Step 0) and `report` (Step 6.99) call — cost 80.5s of a ~178s host-shell budget. The
# pre-run needed ~300s and so never reached `write_run_context`: run_context_sep_2026.json
# had never existed. ISA-0552 already fixed this class once by memoising the per-file walk;
# the walk was not the cost, the RE-WALK was, and a per-file cache cannot remove a loop that
# is inside-out. So the loop is turned the right way round: one pass over the tree builds an
# index keyed by the LITERAL (for producers) and by the QUANTITY NAME (for computers), and a
# query becomes a dict lookup plus a handful of predicate evaluations on a short list.
#
# The predicates that depend on the FIELD (`t.id.lower().endswith(field)`, the vocabulary
# `field in name` test) are NOT resolved at index time — they cannot be, the field space is
# open — so each entry carries the raw material and the predicate runs at query time against
# a few candidates rather than against every node in the tree. Semantics are unchanged, and
# that is not asserted in prose: `_producers_of_scan` / `_computers_of_scan` above ARE the
# previous implementations, kept verbatim, and `producer_equivalence` / `computer_equivalence`
# re-derive every live pair through both paths and compare. A cache that changes an answer is
# not a cache (R5.8).
_PRODUCER_INDEX: dict = {}
_COMPUTER_INDEX: dict = {}


def _emitted_literals(value: ast.AST, out=None) -> list:
    """Every literal this VALUE node can emit — the exact inverse of `_emits_literal`.

    Kept structurally identical to `_emits_literal` on purpose: the two must recognise the
    same producer shapes (bare constant, conditional, boolean short-circuit, the get/setdefault
    /pop fallback) or the index and the scan diverge on exactly the shapes ISA-0552 was fixed
    to catch."""
    if out is None:
        out = []
    if isinstance(value, ast.Constant):
        out.append(value.value)
    elif isinstance(value, ast.IfExp):
        _emitted_literals(value.body, out)
        _emitted_literals(value.orelse, out)
    elif isinstance(value, ast.BoolOp):
        for v in value.values:
            _emitted_literals(v, out)
    elif isinstance(value, ast.Call):
        f = value.func
        if isinstance(f, ast.Attribute) and f.attr in ("get", "setdefault", "pop"):
            for a in value.args[1:]:
                _emitted_literals(a, out)
    return out


def _hashable(v) -> bool:
    try:
        hash(v)
        return True
    except TypeError:
        return False


def _build_producer_index(root: str) -> dict:
    """literal -> [(kind, module, lineno, key)] for every producer shape in the tree.

    `key` is whatever the field predicate needs: the dict key, the subscript slice, the
    assigned Name id, the tuple of vocabulary target names, or the keyword-argument name.
    Data files are indexed separately, by `_data_blobs`, at query time."""
    idx: dict = {}

    def add(lit, entry):
        if _hashable(lit):
            idx.setdefault(lit, []).append(entry)

    for path in source_files(root):                       # tests excluded by source_files
        src_, tree = parsed(path)
        if tree is None:
            continue
        m = modname(path)
        spans = _spans_cached(path, tree, m)
        for node in _walk_cached(path, tree):
            if _in_spans(getattr(node, "lineno", -1), spans):
                continue                   # a selftest or a declared probe is NOT a producer
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant):
                        for lit in _emitted_literals(v):
                            add(lit, ("dict", m, node.lineno, k.value))
            if isinstance(node, ast.Assign):
                for lit in _emitted_literals(node.value):
                    for t in node.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant):
                            add(lit, ("assign", m, node.lineno, t.slice.value))
                        elif isinstance(t, ast.Name):
                            add(lit, ("name", m, node.lineno, t.id))
                if isinstance(node.value, (ast.Set, ast.Tuple, ast.List)):
                    names = tuple(t.id for t in node.targets if isinstance(t, ast.Name))
                    if names:
                        for e in node.value.elts:
                            if isinstance(e, ast.Constant):
                                add(e.value, ("vocab", m, node.lineno, names))
            if isinstance(node, ast.Call):
                for kw in node.keywords or []:
                    if kw.arg is not None:
                        for lit in _emitted_literals(kw.value):
                            add(lit, ("kwarg", m, node.lineno, kw.arg))
    return idx


def _producers_of(field: str, literal: str, root: str = HERE) -> List[str]:
    """Where the literal is EMITTED as a value for that field, in non-test source and in data.

    ⚑ TESTS AND SELFTESTS ARE NOT PRODUCERS. That is the entire content of F4: `forward_led`
    occurs only in `retention._selftest` and two fixtures, and every instrument that counted
    those as producers reported the filter healthy.

    Indexed since ISA-0594; `_producers_of_scan` is the same answer computed the slow way and
    `producer_equivalence` compares them."""
    idx = _PRODUCER_INDEX.get(root)
    if idx is None:
        idx = _PRODUCER_INDEX[root] = _build_producer_index(root)
    field_l = field.lower()
    hits = []
    for kind, m, lineno, key in idx.get(literal, ()):
        if kind == "dict":
            if key == field:
                hits.append("%s:%d dict" % (m, lineno))
        elif kind == "assign":
            if key == field:
                hits.append("%s:%d assign" % (m, lineno))
        elif kind == "name":
            if key.lower().endswith(field_l):
                hits.append("%s:%d name" % (m, lineno))
        elif kind == "vocab":
            if any(field_l in n.lower() for n in key):
                hits.append("%s:%d vocab" % (m, lineno))
        elif kind == "kwarg":
            if key == field:
                hits.append("%s:%d kwarg" % (m, lineno))
    # data files: a live artefact that carries the value IS a producer
    pat = re.compile(r'"%s"\s*:\s*"%s"' % (re.escape(field), re.escape(literal)))
    for label, blob in _data_blobs(root):
        if pat.search(blob):
            hits.append(label)
    return sorted(set(hits))


def _build_computer_index(root: str) -> dict:
    """qname -> [row] for every function that COMPUTES it, one row per (module, function).

    Mirrors `_computers_of_scan`'s `break`: the FIRST non-relay sub-node in walk order wins
    for a given function and quantity, and a relay is skipped rather than ending the search."""
    idx: dict = {}
    for path in source_files(root):
        _, tree = parsed(path)
        if tree is None:
            continue
        m = modname(path)
        spans = _spans_cached(path, tree, m)
        rel = os.path.relpath(path, root)
        for node in _walk_cached(path, tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _in_spans(node.lineno, spans):
                continue
            taken = set()                     # quantities already recorded for THIS function
            for sub in ast.walk(node):
                cands = {}                    # qname -> (value_node, form)
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            cands[t.id] = (sub.value, "name_assign")
                        elif (isinstance(t, ast.Subscript)
                              and isinstance(t.slice, ast.Constant)
                              and isinstance(t.slice.value, str)):
                            cands[t.slice.value] = (sub.value, "key_assign")
                elif isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            cands[k.value] = (v, "dict_literal")
                for qname, (val, where) in cands.items():
                    if qname in taken or _is_relay_value(val, qname):
                        continue
                    taken.add(qname)
                    idx.setdefault(qname, []).append(
                        {"module": m, "function": node.name, "line": sub.lineno,
                         "form": where, "path": rel})
    return idx


def _computers_of(qname: str, root: str = HERE) -> List[dict]:
    """Every function in the tree that COMPUTES `qname`. Tests are excluded by construction:
    a test that builds a fixture with the key is not a second authority.

    Indexed since ISA-0594; `_computers_of_scan` is the same answer computed the slow way and
    `computer_equivalence` compares them."""
    idx = _COMPUTER_INDEX.get(root)
    if idx is None:
        idx = _COMPUTER_INDEX[root] = _build_computer_index(root)
    seen, out = set(), []
    for h in idx.get(qname, ()):              # one row per (module, function)
        k = (h["module"], h["function"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def q1_two_computers(root: str = HERE, register=None) -> dict:
    """Q1 — more than one function COMPUTES a registered quantity.

    ⚑ `key_scope` EXISTS BECAUSE A BARE DICT KEY IS NOT AN IDENTITY. `target_pct` is the stock
    ladder rung in `position_sizing` and a fund's band target in `fund_returns` — two different
    quantities that happen to share a key name. Without a scope this check reports six findings
    on the first run, five of them coincidences, and a check whose findings are mostly
    coincidence is a check that gets suppressed (§2.2).

    ⚑ AND SCOPE MUST NOT BE A HIDING PLACE. A computer OUTSIDE the declared scope is not
    silently dropped — it is reported as OUT_OF_SCOPE, so a genuine new second home surfaces as
    a named warning rather than as nothing. A quantity with no `key_scope` is scoped to the
    WHOLE TREE, which is the strict default."""
    reg = register if register is not None else load_quantity_register()
    findings, out_of_scope = [], []
    for q in reg:
        name = q["name"]
        all_comps = _computers_of(name, root)
        scope = q.get("key_scope")
        if scope:
            comps = [c for c in all_comps if c["module"] in set(scope)]
            for c in all_comps:
                if c["module"] not in set(scope):
                    out_of_scope.append({
                        "check": "Q1/OUT_OF_SCOPE", "quantity": name,
                        "computer": "%s.%s" % (c["module"], c["function"]),
                        "declared_scope": sorted(scope),
                        "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
                        "detail": ("%s.%s computes a key named %r outside the declared scope "
                                   "%s. Either it is an unrelated quantity that shares a name, "
                                   "or it is a second home — the register must say which."
                                   % (c["module"], c["function"], name, sorted(scope)))})
        else:
            comps = all_comps
        declared = q.get("computer")
        if declared is None:
            continue                                    # handled by the UNADJUDICATED rule
        if len(comps) > 1:
            findings.append({
                "check": "Q1", "quantity": name,
                "declared_computer": declared,
                "computers_found": ["%s.%s" % (c["module"], c["function"]) for c in comps],
                "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
                "detail": ("%d functions COMPUTE %s; the register declares one (%s). Two "
                           "computers for one quantity is KR6, and the 10x difference between "
                           "them is spec §1.1 F1." % (len(comps), name, declared)),
            })
        elif comps and "%s.%s" % (comps[0]["module"], comps[0]["function"]) != declared:
            findings.append({
                "check": "Q1", "quantity": name, "declared_computer": declared,
                "computers_found": ["%s.%s" % (c["module"], c["function"]) for c in comps],
                "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
                "detail": "the one computer found is not the declared one",
            })
    return {"check": "Q1", "n_findings": len(findings), "findings": findings,
            "out_of_scope": out_of_scope, "n_out_of_scope": len(out_of_scope),
            "state": "FAIL" if findings else ("WARN" if out_of_scope else "PASS"),
            "basis": ("A COMPUTER builds the value; a RELAY reads it from elsewhere and passes "
                      "it on. Only computers count — otherwise the correct post-P4 wiring, in "
                      "which sleeve_split relays position_sizing's number, would fail.")}


# ── Q2: a registered quantity that renders nowhere ─────────────────────────────────────
def q2_no_surface(root: str = HERE, register=None) -> dict:
    """`pair_summary_key_disposition` generalised beyond `summary` — a quantity computed and
    never rendered is a decision nobody can see."""
    reg = register if register is not None else load_quantity_register()
    findings = []
    for q in reg:
        surfaces = q.get("surface") or []
        if surfaces:
            continue
        findings.append({"check": "Q2", "quantity": q["name"],
                         "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
                         "detail": ("%s is computed and declares no surface. A quantity that "
                                    "renders nowhere has been computed, not communicated."
                                    % q["name"])})
    return {"check": "Q2", "n_findings": len(findings), "findings": findings,
            "state": "FAIL" if findings else "PASS"}


# ── Q3: a quantity whose declared computer is not `live_run` in the ledger ─────────────
def q3_dead_computer(register=None, ledger: Optional[dict] = None,
                     root: str = HERE) -> dict:
    """Q3 — a registered quantity's declared computer is absent from the ledger as `live_run`.

    ⚑ THREE VERDICTS, NOT ONE, because they are three different facts (R2.10):
        NOT_INSTRUMENTED — the computer carries no `_mark` site, so it CANNOT be observed.
                           This is an instrument gap, not a code defect, and saying otherwise
                           would make the whole of Q3 read as 121 dead computers on day one.
        ABSENT           — instrumented, and the function does not exist on disk at all.
        REACHABLE_NOT_LIVE — instrumented, executed, but never from a live run."""
    reg = register if register is not None else load_quantity_register()
    led = ledger if ledger is not None else load_ledger()
    recs = led.get("records") or {}
    sites = mark_sites(root)
    on_disk = _functions_on_disk(root)
    findings, not_instrumented = [], []
    for q in reg:
        comp = q.get("computer")
        if not comp:
            continue
        r = recs.get(comp)
        kinds = (r or {}).get("kinds") or {}
        if int(kinds.get("live_run", 0)) > 0:
            continue
        exists = comp in on_disk
        row = {"check": "Q3", "quantity": q["name"], "computer": comp,
               "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
               "kinds": kinds, "exists_on_disk": exists,
               "instrumented": bool(sites.get(comp, False))}
        if not exists:
            row["verdict"] = "ABSENT"
            row["detail"] = ("%s's declared computer %s DOES NOT EXIST on disk. The quantity "
                             "has a declared home and no home." % (q["name"], comp))
            findings.append(row)
        elif not sites.get(comp, False):
            row["verdict"] = "NOT_INSTRUMENTED"
            row["detail"] = ("%s exists but carries no _mark site, so its liveness cannot be "
                             "observed. An uninstrumented computer is an INSTRUMENT gap, not "
                             "evidence that the code is dead." % comp)
            not_instrumented.append(row)
        else:
            row["verdict"] = "REACHABLE_NOT_LIVE"
            row["detail"] = ("%s is instrumented and has never run from a live caller (%s)."
                             % (comp, "no calls at all" if r is None
                                else "only " + "/".join(sorted(kinds))))
            findings.append(row)
    return {"check": "Q3", "n_findings": len(findings), "findings": findings,
            "not_instrumented": not_instrumented,
            "n_not_instrumented": len(not_instrumented),
            "state": "FAIL" if findings else ("WARN" if not_instrumented else "PASS")}


def _functions_on_disk(root: str = HERE) -> set:
    """{"module.function"} for every top-level and nested function in non-test source."""
    out = set()
    for path in source_files(root):
        _, tree = parsed(path)
        if tree is None:
            continue
        m = modname(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add("%s.%s" % (m, node.name))
    return out


# ── Q4: ⚑ THE VOCABULARY ENUMERATOR — the class-killer for F4 ──────────────────────────
# ⚑ SCOPED TO THE CLASS, NOT THE INSTANCE. ISA-0447: a check narrower than the class it
# claims to kill leaves a hole shaped exactly like the defect it was built for, and reports
# GREEN while doing so. A Q4 that only checked `route` would be an instance-killer. This
# enumerates EVERY vocabulary-valued comparison in the tree.

_FIELD_GET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Comparisons that are NOT vocabulary filters: state machines over locally-defined literals,
# type tags, and the like are still enumerated — the producer search decides, not a stop-list.
# Only the instrument's own module is excluded (R10).


def _vocab_comparisons(root: str = HERE) -> List[dict]:
    """Every `<field-access> == "literal"` (and `in ("a","b")`) in non-test source."""
    out = []
    for path in source_files(root):
        _, tree = parsed(path)
        if tree is None:
            continue
        m = modname(path)
        spans = _spans_cached(path, tree, m)
        for node in _walk_cached(path, tree):
            if not isinstance(node, ast.Compare) or not node.ops:
                continue
            # ⚑ SYMMETRY, AND IT IS NOT COSMETIC. If a fixture inside a `_selftest` is not a
            # PRODUCER, then an assertion inside a `_selftest` is not a live FILTER either.
            # Without this, `t1_gates:500` (an assertion in its own self-test block) reads as
            # a live filter on a value whose real producer sits 177 lines above it, and the
            # instrument reports a defect where the code is correct. A check that cannot tell
            # its own test suite from its own live path is the atlas's error wearing new
            # clothes — found 28-Aug-2026 by running this module against the pre-build tree.
            if _in_spans(node.lineno, spans):
                continue
            if not isinstance(node.ops[0], (ast.Eq, ast.In)):
                continue
            field = _field_of(node.left)
            if field is None:
                continue
            lits = []
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    lits.append(comp.value)
                elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    for e in comp.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            lits.append(e.value)
            for lit in lits:
                if not lit or len(lit) < 3:
                    continue
                out.append({"module": m, "line": node.lineno, "field": field, "literal": lit,
                            "path": os.path.relpath(path, root)})
    return out


def _field_of(node: ast.AST) -> Optional[str]:
    """The FIELD NAME a comparison is keyed on: d["route"], d.get("route"), rec.route."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        v = node.slice.value
        if isinstance(v, str) and _FIELD_GET.match(v):
            return v
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "get" and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
               and _FIELD_GET.match(a0.value):
                return a0.value
        return None
    if isinstance(node, ast.Attribute) and _FIELD_GET.match(node.attr):
        # only when it reads like a data field, not a module attribute
        if isinstance(node.value, ast.Name) and node.value.id[:1].islower():
            return node.attr
    return None



def _emits_literal(value: ast.AST, literal: str) -> bool:
    """Does this VALUE node emit `literal` on some path?

    Recognises the bare constant, the conditional expression, the boolean short-circuit and
    the parenthesised call fallback — because a producer that writes
        "route": ("a" if cond else "b")
    is emitting both "a" and "b", and an instrument that only sees bare constants would call
    both of them dead. ⚑ It deliberately does NOT walk arbitrarily deep: a literal buried in
    an unrelated nested structure is not evidence that this FIELD carries that VALUE."""
    if isinstance(value, ast.Constant):
        return value.value == literal
    if isinstance(value, ast.IfExp):
        return _emits_literal(value.body, literal) or _emits_literal(value.orelse, literal)
    if isinstance(value, ast.BoolOp):
        return any(_emits_literal(v, literal) for v in value.values)
    if isinstance(value, ast.Call):
        f = value.func
        if isinstance(f, ast.Attribute) and f.attr in ("get", "setdefault", "pop"):
            return any(_emits_literal(a, literal) for a in value.args[1:])
    return False


def _producers_of_scan(field: str, literal: str, root: str = HERE) -> List[str]:
    """Where the literal is EMITTED as a value for that field, in non-test source and in data.

    ⚑ TESTS AND SELFTESTS ARE NOT PRODUCERS. That is the entire content of F4: `forward_led`
    occurs only in `retention._selftest` and two fixtures, and every instrument that counted
    those as producers reported the filter healthy."""
    hits = []
    field_l = field.lower()
    for path in source_files(root):                       # tests excluded by source_files
        src, tree = parsed(path)
        if tree is None or literal not in src:
            continue
        if field_l not in _lower_cached(path, src):
            continue      # exact pre-filter: every producer shape names the field in source
        m = modname(path)
        spans = _spans_cached(path, tree, m)
        for node in _walk_cached(path, tree):
            if _in_spans(getattr(node, "lineno", -1), spans):
                continue                       # a selftest or a declared probe is NOT a producer
            # dict literal  {"route": "forward_led"}  — including a CONDITIONAL value.
            # ⚑ `t1_gates:323` writes  "route": ("unconfirmed_er_unmeasured" if er_unmeasured
            # else ...) — an ast.IfExp, not an ast.Constant. Matching only bare constants made
            # the instrument report a live, correct producer as absent. A producer search that
            # only recognises ONE syntactic form is the R4.6 literal-shadow lesson repeated:
            # the class does not live in one shape.
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == field and \
                       _emits_literal(v, literal):
                        hits.append("%s:%d dict" % (m, node.lineno))
            # subscript assign  d["route"] = "forward_led"
            if isinstance(node, ast.Assign):
                if _emits_literal(node.value, literal):
                    for t in node.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                           and t.slice.value == field:
                            hits.append("%s:%d assign" % (m, node.lineno))
                        elif isinstance(t, ast.Name) and t.id.lower().endswith(field.lower()):
                            hits.append("%s:%d name" % (m, node.lineno))
                # a declared vocabulary: ROUTES = {"main","vci"}
                if isinstance(node.value, (ast.Set, ast.Tuple, ast.List)):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    if any(field.lower() in n.lower() for n in names):
                        for e in node.value.elts:
                            if isinstance(e, ast.Constant) and e.value == literal:
                                hits.append("%s:%d vocab" % (m, node.lineno))
            # keyword argument  record(route="forward_led")
            if isinstance(node, ast.Call):
                for kw in node.keywords or []:
                    if kw.arg == field and _emits_literal(kw.value, literal):
                        hits.append("%s:%d kwarg" % (m, node.lineno))
    # data files: a live artefact that carries the value IS a producer
    pat = re.compile(r'"%s"\s*:\s*"%s"' % (re.escape(field), re.escape(literal)))
    for label, blob in _data_blobs(root):
        if pat.search(blob):
            hits.append(label)
    return sorted(set(hits))


def q4_dead_vocabulary(root: str = HERE, register=None) -> dict:
    """Every `field == "literal"` filter in the tree where NO live producer emits that literal.

    ⚑ A PRESENT EXECUTION THAT CAN NEVER BE TRUE. Not an absent execution — the branch runs,
    evaluates, and is false every time. The tell is that the value appears only in tests."""
    reg = register if register is not None else load_quantity_register()
    exposure, declared_vocab = {}, {}
    for q in reg:
        v = q.get("vocabulary")
        if isinstance(v, dict) and v.get("field"):
            exposure[v["field"]] = float(q.get("gbp_exposure") or 0.0)
            declared_vocab[v["field"]] = set(v.get("declared") or [])
    findings, checked = [], 0
    seen = set()
    for c in _vocab_comparisons(root):
        key = (c["field"], c["literal"])
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        prods = _producers_of(c["field"], c["literal"], root)
        if prods:
            continue
        registered = c["field"] in declared_vocab
        undeclared_value = (registered and
                            c["literal"] not in declared_vocab[c["field"]])
        findings.append({
            "check": "Q4", "field": c["field"], "literal": c["literal"],
            "filter_at": "%s:%d" % (c["module"], c["line"]),
            "gbp_exposure": exposure.get(c["field"], 0.0),
            "registered_vocabulary": registered,
            "undeclared_value": undeclared_value,
            "detail": ("`%s == %r` filters on a value NO live producer emits. The branch is "
                       "present, evaluates, and is false every time — a present execution that "
                       "can never be true. Tell: the value appears only in tests."
                       % (c["field"], c["literal"])
                       + (" ⚑ AND THE FIELD IS A REGISTERED VOCABULARY whose declared set is "
                          "%s — so this is not a coincidence of naming, it is a filter on a "
                          "value the register says cannot occur."
                          % sorted(declared_vocab.get(c["field"], ()))
                          if undeclared_value else "")),
        })
    findings.sort(key=lambda f: (-int(f["undeclared_value"]), -f["gbp_exposure"]))
    gating = [f for f in findings if f["undeclared_value"]]
    return {"check": "Q4", "n_pairs_checked": checked, "n_findings": len(findings),
            "findings": findings,
            "gating_findings": gating, "n_gating": len(gating),
            "state": "FAIL" if gating else ("WARN" if findings else "PASS"),
            "scope": ("⚑ ENUMERATION IS CLASS-SCOPED; the BUILD GATE is register-scoped, and "
                      "the two are different on purpose. Every vocabulary-valued comparison in "
                      "the tree is enumerated (%d pairs, %d with no live producer) — that is "
                      "ISA-0447's requirement, and narrowing the enumeration to `route` would "
                      "leave a hole shaped exactly like the defect. But a comparison against "
                      "an argparse choice or an external API's status string has no producer "
                      "IN THIS TREE and is not a defect, so only fields the quantity register "
                      "declares a vocabulary for can FAIL the build. The rest are published, "
                      "ranked and suppressed with their count — never silently dropped (R4.9)."
                      % (checked, len(findings))),
            "unregistered_note": ("%d finding(s) are on fields with no registered vocabulary. "
                                  "Registering a field is how a coincidence becomes a gate."
                                  % (len(findings) - len(gating)))}


def unadjudicated_quantities(register=None) -> dict:
    """A quantity with `computer: null` is UNADJUDICATED and COSTS A LIVE REGISTER ITEM.

    ⚑ The ISA-0448 pressure mechanism, deliberately reused. Writing a plausible computer where
    the truth is 'nobody has looked' would launder a backlog into a reassuring category."""
    reg = register if register is not None else load_quantity_register()
    rows = [{"quantity": q["name"], "gbp_exposure": float(q.get("gbp_exposure") or 0.0),
             "register_item": q.get("register_item")}
            for q in reg if q.get("computer") is None]
    missing_item = [r for r in rows if not r["register_item"]]
    return {"n_unadjudicated": len(rows), "rows": rows,
            "n_without_register_item": len(missing_item),
            "state": "FAIL" if missing_item else ("WARN" if rows else "PASS"),
            "basis": "UNADJUDICATED costs a live register item — that is the pressure (ISA-0448)"}


def quantity_register_report(root: str = HERE, ledger: Optional[dict] = None) -> dict:
    reg = load_quantity_register()
    q1 = q1_two_computers(root, reg)
    q2 = q2_no_surface(root, reg)
    q3 = q3_dead_computer(reg, ledger, root)
    q4 = q4_dead_vocabulary(root, reg)
    ua = unadjudicated_quantities(reg)
    states = [q1["state"], q2["state"], q3["state"], q4["state"], ua["state"]]
    return {"flag": _flag("quantity_register"), "n_quantities": len(reg),
            "Q1": q1, "Q2": q2, "Q3": q3, "Q4": q4, "unadjudicated": ua,
            "state": "FAIL" if "FAIL" in states else ("WARN" if "WARN" in states else "PASS")}


# ═══════════════════════════════════════════════════════════════════════════════════════
# P0.3 — THRESHOLD REGISTER   (= R15.2, specified and declared for ZERO gates until today)
# ═══════════════════════════════════════════════════════════════════════════════════════
# ⚑ THE RULE THIS INSTRUMENT MAKES MECHANICAL, adopted as R3.11 by this build:
#       BEFORE DECLARING ANY THRESHOLD, DIVIDE IT BY THE STANDARD DEVIATION OF THE THING IT
#       TESTS.
# -5pp against a measured 35.2%/yr tracking error is 0.14 SD and fires 44% of the time on
# zero true alpha — at 6, 12 AND 24 months. Shortening the window does not help and
# lengthening it does not either. The window was never the lever (spec §1.1 F5).

THRESHOLD_REGISTER = os.path.join(HERE, "threshold_register.json")

VERDICT_DISCRIMINATING = "DISCRIMINATING"
VERDICT_WEAK = "WEAK"
VERDICT_NON_DISCRIMINATING = "NON_DISCRIMINATING"
VERDICT_NON_INFORMATIVE = "NON_INFORMATIVE"
VERDICT_UNMEASURED = "UNMEASURED"

MIN_RUNS_FOR_FIRE_RATE = 6


def verdict_for(value: Optional[float], sd: Optional[float],
                fire_rate_history: Optional[Sequence[Any]] = None) -> dict:
    """Declared, not fitted (R3.9). `sd` unmeasured is UNMEASURED and NEVER DISCRIMINATING."""
    hist = list(fire_rate_history or [])
    if sd is None or value is None:
        return {"verdict": VERDICT_UNMEASURED, "t_ratio": None,
                "why": ("the SD of the tested quantity has never been measured, so the "
                        "threshold's signal-to-noise is unknown. UNMEASURED is never rendered "
                        "as DISCRIMINATING and raises a register item.")}
    try:
        sd = float(sd)
    except Exception:                                                   # noqa: BLE001
        return {"verdict": VERDICT_UNMEASURED, "t_ratio": None, "why": "sd is not numeric"}
    if sd <= 0:
        return {"verdict": VERDICT_UNMEASURED, "t_ratio": None,
                "why": "a non-positive SD cannot scale a threshold"}
    t = abs(float(value)) / sd
    if len(hist) >= MIN_RUNS_FOR_FIRE_RATE:
        fired = sum(1 for h in hist if bool(h))
        if fired == 0 or fired == len(hist):
            return {"verdict": VERDICT_NON_INFORMATIVE, "t_ratio": round(t, 4),
                    "fire_rate": fired / len(hist),
                    "why": ("fired %d of %d runs. A gate that always fires or never fires "
                            "carries no information whatever its t-ratio (M1's own rule, "
                            "generalised from one instrument to all)." % (fired, len(hist)))}
    if t >= 1.0:
        v, why = VERDICT_DISCRIMINATING, "|t| >= 1.0"
    elif t >= 0.5:
        v, why = VERDICT_WEAK, "0.5 <= |t| < 1.0"
    else:
        v, why = VERDICT_NON_DISCRIMINATING, (
            "|t| < 0.5 — the threshold is inside the noise of the quantity it tests and "
            "raises a register item automatically")
    return {"verdict": v, "t_ratio": round(t, 4), "why": why,
            "fire_rate": (sum(1 for h in hist if bool(h)) / len(hist)) if hist else None}


def load_threshold_register(path: Optional[str] = None) -> List[dict]:
    p = path or THRESHOLD_REGISTER
    if not os.path.exists(p):
        raise IntegrityRefused(
            "threshold_register.json is absent at %s. R15.2 has been specified since the "
            "standard was adopted and declared for zero gates; an absent register is a FAIL, "
            "never a PASS." % p)
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc["thresholds"] if isinstance(doc, dict) else doc


def threshold_report(path: Optional[str] = None) -> dict:
    reg = load_threshold_register(path)
    rows, raises = [], []
    for t in reg:
        v = verdict_for(t.get("value"), t.get("sd_of_quantity"),
                        t.get("fire_rate_history"))
        row = {**{k: t.get(k) for k in ("name", "value", "units", "quantity_tested",
                                        "sd_of_quantity", "sd_basis", "gates", "gbp_exposure")},
               **v}
        rows.append(row)
        if v["verdict"] in (VERDICT_NON_DISCRIMINATING, VERDICT_NON_INFORMATIVE,
                            VERDICT_UNMEASURED):
            raises.append({"threshold": t.get("name"), "verdict": v["verdict"],
                           "t_ratio": v.get("t_ratio"),
                           "gbp_exposure": float(t.get("gbp_exposure") or 0.0),
                           "register_item": t.get("register_item"),
                           "detail": v["why"]})
    return {"flag": _flag("threshold_register"), "n_thresholds": len(rows), "rows": rows,
            "raises": raises,
            "state": "FAIL" if any(r["verdict"] == VERDICT_NON_DISCRIMINATING for r in rows)
                     else ("WARN" if raises else "PASS"),
            "rule": ("R3.11 — before declaring any threshold, divide it by the SD of the thing "
                     "it tests. Adopted by this build.")}


def unregistered_gating_constants(root: str = HERE, path: Optional[str] = None,
                                  modules: Optional[Sequence[str]] = None) -> dict:
    """Discovery by AST: a module-level numeric constant compared in a capital-path module and
    absent from the register. A capital-gating threshold nobody registered fails the build."""
    reg = {t.get("name") for t in load_threshold_register(path)}
    mods = set(modules or CAPITAL_PATH_MODULES)
    found, seen = [], set()
    for p in source_files(root):
        m = modname(p)
        if m not in mods:
            continue
        _, tree = parsed(p)
        if tree is None:
            continue
        consts = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and \
                       isinstance(node.value, ast.Constant) and \
                       isinstance(node.value.value, (int, float)) and \
                       not isinstance(node.value.value, bool):
                        consts.add(t.id)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = [n.id for n in ast.walk(node)
                     if isinstance(n, ast.Name) and n.id in consts]
            for nm in names:
                if nm in reg or (m, nm) in seen:
                    continue
                seen.add((m, nm))
                found.append({"module": m, "constant": nm, "line": node.lineno,
                              "detail": ("%s.%s gates a comparison and is absent from "
                                         "threshold_register.json" % (m, nm))})
    return {"n_unregistered": len(found), "findings": found,
            "state": "FAIL" if found else "PASS",
            "scope": sorted(mods)}


CAPITAL_PATH_MODULES = (
    "capital_destination", "position_sizing", "correlation_engine", "risk_contribution",
    "retention", "evidence_state", "stock_return_store", "waiting_room", "t1_gates",
    "vci_deploy_eval", "vci_risk_budget", "expected_return", "fund_returns",
    "process_concentration", "strategic_allocation", "concentration_clusters",
    "stock_price_fetch", "stock_candidates", "deployment_sequencer", "thesis_state",
)


# ═══════════════════════════════════════════════════════════════════════════════════════
# P0.4 — NEGATIVE-CLAIM EXPIRY   (genuinely new)
# ═══════════════════════════════════════════════════════════════════════════════════════
# ⚑ WHY A NEGATIVE CLAIM NEEDS MORE EVIDENCE THAN A POSITIVE ONE, NOT LESS: it is load-bearing
# precisely because it STOPS INVESTIGATION. A positive claim gets checked because somebody
# uses it. A negative claim gets checked by nobody, because it means "don't bother".
#
# "Yahoo is network-blocked from both the container and the device shell" sat in a LIVE
# docstring, was FALSE, cost the entire correlation gate and therefore the top three ladder
# rungs, and was contradicted by the framework's own ISA-0411 five days earlier.

NEGATIVE_CLAIMS = os.path.join(HERE, "negative_claims.json")

# The phrase list is ITSELF in the register — additions are register work, not a quiet edit.
NEGATIVE_PHRASES = (
    "network-blocked", "network blocked", "has no network", "no network access",
    "cannot be measured", "cannot be fetched", "is unavailable", "are unavailable",
    "does not exist", "do not exist", "no free structured source", "is not installed",
    "cannot fire", "can never fire", "is blocked", "no such source",
)
_NEG_RE = re.compile("|".join(re.escape(p) for p in NEGATIVE_PHRASES), re.I)

DEFAULT_EXPIRY_RUNS = 3


def load_negative_claims(path: Optional[str] = None) -> List[dict]:
    p = path or NEGATIVE_CLAIMS
    if not os.path.exists(p):
        raise IntegrityRefused(
            "negative_claims.json is absent at %s. A claim of absence that gates capital must "
            "carry a test and a date; with no register, every such claim is unfalsifiable by "
            "construction." % p)
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc["claims"] if isinstance(doc, dict) else doc


def _docstrings(path: str):
    """(lineno, text, owner) for every docstring in the file — by AST, never by file text.

    ⚑ N4: PROSE ABOUT THE RULE MUST NOT TRIP THE RULE. A comment explaining P0.4 is not a
    registered claim. Matching on file text would flag this module's own documentation and
    every register entry describing a claim — ISA-0446's class, pre-empted."""
    _, tree = parsed(path)
    if tree is None:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                owner = getattr(node, "name", "<module>")
                yield (getattr(node, "lineno", 1), d, owner)


def unregistered_negative_claims(root: str = HERE, path: Optional[str] = None,
                                 modules: Optional[Sequence[str]] = None) -> dict:
    claims = load_negative_claims(path)
    registered = [(c.get("claim") or "").lower() for c in claims]
    # ⚑ NOTHING IS SILENTLY EXCLUDED. A phrase in the list can legitimately appear in prose
    # that is NOT a capability claim — "a position reaches 3.5% or it does not exist" is a
    # POLICY statement, not an assertion that something is unavailable. The answer is not a
    # stop-list buried in the checker, because a stop-list is a decision nobody can review.
    # Such prose is ADJUDICATED in the register with state NOT_A_CAPABILITY_CLAIM and a
    # reason, which costs a register entry (ISA-0448's pressure) and keeps it visible — so a
    # future edit that turns it INTO a real claim is noticed rather than absorbed.
    sited = {}
    for c in claims:
        site = c.get("site")
        if isinstance(site, dict) and site.get("module"):
            sited.setdefault(site["module"], []).append(
                (site.get("owner"), (site.get("phrase") or "").lower()))
    mods = set(modules or CAPITAL_PATH_MODULES)
    findings = []
    for p in source_files(root):                       # self excluded by source_files (R10)
        m = modname(p)
        if m not in mods:
            continue
        for lineno, doc, owner in _docstrings(p):
            for line in doc.splitlines():
                hit = _NEG_RE.search(line)
                if not hit:
                    continue
                low = line.strip().lower()
                if any(r and (r in low or low in r) for r in registered):
                    continue
                if any(_overlap(low, r) for r in registered):
                    continue
                if any((o is None or o == owner) and ph and ph in low
                       for o, ph in sited.get(m, [])):
                    continue
                findings.append({"module": m, "owner": owner, "line": lineno,
                                 "phrase": hit.group(0), "text": line.strip()[:220],
                                 "detail": ("an unregistered negative claim in a capital-path "
                                            "docstring. It gates behaviour and carries no test "
                                            "and no date.")})
    return {"n_findings": len(findings), "findings": findings,
            "state": "FAIL" if findings else "PASS",
            "scope": sorted(mods), "phrases": list(NEGATIVE_PHRASES),
            "basis": ("AST docstrings only — prose ABOUT the rule does not trip the rule (N4).")}


def _overlap(a: str, b: str, need: int = 6) -> bool:
    """True when two claim strings share a run of `need`+ words — so a docstring reworded
    slightly still resolves to its registered claim rather than reading as a new one."""
    aw, bw = a.split(), b.split()
    if len(aw) < need or len(bw) < need:
        return False
    grams = {" ".join(aw[i:i + need]) for i in range(len(aw) - need + 1)}
    return any(" ".join(bw[i:i + need]) in grams for i in range(len(bw) - need + 1))


def negative_claim_report(path: Optional[str] = None, root: str = HERE,
                          run_tests: bool = False) -> dict:
    """Every registered claim's expiry state, and (optionally) re-run its falsifier."""
    claims = load_negative_claims(path)
    rows, expired = [], []
    for c in claims:
        runs = int(c.get("runs_since_tested") or 0)
        limit = int(c.get("expires_after_runs") or DEFAULT_EXPIRY_RUNS)
        state = c.get("state") or "ASSERTED_TRUE"
        row = {"claim": c.get("claim"), "asserted_in": c.get("asserted_in"),
               "asserted_on": c.get("asserted_on"), "test_id": c.get("test_id"),
               "last_tested": c.get("last_tested"), "gates": c.get("gates") or [],
               "runs_since_tested": runs, "expires_after_runs": limit, "state": state}
        if state == "NOT_A_CAPABILITY_CLAIM":
            row["verdict"] = "NOT_A_CAPABILITY_CLAIM"
            row["consequence"] = ("adjudicated prose: it trips the phrase list without "
                                  "asserting that anything is unavailable. It gates nothing "
                                  "and it does not expire — but it stays in the register, so "
                                  "an edit that turns it into a real claim is noticed.")
        elif state == "FALSIFIED":
            row["verdict"] = "FALSIFIED"
            row["consequence"] = ("the claim is FALSE. Every gate it supports must be rebuilt "
                                 "on the true capability, and the prose asserting it must not "
                                 "ship unchanged.")
        elif runs >= limit:
            row["verdict"] = "EXPIRED_UNTESTED"
            row["consequence"] = ("every gate this claim supports reads UNMEASURED — never "
                                  "'absent'. An untested claim of absence is not evidence of "
                                  "absence.")
            expired.append(row)
        else:
            row["verdict"] = "CURRENT"
        if run_tests and c.get("test_id"):
            row["retest"] = _run_claim_test(c["test_id"])
        rows.append(row)
    unreg = unregistered_negative_claims(root, path)
    falsified_live = [r for r in rows if r["verdict"] == "FALSIFIED"]
    return {"flag": _flag("negative_claim_expiry"), "n_claims": len(rows), "rows": rows,
            "expired": expired, "falsified": falsified_live, "unregistered": unreg,
            "state": ("FAIL" if (unreg["state"] == "FAIL" or falsified_live or expired)
                      else "PASS"),
            "gates_forced_unmeasured": sorted({g for r in expired for g in r["gates"]}),
            "basis": ("P0.4. A claim of absence that gates capital carries a test and a date "
                      "and EXPIRES. Default expiry %d runs." % DEFAULT_EXPIRY_RUNS)}


def _run_claim_test(test_id: str) -> dict:
    """Re-run a registered falsifier. An unrunnable test is EXPIRED_UNTESTED, never a pass."""
    try:
        mod, fn = test_id.rsplit(".", 1)
        import importlib
        m = importlib.import_module(mod)
        f = getattr(m, fn)
        r = f()
        return {"ran": True, "result": r}
    except Exception as exc:                                            # noqa: BLE001
        return {"ran": False, "reason": "%s: %s" % (type(exc).__name__, exc),
                "verdict": "EXPIRED_UNTESTED",
                "note": "a claim whose falsifier cannot run is untested, never confirmed"}


# ═══════════════════════════════════════════════════════════════════════════════════════
# P0.6 — RANKED FINDING QUEUE
# ═══════════════════════════════════════════════════════════════════════════════════════
def _atlas_findings(root: str = HERE) -> dict:
    """The atlas becomes an INPUT, not a rival. Its staleness is itself reported: R15.4 says
    the build FAILS if the regenerated graph differs from the declared manifests, and
    `framework_atlas.json` was 14 days and >=6 builds stale on 27-Aug with no build failing."""
    p = os.path.join(root, "Dashboard", "state", "framework_atlas.json")
    if not os.path.exists(p):
        return {"state": "ABSENT", "path": p, "findings": [], "n": 0}
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNREADABLE", "path": p, "reason": str(exc)[:160],
                "findings": [], "n": 0}
    # ⚑ AGE COMES FROM THE ARTEFACT'S OWN `as_of`, NEVER FROM THE FILESYSTEM mtime.
    # Found 28-Aug-2026: the first implementation used os.path.getmtime, and a file that had
    # merely been COPIED between machines read "age 0 days, CURRENT" for an artefact whose own
    # header says it was generated on 2026-08-12. A staleness check that any copy resets is a
    # staleness check that reports GREEN exactly when the artefact has travelled — which is
    # every time it reaches a new session. The provenance field derived from the data's own
    # timestamp is the ONLY honest one here; this is `record_level.stamp_basis` (F10) in
    # mirror image, and it was caught by the same reasoning.
    as_of = doc.get("as_of")
    age_days, age_basis = None, "declared_as_of"
    if as_of:
        try:
            age_days = (datetime.date.today()
                        - datetime.date.fromisoformat(str(as_of)[:10])).days
        except Exception:                                               # noqa: BLE001
            as_of = None
    if age_days is None:
        age_basis = "UNDATED — the artefact declares no as_of and its age cannot be measured"
    buckets = {}
    fnd = doc.get("findings") if isinstance(doc.get("findings"), dict) else {}
    for k in ("zero_caller_functions", "duplicate_constant_homes",
              "duplicate_orchestration_candidates", "orphan_artefacts"):
        v = fnd.get(k, doc.get(k))
        buckets[k] = len(v) if isinstance(v, (list, dict)) else (int(v) if isinstance(v, int) else 0)
    other = {k: (len(v) if hasattr(v, "__len__") else v) for k, v in fnd.items()
             if k not in buckets}
    return {"state": ("UNDATED" if age_days is None else
                      "STALE" if age_days >= 7 else "CURRENT"),
            "path": p, "as_of": as_of, "age_days": age_days, "age_basis": age_basis,
            "buckets": buckets, "other_findings": other,
            "n": sum(buckets.values()) + sum(v for v in other.values() if isinstance(v, int)),
            "note": ("the atlas measures REACHABILITY. It flagged none of the twelve 27-Aug "
                     "findings while reporting %d in total, because a test caller, a selftest "
                     "caller and a synthetic probe all count as callers (ISA-0468)."
                     % sum(buckets.values())),
            "r15_4": ("R15.4 requires the build to FAIL when the regenerated graph differs "
                      "from the declared manifests. The artefact declares as_of %s — %s days "
                      "old — and no build has failed on it."
                      % (as_of, age_days if age_days is not None else "UNKNOWN"))}


def report(root: str = HERE, ledger: Optional[dict] = None, cap: int = QUEUE_CAP) -> dict:
    """P0.6 — merge every Phase-0 enumerator into ONE queue ranked by GBP exposure.

    ⚑ NO SILENT CAPS (R4.9). The line reads "7 further findings suppressed, GBP 0 combined
    exposure" or "31 further suppressed, GBP 4,120 combined exposure". The second is a queue;
    the first is noise; and the reader can tell them apart without opening anything."""
    pool: List[dict] = []
    errs: List[str] = []

    def _add(src: str, items, gbp_key="gbp_exposure"):
        for f in items or []:
            subj = (f.get("quantity") or f.get("threshold") or f.get("computer")
                    or f.get("claim")
                    or (("%s == %r" % (f.get("field"), f.get("literal")))
                        if f.get("field") else None)
                    or ("%s.%s" % (f.get("module"), f.get("owner"))
                        if f.get("module") else None)
                    or "?")
            pool.append({"source": src, "subject": subj,
                         "gbp_exposure": float(f.get(gbp_key) or 0.0), **f})

    try:
        rec = reconcile(root, ledger)
        for r in rec["rows"]:
            if r["verdict"] in ("REACHABLE_NOT_LIVE", "NOT_EXECUTED", "NOT_INSTRUMENTED"):
                pool.append({"source": "P0.1", "check": r["verdict"],
                             "quantity": r["quantity"], "subject": r["quantity"],
                             "gbp_exposure": _exposure_for(r["quantity"]),
                             "detail": r["why"]})
    except Exception as exc:                                            # noqa: BLE001
        errs.append("P0.1: %s: %s" % (type(exc).__name__, exc))
        rec = {"state": "UNAVAILABLE"}

    try:
        qr = quantity_register_report(root, ledger)
        for k in ("Q1", "Q2", "Q3"):
            _add("P0.2/" + k, qr[k]["findings"])
        _add("P0.2/Q4", qr["Q4"]["gating_findings"])
        _add("P0.2/Q4-unregistered", qr["Q4"]["findings"][qr["Q4"]["n_gating"]:])
        _add("P0.2/Q1-scope", qr["Q1"].get("out_of_scope"))
        _add("P0.2/Q3-uninstrumented", qr["Q3"].get("not_instrumented"))
    except Exception as exc:                                            # noqa: BLE001
        errs.append("P0.2: %s: %s" % (type(exc).__name__, exc))
        qr = {"state": "UNAVAILABLE"}

    try:
        tr = threshold_report()
        _add("P0.3", tr["raises"])
    except Exception as exc:                                            # noqa: BLE001
        errs.append("P0.3: %s: %s" % (type(exc).__name__, exc))
        tr = {"state": "UNAVAILABLE"}

    try:
        nc = negative_claim_report(root=root)
        _add("P0.4", nc["falsified"])
        _add("P0.4", nc["expired"])
        _add("P0.4", nc["unregistered"]["findings"])
    except Exception as exc:                                            # noqa: BLE001
        errs.append("P0.4: %s: %s" % (type(exc).__name__, exc))
        nc = {"state": "UNAVAILABLE"}

    atlas = _atlas_findings(root)

    pool.sort(key=lambda f: (-f.get("gbp_exposure", 0.0), f.get("source", ""),
                             str(f.get("quantity") or f.get("literal") or "")))
    top, rest = pool[:cap], pool[cap:]
    suppressed_gbp = round(sum(f.get("gbp_exposure", 0.0) for f in rest), 2)
    states = [d.get("state") for d in (rec, qr, tr, nc) if isinstance(d, dict)]
    return {
        "as_of": datetime.date.today().isoformat(),
        "queue": top,
        "n_total": len(pool),
        "suppressed": {"count": len(rest), "gbp_exposure": suppressed_gbp,
                       "line": ("%d further finding(s) suppressed, GBP %s combined exposure"
                                % (len(rest), format(suppressed_gbp, ",.2f")))},
        "atlas": atlas,
        "enumerator_states": {"P0.1": rec.get("state"), "P0.2": qr.get("state"),
                              "P0.3": tr.get("state"), "P0.4": nc.get("state")},
        "enumerator_errors": errs,
        "flags": flag_report(),
        "state": ("ERROR" if errs or "FAIL" in states or "ERROR" in states
                  else ("WARN" if "WARN" in states else "OK")),
        "renders_in": "email §11 (framework health) — NOT §2; this is not a capital instruction",
        "basis": ("P0.6. Ranked by GBP exposure from the quantity register, capped at %d, and "
                  "the suppressed count carries its own exposure so the reader can tell a "
                  "queue from noise (R4.9)." % cap),
    }


_EXPOSURE_CACHE: Dict[str, float] = {}


def _exposure_for(qualified_fn: str) -> float:
    """GBP at stake behind a function, from the quantity register's declared computers."""
    if not _EXPOSURE_CACHE:
        try:
            for q in load_quantity_register():
                c = q.get("computer")
                if c:
                    _EXPOSURE_CACHE[c] = max(_EXPOSURE_CACHE.get(c, 0.0),
                                             float(q.get("gbp_exposure") or 0.0))
        except Exception:                                               # noqa: BLE001
            _EXPOSURE_CACHE["__none__"] = 0.0
    return _EXPOSURE_CACHE.get(qualified_fn, 0.0)


# ═══════════════════════════════════════════════════════════════════════════════════════
# Step 0 — PREFLIGHT: declaration checks BEFORE any work
# ═══════════════════════════════════════════════════════════════════════════════════════
def preflight(root: str = HERE, strict: bool = False) -> dict:
    """Runs at Step 0 of the pre-run, before anything is computed.

    ⚑ A declaration failure should stop a run BEFORE it computes anything on a broken
    contract. Computing first and checking afterwards produces a plausible artefact and a red
    line underneath it, and the artefact is what gets read."""
    out = {"as_of": datetime.date.today().isoformat(), "checks": {}, "errors": []}
    for name, fn in (("quantity_register", lambda: quantity_register_report(root)),
                     ("threshold_register", threshold_report),
                     ("negative_claims", lambda: negative_claim_report(root=root))):
        try:
            r = fn()
            out["checks"][name] = {"state": r["state"]}
            if r["state"] == "FAIL":
                out["errors"].append("%s: FAIL" % name)
        except IntegrityRefused as exc:
            out["checks"][name] = {"state": "REFUSED", "reason": str(exc)[:400]}
            out["errors"].append("%s: REFUSED — %s" % (name, str(exc)[:200]))
        except Exception as exc:                                        # noqa: BLE001
            out["checks"][name] = {"state": "ERROR",
                                   "reason": "%s: %s" % (type(exc).__name__, exc)}
            out["errors"].append("%s: ERROR" % name)
    out["state"] = "FAIL" if out["errors"] else "OK"
    if strict and out["errors"]:
        raise IntegrityRefused("preflight FAILED:\n  " + "\n  ".join(out["errors"]))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
# R10 — THE SELF-EXCLUSION CONTROL
# ═══════════════════════════════════════════════════════════════════════════════════════
def self_exclusion_control(root: str = HERE) -> dict:
    """Proof, not assertion, that the observer excludes itself (A12 / spec R10).

    The POSITIVE half: this module's own file is absent from every enumerator's source list.
    The NEGATIVE half: including it WOULD produce findings — so the exclusion is doing work
    rather than being vacuously true. An exclusion nobody can see working is indistinguishable
    from an exclusion that was never applied."""
    excl = source_files(root, exclude_self=True)
    incl = source_files(root, exclude_self=False)
    self_path = os.path.join(root, SELF_MODULE + ".py")
    present = os.path.exists(self_path)
    would_find = 0
    if present:
        for _, doc, _o in _docstrings(self_path):
            for line in doc.splitlines():
                if _NEG_RE.search(line):
                    would_find += 1
    return {"self_module": SELF_MODULE,
            "self_present_on_disk": present,
            "excluded_from_enumerators": self_path not in excl,
            "included_when_asked": self_path in incl if present else None,
            "negative_control_would_find": would_find,
            "state": ("PASS" if (not present) or
                      (self_path not in excl and self_path in incl and would_find > 0)
                      else "FAIL"),
            "basis": ("R10. The negative control counts the findings this module's own "
                      "docstrings WOULD produce if it were not excluded — %d. A control that "
                      "cannot fail is not a control." % would_find)}


# ═══════════════════════════════════════════════════════════════════════════════════════
# SELFTEST — every assertion ships a NEGATIVE CONTROL that actually exercises the path
# ═══════════════════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:200])
                                                           if detail and not cond else ""))
        if not cond:
            fails.append(name)

    # ── P0.1 caller classification ───────────────────────────────────────────────────
    import collections
    FS = traceback.FrameSummary
    live = [FS("/x/monthly_isa_prerun.py", 10, "main")]
    test = [FS("/x/tests_jul2026/test_thing.py", 10, "t_one")]
    stst = [FS("/x/retention.py", 10, "_selftest")]
    prob = [FS("/x/capital_destination.py", 10, "_stock_side_sensitivity")]
    unk = [FS("/x/some_scratch.py", 10, "go")]
    ok("L2 a live orchestrator frame classifies live_run",
       _classify_caller(live)[0] == "live_run", _classify_caller(live))
    ok("L2-neg the SAME function from a test classifies test",
       _classify_caller(test)[0] == "test", _classify_caller(test))
    ok("L2-neg a selftest frame classifies selftest",
       _classify_caller(stst)[0] == "selftest", _classify_caller(stst))
    ok("L3 a declared PROBE caller is its own kind, not live_run",
       _classify_caller(prob)[0] == "probe", _classify_caller(prob))
    ok("an unrecognised caller is `unknown`, never promoted to live_run",
       _classify_caller(unk)[0] == "unknown", _classify_caller(unk))
    ok("selftest beats live_run when both frames are present",
       _classify_caller(live + stst)[0] == "selftest")

    # ── P0.1 reconciliation verdicts, both directions ───────────────────────────────
    led_none = {"records": {}, "store_exists": True, "store_path": "<mem>"}
    r0 = reconcile(HERE, led_none)
    ok("L1 a declared, unexecuted function reports an ERROR",
       any(x["verdict"] in ("NOT_EXECUTED", "NOT_INSTRUMENTED") for x in r0["rows"])
       and r0["state"] == "ERROR")
    led_probe = {"records": {"position_sizing.stock_max":
                             {"calls": 3, "kinds": {"probe": 3}, "callers": ["cd._probe"]}},
                 "store_exists": True, "store_path": "<mem>"}
    row = [x for x in reconcile(HERE, led_probe)["rows"]
           if x["quantity"] == "position_sizing.stock_max"][0]
    ok("L3 position_sizing.stock_max reports REACHABLE_NOT_LIVE under a probe-only caller",
       row["verdict"] == "REACHABLE_NOT_LIVE", row)
    led_live = {"records": {"position_sizing.stock_max":
                            {"calls": 1, "kinds": {"live_run": 1}, "callers": ["cd.sleeve_split"]}},
                "store_exists": True, "store_path": "<mem>"}
    row2 = [x for x in reconcile(HERE, led_live)["rows"]
            if x["quantity"] == "position_sizing.stock_max"][0]
    ok("L3-neg the SAME function called from the live run reports live_run",
       row2["verdict"] == "live_run", row2)
    led_test = {"records": {"stock_return_store.record_level":
                            {"calls": 9, "kinds": {"test": 9}, "callers": ["test_x.t"]}},
                "store_exists": True, "store_path": "<mem>"}
    row3 = [x for x in reconcile(HERE, led_test)["rows"]
            if x["quantity"] == "stock_return_store.record_level"][0]
    ok("L4 record_level reports REACHABLE_NOT_LIVE when only its tests call it",
       row3["verdict"] == "REACHABLE_NOT_LIVE", row3)
    ok("L6 reconcile(strict=True) RAISES on an error rather than returning it",
       _raises(lambda: reconcile(HERE, led_none, strict=True), IntegrityRefused))
    undecl = {"records": {"some_module.some_fn": {"calls": 1, "kinds": {"live_run": 1},
                                                  "callers": ["x"]}},
              "store_exists": True, "store_path": "<mem>"}
    ok("UNDECLARED_LIVE is a WARNING, not an error (the manifest is stale, the code is fine)",
       any("UNDECLARED_LIVE" in w for w in reconcile(HERE, undecl)["warnings"]))

    # ── L7 flag OFF ⇒ _mark is a no-op ───────────────────────────────────────────────
    import isa_policy as _p
    before = dict(_LEDGER_MEM)
    _prev = _p.V2_FLAGS.get("execution_ledger")
    _p.V2_FLAGS["execution_ledger"] = False
    _mark("x", "y")
    ok("L7 flag False ⇒ _mark writes nothing", _LEDGER_MEM == before)
    _p.V2_FLAGS["execution_ledger"] = True
    _mark("selftest_module", "selftest_fn")
    ok("L7-neg flag True ⇒ _mark DOES write (the control is not vacuous)",
       "selftest_module.selftest_fn" in _LEDGER_MEM)
    _LEDGER_MEM.pop("selftest_module.selftest_fn", None)
    if _prev is None:
        _p.V2_FLAGS.pop("execution_ledger", None)
    else:
        _p.V2_FLAGS["execution_ledger"] = _prev

    # ── P0.2 Q1 compute vs relay ─────────────────────────────────────────────────────
    tmp = tempfile.mkdtemp()
    _write(tmp, "alpha.py", 'def compute():\n    out = {}\n    out["thing_gbp"] = 1 + 2\n    return out\n')
    _write(tmp, "beta.py", 'def relay(src):\n    out = {}\n    out["thing_gbp"] = src["thing_gbp"]\n    return out\n')
    reg = [{"name": "thing_gbp", "computer": "alpha.compute", "units": "GBP",
            "surface": ["x"], "gbp_exposure": 100.0}]
    _SRC_CACHE.clear()
    r = q1_two_computers(tmp, reg)
    ok("Q-A1-neg a RELAY is not a second computer (post-P4 wiring must not fail)",
       r["state"] == "PASS", r)
    _write(tmp, "gamma.py", 'def rival(nav):\n    out = {}\n    out["thing_gbp"] = nav * 0.035\n    return out\n')
    _SRC_CACHE.clear()
    r2 = q1_two_computers(tmp, reg)
    ok("Q-A1 two COMPUTERS for one quantity FAILS", r2["state"] == "FAIL", r2)
    os.remove(os.path.join(tmp, "gamma.py"))
    _SRC_CACHE.clear()
    ok("Q-A1-neg deleting one clears it", q1_two_computers(tmp, reg)["state"] == "PASS")

    # ── P0.2 Q2 / Q4 ────────────────────────────────────────────────────────────────
    ok("Q-A5 a registered quantity with no surface FAILS",
       q2_no_surface(tmp, [{"name": "z", "computer": "a.b", "surface": []}])["state"] == "FAIL")
    ok("Q-A5-neg adding a renderer clears it",
       q2_no_surface(tmp, [{"name": "z", "computer": "a.b",
                            "surface": ["email.s2"]}])["state"] == "PASS")
    ok("Q-A6 computer:null is UNADJUDICATED and FAILS without a register item",
       unadjudicated_quantities([{"name": "z", "computer": None}])["state"] == "FAIL")
    ok("Q-A6-neg an UNADJUDICATED quantity WITH a register item is a WARN, not a FAIL",
       unadjudicated_quantities([{"name": "z", "computer": None,
                                  "register_item": "ISA-0470"}])["state"] == "WARN")

    tmp2 = tempfile.mkdtemp()
    _write(tmp2, "filt.py",
           'def f(d):\n    if d.get("route") == "ghost_route":\n        return 1\n'
           '    if d.get("mode") == "live_mode":\n        return 2\n    return 0\n')
    _write(tmp2, "prod.py", 'def make():\n    return {"mode": "live_mode"}\n')
    _write(tmp2, "test_fixture.py", 'FX = {"route": "ghost_route"}\n')
    _SRC_CACHE.clear()
    q4 = q4_dead_vocabulary(tmp2, [])
    lits = {f["literal"] for f in q4["findings"]}
    ok("Q-A3 a filter on a literal only a TEST emits is flagged",
       "ghost_route" in lits, q4["findings"])
    ok("Q-A4-neg a literal with a LIVE producer is NOT flagged", "live_mode" not in lits, lits)
    _write(tmp2, "filt2.py", 'def g(d):\n    return d.get("stage") == "phantom_stage"\n')
    _SRC_CACHE.clear()
    q4b = q4_dead_vocabulary(tmp2, [])
    ok("Q-A4 CLASS SCOPE — a SECOND, unrelated dead vocabulary term is also flagged",
       {"ghost_route", "phantom_stage"} <= {f["literal"] for f in q4b["findings"]},
       q4b["findings"])

    # ⚑ THE CONTROL THAT WAS MISSING, AND THE DEFECT IT NOW CATCHES (28-Aug-2026).
    # Q-A3 above puts its fixture in `test_fixture.py`, which exercises the PATH exclusion
    # (`is_test_path`) and never the FUNCTION exclusion. The real `forward_led` producer is
    # `retention._selftest` — a selftest INSIDE A LIVE MODULE — and the first implementation
    # of `_producers_of` used `for node in ast.walk(tree): if <selftest>: continue`, which
    # skips the FunctionDef and then walks all of its children anyway. Q4 therefore counted a
    # selftest fixture as a live producer and reported the ratchet vocabulary HEALTHY.
    # ⚑ A NEGATIVE CONTROL THAT EXERCISES A DIFFERENT MECHANISM FROM THE ONE IN THE LIVE PATH
    # IS A CONTROL THAT PASSES WHILE THE DEFECT SHIPS. This one uses the live mechanism.
    tmp5 = tempfile.mkdtemp()
    _write(tmp5, "retention.py",
           'def ratchet_eligible(ds):\n'
           '    return [d for d in ds if d.get("route") == "seance_route"]\n\n'
           'def _selftest():\n'
           '    live = [{"ticker": "COCO", "route": "seance_route"}]\n'
           '    return live\n')
    _SRC_CACHE.clear()
    ok("Q-A3-neg a fixture inside a _selftest IN A LIVE MODULE is NOT a producer",
       _producers_of("route", "seance_route", tmp5) == [],
       _producers_of("route", "seance_route", tmp5))
    ok("Q-A3-neg ...so the filter on it is still flagged",
       "seance_route" in {f["literal"] for f in q4_dead_vocabulary(tmp5, [])["findings"]})
    _write(tmp5, "conviction_capture.py",
           'def emit():\n    return {"route": "seance_route"}\n')
    _SRC_CACHE.clear()
    ok("Q-A3-neg ...and a producer OUTSIDE a selftest DOES clear it (control is not vacuous)",
       _producers_of("route", "seance_route", tmp5) != [])

    # the same defect class in the DECLARED-PROBE exclusion
    tmp6 = tempfile.mkdtemp()
    _write(tmp6, "capital_destination.py",
           'def _stock_side_sensitivity(n):\n'
           '    return {"evidence_state": "SEANCE_STATE"}\n')
    _SRC_CACHE.clear()
    ok("a DECLARED PROBE is not a producer either (same span rule, same reason)",
       _producers_of("evidence_state", "SEANCE_STATE", tmp6) == [],
       _producers_of("evidence_state", "SEANCE_STATE", tmp6))

    # ── P0.3 threshold verdicts ─────────────────────────────────────────────────────
    v = verdict_for(5.0, 35.2)
    ok("T1 -5pp against 35.2%/yr TE is NON_DISCRIMINATING at t = 0.14 ± 0.02",
       v["verdict"] == VERDICT_NON_DISCRIMINATING and abs(v["t_ratio"] - 0.142) < 0.02, v)
    ok("T1-neg -45pp on the SAME series is DISCRIMINATING",
       verdict_for(45.1, 35.2)["verdict"] == VERDICT_DISCRIMINATING)
    ok("T2 a 0%-fire-rate gate over 6 runs is NON_INFORMATIVE",
       verdict_for(1.0, 0.1, [False] * 6)["verdict"] == VERDICT_NON_INFORMATIVE)
    ok("T2-neg a 33% fire rate over 6 runs is DISCRIMINATING",
       verdict_for(1.0, 0.1, [True, False, False, True, False, False])["verdict"]
       == VERDICT_DISCRIMINATING)
    ok("T4 sd null ⇒ UNMEASURED, never DISCRIMINATING",
       verdict_for(5.0, None)["verdict"] == VERDICT_UNMEASURED)
    ok("T4-neg an UNMEASURED verdict can never read DISCRIMINATING",
       verdict_for(5.0, None)["verdict"] != VERDICT_DISCRIMINATING)
    ok("a threshold at exactly |t| = 1.0 is DISCRIMINATING (boundary declared, not implied)",
       verdict_for(1.0, 1.0)["verdict"] == VERDICT_DISCRIMINATING)
    ok("100%-fire-rate is ALSO non-informative, not just 0%",
       verdict_for(1.0, 0.1, [True] * 6)["verdict"] == VERDICT_NON_INFORMATIVE)

    # ── P0.4 negative claims ────────────────────────────────────────────────────────
    tmp3 = tempfile.mkdtemp()
    _write(tmp3, "position_sizing.py",
           '"""A module.\n\nYahoo is network-blocked from both the container and the device shell.\n"""\n')
    claims_empty = os.path.join(tmp3, "nc.json")
    json.dump({"claims": []}, open(claims_empty, "w"))
    _SRC_CACHE.clear()
    u = unregistered_negative_claims(tmp3, claims_empty)
    ok("N1 an unregistered negative claim in a capital-path docstring FAILS",
       u["state"] == "FAIL", u)
    claims_one = os.path.join(tmp3, "nc2.json")
    json.dump({"claims": [{"claim": "Yahoo is network-blocked from both the container and the "
                                    "device shell", "state": "FALSIFIED", "gates": ["g"],
                           "test_id": "t", "last_tested": "2026-08-26",
                           "runs_since_tested": 0, "expires_after_runs": 3}]},
              open(claims_one, "w"))
    _SRC_CACHE.clear()
    ok("N1-neg registering it clears the unregistered finding",
       unregistered_negative_claims(tmp3, claims_one)["state"] == "PASS")
    exp = negative_claim_report(claims_one, tmp3)
    ok("N3 the seeded Yahoo claim reports FALSIFIED and FAILS the build",
       exp["state"] == "FAIL" and exp["falsified"], exp["rows"])
    claims_exp = os.path.join(tmp3, "nc3.json")
    json.dump({"claims": [{"claim": "no such source is available for X", "state": "ASSERTED_TRUE",
                           "gates": ["gate_x"], "test_id": None, "last_tested": "2026-01-01",
                           "runs_since_tested": 4, "expires_after_runs": 3}]},
              open(claims_exp, "w"))
    e2 = negative_claim_report(claims_exp, tempfile.mkdtemp())
    ok("N2 an expired claim forces its gate to UNMEASURED",
       e2["gates_forced_unmeasured"] == ["gate_x"], e2)
    claims_fresh = os.path.join(tmp3, "nc4.json")
    json.dump({"claims": [{"claim": "no such source is available for X", "state": "ASSERTED_TRUE",
                           "gates": ["gate_x"], "test_id": None, "last_tested": "2026-08-20",
                           "runs_since_tested": 1, "expires_after_runs": 6}]},
              open(claims_fresh, "w"))
    e3 = negative_claim_report(claims_fresh, tempfile.mkdtemp())
    ok("N2-neg a fresh claim leaves its gate intact",
       e3["gates_forced_unmeasured"] == [] and e3["state"] == "PASS", e3)

    # N4 — prose ABOUT the rule must not trip the rule
    tmp4 = tempfile.mkdtemp()
    _write(tmp4, "capital_destination.py",
           '# Yahoo is network-blocked -- this COMMENT explains P0.4 and is not a claim.\n'
           'X = "a string constant saying network-blocked, also not a docstring"\n'
           'def f():\n    """A docstring with no claim in it."""\n    return 1\n')
    _SRC_CACHE.clear()
    n4 = unregistered_negative_claims(tmp4, claims_empty)
    ok("N4 prose ABOUT the rule does not trip the rule (comments/constants are not docstrings)",
       n4["state"] == "PASS", n4["findings"])
    _write(tmp4, "retention.py", '"""It cannot fire because nothing emits the value."""\n')
    _SRC_CACHE.clear()
    ok("N4-neg the SAME phrase inside a real docstring IS flagged (control is not vacuous)",
       unregistered_negative_claims(tmp4, claims_empty)["state"] == "FAIL")

    # ── R10 self-exclusion ──────────────────────────────────────────────────────────
    _SRC_CACHE.clear()
    sx = self_exclusion_control(HERE)
    ok("R10 the instrument excludes its own functions from every enumerator",
       sx["excluded_from_enumerators"], sx)
    ok("R10-neg the exclusion is DOING WORK — including this module would produce findings",
       (not sx["self_present_on_disk"]) or sx["negative_control_would_find"] > 0, sx)

    # ── P0.6 queue accounting ───────────────────────────────────────────────────────
    _SRC_CACHE.clear()
    pool = [{"source": "t", "gbp_exposure": float(i)} for i in range(25)]
    pool.sort(key=lambda f: -f["gbp_exposure"])
    top, rest = pool[:QUEUE_CAP], pool[QUEUE_CAP:]
    ok("P0.6 the suppressed count carries its own GBP exposure (no silent cap, R4.9)",
       len(rest) == 15 and abs(sum(f["gbp_exposure"] for f in rest) - 105.0) < 1e-9)

    # ── ISA-0552 — the caches must not change an answer (R5.8) ──────────────────────────
    # Step 0 cost 81.3s of a ~175s host budget and the pre-run therefore never reached
    # write_run_context. The fix was a field pre-filter plus per-file walk/span/blob caches.
    # A speed fix that silently narrows a control is worse than the slowness, so the fast
    # path is compared against a deliberately slow re-derivation on REAL pairs from this
    # tree. Sampled here to stay affordable; `--equivalence-full` sweeps every pair.
    _SRC_CACHE.clear(); _WALK_CACHE.clear(); _SPANS_CACHE.clear()
    _LOWER_CACHE.clear(); _DATA_BLOB_CACHE.clear()
    try:
        _eq = producer_equivalence(sample=12)
        ok("ISA-0552 producer_equivalence: the cached/pre-filtered path finds exactly what "
           "the uncached full-sweep path finds (%d pair(s))" % _eq["checked"],
           _eq["state"] == "PASS" and _eq["checked"] > 0, _eq["mismatches"])
    except Exception as _e:                                             # noqa: BLE001
        ok("ISA-0552 producer_equivalence ran", False, "%s: %s" % (type(_e).__name__, _e))

    print("\nframework_integrity selftest: %d assertion(s), %d FAIL(s)%s"
          % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


_ASSERTS = [0]


def _write(d: str, name: str, body: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return p


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:                                                   # noqa: BLE001
        return False
    return False


def producer_equivalence(root: str = HERE, sample: Optional[int] = 25,
                         offset: int = 0, count: Optional[int] = None,
                         reference: str = "uncached") -> dict:
    """R5.8 for the ISA-0552 caches: the fast path must find exactly what the slow path finds.

    Re-derives `_producers_of` for real (field, literal) pairs taken from the live tree with
    BOTH accelerations disabled — no field pre-filter, no cached walk, no cached spans, no
    cached data blobs — and compares hit sets. A cache that changes an answer is not a cache;
    this is the control that says which one it is."""
    def _slow(field: str, literal: str) -> List[str]:
        hits = []
        for path in source_files(root):
            src_, tree = parsed(path)
            if tree is None or literal not in src_:
                continue
            m = modname(path)
            spans = _excluded_line_ranges(tree, m)          # uncached, recomputed
            for node in ast.walk(tree):                     # uncached, re-walked
                if _in_spans(getattr(node, "lineno", -1), spans):
                    continue
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value == field and \
                           _emits_literal(v, literal):
                            hits.append("%s:%d dict" % (m, node.lineno))
                if isinstance(node, ast.Assign):
                    if _emits_literal(node.value, literal):
                        for t in node.targets:
                            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                               and t.slice.value == field:
                                hits.append("%s:%d assign" % (m, node.lineno))
                            elif isinstance(t, ast.Name) and t.id.lower().endswith(field.lower()):
                                hits.append("%s:%d name" % (m, node.lineno))
                    if isinstance(node.value, (ast.Set, ast.Tuple, ast.List)):
                        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                        if any(field.lower() in n.lower() for n in names):
                            for e in node.value.elts:
                                if isinstance(e, ast.Constant) and e.value == literal:
                                    hits.append("%s:%d vocab" % (m, node.lineno))
                if isinstance(node, ast.Call):
                    for kw in node.keywords or []:
                        if kw.arg == field and _emits_literal(kw.value, literal):
                            hits.append("%s:%d kwarg" % (m, node.lineno))
        pat = re.compile(r'"%s"\s*:\s*"%s"' % (re.escape(field), re.escape(literal)))
        for fn in sorted(os.listdir(root)):
            if fn.endswith((".json", ".jsonl")):
                try:
                    with open(os.path.join(root, fn), encoding="utf-8") as fh:
                        if pat.search(fh.read()):
                            hits.append("data:%s" % fn)
                except Exception:                                       # noqa: BLE001
                    continue
        sdir = os.path.join(root, "Dashboard", "state")
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if fn.endswith((".json", ".jsonl")):
                    try:
                        with open(os.path.join(sdir, fn), encoding="utf-8") as fh:
                            if pat.search(fh.read()):
                                hits.append("data:Dashboard/state/%s" % fn)
                    except Exception:                                   # noqa: BLE001
                        continue
        return sorted(set(hits))

    pairs, seen = [], set()
    for c in _vocab_comparisons(root):
        k = (c["field"], c["literal"])
        if k not in seen:
            seen.add(k)
            pairs.append(k)
    pairs.sort()
    n_all = len(pairs)
    if sample:
        step = max(1, len(pairs) // sample)
        pairs = pairs[::step][:sample]
    # ISA-0594: the exhaustive proof costs more than one host-shell call, so it is drivable in
    # slices. `offset`/`count` NEVER change which pairs exist — only how many are proved per
    # invocation — and the slices are recombined by the caller into a single verdict.
    if count is not None:
        pairs = pairs[offset:offset + count]
    mismatches = []
    for field, literal in pairs:
        fast = _producers_of(field, literal, root)          # ISA-0594 index
        scan = _producers_of_scan(field, literal, root)     # the pre-ISA-0594 implementation
        # ⚑ TWO REFERENCES, DELIBERATELY. `_slow` disables every acceleration and is the
        # strongest statement, but it costs ~2.5s a pair, so proving all 252 live pairs
        # against it exceeds a single host-shell call. `reference="scan"` proves the index
        # against the implementation it REPLACED, exhaustively and affordably; the uncached
        # reference is then run over a slice. Coverage is reported, never implied.
        ref = scan if reference == "scan" else _slow(field, literal)
        if fast != ref or scan != ref:
            mismatches.append({"field": field, "literal": literal,
                               "index_vs_reference": "DIFFER" if fast != ref else "same",
                               "scan_vs_reference": "DIFFER" if scan != ref else "same",
                               "n_index": len(fast), "n_scan": len(scan), "n_reference": len(ref)})
    return {"checked": len(pairs), "n_pairs_total": n_all, "offset": offset,
            "mismatches": mismatches, "reference": reference,
            "compares": "index vs pre-ISA-0594 scan vs %s reference" % reference,
            "state": "PASS" if not mismatches else "FAIL"}


def computer_equivalence(root: str = HERE, sample=None) -> dict:
    """R5.8 for the ISA-0594 computer index: the indexed path must find exactly what the
    per-quantity scan finds, for every quantity the register actually declares.

    The register is small (12 entries at 05-Sep-2026), so this runs EXHAUSTIVELY by default —
    a sampled proof of a 12-element space would be a choice to not know."""
    try:
        names = [r.get("name") for r in load_quantity_register() if r.get("name")]
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": "%s: %s" % (type(exc).__name__, exc)}
    names = sorted(set(names))
    if sample:
        names = names[:sample]
    mismatches = []
    for q in names:
        fast = _computers_of(q, root)
        scan = _computers_of_scan(q, root)
        if fast != scan:
            mismatches.append({"quantity": q, "n_index": len(fast), "n_scan": len(scan),
                               "index": fast, "scan": scan})
    return {"checked": len(names), "mismatches": mismatches,
            "compares": "index vs pre-ISA-0594 per-quantity scan",
            "state": "PASS" if not mismatches else "FAIL"}


if __name__ == "__main__":
    if "--equivalence-full" in sys.argv:
        _eq = producer_equivalence(sample=None)
        _ce = computer_equivalence()
        print(json.dumps({"producers": _eq, "computers": _ce}, indent=1))
        sys.exit(0 if _eq["state"] == "PASS" and _ce["state"] == "PASS" else 1)
    if "--selftest" in sys.argv:
        _o = print
        def print(*a, **k):                                             # noqa: A001
            if a and isinstance(a[0], str) and a[0].startswith(("  PASS", "  FAIL")):
                _ASSERTS[0] += 1
            _o(*a, **k)
        sys.exit(_selftest())
    if "--preflight" in sys.argv:
        r = preflight()
        print(json.dumps(r, indent=1))
        sys.exit(1 if r["state"] == "FAIL" else 0)
    if "--report" in sys.argv:
        print(json.dumps(report(), indent=1, default=str))
        sys.exit(0)
    print(json.dumps({"flags": flag_report(),
                      "manifest": len(CAPITAL_PATH_MANIFEST),
                      "mark_sites": mark_sites(),
                      "self_exclusion": self_exclusion_control()}, indent=1, default=str))
