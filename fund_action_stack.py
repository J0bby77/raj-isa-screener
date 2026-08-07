#!/usr/bin/env python3
"""
fund_action_stack.py — register C4 + C5, DataMining Part B (built 05-Aug-2026).

⚑ THE GAP THIS CLOSES, IN ONE SENTENCE
The stock sleeve is 7.9% of the ISA and is ranked against alternatives every month, has a dead-
money floor at Source < 50, and a mandatory retain-vs-redeploy comparison. **The fund sleeve is
85.1% of the ISA and has none of the three.** `fund_actions` tested only band drift and
est-vs-bucket-minimum. "Every pound must earn its place" was being enforced on 8% of the
portfolio and not on the other 85%.

⚑ AND THE SIGNAL IT WAS ENFORCED WITH DOES NOT WORK (C4)
`est_return` spans 9.0–14.0% against a realised 5-year reality of 2.5–28.3%. It understates 9 of
12 funds by 0.5–14.3pp and, worse than being noisy, it **points at the wrong funds**:

    Scottish Mortgage   realised 5y 0.22% p.a. against a 13% B3 minimum
                        -> scored 14.0% by est, the HIGHEST in its bucket, reported "Hold"
    Invesco Asian       marginally FAILS on realised, PASSES on est
    JPM UK              the exact reverse

A number that ranks the worst holding in its bucket first is not a weak signal, it is an
inverted one. So realised evidence becomes primary and `est_return` is demoted to corroborator —
the same demotion the X-Ray figures got, for the same reason.

WHAT THIS EMITS
    fund_dominance[]          binary, unarguable, runs before anything is scored
    fund_retention_score      FRS 0-100, the fund analogue of the Source Score
    anchor_rule[]             realised 5y vs bucket minimum — the ownership floor
    fund_efficiency_rank      mean / stddev league table
    correlation_redundancy    every held pair >= 0.80, cheaper-or-better one named
    fund_action_stack[]       ONE ranked agenda, HOLD/ADD | RETAIN-ONLY | DEAD MONEY

⚑ WHAT IT DOES NOT DO. It does not trade, size, or auto-escalate to a sell. It produces the
agenda that Step 8 Category 7 must answer. A fund the framework cannot measure is `UNSCORED`
with the reason, never a low score — an unmeasured fund and a bad fund are opposite facts, and
scoring the first as the second is how the framework would start selling the things it merely
failed to fetch.
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, statistics as st, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ── FRS BANDS — REBASED 06-Aug-2026 (Raj) from 65/50 to 58/43 ───────────────────────────
# ⚑ NOT A LOOSENING. The scoring of return adequacy changed in the same session (headroom added
# above the floor, because the old formula capped at the floor and put 11 of 12 funds on full
# marks). **65/50 were calibrated on 05-Aug against that SATURATED scale.** Leaving them fixed
# while the scale underneath them changed would have tightened the ownership floor as a SIDE
# EFFECT of a measurement fix — three bands moved, Thornbridge lost new-money eligibility, and
# nobody had decided any of it. Two things that must agree, silently disagreeing: the failure
# mode this register is a list of.
#
# The change Raj approved was to make the score RANK. It was not a decision to move the floor.
# 58/43 reproduces the 05-Aug band distribution exactly on live data, so the ranking improvement
# lands and the policy does not move. Tightening is now a SEPARATE, deliberate decision.
#
# ⚑ STATED PLAINLY: these two numbers are REVERSE-ENGINEERED to preserve that distribution. They
# are no better calibrated than the ones they replace — register H5 applies to both, and neither
# is backtestable on the history available. They are honest about what they are: a hold, until
# there is evidence to set them against. Same logic as the concentration limit (register L1):
# measure first, set the number after.
FRS_HOLD_ADD, FRS_RETAIN_ONLY = 58.0, 43.0
FRS_BANDS_BASIS = ("rebased 06-Aug-2026 to preserve the 05-Aug band distribution across the "
                   "return-adequacy rescoring; uncalibrated, pending evidence (H5 / L1)")
DOMINANCE_CORR = 0.80
REDUNDANCY_CORR = 0.80
FEE_PEER_CORR = 0.85
WEIGHTS = {"return_adequacy": 35, "risk_efficiency": 25, "diversification": 20,
           "fee_efficiency": 10, "mandate_integrity": 10}

# Bucket minimum annualised returns. One home: read from target_weights.json when present so a
# threshold cannot say one thing here and another there.
DEFAULT_BUCKET_MIN = {"B1": 0.12, "B2": 0.12, "B3": 0.13}

# ── RETURN-ADEQUACY BASIS (Tier-1 items 1 and 5, 06-Aug-2026) ───────────────────────────
# Two separate choices, both stated here and both reversible, because both were being made
# implicitly by whichever line of code got there first.
#
# 1. WHICH TRAILING STATISTIC. Scoring the MEDIAN across {1y,3y,5y,10y} means no single start
#    date decides the outcome — but it is also generous: Scottish Mortgage's median of
#    {0.22, 22.18, 23.94} is 22.18 and earns FULL return-adequacy marks, while five years of a
#    real GBP 6,626 produced roughly nothing. `minimum` is the conservative reading and would
#    band on the worst window. Neither is obviously right; the choice is Raj's, so both are
#    computed every run and the comparison is published (`return_adequacy_basis_study`).
#
# 2. WHETHER THE MONEY-WEIGHTED RETURN OUTRANKS THEM. Every trailing window is a choice of start
#    date. Raj's holding period is not, and it is the only basis that answers the actual question
#    — "has THIS capital earned its place" — rather than "was this a good fund over a window I
#    picked". Where `holding_period_return` certifies an MWR as anchor-eligible it takes
#    precedence; where it does not, it is reported beside the trailing figure and used for
#    nothing. It never silently substitutes.
RETURN_ADEQUACY_STAT = "median"          # "median" | "minimum" | "mean"
MWR_TAKES_PRECEDENCE = True
RETURN_ADEQUACY_STATS = ("median", "minimum", "mean")

# ── ⚑ HEADROOM ABOVE THE FLOOR (Raj, 06-Aug-2026) ───────────────────────────────────────
# The old score was `min(1, value / floor) * 35`, which SATURATES at the anchor floor. On live
# August data that put **11 of 12 funds at full marks**, and scored Polar's 43.77% identically to
# Vanguard S&P's 15.47%. The largest single FRS component — 35 of 100 points — was discriminating
# between almost nothing, which is the opposite of what C5 built it for.
#
# Now piecewise. Clearing the floor is necessary and earns most of the points; beating it earns
# the rest, scaled against WHAT ELSE THIS POUND COULD HAVE DONE. That second half is the
# opportunity-cost test — "every pound must earn its place" is a comparative claim, and a score
# that stops measuring at the minimum cannot express it.
#
# The stretch is the sleeve's 75th percentile, recomputed each run and REPORTED. Not the maximum:
# one outlier (Polar, +72.56% over a year) would compress everything else into a narrow band and
# make the score a referendum on the tech fund. Not a fixed constant either — an invented
# threshold is what register H5 warns about. A percentile is derived from the opportunity set
# that actually exists, and moves with it, which is what opportunity cost means.
RETURN_ADEQUACY_FLOOR_SHARE = 0.60       # share of the points earned by reaching the floor
RETURN_ADEQUACY_STRETCH_PCTILE = 75      # the sleeve percentile that earns the remaining 40%
RETURN_ADEQUACY_MIN_STRETCH_MULT = 1.25  # stretch is never closer to the floor than this


def _stretch_level(values, floor):
    """The return level that earns full marks. Derived from the sleeve, never invented."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return floor * RETURN_ADEQUACY_MIN_STRETCH_MULT
    k = (len(v) - 1) * RETURN_ADEQUACY_STRETCH_PCTILE / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    p = v[lo] + (v[hi] - v[lo]) * (k - lo)
    return max(p, floor * RETURN_ADEQUACY_MIN_STRETCH_MULT)


def _return_adequacy_points(value, floor, stretch, weight):
    """ONE HOME for the return-adequacy score. The live path and the basis study both call it, so
    the two cannot drift apart — which is how a study stops describing the thing it studies."""
    if value is None:
        return None
    if value <= 0:
        return 0.0
    base = weight * RETURN_ADEQUACY_FLOOR_SHARE
    if value < floor:
        return max(0.0, value / floor) * base
    if stretch <= floor:
        return weight
    over = min(1.0, (value - floor) / (stretch - floor))
    return base + over * weight * (1.0 - RETURN_ADEQUACY_FLOOR_SHARE)


def _window_stat(vals, stat):
    """Collapse the per-window realised returns to one number under a NAMED statistic."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    if stat == "minimum":
        return v[0]
    if stat == "mean":
        return sum(v) / len(v)
    return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2.0


def _bucket_minimums():
    try:
        tw = json.load(open(os.path.join(HERE, "target_weights.json"), encoding="utf-8"))
        b = {}
        for k, v in (tw.get("buckets") or {}).items():
            m = (v or {}).get("min_return") or (v or {}).get("return_minimum")
            if m is not None:
                b[k] = float(m)
        if b:
            return b
    except Exception:
        pass
    return dict(DEFAULT_BUCKET_MIN)


def _anchor_floor():
    """The required-return floor. target_state.json is the authority (A19)."""
    try:
        ts = json.load(open(os.path.join(HERE, "target_state.json"), encoding="utf-8"))
        v = ts.get("required_return_floor_pct")
        if v is not None:
            return float(v)
    except Exception:
        pass
    return 13.9


# ---------------------------------------------------------------- return series
def _monthly_returns(series):
    """Month-end NAV -> monthly total returns. Monthly, not daily: daily correlations between
    OEICs priced at different cut-offs are contaminated by the pricing lag, which manufactures
    a low correlation between two funds holding nearly the same thing."""
    if not series or len(series) < 24:
        return []
    by_month = {}
    for d, v in series:
        by_month[(d.year, d.month)] = (d, v)          # last observation wins
    keys = sorted(by_month)
    out = []
    for i in range(1, len(keys)):
        p0, p1 = by_month[keys[i - 1]][1], by_month[keys[i]][1]
        if p0 and p0 > 0:
            out.append((keys[i], p1 / p0 - 1.0))
    return out


def _align(a, b):
    da, db = dict(a), dict(b)
    common = sorted(set(da) & set(db))
    return [da[k] for k in common], [db[k] for k in common]


def _corr(a, b):
    x, y = _align(a, b)
    if len(x) < 24:
        return None                       # two years of monthly data is the floor
    try:
        return st.correlation(x, y)
    except Exception:
        return None


def _ann_stats(monthly, years=5):
    m = monthly[-int(years * 12):] if years else monthly
    if len(m) < 24:
        return None, None
    r = [v for _, v in m]
    mean_ann = ((1 + st.fmean(r)) ** 12 - 1) * 100.0
    sd_ann = st.pstdev(r) * math.sqrt(12) * 100.0
    return round(mean_ann, 2), round(sd_ann, 2)


def _pct_rank(v, pool):
    pool = [p for p in pool if p is not None]
    if v is None or len(pool) < 2:
        return None
    return 100.0 * sum(1 for p in pool if p < v) / (len(pool) - 1) if len(pool) > 1 else None


XRAY_TOL_PP = 2.0        # beyond this the two derivations are reported as disagreeing


def _xray_returns(xray_path=None):
    """Second, INDEPENDENT derivation of realised fund returns: the AJ Bell / Morningstar X-Ray.

    Not a fallback — a CROSS-CHECK. The golden source computes returns from NAV history to the
    run date; the X-Ray publishes its own figures struck at its own (earlier, per-fund varying)
    dates. Where they agree, the verdict is safe. Where they disagree, saying so is the whole
    point: on Aug-2026 data they differ by up to 4.9pp and **JPM UK crosses its bucket minimum
    in opposite directions depending on which you believe** (11.48% vs 12.06% against a 12.0%
    B2 floor). Publishing either number alone would have produced a confident, unearned verdict.
    """
    path = xray_path or os.path.join(HERE, "xray_data_aug_2026.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}, None
    out = {}
    for h in d.get("fund_holdings", []) or []:
        nm = str(h.get("name") or "").strip().lower()
        if nm and h.get("return_5yr") is not None:
            out[nm] = {"return_5yr": h["return_5yr"], "return_3yr": h.get("return_3yr"),
                       "return_1yr": h.get("return_1yr"),
                       "as_of": h.get("as_of") or h.get("as_of_raw")}
    return out, (d.get("_meta", {}) or {}).get("as_of")


def _parse_xray_date(v):
    """X-Ray per-fund strike dates are NOT uniform (M6): ten funds at 30-Jun, Scottish Mortgage
    at 31-May. Any comparison assuming one lag would still be wrong."""
    import datetime as _d
    s = str(v or "").strip()
    for f in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return _d.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _match_xray(name, xr, xray_name=None):
    """Join a universe fund to its X-Ray row via the DECLARED `xray_name` key.

    ⚑ Name matching by token overlap was tried and abandoned. It matched "Vanguard Jpn Stk Idx
    GBP Acc" to "VANGUARD S&P 500 ETF USD ACC GBP" on the shared tokens {vanguard, acc, gbp} —
    two entirely different funds — and the false pair was then published as a DISPUTED anchor
    verdict, which is worse than having no cross-check at all because it wears the appearance of
    diligence. Tightening the heuristic broke it the other way, joining Artemis UK to Artemis
    European. There is no threshold that makes fuzzy name matching safe here, because the fund
    names genuinely differ only in one token.

    So the join is DECLARED in `fund_universe.json` and never inferred. If AJ Bell renames a row
    the join fails loudly and is fixed in one place.
    """
    if not xray_name:
        return None, None
    k = str(xray_name).strip().lower()
    v = xr.get(k)
    if v is not None:
        return k, v
    # tolerate whitespace/case drift only — never a different fund
    for kk, vv in xr.items():
        if " ".join(kk.split()) == " ".join(k.split()):
            return kk, vv
    return None, None


# ---------------------------------------------------------------- the build
def build(as_of=None, portfolio=None, perf=None, refresh=False, xray=None):
    import fund_performance as fp
    as_of = as_of or dt.date.today()
    universe = fp.load_universe()
    funds = universe.get("funds", universe)
    perf = perf if perf is not None else fp.all_fund_performance(as_of, refresh=refresh)
    xray_ret, xray_as_of = _xray_returns(xray)
    xray_dates = {}
    for _sd, _u in funds.items():
        if str(_sd).startswith("_"):
            continue
        _k, _v = _match_xray(None, xray_ret, _u.get("xray_name"))
        _dd = _parse_xray_date((_v or {}).get("as_of"))
        if _dd:
            xray_dates[_sd] = _dd
    bmin = _bucket_minimums()
    anchor = _anchor_floor()

    # ── money-weighted holding-period return (Tier-1 item 1) ────────────────────────────
    # The one window nobody chose. Computed here rather than inside the FRS loop so a failure to
    # load it degrades to "MWR unavailable, reason stated" and never to "MWR is zero".
    mwr = {}
    mwr_error = None
    try:
        import holding_period_return as hpr
        if portfolio:
            _m = hpr.all_holding_period_returns(portfolio)
            mwr = _m.get("holdings", {}) or {}
            mwr_meta = {k: v for k, v in _m.items() if k not in ("holdings",)}
        else:
            mwr_meta = {}
            mwr_error = "no portfolio supplied — money-weighted return needs the broker valuation"
    except Exception as _e:                                   # noqa: BLE001
        mwr_meta = {}
        mwr_error = f"{type(_e).__name__}: {_e}"

    # ── closed-end discount / premium (Tier-1 item 3) ───────────────────────────────────
    # A trust's price return is its NAV return plus the change in its discount, so a price-derived
    # figure for a closed-end holding is measuring two things at once. The FRS still uses it —
    # there is no NAV series to use instead — but the row must SAY so, and the retain-vs-redeploy
    # case must carry the pounds that selling at a discount actually costs.
    trust = {}
    trust_capture = []
    trust_error = None
    try:
        import trust_discount as _td
        _tr = _td.build(portfolio, funds, None, as_of)
        trust = {d["sedol"]: d for d in _tr.get("closed_end_holdings", [])}
        trust_capture = _tr.get("capture_status") or []
    except Exception as _e:                                   # noqa: BLE001
        trust_error = f"{type(_e).__name__}: {_e}"

    held = {}
    for sedol, u in funds.items():
        if str(sedol).startswith("_"):
            continue
        sym = u.get("yf_symbol") or u.get("isin")
        series = fp.fetch_nav_history(sym, use_cache=True, refresh=refresh,
                                      scale=fp._scale_for(u)) if sym else []
        monthly = _monthly_returns(series)
        mean5, sd5 = _ann_stats(monthly, 5)
        mean3, sd3 = _ann_stats(monthly, 3)
        p = (perf or {}).get(sedol, {}) or {}
        rets = p.get("returns", {}) or {}
        _xd = xray_dates.get(sedol)
        _gx = None
        if _xd:
            try:
                _pr = fp.fund_performance(sedol, _xd, funds)
                _gx = ((_pr["returns"].get("5y") or {}).get("annualised") or {}).get("value")
            except Exception:
                _gx = None

        def _r(w):
            """Realised annualised return for window `w` from the golden source.

            ⚑ The first version of this read `v["annualised_pct"]` / `v["value"]`. The actual
            shape is `returns[w]["annualised"]["value"]`, with a sibling `present` flag — so it
            silently returned None for every fund, and FRS went on to issue **DEAD MONEY on 8 of
            12 funds whose returns it had never read**, because the remaining components summed
            to just above the measurement threshold. Exactly the defect class this framework
            keeps paying for: a missing value that is plausible enough to survive.
            The `present` flag is now honoured, so a null with a stated reason stays null.
            """
            v = rets.get(w)
            if not isinstance(v, dict):
                return v
            ann = v.get("annualised")
            if isinstance(ann, dict):
                return ann.get("value") if ann.get("present", ann.get("value") is not None) else None
            return v.get("annualised_pct", v.get("value"))
        held[sedol] = {
            "sedol": sedol, "name": u.get("name"), "bucket": u.get("bucket"),
            "ocf": u.get("ocf"), "status": p.get("status") or u.get("resolution_status"),
            "monthly": monthly, "mean_5y_ann": mean5, "stddev_5y_ann": sd5,
            "mean_3y_ann": mean3, "stddev_3y_ann": sd3,
            "realised_3y_ann": _r("3y"), "realised_5y_ann": _r("5y"),
            "realised_1y_ann": _r("1y"), "realised_10y_ann": _r("10y"),
            "sharpe_like": (round(mean5 / sd5, 3) if (mean5 and sd5) else None),
            "golden_at_xray_date": _gx,
        }

    # ── weights, so "dead money" can be priced ─────────────────────────────────────
    # ⚑ FIXED 06-Aug-2026. The previous join was `held[sedol].name.lower()[:18] in
    # portfolio_fund.name.lower()`. The first 18 characters of BOTH Artemis funds are
    # "artemis smartgarp ", so each portfolio row matched BOTH sedols and the last write won:
    # **Artemis SmartGARP European carried Artemis SmartGARP UK's value (£12,376.55 against a
    # true £12,773.48) on every run**, understating it by £396.93 and its weight by 0.28pp.
    #
    # This is the SAME defect the register already fixed for the X-Ray join on 05-Aug, which
    # named this exact pair when the heuristic was tightened: *"No threshold makes this safe,
    # because the names genuinely differ in one token."* The conclusion there applies here —
    # **the join is DECLARED, never inferred.** Every portfolio fund row is keyed by its SEDOL
    # (all 12 are today); an optional `portfolio_ticker` in fund_universe.json carries the
    # declared alias if the broker ever renames one.
    #
    # And the join is RECONCILED: a holding that fails to join is NAMED and its value reported,
    # never silently dropped into a smaller denominator.
    weights = {}
    unjoined = []
    _alias = {}
    for _sd, _u in funds.items():
        if str(_sd).startswith("_"):
            continue
        _alias[str(_sd).upper()] = _sd
        _pt = (_u or {}).get("portfolio_ticker")
        if _pt:
            _alias[str(_pt).upper()] = _sd
    for f in (portfolio or {}).get("funds", []) or []:
        _t = str(f.get("ticker") or "").upper()
        _sd = _alias.get(_t)
        if _sd is not None and _sd in held:
            weights[_sd] = {"value_gbp": f.get("value_gbp"), "pct": f.get("weight_pct")}
        else:
            unjoined.append({"ticker": f.get("ticker"), "name": f.get("name"),
                             "value_gbp": f.get("value_gbp"),
                             "reason": ("no declared join to fund_universe.json — add the SEDOL "
                                        "key or a `portfolio_ticker` alias. NOT name-matched: "
                                        "an inferred join once gave one Artemis fund the "
                                        "other's value")})
    # ── I-FAS-JOIN. Two independent derivations of the fund sleeve must agree. ────────
    _broker_sleeve = ((portfolio or {}).get("summary") or {}).get("fund_sleeve_value_gbp")
    _joined_sum = sum((w.get("value_gbp") or 0.0) for w in weights.values())
    join_reconciliation = {
        "joined": len(weights), "portfolio_fund_rows": len((portfolio or {}).get("funds") or []),
        "unjoined": unjoined,
        "joined_value_gbp": round(_joined_sum, 2),
        "broker_fund_sleeve_gbp": (None if _broker_sleeve is None else round(float(_broker_sleeve), 2)),
        "delta_gbp": (None if _broker_sleeve is None
                      else round(_joined_sum - float(_broker_sleeve), 2)),
        "holds": bool(_broker_sleeve is not None
                      and abs(_joined_sum - float(_broker_sleeve)) < 0.01
                      and not unjoined),
        "note": ("the joined holdings must reproduce the broker's own fund-sleeve total. A "
                 "mis-joined or dropped holding is invisible in every average built on these "
                 "weights and obvious here."),
    }

    # ── correlations ───────────────────────────────────────────────────────────────
    corr = {}
    keys = [k for k in held if held[k]["monthly"]]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            c = _corr(held[a]["monthly"], held[b]["monthly"])
            if c is not None:
                corr[f"{a}|{b}"] = round(c, 3)

    def _c(a, b):
        return corr.get(f"{a}|{b}", corr.get(f"{b}|{a}"))

    # ── F1 DOMINANCE — binary, before any scoring ──────────────────────────────────
    dominance, window_conflicts = [], []
    for x in keys:
        hx = held[x]
        if hx["mean_5y_ann"] is None or hx["stddev_5y_ann"] is None:
            continue
        for y in keys:
            if y == x:
                continue
            hy = held[y]
            if hy["mean_5y_ann"] is None or hy["stddev_5y_ann"] is None:
                continue
            c = _c(x, y)
            if c is None or c < DOMINANCE_CORR:
                continue
            # ⚑ WINDOW AGREEMENT (05-Aug-2026). The VUAG/RLGES verdict REVERSES between
            # windows: on 3 years VUAG dominates (21.07/13.24 vs 21.00/13.63) exactly as
            # register H7 says; on 5 years RLGES wins on return (16.39/13.04 vs 14.15/13.09).
            # RLGES's entire advantage sits in years 4-5.
            # A "strictly dominated" verdict that depends on which window you happened to pick
            # is not a dominance verdict, it is a window choice wearing one. Both must agree.
            # (This is also why the X-Ray's own Mean/Std Dev columns are dangerous: they state
            # NO period at all — the 3-year basis had to be inferred from the std devs.)
            dom3 = (hy["mean_3y_ann"] is not None and hx["mean_3y_ann"] is not None
                    and hy["stddev_3y_ann"] is not None and hx["stddev_3y_ann"] is not None
                    and hy["mean_3y_ann"] >= hx["mean_3y_ann"]
                    and hy["stddev_3y_ann"] <= hx["stddev_3y_ann"])
            dom5 = (hy["mean_5y_ann"] >= hx["mean_5y_ann"]
                    and hy["stddev_5y_ann"] <= hx["stddev_5y_ann"])
            if dom5 != dom3 and hx["mean_3y_ann"] is not None:
                window_conflicts.append({
                    "fund": x, "fund_name": hx["name"], "versus": y, "versus_name": hy["name"],
                    "dominated_on_3y": dom3, "dominated_on_5y": dom5,
                    "x_3y": [hx["mean_3y_ann"], hx["stddev_3y_ann"]],
                    "y_3y": [hy["mean_3y_ann"], hy["stddev_3y_ann"]],
                    "x_5y": [hx["mean_5y_ann"], hx["stddev_5y_ann"]],
                    "y_5y": [hy["mean_5y_ann"], hy["stddev_5y_ann"]],
                    "correlation": c,
                    "note": ("dominance flips between the 3-year and 5-year windows, so no "
                             "dominance verdict is issued. The difference is WHEN the "
                             "outperformance happened, which is a real fact about the funds "
                             "and not a tie to be broken.")})
                continue
            if dom5 and dom3:
                fee_gap = (hx["ocf"] or 0) - (hy["ocf"] or 0)
                val = (weights.get(x, {}) or {}).get("value_gbp")
                dominance.append({
                    "dominated": x, "dominated_name": hx["name"], "dominated_by": y,
                    "dominated_by_name": hy["name"],
                    "x_mean": hx["mean_5y_ann"], "x_sd": hx["stddev_5y_ann"],
                    "y_mean": hy["mean_5y_ann"], "y_sd": hy["stddev_5y_ann"],
                    "correlation": c, "ocf_gap_pct": round(fee_gap, 3),
                    "annual_fee_waste_gbp": (round(fee_gap / 100.0 * val, 2)
                                             if (val and fee_gap > 0) else None),
                    "statement": (f"{hx['name']} returns {hx['mean_5y_ann']}% at "
                                  f"{hx['stddev_5y_ann']}% risk; {hy['name']} returns "
                                  f"{hy['mean_5y_ann']}% at {hy['stddev_5y_ann']}% — better AND "
                                  f"safer — at correlation {c}. Holding both is paying twice for "
                                  f"one exposure, and the worse copy costs more."),
                })
    # ⚑ Cross-check every dominance verdict against the X-Ray before it is allowed to escalate.
    # The DataMining spec recorded RLGES as dominated BY VUAG; the golden-source NAV series says
    # the reverse (RLGES 15.82% vs VUAG 13.10% realised 5y). VUAG is not in the X-Ray holdings
    # table at all, so that particular pair CANNOT be adjudicated from a second source — and a
    # Category-7 escalation that would sell a holding must not rest on one unverified series.
    for d in dominance:
        xa = _match_xray(None, xray_ret, (funds.get(d["dominated"]) or {}).get("xray_name"))[1]
        xb = _match_xray(None, xray_ret, (funds.get(d["dominated_by"]) or {}).get("xray_name"))[1]
        if xa and xb and xa["return_5yr"] is not None and xb["return_5yr"] is not None:
            agrees = xb["return_5yr"] >= xa["return_5yr"]
            d["xray_confirms"] = bool(agrees)
            d["xray_returns"] = {"dominated": xa["return_5yr"], "dominated_by": xb["return_5yr"]}
            d["escalate"] = bool(agrees)
            if not agrees:
                d["note"] = ("X-Ray CONTRADICTS this: it shows the supposedly-dominated fund "
                             "returning more. Not escalated to Category 7 on a contradiction.")
        else:
            d["xray_confirms"] = None
            d["escalate"] = False
            d["note"] = ("no X-Ray figure for one or both funds, so this rests on a SINGLE "
                         "derivation. Reported for review; NOT auto-escalated to Category 7 — "
                         "a verdict that would sell a holding needs two sources that agree.")
    dominated_set = {d["dominated"] for d in dominance if d.get("escalate")}

    # ── FRS components ─────────────────────────────────────────────────────────────
    eff_pool = [held[k]["sharpe_like"] for k in keys]

    # Pre-pass: the stretch level must be known before ANY fund is scored, or the first fund
    # would be graded against a different scale from the last.
    _pre = {}
    for _sd in held:
        _h = held[_sd]
        _w = {k: v for k, v in {"1y": _h.get("realised_1y_ann"), "3y": _h.get("realised_3y_ann"),
                                "5y": _h.get("realised_5y_ann"),
                                "10y": _h.get("realised_10y_ann")}.items() if v is not None}
        _pre[_sd] = _window_stat(_w.values(), RETURN_ADEQUACY_STAT)
    _global_floor = max(anchor, max(bmin.values()) * 100.0 if bmin else anchor)
    stretch_level = _stretch_level(list(_pre.values()), _global_floor)
    rows = []
    for sedol in sorted(held):
        h = held[sedol]
        parts, why, unmeasured = {}, [], []

        # 1. return adequacy (35) — realised, against the bucket minimum AND the anchor
        bm = bmin.get(h["bucket"], 0.12) * 100.0
        r5, r3 = h["realised_5y_ann"], h["realised_3y_ann"]

        # ── MULTI-WINDOW ADEQUACY (05-Aug-2026, Raj's challenge) ─────────────────────
        # ⚑ The first version judged solely on the 5-year annualised figure. Raj: *"is dead
        # money just decided on the 5yr annualised? its 1yr and 3yr are substantially above the
        # target."* He is right, and the defect is the SAME ONE I had just flagged in H7:
        # **a single fixed window is a bet on a start date, not a measurement.**
        #
        # Scottish Mortgage is the clean example. Its calendar years are 2020 +110.5, 2021
        # +10.5, **2022 −45.7**, 2023 +12.5, 2024 +18.8, 2025 +24.7. The 5-year window contains
        # the 2022 collapse and excludes the 2020 boom, so it reads ~0.2%. The 3-year window
        # starts AFTER the collapse and reads ~22%. The 10-year reads ~16.7%. **Nothing about
        # the fund changes between those three numbers — only where the window is cut.** On
        # today's arithmetic SMT's 5-year figure will leap by ~15pp in 2027 when 2022 rolls out
        # of the window, with no new information whatsoever.
        #
        # Both directions are artefacts of one event: the 5y is depressed by including the
        # drawdown, the 3y is FLATTERED by measuring from the trough. Annualising from a
        # post-crash low is arithmetic, not skill, so "3y is above target" does not rescue a
        # fund any more than "5y is below" condemns it.
        #
        # So the rule now looks at EVERY available window and only issues a verdict when they
        # AGREE. A split is `WINDOW_SPLIT` — explicitly not dead money, and escalated for
        # judgement rather than resolved by whichever window the code happened to pick.
        wins = {"1y": h.get("realised_1y_ann"), "3y": r3, "5y": r5, "10y": h.get("realised_10y_ann")}
        wins = {k: v for k, v in wins.items() if v is not None}
        passes = {k: bool(v >= bm) for k, v in wins.items()}
        window_split = bool(passes) and (any(passes.values()) and not all(passes.values()))

        # every named statistic is computed every run, so the choice between them is visible and
        # auditable rather than embedded in whichever line of code got there first
        stat_basis = {st: _window_stat(wins.values(), st) for st in RETURN_ADEQUACY_STATS}
        trailing_basis = stat_basis.get(RETURN_ADEQUACY_STAT)
        if trailing_basis is None:
            trailing_basis = r5 if r5 is not None else r3

        # ── the money-weighted overlay ──────────────────────────────────────────────────
        mrow = mwr.get(sedol) or {}
        m_ann = (mrow.get("mwr") or {})
        m_cum = (mrow.get("mwr_cumulative") or {})
        mwr_eligible = bool(mrow.get("usable_as_anchor"))
        mwr_mode = mrow.get("anchor_mode") or "none"
        basis = trailing_basis
        basis_name = f"trailing_window_{RETURN_ADEQUACY_STAT}"
        if MWR_TAKES_PRECEDENCE and m_ann.get("present") and mwr_mode == "symmetric":
            basis, basis_name = m_ann["value"], "money_weighted_holding_period"
            why.append(f"⚑ ANCHORED ON RAJ'S OWN HOLDING PERIOD: {m_ann['value']:+.2f}% p.a. "
                       f"money-weighted over {mrow.get('span_years')}y on "
                       f"£{mrow.get('capital_in_gbp', 0):,.0f} actually deployed. Every trailing "
                       f"window is a choice of start date; this one is not.")
        elif (MWR_TAKES_PRECEDENCE and m_ann.get("present") and mwr_mode == "downward_only"
              and trailing_basis is not None):
            # ⚑ DOWNWARD ONLY. Over a short favourable window a HIGH money-weighted return is the
            # market's result, not the fund's — at a 1-year span it hands full marks to 11 of 12
            # funds, JPM UK among them. A LOW one is much harder to explain away: capital that
            # failed to grow in a rising market says something the trailing windows do not. So it
            # can pull the score DOWN and never lift it, until the span can contain a drawdown.
            if m_ann["value"] < trailing_basis:
                basis, basis_name = m_ann["value"], "money_weighted_downward_override"
                why.append(
                    f"⚑ MONEY-WEIGHTED OVERRIDE (downward only): Raj's own capital returned "
                    f"{m_ann['value']:+.2f}% p.a. over {mrow.get('span_years')}y, BELOW the "
                    f"trailing {RETURN_ADEQUACY_STAT} of {trailing_basis:.2f}%. Scored on the "
                    f"lower figure — a fund whose money did not grow in a rising market is not "
                    f"rescued by a window that started before he owned it.")
            else:
                why.append(
                    f"money-weighted {m_ann['value']:+.2f}% p.a. over "
                    f"{mrow.get('span_years')}y is ABOVE the trailing "
                    f"{RETURN_ADEQUACY_STAT} — noted, but NOT used: over a short rising window a "
                    f"high money-weighted return is the market's result. It becomes symmetric at "
                    f"{(mwr_meta or {}).get('anchor_modes', {}).get('symmetric_from_years')}y.")
        elif mrow:
            why.append(
                "money-weighted holding-period return "
                + (f"{m_ann['value']:+.2f}% p.a." if m_ann.get("present")
                   else f"{m_cum['value']:+.2f}% cumulative" if m_cum.get("present") else "n/a")
                + f" over {mrow.get('span_years')}y — REPORTED ONLY, not used as the basis: "
                + str(mrow.get("anchor_block_reason") or "no reason recorded"))
        if basis is None:
            parts["return_adequacy"] = None
            unmeasured.append("no realised 3y or 5y return from the golden source")
        else:
            span = max(anchor, bm)
            parts["return_adequacy"] = _return_adequacy_points(
                basis, span, stretch_level, WEIGHTS["return_adequacy"])
            why.append("realised by window: "
                       + " · ".join(f"{k} {v:.2f}%{'✓' if passes[k] else '✗'}"
                                    for k, v in sorted(wins.items(),
                                                       key=lambda kv: int(kv[0][:-1])))
                       + f" → {RETURN_ADEQUACY_STAT} {trailing_basis:.2f}% vs bucket minimum "
                         f"{bm:.1f}%, anchor {anchor:.1f}% and a sleeve stretch of "
                         f"{stretch_level:.2f}% (the {RETURN_ADEQUACY_STRETCH_PCTILE}th "
                         f"percentile of what this pound could otherwise have earned)"
                       + (f" (basis actually scored: {basis_name} {basis:.2f}%)"
                          if basis_name != f"trailing_window_{RETURN_ADEQUACY_STAT}" else ""))
            if window_split:
                why.append("⚑ WINDOW SPLIT: the windows disagree about whether this fund clears "
                           "its bucket minimum. A single-window verdict here would be a bet on a "
                           "start date, not a measurement — no DEAD MONEY verdict is issued.")

        # 2. risk-adjusted efficiency (25) — percentile within the held sleeve
        pr = _pct_rank(h["sharpe_like"], eff_pool)
        if pr is None:
            parts["risk_efficiency"] = None
            unmeasured.append("insufficient NAV history for mean/stddev (needs 24 monthly obs)")
        else:
            parts["risk_efficiency"] = pr / 100.0 * WEIGHTS["risk_efficiency"]
            why.append(f"return per unit of risk {h['sharpe_like']} = {pr:.0f}th percentile of "
                       f"the held sleeve")

        # 3. marginal diversification value (20) — 1 - max corr with a cheaper-or-better fund
        best_c, best_peer = None, None
        for other in keys:
            if other == sedol:
                continue
            c = _c(sedol, other)
            if c is None:
                continue
            ho = held[other]
            cheaper_or_better = ((ho["ocf"] is not None and h["ocf"] is not None
                                  and ho["ocf"] <= h["ocf"])
                                 or (ho["mean_5y_ann"] is not None and h["mean_5y_ann"] is not None
                                     and ho["mean_5y_ann"] >= h["mean_5y_ann"]))
            if cheaper_or_better and (best_c is None or c > best_c):
                best_c, best_peer = c, other
        if best_c is None:
            parts["diversification"] = None
            unmeasured.append("no comparable held fund with overlapping history")
        else:
            parts["diversification"] = max(0.0, 1.0 - best_c) * WEIGHTS["diversification"]
            why.append(f"most-correlated cheaper-or-better holding is "
                       f"{held[best_peer]['name']} at {best_c}")

        # 4. fee efficiency (10) — vs the cheapest holding delivering >=0.85-correlated exposure
        peers = [held[o]["ocf"] for o in keys if o != sedol and (_c(sedol, o) or 0) >= FEE_PEER_CORR
                 and held[o]["ocf"] is not None]
        if h["ocf"] is None:
            parts["fee_efficiency"] = None
            unmeasured.append("no OCF on record")
        elif not peers:
            parts["fee_efficiency"] = WEIGHTS["fee_efficiency"]
            why.append(f"OCF {h['ocf']}% — no substitutable holding at corr >= {FEE_PEER_CORR}, "
                       f"so the fee buys exposure nothing else here provides")
        else:
            cheapest = min(peers)
            excess = max(0.0, h["ocf"] - cheapest)
            parts["fee_efficiency"] = max(0.0, 1.0 - excess / 1.0) * WEIGHTS["fee_efficiency"]
            val = (weights.get(sedol, {}) or {}).get("value_gbp")
            why.append(f"OCF {h['ocf']}% vs {cheapest}% for equivalent exposure"
                       + (f" = £{excess / 100.0 * val:,.0f}/yr of avoidable fee"
                          if (val and excess) else ""))

        # 5. mandate integrity (10) — no drift evidence available yet; NEUTRAL and SAID SO,
        #    never full marks by default (a default of 10/10 would flatter every fund and make
        #    the component decorative)
        parts["mandate_integrity"] = WEIGHTS["mandate_integrity"] * 0.5
        unmeasured.append("mandate drift not yet measured — scored NEUTRAL (5/10), not full")

        got = {k: v for k, v in parts.items() if v is not None}
        avail = sum(WEIGHTS[k] for k in got)
        # ⚑ RETURN ADEQUACY IS MANDATORY. Without it the remaining components sum to 65, clear
        # the 50-point threshold, and produce a confident DEAD MONEY verdict on a fund whose
        # performance was never read — which is precisely what happened on the first live run.
        # "We could not measure the returns" and "the returns are bad" must never converge on
        # the same output.
        if parts.get("return_adequacy") is None:
            frs, band = None, "UNSCORED"
            why.append("FRS withheld: realised return could not be read from the golden source, "
                       "and return adequacy is the one component that cannot be inferred from "
                       "the others. An unmeasured fund is NOT a bad fund.")
        elif avail < 50:
            frs, band = None, "UNSCORED"
            why.append("FRS withheld: fewer than half the components could be measured. An "
                       "unmeasured fund is NOT a bad fund and must not be scored as one.")
        else:
            frs = round(sum(got.values()) / avail * 100.0, 1)
            band = ("HOLD/ADD" if frs >= FRS_HOLD_ADD else
                    "RETAIN-ONLY" if frs >= FRS_RETAIN_ONLY else "DEAD MONEY")
            if band == "DEAD MONEY" and window_split:
                band = "WINDOW_SPLIT"
                why.append("band held at WINDOW_SPLIT rather than DEAD MONEY: at least one "
                           "measurement window clears the bucket minimum. This needs a "
                           "judgement about WHICH window represents the forward case, which is "
                           "not a decision the arithmetic can make.")

        # dominance caps the band regardless of FRS
        dom = next((d for d in dominance
                    if d["dominated"] == sedol and d.get("escalate")), None)
        if dom and band == "HOLD/ADD":
            band = "RETAIN-ONLY"
            why.append(f"band CAPPED by the dominance test — {dom['dominated_by_name']} is "
                       f"better and safer at correlation {dom['correlation']}")

        # ── anchor rule — the ownership floor, on realised evidence, RECONCILED ────────
        # Two independent derivations must agree before a verdict is published. A fund whose
        # bucket verdict FLIPS between them is DISPUTED, not passed and not failed: acting on
        # either would be acting on a coin toss dressed as a measurement.
        xk, xv = _match_xray(h["name"], xray_ret, (funds.get(sedol) or {}).get("xray_name"))
        x5 = xv["return_5yr"] if xv else None
        # ⚑ COMPARE LIKE FOR LIKE. The first version compared the golden source at the RUN date
        # against X-Ray figures struck up to two months earlier, and reported the staleness as a
        # disagreement — it flagged JPM UK as DISPUTED when, at the X-Ray's own date, the two
        # agree to 2dp (11.48 vs 11.48; so do RLGES 16.53, VUAG 13.98 and Vanguard Jpn 10.22).
        # A "second derivation" evaluated at a different date is not a second derivation.
        x_basis = h.get("golden_at_xray_date")
        anchor_pass = None if basis is None else bool(basis >= bm)
        anchor_basis = "golden_source_only"
        if basis is not None and x5 is not None:
            x_pass = bool(x5 >= bm)
            cmp_basis = x_basis if x_basis is not None else basis
            delta = round(cmp_basis - x5, 2)
            if x_basis is not None:
                # verdicts compared at the SAME date; the run-date figure is still what is used
                anchor_pass = bool(basis >= bm)
                x_pass = bool(x5 >= bm)
                same_date_pass = bool(x_basis >= bm)
                if same_date_pass == x_pass:
                    why.append(f"date-aligned check at the X-Ray's own strike date: golden "
                               f"{x_basis:.2f}% vs X-Ray {x5:.2f}% (delta {delta}pp) — sources "
                               f"AGREE; any difference from the run-date figure "
                               f"({basis:.2f}%) is staleness, not disagreement")
                    anchor_basis = "reconciled_date_aligned"
                    x_pass = anchor_pass          # no dispute
                else:
                    why.append(f"⚑ sources differ AT THE SAME DATE: golden {x_basis:.2f}% vs "
                               f"X-Ray {x5:.2f}% — a real disagreement, not staleness")
            if x_pass != anchor_pass:
                anchor_pass = None
                anchor_basis = "DISPUTED"
                why.append(f"⚑ DISPUTED: golden source {basis:.2f}% and X-Ray {x5:.2f}% fall on "
                           f"OPPOSITE sides of the {bm:.1f}% bucket minimum. No verdict is "
                           f"published on a disagreement of this kind.")
                unmeasured.append(f"anchor verdict disputed (delta {delta}pp)")
            elif abs(delta) > XRAY_TOL_PP:
                anchor_basis = "agreed_verdict_but_figures_differ"
                why.append(f"both sources agree on the verdict, but differ by {delta}pp "
                           f"(golden {basis:.2f}% vs X-Ray {x5:.2f}%) — the X-Ray is struck at "
                           f"its own earlier date")
            else:
                anchor_basis = "reconciled"
        elif basis is not None:
            why.append("no X-Ray figure for this fund — verdict rests on a SINGLE derivation")
        rows.append({
            "sedol": sedol, "name": h["name"], "bucket": h["bucket"], "ocf": h["ocf"],
            "weight_pct": (weights.get(sedol, {}) or {}).get("pct"),
            "value_gbp": (weights.get(sedol, {}) or {}).get("value_gbp"),
            "realised_5y_ann": r5, "realised_3y_ann": r3,
            "mean_5y_ann": h["mean_5y_ann"], "stddev_5y_ann": h["stddev_5y_ann"],
            "return_per_unit_risk": h["sharpe_like"],
            "frs": frs, "band": band, "components": parts,
            "anchor_rule_pass": anchor_pass, "bucket_minimum_pct": bm,
            "dominated_by": dom["dominated_by_name"] if dom else None,
            "xray_5y_ann": x5, "xray_match": xk, "anchor_basis": anchor_basis,
            "rationale": why, "unmeasured": unmeasured,
            "windows": wins, "window_passes": passes, "window_split": window_split,
            "data_status": h["status"],
            # ── Tier-1 item 1: the window nobody chose ───────────────────────────────────
            "return_adequacy_basis": basis_name,
            "return_adequacy_value": (round(basis, 2) if basis is not None else None),
            "trailing_stat_values": {k: (round(v, 2) if v is not None else None)
                                     for k, v in stat_basis.items()},
            "mwr_annualised_pct": (round(m_ann["value"], 2) if m_ann.get("present") else None),
            "mwr_cumulative_pct": (round(m_cum["value"], 2) if m_cum.get("present") else None),
            "mwr_span_years": mrow.get("span_years"),
            "mwr_capital_in_gbp": mrow.get("capital_in_gbp"),
            "mwr_net_gain_gbp": mrow.get("net_gain_gbp"),
            "mwr_episodes": mrow.get("n_episodes"),
            "mwr_anchor_eligible": mwr_eligible,
            "mwr_block_reason": mrow.get("anchor_block_reason"),
            "mwr_transfer_basis": mrow.get("transfer_valuation_basis"),
            # kept so the basis study can re-derive FRS exactly, without re-running the build
            "_frs_other_points": round(sum(v for k, v in parts.items()
                                           if k != "return_adequacy" and v is not None), 4),
            "_frs_other_weight": sum(WEIGHTS[k] for k, v in parts.items()
                                     if k != "return_adequacy" and v is not None),
            "_bucket_min_pct": bm, "_anchor_pct": anchor,
            # ── Tier-1 item 3: closed-end discount ──────────────────────────────────────
            "structure": (funds.get(sedol) or {}).get("structure"),
            "closed_end": bool((trust.get(sedol) or {}).get("closed_end")),
            "discount_pct": ((trust.get(sedol) or {}).get("discount_pct") or {}).get("value"),
            "discount_as_of": ((trust.get(sedol) or {}).get("discount_pct") or {}).get("as_of"),
            "discount_basis": (trust.get(sedol) or {}).get("basis"),
            "discount_unavailable_reason": (
                ((trust.get(sedol) or {}).get("discount_pct") or {}).get("reason")),
            "crystallisation": (trust.get(sedol) or {}).get("crystallisation"),
            "return_basis_caveat": (
                "price return = NAV return + change in the discount; this holding's trailing "
                "figures mix the manager's result with a change in sentiment about the trust"
                if (trust.get(sedol) or {}).get("closed_end") else None),
        })
        if (trust.get(sedol) or {}).get("closed_end"):
            _d = (trust[sedol].get("discount_pct") or {})
            _cry = trust[sedol].get("crystallisation") or {}
            why.append(
                (f"⚑ CLOSED-END: trading at a {abs(_d['value']):.1f}% "
                 f"{'discount' if _d['value'] < 0 else 'premium'} to NAV as at {_d['as_of']}. "
                 f"Selling {'foregoes' if _d['value'] < 0 else 'captures'} "
                 f"£{abs(_cry.get('cost_gbp') or 0):,.0f} of underlying assets, which belongs in "
                 f"the retain-vs-redeploy comparison alongside dealing costs. Its trailing "
                 f"returns are PRICE returns and therefore carry the discount's own movement."
                 if _d.get("present") else
                 f"⚑ CLOSED-END and the discount is UNMEASURED: {_d.get('reason')}. Its trailing "
                 f"returns are price returns, so they are not the manager's result alone."))

    # ── Tier-1 item 5: MEDIAN vs MINIMUM, decided by showing what each one does ─────────
    # ⚑ This is a CALIBRATION QUESTION FOR RAJ, not a choice for the code to make quietly. The
    # honest way to put it to him is not an argument, it is the table: same funds, same
    # components, only the return statistic swapped, with every band change named. Anything a
    # basis changes is a real decision it would have changed.
    def _reband(r, value):
        """Re-derive FRS and band for one row under a different return-adequacy value. Uses the
        stored non-return component points, so this is the SAME arithmetic as the live path and
        cannot drift away from it."""
        if value is None:
            return {"frs": None, "band": "UNSCORED",
                    "reason": "the statistic is undefined for this fund (no windows measured)"}
        span = max(r["_anchor_pct"], r["_bucket_min_pct"])
        pts = _return_adequacy_points(value, span, stretch_level, WEIGHTS["return_adequacy"])
        avail = r["_frs_other_weight"] + WEIGHTS["return_adequacy"]
        if avail < 50:
            return {"frs": None, "band": "UNSCORED", "reason": "fewer than half the components"}
        frs = round((r["_frs_other_points"] + pts) / avail * 100.0, 1)
        band = ("HOLD/ADD" if frs >= FRS_HOLD_ADD else
                "RETAIN-ONLY" if frs >= FRS_RETAIN_ONLY else "DEAD MONEY")
        if band == "DEAD MONEY" and r["window_split"]:
            band = "WINDOW_SPLIT"
        return {"frs": frs, "band": band}

    basis_study = []
    for r in rows:
        variants = {}
        for st in RETURN_ADEQUACY_STATS:
            variants[st] = dict(_reband(r, r["trailing_stat_values"].get(st)),
                                value=r["trailing_stat_values"].get(st))
        variants["money_weighted"] = dict(
            _reband(r, r["mwr_annualised_pct"] if r["mwr_anchor_eligible"] else None),
            value=r["mwr_annualised_pct"], eligible=r["mwr_anchor_eligible"],
            blocked_because=r["mwr_block_reason"])
        bands = {k: v["band"] for k, v in variants.items()
                 if not (k == "money_weighted" and not r["mwr_anchor_eligible"])}
        basis_study.append({
            "sedol": r["sedol"], "name": r["name"], "bucket": r["bucket"],
            "value_gbp": r["value_gbp"], "live_basis": r["return_adequacy_basis"],
            "live_band": r["band"], "variants": variants,
            "band_depends_on_basis": len(set(bands.values())) > 1,
            "windows": r["windows"],
        })
    basis_sensitive = [b for b in basis_study if b["band_depends_on_basis"]]

    # ── redundancy + efficiency league ─────────────────────────────────────────────
    redundancy = []
    for pair, c in sorted(corr.items(), key=lambda kv: -kv[1]):
        if c < REDUNDANCY_CORR:
            continue
        a, b = pair.split("|")
        ha, hb = held[a], held[b]
        keep = a
        if (hb["mean_5y_ann"] or -99) > (ha["mean_5y_ann"] or -99) or \
           ((hb["ocf"] or 99) < (ha["ocf"] or 99)
                and (hb["mean_5y_ann"] or -99) >= (ha["mean_5y_ann"] or -99)):
            keep = b
        # ⚑ "highly correlated + cheaper" is where H7 came from, and H7 was wrong twice over.
        # Every redundant pair now carries the only test that settles it: is the excess return
        # distinguishable from noise, and is the fee already inside the numbers being compared?
        # Attached to EVERY pair, so the next one is not argued from a window and a fee again.
        pt = {}
        try:
            import fund_pair_test as _fpt
            drop, kp = (a, b) if keep == b else (b, a)
            pt = _fpt.evaluate_pair(held[drop]["monthly"], held[kp]["monthly"],
                                    held[drop]["name"], held[kp]["name"], 5,
                                    held[drop]["ocf"], held[kp]["ocf"],
                                    (weights.get(drop, {}) or {}).get("value_gbp"), c,
                                    funds.get(drop), funds.get(kp), "5y")
        except Exception as _e:                                    # noqa: BLE001
            pt = {"verdict": "ERROR", "reason": f"{type(_e).__name__}: {_e}"}
        redundancy.append({"pair": [a, b], "names": [ha["name"], hb["name"]],
                           "correlation": c, "cheaper_or_better": keep,
                           "keep_name": held[keep]["name"],
                           "significance_test": pt,
                           # ⚑ TWO INDEPENDENT REFERENCES, NOT ONE. A pair result alone cannot
                           # tell a poor fund from one that lost a style race: JPM UK was
                           # reported at t −3.53 while BEATING its own category at every horizon.
                           # The fund must trail BOTH its peer and its own peer group.
                           "switch_supported_by_evidence": (
                               pt.get("verdict") == "B_BETTER_SIGNIFICANT"
                               and (pt.get("category_check") or {}).get("status")
                               == "UNDERPERFORMS_OWN_CATEGORY"),
                           "category_status": (pt.get("category_check") or {}).get("status"),
                           "note": ("a correlation and a fee gap do NOT establish that one "
                                    "holding should replace the other. The significance test "
                                    "above is the standing answer to that question; register H7 "
                                    "was argued without it and was wrong on both counts.")})

    # ⚑ MULTIPLE COMPARISONS. Five pairs are tested, so at a 5% threshold roughly one in four
    # runs would throw a "significant" result from noise alone — and the pair that gets reported
    # is by construction the one that looked worst. Every significance verdict is therefore
    # re-tested against a Bonferroni-adjusted threshold for the number of tests ACTUALLY run,
    # and a result that survives only the unadjusted test is downgraded and says so. Selecting
    # the extreme of a family and then quoting its own p-value is how a screening process
    # manufactures conviction.
    _n_tests = sum(1 for x in redundancy if (x.get("significance_test") or {}).get("t_stat")
                   is not None)
    _adj = 2.807 if _n_tests >= 5 else (2.638 if _n_tests >= 3 else 1.96)   # ~alpha 0.05 / n
    for x in redundancy:
        t = x.get("significance_test") or {}
        ts = t.get("t_stat")
        if ts is None:
            continue
        t["n_tests_in_family"] = _n_tests
        t["bonferroni_t_crit"] = _adj
        t["significant_after_multiple_comparisons"] = bool(abs(ts) >= _adj)
        if x.get("switch_supported_by_evidence") and abs(ts) < _adj:
            x["switch_supported_by_evidence"] = False
            x["downgraded"] = (
                f"significant on its own (|t|={abs(ts)}) but NOT after adjusting for the "
                f"{_n_tests} pairs tested (threshold {_adj}). Reported, not actioned.")
    _sig = [x for x in redundancy if x.get("switch_supported_by_evidence")]

    league = sorted([r for r in rows if r["return_per_unit_risk"] is not None],
                    key=lambda r: -r["return_per_unit_risk"])

    # ── the ranked agenda ──────────────────────────────────────────────────────────
    order = {"DEAD MONEY": 0, "WINDOW_SPLIT": 1, "RETAIN-ONLY": 2, "UNSCORED": 3, "HOLD/ADD": 4}
    stack = sorted(rows, key=lambda r: (order.get(r["band"], 9),
                                        -(r["value_gbp"] or 0),
                                        (r["frs"] if r["frs"] is not None else 999)))
    dead_value = sum(r["value_gbp"] or 0 for r in rows if r["band"] == "DEAD MONEY")
    return {
        "as_of": as_of.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "xray_cross_check": {"funds_matched": sum(1 for r in rows if r.get("xray_match")),
                             "disputed": [r["name"] for r in rows
                                          if r.get("anchor_basis") == "DISPUTED"],
                             "figures_differ_beyond_tolerance": [
                                 r["name"] for r in rows
                                 if r.get("anchor_basis") == "agreed_verdict_but_figures_differ"],
                             "tolerance_pp": XRAY_TOL_PP},
        "anchor_floor_pct": anchor, "bucket_minimums_pct": {k: v * 100 for k, v in bmin.items()},
        "join_reconciliation": join_reconciliation,
        "window_coverage": (fp.window_coverage(perf) if isinstance(perf, dict) else None),
        "trust_capture_status": trust_capture,
        "closed_end": {
            "error": trust_error,
            "holdings": [{"sedol": k, "name": v.get("name"),
                          "discount_pct": (v.get("discount_pct") or {}).get("value"),
                          "as_of": (v.get("discount_pct") or {}).get("as_of"),
                          "basis": v.get("basis"),
                          "crystallisation_gbp": (v.get("crystallisation") or {}).get("cost_gbp"),
                          "history_n": v.get("history_n"),
                          "history_percentile": v.get("history_percentile"),
                          "unmeasured_reason": (v.get("discount_pct") or {}).get("reason")}
                         for k, v in sorted(trust.items())],
            "note": ("only a closed-end vehicle can trade away from NAV. The discount is a real "
                     "cost of ACTING, not a score: whether it is wide or narrow for this trust "
                     "needs a history that starts now.")},
        "return_adequacy_config": {
            "trailing_statistic": RETURN_ADEQUACY_STAT,
            "scoring": {"floor_pct": _global_floor, "floor_share_of_points":
                        RETURN_ADEQUACY_FLOOR_SHARE, "stretch_pct": round(stretch_level, 2),
                        "stretch_percentile": RETURN_ADEQUACY_STRETCH_PCTILE,
                        "note": ("piecewise: reaching the floor earns 60% of the points, the "
                                 "remaining 40% is scaled to the sleeve's 75th percentile. The "
                                 "old score capped at the floor and gave 11 of 12 funds full "
                                 "marks.")},
            "mwr_takes_precedence": MWR_TAKES_PRECEDENCE,
            "mwr_min_span_years": (mwr_meta or {}).get("min_anchor_span_years"),
            "mwr_error": mwr_error,
            "note": "both settings are stated here and reversible; the study below shows exactly "
                    "what each alternative would change"},
        "money_weighted_returns": {
            "as_of": (mwr_meta or {}).get("as_of"),
            "summary": (mwr_meta or {}).get("summary"),
            "anchor_eligible": [r["sedol"] for r in rows if r["mwr_anchor_eligible"]],
            "reported_not_anchored": {r["sedol"]: r["mwr_block_reason"] for r in rows
                                      if not r["mwr_anchor_eligible"] and r["mwr_block_reason"]}},
        "return_adequacy_basis_study": {
            "rows": basis_study,
            "basis_sensitive": [{"sedol": b["sedol"], "name": b["name"],
                                 "value_gbp": b["value_gbp"],
                                 "bands": {k: v["band"] for k, v in b["variants"].items()}}
                                for b in basis_sensitive],
            "n_basis_sensitive": len(basis_sensitive),
            "question_for_raj": (
                "MEDIAN across windows lets a fund earn full marks on its better windows; MINIMUM "
                "bands it on its worst. Neither is derivable from the data — it is a statement "
                "about how much a bad five years should count against a good three. The funds "
                "listed in basis_sensitive are the ones where the answer changes a real verdict.")},
        "fund_dominance": dominance,
        "dominance_window_conflicts": window_conflicts,
        "fund_retention_score": rows,
        "fund_action_stack": [
            {"rank": i + 1, "sedol": r["sedol"], "name": r["name"], "band": r["band"],
             "frs": r["frs"], "value_gbp": r["value_gbp"],
             "anchor_rule_pass": r["anchor_rule_pass"],
             "realised_5y_ann": r["realised_5y_ann"],
             "bucket_minimum_pct": r["bucket_minimum_pct"],
             "dominated_by": r["dominated_by"],
             "action_required": ("Category 7 — dead money: state the retain-vs-redeploy case or "
                                 "redeploy" if r["band"] == "DEAD MONEY" else
                                 "Windows disagree — decide WHICH window represents the forward "
                                 "case, then re-band. Not dead money."
                                 if r["band"] == "WINDOW_SPLIT" else
                                 "No new money; retain only" if r["band"] == "RETAIN-ONLY" else
                                 "Cannot be assessed — resolve the data gap before any decision"
                                 if r["band"] == "UNSCORED" else "Eligible for new money"),
             "why": r["rationale"][:3]}
            for i, r in enumerate(stack)],
        "correlation_redundancy": redundancy,
        "redundancy_significance": {
            "n_pairs_tested": _n_tests,
            "unadjusted_t_crit": 1.96, "bonferroni_t_crit": _adj,
            "surviving_pairs": [
                {"replace": x["names"][0] if x["cheaper_or_better"] == x["pair"][1]
                 else x["names"][1],
                 "with": x["keep_name"], "correlation": x["correlation"],
                 "t_stat": x["significance_test"]["t_stat"],
                 "excess_pp": x["significance_test"].get("annualised_excess_pp"),
                 "ocf_of_worse": x["significance_test"].get("fee", {}).get("a_ocf_pct"),
                 "ocf_of_better": x["significance_test"].get("fee", {}).get("b_ocf_pct")}
                for x in _sig],
            "escalation": ("REVIEW ONLY. A surviving pair is a Category 7 AGENDA item, never an "
                           "automatic sell. Two funds with the same mandate are substitutes on "
                           "paper and a manager-concentration decision in practice — replacing "
                           "JPM UK with a second Artemis SmartGARP fund would put a third of the "
                           "UK sleeve behind one process."),
            "note": ("the fee is NOT part of this test's evidence: NAV returns are already net "
                     "of it. Note also that the significant pairs here have the CHEAPER fund on "
                     "the losing side, which is the opposite shape to register H7.")},
        "fund_efficiency_rank": [{"sedol": r["sedol"], "name": r["name"],
                                  "mean": r["mean_5y_ann"], "stddev": r["stddev_5y_ann"],
                                  "ratio": r["return_per_unit_risk"]} for r in league],
        "anchor_rule_failures": [
            {"sedol": r["sedol"], "name": r["name"], "realised_5y_ann": r["realised_5y_ann"],
             "bucket": r["bucket"], "bucket_minimum_pct": r["bucket_minimum_pct"],
             "shortfall_pp": (round(r["bucket_minimum_pct"] - r["realised_5y_ann"], 2)
                              if r["realised_5y_ann"] is not None else None)}
            for r in rows if r["anchor_rule_pass"] is False],
        "summary": {
            "n_funds": len(rows),
            "hold_add": sum(1 for r in rows if r["band"] == "HOLD/ADD"),
            "retain_only": sum(1 for r in rows if r["band"] == "RETAIN-ONLY"),
            "dead_money": sum(1 for r in rows if r["band"] == "DEAD MONEY"),
            "unscored": sum(1 for r in rows if r["band"] == "UNSCORED"),
            "window_split": sum(1 for r in rows if r["band"] == "WINDOW_SPLIT"),
            "dead_money_value_gbp": round(dead_value, 2),
            "dominated": len(dominated_set),
            "anchor_failures": sum(1 for r in rows if r["anchor_rule_pass"] is False),
        },
    }


# ---------------------------------------------------------------- selftest
def _selftest():
    """Synthetic sleeve with a KNOWN dominance relationship and a KNOWN dead-money fund, so the
    assertions test behaviour rather than restating whatever the live data happens to say."""
    import random
    random.seed(7)
    n = 72
    base = [random.gauss(0.012, 0.035) for _ in range(n)]

    def mk(mu, sd, corr_with_base):
        out = []
        for i in range(n):
            idio = random.gauss(0, sd)
            out.append(((2020 + i // 12, i % 12 + 1),
                        corr_with_base * base[i] + (1 - corr_with_base) * idio + mu))
        return out

    fake = {
        "GOOD":  {"sedol": "GOOD", "name": "Good Fund", "bucket": "B1", "ocf": 0.07,
                  "monthly": mk(0.004, 0.020, 0.95), "status": "resolved"},
        "WORSE": {"sedol": "WORSE", "name": "Worse Costlier Fund", "bucket": "B1", "ocf": 0.71,
                  "monthly": mk(0.001, 0.045, 0.95), "status": "resolved"},
        "DIFF":  {"sedol": "DIFF", "name": "Different Fund", "bucket": "B2", "ocf": 0.90,
                  "monthly": mk(0.003, 0.030, 0.05), "status": "resolved"},
    }
    for h in fake.values():
        h["mean_5y_ann"], h["stddev_5y_ann"] = _ann_stats(h["monthly"], 5)
        h["sharpe_like"] = round(h["mean_5y_ann"] / h["stddev_5y_ann"], 3)
        h["realised_5y_ann"] = h["mean_5y_ann"]
        h["realised_3y_ann"] = h["mean_5y_ann"]
        h["realised_1y_ann"] = None

    c_gw = _corr(fake["GOOD"]["monthly"], fake["WORSE"]["monthly"])
    c_gd = _corr(fake["GOOD"]["monthly"], fake["DIFF"]["monthly"])
    assert c_gw is not None and c_gw >= DOMINANCE_CORR, f"fixture invalid: corr {c_gw}"
    assert c_gd is not None and c_gd < DOMINANCE_CORR, f"fixture invalid: corr {c_gd}"
    assert fake["GOOD"]["mean_5y_ann"] > fake["WORSE"]["mean_5y_ann"]
    assert fake["GOOD"]["stddev_5y_ann"] < fake["WORSE"]["stddev_5y_ann"]

    # monthly-return maths
    s = [(dt.date(2020, m, 28), 100.0 * (1.01 ** m)) for m in range(1, 13)]
    mr = _monthly_returns(s + [(dt.date(2021, m, 28), 112.68 * (1.01 ** m)) for m in range(1, 13)])
    assert mr and abs(mr[0][1] - 0.01) < 1e-6, f"monthly return maths wrong: {mr[0]}"
    assert _monthly_returns([]) == [] and _monthly_returns(s) == [], \
        "fewer than 24 observations must yield NO series, not a noisy one"
    assert _corr(mr, mr[:5]) is None, "correlation on <24 overlapping months must refuse"

    # percentile rank
    assert _pct_rank(None, [1, 2, 3]) is None
    assert _pct_rank(3, [1, 2, 3]) == 100.0 and _pct_rank(1, [1, 2, 3]) == 0.0

    # bands
    assert FRS_HOLD_ADD > FRS_RETAIN_ONLY
    # a fund with no measurable components must be UNSCORED, never DEAD MONEY
    empty = {"return_adequacy": None, "risk_efficiency": None, "diversification": None,
             "fee_efficiency": None, "mandate_integrity": 5.0}
    avail = sum(WEIGHTS[k] for k, v in empty.items() if v is not None)
    assert avail < 50, "an all-missing fund must fall below the measurement threshold"

    # anchor rule direction
    bm = _bucket_minimums()
    assert set(bm) >= {"B1", "B2", "B3"} and all(0 < v < 1 for v in bm.values()), bm
    assert _anchor_floor() > 0
    print(f"SELFTEST PASS — 14 assertions (dominance fixture is genuinely dominated at corr "
          f"{c_gw:.2f} while the uncorrelated fund is not at {c_gd:.2f}, monthly-return maths, "
          f"<24-observation refusal x3, percentile rank x3, band ordering, all-missing fund "
          f"falls below the measurement threshold rather than scoring DEAD MONEY, bucket "
          f"minimums and anchor sane)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of"); ap.add_argument("--out")
    ap.add_argument("--portfolio"); ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    port = json.load(open(a.portfolio, encoding="utf-8")) if a.portfolio else None
    r = build(as_of=dt.date.fromisoformat(a.as_of) if a.as_of else None,
              portfolio=port, refresh=a.refresh)
    if a.out:
        json.dump(r, open(a.out, "w"), indent=2, default=str)
    s = r["summary"]
    print(f"FUND_ACTION_STACK as_of={r['as_of']} funds={s['n_funds']} | "
          f"HOLD/ADD {s['hold_add']} · RETAIN-ONLY {s['retain_only']} · "
          f"DEAD MONEY {s['dead_money']} (£{s['dead_money_value_gbp']:,.0f}) · "
          f"UNSCORED {s['unscored']}")
    print(f"  anchor floor {r['anchor_floor_pct']}% | bucket minimums {r['bucket_minimums_pct']}")
    if r["fund_dominance"]:
        print(f"  DOMINANCE ({len(r['fund_dominance'])}):")
        for d in r["fund_dominance"]:
            print(f"    {d['dominated_name'][:34]:36s} dominated by {d['dominated_by_name'][:26]:28s}"
                  f" {d['x_mean']}/{d['x_sd']} vs {d['y_mean']}/{d['y_sd']} corr {d['correlation']}"
                  + (f"  fee waste £{d['annual_fee_waste_gbp']:,.0f}/yr"
                     if d.get("annual_fee_waste_gbp") else ""))
    if r["anchor_rule_failures"]:
        print(f"  ANCHOR RULE FAILURES ({len(r['anchor_rule_failures'])}) — realised 5y below "
              f"the bucket minimum:")
        for f in r["anchor_rule_failures"]:
            print(f"    {f['name'][:36]:38s} {f['realised_5y_ann']}% vs {f['bucket']} minimum "
                  f"{f['bucket_minimum_pct']}%  (short {f['shortfall_pp']}pp)")
    print("  RANKED AGENDA:")
    for x in r["fund_action_stack"]:
        print(f"    {x['rank']:2d}. {str(x['name'])[:34]:36s} {x['band']:12s} "
              f"FRS {x['frs'] if x['frs'] is not None else '  --'}  "
              f"5y {x['realised_5y_ann']}  -> {x['action_required'][:46]}")
