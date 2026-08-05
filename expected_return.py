#!/usr/bin/env python3
"""
expected_return.py — Fix Pack A2 (12-Jul-2026). THE single E[r] implementation.

expected_return_12_24m = er_growth + er_rerate + er_yield   (annualised, % p.a.)
  er_growth = forward EPS growth (2y annualised; fallback: fwd-growth proxy, then rev growth x 0.8)
  er_rerate = clamp((anchor_multiple / current_multiple) ** 0.5 - 1, -CAP, +CAP), then
              C1-shaped: zeroed inside a neutral band, and the DE-RATE side damped by regime
              (RISK_ON 0.25 / LATE_CYCLE 0.50 / RISK_OFF 1.0). The RE-RATE credit for a cheap
              name is never damped. Set ER_RERATE_MODE="legacy" to restore the raw monotonic term.
  er_yield  = dividend_yield + net_buyback_yield (from 3y share-count change)

Own-history-anchored by design — consensus targets are sentiment data (Correction #5), never inputs here.
One implementation, imported by screener_core (screen) AND normalise_adapter/rerank (pre-run, live price).
Gate consumption (P2, T1_QUALIFICATION_MODE): er >= scoring_config.ER_DEPLOY_FLOOR or documented catalyst.
Stdlib only. Self-test: python3 expected_return.py
"""
from __future__ import annotations

try:
    import scoring_config as _cfg
    _CAP = float(getattr(_cfg, "ER_RERATE_CAP", 0.10))
    _RERATE_MODE = str(getattr(_cfg, "ER_RERATE_MODE", "regime_aware"))
    _NEUTRAL_BAND = float(getattr(_cfg, "ER_RERATE_NEUTRAL_BAND", 0.05))
    _REGIME_DAMPING = dict(getattr(_cfg, "ER_RERATE_REGIME_DAMPING", {}) or {}) or {
        "RISK_ON": 0.25, "LATE_CYCLE": 0.50, "RISK_OFF": 1.00, "RECOVERY": 1.00}
except Exception:            # standalone/self-test safety — never block a screen on config import
    _CAP = 0.10
    _RERATE_MODE = "regime_aware"
    _NEUTRAL_BAND = 0.05
    _REGIME_DAMPING = {"RISK_ON": 0.25, "LATE_CYCLE": 0.50,
                       "RISK_OFF": 1.00, "RECOVERY": 1.00}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace("£", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def compute_expected_return(*, fwd_eps_growth_pct=None, rev_growth_pct=None,
                            current_multiple=None, median_5y_multiple=None,
                            dividend_yield_pct=None, sharecount_change_3y_pct_pa=None,
                            regime=None):
    """Pure function. Percent-unit inputs (12 == 12%). Returns dict:
    {expected_return_12_24m, er_growth, er_rerate, er_yield, er_confidence, er_basis}
    Missing term -> contributes 0, lowers er_confidence, and is named in er_basis (never silently proxied)."""
    basis, present = [], 0.0

    g = _num(fwd_eps_growth_pct)
    if g is not None:
        basis.append("growth=fwd_eps"); present += 0.5
    else:
        rg = _num(rev_growth_pct)
        if rg is not None:
            g = rg * 0.8; basis.append("growth=rev_x0.8_fallback"); present += 0.3
        else:
            g = 0.0; basis.append("growth=MISSING")
    g = max(min(g, 50.0), -25.0)   # sanity clamp, mirrors Part B g-cap doctrine

    cur, med = _num(current_multiple), _num(median_5y_multiple)
    if cur and med and cur > 0 and med > 0:
        raw = (med / cur) ** 0.5 - 1.0
        rer = 100.0 * max(min(raw, _CAP), -_CAP)
        _tag = "rerate=own_history_median"

        # ── C1 (02-Aug-2026): SHAPE and REGIME ────────────────────────────────────────
        # Measured over 13.6 years, 1,680 names, multi-market: forward 52-week excess return
        # by own-history extension decile is U-SHAPED, not monotonic —
        #   D1 (cheapest) +2.4% · D2-D7 -2 to -3% · D10 (most extended) +6.8%
        # A monotonic penalty therefore punishes the BEST decile hardest and rewards the
        # WORST. It is also regime-conditional: D10-D1 spread is +8.5pp in Bull and -10.2pp
        # in Bear, and it flips by era (+19.5 / -15.1 / +14.7pp).
        #
        # The sign is NOT flipped. Rewarding extension would have lost 15pp in 2020-22, the
        # study universe has ZERO delistings (total survivorship), and the effective sample
        # is ~9 independent observations. Two changes only, both conservative:
        #   1. NEUTRAL BAND — inside it the middle deciles carry no information, so they are
        #      scored 0 rather than assigned a confident penalty.
        #   2. ASYMMETRIC REGIME DAMPING — a CHEAP name keeps its full re-rate credit in every
        #      regime (D1 is positive everywhere, strongest in Bear). An EXPENSIVE name's
        #      penalty is damped where the evidence says reversion does not happen.
        # Damping is 0.25 rather than 0 in RISK_ON deliberately: the evidence is directional,
        # not strong enough to switch the term off.
        if _RERATE_MODE == "regime_aware":
            band = _NEUTRAL_BAND
            if abs(raw) <= band:
                rer = 0.0
                _tag = f"rerate=neutral_band(|{raw:+.3f}|<={band})"
            elif raw < 0:                      # expensive vs its own history
                f = _REGIME_DAMPING.get(str(regime or "").upper(), 1.0)
                rer *= f
                _tag = f"rerate=de_rate_damped(regime={regime or 'unknown'},x{f})"
            else:                              # cheap vs its own history — kept in full
                _tag = "rerate=re_rate_credit_full"
        basis.append(_tag); present += 0.3
    else:
        rer = 0.0; basis.append("rerate=MISSING")

    dy = _num(dividend_yield_pct) or 0.0
    sc = _num(sharecount_change_3y_pct_pa)
    bb = -sc if sc is not None else 0.0            # shrinking count (negative change) = positive yield
    if _num(dividend_yield_pct) is not None or sc is not None:
        basis.append("yield=div+buyback"); present += 0.2
    else:
        basis.append("yield=MISSING")
    y = max(min(dy + bb, 15.0), -10.0)

    er = round(g + rer + y, 1)
    return {"expected_return_12_24m": er, "er_growth": round(g, 1), "er_rerate": round(rer, 1),
            "er_yield": round(y, 1), "er_confidence": round(min(present, 1.0), 2),
            "er_basis": "|".join(basis)}


# Row adapter — tolerant of the screen/pre-run field-name variants; extend lists, never rename here.
# VERIFIED 12-Jul-2026 against screener_core.FIELD_MAP (the authoritative full_data schema):
#   fwd_eps_growth       = FRACTION (0.12 == 12%)  -> scale 100   (screener_core Metric 9)
#   rev_est_fwd_pct      = percent                 -> scale 1
#   trailing_pe          = multiple                -> scale 1
#   val_hist_pe_3yr_avg  = multiple (own-history anchor; the old *_5y candidates do NOT exist)
#   share_count_change   = FRACTION per annum      -> scale 100   (share_chg_ann, Part A)
#   dividend yield: NOT in full_data — er_yield at screen is buyback-only (honest, er_basis shows it)
# Each candidate is (field_name, scale_to_percent_units).
_KEYS = {
    "fwd_eps_growth_pct": [("fwd_eps_growth", 100), ("forward_eps_growth_pct", 1), ("eps_growth_fwd_pct", 1)],
    "rev_growth_pct": [("rev_est_fwd_pct", 1), ("revenue_growth_fwd_pct", 1), ("recent_revenue_growth_pct", 1)],
    "current_multiple": [("trailing_pe", 1), ("val_hist_current_pe", 1), ("current_pe", 1), ("fwd_pe", 1)],
    # C2: the MEDIAN anchor first, the legacy 3-year MEAN only as a fallback.
    "median_5y_multiple": [("val_hist_pe_anchor", 1), ("val_hist_median_pe_5y", 1),
                           ("pe_5y_median", 1), ("val_hist_pe_3yr_avg", 1)],
    "dividend_yield_pct": [("dividend_yield_pct", 1), ("dividend_yield", 1)],
    "sharecount_change_3y_pct_pa": [("share_count_change", 100), ("share_count_change_3y_pct_pa", 1),
                                    ("sharecount_change_pct_pa", 1)],
}


_REGIME_CACHE = {}


def current_market_regime(here=None):
    """The MECHANICAL market regime (drawdown_monitor B7: RISK_ON / LATE_CYCLE / RISK_OFF /
    RECOVERY), read once per process from drawdown_state.json.

    This is the PRICE-state regime, not Step 4's macro judgement. Precedence is deliberate and
    matches the two-regime resolution already in the framework: mechanical price state governs
    anything that moves capital automatically; macro judgement only shifts a threshold. E[r]
    feeds a deploy floor, so it takes the mechanical one.

    Returns None when unavailable — and None means UNDAMPED (the conservative full penalty),
    never a guessed regime.
    """
    if "v" in _REGIME_CACHE:
        return _REGIME_CACHE["v"]
    import json as _json
    import os as _os
    base = here or _os.path.dirname(_os.path.abspath(__file__))
    try:
        with open(_os.path.join(base, "drawdown_state.json"), encoding="utf-8") as f:
            _REGIME_CACHE["v"] = (_json.load(f) or {}).get("regime_state")
    except Exception:
        _REGIME_CACHE["v"] = None
    return _REGIME_CACHE["v"]


def expected_return_for_row(row, get=None, regime=None):
    g = get or (lambda r, k: r.get(k) if hasattr(r, "get") else None)
    kw = {}
    for arg, cands in _KEYS.items():
        v = None
        for k, scale in cands:
            raw = g(row, k)
            if raw not in (None, ""):
                n = _num(raw)
                if n is not None:
                    v = n * scale
                    break
        kw[arg] = v
    kw["regime"] = regime if regime is not None else current_market_regime()
    return compute_expected_return(**kw)


if __name__ == "__main__":
    # Fixture 1: growth compounder near median multiple, buyback
    a = compute_expected_return(fwd_eps_growth_pct=14, current_multiple=24, median_5y_multiple=25,
                                dividend_yield_pct=0.6, sharecount_change_3y_pct_pa=-1.5)
    # Fixture 2: Maturing momentum name at 90th-pct multiple (negative rerate, capped)
    b = compute_expected_return(fwd_eps_growth_pct=9, current_multiple=40, median_5y_multiple=22)
    # Fixture 3: sparse data (fallback growth only)
    c = compute_expected_return(rev_growth_pct=20)
    for name, r in (("compounder", a), ("late_cycle", b), ("sparse", c)):
        print(name, r)
    assert a["expected_return_12_24m"] > 14 and a["er_confidence"] == 1.0
    assert b["er_rerate"] == -10.0 and b["expected_return_12_24m"] < 0.5 + b["er_growth"]
    assert c["er_basis"].startswith("growth=rev_x0.8_fallback") and c["er_confidence"] < 0.5
    # Fixture 4 (12-Jul): row adapter on REAL screen field names/units — fwd_eps_growth and
    # share_count_change are fractions in full_data and must be scaled x100 by the adapter.
    d = expected_return_for_row({"fwd_eps_growth": 0.14, "trailing_pe": 24, "val_hist_pe_3yr_avg": 25,
                                 "share_count_change": -0.015})
    assert d["er_growth"] == 14.0 and d["er_yield"] == 1.5, d
    assert d["er_basis"].startswith("growth=fwd_eps") and "rerate=" in d["er_basis"], d

    # ── C1 (02-Aug-2026) ──────────────────────────────────────────────────────────────
    # A name sitting AT its own anchor carries no information — the middle deciles were
    # -2 to -3% excess. It is scored 0 rather than given a confident small penalty.
    nb = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=24,
                                 median_5y_multiple=25)
    assert nb["er_rerate"] == 0.0 and "neutral_band" in nb["er_basis"], nb

    # The DE-RATE penalty is damped where the evidence says reversion does not happen...
    on  = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=40,
                                  median_5y_multiple=22, regime="RISK_ON")
    off = compute_expected_return(fwd_eps_growth_pct=10, current_multiple=40,
                                  median_5y_multiple=22, regime="RISK_OFF")
    assert off["er_rerate"] == -10.0, off
    assert on["er_rerate"] == -2.5, on
    assert on["er_rerate"] > off["er_rerate"], (on, off)

    # ...but a CHEAP name keeps its full re-rate credit in EVERY regime (D1 is positive
    # everywhere, strongest in Bear). Damping the upside would be an unevidenced asymmetry.
    ch_on  = compute_expected_return(fwd_eps_growth_pct=5, current_multiple=10,
                                     median_5y_multiple=20, regime="RISK_ON")
    ch_off = compute_expected_return(fwd_eps_growth_pct=5, current_multiple=10,
                                     median_5y_multiple=20, regime="RISK_OFF")
    assert ch_on["er_rerate"] == ch_off["er_rerate"] == 10.0, (ch_on, ch_off)
    assert "re_rate_credit_full" in ch_on["er_basis"], ch_on

    # The sign is never flipped: an expensive name is never REWARDED for being expensive.
    assert on["er_rerate"] <= 0.0, on

    # C2: the median anchor is preferred over the legacy 3-year mean when present.
    e = expected_return_for_row({"fwd_eps_growth": 0.10, "trailing_pe": 40,
                                 "val_hist_pe_anchor": 22, "val_hist_pe_3yr_avg": 39})
    assert e["er_rerate"] < 0, e          # the median anchor, not the contaminated mean
    print("SELF-TEST OK (incl. C1 shape/regime + C2 anchor preference)")


def apply_capital_signal_conflict(row):
    """Review item 8 (18-Jul-26): E[r] is growth-anchored, implied_upside_fv is multiple-anchored;
    they can disagree violently with no flag (MU +58.5%pa vs FV -42.9%). Compare E[r] %pa against
    the ANNUALISED FV-implied return over the 12-24m window (18m midpoint: ((1+u)^(12/18)-1)*100).
    Gap > cfg.CAPITAL_SIGNAL_CONFLICT_PP -> capital_signal_conflict=True + er_confidence capped at
    cfg.CONFLICT_ER_CONF_CAP (below the A5 v3 0.75 full-size bar: conflicted signals size as
    starter, never full). Mutates + returns row; no-op when either input missing."""
    try:
        import scoring_config as _cfg
    except Exception:
        _cfg = None
    thr = float(getattr(_cfg, "CAPITAL_SIGNAL_CONFLICT_PP", 25.0))
    cap = float(getattr(_cfg, "CONFLICT_ER_CONF_CAP", 0.5))
    er = _num(row.get("expected_return_12_24m"))
    u = _num(row.get("implied_upside_fv"))
    row.setdefault("capital_signal_conflict", False)
    if er is None or u is None or u <= -1.0:
        return row
    fv_ann = ((1.0 + u) ** (12.0 / 18.0) - 1.0) * 100.0
    row["fv_annualised_pct"] = round(fv_ann, 1)
    if abs(er - fv_ann) > thr:
        row["capital_signal_conflict"] = True
        ec = _num(row.get("er_confidence"))
        if ec is None or ec > cap:
            row["er_confidence"] = cap
            row["er_basis"] = (str(row.get("er_basis") or "") + "|conflict_capped").lstrip("|")
    return row
