#!/usr/bin/env python3
"""
regime_resolver.py — resolves the two-regime contradiction. 02-Aug-2026.

THE PROBLEM AS RAISED
---------------------
"The framework is carrying two contradictory regime variables. My Step 4 macro call is
Slowdown; `drawdown_state.regime_state` is RISK_ON. They drive different machinery and they
disagree — Slowdown argues for a defensive tilt, RISK_ON forbids one. That needs resolving
before Category 8 can ever fire sensibly."

THE DIAGNOSIS — they do not actually disagree, and that is the harder problem
----------------------------------------------------------------------------
They cannot agree or disagree, because they have no shared referent:

  macro_regime   Expansion | Slowdown | Contraction | Recovery
                 A Step 4 JUDGEMENT about the ECONOMY — rates, inflation, growth, trade.
                 FORWARD-LOOKING. Consumed by Step 6.3's fund-band tilt.

  market_regime  RISK_ON | LATE_CYCLE | RISK_OFF | RECOVERY
                 A MECHANICAL classification of PRICE BEHAVIOUR — VUAG vs its 200-day MA,
                 drawdown band off the 252-day high, 63-day slope. LAGGING BY CONSTRUCTION.
                 Consumed by the B1 drawdown ladder, B7 admission doors and B4 Category 8.

A slowing economy alongside a market 2.5% off its high and above its 200dma is not a
contradiction. It is the single most ordinary configuration in a late expansion: fundamentals
soften first, prices roll over later. 1999-2000, 2007 and 2021 all looked exactly like this.

So the defect is NOT that the two numbers conflict. It is that:

  1. **Nothing in the framework says they are different variables.** Both are called "regime".
     Both are four-state. Both contain a state spelled Recovery/RECOVERY. A reader — human or
     code — naturally reads them as two readings of one thing, and a future edit will
     eventually cross-wire them. The collision is the bug; the disagreement is the symptom.

  2. **Category 8 is unreachable by construction.** `REGIME_B4_MENU["RISK_ON"] = []` — no
     tactical tilt without documented cause. But market_regime only leaves RISK_ON once price
     damage has ALREADY happened. The macro case for a defensive tilt arrives BEFORE that, by
     definition. So the one variable that gates Category 8 is structurally incapable of
     expressing the reason you would ever want to use it. The Run_Context calls the category
     "unforced — most months it correctly ranks nowhere". That is generous: it cannot rank
     anywhere, whatever the evidence.

     This also sits badly with the framework's own stated identity. It is FORWARD-LED — it
     exists to act on signals that lead price. Gating its only defensive instrument purely on
     a lagging, price-confirmed variable contradicts that identity.

THE RESOLUTION — three parts
-----------------------------
1. **NAMESPACE THEM.** `macro_regime` and `market_regime`. Never "regime" unqualified. This
   module refuses to answer an unqualified query, and `consistency_check` pair M10 fails if
   operative prose uses the bare term where one of the two is meant.

2. **PRECEDENCE, STATED ONCE.** The governing principle:

       Mechanical price state governs anything that MOVES CAPITAL automatically.
       Macro judgement governs anything that only SHIFTS A THRESHOLD.
       The two must AGREE only where a discretionary, reversible, capped tilt is authorised.

   B1 drawdown tranches: market_regime only — a macro opinion must never fire a deployment
   tranche. Step 6.3 fund bands: macro_regime only — it moves no capital, it moves an action
   threshold. B7 doors: market_regime only. Category 8: BOTH, per (3).

3. **CATEGORY 8 GATES ON THE PAIR.** A defensive tilt becomes permissible under RISK_ON when
   macro_regime is Slowdown or Contraction AND at least one MECHANICAL corroborator is
   present — so the door opens on evidence, not on mood. Selection of which exposure remains
   judgement, as designed.

   SHIPPED AS ADVISORY, NOT LIVE. Per build hazard H7 nothing here adjusts a live gate. This
   follows the framework's own established pattern for exactly this class of change — B7's own
   doors run with `REGIME_DOORS_ACTIVE = False`, shadow first, go-live reviewed in September.
   `CATEGORY8_COMPOSITE_ACTIVE` defaults False; the composite is computed, emitted and
   reviewed. Registered as REG-1.

CLI:
  python3 regime_resolver.py --selftest
  python3 regime_resolver.py --resolve --macro Slowdown
"""
from __future__ import annotations
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))

MACRO_STATES = ("Expansion", "Slowdown", "Contraction", "Recovery")
MARKET_STATES = ("RISK_ON", "LATE_CYCLE", "RISK_OFF", "RECOVERY")

# The two vocabularies overlap on exactly one token, and that overlap is the whole reason the
# collision is so easy to make. Renaming either state would break stored drawdown_state files
# and every REGIME_* config key, so instead the collision is made LOUD: any interface that
# carries a regime value carries its namespace with it.
COLLIDING_TOKENS = {m.upper() for m in MACRO_STATES} & set(MARKET_STATES)   # {"RECOVERY"}

# Which variable governs which machinery. This table IS the precedence rule.
AUTHORITY = {
    "b1_drawdown_ladder":   "market_regime",   # moves capital automatically -> mechanical only
    "b7_admission_doors":   "market_regime",   # admits names to a screen    -> mechanical only
    "step6_3_fund_tilt":    "macro_regime",    # moves a threshold, not money-> judgement only
    "category8_tactical":   "both",            # discretionary, reversible, capped -> the pair
    "min_hold":             "neither",         # C-1 anti-churn is regime-INDEPENDENT, always
}

DEFENSIVE_MACRO = ("Slowdown", "Contraction")

# Mechanical corroborators. At least one must hold before a defensive tilt is permitted under a
# RISK_ON market. None of them is a price-drawdown test — the entire point is to admit evidence
# that LEADS price, since a drawdown test is what already gates the category into unreachability.
CORROBORATORS = {
    "breadth_deteriorating": "63-day slope negative while still above the 200-day MA "
                             "(price holding up on narrowing participation)",
    "revisions_negative":    "net 30-day earnings revisions negative across the qualified set",
    "curve_inverted":        "2s10s inverted or re-steepening from inversion",
    "credit_widening":       "IG or HY spreads widened materially over 3 months",
    "vol_term_inverted":     "VIX term structure in backwardation",
}

CATEGORY8_COMPOSITE_ACTIVE = False   # H7: shadow first. Go-live decision Sep-2026 (REG-1).


class AmbiguousRegimeError(ValueError):
    """Raised when something asks for 'the regime' without saying which one."""


def get_regime(which, macro=None, market=None):
    """Deliberately has NO default. Asking this module for 'the regime' is a bug, and it says
    so rather than picking one and being right half the time."""
    if which not in ("macro_regime", "market_regime"):
        raise AmbiguousRegimeError(
            f"get_regime({which!r}): there is no such thing as 'the regime' in this framework. "
            f"Ask for 'macro_regime' (Step 4 judgement about the economy; "
            f"{MACRO_STATES}) or 'market_regime' (mechanical price state from drawdown_monitor; "
            f"{MARKET_STATES}). They measure different things and govern different machinery — "
            f"see AUTHORITY.")
    return macro if which == "macro_regime" else market


def read_market_regime(state_path=None):
    """market_regime from drawdown_state.json. Never inferred, never defaulted."""
    p = state_path or os.path.join(HERE, "drawdown_state.json")
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except Exception as e:
        return None, {"error": f"drawdown_state unreadable: {e}"}
    r = st.get("regime_state")
    if r is not None and r not in MARKET_STATES:
        return None, {"error": f"unknown market_regime {r!r}"}
    return r, st.get("regime_basis") or {}


def normalise_macro(value):
    """Accept the Step 4 output in the form the review actually writes it."""
    if value is None:
        return None
    v = str(value).strip().strip(".").title()
    return v if v in MACRO_STATES else None


def resolve(macro_regime=None, market_regime=None, corroborators=None, state_path=None,
            active=None):
    """Return the full two-regime picture plus the advisory Category-8 verdict."""
    active = CATEGORY8_COMPOSITE_ACTIVE if active is None else active
    macro = normalise_macro(macro_regime)
    basis = {}
    if market_regime is None:
        market_regime, basis = read_market_regime(state_path)
    corr = {k: bool(v) for k, v in (corroborators or {}).items() if k in CORROBORATORS}
    corr_true = [k for k, v in corr.items() if v]

    out = {
        "macro_regime": macro,
        "macro_regime_source": "step4_judgement",
        "macro_regime_governs": [k for k, v in AUTHORITY.items() if v in ("macro_regime", "both")],
        "market_regime": market_regime,
        "market_regime_source": "drawdown_monitor.classify_regime (mechanical)",
        "market_regime_basis": basis,
        "market_regime_governs": [k for k, v in AUTHORITY.items() if v in ("market_regime", "both")],
        "authority": dict(AUTHORITY),
        "corroborators": corr,
        "corroborators_present": corr_true,
        "composite_active": active,
    }

    # ---- the relationship between them, named ------------------------------------------
    out["relationship"] = _describe(macro, market_regime)

    # ---- advisory Category-8 eligibility -----------------------------------------------
    legacy_menu = _legacy_menu(market_regime)
    out["category8_legacy_menu"] = legacy_menu
    verdict, reason, menu = _category8(macro, market_regime, corr_true, legacy_menu)
    out["category8"] = {
        "eligible": verdict,
        "permitted_tilts": menu,
        "reason": reason,
        "binding": bool(active),
        "note": ("ADVISORY ONLY — CATEGORY8_COMPOSITE_ACTIVE is False. The legacy "
                 "REGIME_B4_MENU still governs. Registered as REG-1; go-live reviewed "
                 "Sep-2026, following the same shadow-first pattern as the B7 doors."
                 if not active else
                 "LIVE — the composite gate governs Category 8 admission."),
    }
    return out


def _describe(macro, market):
    if macro is None or market is None:
        return {"state": "INCOMPLETE",
                "text": "One of the two regimes is unavailable; no relationship can be stated."}
    defensive_macro = macro in DEFENSIVE_MACRO
    benign_market = market in ("RISK_ON",)
    if defensive_macro and benign_market:
        return {"state": "LEAD_LAG",
                "text": (f"macro_regime={macro} (forward, economic) alongside "
                         f"market_regime={market} (lagging, price-confirmed) is NOT a "
                         f"contradiction — it is the ordinary late-expansion configuration in "
                         f"which fundamentals soften before prices do. It is precisely the "
                         f"window in which a defensive tilt is cheapest, and precisely the "
                         f"window the legacy single-variable gate forbids one.")}
    if not defensive_macro and market in ("RISK_OFF", "LATE_CYCLE"):
        return {"state": "PRICE_LEADS",
                "text": (f"market_regime={market} has deteriorated while macro_regime={macro} "
                         f"has not. Price is leading the economic read. Treat the macro call as "
                         f"the one more likely to be revised.")}
    if defensive_macro and market in ("RISK_OFF", "LATE_CYCLE"):
        return {"state": "ALIGNED_DEFENSIVE",
                "text": "Both readings agree that risk is elevated."}
    return {"state": "ALIGNED_CONSTRUCTIVE",
            "text": "Both readings are constructive."}


def _legacy_menu(market):
    try:
        sys.path.insert(0, HERE)
        import scoring_config as cfg
        return list((getattr(cfg, "REGIME_B4_MENU", {}) or {}).get(market, []))
    except Exception:
        return {"RISK_ON": [], "LATE_CYCLE": ["min_vol", "quality"],
                "RISK_OFF": ["hold_existing"],
                "RECOVERY": ["equal_weight", "value"]}.get(market, [])


def _category8(macro, market, corr_true, legacy_menu):
    """The composite gate. Widens the legacy menu ONLY in the one case that made the category
    unreachable, and only on mechanical corroboration."""
    if macro is None or market is None:
        return False, ("Cannot assess: one of the two regimes is unavailable. Category 8 is "
                       "unforced — absence of a regime read is a reason not to act, never a "
                       "reason to act."), []
    if legacy_menu:
        return True, (f"market_regime={market} already permits {legacy_menu} under the legacy "
                      f"REGIME_B4_MENU. The composite changes nothing here."), legacy_menu
    if market != "RISK_ON":
        return False, (f"market_regime={market} permits no tilt and is not the RISK_ON case the "
                       f"composite addresses."), []
    # market_regime == RISK_ON, legacy menu empty — the unreachable case.
    if macro not in DEFENSIVE_MACRO:
        return False, (f"market_regime=RISK_ON and macro_regime={macro}: both readings are "
                       f"constructive. No tilt, and no disagreement to resolve."), []
    if not corr_true:
        return False, (f"macro_regime={macro} argues for a defensive tilt but market_regime="
                       f"RISK_ON and NO mechanical corroborator is present. A macro judgement "
                       f"alone must not open this door — that is how a discretionary defensive "
                       f"tilt gets taken every time the news feels bad. Provide at least one of "
                       f"{sorted(CORROBORATORS)}."), []
    return True, (f"macro_regime={macro} (forward) argues for defensiveness; market_regime="
                  f"RISK_ON (lagging) has not yet confirmed; and {len(corr_true)} mechanical "
                  f"corroborator(s) present: {corr_true}. This is the lead-lag window the "
                  f"legacy single-variable gate could never express. Tilt PERMITTED, capped and "
                  f"reversible as per B4; selection remains judgement."), ["min_vol", "quality"]


# ── self-test ────────────────────────────────────────────────────────────────────────────

def _selftest():
    fails = []

    def ok(label, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(label)

    # 1. The module refuses to answer an ambiguous question.
    raised = False
    try:
        get_regime("regime")
    except AmbiguousRegimeError:
        raised = True
    ok("U-RG1 'the regime' is refused, not guessed", raised)
    ok("U-RG2 both namespaced queries answer",
       get_regime("macro_regime", "Slowdown", "RISK_ON") == "Slowdown"
       and get_regime("market_regime", "Slowdown", "RISK_ON") == "RISK_ON")

    # 2. The collision that makes conflation easy is identified, not hidden.
    ok("U-RG3 the one colliding token is named", COLLIDING_TOKENS == {"RECOVERY"},
       str(COLLIDING_TOKENS))

    # 3. THE ACTUAL CASE, 02-Aug-2026.
    r = resolve(macro_regime="Slowdown", market_regime="RISK_ON")
    ok("U-RG4 Slowdown + RISK_ON classified LEAD_LAG, not 'contradiction'",
       r["relationship"]["state"] == "LEAD_LAG")
    ok("U-RG5 legacy menu under RISK_ON is empty — Category 8 unreachable",
       r["category8_legacy_menu"] == [])
    ok("U-RG6 composite still refuses WITHOUT mechanical corroboration",
       r["category8"]["eligible"] is False
       and "NO mechanical corroborator" in r["category8"]["reason"])

    r2 = resolve(macro_regime="Slowdown", market_regime="RISK_ON",
                 corroborators={"revisions_negative": True})
    ok("U-RG7 composite PERMITS with one corroborator", r2["category8"]["eligible"] is True)
    ok("U-RG8 permitted tilts are defensive", r2["category8"]["permitted_tilts"] == ["min_vol", "quality"])
    ok("U-RG9 but it is ADVISORY, not binding (H7 — shadow first)",
       r2["category8"]["binding"] is False and "ADVISORY" in r2["category8"]["note"])
    r2live = resolve(macro_regime="Slowdown", market_regime="RISK_ON",
                     corroborators={"revisions_negative": True}, active=True)
    ok("U-RG9b ...and becomes binding only when explicitly activated",
       r2live["category8"]["binding"] is True)

    # 4. A macro opinion alone can never open the door.
    r3 = resolve(macro_regime="Contraction", market_regime="RISK_ON",
                 corroborators={"revisions_negative": False})
    ok("U-RG10 a false corroborator is not a corroborator", r3["category8"]["eligible"] is False)

    # 5. Constructive macro under RISK_ON: nothing to resolve.
    r4 = resolve(macro_regime="Expansion", market_regime="RISK_ON")
    ok("U-RG11 Expansion + RISK_ON -> no tilt, no disagreement",
       r4["category8"]["eligible"] is False and "no disagreement" in r4["category8"]["reason"])
    ok("U-RG11b ...and is described as aligned",
       r4["relationship"]["state"] == "ALIGNED_CONSTRUCTIVE")

    # 6. The legacy menu is never narrowed by the composite.
    r5 = resolve(macro_regime="Expansion", market_regime="LATE_CYCLE")
    ok("U-RG12 composite NEVER narrows an existing legacy permission",
       r5["category8"]["eligible"] is True
       and set(r5["category8"]["permitted_tilts"]) >= {"min_vol", "quality"})

    # 7. Precedence: capital-moving machinery is mechanical-only.
    ok("U-RG13 B1 drawdown ladder is market_regime ONLY",
       AUTHORITY["b1_drawdown_ladder"] == "market_regime")
    ok("U-RG14 Step 6.3 fund tilt is macro_regime ONLY",
       AUTHORITY["step6_3_fund_tilt"] == "macro_regime")
    ok("U-RG15 Category 8 is the ONLY consumer of both",
       [k for k, v in AUTHORITY.items() if v == "both"] == ["category8_tactical"])
    ok("U-RG16 min-hold is regime-INDEPENDENT (C-1 must never become regime-gated)",
       AUTHORITY["min_hold"] == "neither")

    # 8. Missing inputs never produce a permissive answer.
    r6 = resolve(macro_regime=None, market_regime="RISK_ON")
    ok("U-RG17 a missing macro read is a reason NOT to act",
       r6["category8"]["eligible"] is False)
    # market_regime=None means "not supplied -> read from disk", so the honest test points at
    # a file that is genuinely absent.
    r7 = resolve(macro_regime="Slowdown", market_regime=None,
                 state_path="/nonexistent/drawdown_state.json",
                 corroborators={"revisions_negative": True})
    ok("U-RG18 a missing market read is a reason NOT to act",
       r7["category8"]["eligible"] is False)
    ok("U-RG19 an unparseable macro string is None, not silently Expansion",
       normalise_macro("mildly slowing") is None)
    ok("U-RG20 the Step 4 output form is accepted", normalise_macro("slowdown") == "Slowdown")

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)}) {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--macro")
    ap.add_argument("--market")
    ap.add_argument("--corroborator", action="append", default=[])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.resolve:
        r = resolve(macro_regime=a.macro, market_regime=a.market,
                    corroborators={c: True for c in a.corroborator})
        print(json.dumps(r, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
