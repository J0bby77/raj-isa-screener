#!/usr/bin/env python3
"""
compliance.py -- THE single authoritative source for personal-account-dealing (PAD) regime state.

WHY THIS FILE EXISTS (Raj, 29-Jul-2026)
--------------------------------------
Raj was made redundant from Citi. Citi Personal Trading rules (preclearance, 2-day approval
validity, 30-day regulatory minimum hold, narrow-based instrument test) therefore no longer
bind. They are PAUSED, not deleted: a future role at another bank is expected to impose an
equivalent regime, at which point the framework must restore EXACTLY the prior behaviour by
flipping ONE constant -- scoring_config.COMPLIANCE_REGIME -- back to "CITI_PT".

DESIGN RULES (do not violate; they are the point of the module)
--------------------------------------------------------------
R1. No caller anywhere may test `COMPLIANCE_REGIME` directly. Call the predicates here.
    Direct string comparisons scattered through 57 scripts is precisely the prose/code
    duality class (audit H-7) this module exists to prevent.
R2. THE 182-DAY FRAMEWORK MIN-HOLD IS NOT A COMPLIANCE RULE AND IS NOT PAUSED.
    `scoring_config.MIN_HOLD_DAYS` (182) is the C-1 anti-churn fix shipped 22-Jul-2026 after
    three 3-5yr theses were exited in 21-56 days for a realised -GBP1,097. It is a FRAMEWORK
    rule about thesis horizon. The REGULATORY hold that pauses is the 30-day Citi PT rule,
    exposed here as `min_hold_days()`. Two different numbers, two different reasons.
    Conflating them re-opens the single most expensive defect the audit found.
R3. Under a paused regime the framework must not silently become more permissive in SCORING.
    Removing friction from Dimension 10 (Execution Practicality) without rescaling would
    hand every candidate ~4 free points and quietly lower the >=60 / >=75 conviction bars.
    Use `score_d10()`; never award the compliance sub-score by default.
R4. Instrument choices made while paused must record their RESTORATION RISK -- a narrow-based
    ETF bought today is freely tradeable, but would need preclearance (and could be trapped by
    a 30-day hold) the day Raj joins another bank. See `restoration_risk()`.

Pure stdlib. No I/O. Safe to import from any pipeline stage.
"""
from __future__ import annotations

CITI_PT = "CITI_PT"   # bank personal-account-dealing regime in force
NONE = "NONE"         # no employer PAD restrictions

_VALID_REGIMES = (CITI_PT, NONE)

# Behaviour table. Adding a future employer regime = add a row here, nothing else.
_REGIME_SPEC = {
    CITI_PT: {
        "label": "Citi Personal Trading",
        "preclearance_required": True,
        "approval_validity_days": 2,
        "min_hold_days": 30,
        "narrow_based_test_applies": True,
        # Dimension 10 weighting (capital/execution, compliance) -- must sum to 10.
        "d10_weights": (6.0, 4.0),
    },
    NONE: {
        "label": "No employer PAD regime",
        "preclearance_required": False,
        "approval_validity_days": None,
        "min_hold_days": 0,
        "narrow_based_test_applies": False,
        "d10_weights": (10.0, 0.0),
    },
}


def _cfg(name, default=None):
    try:
        import scoring_config as cfg
        return getattr(cfg, name, default)
    except Exception:
        return default


def regime() -> str:
    """Current PAD regime. Unknown/missing values fail SAFE (-> CITI_PT, the restrictive one)."""
    r = str(_cfg("COMPLIANCE_REGIME", CITI_PT) or CITI_PT).strip().upper()
    return r if r in _VALID_REGIMES else CITI_PT


def _spec() -> dict:
    return _REGIME_SPEC[regime()]


def active() -> bool:
    """True when an employer PAD regime binds."""
    return regime() != NONE


def label() -> str:
    return _spec()["label"]


def effective_from() -> str:
    return str(_cfg("COMPLIANCE_REGIME_EFFECTIVE_FROM", "") or "")


# ---------------------------------------------------------------- predicates
def preclearance_required(instrument_type: str | None = None) -> bool:
    """instrument_type: 'stock' | 'narrow_etf' | 'broad_etf' | 'ucits_etf' | 'fund' | None.
    Under CITI_PT: stocks and narrow-based instruments need preclearance; UCITS ETFs, broad-based
    funds/closed-end funds do not. Under NONE: nothing does."""
    if not _spec()["preclearance_required"]:
        return False
    if instrument_type is None:
        return True
    t = str(instrument_type).strip().lower()
    exempt = {"ucits_etf", "broad_etf", "fund", "oeic", "investment_trust_broad", "cash", "mmf"}
    return t not in exempt


def approval_validity_days():
    """Trading days a preclearance approval stays valid, or None when no regime binds."""
    return _spec()["approval_validity_days"]


def min_hold_days() -> int:
    """REGULATORY minimum hold in calendar days (Citi PT = 30; none = 0).

    NOT the framework anti-churn rule -- see R2 in the module docstring and
    `framework_min_hold_days()`."""
    return int(_spec()["min_hold_days"])


def framework_min_hold_days() -> int:
    """The C-1 anti-churn horizon rule (182d). Regime-independent BY DESIGN. Exposed here only
    so callers reading this module cannot mistake the two."""
    return int(_cfg("MIN_HOLD_DAYS", 182))


def narrow_based_test_applies() -> bool:
    """Whether the >25%-single-issuer narrow/broad classification gates a trade."""
    return bool(_spec()["narrow_based_test_applies"])


def earliest_sale_date(trade_date):
    """datetime.date of the earliest permitted sale under the REGULATORY hold, or the trade
    date itself when no regime binds. Day 1 = trade date; sellable on Day 31 under Citi PT."""
    import datetime
    if isinstance(trade_date, str):
        trade_date = datetime.date.fromisoformat(trade_date[:10])
    d = min_hold_days()
    return trade_date if d <= 0 else trade_date + datetime.timedelta(days=d)


# ---------------------------------------------------------------- scoring integrity (R3)
def d10_weights():
    """(capital_execution_weight, compliance_weight); always sums to 10."""
    return tuple(_spec()["d10_weights"])


def score_d10(capital_execution_0_10, compliance_0_10=None):
    """Dimension 10 (Execution Practicality), 0-10, regime-correct.

    capital_execution_0_10 -- cash sufficiency after buffer, dealing cost, FX, liquidity/spread.
    compliance_0_10        -- preclearance routine-ness, 30-day conflict, clean window.
                              Ignored (and NOT defaulted to full marks) when no regime binds.

    Under NONE the capital/execution axis carries the whole dimension, so a cash-constrained
    name still scores low and the >=60 / >=75 conviction bars keep their meaning (R3)."""
    cw, mw = d10_weights()
    cap = max(0.0, min(10.0, float(capital_execution_0_10)))
    if mw == 0.0:
        return round(cap, 1)
    if compliance_0_10 is None:
        raise ValueError(
            "compliance sub-score is required while regime %s binds -- refusing to award it "
            "by default (compliance.py R3)" % regime())
    comp = max(0.0, min(10.0, float(compliance_0_10)))
    return round((cap * cw + comp * mw) / 10.0, 1)


# ---------------------------------------------------------------- blockers
def clear_regime_blockers(name: dict) -> dict:
    """Force compliance-derived blocker fields False when no regime binds, so a stale flag left
    in watchlist/portfolio JSON cannot suppress an upgrade or deploy. Mutates and returns `name`."""
    if active():
        return name
    for k in ("preclearance_block", "in_30day_hold", "preclearance_pending", "narrow_based_block"):
        if name.get(k):
            name[k] = False
            name.setdefault("_regime_cleared", []).append(k)
    return name


def restoration_risk(instrument_type: str | None) -> tuple[bool, str]:
    """(at_risk, note) -- R4. Flags positions opened while paused that WOULD attract preclearance
    (and a 30-day hold) if a bank PAD regime is restored. Advisory; never a gate."""
    if active():
        return False, ""
    if instrument_type is None:
        return False, ""
    would = _REGIME_SPEC[CITI_PT]
    t = str(instrument_type).strip().lower()
    exempt = {"ucits_etf", "broad_etf", "fund", "oeic", "investment_trust_broad", "cash", "mmf"}
    if t in exempt:
        return False, ""
    return True, (
        "RESTORATION RISK: '%s' is freely tradeable now, but would require preclearance and a "
        "%d-day hold if a bank PAD regime is restored." % (t, would["min_hold_days"]))


# ---------------------------------------------------------------- presentation
def status_line() -> str:
    """One line for emails / Run_Context / run_context JSON."""
    if active():
        return ("Compliance regime: %s ACTIVE - preclearance required (approval valid %s trading "
                "days); %d-day regulatory minimum hold." %
                (label(), approval_validity_days(), min_hold_days()))
    eff = effective_from()
    return ("Compliance regime: PAUSED%s - no employer PAD restrictions: no preclearance, no "
            "approval window, no regulatory minimum hold. Framework %d-day min-hold (C-1) is "
            "UNAFFECTED and still applies." %
            ((" since " + eff) if eff else "", framework_min_hold_days()))


def execution_reminder() -> str:
    """The execution-guidance reminder string (Step 10.6 / email footer)."""
    if active():
        return ("Citi preclearance required before executing any individual stock trade; approval "
                "valid %s trading days; %d-day minimum holding period applies."
                % (approval_validity_days(), min_hold_days()))
    return ("No preclearance required and no regulatory holding period (employer PAD regime paused "
            "29-Jul-2026). The framework's %d-day minimum-hold rule (C-1 anti-churn) still applies "
            "to Path A positions." % framework_min_hold_days())


def as_dict() -> dict:
    """Machine-readable regime block for run_context_[mmm].json / step9_pre / dashboards."""
    return {
        "regime": regime(),
        "label": label(),
        "active": active(),
        "effective_from": effective_from(),
        "preclearance_required": preclearance_required(),
        "approval_validity_days": approval_validity_days(),
        "regulatory_min_hold_days": min_hold_days(),
        "framework_min_hold_days": framework_min_hold_days(),
        "narrow_based_test_applies": narrow_based_test_applies(),
        "d10_weights": {"capital_execution": d10_weights()[0], "compliance": d10_weights()[1]},
        "note": str(_cfg("COMPLIANCE_REGIME_NOTE", "") or ""),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(as_dict(), indent=2))
    print(status_line())
