#!/usr/bin/env python3
"""
isa_policy.py — ISA V2.1 governance layer: policy version, feature flags, and THE single
guarded accessor for every anchor-derived threshold.

Built 26-Aug-2026 for ISA-0354 (V2.1-A). Authority: ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS — one failure class, eight live instances
═══════════════════════════════════════════════════════════════════════════════════════════

`scoring_config.ER_DEPLOY_FLOOR` is DERIVED: `REQUIRED_RETURN_MID + ER_FRICTION_BUFFER`, and
`REQUIRED_RETURN_MID` is itself derived by `derive_required_return.py`, which solves for the
return that takes the CURRENT PORTFOLIO VALUE plus the contribution schedule to the GBP 1m
floor by `target_date`.

⚑ THAT IS THE WHOLE ANSWER TO "13.8 OR 13.9". Neither is a declaration. They are the SAME
  derivation at two portfolio values:
      12-Jul-2026   NAV GBP 144,342.19  ->  13.9%
      12-Aug-2026   NAV GBP 139,738.00  ->  13.8%
  The anchor re-derives at the 30-Sep-2026 window and WILL move again when the September
  GBP 11,250 lands. Anything that stores 13.8 or 13.9 as a constant is stale the moment the
  market moves. (Raj, 26-Aug-2026 — the challenge that dissolved ISA-0431's open question.)

The R4.6 enumeration run before this build found the literal-shadow class in FOUR syntactic
forms. ISA-0432 recorded four instances; disk holds eight:

  FORM 1 — literal default beside the name in a call
      bottleneck_fv.py:113     _c("VCI_REQUIRED_ANNUAL_RETURN", 0.14)   vs live 0.138  [NEW]
      fund_returns.py:193      _g("FUND_GATE_PCT", 12.0)                vs live 11.9   [NEW]
      step9_pre_builder.py:1111 getattr(_cfg,"ER_DEPLOY_FLOOR",15.9)    vs live 15.8
      t1_gates.py:341/476      _c("ER_DEPLOY_FLOOR", 15.9)              vs live 15.8
  FORM 2 — accessor function with a bare numeric fallthrough
      fund_action_stack.py:259 _anchor_floor() -> 13.9                  vs live 13.8
  FORM 3 — module-level import-time fallback record
      scoring_config.py:413    _load_target_state() -> {..: 13.9, ..}   vs live 13.8
  FORM 4 — a JSON policy file restating a derived quantity
      target_weights.json      total_isa_on_track 0.14                  vs live 0.138

⚑ FORM 4 IS WHY A GREP OVER *.py WOULD NOT HAVE CLOSED THIS. The check below is AST-based
  over Python AND value-based over the JSON policy files, because the class does not live in
  one language.

⚑ AND THE FAILURE IS NOT HYPOTHETICAL. `test_return_architecture` has been RED in the
  delivered location on exactly this: "derived thresholds reproduce the frozen constants at
  today's anchor" fails because target_weights' 14.0 no longer equals the derived 13.8.

═══════════════════════════════════════════════════════════════════════════════════════════
THE CONTRACT (R4.1, R4.7)
═══════════════════════════════════════════════════════════════════════════════════════════
`derived(name)` returns a float or RAISES `PolicyUnavailable`. It NEVER returns a default.
"Missing" is not representable as a number here — that is the entire point. A caller that
wants to degrade gracefully must catch the exception and say so out loud, which is visible in
review; a caller that supplies `15.9` is invisible until the number is wrong.

ROLLBACK (R4.13) — one constant each, never a code revert:
    V2_FLAGS["<capability>"] = False   restores pre-V2.1 behaviour for that capability
    POLICY_STRICT_ACCESSOR   = False   restores the old default-on-absence behaviour
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════════════
# 1. POLICY VERSION
# ═══════════════════════════════════════════════════════════════════════════════════════
POLICY_VERSION = "ISA_V2_1"
POLICY_VERSION_DATE = "2026-08-26"
POLICY_AUTHORITY = "ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md"

# ═══════════════════════════════════════════════════════════════════════════════════════
# 2. FEATURE FLAGS — rollback is one constant (R4.13)
# ═══════════════════════════════════════════════════════════════════════════════════════
# Each flag gates exactly one capability. A flag that is False must reproduce the
# pre-V2.1 behaviour EXACTLY — asserted by the golden fixture in test_v21_a_governance.py.
V2_FLAGS: Dict[str, bool] = {
    # --- V2.1-A (governance) -----------------------------------------------------------
    "policy_strict_accessor":     True,   # derived() raises instead of defaulting
    "duplicate_threshold_check":  True,   # consistency_check fails on a literal shadow copy
    "stock_ladder_caps":          True,   # ISA-0427: max_stock_position_pct 0.05 -> 0.065

    # --- V2.1-B (measurement, SHADOW ONLY — moves no capital) --------------------------
    "correlation_engine":         True,   # 104w Friday GBP total-return store + shrinkage
    "evidence_state":             True,   # counted-channel evidence classifier
    "n_eff_diagnostic":           True,   # diversification ratio, reported not enforced

    # --- V2.1-C (sizing — MOVES CAPITAL) -----------------------------------------------
    "fixed_ladder":               True,   # 3.5 / 4.5 / 5.5 / 6.5
    "demand_pull_stock_max":      True,   # ISA-0430: stock_max = sum of qualifying uses
    "correlation_admission_gate": True,   # rho gate -> REPLACEMENT_ONLY
    "vci_budget_sizing":          True,   # w_vci derived from B and N, binary map deleted

    # --- V2.1-D (lifecycle — MOVES CAPITAL) --------------------------------------------
    "drawdown_b1_fix":            True,   # sqrt(252) correction
    "risk_contribution_hybrid":   True,   # D10 flag + M1/M2/M3 monitors
    "retention_realised_fraction": True,  # er_entry persistence + re-underwrite
    "stepdown_ratchet":           True,   # replaces the D6 probation revert-to-floor
    "a20_slot_competition":       True,   # SHADOW ONLY for >= 2 runs

    # --- PHASE 0 (enforcement — the 27-Aug-2026 build spec) -----------------------------
    # ⚑ These four move NO capital. They measure whether the capital-moving code above is
    # actually EXECUTED, DECLARED and EVIDENCED. Phase 0 exists because none of the twelve
    # defects found on 26/27-Aug required insight: every one was findable by a mechanical,
    # enumerable procedure that nothing was running when nobody was building (spec §2.3).
    # --- PHASE 1 (authority — MOVES CAPITAL) --------------------------------------------
    "single_sizing_authority":    True,   # P7: thesis_state replaces the /100 as the gate

    # --- PHASE 2/3 (measurement + selection) -------------------------------------------
    # ⚑ ISA-0577 (03-Sep-2026). `symbol_map_refresh` gates Step 5x, which brings
    # stock_symbol_map.json up to the live universe before 5y fetches it. Without it the map is
    # a hand-run artefact frozen at its built_on date while the universe grows every month: on
    # 03-Sep-2026, 119 of 178 names (66.9%) were unmapped, unfetched, UNMEASURED, sized on
    # A2.3's adverse 0.70 and capped at STARTER. False reproduces 02-Sep-2026 behaviour exactly.
    "symbol_map_refresh":         True,   # P1.2c: Step 5x refreshes the ticker->Yahoo symbol map
    "stock_return_fetch":         True,   # P1: populate the weekly GBP total-return store
    "stock_candidate_pipeline":   True,   # P3: a real candidate list reaches stock_max
    "deployment_sequencer":       True,   # P6: correlation orders the queue, never the score

    # --- PHASE 4 (capital — MOVES THE MOST CAPITAL IN THE BUILD) ------------------------
    # ⚑ P4.7: False is a REFUSAL (stock_max = 0, STOCK_SLEEVE_REFUSED), never a band-floor
    # number. A rollback that computes a DIFFERENT number is a second authority.
    "demand_pull_live":           True,   # P4: sleeve_split reads position_sizing.stock_max
    "partial_starter_entry":      True,   # P4: D15-D17 floor-then-priority fill
    # ⚑ ISA-0490 (29-Aug-2026). The three flags above were ON and the three modules they
    # govern were GREEN, and `build()` still called none of them: it passed candidates=None
    # to sleeve_split, which read it as [] and routed every pound to funds. A flag that is ON
    # over a call that does not happen is FC-E wearing a green light. This flag governs the
    # CALL, not the capability — turning it off makes sleeve_split REFUSE, never fall back.
    "capital_pipeline_wired":     True,   # P3->P6->P4 reached from capital_destination.build

    "execution_ledger":           True,   # P0.1 reachable is not live
    "quantity_register":          True,   # P0.2 one quantity, one computer, one surface
    "threshold_register":         True,   # P0.3 = R15.2 — divide the threshold by the SD
    "negative_claim_expiry":      True,   # P0.4 a claim of absence carries a test and a date
}

# A capability that ships in shadow may be ON without moving capital. Declared here so a
# reader never has to infer it, and asserted by the battery.
V2_SHADOW_ONLY = frozenset({
    "correlation_engine", "evidence_state", "n_eff_diagnostic", "a20_slot_competition",
    # Phase 0 OBSERVES the framework; it never sizes, gates or routes a pound. It can fail a
    # BUILD and it can fail a RUN — which is not the same thing as moving capital, and the
    # distinction is declared here so a reader never has to infer it.
    "execution_ledger", "quantity_register", "threshold_register", "negative_claim_expiry",
})

# NOTE: the strict-accessor rollback is the V2_FLAGS entry "policy_strict_accessor", read via
# flag() at the point of use. It is NOT a second module constant — that would be exactly the
# two-homes defect this module exists to kill.

# ⚑ THE WIRING LEDGER — this project's dominant failure mode is an absent execution that
# reports success (FC-E, six recorded occurrences, the last at FUNCTION granularity). A feature
# flag nothing reads is the purest form of it: the flag is green, the capability is absent, and
# the battery is satisfied. So `test_v21_a_governance` asserts that EVERY flag is either
# referenced somewhere on disk OR named here with the item that will wire it. A flag can
# therefore never be quietly decorative — landing a capability means deleting its line below,
# and forgetting to wire one means the suite goes red.
V2_FLAGS_PENDING_WIRE = {
    "correlation_engine":          "ISA-0355 (V2.1-B)",
    "evidence_state":              "ISA-0355 (V2.1-B)",
    "n_eff_diagnostic":            "ISA-0355 (V2.1-B)",
    "fixed_ladder":                "ISA-0356 (V2.1-C)",
    "demand_pull_stock_max":       "ISA-0356 (V2.1-C)",
    "correlation_admission_gate":  "ISA-0356 (V2.1-C)",
    "vci_budget_sizing":           "ISA-0356 (V2.1-C)",
    "drawdown_b1_fix":             "ISA-0357 (V2.1-D)",
    "risk_contribution_hybrid":    "ISA-0357 (V2.1-D)",
    "retention_realised_fraction": "ISA-0357 (V2.1-D)",
    "stepdown_ratchet":            "ISA-0357 (V2.1-D)",
    "a20_slot_competition":        "ISA-0357 (V2.1-D)",
}


def flag(name: str) -> bool:
    """Read a V2 feature flag. An unknown flag RAISES — a typo must not read as False,
    which would silently disable a capability (FC-A)."""
    if name not in V2_FLAGS:
        raise KeyError(
            f"isa_policy.flag: unknown flag {name!r}. Declared flags: "
            f"{sorted(V2_FLAGS)}. A flag is declared here or it does not exist."
        )
    return bool(V2_FLAGS[name])


# ═══════════════════════════════════════════════════════════════════════════════════════
# 3. THE DERIVED-THRESHOLD REGISTRY — the single declaration of what is derived
# ═══════════════════════════════════════════════════════════════════════════════════════
# `home`        : the module that COMPUTES it. Exactly one (R4.4).
# `derivation`  : how, in one line, for the reader who finds a surprising number.
# `json_aliases`: keys in JSON POLICY files that state the SAME QUANTITY. Any such key is a
#                 FORM 4 shadow copy and the consistency check compares its value.
# `unit`        : "pct" (13.8 == 13.8%) or "frac" (0.138 == 13.8%). The two are mixed across
#                 this codebase and comparing them naively is how a 100x error hides
#                 (ISA-0429). Every comparison below converts explicitly.
class PolicyUnavailable(RuntimeError):
    """A derived threshold could not be obtained. NEVER caught-and-defaulted inside this
    module — the caller decides, out loud."""


DERIVED_THRESHOLDS: Dict[str, Dict[str, Any]] = {
    "REQUIRED_RETURN_MID": {
        "home": "derive_required_return.py -> target_state.json",
        "derivation": "monthly-compounded solve: portfolio_value + contribution_schedule "
                      "-> target_floor_gbp by target_date, clamped to [10.0, 18.0]",
        "unit": "pct",
        "json_aliases": [
            # role SHADOW: nothing on disk reads these. They sit in a policy file looking
            # authoritative and govern nothing — return_architecture's own note names the hazard:
            # "delete it or mark it, but do not let a future reader gate on it."
            ("target_weights.json", ("return_objective", "working_target"), "frac", "shadow"),
            ("target_weights.json", ("return_objective", "total_isa_working_target"), "frac", "shadow"),
            # role CORROBORATOR: return_architecture.threshold_parity() compares this against the
            # derived value EVERY RUN and publishes any divergence in `divergences[]`, with the
            # derived value declared operative. That is R5.2 working as intended, not a shadow
            # copy — so the check verifies the REPORTER EXISTS rather than that the values agree.
            ("target_weights.json", ("thresholds", "total_isa_on_track"), "frac", "corroborator"),
            ("target_weights.json", ("thresholds", "total_isa_watch_low"), "frac", "corroborator"),
        ],
        "legacy_default": 13.9,
    },
    "ER_DEPLOY_FLOOR": {
        "home": "scoring_config.py (REQUIRED_RETURN_MID + ER_FRICTION_BUFFER)",
        "derivation": "the anchor plus the friction/FX/estimation buffer",
        "unit": "pct",
        "json_aliases": [],
        "legacy_default": 15.9,
    },
    "FUND_GATE_PCT": {
        "home": "scoring_config.py (REQUIRED_RETURN_MID - 1.9)",
        "derivation": "the anchor less the margin the stock sleeve is expected to contribute",
        "unit": "pct",
        "json_aliases": [
            # SUPERSEDED, and return_architecture declares it so explicitly: the single
            # pass/fail line was replaced by the A11/D8 two-band scheme. Comparing it to either
            # band would manufacture a divergence out of a structural change.
            ("target_weights.json", ("thresholds", "fund_sleeve_weighted_avg_min"),
             "frac", "superseded"),
            # SHADOW: a second copy of the same superseded quantity, in a different section, that
            # nothing reads at all.
            ("target_weights.json", ("return_objective",
                                     "fund_sleeve_weighted_avg_threshold"), "frac", "shadow"),
        ],
        "legacy_default": 12.0,
    },
    "VCI_REQUIRED_ANNUAL_RETURN": {
        "home": "scoring_config.py (REQUIRED_RETURN_MID / 100)",
        "derivation": "the anchor as a fraction — the VCI E1 hurdle h",
        "unit": "frac",
        "json_aliases": [],
        "legacy_default": 0.14,
    },
}

# A corroborator alias is legitimate ONLY while something actually compares and publishes it.
# If the reporter disappears, the "corroborator" silently becomes a shadow copy — so the check
# asserts the reporter exists and emits a divergence field, not that the values agree.
ALIAS_REPORTERS = {
    "corroborator": ("return_architecture.py", "thresholds", "divergences"),
    "superseded":   ("return_architecture.py", "thresholds", "superseded_constants"),
}

# Constants that are DECLARED, not derived. A literal default beside one of these is fine —
# it restates a declaration, it does not shadow a moving quantity. Listed so the check can
# tell the two apart rather than flagging every default in the codebase.
DECLARED_CONSTANTS = frozenset({
    "MIN_HOLD_DAYS", "VCI_SLEEVE_BINARY_RISK_BUDGET", "ER_FRICTION_BUFFER",
    "FUND_MIN_COVERAGE", "VCI_FLOOR_MAX", "SLEEVE_PROBATION_PP",
})


def _target_state() -> Optional[dict]:
    try:
        with open(os.path.join(HERE, "target_state.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def derived(name: str, cfg=None) -> float:
    """THE guarded accessor. Returns a float or RAISES PolicyUnavailable (R4.7).

    Never returns a default. A caller that cannot proceed without the value must fail; a
    caller that can degrade must catch this explicitly, so the degradation is visible."""
    if name not in DERIVED_THRESHOLDS:
        raise KeyError(
            f"isa_policy.derived: {name!r} is not a declared derived threshold. "
            f"Declared: {sorted(DERIVED_THRESHOLDS)}. If it is a DECLARED constant read it "
            f"from scoring_config directly."
        )
    if cfg is None:
        try:
            import scoring_config as cfg  # noqa: PLC0415
        except Exception as exc:
            raise PolicyUnavailable(
                f"{name}: scoring_config is unimportable ({exc}). The anchor cannot be "
                f"derived, so no capital decision may be priced. Fix the config."
            ) from exc
    val = getattr(cfg, name, None)
    if val is None:
        spec = DERIVED_THRESHOLDS[name]
        raise PolicyUnavailable(
            f"{name} is absent from scoring_config. It is DERIVED by {spec['home']} "
            f"({spec['derivation']}). Refusing to substitute a literal: the legacy value "
            f"{spec['legacy_default']} was correct at a portfolio value that no longer holds."
        )
    if not flag("policy_strict_accessor"):
        return float(val)          # ROLLBACK path: pre-V2.1 permissive behaviour
    fval = float(val)
    if fval != fval or fval in (float("inf"), float("-inf")):
        raise PolicyUnavailable(f"{name} is not a finite number: {val!r}")
    return fval


def as_pct(name: str, cfg=None) -> float:
    """The threshold in PERCENT units (13.8 == 13.8%), whatever its native unit."""
    v = derived(name, cfg=cfg)
    return v * 100.0 if DERIVED_THRESHOLDS[name]["unit"] == "frac" else v


def policy_stamp(cfg=None) -> dict:
    """The stamp every V2 artefact carries, so a reader can tell which policy produced it
    (R4.2 — every figure carries as_of and source)."""
    ts = _target_state() or {}
    out = {
        "policy_version": POLICY_VERSION,
        "policy_authority": POLICY_AUTHORITY,
        "anchor_operative_pct": ts.get("required_return_operative_pct"),
        "anchor_derived_at": ts.get("derived_at"),
        "anchor_next_due": ts.get("next_derivation_due"),
        "anchor_portfolio_value_gbp": ts.get("portfolio_value_gbp"),
        "anchor_portfolio_value_date": ts.get("portfolio_value_date"),
        "flags_on": sorted(k for k, v in V2_FLAGS.items() if v),
        "flags_off": sorted(k for k, v in V2_FLAGS.items() if not v),
        "shadow_only": sorted(V2_SHADOW_ONLY),
    }
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(policy_stamp(), indent=1))
    for n in sorted(DERIVED_THRESHOLDS):
        try:
            print(f"  {n:32s} = {derived(n):>8.4f}  ({as_pct(n):.3f}%)")
        except PolicyUnavailable as e:
            print(f"  {n:32s} = UNAVAILABLE — {e}")
            sys.exit(1)
