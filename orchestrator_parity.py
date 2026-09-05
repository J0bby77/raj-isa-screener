#!/usr/bin/env python3
"""
orchestrator_parity.py — 07-Aug-2026.

THE DEFECT THIS CLOSES
----------------------
`run_manifest.py` (02-Aug) named the signature of every silent failure on record: *something
didn't run, and the log looked fine.* It answers that for the MONTHLY pre-run, which imports it.
The WEEKLY SCREEN — the thing that produces the candidate list — imports nothing of the kind.

And the weekly screen has a second, sharper version of the same problem. `screener_local.py` is
the live path. It does NOT call `screener_core.run_scheduled()`; it re-implements that function's
orchestration by hand. So a capability added to `run_scheduled` — which is where the code reads
naturally, and where every reviewer looks — is INVISIBLE to the path that executes. Measured
07-Aug-2026, three capabilities had diverged:

  * `apply_cross_sectional_momentum` — WP-M set PRICE_MOM_SCORING="percentile" on 29-Jul-2026 and
    the only code that acts on it never ran. `price_mom_pctl` was a declared column with 0 of 312
    non-null values, and forward_axis_score — 60% of the Source Score, which SELECTS the SUMMARY —
    was computed on the saturating absolute bands WP-M existed to retire.
  * `emit_gate_variables` — so point-in-time constituent membership covered scored names only and
    every weekly frame carried a survivor-biased universe. Irreversible: a lost week is gone.
  * `save_run_qa` — so `{run_date}_{group}_run_qa.csv` was referenced by the email builder and
    produced by nothing.

None of these is a wrong value. Each is an ABSENT EXECUTION THAT REPORTED SUCCESS. That is a
different failure class from the one the open-items register catalogues ("a stored value that says
one thing and is another"), and the existing defences do not reach it: `py_compile` passes a
mangled import; `pair_undefined_constants` catches a constant that was never DEFINED (F4) but not
one that is defined, declared operative, and read by no reachable code.

WHAT THIS ASSERTS
-----------------
1. **Capability parity.** Every core function `run_scheduled` invokes must also be invoked by
   `screener_local`, or be listed in EXEMPT with a dated reason. An exemption is a declaration,
   not a suppression: a blank reason FAILS, so the list cannot be padded to silence the check.

2. **Config reachability.** A constant in `scoring_config.py` whose only readers sit in code
   UNREACHABLE from the live entry point is reported. This is the check that would have caught
   PRICE_MOM_SCORING on 29-Jul instead of on 07-Aug: the constant was defined, read, and read
   only inside a function nothing on the live path could reach.

Per the engineering standard this module OBSERVES. It changes no weight, gate or threshold.

CLI:
  python3 orchestrator_parity.py --selftest
  python3 orchestrator_parity.py --check
"""
from __future__ import annotations
import argparse, ast, json, os, sys
import isa_source_cache as _sc                 # ISA-0594: one home for read/parse/walk

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1

CORE = "screener_core.py"
LOCAL = "screener_local.py"
CONFIG = "scoring_config.py"
REFERENCE_ORCHESTRATOR = "run_scheduled"
LIVE_ENTRY = ("screener_local.py", "main")

# ── DECLARED DIVERGENCES ─────────────────────────────────────────────────────────────────────
# The ONLY way a capability may be absent from the live path. Each needs a reason a human wrote
# and a date. Reviewed whenever this test fails.
EXEMPT = {
    "screen_group_nasdaq": (
        "2026-08-07: owns its own fetching and cannot be driven batch-wise, so screener_local "
        "re-implements the Nasdaq gate sequence inline as screen_batch_nasdaq. A SECOND HOME for "
        "gate logic, accepted knowingly. If the Nasdaq gates change, BOTH must change."
    ),
}


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _called_names(node):
    """Every function name invoked below `node`, by bare name or attribute (core.foo())."""
    out = set()
    for n in _sc.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _module_functions(tree):
    return {n.name: n for n in _sc.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# ── 1. CAPABILITY PARITY ─────────────────────────────────────────────────────────────────────
def capability_diff(core_src=None, local_src=None):
    core_tree = _sc.parse_text(core_src if core_src is not None else _read(CORE))
    local_tree = _sc.parse_text(local_src if local_src is not None else _read(LOCAL))

    ref = [n for n in _sc.walk(core_tree)
           if isinstance(n, ast.FunctionDef) and n.name == REFERENCE_ORCHESTRATOR]
    if not ref:
        return {"ok": False, "reason": f"{REFERENCE_ORCHESTRATOR} not found in {CORE} — the "
                                       "reference orchestrator was renamed or removed; this "
                                       "check cannot silently pass on its absence"}

    core_defs = set(_module_functions(core_tree))
    required = sorted(_called_names(ref[0]) & core_defs)
    local_calls = _called_names(local_tree)

    missing = [c for c in required if c not in local_calls]
    undeclared = [c for c in missing if c not in EXEMPT]
    blank = [c for c, r in EXEMPT.items() if not str(r).strip()]
    stale = [c for c in EXEMPT if c not in required]

    return {
        "ok": not undeclared and not blank,
        "required": required, "missing": missing,
        "undeclared_missing": undeclared,
        "exempt_with_blank_reason": blank,
        "stale_exemptions": stale,
        "reason": None if (not undeclared and not blank) else
                  (f"capabilities in {REFERENCE_ORCHESTRATOR} absent from the live path and not "
                   f"declared: {undeclared}" if undeclared else
                   f"exemptions with no stated reason: {blank}"),
    }


# ── 2. CONFIG REACHABILITY ───────────────────────────────────────────────────────────────────
def _reachable(entry_mod_src, other_src, entry_fn):
    """Functions reachable from `entry_fn`, across the two modules, by name.

    Deliberately NAME-BASED and over-inclusive: a false 'reachable' makes this check quieter, a
    false 'unreachable' makes it a nuisance alarm. A check that cries wolf gets switched off, so
    it errs toward silence and reports only what it is sure about.
    """
    entry_tree, other_tree = _sc.parse_text(entry_mod_src), _sc.parse_text(other_src)
    entry_funcs = _module_functions(entry_tree)
    other_funcs = _module_functions(other_tree)

    # ⚑ RESOLVE ENTRY-FIRST, and never merge the two namespaces into one dict.
    # The first cut of this function did merge them, and BOTH modules define `main`. The other
    # module's `main` won the collision, so the walk entered `run_scheduled` — the very
    # orchestrator this check exists to prove is NOT reachable — and reported the whole tree
    # live. The synthetic selftest could not see it because it had no name collision; only the
    # negative control against the real files did. A reachability check that resolves a name to
    # the wrong module's function reports the opposite of the truth, silently.
    def resolve(name):
        return entry_funcs.get(name) or other_funcs.get(name)

    seen, stack = set(), [entry_fn]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        node = resolve(name)
        if node is None:
            continue
        for c in _called_names(node):
            if c not in seen and resolve(c) is not None:
                stack.append(c)
    # module-level code in the entry module always runs
    for n in entry_tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for c in _called_names(n):
                if resolve(c) is not None:
                    seen.add(c)
    funcs = dict(other_funcs); funcs.update(entry_funcs)
    return seen, funcs


def _constant_reads(src):
    """{CONST_NAME: {enclosing function names that read it}} for getattr(_cfg,"X") / cfg.X / X."""
    tree = _sc.parse_text(src)
    reads = {}
    parent = {}
    for n in _sc.walk(tree):
        for ch in ast.iter_child_nodes(n):
            parent[ch] = n

    def enclosing(n):
        while n is not None:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return n.name
            n = parent.get(n)
        return "<module>"

    def note(name, node):
        reads.setdefault(name, set()).add(enclosing(node))

    for n in _sc.walk(tree):
        # getattr(_cfg, "NAME", default)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" \
           and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
           and isinstance(n.args[1].value, str):
            note(n.args[1].value, n)
        # _cfg.NAME / scoring_config.NAME / cfg.NAME
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id in ("_cfg", "cfg", "scoring_config", "_scoring_config"):
            note(n.attr, n)
        # from scoring_config import NAME
        elif isinstance(n, ast.ImportFrom) and (n.module or "").endswith("scoring_config"):
            for al in n.names:
                note(al.name, n)
    return reads


def unreachable_config_reads(core_src=None, local_src=None, config_src=None):
    core_src = core_src if core_src is not None else _read(CORE)
    local_src = local_src if local_src is not None else _read(LOCAL)
    config_src = config_src if config_src is not None else _read(CONFIG)

    declared = {n.targets[0].id for n in _sc.parse_text(config_src).body
                if isinstance(n, ast.Assign) and n.targets and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id.isupper()}

    reachable, _ = _reachable(local_src, core_src, LIVE_ENTRY[1])
    reads = _constant_reads(core_src)
    for k, v in _constant_reads(local_src).items():
        reads.setdefault(k, set()).update(v)

    stranded = []
    for const in sorted(declared):
        readers = reads.get(const)
        if not readers:
            continue                     # never read at all — a different check's business
        if readers & (reachable | {"<module>"}):
            continue                     # at least one reader is live
        stranded.append({"constant": const, "read_only_by": sorted(readers)})

    return {"ok": not stranded, "declared": len(declared), "stranded": stranded,
            "reason": None if not stranded else
                      f"{len(stranded)} config constant(s) are read only by code unreachable from "
                      f"{LIVE_ENTRY[0]}:{LIVE_ENTRY[1]} — they are declared operative and are not"}


# ── D-24 §1.3 (09-Aug-2026): THE SAME CHECK, APPLIED TO A CONTRACT SURFACE ───────────────────
# `expected_return`'s docstring claimed two consumers. On disk there were NINE, and every one
# called `expected_return_for_row(row)` with no context — so adding an anchor-table parameter to
# one leaves the other eight silently running the defective behaviour. That is capability parity
# again, except the thing that diverges is a CALLER SET rather than a code path. The defence is
# the same shape: declare the set, and fail until a newcomer is declared too.
ER_MODULE = "expected_return"


def _er_importers(root=None):
    """Every .py under the analysis dir that imports `expected_return`, by AST — not by grep."""
    root = root or HERE
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        # Live code only. Dated backup folders (`_bak_*`) and `.PRE_*` snapshots are frozen
        # copies of code that has already been superseded — they cannot call anything, and
        # requiring them in the manifest would turn a control into a chore and get it suppressed.
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", "tests_jul2026", ".git",
                                    "calibration_pathc_jul2026")
                       and not d.startswith("_bak") and not d.startswith(".PRE_")]
        for fn in filenames:
            if not fn.endswith(".py") or fn == ER_MODULE + ".py" or ".PRE_" in fn:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                tree = _sc.parse_path(fp)
            except Exception:                                # noqa: BLE001
                continue
            for n in _sc.walk(tree):
                if isinstance(n, ast.Import) and any(a.name == ER_MODULE for a in n.names):
                    found.add(os.path.relpath(fp, root).replace(os.sep, "/"))
                elif isinstance(n, ast.ImportFrom) and n.module == ER_MODULE:
                    found.add(os.path.relpath(fp, root).replace(os.sep, "/"))
    return found


def er_callsite_manifest(root=None, manifest=None):
    """Importers of `expected_return` MUST equal scoring_config.ER_CALLSITE_MANIFEST."""
    if manifest is None:
        sys.path.insert(0, HERE)
        import scoring_config as _cfg
        manifest = dict(getattr(_cfg, "ER_CALLSITE_MANIFEST", {}) or {})
    found = _er_importers(root)
    declared = set(manifest)
    undeclared = sorted(found - declared)          # a tenth caller nobody registered
    absent = sorted(declared - found)              # declared but no longer importing
    blank = sorted(k for k, v in manifest.items() if not str(v or "").strip())
    ok = not undeclared and not absent and not blank
    return {"ok": ok, "declared": sorted(declared), "found": sorted(found),
            "undeclared_callers": undeclared, "declared_but_absent": absent,
            "blank_reason": blank,
            "reason": None if ok else
                      (f"E[r] caller set != ER_CALLSITE_MANIFEST — undeclared {undeclared}, "
                       f"absent {absent}, blank {blank}. Every caller must decide explicitly "
                       f"whether it receives the anchor table (D-24 §1.3).")}


def er_reachability(): 
    """D-24 §6 — the fundamentals evidence route must be ACHIEVABLE at all."""
    sys.path.insert(0, HERE)
    import expected_return as _er
    r = _er.assert_er_route_reachable(raise_on_fail=False)
    return {"ok": bool(r["ok"]), "max_attainable": r["max_attainable"], "floor": r["floor"],
            "reason": None if r["ok"] else r["message"]}


def check_all():
    a = capability_diff()
    b = unreachable_config_reads()
    c = er_callsite_manifest()
    d = er_reachability()
    return {"schema_version": SCHEMA_VERSION,
            "ok": bool(a["ok"] and b["ok"] and c["ok"] and d["ok"]),
            "capability_parity": a, "config_reachability": b,
            "er_callsite_manifest": c, "er_reachability": d}


# ── SELFTEST ─────────────────────────────────────────────────────────────────────────────────
def selftest():
    n = 0
    CORE_SRC = '''
import scoring_config as _cfg
def alpha(): pass
def beta(): pass
def gamma():
    return getattr(_cfg, "STRANDED_KNOB", "x")
def helper():
    return getattr(_cfg, "LIVE_KNOB", "y")
def run_scheduled(g):
    alpha(); beta(); gamma()
'''
    LOCAL_SRC = '''
import screener_core as core
def main():
    core.alpha(); core.beta(); core.helper()
'''
    CFG_SRC = 'LIVE_KNOB = "a"\nSTRANDED_KNOB = "percentile"\nlowercase_ignored = 1\n'

    # 1. a capability present in run_scheduled and absent from local is caught
    d = capability_diff(CORE_SRC, LOCAL_SRC)
    assert not d["ok"] and d["undeclared_missing"] == ["gamma"], d
    n += 1

    # 2. NEGATIVE CONTROL — the check must PASS when parity holds, or it proves nothing
    d2 = capability_diff(CORE_SRC, LOCAL_SRC.replace("core.helper()", "core.helper(); core.gamma()"))
    assert d2["ok"] and not d2["missing"], d2
    n += 1

    # 3. a renamed/removed reference orchestrator fails LOUD, never passes on absence
    d3 = capability_diff(CORE_SRC.replace("def run_scheduled", "def run_scheduled_v2"), LOCAL_SRC)
    assert not d3["ok"] and "not found" in d3["reason"], d3
    n += 1

    # 4. THE ONE THAT MATTERS — PRICE_MOM_SCORING's exact shape: defined, read, and read only by
    #    code the live entry point cannot reach.
    r = unreachable_config_reads(CORE_SRC, LOCAL_SRC, CFG_SRC)
    assert not r["ok"], r
    assert [x["constant"] for x in r["stranded"]] == ["STRANDED_KNOB"], r
    assert r["stranded"][0]["read_only_by"] == ["gamma"], r
    n += 1

    # 5. NEGATIVE CONTROL — once the live path reaches it, it is no longer stranded
    r2 = unreachable_config_reads(CORE_SRC, LOCAL_SRC.replace("core.helper()", "core.helper(); core.gamma()"), CFG_SRC)
    assert r2["ok"], r2
    n += 1

    # 5b. ⚑ REGRESSION — the name collision that made the first cut of _reachable lie.
    #     Both modules define `main`; the OTHER module's `main` reaches the stranded reader.
    #     Entry-first resolution must keep it unreachable.
    CORE_COLLIDE = CORE_SRC + '\ndef main():\n    run_scheduled("X")\n'
    r_c = unreachable_config_reads(CORE_COLLIDE, LOCAL_SRC, CFG_SRC)
    assert not r_c["ok"], "name collision on `main` resolved to the wrong module — check lies"
    assert [x["constant"] for x in r_c["stranded"]] == ["STRANDED_KNOB"], r_c
    n += 1

    # 6. a constant nobody reads at all is NOT reported here (different check, no double-alarm)
    r3 = unreachable_config_reads(CORE_SRC, LOCAL_SRC, CFG_SRC + 'ORPHAN = 3\n')
    assert "ORPHAN" not in [x["constant"] for x in r3["stranded"]], r3
    n += 1

    # 7. an exemption with a blank reason FAILS — the list cannot be padded into silence
    import copy
    keep = dict(EXEMPT)
    try:
        EXEMPT.clear(); EXEMPT["gamma"] = "   "
        d4 = capability_diff(CORE_SRC, LOCAL_SRC)
        assert not d4["ok"] and d4["exempt_with_blank_reason"] == ["gamma"], d4
        EXEMPT["gamma"] = "2026-08-07: declared, with a reason"
        d5 = capability_diff(CORE_SRC, LOCAL_SRC)
        assert d5["ok"] and d5["missing"] == ["gamma"], d5
    finally:
        EXEMPT.clear(); EXEMPT.update(keep)
    n += 2

    # 8. a stale exemption (for a capability no longer required) is surfaced, not left to rot
    keep = dict(EXEMPT)
    try:
        EXEMPT["no_such_function"] = "2026-01-01: obsolete"
        d6 = capability_diff(CORE_SRC, LOCAL_SRC.replace("core.helper()", "core.helper(); core.gamma()"))
        assert "no_such_function" in d6["stale_exemptions"], d6
    finally:
        EXEMPT.clear(); EXEMPT.update(keep)
    n += 1

    print(f"orchestrator_parity selftest OK ({n} assertions)")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest(); sys.exit(0)
    res = check_all()
    print(json.dumps(res, indent=2, default=str))
    sys.exit(0 if res["ok"] else 1)
