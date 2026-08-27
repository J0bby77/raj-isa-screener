#!/usr/bin/env python3
"""
asset_drawdown.py — V2.1-D (ISA-0357) §8/§9 drawdown state machine. Defect B1 CORRECTED.
Authority: clean spec s10 row 4 ("defect B1 (sqrt 252) FIRST"); amendments A6, A7.

═══════════════════════════════════════════════════════════════════════════════════════════
DEFECT B1 — THE √252 ERROR, AND THE CONVENTION THIS MODULE PICKS
═══════════════════════════════════════════════════════════════════════════════════════════
V2.1 §8 and §9 both defined:

    abnormal_dd_z = relative_episode_return / (active_vol_DAILY * sqrt(days / 252))

Episode volatility for a DAILY sigma over d days is `sigma_daily * sqrt(d)`, NOT
`sigma_daily * sqrt(d/252)`. The denominator was understated by **sqrt(252) ~ 15.87x**, so every
z was inflated ~16-fold and EVERY HOLDING AND EVERY FUND WOULD HAVE PRINTED SEVERE/EXTREME ON
FIRST RUN.

⚑ THE AMENDMENT OFFERED TWO CORRECTIONS AND REQUIRED THE MODULE TO STATE WHICH. THIS MODULE
USES `sigma_daily * sqrt(days)` DIRECTLY. Stated here, once, so no caller has to infer it, and
asserted by `_selftest` with a synthetic series at exactly 2 sigma returning z = 2.0.

═══════════════════════════════════════════════════════════════════════════════════════════
A6 — EMPIRICAL PERCENTILE IS PRIMARY, z IS THE CORROBORATOR
═══════════════════════════════════════════════════════════════════════════════════════════
V2.1 listed the two as complementary and then defined every state trigger in sigma. But a
drawdown is a MAXIMUM over an episode, so its null distribution is not normal: for a driftless
random walk E[max drawdown] ~ 1.26 * sigma * sqrt(n), not zero. Sigma thresholds calibrated on
the normal flag ROUTINE behaviour as abnormal even after B1 is fixed.

So: the empirical own-history percentile decides the state; z is reported beside it; and where
they disagree BOTH are published (R6.2 — disagreement is published, never blended).

⚑ COVERAGE GATE BEFORE ANY STATE. The 5y own-history percentile needs deep history for
GDWN.L, APN.L, BFT.WA, ORNBV.HE and LOOMIS.ST. Below threshold the state is UNMEASURED and the
position is capped at its CURRENT target — never assigned a state from a thin sample (R4.10).

⚑ A7 PREREQUISITE — NOW ENFORCED, 26-Aug-2026 (ISA-0440). It said "NOT ENFORCED HERE BUT
STATED" for eleven days, and a prerequisite that lives in a docstring is not a prerequisite.
The active-fund z depends on benchmark integrity and 4 of 27 series carry a defect (IWRD.L and
IJPN.L silently missing dividends). A dividend-less benchmark understates the BENCHMARK's return,
which OVERSTATES the fund's relative return, which makes the active drawdown look SHALLOWER than
it is — so the defect produces false NEGATIVES and suppresses this flag exactly when it is needed.
`benchmark_precondition()` reads `benchmark_registry.validate_all()` and, on the FUND route, an
unclean or unreadable registry returns UNMEASURED / CAP_AT_CURRENT. It never returns NORMAL: a
control fed a null BLOCKS, it does not pass (R4.3). Rollback: `A7_BENCHMARK_GATE = False`.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional

TRADING_DAYS = 252

# Empirical percentile thresholds (A6). PROVISIONAL — published before they gate (A2's standard).
PCTL_WATCH = 0.80
PCTL_SEVERE = 0.90
PCTL_EXTREME = 0.97
MIN_EPISODES_FOR_PERCENTILE = 20
MIN_DAYS_FOR_SIGMA = 60

NORMAL, WATCH, SEVERE, EXTREME, UNMEASURED = "NORMAL", "WATCH", "SEVERE", "EXTREME", "UNMEASURED"


def episode_sigma(sigma_daily: float, days: int) -> float:
    """⚑ DEFECT B1 CORRECTED. sigma over d days = sigma_daily * sqrt(d).

    The V2.1 form divided by 252 inside the root, understating this by sqrt(252) = 15.87x."""
    if sigma_daily is None or sigma_daily <= 0:
        raise ValueError("sigma_daily must be positive — a zero sigma would make every z "
                         "infinite, which is the FC-A pattern (a plausible-looking number "
                         "standing in for an unmeasurable one)")
    if days is None or days <= 0:
        raise ValueError("days must be positive")
    return float(sigma_daily) * math.sqrt(int(days))


def drawdown_z(episode_return: float, sigma_daily: float, days: int) -> float:
    """z = episode return / episode sigma. NEGATIVE for a drawdown."""
    return float(episode_return) / episode_sigma(sigma_daily, days)


def _legacy_v21_z(episode_return: float, sigma_daily: float, days: int) -> float:
    """The DEFECTIVE V2.1 formula, retained ONLY so the selftest can measure the 15.87x gap.
    Never called in production — a wrong formula kept as a callable is how a wrong formula
    comes back."""
    return float(episode_return) / (float(sigma_daily) * math.sqrt(int(days) / TRADING_DAYS))


def max_drawdown(levels: List[float]) -> dict:
    """Peak-to-trough over the series, with the episode length that produced it."""
    if not levels or len(levels) < 2:
        return {"depth_pct": None, "days": 0, "measured": False,
                "detail": "fewer than two observations"}
    peak, peak_i, worst, w_start, w_end = levels[0], 0, 0.0, 0, 0
    for i, v in enumerate(levels):
        if v > peak:
            peak, peak_i = v, i
        dd = (v / peak - 1.0) if peak else 0.0
        if dd < worst:
            worst, w_start, w_end = dd, peak_i, i
    return {"depth_pct": round(worst * 100, 4), "days": w_end - w_start, "measured": True,
            "peak_index": w_start, "trough_index": w_end}


def expected_max_drawdown(sigma_daily: float, n_days: int) -> float:
    """E[max drawdown] ~ 1.26 * sigma * sqrt(n) for a driftless walk — the A6 null.

    ⚑ This is why a sigma threshold calibrated on the normal is wrong: the EXPECTED maximum is
    already 1.26 sigma, so a '2 sigma' drawdown is barely above the null."""
    return -1.26 * float(sigma_daily) * math.sqrt(int(n_days))


A7_BENCHMARK_GATE = True          # ROLLBACK (R4.13): False -> the fund route stops checking.

FUND_ROUTE = "fund"               # measured against a benchmark_registry comparator (V2.1 s9)
STOCK_ROUTE = "stock"             # measured against its own history / a price comparator


def benchmark_precondition(validate=None) -> dict:
    """-> the A7 prerequisite as a MEASURED state, not a sentence in a docstring.

    ⚑ AN UNREADABLE REGISTRY IS TREATED AS UNCLEAN, NOT AS ABSENT-AND-THEREFORE-FINE. The whole
    class of defect this guards against (a benchmark quietly missing dividends) is invisible in
    the output it corrupts, so "we could not check" and "it is dirty" have to produce the same
    refusal (R4.3). The difference is recorded in `state`, not in the consequence.
    """
    try:
        import benchmark_registry as _br
        v = (validate or _br.validate_all)()
    except Exception as e:                                              # noqa: BLE001
        return {"state": "UNREADABLE", "clean": False,
                "errors": ["%s: %s" % (type(e).__name__, e)],
                "basis": ("benchmark_registry could not be read, so benchmark integrity is "
                          "UNKNOWN. A7 treats unknown as unclean.")}
    errs = list(v.get("errors") or [])
    return {"state": ("CLEAN" if not errs else "UNCLEAN"), "clean": not errs,
            "as_of": v.get("as_of"), "errors": errs,
            "n_comparators": len(v.get("comparators") or {}),
            "adjudicated": list(v.get("adjudicated") or []),
            "basis": ("benchmark_registry.validate_all(): %d comparator(s), %d error(s). A "
                      "dividend-less benchmark OVERSTATES a fund's relative return and would "
                      "SUPPRESS this flag, so the fund route may not run against a dirty "
                      "registry (A7)." % (len(v.get("comparators") or {}), len(errs)))}


def classify(*, episode_return_pct: float, sigma_daily_pct: float, days: int,
             history_depths_pct: Optional[List[float]] = None,
             coverage_years: Optional[float] = None,
             min_coverage_years: float = 3.0,
             route: str = STOCK_ROUTE,
             benchmark_state: Optional[dict] = None,
             period: str = "day") -> dict:
    """A6 state machine. Empirical percentile PRIMARY, z CORROBORATOR, disagreement PUBLISHED.

    `route=FUND_ROUTE` additionally requires the A7 benchmark precondition (see module docstring).

    ⚑ `sigma_daily_pct` and `days` ARE A UNIT PAIR, and `period` NAMES THE UNIT. The arithmetic
    (`sigma * sqrt(n)`) is period-agnostic, so a monthly sigma with a count of months is exactly
    as correct as a daily sigma with a count of days — and exactly as WRONG if the two are mixed.
    The fund route runs on MONTHS because that is the resolution the NAV series is published at,
    and every output it produces carries `period: "month"` so no later reader can take `days: 14`
    for a fortnight. A parameter named for one unit and fed another is how B1 (sqrt(252)) and the
    X-Ray date defect both happened; naming it is cheaper than renaming it (R4.2).
    """
    hist = [h for h in (history_depths_pct or []) if h is not None]

    # ── A7 PREREQUISITE, BEFORE ANYTHING ELSE (ISA-0440) ─────────────────────────────
    if route == FUND_ROUTE and A7_BENCHMARK_GATE:
        bm = benchmark_state if benchmark_state is not None else benchmark_precondition()
        if not bm.get("clean"):
            return {
                "state": UNMEASURED, "measured": False,
                "percentile": None, "z": None, "agree": None,
                "size_action": "CAP_AT_CURRENT",
                "refused_by": "A7_BENCHMARK_PRECONDITION", "period": period,
                "benchmark_precondition": bm,
                "detail": ("REFUSED before measurement: the benchmark registry is %s (%s). The "
                           "active-fund z is a fund return MINUS a benchmark return, and a "
                           "benchmark short of its dividends makes that difference look BETTER "
                           "than it is — so running this flag on a dirty registry does not "
                           "produce a noisy answer, it produces a REASSURING one. UNMEASURED and "
                           "capped at current is the correct output (R4.3, A7)."
                           % (bm.get("state"), "; ".join(bm.get("errors") or []) or "no detail")),
            }

    # ── coverage gate FIRST (R4.10) ──────────────────────────────────────────────────
    if (coverage_years is not None and coverage_years < min_coverage_years) \
            or len(hist) < MIN_EPISODES_FOR_PERCENTILE:
        z = None
        try:
            z = round(drawdown_z(episode_return_pct, sigma_daily_pct, days), 4)
        except Exception:
            pass
        return {
            "state": UNMEASURED, "measured": False, "period": period,
            "percentile": None, "z": z, "agree": None,
            "size_action": "CAP_AT_CURRENT",
            "detail": (f"own-history coverage is {coverage_years}y against {min_coverage_years}y "
                       f"required, with {len(hist)} prior episodes against "
                       f"{MIN_EPISODES_FOR_PERCENTILE}. A6: below threshold the state is "
                       f"UNMEASURED and the position is capped at its CURRENT target. A state "
                       f"assigned from a thin sample is a verdict about the sample (R4.10)."),
        }

    depth = float(episode_return_pct)
    worse = sum(1 for h in hist if h <= depth)
    pctl = worse / len(hist)                      # fraction of history AT LEAST this bad
    rank = 1.0 - pctl                             # how extreme, 0..1
    z = round(drawdown_z(depth, sigma_daily_pct, days), 4)

    if rank >= PCTL_EXTREME:
        state = EXTREME
    elif rank >= PCTL_SEVERE:
        state = SEVERE
    elif rank >= PCTL_WATCH:
        state = WATCH
    else:
        state = NORMAL

    # z's own reading, on the SAME scale, for the disagreement report
    z_state = (EXTREME if z <= -3.0 else SEVERE if z <= -2.0 else
               WATCH if z <= -1.5 else NORMAL)
    e_max = expected_max_drawdown(sigma_daily_pct, days)

    return {
        "state": state, "measured": True, "period": period,
        "percentile_rank": round(rank, 4), "z": z, "z_state": z_state,
        "agree": state == z_state,
        "expected_max_dd_pct": round(e_max, 4),
        "depth_vs_null_ratio": round(depth / e_max, 3) if e_max else None,
        "n_history_episodes": len(hist),
        "size_action": ("NONE" if state == NORMAL else "REVIEW" if state == WATCH
                        else "TRIM_CANDIDATE"),
        "detail": (f"depth {depth:.2f}% is at rank {rank:.1%} of {len(hist)} own-history "
                   f"episodes -> {state} (PRIMARY). z = {z} -> {z_state} (corroborator). "
                   f"E[max dd] for a driftless walk of {days}d at this sigma is {e_max:.2f}%, "
                   f"so the null is already {abs(e_max):.2f}% deep."
                   + ("" if state == z_state else
                      f" ⚑ THE TWO DISAGREE and BOTH are published (R6.2) — a blended number "
                      f"would conceal that the empirical and normal readings differ, which is "
                      f"itself information about this name's tail.")),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §9 — THE ACTIVE-FUND DRAWDOWN, WIRED.  ISA-0440 / A7, 26-Aug-2026.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# ⚑ THIS MODULE WAS BUILT ON 26-Aug AND CALLED BY NOTHING. `monthly_isa_prerun` Step 6.12's own
# label reads "... + asset_drawdown + risk_contribution + retention" and the step never imported
# it. That is the project's dominant failure class — an absent execution reporting success — with
# the aggravation that the run surface ADVERTISED the module by name. Raised as its own item.
#
# ⚑ WHAT AN "EPISODE" IS HERE, STATED BECAUSE IT IS A CHOICE. The active series is the fund's
# monthly GBP return MINUS its comparator's, compounded into an index. An episode is a peak-to-
# trough excursion of that index. The CURRENT episode is measured from the running maximum to
# today; the OWN HISTORY is every completed excursion before it. So the percentile answers "how
# does this shortfall compare with this fund's own past shortfalls against this benchmark", not
# "how does it compare with a normal distribution" — which is the whole of A6.
#
# ⚑ AND IT RUNS ON MONTHS. The NAV series is monthly, so `period="month"` is stamped on every
# output. Nothing here converts to days: a conversion would be a second unit for one quantity.


def _active_index(fund_monthly: dict, bench_monthly: dict) -> tuple:
    """-> (keys, relative monthly returns %, cumulative active index). Common months only."""
    common = sorted(set(fund_monthly) & set(bench_monthly))
    rel = [(float(fund_monthly[k]) - float(bench_monthly[k])) * 100.0 for k in common]
    idx, lvl = [], 100.0
    for r in rel:
        lvl *= (1.0 + r / 100.0)
        idx.append(lvl)
    return common, rel, idx


def _episodes(index: List[float]) -> tuple:
    """-> (completed_episode_depths_pct, current_depth_pct, current_len).

    A completed episode is a peak -> trough -> RECOVERY back to that peak. The excursion still
    open at the end of the series is the CURRENT one and is deliberately NOT counted in the
    history it is about to be compared against (that would be scoring a name against itself).
    """
    completed, peak, trough, since_peak = [], None, None, 0
    for lvl in index:
        if peak is None or lvl >= peak:
            if trough is not None and peak:
                completed.append((trough / peak - 1.0) * 100.0)
            peak, trough, since_peak = lvl, None, 0
        else:
            trough = lvl if trough is None else min(trough, lvl)
            since_peak += 1
    current = ((trough / peak - 1.0) * 100.0) if (trough is not None and peak) else 0.0
    return completed, current, since_peak


def fund_active_drawdowns(universe=None, portfolio=None, sedols=None,
                          benchmark_state=None) -> dict:
    """-> the §9 active-drawdown state for every held fund, behind the A7 precondition.

    ⚑ THE PRECONDITION IS EVALUATED ONCE AND PASSED DOWN, so a dirty registry refuses EVERY fund
    for the same stated reason rather than twelve times over with twelve slightly different
    messages. It is also returned, so the caller can render the refusal (ISA-0439).
    """
    import benchmark_registry as _br
    U = universe if universe is not None else _br.load_universe()
    bm = benchmark_state if benchmark_state is not None else benchmark_precondition()

    if sedols is None:
        if portfolio is None:
            import capital_destination as _cd
            portfolio = _cd._load_portfolio()
        sedols = [f["ticker"] for f in portfolio.get("funds", [])]

    rows, unreadable = [], []
    for sd in sorted(sedols):
        try:
            fr = _br._fund_returns(sd, U)
            bt, _cmt = _br.comparator_for(sd, U)
            bench = _br.gbp_returns(bt)
            keys, rel, idx = _active_index(fr, bench)
            if len(idx) < 2:
                raise ValueError("only %d common month(s) with %s" % (len(idx), bt))
            hist, cur, cur_len = _episodes(idx)
            sigma_m = statistics.pstdev(rel) if len(rel) > 1 else 0.0
            st = classify(episode_return_pct=cur, sigma_daily_pct=sigma_m,
                          days=max(cur_len, 1), history_depths_pct=hist,
                          coverage_years=len(idx) / 12.0, min_coverage_years=3.0,
                          route=FUND_ROUTE, benchmark_state=bm, period="month")
            rows.append({"sedol": sd, "comparator": bt, "state": st["state"],
                         "size_action": st["size_action"],
                         "current_active_drawdown_pct": round(cur, 2),
                         "months_since_peak": cur_len,
                         "n_completed_episodes": len(hist),
                         "months_common": len(idx),
                         "first_month": "%04d-%02d" % keys[0], "last_month": "%04d-%02d" % keys[-1],
                         "active_sigma_monthly_pct": round(sigma_m, 4),
                         "detail": st["detail"], "refused_by": st.get("refused_by")})
        except Exception as e:                                          # noqa: BLE001
            # R4.9 — a fund we cannot read is COUNTED and NAMED. It is not dropped, and it is
            # certainly not reported as NORMAL.
            unreadable.append({"sedol": sd, "error": "%s: %s" % (type(e).__name__, e)})
    return {
        "state": ("MEASURED" if bm.get("clean") else "REFUSED_BENCHMARK_PRECONDITION"),
        "item": "ISA-0440 / A7 / V2.1 s9",
        "period": "month",
        "benchmark_precondition": bm,
        "n_funds": len(sedols), "n_read": len(rows), "n_unreadable": len(unreadable),
        "unreadable": unreadable,
        "funds": rows,
        "basis": ("active = fund monthly GBP return - comparator monthly GBP return, compounded; "
                  "an episode is a peak-to-trough excursion of that index; the CURRENT excursion "
                  "is compared against this fund's own COMPLETED ones (A6: empirical percentile "
                  "primary, z corroborator). Behind the A7 benchmark precondition."),
    }


def _selftest():
    # ── B1: the correction, and the size of the error it removes ──────────────────────
    z = drawdown_z(-2.0, 1.0, 1)
    assert abs(z + 2.0) < 1e-9, f"a 1-day 2-sigma move must give z=-2.0, got {z}"
    z100 = drawdown_z(-10.0, 1.0, 100)          # sigma_100d = 1.0*sqrt(100) = 10
    assert abs(z100 + 1.0) < 1e-9, z100
    # the mandated unit test: a synthetic series at exactly 2 sigma returns z = 2.0
    for d in (1, 10, 63, 252):
        s = 1.7
        ret = -2.0 * episode_sigma(s, d)
        assert abs(drawdown_z(ret, s, d) + 2.0) < 1e-9, (d, drawdown_z(ret, s, d))
    # and the legacy form is wrong by exactly sqrt(252)
    ratio = _legacy_v21_z(-10.0, 1.0, 100) / drawdown_z(-10.0, 1.0, 100)
    assert abs(ratio - math.sqrt(TRADING_DAYS)) < 1e-9, ratio
    assert abs(ratio - 15.8745) < 1e-3, ratio
    # ⚑ the concrete consequence: a routine 1-sigma move printed as 15.9 sigma
    assert _legacy_v21_z(-10.0, 1.0, 100) < -15.0

    try:
        episode_sigma(0.0, 10); raise AssertionError("zero sigma must raise")
    except ValueError:
        pass

    # ── max_drawdown ──────────────────────────────────────────────────────────────────
    md = max_drawdown([100, 110, 99, 105, 88, 95])
    assert abs(md["depth_pct"] - (88 / 110 - 1) * 100) < 1e-6, md
    assert md["days"] == 3, md
    assert max_drawdown([100])["measured"] is False

    # ── A6: percentile is PRIMARY ─────────────────────────────────────────────────────
    hist = [-(i + 1) for i in range(40)]     # -1% .. -40%
    mild = classify(episode_return_pct=-3.0, sigma_daily_pct=1.0, days=30,
                    history_depths_pct=hist, coverage_years=5)
    assert mild["state"] == NORMAL, mild
    deep = classify(episode_return_pct=-39.5, sigma_daily_pct=1.0, days=30,
                    history_depths_pct=hist, coverage_years=5)
    assert deep["state"] == EXTREME, deep

    # ── the coverage gate refuses rather than guessing ────────────────────────────────
    thin = classify(episode_return_pct=-20.0, sigma_daily_pct=1.0, days=30,
                    history_depths_pct=hist[:5], coverage_years=5)
    assert thin["state"] == UNMEASURED and thin["size_action"] == "CAP_AT_CURRENT", thin
    short = classify(episode_return_pct=-20.0, sigma_daily_pct=1.0, days=30,
                     history_depths_pct=hist, coverage_years=1.0)
    assert short["state"] == UNMEASURED, short
    assert short["measured"] is False

    # ── the null is not zero, and that is reported ────────────────────────────────────
    e = expected_max_drawdown(1.0, 100)
    assert abs(e + 12.6) < 1e-6, e
    assert deep["expected_max_dd_pct"] is not None

    # ── disagreement is PUBLISHED, never blended ──────────────────────────────────────
    dis = classify(episode_return_pct=-39.5, sigma_daily_pct=20.0, days=30,
                   history_depths_pct=hist, coverage_years=5)
    assert dis["state"] == EXTREME and dis["z_state"] == NORMAL, dis
    assert dis["agree"] is False and "DISAGREE" in dis["detail"].upper(), dis

    # ⚑ THE HEADLINE REGRESSION: under the OLD formula a routine name printed SEVERE.
    # Under the corrected one it does not.
    routine_z = drawdown_z(-6.0, 1.0, 30)
    assert routine_z > -1.5, routine_z
    assert _legacy_v21_z(-6.0, 1.0, 30) < -3.0, "the old formula would have printed EXTREME"
    print("asset_drawdown selftest OK (21 assertions) — B1 corrected, error was "
          f"{math.sqrt(TRADING_DAYS):.4f}x")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(f"episode_sigma convention: sigma_daily * sqrt(days). "
          f"V2.1's form was wrong by sqrt(252) = {math.sqrt(TRADING_DAYS):.4f}x")
