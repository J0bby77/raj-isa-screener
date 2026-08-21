#!/usr/bin/env python3
"""FUND ROTATION, CONCENTRATION AND MANDATE-DRIFT STUDY — the analysis four ANALYSIS_FIRST
register items were waiting on.

  ISA-0153 (D-18)  rotation on trailing performance has no measured edge — ONE datapoint
  ISA-0154 (D-19)  a mechanical FRS-ranked rule buys the worst-evidenced fund
  ISA-0165 (O-7)   T4 mandate-breach trigger: threshold DECLARED, never calibrated
  ISA-0329         the sleeve's measured edge sits in ONE manager's ONE process

R13.2: analysis precedes design, and the analysis is a deliverable in its own right. Every number
here comes off disk — the NAV/benchmark panel `fund_learning.build_panel()` already builds from
the declared routes. Nothing is fetched and nothing is re-derived (R4.5).

R3.1: the three questions were fixed BEFORE the grids were run, and each grid is reported IN FULL.
A rotation study that reports its best variant is a search, not a test.

CLI:
  python3 fund_rotation_analysis.py --run --write
  python3 fund_rotation_analysis.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STUDY_VERSION = "1.0.0"
OUT_NAME = "fund_rotation_analysis_aug2026.json"

# ── DECLARED grid. Fixed before running (R3.1). Every cell is reported, none is selected.
ROTATE_TOP_N = (1, 2, 3)
ROTATE_LOOKBACK_M = (6, 12, 24)
ROTATE_CADENCE_M = (1, 6, 12)
N_RANDOM_CONTROLS = 200          # R3.8 negative control: random selection, same cadence
RANDOM_SEED = 20260815

T4_ROLL_WINDOW_M = 36            # matches fund_learning.ROLLING_WINDOW_M — one home for the window
T4_CANDIDATE_DROPS = (0.10, 0.15, 0.20, 0.25, 0.30)   # R2 fall below own median, grid not a pick
T4_SUSTAIN_OBS = 2               # O-7's "two consecutive semi-annual observations"
SEMI_ANNUAL_M = 6

# ── ISA-0165 · THE OPERATIVE T4 TRIGGER (19-Aug-2026) ─────────────────────────────────────────
# The calibration below chose D from a FIRE RATE, not from a cross-sectional gap: 0.20 sits just
# inside the pooled P05 (-0.248) and fires on 5.9% of semi-annual observations across two funds.
# 0.10 fires on 11.1% and four funds -- a trigger that fires on a third of the sleeve across a
# cycle is a description, not a trigger. 0.25 and 0.30 flag the same two funds with fewer events:
# past 0.20 the threshold buys quiet, not precision.
T4_ENABLED = True                # R4.13 ROLLBACK: False -> the trigger REFUSES and emits nothing.
T4_DROP = 0.20                   # the single constant. Rollback is changing this one number.
# ⚑ R15.2. The fire rate is RE-DERIVED every run and checked against this declared band, so the
# trigger cannot quietly become a description of the sleeve (too high) or a decoration that can
# never fire (too low). Leaving the band raises an item -- it does not silently recalibrate D.
T4_FIRE_RATE_BAND_PCT = (2.0, 8.0)
T4_MIN_SEMI_ANNUAL_OBS = 40      # below this the rate is not a rate; the band check is REFUSED.


def _ann(rets):
    if not rets:
        return None
    g = 1.0
    for r in rets:
        g *= (1.0 + r)
    return (g ** (12.0 / len(rets)) - 1.0) * 100.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    n = len(xs)
    if n < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _pctile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = q * (len(s) - 1)
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (i - lo)


def _tstat(xs):
    """t on the mean. Reported, never used as an estimator (R3.4)."""
    n = len(xs)
    if n < 3:
        return None
    s = _sd(xs)
    return (_mean(xs) / (s / math.sqrt(n))) if s else None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# common window — every question below is answered on ONE declared window (R2.6)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def common_window(panel):
    """Months where EVERY fund and its benchmark has a return.

    ⚑ A rotation study run on each fund's own window would let a fund enter the comparison only
    in the periods it happened to exist, which is survivorship inside the test itself (R3.3).
    """
    sets = []
    for p in panel.values():
        sets.append(set(p["fund"]) & set(p["bench"]))
    if not sets:
        return []
    keys = sorted(set.intersection(*sets))
    return keys


def active_matrix(panel, keys):
    """Active return per fund = fund - its own comparator, monthly, over the common window.

    Simple active return, NOT the regression residual, and the choice is declared: the residual is
    orthogonal to the benchmark BY CONSTRUCTION, so a residual correlation matrix would understate
    shared exposure between two funds whose benchmarks differ. Active return keeps whatever the
    manager did that its own comparator did not, which is the quantity a concentration question is
    actually about.
    """
    return {sd: [p["fund"][k] - p["bench"][k] for k in keys] for sd, p in panel.items()}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ISA-0329 — how many INDEPENDENT processes is this sleeve actually running?
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _corr(a, b):
    n = len(a)
    ma, mb = _mean(a), _mean(b)
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((v - ma) ** 2 for v in a))
    db = math.sqrt(sum((v - mb) ** 2 for v in b))
    return (num / (da * db)) if da and db else None


def _eigenvalues_sym(M, iters=500):
    """Eigenvalues of a small symmetric matrix by cyclic Jacobi. Stdlib only, and exact enough:
    the quantity wanted is a ratio of sums, not any single eigenvalue."""
    n = len(M)
    A = [row[:] for row in M]
    for _ in range(iters):
        off = max(((abs(A[i][j]), i, j) for i in range(n) for j in range(i + 1, n)),
                  default=(0, 0, 0))
        if off[0] < 1e-12:
            break
        _, p, q = off
        if abs(A[p][p] - A[q][q]) < 1e-18:
            theta = math.pi / 4
        else:
            theta = 0.5 * math.atan2(2 * A[p][q], A[p][p] - A[q][q])
        c, s = math.cos(theta), math.sin(theta)
        for k in range(n):
            akp, akq = A[k][p], A[k][q]
            A[k][p] = c * akp + s * akq
            A[k][q] = -s * akp + c * akq
        for k in range(n):
            apk, aqk = A[p][k], A[q][k]
            A[p][k] = c * apk + s * aqk
            A[q][k] = -s * apk + c * aqk
    return sorted((A[i][i] for i in range(n)), reverse=True)


def concentration(panel, keys, process_keys=None):
    """ISA-0329. Two independent readings of the same question, published side by side (R6.2).

    (a) OUTPUT concentration — the correlation structure of realised active returns, summarised as
        an effective number of independent bets: N_eff = (sum L)^2 / sum L^2 over the eigenvalues
        of the active-return correlation matrix. On an N-fund sleeve this runs from 1 (all one bet)
        to N (all independent).
    (b) SOURCE concentration — how many DECLARED processes the sleeve runs. This is the reading
        ISA-0329 says is missing, and it cannot be computed: no fund in fund_universe.json carries
        a process or manager key. Reported as UNMEASURED, never as clean (R2.10).
    """
    A = active_matrix(panel, keys)
    sds = sorted(A)
    n = len(sds)
    C = [[1.0 if i == j else (_corr(A[sds[i]], A[sds[j]]) or 0.0) for j in range(n)]
         for i in range(n)]
    ev = _eigenvalues_sym(C)
    tot = sum(ev)
    n_eff = (tot ** 2) / sum(e * e for e in ev) if ev and sum(e * e for e in ev) else None
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append({"a": sds[i], "b": sds[j], "corr": round(C[i][j], 4)})
    pairs.sort(key=lambda p: -abs(p["corr"]))
    declared = {sd: (process_keys or {}).get(sd) for sd in sds}
    n_declared = len({v for v in declared.values() if v})
    return {
        "n_funds": n,
        "window_months": len(keys),
        "window": [f"{keys[0][0]}-{keys[0][1]:02d}", f"{keys[-1][0]}-{keys[-1][1]:02d}"],
        "effective_independent_bets": round(n_eff, 3) if n_eff else None,
        "effective_as_pct_of_nominal": round(100 * n_eff / n, 1) if n_eff else None,
        "top_pairwise_active_correlations": pairs[:8],
        "mean_pairwise_active_correlation": round(_mean([p["corr"] for p in pairs]), 4),
        "eigenvalue_top_share_pct": round(100 * ev[0] / tot, 1) if ev and tot else None,
        "source_concentration": {
            "declared_process_keys": n_declared,
            "status": "UNMEASURED" if not n_declared else "MEASURED",
            "why": ("No fund in fund_universe.json declares a process or manager key, so process "
                    "concentration CANNOT be computed - only the correlation of the OUTPUT can. "
                    "Two funds running one proprietary screen may show a modest output correlation "
                    "and still fail together the day that screen stops working, which is exactly "
                    "the exposure ISA-0329 names. UNMEASURED is not low (R2.10)."),
        },
        "read_this_first": (
            "The effective count is a property of ONE realised history and inherits its window "
            "(R2.6). It answers 'how many separate bets did this sleeve turn out to be', not 'how "
            "many will it be'. It is evidence for a concentration LIMIT, never a target."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ISA-0153 / ISA-0154 — does rotating on trailing performance beat doing nothing? Full grid.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _equal_weight_path(A_keys, panel, keys):
    return [_mean([panel[sd]["fund"][k] for sd in A_keys]) for k in keys]


def rotation_grid(panel, keys, seed=RANDOM_SEED):
    """Every (top_n, lookback, cadence) cell, plus a random-selection negative control.

    R3.8: the negative control is a rule with the SAME turnover and cadence that cannot possibly
    have skill. If the trailing rule does not beat it, 'no edge' is measured rather than asserted.
    """
    import random
    sds = sorted(panel)
    hold_all = _equal_weight_path(sds, panel, keys)
    base_ann = _ann(hold_all)
    rng = random.Random(seed)
    cells = []
    for top_n in ROTATE_TOP_N:
        for lb in ROTATE_LOOKBACK_M:
            for cad in ROTATE_CADENCE_M:
                if len(keys) <= lb + 12:
                    continue
                path, held_log, rebalances = [], [], 0
                held = None
                for i in range(lb, len(keys)):
                    if held is None or (i - lb) % cad == 0:
                        window = keys[i - lb:i]
                        score = {sd: _ann([panel[sd]["fund"][k] for k in window]) for sd in sds}
                        held = sorted(sds, key=lambda s: -(score[s] if score[s] is not None else -1e9))[:top_n]
                        rebalances += 1
                        held_log.append({"at": f"{keys[i][0]}-{keys[i][1]:02d}", "held": list(held)})
                    path.append(_mean([panel[sd]["fund"][keys[i]] for sd in held]))
                bench_path = hold_all[lb:]
                excess = [path[j] - bench_path[j] for j in range(len(path))]
                # negative control at identical cadence and turnover
                ctrl = []
                for _ in range(N_RANDOM_CONTROLS):
                    cpath, cheld = [], None
                    for i in range(lb, len(keys)):
                        if cheld is None or (i - lb) % cad == 0:
                            cheld = rng.sample(sds, top_n)
                        cpath.append(_mean([panel[sd]["fund"][keys[i]] for sd in cheld]))
                    ctrl.append(_ann(cpath))
                ctrl_sorted = sorted(c for c in ctrl if c is not None)
                rot_ann = _ann(path)
                pct_rank = (sum(1 for c in ctrl_sorted if c < rot_ann) / len(ctrl_sorted)
                            if ctrl_sorted else None)
                cells.append({
                    "top_n": top_n, "lookback_m": lb, "cadence_m": cad,
                    "n_months_scored": len(path), "rebalances": rebalances,
                    "rotation_ann_pct": round(rot_ann, 3) if rot_ann is not None else None,
                    "hold_all_ann_pct": round(_ann(bench_path), 3),
                    "excess_ann_pp": (round(rot_ann - _ann(bench_path), 3)
                                      if rot_ann is not None else None),
                    "excess_t": (round(_tstat(excess), 3) if _tstat(excess) is not None else None),
                    "random_control_median_ann_pct": (round(_pctile(ctrl_sorted, 0.5), 3)
                                                      if ctrl_sorted else None),
                    "beats_random_pctile": round(100 * pct_rank, 1) if pct_rank is not None else None,
                    "final_holding": held_log[-1]["held"] if held_log else None,
                })
    beat = [c for c in cells if (c["excess_ann_pp"] or 0) > 0]
    beat_ctrl = [c for c in cells if (c["beats_random_pctile"] or 0) >= 95.0]
    return {
        "hold_all_ann_pct": round(base_ann, 3) if base_ann is not None else None,
        "window": [f"{keys[0][0]}-{keys[0][1]:02d}", f"{keys[-1][0]}-{keys[-1][1]:02d}"],
        "window_months": len(keys),
        "n_funds": len(sds),
        "cells": cells,
        "cells_beating_hold_all": len(beat),
        "cells_total": len(cells),
        "cells_beating_random_control_at_95": len(beat_ctrl),
        "effective_n": (
            f"{len(keys)} months of ONE history across {len(sds)} correlated funds. The grid has "
            f"{len(cells)} cells drawn from that single path, so they are NOT {len(cells)} "
            f"independent tests - if any cell wins it is the one that fitted this path (R3.2). "
            f"The grid exists to show whether the RESULT IS UNIFORM, not to select a winner."),
        "read_this_first": (
            f"{len(beat)} of {len(cells)} cells beat holding everything, and "
            f"{len(beat_ctrl)} of {len(cells)} beat a random-selection control at the 95th "
            f"percentile. A rule that cannot beat random selection at identical turnover has no "
            f"measured skill, whatever its absolute return."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ISA-0165 (O-7) — calibrating T4, the mandate-breach trigger
# ══════════════════════════════════════════════════════════════════════════════════════════════

def t4_calibration(panel, window=T4_ROLL_WINDOW_M):
    """O-7's proposed rule: rolling 36m R2 falls >= D below the fund's OWN rolling-R2 median,
    sustained over two consecutive semi-annual observations. D was DECLARED at 0.20 and never
    calibrated. This produces the distribution D has to sit in, and the FIRE RATE at each candidate.

    ⚑ The number that decides a threshold is not the cross-sectional gap - it is how often the
    rule FIRES on a sleeve nobody thinks is breaching. A trigger firing on a third of the sleeve
    every period is a description, not a trigger.
    """
    import fund_learning as FL
    drift = FL.rolling_drift(panel, window=window)["per_fund"]
    per_fund, all_dev = {}, []
    for sd, v in drift.items():
        if "series" not in v:
            per_fund[sd] = {"refused": v.get("refused")}
            continue
        r2s = [s["r2"] for s in v["series"] if s["r2"] is not None]
        if len(r2s) < 12:
            per_fund[sd] = {"refused": f"only {len(r2s)} rolling R2 observations"}
            continue
        med = _pctile(sorted(r2s), 0.5)
        dev = [r - med for r in r2s]
        all_dev.extend(dev)
        per_fund[sd] = {
            "n_obs": len(r2s), "r2_median": round(med, 4),
            "r2_min": round(min(r2s), 4), "r2_max": round(max(r2s), 4),
            "worst_deviation": round(min(dev), 4),
            "current_deviation": round(dev[-1], 4),
            "semi_annual_obs": [round(d, 4) for d in dev[::SEMI_ANNUAL_M]],
        }
    grid = []
    for D in T4_CANDIDATE_DROPS:
        fired_funds, fired_obs, total_obs = [], 0, 0
        for sd, v in per_fund.items():
            if "semi_annual_obs" not in v:
                continue
            sa = v["semi_annual_obs"]
            total_obs += max(len(sa) - 1, 0)
            run, fired = 0, False
            for d in sa:
                run = run + 1 if d <= -D else 0
                if run >= T4_SUSTAIN_OBS:
                    fired = True
                    fired_obs += 1
            if fired:
                fired_funds.append(sd)
        grid.append({
            "drop_threshold": D,
            "funds_that_would_have_fired": sorted(fired_funds),
            "n_funds_fired": len(fired_funds),
            "fire_events": fired_obs,
            "semi_annual_observations": total_obs,
            "fire_rate_pct": round(100 * fired_obs / total_obs, 2) if total_obs else None,
        })
    devs = sorted(all_dev)
    return {
        "rolling_window_m": window,
        "sustain_observations": T4_SUSTAIN_OBS,
        "per_fund": per_fund,
        "pooled_deviation_distribution": {
            "n": len(devs),
            "p50": round(_pctile(devs, 0.50), 4) if devs else None,
            "p10": round(_pctile(devs, 0.10), 4) if devs else None,
            "p05": round(_pctile(devs, 0.05), 4) if devs else None,
            "p01": round(_pctile(devs, 0.01), 4) if devs else None,
            "min": round(devs[0], 4) if devs else None,
        },
        "threshold_grid": grid,
        "read_this_first": (
            "Deviations are measured against each fund's OWN rolling median, so a concentrated "
            "mandate with structurally low R2 is not penalised for its level - which is why a "
            "fixed R2 floor was the wrong instrument (O-7 records it flagging 1 of 11). The grid "
            "reports the fire rate at each candidate drop, because a threshold is chosen against "
            "how often it fires on a healthy sleeve, not against a gap in one cross-section."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ISA-0165 (O-7) — T4, THE OPERATIVE MANDATE-DRIFT TRIGGER
# ══════════════════════════════════════════════════════════════════════════════════════════════

def t4_mandate_drift(panel=None, calibration=None, drop=None, universe=None):
    """-> the T4 verdict per fund, and the R15.2 fire-rate check on the trigger itself.

    THE RULE. A fund's rolling 36-month R-squared against its own comparator falls `drop` or more
    BELOW that fund's OWN rolling-R2 median, and stays there for T4_SUSTAIN_OBS consecutive
    semi-annual observations.

    ⚑ MEASURED AGAINST THE FUND'S OWN MEDIAN, never against a fixed R2 floor. A concentrated value
    mandate has structurally low R2 with no drift at all; O-7 records a fixed floor flagging 1 of
    11 funds for having the mandate it was bought for.

    ⚑ A FUND WITH NO TESTABLE MANDATE REFUSES, IT IS NOT SKIPPED. `benchmark_registry.t4_mandate_for`
    is the one home for "what is this fund supposed to be doing", and it RAISES where neither a
    prospectus benchmark nor an investor mandate exists. NOT_TESTED and TESTED_AND_FINE must never
    render the same (R2.10) -- so a refusal is a state on the row, with its reason.

    ⚑ THE MANDATE BASIS TRAVELS WITH THE ALERT. `prospectus` means the manager has left the
    benchmark it published; `investor` means this is no longer the exposure Raj bought. Those are
    different claims and an alert that does not say which is not actionable.
    """
    if not T4_ENABLED:
        return {"state": "DISABLED", "reason": "fund_rotation_analysis.T4_ENABLED is False (R4.13)"}
    D = float(T4_DROP if drop is None else drop)
    if calibration is None:
        if panel is None:
            import fund_learning as FL
            panel = FL.build_panel()["panel"]
        calibration = t4_calibration(panel)

    try:
        import benchmark_registry as _breg
        U = universe if universe is not None else _breg.load_universe()
    except Exception as _e:                                      # noqa: BLE001
        _breg, U = None, None

    rows, fired_obs, total_obs = [], 0, 0
    for sd, v in sorted((calibration.get("per_fund") or {}).items()):
        mandate, basis, refusal = None, None, None
        if _breg is not None:
            try:
                mandate, basis = _breg.t4_mandate_for(sd, U)
            except Exception as e:                               # noqa: BLE001
                refusal = "%s: %s" % (type(e).__name__, e)
        else:
            refusal = "benchmark_registry unavailable — the mandate question has no answer here"

        if "semi_annual_obs" not in v:
            rows.append({"sedol": sd, "state": "REFUSED", "mandate": mandate,
                         "mandate_basis": basis,
                         "reason": v.get("refused") or "no rolling-R2 series"})
            continue
        if refusal:
            rows.append({"sedol": sd, "state": "REFUSED", "mandate": None, "mandate_basis": None,
                         "reason": refusal})
            continue

        sa = v["semi_annual_obs"]
        total_obs += max(len(sa) - 1, 0)
        run_len, streak, events = 0, 0, 0
        for d in sa:
            run_len = run_len + 1 if d <= -D else 0
            if run_len >= T4_SUSTAIN_OBS:
                events += 1
        streak = run_len                      # the CURRENT consecutive count, at the last obs
        fired_obs += events
        firing = streak >= T4_SUSTAIN_OBS
        rows.append({
            "sedol": sd,
            "state": ("FIRING" if firing else
                      ("WATCH" if (sa and sa[-1] <= -D) else "OK")),
            "mandate": mandate, "mandate_basis": basis,
            "r2_median": v.get("r2_median"),
            "current_deviation": v.get("current_deviation"),
            "worst_deviation": v.get("worst_deviation"),
            "consecutive_obs_below": streak,
            "sustain_required": T4_SUSTAIN_OBS,
            "historic_fire_events": events,
            "claim": (None if not firing else
                      ("this fund has tracked its %s mandate (%s) at least %.2f below its own "
                       "rolling-R2 median of %.3f for %d consecutive semi-annual observations"
                       % (basis, mandate, D, v.get("r2_median") or 0.0, streak))),
        })

    # ── R15.2: the trigger watches ITSELF ─────────────────────────────────────────────────────
    rate = (100.0 * fired_obs / total_obs) if total_obs else None
    lo, hi = T4_FIRE_RATE_BAND_PCT
    if total_obs < T4_MIN_SEMI_ANNUAL_OBS:
        band = {"state": "REFUSED",
                "reason": ("%d semi-annual observations is below T4_MIN_SEMI_ANNUAL_OBS=%d — a "
                           "rate computed here would be noise wearing a percentage sign"
                           % (total_obs, T4_MIN_SEMI_ANNUAL_OBS))}
    elif rate is None:
        band = {"state": "REFUSED", "reason": "no observations"}
    else:
        band = {"state": ("OK" if lo <= rate <= hi else
                          ("BREACH_HIGH" if rate > hi else "BREACH_LOW")),
                "fire_rate_pct": round(rate, 2), "band_pct": [lo, hi]}
        if band["state"] != "OK":
            band["raise_item"] = (
                "R15.2: T4's fire rate is %.2f%%, outside its declared %g-%g%% band. RAISE a "
                "register item against ISA-0165 rather than moving T4_DROP — a threshold that is "
                "retuned every time it leaves its band is a description of the sleeve, not a "
                "trigger on it. %s" % (rate, lo, hi,
                    "Firing too often: the rule is describing normal behaviour." if rate > hi
                    else "Firing too rarely: the rule can no longer detect the thing it exists for."))
    return {
        "state": "MEASURED",
        "drop_threshold": D,
        "sustain_observations": T4_SUSTAIN_OBS,
        "rolling_window_m": T4_ROLL_WINDOW_M,
        "basis": ("D chosen from the FIRE RATE, not a cross-sectional gap: 0.20 sits just inside "
                  "the pooled P05 of deviations and fires on ~6% of semi-annual observations "
                  "(ISA_Study_FundRotation_Concentration_T4_15Aug2026.md §3)"),
        "funds": rows,
        "firing": [r["sedol"] for r in rows if r["state"] == "FIRING"],
        "watch": [r["sedol"] for r in rows if r["state"] == "WATCH"],
        "refused": [r["sedol"] for r in rows if r["state"] == "REFUSED"],
        "n_tested": sum(1 for r in rows if r["state"] != "REFUSED"),
        "fire_events": fired_obs,
        "semi_annual_observations": total_obs,
        "fire_rate_band": band,
        "acts_on_nothing": ("T4 RAISES A FLAG. It does not sell, trim or replace a fund — a "
                            "mandate breach is a reason to look, and the look is Step 6.6. "
                            "⚑ AMENDED 20-Aug-2026 (ISA-0386): it now also DEMOTES a firing fund "
                            "in the capital ordering (capital_destination C3) — a fund that has "
                            "stopped doing what it was bought for is not a destination for NEW "
                            "capital. Withholding new money is not selling, trimming or "
                            "replacing, and the three verbs above are unchanged; this sentence is "
                            "updated because a build that makes a spec sentence incomplete must "
                            "update that sentence (R4.4)."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════

def run(write=False):
    import fund_learning as FL
    built = FL.build_panel()
    panel = built["panel"]
    keys = common_window(panel)
    out = {
        "schema_version": STUDY_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": dt.date.today().isoformat(),
        "answers": ["ISA-0153", "ISA-0154", "ISA-0165", "ISA-0329"],
        "panel": {"n_funds": built["n_funds"], "n_refused": built["n_refused"],
                  "common_window_months": len(keys)},
        "pre_registered_grid": {
            "top_n": list(ROTATE_TOP_N), "lookback_m": list(ROTATE_LOOKBACK_M),
            "cadence_m": list(ROTATE_CADENCE_M), "random_controls": N_RANDOM_CONTROLS,
            "t4_drops": list(T4_CANDIDATE_DROPS), "seed": RANDOM_SEED,
            "declared": "fixed in code before the study was run (R3.1); every cell reported"},
        "concentration": concentration(panel, keys),
        "rotation_grid": rotation_grid(panel, keys),
        "t4_calibration": t4_calibration(panel),
    }
    out["t4_trigger"] = t4_mandate_drift(calibration=out["t4_calibration"])   # ISA-0165
    if write:
        (HERE / OUT_NAME).write_text(json.dumps(out, indent=1, sort_keys=True, default=str),
                                     encoding="utf-8")
        out["_written"] = OUT_NAME
    return out


def report(o=None):
    o = o or run()
    c, g, t = o["concentration"], o["rotation_grid"], o["t4_calibration"]
    L = [f"FUND ROTATION / CONCENTRATION / MANDATE-DRIFT STUDY  as_of {o['as_of']}",
         f"panel {o['panel']['n_funds']} funds, common window {o['panel']['common_window_months']}m "
         f"{g['window'][0]}..{g['window'][1]}", ""]
    L += ["ISA-0329 CONCENTRATION",
          f"   effective independent bets {c['effective_independent_bets']} of {c['n_funds']} "
          f"({c['effective_as_pct_of_nominal']}% of nominal)",
          f"   mean pairwise active correlation {c['mean_pairwise_active_correlation']}   "
          f"top eigenvalue share {c['eigenvalue_top_share_pct']}%",
          f"   declared process keys: {c['source_concentration']['declared_process_keys']} "
          f"({c['source_concentration']['status']})", "   most-correlated pairs:"]
    for p in c["top_pairwise_active_correlations"][:5]:
        L.append(f"      {p['a']:<9} {p['b']:<9} {p['corr']:+.3f}")
    L += ["", "ISA-0153/0154 ROTATION GRID",
          f"   hold everything: {g['hold_all_ann_pct']}% p.a.",
          f"   cells beating hold-all: {g['cells_beating_hold_all']}/{g['cells_total']}   "
          f"beating a random control at P95: {g['cells_beating_random_control_at_95']}/{g['cells_total']}",
          "   topN lb cad   rot%     hold%    excess   t      vs-random-pctile"]
    for cell in g["cells"]:
        L.append("   %4d %3d %3d  %7.2f %8.2f %8.2f  %6s %10s" % (
            cell["top_n"], cell["lookback_m"], cell["cadence_m"],
            cell["rotation_ann_pct"] or 0, cell["hold_all_ann_pct"] or 0,
            cell["excess_ann_pp"] or 0, cell["excess_t"], cell["beats_random_pctile"]))
    L += ["", "ISA-0165 T4 CALIBRATION  (rolling %dm R2 vs the fund's OWN median)" % t["rolling_window_m"],
          f"   pooled deviation distribution: p50 {t['pooled_deviation_distribution']['p50']}  "
          f"p10 {t['pooled_deviation_distribution']['p10']}  "
          f"p05 {t['pooled_deviation_distribution']['p05']}  "
          f"min {t['pooled_deviation_distribution']['min']}",
          "   drop   funds fired   fire events / obs   fire rate"]
    for row in t["threshold_grid"]:
        L.append("   %.2f   %-12d %5d / %-5d      %s%%" % (
            row["drop_threshold"], row["n_funds_fired"], row["fire_events"],
            row["semi_annual_observations"], row["fire_rate_pct"]))
    return "\n".join(L)


def selftest(verbose=True) -> int:
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    ok(abs(_ann([0.0] * 12) - 0.0) < 1e-9, "a flat year annualises to zero")
    ok(abs(_ann([0.01] * 12) - 12.6825) < 1e-3, "1%/month must compound to 12.68% p.a.")
    ok(_pctile([1, 2, 3, 4, 5], 0.5) == 3, "median of 1..5 is 3")

    # eigenvalues of a known 2x2 correlation matrix
    ev = _eigenvalues_sym([[1.0, 0.5], [0.5, 1.0]])
    ok(abs(ev[0] - 1.5) < 1e-6 and abs(ev[1] - 0.5) < 1e-6, f"eigenvalues wrong: {ev}")

    # effective-N endpoints: identical funds -> 1 bet; orthogonal funds -> N bets
    keys = [(2020, (i % 12) + 1) for i in range(60)]
    keys = [(2020 + i // 12, (i % 12) + 1) for i in range(60)]
    import random
    rng = random.Random(1)
    base = [rng.gauss(0, 0.03) for _ in keys]
    same = {f"F{i}": {"fund": {k: base[j] for j, k in enumerate(keys)},
                      "bench": {k: 0.0 for k in keys}} for i in range(4)}
    c1 = concentration(same, keys)
    ok(abs(c1["effective_independent_bets"] - 1.0) < 0.05,
       f"four identical funds must be ~1 independent bet, got {c1['effective_independent_bets']}")
    indep = {f"F{i}": {"fund": {k: rng.gauss(0, 0.03) for k in keys},
                       "bench": {k: 0.0 for k in keys}} for i in range(4)}
    c2 = concentration(indep, keys)
    ok(c2["effective_independent_bets"] > 3.0,
       f"four independent funds must be ~4 bets, got {c2['effective_independent_bets']}")
    ok(c2["source_concentration"]["status"] == "UNMEASURED",
       "with no declared process keys, SOURCE concentration must report UNMEASURED, never clean")

    # rotation: a panel with ONE genuinely persistent winner must be detectable, or the test is blind
    win = {}
    for i in range(4):
        edge = 0.006 if i == 0 else 0.0
        win[f"F{i}"] = {"fund": {k: edge + rng.gauss(0, 0.02) for k in keys},
                        "bench": {k: 0.0 for k in keys}}
    g = rotation_grid(win, keys, seed=3)
    ok(g["cells_beating_hold_all"] > 0,
       "a panel containing a persistent winner must produce at least one winning cell — "
       "otherwise the study cannot detect an edge that exists and its null is meaningless")
    ok(all(c["random_control_median_ann_pct"] is not None for c in g["cells"]),
       "every cell must carry its random-selection control (R3.8)")
    ok(g["cells_total"] > 0 and all(c["excess_t"] is not None for c in g["cells"]),
       "every cell reports its t — reported, never used to zero anything (R3.4)")

    # common_window intersects, it does not union
    p = {"A": {"fund": {(2020, 1): 0.0, (2020, 2): 0.0}, "bench": {(2020, 1): 0.0, (2020, 2): 0.0}},
         "B": {"fund": {(2020, 2): 0.0}, "bench": {(2020, 2): 0.0}}}
    ok(common_window(p) == [(2020, 2)],
       "the common window must be the INTERSECTION — a union lets a fund enter only when it "
       "happened to exist, which is survivorship inside the test (R3.3)")

    # ── ISA-0165 · THE OPERATIVE T4 TRIGGER ───────────────────────────────────────────────────
    # A synthetic calibration, so the trigger's LOGIC is tested independently of this month's data.
    def _cal(obs_by_fund):
        return {"per_fund": {sd: {"semi_annual_obs": o, "r2_median": 0.8,
                                  "current_deviation": o[-1], "worst_deviation": min(o)}
                             for sd, o in obs_by_fund.items()}}

    # A synthetic universe, because a fund with no testable mandate is correctly REFUSED and the
    # logic below is about the trigger's arithmetic, not about the mandate lookup.
    def _uni(obs_by_fund):
        return {sd: {"mandate_benchmark": {
                    "declared": True, "index_name": "TEST_INDEX", "source_doc": "selftest",
                    "as_of": "2026-08-19", "accessibility": "n/a",
                    "comparator": {"ticker": "TEST"}}} for sd in obs_by_fund}

    def _t4(obs, **kw):
        return t4_mandate_drift(calibration=_cal(obs), universe=_uni(obs), **kw)

    # 12 clean observations per fund so the band check is not REFUSED for want of observations
    clean = [0.0] * 12
    t = _t4({f"F{i}": list(clean) for i in range(5)})
    ok(t["state"] == "MEASURED" and not t["firing"],
       "NEGATIVE CONTROL: a sleeve tracking its mandates perfectly fires on nobody")
    ok(t["fire_rate_band"]["state"] == "BREACH_LOW" and "raise_item" in t["fire_rate_band"],
       "R15.2: a trigger that NEVER fires is out of band too, and says to raise an item rather "
       "than to loosen the threshold — a rule that cannot fire is a decoration")

    one_below = list(clean); one_below[-1] = -0.30
    t1 = _t4({"F0": one_below})
    ok(t1["funds"][0]["state"] == "WATCH" and not t1["firing"],
       "ONE observation below the drop is WATCH, never FIRING — the SUSTAIN requirement is what "
       "separates a mandate breach from a single noisy window")

    two_below = list(clean); two_below[-1] = two_below[-2] = -0.30
    t2 = _t4({"F0": two_below})
    ok(t2["funds"][0]["state"] == "FIRING" and t2["funds"][0]["consecutive_obs_below"] == 2,
       "TWO consecutive observations below the drop FIRES")

    just_above = list(clean); just_above[-1] = just_above[-2] = -(T4_DROP - 0.001)
    ok(_t4({"F0": just_above})["funds"][0]["state"] != "FIRING",
       "the threshold is a real edge: 0.001 inside it does not fire")

    ok(_t4({"F0": [-0.30] * 12, "F1": [-0.30] * 12, "F2": [-0.30] * 12,
            "F3": [-0.30] * 12, "F4": [-0.30] * 12})["fire_rate_band"]["state"] == "BREACH_HIGH",
       "R15.2: a trigger firing on everything is out of band on the HIGH side — a trigger that "
       "fires on the whole sleeve is a description of it")

    ok(t4_mandate_drift(calibration={"per_fund": {"F0": {"refused": "too short"}}})["refused"]
       == ["F0"],
       "R2.10: a fund with no rolling-R2 series is REFUSED and named — 'not tested' and 'tested "
       "and fine' must never render the same")

    ok(all(r.get("mandate_basis") in (None, "prospectus", "investor")
           for r in t2["funds"]),
       "every tested row carries the MANDATE BASIS — 'left the benchmark it published' and 'no "
       "longer the exposure Raj bought' are different claims and the alert must say which")

    _saved = globals()["T4_ENABLED"]
    try:
        globals()["T4_ENABLED"] = False
        ok(_t4({"F0": two_below})["state"] == "DISABLED",
           "R4.13: T4_ENABLED=False disables the trigger with one constant and no code revert")
    finally:
        globals()["T4_ENABLED"] = _saved

    if verbose:
        print(f"fund_rotation_analysis selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fund rotation / concentration / T4 study")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.run or a.write:
        o = run(write=a.write)
        print(report(o))
        if a.write:
            print(f"\nwrote {o.get('_written')}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
