#!/usr/bin/env python3
"""FUND LEARNING BATTERY — L-1, L-2, L-3, L-5, L-6, L-7, L-8 as periodic EMITTERS.

Register items: ISA-0184 (L-1) · ISA-0185 (L-2) · ISA-0186 (L-3) · ISA-0188 (L-5) ·
ISA-0189 (L-6) · ISA-0190 (L-7) · ISA-0191 (L-8).  L-4 (ISA-0187) is `disagreement_log.py`,
because a disagreement log scoped to funds would be the wrong shape for a framework-wide check.

WHY THIS EXISTS
---------------
The 09-Aug-2026 fund session produced eight findings and wrote them into a markdown file. R8.2:
"a learning task that cannot prove it ran is not a learning task." A finding in prose sharpens
never; an emitter sharpens every time the panel grows. Each function here answers ONE of those
findings, writes its answer into a dated store, and carries an assertion that proves it ran.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not re-derive NAV series, benchmark returns or regressions. `fund_performance`,
`benchmark_registry` and `beta_alpha_study` already own those and a second copy would be a defect
on the day it was created (R4.5). This module CONSUMES them.

It does not rank or recommend. Every output here is evidence for a later decision, and several
outputs say the honest thing is that the evidence cannot support one — 12 funds is n=12, and a
rank correlation on 12 names has a standard error of roughly 0.30 whatever it prints (R3.2).

CLI:
  python3 fund_learning.py --emit --write     # run every emitter, write the dated store
  python3 fund_learning.py --controls         # L-7 only; exit 1 if a standing control fails
  python3 fund_learning.py --report
  python3 fund_learning.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LEARNING_VERSION = "1.0.0"

# ── DECLARED constants. Each is a judgement and is named as one (R14.4), with what would move it.
ROLLING_WINDOW_M = 36        # L-5: the shortest window the sleeve's own window_sweep reports
ROLLING_STEP_M = 1
MIN_HALF_MONTHS = 24         # L-1: a half shorter than this cannot rank anything
CONTROL_ALPHA_TOL_PP = 0.50  # L-7: |alpha - (-OCF)| tolerance for an index tracker, pp per annum
CONTROL_BETA_TOL = 0.10      # L-7: |beta - 1.0| tolerance for an index tracker
CONTROL_R2_MIN = 0.85        # L-7: an index tracker below this is not tracking what we think
PROXY_CORR_MIN = 0.99        # L-3: monthly return correlation floor for a share-class proxy
PROXY_ANN_DIFF_MAX_PP = 0.75  # L-3: annualised difference ceiling over the overlap window


def _study_path(base=None):
    base = Path(base or HERE)
    cands = sorted(base.glob("beta_alpha_study_*.json"))
    if not cands:
        raise FileNotFoundError(
            "no beta_alpha_study_*.json on disk. This module CONSUMES that study and does not "
            "recompute it (R4.5) — run beta_alpha_study.py first.")
    return cands[-1]


def load_study(path=None):
    p = Path(path) if path else _study_path()
    d = json.loads(p.read_text(encoding="utf-8"))
    # R5.1 — assert the contract at the boundary, as it is read.
    for k in ("funds", "controls", "coverage", "method"):
        if k not in d:
            raise ValueError(f"{p.name} is missing `{k}` — the study contract has changed and this "
                             f"module must be updated with it, not silently degraded (R4.7)")
    d["_source_file"] = p.name
    return d


def store_name(as_of=None):
    d = as_of or dt.date.today()
    return f"fund_learning_{d.strftime('%b').lower()}_{d.year}.json"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# panel — monthly GBP returns per fund and its comparator, via the DECLARED routes only
# ══════════════════════════════════════════════════════════════════════════════════════════════

def build_panel(universe=None):
    """{sedol: {"fund": {(y,m): r}, "bench": {(y,m): r}, "meta": {...}}}

    Every series comes from the module that owns it. `refused` is a first-class outcome and is
    counted, never dropped (R4.9).
    """
    import fund_performance as fp
    import fund_action_stack as fas
    import benchmark_registry as breg

    U = universe if universe is not None else fp.load_universe()
    panel, refused = {}, []
    for sd, u in U.items():
        if str(sd).startswith("_") or not isinstance(u, dict):
            continue
        try:
            nav = fp.nav_series_for(sd, u)
            fr = dict(fas._monthly_returns(nav)) if nav else {}
        except Exception as exc:                                  # noqa: BLE001
            refused.append({"sedol": sd, "stage": "nav", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not fr:
            refused.append({"sedol": sd, "stage": "nav", "reason": "no monthly returns from NAV"})
            continue
        try:
            bt, _comp = breg.comparator_for(sd, U)
            br = dict(breg.gbp_returns(bt))
        except Exception as exc:                                  # noqa: BLE001
            refused.append({"sedol": sd, "stage": "benchmark",
                            "reason": f"{type(exc).__name__}: {exc}"})
            continue
        panel[sd] = {"fund": fr, "bench": br,
                     "meta": {"name": u.get("name"), "bucket": u.get("bucket"),
                              "ocf": u.get("ocf"), "benchmark_ticker": bt,
                              "inception": u.get("inception"),
                              "inception_basis": u.get("inception_basis"),
                              "series_start": min(fr) if fr else None,
                              "series_end": max(fr) if fr else None,
                              "n_months": len(fr)}}
    return {"panel": panel, "refused": refused,
            "n_funds": len(panel), "n_refused": len(refused)}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# statistics — small, explicit, and honest about n
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a, b):
    """(rho, n, se, ci95). SE is the large-sample 1/sqrt(n-1) — reported because on n=12 it is
    ~0.30 and a rho of 0.4 and a rho of 0.0 are the same statement (R3.2, R3.4)."""
    n = len(a)
    if n < 3 or len(b) != n:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if da == 0 or db == 0:
        return {"rho": None, "n": n, "se": None, "ci95": None,
                "refused": "a measure with no cross-sectional spread cannot be ranked"}
    rho = num / (da * db)
    se = 1.0 / math.sqrt(n - 1)
    return {"rho": round(rho, 4), "n": n, "se": round(se, 4),
            "ci95": [round(max(-1.0, rho - 1.96 * se), 4), round(min(1.0, rho + 1.96 * se), 4)]}


def pearson(a, b):
    n = len(a)
    if n < 3 or len(b) != n:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return round(num / (da * db), 6) if da and db else None


def _ann(monthly_rets):
    if not monthly_rets:
        return None
    g = 1.0
    for r in monthly_rets:
        g *= (1.0 + r)
    return round((g ** (12.0 / len(monthly_rets)) - 1.0) * 100.0, 4)


def regress(y, x):
    """alpha (monthly), beta, r2, n — OLS. Deliberately NOT re-implementing beta_alpha_study's
    Newey-West path: this is used only for ROLLING DRIFT, where the quantity of interest is the
    movement of the point estimate and HAC errors do not change it."""
    n = len(y)
    if n < 12 or len(x) != n:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx == 0:
        return None
    beta = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    alpha = my - beta * mx
    ss_res = sum((y[i] - (alpha + beta * x[i])) ** 2 for i in range(n))
    ss_tot = sum((v - my) ** 2 for v in y)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else None
    return {"alpha_m": alpha, "beta": beta, "r2": r2, "n": n}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-1 (ISA-0184) — rank persistence of every fund performance measure
# ══════════════════════════════════════════════════════════════════════════════════════════════

MEASURES = ("total_return", "alpha", "beta", "r_squared", "up_capture", "down_capture",
            "info_ratio")


def _half_measures(fr, br, keys):
    """Every measure computed on ONE window, so H1 and H2 are computed by identical code."""
    y = [fr[k] for k in keys]
    x = [br[k] for k in keys]
    reg = regress(y, x)
    up = [(y[i], x[i]) for i in range(len(keys)) if x[i] > 0]
    dn = [(y[i], x[i]) for i in range(len(keys)) if x[i] <= 0]
    # ⚑ ISA-0350 (15-Aug-2026, found by verifying the output before writing about it, R2.1).
    # This first computed the information ratio as mean(residual)/sd(residual). OLS forces the
    # residual mean to ZERO on its own fitting window, so the ratio was 0.0 for all 11 funds in
    # both halves — a perfectly plausible number produced by a structural impossibility, which is
    # FC-A exactly. rank_persistence then REFUSED the measure for "no cross-sectional spread",
    # so the refusal was correct and the quantity was wrong: had the refusal not existed, a
    # column of zeroes would have been published as a finding about the funds.
    # The correct quantity is the APPRAISAL RATIO — the intercept over residual risk — which is
    # what "information ratio" means against a BETA-ADJUSTED benchmark. Both terms are
    # annualised: alpha is already per-month from the fit, residual sd scales by sqrt(12).
    n = len(keys)
    resid = [y[i] - ((reg["alpha_m"] + reg["beta"] * x[i]) if reg else 0.0)
             for i in range(n)]
    mr = sum(resid) / n
    sd = math.sqrt(sum((v - mr) ** 2 for v in resid) / (n - 1)) if n > 1 else 0.0
    resid_sd_ann = sd * math.sqrt(12.0)
    alpha_ann = (reg["alpha_m"] * 12.0) if reg else None
    return {
        "total_return": _ann(y),
        "alpha": round(reg["alpha_m"] * 12 * 100, 4) if reg else None,
        "beta": round(reg["beta"], 4) if reg else None,
        "r_squared": round(reg["r2"], 4) if reg and reg["r2"] is not None else None,
        "up_capture": round(100 * (sum(v for v, _ in up) / sum(v for _, v in up)), 2)
                      if up and sum(v for _, v in up) else None,
        "down_capture": round(100 * (sum(v for v, _ in dn) / sum(v for _, v in dn)), 2)
                        if dn and sum(v for _, v in dn) else None,
        "info_ratio": (round(alpha_ann / resid_sd_ann, 4)
                       if (alpha_ann is not None and resid_sd_ann) else None),
        "resid_sd_ann_pct": round(resid_sd_ann * 100, 4) if resid_sd_ann else None,
        "n_months": n,
    }


def rank_persistence(panel):
    """L-1. Split each fund's OWN overlap into two equal, NON-overlapping halves, measure both,
    and rank-correlate H1 against H2 across funds.

    ⚑ Two things this does not do. It does not use overlapping windows (R3.2 — 17 overlapping
    half-years is not 17 observations). It does not report a rho without its interval, because
    on this sleeve every interval spans zero and the interval IS the finding.
    """
    per_fund, skipped = {}, []
    for sd, p in panel.items():
        keys = sorted(set(p["fund"]) & set(p["bench"]))
        if len(keys) < 2 * MIN_HALF_MONTHS:
            skipped.append({"sedol": sd, "n_overlap": len(keys),
                            "reason": f"needs {2 * MIN_HALF_MONTHS} overlapping months for two "
                                      f"independent halves"})
            continue
        mid = len(keys) // 2
        per_fund[sd] = {
            "first_half": _half_measures(p["fund"], p["bench"], keys[:mid]),
            "second_half": _half_measures(p["fund"], p["bench"], keys[mid:]),
            "window": [f"{keys[0][0]}-{keys[0][1]:02d}", f"{keys[-1][0]}-{keys[-1][1]:02d}"],
            "split_at": f"{keys[mid][0]}-{keys[mid][1]:02d}",
        }
    out = {}
    for m in MEASURES:
        pairs = [(v["first_half"][m], v["second_half"][m]) for v in per_fund.values()
                 if v["first_half"][m] is not None and v["second_half"][m] is not None]
        if len(pairs) < 3:
            out[m] = {"refused": f"only {len(pairs)} funds have this measure in both halves"}
            continue
        s = spearman([a for a, _ in pairs], [b for _, b in pairs])
        s["persistent"] = bool(s.get("ci95") and s["ci95"][0] > 0)
        out[m] = s
    persistent = [m for m, v in out.items() if v.get("persistent")]
    return {
        "per_fund": per_fund,
        "skipped": skipped,
        "rank_ic": out,
        "measures_with_evidence_of_persistence": persistent,
        "read_this_first": (
            f"{len(per_fund)} funds. A Spearman rho on n={len(per_fund)} has a standard error of "
            f"about {1 / math.sqrt(max(len(per_fund) - 1, 1)):.2f}, so any rho whose 95% interval "
            f"spans zero is not evidence of persistence in either direction — it is evidence that "
            f"this sleeve is too small to answer the question. Reporting the point estimate alone "
            f"would let a rho of 0.4 be read as a finding (R3.4). "
            + (f"Measures clearing the bar: {', '.join(persistent)}." if persistent else
               "NO measure clears the bar, which is the same null the 09-Aug study found — now "
               "recomputed, dated, stored and repeatable rather than a sentence in a markdown "
               "file.")),
        "pre_registered_revisit": (
            "Re-run every 6 months. The estimate sharpens only as the PANEL LENGTHENS, not as the "
            "fund count grows, because the fund count is fixed at 12 — so the honest expectation "
            "is that this stays inconclusive for years, and the value of the emitter is that it "
            "says so with a number instead of leaving the question open."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-2 (ISA-0185) — regression-window audit against declared inception
# ══════════════════════════════════════════════════════════════════════════════════════════════

def window_audit(panel, study):
    """L-2. R3.10: every regression declares its window against DECLARED share-class inception
    and flags where more history exists.

    Three quantities, kept apart because collapsing them is exactly the defect this catches:
      regression_months  — what beta_alpha_study actually used
      series_months      — what the NAV series on disk actually holds
      declared_inception — what the fund SAYS, which for most of this sleeve is nothing
    """
    rows, no_inception, unused_history = [], [], []
    for sd, p in panel.items():
        m = p["meta"]
        st = (study.get("funds") or {}).get(sd) or {}
        reg_n = ((st.get("single_factor") or {}).get("n_months")
                 or st.get("n_months"))
        ser_n = m["n_months"]
        inc = m.get("inception")
        row = {
            "sedol": sd, "name": m["name"],
            "regression_months": reg_n,
            "series_months": ser_n,
            "series_start": (f"{m['series_start'][0]}-{m['series_start'][1]:02d}"
                             if m.get("series_start") else None),
            "declared_inception": inc,
            "inception_basis": m.get("inception_basis"),
            "unused_months": (ser_n - reg_n) if (reg_n and ser_n) else None,
        }
        if not inc:
            row["inception_status"] = "NOT_DECLARED"
            no_inception.append(sd)
        else:
            row["inception_status"] = "declared"
        if row["unused_months"] and row["unused_months"] > 6:
            unused_history.append(row)
        rows.append(row)
    rows.sort(key=lambda r: -(r["unused_months"] or 0))
    return {
        "rows": rows,
        "funds_with_no_declared_inception": sorted(no_inception),
        "funds_with_unused_history": [r["sedol"] for r in unused_history],
        "verdict": (
            f"{len(no_inception)} of {len(rows)} funds have NO declared share-class inception. "
            f"R3.10 requires every regression to declare its window against declared inception, "
            f"and for those funds that rule is UNSATISFIABLE today — the framework can only "
            f"compare its window against the observed series start, and an observed start and an "
            f"inception are different facts (a short fetch looks identical to a young fund). "
            f"{len(unused_history)} fund(s) have more than six months of NAV history on disk that "
            f"the regression did not use."),
        "what_would_resolve_it": (
            "One date per fund from its KID or factsheet, written to fund_universe.json "
            "`inception` with `inception_basis: share_class` and a source. It closes permanently: "
            "an inception date does not change."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-3 (ISA-0186) — share-class proxy validation, as a reusable capability
# ══════════════════════════════════════════════════════════════════════════════════════════════

def validate_proxy(target_rets, proxy_rets, label=""):
    """L-3. Is `proxy` an admissible stand-in for `target` over their overlap?

    The Ranmore one-off used exactly this pair of tests (+0.12pp annualised, correlation 1.0000);
    this makes it a function rather than a paragraph. BOTH tests must pass — a high correlation
    with a persistent level difference is a different share class, and a matching level with a
    low correlation is a coincidence (R6.2: two tests, both reported, never blended).
    """
    keys = sorted(set(target_rets) & set(proxy_rets))
    if len(keys) < 12:
        return {"admissible": False, "n_overlap": len(keys),
                "refused": "fewer than 12 overlapping months — not enough to validate a proxy"}
    t = [target_rets[k] for k in keys]
    p = [proxy_rets[k] for k in keys]
    at, ap = _ann(t), _ann(p)
    corr = pearson(t, p)
    diff = round(at - ap, 4)
    ok_corr = corr is not None and corr >= PROXY_CORR_MIN
    ok_diff = abs(diff) <= PROXY_ANN_DIFF_MAX_PP
    return {
        "label": label, "n_overlap": len(keys),
        "overlap": [f"{keys[0][0]}-{keys[0][1]:02d}", f"{keys[-1][0]}-{keys[-1][1]:02d}"],
        "target_ann_pct": at, "proxy_ann_pct": ap,
        "ann_diff_pp": diff, "monthly_corr": corr,
        "corr_test": {"pass": ok_corr, "floor": PROXY_CORR_MIN},
        "diff_test": {"pass": ok_diff, "ceiling_pp": PROXY_ANN_DIFF_MAX_PP},
        "admissible": bool(ok_corr and ok_diff),
        "basis": ("both tests must pass: correlation alone cannot detect a fee difference, and a "
                  "matching annualised return alone cannot detect a different portfolio"),
    }


def proxy_report(panel):
    """Runs L-3 over every DECLARED proxy relationship. There are none declared today, and that
    is reported as such rather than as a clean pass (R2.10)."""
    declared = []          # populated from fund_universe when a proxy is declared there
    for sd, p in panel.items():
        px = (p["meta"] or {}).get("proxy_for")
        if px:
            declared.append((sd, px))
    if not declared:
        return {"declared_proxies": 0,
                "status": "NO_DECLARED_PROXY_RELATIONSHIPS",
                "note": ("validate_proxy() is available and tested, but no fund in "
                         "fund_universe.json declares another as its proxy, so there is nothing "
                         "to validate this run. This is NOT a pass — it is an empty set, and the "
                         "two must never render the same. The Ranmore case that motivated L-3 was "
                         "handled outside the universe file; declaring it there makes it a "
                         "standing check instead of a memory."),
                "results": []}
    out = []
    for sd, px in declared:
        if px in panel:
            out.append(validate_proxy(panel[sd]["fund"], panel[px]["fund"], f"{sd}<-{px}"))
    return {"declared_proxies": len(declared), "status": "CHECKED", "results": out}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-5 (ISA-0188) — rolling R² / beta drift series, STORED
# ══════════════════════════════════════════════════════════════════════════════════════════════

def rolling_drift(panel, window=ROLLING_WINDOW_M, step=ROLLING_STEP_M):
    """L-5. The point is STORAGE. O-7's rotation trigger needs a threshold on beta/R2 drift and
    today that threshold is DECLARED because nobody has the distribution. This writes the
    distribution down every run so that in a year it can be calibrated instead."""
    out = {}
    for sd, p in panel.items():
        keys = sorted(set(p["fund"]) & set(p["bench"]))
        if len(keys) < window + 6:
            out[sd] = {"refused": f"{len(keys)} overlapping months; needs {window + 6}"}
            continue
        series = []
        for i in range(0, len(keys) - window + 1, step):
            w = keys[i:i + window]
            r = regress([p["fund"][k] for k in w], [p["bench"][k] for k in w])
            if not r:
                continue
            series.append({"end": f"{w[-1][0]}-{w[-1][1]:02d}",
                           "beta": round(r["beta"], 4),
                           "r2": round(r["r2"], 4) if r["r2"] is not None else None,
                           "alpha_ann_pct": round(r["alpha_m"] * 12 * 100, 3)})
        if not series:
            out[sd] = {"refused": "no window produced a regression"}
            continue
        betas = [s["beta"] for s in series]
        r2s = [s["r2"] for s in series if s["r2"] is not None]
        out[sd] = {
            "window_months": window, "n_windows": len(series),
            "beta": {"first": betas[0], "last": betas[-1], "min": min(betas), "max": max(betas),
                     "range": round(max(betas) - min(betas), 4)},
            "r2": ({"first": r2s[0], "last": r2s[-1], "min": min(r2s), "max": max(r2s),
                    "range": round(max(r2s) - min(r2s), 4)} if r2s else None),
            "series": series,
        }
    ranked = sorted(((sd, v["beta"]["range"]) for sd, v in out.items() if "beta" in v),
                    key=lambda t: -t[1])
    return {
        "per_fund": out,
        "widest_beta_drift": ranked[:5],
        "read_this_first": (
            f"Rolling {window}-month windows stepped monthly. These windows OVERLAP by "
            f"{window - step} months, so the series is a smoothed picture of one history and NOT "
            f"{len(ranked)} independent observations — it may be read for shape and range, never "
            f"used as a sample size (R3.2). Stored so O-7's drift threshold can eventually be "
            f"calibrated against a distribution rather than declared against a guess."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-6 (ISA-0189) — pre-registered alpha-persistence tests
# ══════════════════════════════════════════════════════════════════════════════════════════════

def alpha_persistence_registrations(study):
    """L-6. R3.1: hypothesis, test, threshold and success criterion recorded BEFORE the data that
    would settle it exists. Two named cases, registered now so neither can be rescued post-hoc."""
    funds = study.get("funds") or {}

    def half(sd, which):
        return ((funds.get(sd) or {}).get("split_sample") or {}).get(which) or {}

    regs = []
    for sd, label, direction in (
            ("BR2Q8G6", "Ranmore Global Equity Institutional", "second-half alpha must repeat"),
            ("B2PLJM6", "Artemis SmartGARP UK Eq I Acc", "rising alpha must not be a window artefact")):
        f = funds.get(sd) or {}
        h1, h2 = half(sd, "first_half"), half(sd, "second_half")
        regs.append({
            "id": f"FL-ALPHA-PERSIST-{sd}",
            "fund": sd, "name": f.get("name") or label,
            "registered_on": dt.date.today().isoformat(),
            "hypothesis": (
                f"The alpha measured on the SECOND half of {f.get('name') or label}'s window "
                f"({h2.get('alpha_ann_pct')}% ann) is a property of the manager's process and "
                f"will repeat out of sample, rather than being the window it was measured in. "
                f"First half: {h1.get('alpha_ann_pct')}% ann."),
            "why_it_matters": direction,
            "test": ("Re-run beta_alpha_study on the SAME benchmark and re-measure alpha over the "
                     "months added since this registration date, as a standalone window. That "
                     "window is out of sample by construction because it did not exist today."),
            "threshold": ("PASS if the out-of-sample alpha point estimate is positive AND its 95% "
                          "interval excludes zero. INCONCLUSIVE if positive with an interval "
                          "spanning zero. FAIL if the point estimate is negative."),
            "minimum_window": "24 months of new data, i.e. not before "
                              f"{(dt.date.today() + dt.timedelta(days=730)).isoformat()}",
            "success_criterion": ("The registration is settled by the FIRST evaluation at or after "
                                  "the minimum window. No second look, no window extension, no "
                                  "benchmark change — any of those makes it a new registration "
                                  "with a new date (R3.1: no post-hoc rescue)."),
            "negative_control": ("VUAG must simultaneously show alpha within "
                                 f"{CONTROL_ALPHA_TOL_PP}pp of -OCF. If the control drifts, the "
                                 "measurement changed and the test is void rather than failed."),
            "prior_full_window": {
                "alpha_ann_pct": (f.get("single_factor") or {}).get("alpha_ann_pct"),
                "alpha_t": (f.get("single_factor") or {}).get("alpha_t"),
                "n_months": (f.get("single_factor") or {}).get("n_months"),
                "window_sweep_sign_stable": (f.get("window_sweep") or {}).get("sign_stable"),
                "window_sweep_range_pp": (f.get("window_sweep") or {}).get("alpha_range_pp"),
            },
        })
    return {"registrations": regs,
            "note": ("Registered here and mirrored into calibration_registry.json by --write. "
                     "R3.1 is satisfied by the DATE, not by the file: what makes these honest is "
                     "that the data which will settle them does not exist yet.")}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-7 (ISA-0190) — standing benchmark controls, ASSERTED
# ══════════════════════════════════════════════════════════════════════════════════════════════

def benchmark_controls(study):
    """L-7. An index tracker must show beta ~1.0, alpha ~ -OCF and a high R2. If it does not, the
    BENCHMARK MAPPING is wrong — which is how XLKQ.L, labelled 'MSCI World Info Tech', was found
    to be a US-only S&P sector ETF. These are the cheapest assertions in the framework and they
    police every other alpha in the study, because they share its measurement path."""
    breaches, checked = [], []
    for sd, c in (study.get("controls") or {}).items():
        f = (study.get("funds") or {}).get(sd) or {}
        ocf = c.get("ocf")
        beta, alpha, r2 = c.get("beta"), c.get("alpha_ann_pct"), c.get("r_squared")
        row = {"sedol": sd, "name": f.get("name"), "ocf": ocf, "beta": beta,
               "alpha_ann_pct": alpha, "r_squared": r2,
               "benchmark_ticker": f.get("benchmark_ticker")}
        if ocf is None or beta is None or alpha is None or r2 is None:
            row["verdict"] = "UNKNOWN"
            row["why"] = ("a control fed a null BLOCKS; it never passes (R4.3). Missing: "
                          + ", ".join(k for k, v in (("ocf", ocf), ("beta", beta),
                                                     ("alpha", alpha), ("r2", r2)) if v is None))
            breaches.append(row)
            checked.append(row)
            continue
        tests = {
            "beta_near_1": abs(beta - 1.0) <= CONTROL_BETA_TOL,
            "alpha_near_minus_ocf": abs(alpha - (-ocf)) <= CONTROL_ALPHA_TOL_PP,
            "r2_high": r2 >= CONTROL_R2_MIN,
        }
        row["tests"] = tests
        row["alpha_minus_expected_pp"] = round(alpha - (-ocf), 4)
        row["verdict"] = "PASS" if all(tests.values()) else "FAIL"
        if row["verdict"] == "FAIL":
            row["why"] = ("an index tracker failing these is evidence the COMPARATOR is wrong, "
                          "not that the manager added or destroyed value")
            breaches.append(row)
        checked.append(row)
    return {"checked": checked, "breaches": breaches, "n_controls": len(checked),
            "tolerances": {"beta": CONTROL_BETA_TOL, "alpha_pp": CONTROL_ALPHA_TOL_PP,
                           "r2_min": CONTROL_R2_MIN},
            "pass": not breaches}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L-8 (ISA-0191) — regime-conditional behaviour, queued for labelling
# ══════════════════════════════════════════════════════════════════════════════════════════════

def regime_label_queue(study):
    """L-8. Down-month alpha and capture ratios are either a persistent property or a window
    artefact, and only time separates them. This queues each candidate with the number, the
    sample it rests on, and what would falsify it — so the next run compares rather than
    rediscovers."""
    queue = []
    for sd, f in (study.get("funds") or {}).items():
        r = f.get("regime") or {}
        if not r:
            continue
        n_dn, n_up = r.get("n_down"), r.get("n_up")
        dn_a = (r.get("down") or {}).get("alpha_ann_pct")
        up_a = (r.get("up") or {}).get("alpha_ann_pct")
        dn_t = (r.get("down") or {}).get("alpha_t")
        item = {
            "sedol": sd, "name": f.get("name"),
            "n_up": n_up, "n_down": n_dn,
            "up_capture_pct": r.get("up_capture_pct"),
            "down_capture_pct": r.get("down_capture_pct"),
            "down_alpha_ann_pct": dn_a, "down_alpha_t": dn_t, "up_alpha_ann_pct": up_a,
            "beats_in_both": r.get("beats_in_both"),
            "both_significant": r.get("both_significant"),
        }
        flags = []
        if n_dn is not None and n_dn < 30:
            flags.append(f"down-month sample is {n_dn} months — a regime statistic on fewer than "
                         f"30 observations is a description of one drawdown, not a property")
        if dn_a is not None and up_a is not None and abs(dn_a - up_a) > 10:
            flags.append(f"up/down alpha differ by {abs(dn_a - up_a):.1f}pp — either a genuine "
                         f"regime asymmetry or the split picking up one episode")
        if r.get("down_capture_pct") is not None and r["down_capture_pct"] < 0:
            flags.append("negative down-capture: the fund ROSE while the benchmark fell on "
                         "average, which is a strong claim resting entirely on the down sample")
        item["flags"] = flags
        item["label_status"] = "AWAITING_OUT_OF_SAMPLE" if flags else "no_candidate"
        item["what_would_falsify_it"] = (
            "the same regime split re-measured on months added after this run showing the "
            "asymmetry within noise of zero")
        if flags:
            queue.append(item)
    queue.sort(key=lambda i: -abs((i.get("down_alpha_ann_pct") or 0)))
    return {"queue": queue, "n_queued": len(queue),
            "read_this_first": (
                "Nothing here is a verdict. Each entry is a claim with its sample size attached, "
                "recorded so the next run can compare the same number rather than re-derive a "
                "different one. R3.6: signs flip by regime and era, and a regime statistic "
                "measured on a single regime cannot distinguish the two.")}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# emit
# ══════════════════════════════════════════════════════════════════════════════════════════════

EMITTERS = ("rank_persistence", "window_audit", "proxy_report", "rolling_drift",
            "alpha_persistence", "benchmark_controls", "regime_label_queue")


def emit(as_of=None, write=False, universe=None, study=None):
    study = study or load_study()
    built = build_panel(universe)
    panel = built["panel"]
    out = {
        "schema_version": LEARNING_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": (as_of or dt.date.today()).isoformat(),
        "consumes": {"study": study.get("_source_file"),
                     "study_generated_at": study.get("generated_at")},
        "panel": {"n_funds": built["n_funds"], "n_refused": built["n_refused"],
                  "refused": built["refused"]},
        "rank_persistence": rank_persistence(panel),                 # L-1  ISA-0184
        "window_audit": window_audit(panel, study),                  # L-2  ISA-0185
        "proxy_report": proxy_report(panel),                         # L-3  ISA-0186
        "rolling_drift": rolling_drift(panel),                       # L-5  ISA-0188
        "alpha_persistence": alpha_persistence_registrations(study),  # L-6  ISA-0189
        "benchmark_controls": benchmark_controls(study),             # L-7  ISA-0190
        "regime_label_queue": regime_label_queue(study),             # L-8  ISA-0191
    }
    out["liveness"] = liveness(out)
    if write:
        p = HERE / store_name(as_of)
        p.write_text(json.dumps(out, indent=1, sort_keys=True, default=str), encoding="utf-8")
        out["_written"] = p.name
    return out


def liveness(emitted) -> dict:
    """R8.2 — every learning task ships an assertion that proves it RAN.

    Not 'the key exists': a key can exist holding an empty dict. Each emitter names the specific
    evidence that it did work, and `ran` is False when it did not.
    """
    rp = emitted.get("rank_persistence") or {}
    wa = emitted.get("window_audit") or {}
    pr = emitted.get("proxy_report") or {}
    rd = emitted.get("rolling_drift") or {}
    ap = emitted.get("alpha_persistence") or {}
    bc = emitted.get("benchmark_controls") or {}
    rq = emitted.get("regime_label_queue") or {}
    return {
        "rank_persistence": {"ran": bool(rp.get("rank_ic")), "item": "ISA-0184",
                             "evidence": f"{len(rp.get('per_fund') or {})} funds split into two "
                                         f"non-overlapping halves; "
                                         f"{len(rp.get('rank_ic') or {})} measures rank-correlated"},
        "window_audit": {"ran": bool(wa.get("rows")), "item": "ISA-0185",
                         "evidence": f"{len(wa.get('rows') or [])} funds audited; "
                                     f"{len(wa.get('funds_with_no_declared_inception') or [])} "
                                     f"with no declared inception"},
        "proxy_report": {"ran": pr.get("status") in ("CHECKED", "NO_DECLARED_PROXY_RELATIONSHIPS"),
                         "item": "ISA-0186",
                         "evidence": f"status {pr.get('status')}, "
                                     f"{pr.get('declared_proxies')} declared relationships"},
        "rolling_drift": {"ran": any("series" in v for v in (rd.get("per_fund") or {}).values()),
                          "item": "ISA-0188",
                          "evidence": f"{sum(len(v.get('series', [])) for v in (rd.get('per_fund') or {}).values())} "
                                      f"rolling windows stored"},
        "alpha_persistence": {"ran": len(ap.get("registrations") or []) >= 2, "item": "ISA-0189",
                              "evidence": f"{len(ap.get('registrations') or [])} hypotheses "
                                          f"pre-registered with thresholds and revisit dates"},
        "benchmark_controls": {"ran": bool(bc.get("checked")), "item": "ISA-0190",
                               "evidence": f"{bc.get('n_controls')} controls asserted; "
                                           f"{len(bc.get('breaches') or [])} breach(es)"},
        "regime_label_queue": {"ran": "queue" in rq, "item": "ISA-0191",
                               "evidence": f"{rq.get('n_queued')} candidates queued for "
                                           f"out-of-sample labelling"},
    }


def check(emitted=None) -> list:
    """What the routine battery asserts. Non-empty means something is wrong."""
    e = emitted or emit()
    out = []
    for name, lv in (e.get("liveness") or {}).items():
        if not lv.get("ran"):
            out.append(f"LEARNING TASK DEAD: {name} ({lv.get('item')}) produced no output — "
                       f"a learning task that cannot prove it ran is not a learning task (R8.2)")
    for b in (e.get("benchmark_controls") or {}).get("breaches") or []:
        out.append(f"CONTROL {b.get('verdict')}: {b.get('sedol')} {b.get('name')} "
                   f"beta={b.get('beta')} alpha={b.get('alpha_ann_pct')} "
                   f"vs -OCF={-(b.get('ocf') or 0)} r2={b.get('r_squared')} — "
                   f"{b.get('why')}")
    return out


def report(e=None) -> str:
    e = e or emit()
    L = [f"FUND LEARNING BATTERY  as_of {e['as_of']}  (consumes {e['consumes']['study']})", ""]
    L.append(f"panel: {e['panel']['n_funds']} funds, {e['panel']['n_refused']} refused")
    L.append("")
    L.append("L-1 RANK PERSISTENCE (ISA-0184)")
    for m, v in (e["rank_persistence"]["rank_ic"] or {}).items():
        if v.get("refused"):
            L.append(f"   {m:<14} REFUSED — {v['refused']}")
        else:
            L.append(f"   {m:<14} rho {v['rho']:+.3f}  n {v['n']}  95% CI "
                     f"[{v['ci95'][0]:+.2f},{v['ci95'][1]:+.2f}]  "
                     f"{'PERSISTENT' if v.get('persistent') else 'inconclusive'}")
    L += ["", "L-2 WINDOW AUDIT (ISA-0185)", "   " + e["window_audit"]["verdict"]]
    L += ["", "L-3 PROXY VALIDATION (ISA-0186)", "   " + e["proxy_report"]["status"]]
    L += ["", "L-5 ROLLING DRIFT (ISA-0188)  widest beta range:"]
    for sd, rng in e["rolling_drift"]["widest_beta_drift"]:
        L.append(f"   {sd:<10} beta range {rng:.3f}")
    L += ["", "L-6 PRE-REGISTERED ALPHA PERSISTENCE (ISA-0189)"]
    for r in e["alpha_persistence"]["registrations"]:
        L.append(f"   {r['id']}  minimum window ends {r['minimum_window'].split('before ')[-1]}")
    L += ["", "L-7 STANDING CONTROLS (ISA-0190)"]
    for c in e["benchmark_controls"]["checked"]:
        L.append(f"   {c['sedol']:<10} {c['verdict']:<8} beta {c.get('beta')} "
                 f"alpha {c.get('alpha_ann_pct')} vs -OCF {-(c.get('ocf') or 0)} "
                 f"r2 {c.get('r_squared')}")
    L += ["", f"L-8 REGIME LABEL QUEUE (ISA-0191): {e['regime_label_queue']['n_queued']} queued"]
    for q in e["regime_label_queue"]["queue"][:6]:
        L.append(f"   {q['sedol']:<10} down-alpha {q.get('down_alpha_ann_pct')} "
                 f"(n_down {q.get('n_down')})  down-capture {q.get('down_capture_pct')}")
    L += ["", "LIVENESS (R8.2)"]
    for k, v in e["liveness"].items():
        L.append(f"   {'ok ' if v['ran'] else 'DEAD'} {k:<20} {v['item']}  {v['evidence']}")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# selftest
# ══════════════════════════════════════════════════════════════════════════════════════════════

def selftest(verbose=True) -> int:
    import random
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    # ---- spearman, against a hand-computable case
    ok(spearman([1, 2, 3, 4], [1, 2, 3, 4])["rho"] == 1.0, "perfect concordance must be rho 1")
    ok(spearman([1, 2, 3, 4], [4, 3, 2, 1])["rho"] == -1.0, "perfect discordance must be rho -1")
    ok(spearman([1, 1, 1, 1], [1, 2, 3, 4]).get("refused"),
       "a measure with no spread must REFUSE, not return 0 (R4.3)")
    s = spearman([1, 2, 3, 4], [1, 2, 3, 4])
    ok(s["ci95"][0] < 1.0, "the interval must be reported and must be wide on n=4")

    # ---- regress, against a noiseless construction
    x = [0.01 * i for i in range(-20, 20)]
    y = [0.004 / 12 + 1.3 * v for v in x]
    r = regress(y, x)
    ok(abs(r["beta"] - 1.3) < 1e-9 and abs(r["r2"] - 1.0) < 1e-9,
       f"a noiseless line must recover its slope exactly, got {r}")
    ok(regress([1, 2], [1, 2]) is None, "a window shorter than 12 must refuse")
    ok(regress(y, [0.5] * len(y)) is None, "a constant regressor must refuse, never divide by zero")

    # ---- validate_proxy: BOTH tests, and each must be able to fail alone
    random.seed(7)
    base = {(2020, m % 12 + 1): random.gauss(0.008, 0.03) for m in range(60)}
    same = dict(base)
    ok(validate_proxy(base, same)["admissible"], "an identical series must be admissible")
    # same shape, 3% a year cheaper -> correlation ~1 but the level test must FAIL
    cheaper = {k: v + 0.03 / 12 for k, v in base.items()}
    r1 = validate_proxy(base, cheaper)
    ok(r1["corr_test"]["pass"] and not r1["diff_test"]["pass"] and not r1["admissible"],
       f"a fee-level difference must fail the LEVEL test with correlation still passing: {r1}")
    # different portfolio, similar level -> level test may pass but correlation must FAIL
    noise = {k: base[k] * -1 for k in base}
    r2 = validate_proxy(base, noise)
    ok(not r2["corr_test"]["pass"] and not r2["admissible"],
       "an inverted series must fail the correlation test")
    ok(validate_proxy({(2020, 1): 0.01}, {(2020, 1): 0.01}).get("refused"),
       "fewer than 12 overlapping months must refuse, not pass on one point")

    # ---- benchmark_controls: a null must BLOCK, and a real miss must FAIL
    good = {"funds": {"X": {"name": "tracker", "benchmark_ticker": "T"}}, "controls": {
        "X": {"beta": 1.0, "alpha_ann_pct": -0.07, "ocf": 0.07, "r_squared": 0.99}},
        "coverage": {}, "method": {}}
    ok(benchmark_controls(good)["pass"], "a clean tracker must pass its controls")
    bad_beta = json.loads(json.dumps(good))
    bad_beta["controls"]["X"]["beta"] = 0.6
    ok(not benchmark_controls(bad_beta)["pass"], "beta 0.6 on a tracker must FAIL the control")
    bad_alpha = json.loads(json.dumps(good))
    bad_alpha["controls"]["X"]["alpha_ann_pct"] = 3.0
    ok(not benchmark_controls(bad_alpha)["pass"],
       "alpha +3% on an index tracker must FAIL — it is comparator evidence, not skill")
    nullish = json.loads(json.dumps(good))
    nullish["controls"]["X"]["ocf"] = None
    res = benchmark_controls(nullish)
    ok(not res["pass"] and res["breaches"][0]["verdict"] == "UNKNOWN",
       "a control fed a null must return UNKNOWN and BLOCK, never PASS (R4.3, KR3)")

    # ---- load_study asserts its contract
    raised = False
    try:
        import tempfile
        tp = Path(tempfile.mkdtemp()) / "beta_alpha_study_x.json"
        tp.write_text(json.dumps({"funds": {}}), encoding="utf-8")
        load_study(tp)
    except ValueError as e:
        raised = "contract has changed" in str(e)
    ok(raised, "a study missing a required block must RAISE at the boundary (R5.1)")

    # ---- liveness: an empty emission must report DEAD, not quietly pass
    dead = {"rank_persistence": {}, "window_audit": {}, "proxy_report": {},
            "rolling_drift": {}, "alpha_persistence": {}, "benchmark_controls": {},
            "regime_label_queue": {}}
    lv = liveness(dead)
    ok(all(not v["ran"] for v in lv.values()),
       "an emitter that produced nothing must report ran=False (R8.2)")
    ok(len(check({"liveness": lv})) == 7,
       f"check() must name every dead emitter, got {len(check({'liveness': lv}))}")

    # ---- half measures are computed by identical code on both halves (no drift between them)
    keys = sorted(base)
    bench = {k: base[k] * 0.9 for k in keys}
    h = _half_measures(base, bench, keys)
    ok(h["n_months"] == len(keys) and h["beta"] is not None,
       "half measures must populate on a valid window")

    # ---- ISA-0350 NEGATIVE CONTROL. The first version of info_ratio was mean(residual)/sd,
    # which OLS forces to zero on its own fitting window: it returned 0.0 for all 11 live funds
    # and looked entirely plausible. A constructed fund with a KNOWN positive alpha must show a
    # positive, finite information ratio — this is the assertion that would have caught it.
    signal = {k: 0.9 * base[k] + 0.005 + random.gauss(0, 0.004) for k in keys}
    hs = _half_measures(signal, base, keys)
    ok(hs["info_ratio"] is not None and hs["info_ratio"] > 0.5,
       f"a fund constructed with +0.5%/month alpha must show a clearly positive information "
       f"ratio, got {hs['info_ratio']} — a structurally-zero IR is FC-A")
    ok(abs(hs["alpha"] - 6.0) < 2.5,
       f"...and its annualised alpha must be near +6%, got {hs['alpha']}")
    drag = {k: 0.9 * base[k] - 0.005 + random.gauss(0, 0.004) for k in keys}
    ok(_half_measures(drag, base, keys)["info_ratio"] < -0.5,
       "a constructed NEGATIVE alpha must show a clearly negative information ratio")
    ok(hs.get("resid_sd_ann_pct") and hs["resid_sd_ann_pct"] > 0,
       "residual risk must be published alongside the ratio, not hidden inside it")

    if verbose:
        print(f"fund_learning selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fund learning battery — L-1/2/3/5/6/7/8")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--controls", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.controls:
        r = benchmark_controls(load_study())
        print(json.dumps(r, indent=1))
        return 0 if r["pass"] else 1
    if a.emit or a.write:
        e = emit(write=a.write)
        print(report(e))
        if a.write:
            print(f"\nwrote {e.get('_written')}")
        return 0
    if a.check:
        v = check()
        print("\n".join(v) if v else "fund learning battery: every emitter live, controls pass")
        return 1 if v else 0
    if a.report:
        print(report())
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
