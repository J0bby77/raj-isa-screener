#!/usr/bin/env python3
"""
score_definition.py - ISA-0619 (Raj 24-Sep-2026): the ONE declared scoring-definition identity.

⚑ WHY. calibration_guard.config_fingerprint hashes 23 scoring_config PARAMETERS. A code-level change
to what a score MEANS moves no parameter: the 17-Jul revisions split and ISA-0720 (23-Sep) both left
it at 7e6fc8157576, so observations produced under different definitions looked comparable.

⚑ WHAT THE IDENTITY IS. sha256 of the NORMALISED AST (docstrings stripped; comments never reach the
AST) of every function reachable from the declared scoring ROOTS in screener_core + source_score,
plus the source expression of every scoring_config constant those functions reference. A semantic
code change moves it; a comment/docstring/formatting change does not; a referenced constant moves
it; an unreferenced constant does not.

⚑ ONE HOME FOR DECLARATIONS: scoring_config.SCORE_DEFINITIONS (identity -> id/status/compatible_with)
and scoring_config.SCORE_PANEL_PROVENANCE (pre-stamp populations). Comparability is identity
equality or an explicit `compatible_with` declaration - NEVER inference. An old row can never acquire
the current identity: a write whose run_date is not the write date is not stamped (basis
HISTORICAL_WRITE_UNSTAMPED) and resolves through the provenance table or to UNKNOWN_DEFINITION.
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import sys
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_VERSION = "1.0"
ROOTS = {"screener_core": ("_score_ticker", "apply_cross_sectional_momentum", "score_part_a",
                           "score_part_b", "compute_forward_axis", "merge_est_rev_direction",
                           "overlay_estimate_revisions"),
         "source_score": ("compute_source_score", "source_score_components_for_row",
                          "source_score_for_row", "summary_eligible", "select_summary")}
CFG_ALIASES = {"_cfg", "cfg", "scoring_config"}
MODULES = ("screener_core", "source_score", "scoring_config")

# row-level states (relative to the CURRENT identity)
COMPARABLE = "COMPARABLE_CURRENT"
LEGACY = "INCOMPARABLE_LEGACY_DEFINITION"
UNKNOWN = "INCOMPARABLE_UNKNOWN_DEFINITION"
UNDECLARED = "UNDECLARED_CURRENT_DEFINITION"

_CACHE: Dict[str, dict] = {}


def _strip(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            n.body = n.body[1:] or [ast.Pass()]
    return tree


def identity(texts: Dict[str, str]) -> dict:
    """Identity of a scoring definition from module SOURCE TEXTS (so a historical version can be
    identified from its archived source without importing it)."""
    trees = {m: ast.parse(t) for m, t in texts.items() if m != "scoring_config"}
    funcs = {m: {n.name: n for n in t.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
             for m, t in trees.items()}
    alias = {}
    for m, t in trees.items():
        a = {}
        for n in ast.walk(t):
            if isinstance(n, ast.Import):
                for x in n.names:
                    a[x.asname or x.name] = x.name
            if isinstance(n, ast.ImportFrom) and n.module:
                for x in n.names:
                    a[x.asname or x.name] = n.module + "." + x.name
        alias[m] = a
    seen, cfgnames = set(), set()
    stack = [(m, f) for m, fs in ROOTS.items() for f in fs if f in funcs.get(m, {})]
    missing = sorted("%s.%s" % (m, f) for m, fs in ROOTS.items() for f in fs if f not in funcs.get(m, {}))
    while stack:
        m, f = stack.pop()
        if (m, f) in seen:
            continue
        seen.add((m, f))
        for n in ast.walk(funcs[m][f]):
            if isinstance(n, ast.Call):
                fn = n.func
                if isinstance(fn, ast.Name):
                    if fn.id in funcs[m]:
                        stack.append((m, fn.id))
                    mm, _, ff = alias[m].get(fn.id, "").rpartition(".")
                    if mm in funcs and ff in funcs[mm]:
                        stack.append((mm, ff))
                    if fn.id == "getattr" and len(n.args) > 1 and isinstance(n.args[0], ast.Name) \
                            and n.args[0].id in CFG_ALIASES and isinstance(n.args[1], ast.Constant):
                        cfgnames.add(n.args[1].value)
                    if fn.id == "_c" and n.args and isinstance(n.args[0], ast.Constant):
                        cfgnames.add(n.args[0].value)
                elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    mod = alias[m].get(fn.value.id, fn.value.id)
                    if mod in funcs and fn.attr in funcs[mod]:
                        stack.append((mod, fn.attr))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in CFG_ALIASES:
                cfgnames.add(n.attr)
    fsig = {"%s.%s" % k: hashlib.sha256(ast.dump(_strip(ast.parse(ast.unparse(funcs[k[0]][k[1]]))),
                                                 include_attributes=False).encode()).hexdigest()[:16]
            for k in sorted(seen)}
    cvals = {}
    for n in ast.parse(texts.get("scoring_config", "")).body:
        tg, v = [], None
        if isinstance(n, ast.Assign):
            tg, v = [t.id for t in n.targets if isinstance(t, ast.Name)], n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
            tg, v = [n.target.id], n.value
        for t in tg:
            if t in cfgnames:
                cvals[t] = ast.dump(v, include_attributes=False)
    csig = {k: hashlib.sha256(v.encode()).hexdigest()[:16] for k, v in sorted(cvals.items())}
    unresolved = sorted(cfgnames - set(cvals))
    h = hashlib.sha256(json.dumps({"f": fsig, "c": csig, "u": unresolved}, sort_keys=True).encode()).hexdigest()
    return {"hash": h[:16], "identity_version": IDENTITY_VERSION, "functions": fsig, "config": csig,
            "config_unresolved": unresolved, "roots_missing": missing, "n_functions": len(fsig)}


def _source_path(mod: str, root: Optional[str]) -> str:
    if root is None:
        m = sys.modules.get(mod)
        f = getattr(m, "__file__", None) if m is not None else None
        if f and f.endswith(".py") and os.path.exists(f):
            return f                      # the code THIS process is actually running
    return os.path.join(root or HERE, mod + ".py")


def current_identity(root: Optional[str] = None, refresh: bool = False) -> dict:
    key = root or "__process__"
    if key in _CACHE and not refresh:
        return _CACHE[key]
    texts = {}
    for m in MODULES:
        with open(_source_path(m, root), encoding="utf-8") as fh:
            texts[m] = fh.read()
    out = identity(texts)
    reg = registry(root)
    ent = reg.get(out["hash"])
    out["declared"] = bool(ent)
    out["id"] = (ent or {}).get("id")
    out["status"] = (ent or {}).get("status")
    _CACHE[key] = out
    return out


def registry(root: Optional[str] = None) -> dict:
    try:
        if root:
            ns: dict = {}
            src = open(os.path.join(root, "scoring_config.py"), encoding="utf-8").read()
            tree = ast.parse(src)
            for n in tree.body:
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCORE_DEFINITIONS"
                                                     for t in n.targets):
                    return ast.literal_eval(n.value)
            return {}
        import scoring_config as _sc
        return dict(getattr(_sc, "SCORE_DEFINITIONS", {}) or {})
    except Exception:                                                   # noqa: BLE001
        return {}


def provenance_table() -> dict:
    try:
        import scoring_config as _sc
        return dict(getattr(_sc, "SCORE_PANEL_PROVENANCE", {}) or {})
    except Exception:                                                   # noqa: BLE001
        return {}


def compatible(a: Optional[str], b: Optional[str], reg: Optional[dict] = None) -> bool:
    """Two identities are comparable only if equal or explicitly declared compatible."""
    if not a or not b:
        return False
    if a == b:
        return True
    reg = reg if reg is not None else registry()
    return b in ((reg.get(a) or {}).get("compatible_with") or []) or \
        a in ((reg.get(b) or {}).get("compatible_with") or [])


def stamp(run_date: Optional[str] = None, *, as_of: Optional[str] = None,
          today: Optional[str] = None) -> dict:
    """Fields stamped on a row AT THE MOMENT IT IS SCORED. A write for a run_date that is not the
    write date (a backfill, a re-log of an old screen) carries NO identity - it must never inherit
    the current one by inference (ISA-0619)."""
    try:
        from framework_integrity import _mark as _fi_mark
        _fi_mark("score_definition", "stamp")
    except Exception:                                                   # noqa: BLE001
        pass
    today = today or datetime.date.today().isoformat()
    rd = str(run_date or today)[:10]
    try:
        gap = abs((datetime.date.fromisoformat(today) - datetime.date.fromisoformat(rd)).days)
    except ValueError:
        gap = 999
    base = {"score_panel_schema_version": _schema(), "snapshot_as_of": as_of or today}
    if gap > 1:
        base.update(score_definition_hash=None, score_definition_id=None,
                    score_definition_basis="HISTORICAL_WRITE_UNSTAMPED", calibration_fingerprint=None)
        return base
    cur = current_identity()
    cf = None
    try:
        import calibration_guard as _cg
        cf = _cg.config_fingerprint()["hash"]
    except Exception:                                                   # noqa: BLE001
        cf = None
    base.update(score_definition_hash=cur["hash"],
                score_definition_id=cur.get("id") or "UNDECLARED",
                score_definition_basis="STAMPED_AT_SCORING",
                calibration_fingerprint=cf)
    return base


def _schema():
    try:
        import scoring_config as _sc
        return getattr(_sc, "SCORE_PANEL_SCHEMA_VERSION", None)
    except Exception:                                                   # noqa: BLE001
        return None


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() in ("", "nan", "None")


def row_definition(row: dict, *, prov: Optional[dict] = None) -> dict:
    """{hash, basis} of the definition that GENERATED this row - stamped, else the governed
    provenance table, else UNKNOWN. Never the current identity by inference."""
    h = row.get("score_definition_hash")
    if not _blank(h) and str(row.get("score_definition_basis") or "") == "STAMPED_AT_SCORING":
        return {"hash": str(h), "basis": "STAMPED_AT_SCORING"}
    prov = prov if prov is not None else provenance_table()
    k = "%s|%s" % (str(row.get("run_date") or "")[:10], row.get("group"))
    p = prov.get(k)
    if p and p.get("state") == "PROVEN_HISTORICAL" and p.get("definition_hash"):
        return {"hash": p["definition_hash"], "basis": "PROVENANCE:" + k}
    return {"hash": None, "basis": ("PROVENANCE_UNKNOWN:" + k) if p else "UNSTAMPED_NO_PROVENANCE"}


def row_state(row: dict, *, current: Optional[dict] = None, reg: Optional[dict] = None,
              prov: Optional[dict] = None) -> dict:
    cur = current or current_identity()
    if not cur.get("declared"):
        return {"state": UNDECLARED, "current_hash": cur.get("hash"), "row_hash": None,
                "why": "the running scoring code's identity is not declared in scoring_config.SCORE_DEFINITIONS"}
    d = row_definition(row, prov=prov)
    if d["hash"] is None:
        return {"state": UNKNOWN, "current_hash": cur["hash"], "row_hash": None, "basis": d["basis"]}
    if compatible(d["hash"], cur["hash"], reg):
        return {"state": COMPARABLE, "current_hash": cur["hash"], "row_hash": d["hash"], "basis": d["basis"]}
    return {"state": LEGACY, "current_hash": cur["hash"], "row_hash": d["hash"], "basis": d["basis"]}


def panel_census(rows, *, current: Optional[dict] = None) -> dict:
    """Counts of the panel population by state (for reports and the build record)."""
    cur = current or current_identity()
    reg, prov = registry(), provenance_table()
    out: Dict[str, int] = {}
    for r in rows:
        s = row_state(r, current=cur, reg=reg, prov=prov)["state"]
        out[s] = out.get(s, 0) + 1
    return {"current_hash": cur.get("hash"), "current_id": cur.get("id"), "by_state": out}


# ─────────────────────────────────────────────────────────── selftest ─────────────

def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- %s" % detail))

    texts = {m: open(os.path.join(HERE, m + ".py"), encoding="utf-8").read() for m in MODULES}
    base = identity(texts)
    ok("the identity covers the scoring call graph (>= 30 functions, roots present)",
       base["n_functions"] >= 30 and not base["roots_missing"], base["roots_missing"])
    # MUST-FIRE: a code-semantic change with UNCHANGED config moves the identity
    sc = texts["screener_core"]
    tgt = "def compute_forward_axis("
    i = sc.index(tgt)
    j = sc.index("\n", sc.index(":", i)) + 1
    mutated = sc[:j] + "    scored['__isa0619_probe__'] = 1\n" + sc[j:]
    ok("MUST-FIRE: a scoring-code semantic change with unchanged config changes the identity",
       identity(dict(texts, screener_core=mutated))["hash"] != base["hash"])
    # NEGATIVE CONTROL: comment / docstring changes do not
    commented = sc[:j] + "    # ISA-0619 probe comment only\n" + sc[j:]
    ok("NEGATIVE CONTROL: a comment-only change keeps the identity (genuinely unchanged definition)",
       identity(dict(texts, screener_core=commented))["hash"] == base["hash"])
    # a referenced constant moves it; an unreferenced one does not
    cfg = texts["scoring_config"]
    ok("MUST-FIRE: a referenced scoring constant (SOURCE_WEIGHTS) change moves the identity",
       identity(dict(texts, scoring_config=cfg.replace('"analyst": 0.10}', '"analyst": 0.11}', 1)))["hash"]
       != base["hash"])
    ok("NEGATIVE CONTROL: an unreferenced constant change keeps the identity",
       identity(dict(texts, scoring_config=cfg + "\nISA0619_UNREFERENCED_PROBE = 1\n"))["hash"] == base["hash"])
    # stamping: an old row cannot acquire the current identity by inference
    st = stamp("2026-09-19", today="2026-10-03")
    ok("MUST-FIRE: a historical write (run_date != write date) is NEVER stamped with the current identity",
       st["score_definition_hash"] is None and st["score_definition_basis"] == "HISTORICAL_WRITE_UNSTAMPED")
    st2 = stamp("2026-10-03", today="2026-10-03")
    ok("a same-day scoring write carries the running identity",
       st2["score_definition_basis"] == "STAMPED_AT_SCORING" and st2["score_definition_hash"])
    cur = {"hash": "CUR", "declared": True, "id": "SD-X"}
    reg = {"CUR": {"compatible_with": []}, "OLD": {"compatible_with": []}}
    prov = {"2026-09-19|SP500": {"state": "PROVEN_HISTORICAL", "definition_hash": "OLD"},
            "2026-07-17|MIDCAP400": {"state": "UNKNOWN_DEFINITION", "definition_hash": None}}
    rs = lambda r: row_state(r, current=cur, reg=reg, prov=prov)["state"]          # noqa: E731
    ok("MUST-FIRE: a proven-historical September row is INCOMPARABLE_LEGACY_DEFINITION to the current",
       rs({"run_date": "2026-09-19", "group": "SP500"}) == LEGACY)
    ok("MUST-FIRE: an unprovable row is INCOMPARABLE_UNKNOWN_DEFINITION",
       rs({"run_date": "2026-07-17", "group": "MIDCAP400"}) == UNKNOWN)
    ok("MUST-FIRE: an old row claiming the current hash WITHOUT the scoring stamp basis is not trusted",
       rs({"run_date": "2026-09-19", "group": "SP500", "score_definition_hash": "CUR"}) == LEGACY)
    ok("a row stamped at scoring with the current identity is COMPARABLE",
       rs({"run_date": "2026-10-03", "group": "SP500", "score_definition_hash": "CUR",
           "score_definition_basis": "STAMPED_AT_SCORING"}) == COMPARABLE)
    reg2 = {"CUR": {"compatible_with": ["OLD"]}, "OLD": {}}
    ok("a GOVERNED compatible_with declaration (and only that) makes two identities comparable",
       compatible("OLD", "CUR", reg2) and not compatible("OLD", "CUR", reg))
    ok("MUST-FIRE: an undeclared running identity is UNDECLARED, never comparable",
       row_state({"score_definition_hash": "X", "score_definition_basis": "STAMPED_AT_SCORING"},
                 current={"hash": "X", "declared": False})["state"] == UNDECLARED)
    live = current_identity(HERE, refresh=True)
    ok("the running scoring code's identity is DECLARED in scoring_config.SCORE_DEFINITIONS",
       live["declared"], live["hash"])
    print("score_definition selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps({k: v for k, v in current_identity(HERE).items() if k not in ("functions", "config")}, indent=1))
