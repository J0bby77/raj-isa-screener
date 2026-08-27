#!/usr/bin/env python3
"""
v21_golden_fixture.py — the V2.1 behaviour-neutrality guarantee (ISA-0354, R5.6, R4.13).

═══════════════════════════════════════════════════════════════════════════════════════════
WHAT THIS IS FOR
═══════════════════════════════════════════════════════════════════════════════════════════
V2.1 ships four items (A, B, C, D) of which C and D MOVE CAPITAL. The clean spec s10 puts A
first for one reason: "rollback of B-D is not deterministic without it."

This module is that determinism. It freezes the values every V2 flag is supposed to leave
alone, so that:

  1. Turning a V2 flag OFF reproduces the frozen state EXACTLY. If it does not, the rollback
     is not a rollback and the flag is decorative.
  2. A SHADOW-ONLY capability (correlation engine, evidence state, N_eff, A20) may be ON and
     must STILL reproduce the frozen state. That is what "shadow" means, and it is the single
     most important assertion in the V2.1-B build — the same role assertion F5 played in the
     fund-E[r] work, where a 100%-method change had to ship behaviour-neutral.

⚑ WHAT IT DELIBERATELY DOES NOT FREEZE. The anchor and everything derived from it. Those move
whenever the portfolio value moves, which is the whole point of ISA-0435 — freezing them would
re-create the defect this build exists to remove. Instead the fixture records the anchor and
the portfolio value it was struck at, and the checker REPORTS anchor movement as expected
change rather than failing on it (R2.3: attribution is arithmetic — how much was data, how
much was method).

⚑ AND IT DOES NOT FREEZE A NUMBER THE CODE HAPPENS TO PRINT. ISA-0383's lesson, recorded in
memory as "DO NOT re-freeze to whatever the code prints": a fixture struck from live output
launders an unexplained move into a baseline. `freeze()` therefore refuses to overwrite an
existing fixture unless `--force` is passed WITH a `--reason`, and stores the reason.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "v21_golden_fixture.json")
TOL_PP = 1e-6


def _sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def capture() -> dict:
    """The observable surface V2 must not disturb while its flags are off or shadow-only."""
    import isa_policy as pol
    import scoring_config as cfg
    with open(os.path.join(HERE, "target_weights.json"), encoding="utf-8") as fh:
        tw = json.load(fh)
    with open(os.path.join(HERE, "target_state.json"), encoding="utf-8") as fh:
        ts = json.load(fh)

    # ── anchor CONTEXT, recorded and not asserted (see the docstring) ────────────────────
    anchor = {
        "operative_pct": ts.get("required_return_operative_pct"),
        "derived_at": ts.get("derived_at"),
        "portfolio_value_gbp": ts.get("portfolio_value_gbp"),
        "portfolio_value_date": ts.get("portfolio_value_date"),
        "next_derivation_due": ts.get("next_derivation_due"),
    }
    # ── DERIVED thresholds, asserted RELATIVE to the anchor, never as levels ─────────────
    # A level assertion goes red every time the portfolio moves. An OFFSET assertion goes red
    # only when the DERIVATION changes, which is the thing a build can actually break.
    a = float(cfg.REQUIRED_RETURN_MID)
    offsets = {
        "ER_DEPLOY_FLOOR_minus_anchor": round(float(cfg.ER_DEPLOY_FLOOR) - a, 6),
        "FUND_GATE_PCT_minus_anchor": round(float(cfg.FUND_GATE_PCT) - a, 6),
        "FUND_GATE_pass_minus_anchor": round(float(cfg.FUND_GATE_BANDS["pass"]) - a, 6),
        "FUND_GATE_inconclusive_minus_anchor":
            round(float(cfg.FUND_GATE_BANDS["inconclusive"]) - a, 6),
        "VCI_hurdle_minus_anchor": round(float(cfg.VCI_REQUIRED_ANNUAL_RETURN) * 100.0 - a, 6),
    }
    # ── DECLARED policy: absolute, and must not move without a decision ──────────────────
    th, ss = tw.get("thresholds", {}), tw.get("stock_sleeve", {})
    declared = {
        "sizing_ladder_pct": ss.get("sizing_ladder_pct"),
        "evidence_state_to_rung": ss.get("evidence_state_to_rung"),
        "max_stock_position_pct": th.get("max_stock_position_pct"),
        "typical_stock_position_low": th.get("typical_stock_position_low"),
        "typical_stock_position_high": th.get("typical_stock_position_high"),
        "max_single_fund_pct": th.get("max_single_fund_pct"),
        "phase1_target_low": ss.get("phase1_target_low"),
        "phase1_target_high": ss.get("phase1_target_high"),
        "stepdown_ratchet_floor_pct": (ss.get("stepdown_ratchet") or {}).get("ratchet_floor_pct"),
        "MIN_HOLD_DAYS": getattr(cfg, "MIN_HOLD_DAYS", None),
        "VCI_SLEEVE_BINARY_RISK_BUDGET": getattr(cfg, "VCI_SLEEVE_BINARY_RISK_BUDGET", None),
        "ER_FRICTION_BUFFER": getattr(cfg, "ER_FRICTION_BUFFER", None),
        "SLEEVE_PROBATION_PP": getattr(cfg, "SLEEVE_PROBATION_PP", None),
    }
    return {
        "policy_version": pol.POLICY_VERSION,
        "anchor_context": anchor,
        "derived_offsets": offsets,
        "declared_policy": declared,
        "bucket_totals": tw.get("bucket_totals"),
        "flags": dict(pol.V2_FLAGS),
        "shadow_only": sorted(pol.V2_SHADOW_ONLY),
    }


def freeze(force=False, reason=None) -> dict:
    if os.path.exists(FIXTURE) and not force:
        raise SystemExit(
            f"REFUSED: {os.path.basename(FIXTURE)} already exists.\n"
            "  Re-freezing a fixture to whatever the code currently prints launders an\n"
            "  unexplained move into a baseline (ISA-0383). If the change is intended, pass\n"
            "  --force --reason '<what changed and why it is correct>'.")
    if force and not reason:
        raise SystemExit("REFUSED: --force requires --reason. A re-freeze with no stated cause "
                         "is indistinguishable from hiding a regression.")
    snap = capture()
    doc = {
        "_what": "V2.1 behaviour-neutrality fixture. See v21_golden_fixture.py.",
        "frozen_on": datetime.date.today().isoformat(),
        "frozen_reason": reason or "initial freeze at the V2.1-A build",
        "snapshot": snap,
        "sha": _sha(snap),
    }
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    return doc


def verify(live=None) -> dict:
    """Returns {status, holds, diffs, anchor_moved}. ABSENT is not a pass and not a failure."""
    if not os.path.exists(FIXTURE):
        return {"status": "ABSENT", "holds": None, "diffs": [],
                "detail": "no fixture frozen; run --freeze"}
    doc = json.load(open(FIXTURE, encoding="utf-8"))
    want = doc["snapshot"]
    got = live if live is not None else capture()
    diffs, notes = [], []

    # anchor: reported, never asserted
    wa, ga = want["anchor_context"], got["anchor_context"]
    anchor_moved = wa.get("operative_pct") != ga.get("operative_pct")
    if anchor_moved:
        notes.append(
            f"ANCHOR MOVED {wa.get('operative_pct')}% -> {ga.get('operative_pct')}% "
            f"(portfolio {wa.get('portfolio_value_gbp')} -> {ga.get('portfolio_value_gbp')}). "
            f"EXPECTED: the anchor is derived from the portfolio value (ISA-0435). The derived "
            f"OFFSETS below are what must hold.")

    for k, w in sorted(want["derived_offsets"].items()):
        g = got["derived_offsets"].get(k)
        if g is None or abs(float(g) - float(w)) > TOL_PP:
            diffs.append(f"derived_offsets.{k}: frozen {w} vs live {g} — the DERIVATION changed, "
                         f"not merely the anchor")
    for k, w in sorted(want["declared_policy"].items()):
        g = got["declared_policy"].get(k)
        if g != w:
            diffs.append(f"declared_policy.{k}: frozen {w!r} vs live {g!r} — a declared policy "
                         f"constant moved. This requires a decision, not a build.")
    if want.get("bucket_totals") != got.get("bucket_totals"):
        diffs.append("bucket_totals moved — see TW1 / ISA-0398")
    return {"status": "CHECKED", "holds": not diffs, "diffs": diffs,
            "anchor_moved": anchor_moved, "notes": notes,
            "frozen_on": doc.get("frozen_on"), "frozen_reason": doc.get("frozen_reason")}


def verify_flags_off() -> dict:
    """The rollback assertion: with every V2 flag OFF, the frozen state must still hold.

    ⚑ This is what makes the flags real rather than decorative. A flag that is read by nothing
    passes trivially here, which is why the battery ALSO asserts that each flag is referenced
    on disk (test_v21_a_governance)."""
    import isa_policy as pol
    saved = dict(pol.V2_FLAGS)
    try:
        for k in pol.V2_FLAGS:
            pol.V2_FLAGS[k] = False
        return verify()
    finally:
        pol.V2_FLAGS.clear()
        pol.V2_FLAGS.update(saved)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reason")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.freeze:
        d = freeze(force=args.force, reason=args.reason)
        print(f"frozen {d['frozen_on']} sha={d['sha']}"); sys.exit(0)
    r = verify()
    print(json.dumps(r, indent=1))
    for n in r.get("notes", []):
        print("  NOTE:", n)
    sys.exit(0 if r["status"] == "ABSENT" or r["holds"] else 1)
