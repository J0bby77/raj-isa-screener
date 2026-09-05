"""sleeve_risk — ONE home for the stock sleeve's covariance-side questions (ISA-0600).

Built 05-Sep-2026 on Raj's instruction, against a measured finding: the deployment ranking is
driven by `source_score`, whose IC is UNVALIDATED (0 matured 3-month observations against a
pre-registered gate of 200; the only populated column, IC@1m, is negative for the ranking
signals on 13% price coverage and one regime) — while the covariance IS measured, at 175 of 180
names and 156 weekly observations. Ranking on the unproven input while ignoring the proven one
had put the two MOST duplicative candidates in the book (WDC beta 0.88, NVDA 0.67) at the top of
the in-window list.

THE ESTIMATION-ERROR ARGUMENT, which is why this module exists at all:
    mean return   SE ~ sigma/sqrt(T) = 42%/sqrt(3y)  ~ 24pp per year   -> not estimable
    correlation   SE ~ (1-rho^2)/sqrt(T), T=156w     ~ 0.08            -> estimable
A framework that cannot tell a 5% expected return from a 25% one CAN tell a beta of 0.88 from a
beta of 0.03. Michaud's "error maximisation" is the general statement; this module is the
specific consequence.

⚑ THE CONSTRAINT BELONGS TO THE TRANSACTION, NOT TO THE NAME (Raj, 05-Sep-2026). An ADDITION
from cash increases exposure to the factor the sleeve already carries, so beta is the right
test. A SWITCH replaces one exposure with another, and the right test is the net effect on
sleeve risk — sigma(sleeve - donor + candidate) vs sigma(sleeve). NVDA as a seventh holding and
NVDA replacing AVGO are different transactions with different risk consequences, and only the
first is what beta measures. So the beta cap is the special case where the donor is CASH, and a
high-beta name must never be blocked as a REPLACEMENT candidate on beta alone.

⚑ NON-SYNCHRONOUS PRICING IS CORRECTED, NOT IGNORED. A small or illiquid non-UK/US listing
trades out of phase with the sleeve, which biases a naive contemporaneous beta DOWNWARD and
would make an uncorrected (1 - rho) preference systematically reward illiquidity. `beta` is
therefore the DIMSON sum over lags 0..DIMSON_LAGS, and `staleness` reports the share of exactly
zero weekly returns so an inert series is named rather than rewarded.

This module MEASURES. It sets no policy: thresholds live in isa_policy (R4.4).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

WEEKS_PER_YEAR = 52.0
DIMSON_LAGS = 1
MIN_OVERLAP_WEEKS = 52


class RiskRefused(RuntimeError):
    """The measurement cannot be made on a basis it can honour. Raised, never downgraded to a
    silent zero: a beta of 0.0 and an unmeasurable beta are different facts (R2.10)."""


# ────────────────────────────────────────────────────────────────────── series
_LEVEL_CACHE: dict = {}


def _levels(store: dict, ticker: str) -> Dict[str, float]:
    """GBP total-return levels by date, memoised per (store, ticker).

    ⚑ COST ONLY. The gate asks about ~63 candidates against the same 6-name sleeve, so without
    this the sleeve's series is rebuilt 63 times and the pre-run grew 50s. Keyed on the store
    object's identity, so a different store cannot collide with a cached one."""
    key = (id(store), ticker)
    got = _LEVEL_CACHE.get(key)
    if got is not None:
        return got
    rec = ((store.get("names") or {}).get(ticker) or {})
    out = {}
    for d, o in (rec.get("observations") or {}).items():
        g = (o or {}).get("gbp")
        if g is not None:
            out[d] = float(g)
    _LEVEL_CACHE[key] = out
    return out


def returns_on(store: dict, ticker: str, dates: Sequence[str]) -> List[float]:
    lv = _levels(store, ticker)
    v = [lv[d] for d in dates]
    return [v[i] / v[i - 1] - 1.0 for i in range(1, len(v)) if v[i - 1]]


def common_dates(store: dict, tickers: Iterable[str]) -> List[str]:
    keys = None
    for t in tickers:
        k = set(_levels(store, t))
        keys = k if keys is None else (keys & k)
    return sorted(keys or [])


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _cov(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = _mean(a[:n]), _mean(b[:n])
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)


def staleness(store: dict, ticker: str) -> Optional[float]:
    """Share of weekly returns that are EXACTLY zero — the fingerprint of a stale or inert quote.

    A name that does not move cannot be shown to be uncorrelated; it can only be shown to be
    unpriced. This is the liquidity control that stops a low-beta preference from becoming an
    illiquidity preference."""
    lv = _levels(store, ticker)
    ds = sorted(lv)
    if len(ds) < MIN_OVERLAP_WEEKS:
        return None
    r = [lv[ds[i]] / lv[ds[i - 1]] - 1.0 for i in range(1, len(ds)) if lv[ds[i - 1]]]
    return (sum(1 for x in r if x == 0.0) / len(r)) if r else None


# ────────────────────────────────────────────────────────────────────── sleeve
def sleeve_weights(portfolio: dict, exclude: Optional[str] = None) -> Dict[str, float]:
    """Value weights of the held direct holdings, normalised, optionally self-excluded.

    ⚑ SELF-EXCLUSION IS NOT COSMETIC. A held name's beta to a sleeve that CONTAINS it is
    mechanically near 1 and says nothing about whether adding to it concentrates the book."""
    held = {s.get("ticker"): float(s.get("value_gbp") or 0.0)
            for s in (portfolio.get("stocks") or []) if s.get("ticker")}
    held.pop(exclude, None)
    tot = sum(held.values())
    if not tot:
        raise RiskRefused("the stock sleeve has no value — beta to it is undefined, not zero")
    return {k: v / tot for k, v in held.items() if v}


def sigma(store: dict, weights: Dict[str, float], dates: Optional[Sequence[str]] = None
          ) -> float:
    """Annualised volatility of a weighted basket, on the window common to its members."""
    names = [t for t, w in weights.items() if w]
    if not names:
        raise RiskRefused("no names to measure")
    dates = dates or common_dates(store, names)
    if len(dates) - 1 < MIN_OVERLAP_WEEKS:
        raise RiskRefused("only %d overlapping weeks, %d required"
                          % (max(len(dates) - 1, 0), MIN_OVERLAP_WEEKS))
    R = {t: returns_on(store, t, dates) for t in names}
    var = sum(weights[a] * weights[b] * _cov(R[a], R[b]) for a in names for b in names)
    return math.sqrt(max(var, 0.0) * WEEKS_PER_YEAR)


def beta_to_sleeve(store: dict, ticker: str, weights: Dict[str, float],
                   lags: int = DIMSON_LAGS) -> dict:
    """DIMSON beta of `ticker` to the weighted sleeve: the SUM of the contemporaneous and lagged
    slopes, which is the standard correction for non-synchronous trading.

    Returns the components too, so "this name looks uncorrelated" and "this name trades out of
    phase" are distinguishable rather than one number."""
    names = [t for t, w in weights.items() if w]
    dates = common_dates(store, list(names) + [ticker])
    if len(dates) - 1 < MIN_OVERLAP_WEEKS:
        raise RiskRefused("%s: only %d overlapping weeks with the sleeve, %d required"
                          % (ticker, max(len(dates) - 1, 0), MIN_OVERLAP_WEEKS))
    R = {t: returns_on(store, t, dates) for t in names}
    n = len(next(iter(R.values())))
    p = [sum(weights[t] * R[t][i] for t in names) for i in range(n)]
    c = returns_on(store, ticker, dates)
    var_p = _cov(p, p)
    if var_p <= 0:
        raise RiskRefused("%s: sleeve variance is zero over the window" % ticker)
    betas = []
    for L in range(0, max(lags, 0) + 1):
        # candidate at t against sleeve at t-L
        cc, pp = (c[L:], p[:n - L]) if L else (c, p)
        betas.append(_cov(cc, pp) / var_p)
    b0 = betas[0]
    # ⚑ THE CORRECTION MAY ONLY RAISE BETA, AND THAT IS NOT A FUDGE — MEASURED 05-Sep-2026.
    # Dimson exists to remove the DOWNWARD bias non-synchronous trading puts on a thinly traded
    # name's beta, and on this book it does exactly that where it should: the lag term is
    # POSITIVE for the illiquid non-US listings (SUBC.OL 0.05 -> 0.16, IDR.MC 0.16 -> 0.22,
    # ORNBV.HE 0.04 -> 0.06). But for liquid US momentum names the lag term came out NEGATIVE
    # (NVDA 0.67 -> 0.53, ANET 0.65 -> 0.50), which is short-horizon mean reversion and sampling
    # noise, not non-synchronicity — and taking it would have let NVDA under a 0.60 cap on an
    # adjustment the correction was never built to make. So the sum is used only when it
    # EXCEEDS the contemporaneous slope. The correction can fix the bias it was designed for and
    # cannot invent the opposite one.
    bsum = max(b0, sum(betas))
    # ⚑ THE CORRECTION'S OWN ASSUMPTION, MEASURED RATHER THAN ASSUMED. The Dimson sum is valid
    # when the REFERENCE return is serially uncorrelated; where the sleeve is autocorrelated the
    # lag term picks up that autocorrelation and inflates beta for a perfectly synchronous name.
    # Real weekly equity returns sit near zero here, but "near zero" is a measurement, so it is
    # reported with the beta it qualifies rather than trusted silently.
    ac1 = (_cov(p[1:], p[:-1]) / var_p) if len(p) > 2 and var_p > 0 else None
    sd_c = math.sqrt(max(_cov(c, c), 0.0))
    rho = (_cov(c, p) / (math.sqrt(var_p) * sd_c)) if sd_c > 0 else None
    res = math.sqrt(max(_cov(c, c) - bsum ** 2 * var_p, 0.0) * WEEKS_PER_YEAR)
    return {"ticker": ticker, "beta": bsum, "beta_contemporaneous": b0,
            "beta_dimson_sum": sum(betas), "beta_uplift": bsum - b0, "lags": lags, "rho": rho,
            "residual_vol_annual": res, "weeks": n,
            "sleeve_autocorr_lag1": ac1,
            "dimson_assumption": ("the lag term is only a non-synchronicity correction while the "
                                  "sleeve return is serially uncorrelated; sleeve_autocorr_lag1 "
                                  "reports how far that holds on this window"),
            "basis": ("Dimson beta = sum of slopes at lags 0..%d against the value-weighted held "
                      "sleeve (self-excluded), floored at the contemporaneous slope so the "
                      "correction can only raise beta; GBP total-return weekly, %d weeks"
                      % (lags, n))}


# ────────────────────────────────────────────────────────── the two transactions
def delta_sigma_add(store: dict, portfolio: dict, ticker: str, size_gbp: float,
                    sigma_before: Optional[float] = None) -> dict:
    """The risk consequence of ADDING `size_gbp` of `ticker` from CASH."""
    held = {s.get("ticker"): float(s.get("value_gbp") or 0.0)
            for s in (portfolio.get("stocks") or []) if s.get("ticker")}
    tot = sum(held.values())
    if not tot:
        raise RiskRefused("empty sleeve")
    w0 = {k: v / tot for k, v in held.items() if v}
    s0 = sigma(store, w0) if sigma_before is None else sigma_before
    nt = tot + size_gbp
    w1 = {k: v / nt for k, v in held.items() if v}
    w1[ticker] = w1.get(ticker, 0.0) + size_gbp / nt
    s1 = sigma(store, w1)
    return {"action": "ADD", "ticker": ticker, "size_gbp": size_gbp,
            "sigma_before": s0, "sigma_after": s1, "delta_sigma": s1 - s0,
            "delta_sigma_per_1k": (s1 - s0) / (size_gbp / 1000.0) if size_gbp else None}


def delta_sigma_switch(store: dict, portfolio: dict, donor: str, candidate: str,
                       size_gbp: float) -> dict:
    """The risk consequence of SELLING `size_gbp` of `donor` and BUYING the same of `candidate`.

    ⚑ THIS, NOT BETA, IS THE TEST FOR A REPLACEMENT. Sleeve value is unchanged, so the question
    is only whether the swap raises or lowers portfolio risk — and a high-beta candidate
    replacing a high-beta donor can be risk-neutral or risk-reducing while improving the return
    case. Blocking it on beta alone would refuse a better use of the same pound."""
    held = {s.get("ticker"): float(s.get("value_gbp") or 0.0)
            for s in (portfolio.get("stocks") or []) if s.get("ticker")}
    tot = sum(held.values())
    if not tot:
        raise RiskRefused("empty sleeve")
    if held.get(donor, 0.0) + 1e-9 < size_gbp:
        raise RiskRefused("%s holds %.2f, cannot fund a %.2f switch"
                          % (donor, held.get(donor, 0.0), size_gbp))
    w0 = {k: v / tot for k, v in held.items() if v}
    s0 = sigma(store, w0)
    after = dict(held)
    after[donor] -= size_gbp
    after[candidate] = after.get(candidate, 0.0) + size_gbp
    w1 = {k: v / tot for k, v in after.items() if v > 0}
    s1 = sigma(store, w1)
    return {"action": "SWITCH", "donor": donor, "ticker": candidate, "size_gbp": size_gbp,
            "sigma_before": s0, "sigma_after": s1, "delta_sigma": s1 - s0,
            "risk_improving": s1 <= s0}


# ────────────────────────────────────────────────────────────────────── the gate
def _thresholds():
    """Policy lives in isa_policy (R4.4). An unimportable policy is REFUSED, never defaulted —
    a silently-defaulted risk limit is worse than none, because it reports as enforced."""
    try:
        import isa_policy as pol
        return (float(pol.SLEEVE_BETA_MAX), int(pol.SLEEVE_BETA_DIMSON_LAGS),
                float(pol.SLEEVE_STALE_ZERO_RETURN_MAX))
    except Exception as exc:                                            # noqa: BLE001
        raise RiskRefused("isa_policy sleeve thresholds unavailable (%s: %s) — the gate cannot "
                          "be applied and must not be silently skipped (R4.9)"
                          % (type(exc).__name__, exc))


def gate_add(store: dict, portfolio: dict, ticker: str) -> dict:
    """Is `ticker` admissible as an ADDITION FROM CASH to the stock sleeve?

    Returns a verdict for every name, including the ones that pass — an instrument that only
    speaks when it blocks cannot be shown to be working."""
    beta_max, lags, stale_max = _thresholds()
    held = {s.get("ticker") for s in (portfolio.get("stocks") or [])}
    out = {"ticker": ticker, "action": "ADD", "beta_max": beta_max,
           "already_held": ticker in held}
    try:
        w = sleeve_weights(portfolio, exclude=ticker if ticker in held else None)
        b = beta_to_sleeve(store, ticker, w, lags=lags)
        out.update({k: b[k] for k in ("beta", "beta_contemporaneous", "beta_dimson_sum",
                                      "beta_uplift", "rho", "residual_vol_annual", "weeks",
                                      "basis")})
    except RiskRefused as exc:
        # ⚑ UNMEASURED IS NOT PASSED AND IT IS NOT BLOCKED — it is UNMEASURED (R2.10). The
        # caller decides, and it decides knowing which of the three it has.
        out.update({"verdict": "UNMEASURED", "reason": str(exc)})
        return out
    st = staleness(store, ticker)
    out["zero_return_share"] = st
    if st is not None and st > stale_max:
        out.update({"verdict": "BLOCKED", "reason":
                    "zero-return share %.2f exceeds %.2f — the series is stale or inert, so its "
                    "low beta is a pricing artefact rather than diversification (ISA-0600)"
                    % (st, stale_max)})
        return out
    if out["beta"] > beta_max:
        out.update({"verdict": "BLOCKED", "reason":
                    "Dimson beta %.2f to the held sleeve exceeds %.2f — as an ADDITION this "
                    "concentrates the factor the sleeve already carries. It is NOT blocked as a "
                    "switch candidate; a replacement is judged on net delta-sigma (ISA-0600)"
                    % (out["beta"], beta_max)})
        return out
    out["verdict"] = "PASS"
    out["reason"] = ("Dimson beta %.2f <= %.2f and zero-return share %s"
                     % (out["beta"], beta_max,
                        ("%.2f" % st) if st is not None else "unmeasured"))
    return out


def rank_by_marginal_risk(store: dict, portfolio: dict, tickers: Sequence[str],
                          size_gbp: float) -> List[dict]:
    """Order ADMITTED names by the risk they add per pound, cheapest first.

    ⚑ THIS IS THE ORDERING ONLY, NEVER THE ADMISSION. Every name reaching here has already
    passed the forward-led viability floor, the hard quality floor and the E[r] floor — the
    return side is controlled by those gates. What this decides is which of several names that
    all cleared the same bar receives the marginal pound, and it decides it on the only input
    with a defensible standard error while the IC gate is unmet."""
    rows = []
    base = None                     # sigma of the untouched sleeve: identical for every name
    for t in tickers:
        try:
            d = delta_sigma_add(store, portfolio, t, size_gbp, sigma_before=base)
            base = d["sigma_before"]
            d["measured"] = True
        except RiskRefused as exc:
            d = {"action": "ADD", "ticker": t, "measured": False, "reason": str(exc),
                 "delta_sigma": None}
        rows.append(d)
    # unmeasured names sort LAST and are named; they never silently take the top of the stack
    return sorted(rows, key=lambda r: (r["delta_sigma"] is None,
                                       r["delta_sigma"] if r["delta_sigma"] is not None else 0.0))
