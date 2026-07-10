#!/usr/bin/env python3
"""
vci_learning.py — VCI self-improving learning module (built INTO the VCI run).

Spec: VCI_Forward_Led_Framework_Implementation_Jul2026.md §13.

Captures a feature-vector snapshot per name per run, labels outcomes when catalysts resolve,
estimates the parameters that calibrate the framework (per-component IC, empirical win-rate p by
ACS band × asset_structure, loss-on-failure L, FV calibration error), and PROPOSES parameter
updates — applying them automatically only above the §11.6 calibration gate and only within capped
step sizes. Hard safety rails (ACS floor, F1-F3, binary cap, asymmetry floors) are NEVER auto-moved.

Stores (JSON, append-only observations):
  vci_learning_store.json       — one row per name per run (+ outcome once resolved)
  vci_calibration_state.json    — current estimated params + gate status (read by vci_source_score)
  vci_calibration_changelog.json— every applied auto-change (reversible audit)

Pure-Python, stdlib-only. Safe to import anywhere.
"""
from __future__ import annotations
import json, os, statistics
from datetime import date, datetime

GATE_N = 12                      # §11.6 — min resolved outcomes in a bucket before auto-apply
WEIGHT_STEP_CAP = 0.05           # §13.4 — max single-weight move per run
FLOOR_STEP_CAP = 0.25            # §13.4 — max floor move per run (proposals only; floors are rails)

DEFAULT_WEIGHTS = {"asymmetry": 0.30, "quality": 0.30, "catalyst": 0.20, "signals": 0.20}

FEATURE_KEYS = (
    "run_date", "ticker", "theme", "asset_structure", "acs", "sub_dims", "signals",
    "signal_count", "vci_source_score", "components", "fv_inputs", "bottleneck_fv_per_share",
    "fv_asymmetry", "fv_source", "price", "days_to_catalyst", "catalyst_type", "catalyst_date",
    "deploy_eligible", "decision", "size_pct",
    # populated later by label_outcome:
    "outcome", "realised_return", "resolved_date",
)


# ---- store I/O -----------------------------------------------------------------------------
def _load(path, default):
    if path and os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            return default
    return default


def _save(path, obj):
    if not path:
        return
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, default=str)


def _today():
    return date.today().isoformat()


# ---- 13.1 capture --------------------------------------------------------------------------
def capture(observation: dict, store_path: str) -> dict:
    """Append one feature-vector row. Upsert on (run_date, ticker) so a re-run overwrites,
    never duplicates. Unknown keys are dropped to keep the schema stable."""
    store = _load(store_path, {"observations": []})
    row = {k: observation.get(k) for k in FEATURE_KEYS}
    row.setdefault("run_date", _today())
    key = (row.get("run_date"), row.get("ticker"))
    store["observations"] = [o for o in store["observations"]
                             if (o.get("run_date"), o.get("ticker")) != key]
    store["observations"].append(row)
    _save(store_path, store)
    return row


# ---- 13.2 label outcomes -------------------------------------------------------------------
def label_outcome(ticker: str, run_date: str, outcome: str, realised_return: float,
                  store_path: str) -> bool:
    """Attach a resolved outcome ('win'|'fail'|'neutral') + realised return to the matching row."""
    store = _load(store_path, {"observations": []})
    hit = False
    for o in store["observations"]:
        if o.get("ticker") == ticker and o.get("run_date") == run_date:
            o["outcome"], o["realised_return"], o["resolved_date"] = outcome, realised_return, _today()
            hit = True
    _save(store_path, store)
    return hit


# ---- 13.3 estimate -------------------------------------------------------------------------
def _acs_band(acs):
    if acs is None:
        return "unknown"
    return "85+" if acs >= 85 else "78-84" if acs >= 78 else "75-77" if acs >= 75 else "<75"


def _spearman(xs, ys):
    """Rank correlation with a tiny-sample guard. Returns None if n<4 or degenerate."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 4:
        return None
    xs2, ys2 = zip(*pairs)
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = rank(xs2), rank(ys2)
    try:
        return round(statistics.correlation(rx, ry), 3)   # py3.10+
    except Exception:
        return None


def estimate(store_path: str, state_path: str = None, gate_n: int = GATE_N) -> dict:
    """Recompute calibration params from RESOLVED observations. Sets calibration_gate_passed
    only when resolved_count >= gate_n. Writes vci_calibration_state.json when state_path given."""
    store = _load(store_path, {"observations": []})
    resolved = [o for o in store["observations"] if o.get("outcome") in ("win", "fail", "neutral")]
    n = len(resolved)

    # empirical p by (band, structure)
    p_by = {}
    for struct in ("platform", "single_asset"):
        for band in ("85+", "78-84", "75-77"):
            grp = [o for o in resolved if o.get("asset_structure") == struct and _acs_band(o.get("acs")) == band]
            wins = sum(1 for o in grp if o["outcome"] == "win")
            if grp:
                p_by[f"{struct}/{band}"] = {"p": round(wins / len(grp), 3), "n": len(grp)}

    # empirical L (mean loss on fails) by structure
    L_by = {}
    for struct in ("platform", "single_asset"):
        losses = [abs(o.get("realised_return", 0.0)) for o in resolved
                  if o.get("asset_structure") == struct and o["outcome"] == "fail" and o.get("realised_return") is not None]
        if losses:
            L_by[struct] = {"L": round(statistics.mean(losses), 3), "n": len(losses)}

    # per-component IC vs realised return
    rets = [o.get("realised_return") for o in resolved]
    comp_ic = {}
    for comp in ("asymmetry", "quality", "catalyst", "signals"):
        xs = [(o.get("components") or {}).get(comp) for o in resolved]
        comp_ic[comp] = _spearman(xs, rets)

    state = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "resolved_count": n,
        "calibration_gate_passed": n >= gate_n,
        "p_by_band": p_by,
        "L_by_structure": L_by,
        "component_ic": comp_ic,
        "weights": _load(state_path, {}).get("weights", DEFAULT_WEIGHTS),
    }
    _save(state_path, state)
    return state


# ---- 13.4 propose / gated-apply ------------------------------------------------------------
def propose_or_apply(state_path: str, changelog_path: str = None) -> dict:
    """Below the gate: PROPOSE weight deltas from component IC (no live change). Above the gate:
    APPLY within WEIGHT_STEP_CAP. Floors/rails are proposal-only, NEVER auto-applied (§13.4)."""
    state = _load(state_path, {})
    ic = state.get("component_ic", {})
    cur = dict(state.get("weights", DEFAULT_WEIGHTS))
    # target: weight proportional to max(IC,0) across components with a measured IC; else keep prior
    measured = {k: v for k, v in ic.items() if v is not None}
    result = {"gate_passed": state.get("calibration_gate_passed", False),
              "proposed": None, "applied": None, "note": ""}
    if len(measured) < 2:
        result["note"] = "insufficient measured IC — keep prior weights (advisory)"
        return result
    pos = {k: max(v, 0.0) for k, v in measured.items()}
    tot = sum(pos.values()) or 1.0
    target = {k: round(pos.get(k, cur.get(k, 0)) / tot, 3) for k in cur}
    result["proposed"] = target
    if not state.get("calibration_gate_passed"):
        result["note"] = f"below gate ({state.get('resolved_count',0)}/{GATE_N}) — proposal only"
        return result
    # apply within step cap
    applied = {}
    for k in cur:
        delta = max(-WEIGHT_STEP_CAP, min(WEIGHT_STEP_CAP, target.get(k, cur[k]) - cur[k]))
        applied[k] = round(cur[k] + delta, 3)
    tot2 = sum(applied.values()) or 1.0
    applied = {k: round(v / tot2, 3) for k, v in applied.items()}
    state["weights"] = applied
    _save(state_path, state)
    if changelog_path:
        log = _load(changelog_path, {"changes": []})
        log["changes"].append({"date": _today(), "from": cur, "to": applied, "reason": "IC-driven, step-capped"})
        _save(changelog_path, log)
    result["applied"] = applied
    result["note"] = "applied within step cap"
    return result


# ---- inline self-test (L-T1..L-T4) ---------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    store = f"{d}/vci_learning_store.json"; state = f"{d}/vci_calibration_state.json"; clog = f"{d}/vci_calibration_changelog.json"

    # L-T1 capture
    capture({"ticker": "ABCL", "run_date": "2026-07-12", "asset_structure": "platform", "acs": 78,
             "fv_asymmetry": 2.42, "components": {"asymmetry": .28, "quality": .12, "catalyst": .9, "signals": 1.0},
             "vci_source_score": 51.0, "decision": "deploy", "size_pct": 0.75}, store)
    assert len(_load(store, {})["observations"]) == 1
    capture({"ticker": "ABCL", "run_date": "2026-07-12", "asset_structure": "platform", "acs": 78}, store)  # upsert
    assert len(_load(store, {})["observations"]) == 1, "upsert must not duplicate"
    print("L-T1 capture + upsert OK")

    # L-T2 label
    assert label_outcome("ABCL", "2026-07-12", "win", 1.30, store)
    print("L-T2 label OK")

    # L-T3 estimate with a seeded synthetic history
    for i in range(14):
        capture({"ticker": f"SYN{i}", "run_date": "2026-06-01", "asset_structure": "platform", "acs": 80,
                 "components": {"asymmetry": (i % 5) / 5, "quality": .3, "catalyst": .5, "signals": .6}}, store)
        label_outcome(f"SYN{i}", "2026-06-01", "win" if i % 3 else "fail", (0.6 if i % 3 else -0.4), store)
    st = estimate(store, state, gate_n=GATE_N)
    print("L-T3 estimate: resolved", st["resolved_count"], "gate", st["calibration_gate_passed"],
          "| p_by", list(st["p_by_band"].items())[:1], "| ic", st["component_ic"])
    assert st["resolved_count"] >= GATE_N and st["calibration_gate_passed"]

    # L-T4 gated apply (above gate -> applies within cap)
    r = propose_or_apply(state, clog)
    print("L-T4 propose/apply:", r["note"], "| applied", r["applied"])
    assert r["applied"] is not None and abs(sum(r["applied"].values()) - 1.0) < 0.01  # 3dp rounding tolerance
    # L-T5 safety rail: floors never appear in applied weights
    assert set(r["applied"].keys()) == set(DEFAULT_WEIGHTS.keys())
    print("vci_learning self-test PASSED")
