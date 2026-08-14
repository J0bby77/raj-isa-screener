"""ISA-0310 — the plausibility band for the implied forward market return M*.

D-23 publishes M* (the forward MARKET return this allocation requires to reach the anchor) but
withheld any verdict, because a verdict needs a DECLARED band of plausible long-run equity
returns and no such band existed. This module is that band's ONE HOME (R: one home per rule).

⚑ WHY A PERCENTILE AND NOT TWO PERCENTAGES (Raj's decision, 13-Aug-2026 — "option C").
D-12 measures that roughly 1.2pp of M covers the whole distance between Flag and On-track, so
the LIVE RANGE M* can occupy is ~1.2pp wide. Any band drawn on plausibility grounds is 3-5pp
wide — WIDER than the range M* can move through. A two-bound percentage band would therefore
have pinned the verdict to a constant and the instrument would have been decorative. Expressing
M* as its PERCENTILE in the declared distribution and setting the thresholds on the percentile
restores resolution: at the current level a 1.2pp move in M* shifts the percentile by ~7-10
points and DOES flip the verdict. The judgement is identical; only its resolution changed.

⚑ WHAT THIS MODULE REFUSES TO DO. It does not forecast. The distribution is DECLARED, dated,
attributed and revisit-bound, and every call republishes it in full so the judgement can never
go invisible (R4.8). Where M* is Missing, the percentile is Missing — never a number.
"""
from __future__ import annotations
import math

SCHEMA_VERSION = "1.0.0"
ITEM = "ISA-0310"

# ═══ THE DECLARED DISTRIBUTION ══════════════════════════════════════════════════════════════
# Basis MUST equal M*'s basis exactly or the verdict is meaningless. M* solves
# solve_required_annual_pct(1_000_000, 144_342.19, 2026-06-30, 2037-12-31, schedule): a NOMINAL
# GBP GEOMETRIC annual total return, net of fund OCF (NAV-based), gross of platform fee.
MSTAR_BASIS = "nominal_gbp_geometric_annual_total_return_net_of_ocf"

DECLARED = {
    "real_cagr_pct": {
        "value": 5.2, "as_of": "2024-12-31",
        "source": "Dimson-Marsh-Staunton / UBS Global Investment Returns Yearbook 2025, WORLD "
                  "equity index, annualized REAL return 1900-2024 = 5.2%. Read back 13-Aug-2026 "
                  "via Cambridge Judge Business School's report on the Yearbook "
                  "(jbs.cam.ac.uk/2025/report-stocks-have-far-outperformed-over-the-past-125-years/)",
        "verification": "VERIFIED",
        "verified_on": "2026-08-13",
        "correction": ("was 5.0 (declared from recall). ⚑ THE 6.6% FIGURE CIRCULATING FOR THE "
                       "2026 EDITION IS THE US MARKET, NOT THE WORLD INDEX - substituting it "
                       "would have been the exact defect this framework exists to stop: a "
                       "plausible number measuring something else.")},
    "inflation_pct": {
        "value": 2.0, "as_of": "2026-08-13",
        "source": "Bank of England CPI target — forward-looking, deliberately NOT realised UK "
                  "inflation, because the horizon is forward",
        "verification": "DECLARED_UNVERIFIED"},
    "annual_vol_pct": {
        "value": 16.0, "as_of": "2024-12-31",
        "source": "DMS world equity index, annual standard deviation of nominal returns, "
                  "GBP-investor basis",
        "verification": "DECLARED_UNVERIFIED"},
    "variance_ratio": {
        "value": 0.85, "as_of": "2026-08-13",
        "source": "long-horizon variance ratio for global equity (mean reversion). <1 shrinks "
                  "the CAGR dispersion relative to iid scaling; 1.0 would be the iid null",
        "verification": "DECLARED_UNVERIFIED"},
}

# Verdict thresholds ON THE PERCENTILE. Raj's declaration, 13-Aug-2026.
THRESHOLD_ON_TRACK_P = 0.50   # <= P50: the plan needs a BELOW-AVERAGE market
THRESHOLD_FLAG_P     = 0.80   # >  P80: the plan needs an outcome that fails ~1 year in 5

FALSIFIED_BY = ("a realised long-run global equity return, or a published long-horizon forecast "
                "distribution, that places M* materially outside this percentile — or any "
                "revision to the DMS world-equity real return or volatility that moves the "
                "percentile by more than 5 points")
REVISIT_BY = "2027-06-30"

DECLARATION_NOTE = (
    "ABOVE P80 THE FINDING IS ABOUT THE TARGET, NOT THE ALLOCATION. A plan that requires a "
    "market outcome in the top fifth of the long-run distribution is not fixed by choosing "
    "different funds; it is fixed by contributions, horizon, or the GBP 1.0m figure itself. "
    "This is the whole reason the band had to be declared before the verdict could be read.")


class PlausibilityError(Exception):
    """Raised where the declaration itself is unusable. Never swallowed."""


# ═══ TWO INDEPENDENT NORMAL PATHS (R5.2) ════════════════════════════════════════════════════
def _phi(z: float) -> float:
    """Forward CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Inverse CDF — Acklam rational approximation. A GENUINELY DIFFERENT code path from _phi,
    so composing them tests scale and sign errors that a single path would hide."""
    if not (0.0 < p < 1.0):
        raise PlausibilityError(f"probability {p!r} outside (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _declared(key: str) -> float:
    d = DECLARED.get(key)
    if not isinstance(d, dict) or "value" not in d or not d.get("as_of") or not d.get("source"):
        raise PlausibilityError(
            f"declared parameter {key!r} is missing value/as_of/source — a figure without a date "
            f"and a source may not enter a verdict")
    return float(d["value"])


def horizon_distribution(horizon_years: float) -> dict:
    """The declared distribution of the T-year annualised NOMINAL GBP GEOMETRIC return.

    Modelled in LOG space, which is where a compounded return is additive: the T-year CAGR is
    the mean of T annual log returns, so its dispersion scales with sqrt(VR/T).
    """
    if not horizon_years or horizon_years <= 0:
        raise PlausibilityError(f"horizon_years {horizon_years!r} must be positive")
    real, infl = _declared("real_cagr_pct"), _declared("inflation_pct")
    vol, vr = _declared("annual_vol_pct"), _declared("variance_ratio")
    mean_nominal_pct = ((1 + real / 100.0) * (1 + infl / 100.0) - 1.0) * 100.0
    mu_log = math.log1p(mean_nominal_pct / 100.0)
    sigma_cagr_log = (vol / 100.0) * math.sqrt(vr / horizon_years)
    return {
        "basis": MSTAR_BASIS,
        "horizon_years": round(horizon_years, 6),
        "mean_nominal_cagr_pct": round(mean_nominal_pct, 4),
        "mu_log": round(mu_log, 10),
        "sigma_cagr_log": round(sigma_cagr_log, 10),
        "sd_of_cagr_pp_approx": round(sigma_cagr_log * 100.0, 4),
        "construction": ("mean = (1+real)*(1+inflation)-1, compounded not added; dispersion = "
                         "annual_vol * sqrt(variance_ratio / T), in log space"),
        "declared_parameters": DECLARED,
        "unverified_parameters": sorted(k for k, v in DECLARED.items()
                                        if v.get("verification") != "VERIFIED"),
    }


def quantile_pct(p: float, horizon_years: float) -> float:
    """The nominal GBP CAGR at probability p — the inverse of `percentile_of`."""
    d = horizon_distribution(horizon_years)
    return (math.exp(d["mu_log"] + _phi_inv(p) * d["sigma_cagr_log"]) - 1.0) * 100.0


def _monte_carlo_percentile(m_star_pct, horizon_years, n=200_000, seed=20260813):
    """A THIRD, fully independent derivation: draw the distribution and count. Deterministic
    seed so it is a reproducible invariant, not a flaky test."""
    import random
    d = horizon_distribution(horizon_years)
    rng = random.Random(seed)
    tgt = math.log1p(m_star_pct / 100.0)
    hit = 0
    for _ in range(n):
        if d["mu_log"] + rng.gauss(0.0, 1.0) * d["sigma_cagr_log"] <= tgt:
            hit += 1
    return hit / n


# ── ISA-0335. THE VERDICT MUST DECLARE HOW MUCH OF ITSELF RESTS ON UNVERIFIED INPUTS. ───────
# Measured 13-Aug-2026: across the plausible range of the three parameters still tagged
# DECLARED_UNVERIFIED, the verdict is NOT stable. Reporting the point estimate alone would have
# presented a coin-flip as a finding. This grid runs on every call and travels with the verdict.
ROBUSTNESS_GRID = {"inflation_pct": (2.0, 2.5, 3.0),
                   "annual_vol_pct": (14.0, 16.0, 18.0),
                   "variance_ratio": (0.80, 0.90, 1.00)}


def robustness(m_star_pct, horizon_years):
    """How much of the verdict is the portfolio, and how much is an unverified assumption?"""
    import copy, itertools
    if m_star_pct is None:
        return {"status": "REFUSED_NO_M_STAR"}
    base = copy.deepcopy(DECLARED)
    counts, pcts = {}, []
    try:
        for combo in itertools.product(*ROBUSTNESS_GRID.values()):
            for k, v in zip(ROBUSTNESS_GRID, combo):
                DECLARED[k] = {**base[k], "value": v}
            r = assess(m_star_pct, horizon_years, run_monte_carlo=False, with_robustness=False)
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
            pcts.append(r["percentile"])
    finally:
        DECLARED.clear(); DECLARED.update(base)
    n = sum(counts.values())
    top = max(counts, key=counts.get)
    return {"n_grid_points": n, "grid": ROBUSTNESS_GRID, "verdict_counts": counts,
            "percentile_range": [round(min(pcts), 6), round(max(pcts), 6)],
            "modal_verdict": top, "modal_share": round(counts[top] / n, 4),
            "unanimous": bool(len(counts) == 1),
            "varied_parameters_are_unverified": sorted(
                k for k in ROBUSTNESS_GRID if DECLARED[k].get("verification") != "VERIFIED"),
            "note": ("the grid varies ONLY the parameters that are still DECLARED_UNVERIFIED. If "
                     "this is not unanimous, the verdict is partly an artefact of an unverified "
                     "assumption and must be read as such (ISA-0335).")}


def assess(m_star_pct, horizon_years, m_star_basis=MSTAR_BASIS, m_star_status="computed",
           run_monte_carlo=True, with_robustness=True):
    """Turn M* into a verdict. THE ONLY PLACE A VERDICT ON M* IS PRODUCED."""
    # ── I-MPB-4. BASIS CONTRACT AT THE ARTEFACT BOUNDARY. A percentile read off a distribution
    #    built on a different basis is the exact failure class this framework exists to stop:
    #    a stored value that is plausible and wrong.
    if m_star_basis != MSTAR_BASIS:
        raise PlausibilityError(
            f"basis mismatch: M* is {m_star_basis!r} but the declared distribution is "
            f"{MSTAR_BASIS!r}. A percentile across bases is meaningless, not approximate.")
    base = {"schema_version": SCHEMA_VERSION, "item": ITEM, "basis": MSTAR_BASIS,
            "thresholds": {"on_track_at_or_below_p": THRESHOLD_ON_TRACK_P,
                           "flag_above_p": THRESHOLD_FLAG_P},
            "declared_by": "Raj", "declared_on": "2026-08-13",
            "falsified_by": FALSIFIED_BY, "revisit_by": REVISIT_BY,
            "declaration_note": DECLARATION_NOTE}
    # ── I-MPB-5. MISSING IS NOT A NUMBER.
    if m_star_pct is None or m_star_status != "computed":
        return {**base, "status": "REFUSED_NO_M_STAR", "m_star_pct": None, "percentile": None,
                "verdict": None,
                "note": (f"M* is Missing (upstream status {m_star_status!r}), so its percentile "
                         f"is Missing. A verdict here would be a claim about a portfolio that "
                         f"could not be measured.")}
    # ── I-MPB-6. THRESHOLDS ORDERED AND IN RANGE.
    if not (0.0 < THRESHOLD_ON_TRACK_P < THRESHOLD_FLAG_P < 1.0):
        raise PlausibilityError("verdict thresholds are unordered or outside (0,1)")

    d = horizon_distribution(horizon_years)
    z = (math.log1p(float(m_star_pct) / 100.0) - d["mu_log"]) / d["sigma_cagr_log"]
    p = _phi(z)

    # ── I-MPB-1. ROUND TRIP. Independent inverse path must reproduce M* from its own percentile.
    back_pct = quantile_pct(p, horizon_years)
    gap = abs(back_pct - float(m_star_pct))
    if gap > 1e-6:
        raise PlausibilityError(
            f"I-MPB-1 failed: percentile {p:.10f} inverts to {back_pct:.10f}%, not "
            f"{float(m_star_pct):.10f}% (gap {gap:.2e}pp)")

    mc = _monte_carlo_percentile(m_star_pct, horizon_years) if run_monte_carlo else None
    mc_gap = abs(mc - p) if mc is not None else None
    # ── I-MPB-2. A THIRD PATH AGREES.
    if mc_gap is not None and mc_gap > 0.01:
        raise PlausibilityError(
            f"I-MPB-2 failed: Monte Carlo percentile {mc:.4f} disagrees with analytic {p:.4f}")

    if p <= THRESHOLD_ON_TRACK_P:
        verdict, meaning = "ON_TRACK", (
            "M* is at or below the median of the declared distribution — the plan reaches the "
            "target on a BELOW-AVERAGE market.")
    elif p <= THRESHOLD_FLAG_P:
        verdict, meaning = "WATCH", (
            "M* is above the median but inside the declared plausible range. The plan requires "
            "an above-average market and is sensitive to the next few years.")
    else:
        verdict, meaning = "FLAG_TARGET_REVIEW", (
            "M* sits above P{:.0f}: the plan requires a market outcome that the long-run "
            "distribution fails to deliver roughly {:.0f} years in {:.0f}. ⚑ THE REMEDY IS THE "
            "TARGET, THE CONTRIBUTIONS OR THE HORIZON — NOT THE FUND SELECTION."
        ).format(THRESHOLD_FLAG_P * 100, (1 - THRESHOLD_FLAG_P) * 100 / 10, 10)

    return {**base, "status": "computed", "m_star_pct": round(float(m_star_pct), 4),
            "percentile": round(p, 6), "percentile_label": f"P{p * 100:.1f}",
            "z_score": round(z, 6), "verdict": verdict, "verdict_meaning": meaning,
            "distribution": d,
            "identity_check": {"percentile": round(p, 10),
                               "inverted_back_to_pct": round(back_pct, 10),
                               "m_star_pct": round(float(m_star_pct), 10),
                               "abs_gap_pp": round(gap, 14), "tolerance_pp": 1e-6,
                               "monte_carlo_percentile": (round(mc, 6) if mc is not None else None),
                               "monte_carlo_abs_gap": (round(mc_gap, 6) if mc_gap is not None else None),
                               "monte_carlo_tolerance": 0.01,
                               "holds": bool(gap <= 1e-6 and (mc_gap is None or mc_gap <= 0.01)),
                               "note": ("three paths: erf forward CDF, Acklam inverse CDF, and a "
                                        "seeded Monte Carlo draw. All three must agree.")},
            "sensitivity": {
                "d12_span_pp": 1.2,
                "percentile_at_m_star_minus_1_2pp": round(_phi(
                    (math.log1p((float(m_star_pct) - 1.2) / 100.0) - d["mu_log"])
                    / d["sigma_cagr_log"]), 6),
                "percentile_at_m_star_plus_1_2pp": round(_phi(
                    (math.log1p((float(m_star_pct) + 1.2) / 100.0) - d["mu_log"])
                    / d["sigma_cagr_log"]), 6),
                "note": ("D-12 measures that ~1.2pp of M covers Flag to On-track. This is the "
                         "reason the verdict is set on the PERCENTILE and not on two percentage "
                         "bounds: a 3-5pp wide percentage band is wider than the range M* can "
                         "occupy and would have frozen the verdict.")},
            "robustness": (robustness(m_star_pct, horizon_years) if with_robustness else None),
            "source_verification_required": d["unverified_parameters"],
            "provisional": bool(d["unverified_parameters"]),
            "provisional_reason": (
                "the distribution parameters are DECLARED from named published sources but have "
                "not been re-read against those sources inside this framework. The verdict is "
                "published because refusing again would leave ISA-0310 blocked; it is tagged "
                "provisional so the unverified input can never become invisible (R4.8)."
            ) if d["unverified_parameters"] else None}
