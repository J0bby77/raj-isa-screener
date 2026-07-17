#!/usr/bin/env python3
"""
expected_return.py — Fix Pack A2 (12-Jul-2026). THE single E[r] implementation.

expected_return_12_24m = er_growth + er_rerate + er_yield   (annualised, % p.a.)
  er_growth = forward EPS growth (2y annualised; fallback: fwd-growth proxy, then rev growth x 0.8)
  er_rerate = clamp((median_5y_multiple / current_multiple) ** 0.5 - 1, -CAP, +CAP)  # 2y drift to own median
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
except Exception:            # standalone/self-test safety — never block a screen on config import
    _CAP = 0.10


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
                            dividend_yield_pct=None, sharecount_change_3y_pct_pa=None):
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
        rer = 100.0 * max(min((med / cur) ** 0.5 - 1.0, _CAP), -_CAP)
        basis.append("rerate=own_5y_median"); present += 0.3
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
    "median_5y_multiple": [("val_hist_pe_3yr_avg", 1), ("val_hist_median_pe_5y", 1), ("pe_5y_median", 1)],
    "dividend_yield_pct": [("dividend_yield_pct", 1), ("dividend_yield", 1)],
    "sharecount_change_3y_pct_pa": [("share_count_change", 100), ("share_count_change_3y_pct_pa", 1),
                                    ("sharecount_change_pct_pa", 1)],
}


def expected_return_for_row(row, get=None):
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
    assert d["er_basis"].startswith("growth=fwd_eps") and "rerate=own_5y_median" in d["er_basis"], d
    print("SELF-TEST OK")
