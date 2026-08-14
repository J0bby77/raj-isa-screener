#!/usr/bin/env python3
"""
beta_alpha_study.py — RESEARCH (06-Aug-2026). Not wired into any run.

THE QUESTION: of the 13-44% these funds have realised, how much is asset-class BETA and how much
is manager ALPHA? Everything about a forward expected-return model depends on the answer, and the
framework has never asked it.

⚑ METHOD VALIDATION IS THE FIRST OUTPUT, NOT THE LAST. Two holdings are INDEX TRACKERS
(Vanguard S&P 500, Vanguard Japan). Against the right benchmark their alpha MUST come out at
roughly minus the OCF and their beta at ~1.0. If the controls fail, the method is broken and no
number below it means anything. This is the two-independent-derivations discipline applied to a
study rather than to a stored value.

⚑ AND THE BENCHMARK IS THE WHOLE BALL GAME. Measuring a value manager against a blend index
credits the value factor to the manager as "skill" — the JPM UK defect exactly (every number
right, the reference wrong). Where a style tilt is plausible the study runs a second regression
with a style factor and REPORTS BOTH, because a single-factor alpha for a style manager is a
number that will be quoted and should not be.
"""
import csv, hashlib, json, math, os, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "bench_cache")
sys.path.insert(0, HERE)

# ── the benchmark is READ, not chosen here. ───────────────────────────────────────────────
# ⚑ MAP IS RETIRED (13-Aug-2026, ISA-0320). It held twelve benchmarks chosen by me, with no record
# of what any fund's own prospectus names and no way for a stored artefact to disagree with it
# loudly. Both facts now live in `fund_universe.mandate_benchmark` and are read through
# `benchmark_registry`, which is the single home (R4.4/R6.1). A fund with no block RAISES.
#
# ⚑ ONE SUBSTANTIVE CHANGE OF NUMBER CAME WITH IT: B42W4J8's benchmark was XLKQ.L labelled
# "MSCI World Info Tech". XLKQ.L is the Invesco Technology S&P US SELECT SECTOR ETF — US-only,
# corr 0.9970 with IITU.L against 0.885 with the world-technology trackers. A global technology
# fund was measured against a US sector index wearing a global label. Its alpha moves -3.11% to
# -0.71% on the corrected comparator.
import benchmark_registry as breg

STYLE = {"BR2Q8G6": "IWVL.L", "B2PLJD7": "IWVL.L", "B2PLJM6": "IWVL.L", "B5TP8W8": "IWQU.L"}
# USD_QUOTED retired: the quote currency is read from bench_cache/_meta.json by
# benchmark_registry.gbp_returns. A hand-maintained set is a second home for a fact the
# metadata already carries, and a missing entry silently left the currency move inside beta.
MATERIAL_SIGN_FLIP_PP = 2.0   # see the materiality gate below; DECLARED judgement (R14.4)
RF_SENSITIVITY = (0.0, 2.0, 4.0)      # annual %, to show alpha's sensitivity to the rf assumption


def read_series(tkr):
    p = os.path.join(BENCH, f"{tkr.replace('=','_')}.csv")
    out = {}
    with open(p) as f:
        for r in csv.DictReader(f):
            d = dt.date.fromisoformat(r["date"])
            out[(d.year, d.month)] = float(r["close"])
    return out


def to_monthly_returns(levels):
    ks = sorted(levels)
    return {ks[i]: levels[ks[i]] / levels[ks[i - 1]] - 1.0
            for i in range(1, len(ks)) if levels[ks[i - 1]]}


def gbp_returns(tkr):
    """Monthly total return in GBP, via benchmark_registry — which reads the quote currency from
    the feed metadata, applies the declared series start, and drops the months the spike scan
    found to be defective prints. One home for all three (R4.4)."""
    return breg.gbp_returns(tkr)


def ols(y, X):
    """OLS with intercept. X = list of regressor columns. Returns (coefs, se, r2, n).
    Newey-West HAC (lag 3) standard errors — monthly fund returns are autocorrelated and OLS
    standard errors would overstate the significance of any alpha."""
    n = len(y)
    k = len(X) + 1
    A = [[1.0] + [X[j][i] for j in range(len(X))] for i in range(n)]
    XtX = [[sum(A[i][a] * A[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    Xty = [sum(A[i][a] * y[i] for i in range(n)) for a in range(k)]
    # gaussian elimination with partial pivoting
    M = [row[:] + [Xty[i]] for i, row in enumerate(XtX)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-14:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(k):
            if r == c:
                continue
            f = M[r][c] / M[c][c]
            for cc in range(c, k + 1):
                M[r][cc] -= f * M[c][cc]
    beta = [M[i][k] / M[i][i] for i in range(k)]
    resid = [y[i] - sum(beta[a] * A[i][a] for a in range(k)) for i in range(n)]
    ybar = sum(y) / n
    sst = sum((v - ybar) ** 2 for v in y)
    sse = sum(e * e for e in resid)
    r2 = 1 - sse / sst if sst > 0 else None
    # (X'X)^-1
    inv = []
    for col in range(k):
        e = [1.0 if i == col else 0.0 for i in range(k)]
        MM = [XtX[i][:] + [e[i]] for i in range(k)]
        for c in range(k):
            p = max(range(c, k), key=lambda r: abs(MM[r][c]))
            MM[c], MM[p] = MM[p], MM[c]
            for r in range(k):
                if r == c:
                    continue
                f = MM[r][c] / MM[c][c]
                for cc in range(c, k + 1):
                    MM[r][cc] -= f * MM[c][cc]
        inv.append([MM[i][k] / MM[i][i] for i in range(k)])
    inv = [[inv[j][i] for j in range(k)] for i in range(k)]
    L = 3
    S = [[0.0] * k for _ in range(k)]
    for i in range(n):
        for a in range(k):
            for b in range(k):
                S[a][b] += resid[i] * resid[i] * A[i][a] * A[i][b]
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        for i in range(l, n):
            for a in range(k):
                for b in range(k):
                    S[a][b] += w * resid[i] * resid[i - l] * (A[i][a] * A[i - l][b] + A[i - l][a] * A[i][b])
    V = [[sum(inv[a][x] * S[x][y2] for x in range(k)) for y2 in range(k)] for a in range(k)]
    V = [[sum(V[a][x] * inv[x][b] for x in range(k)) for b in range(k)] for a in range(k)]
    se = [math.sqrt(V[i][i]) if V[i][i] > 0 else float("nan") for i in range(k)]
    return beta, se, r2, n, resid


def ann(m):
    return ((1 + m) ** 12 - 1) * 100.0


def annualise_series(rets):
    p = 1.0
    for v in rets:
        p *= (1 + v)
    return (p ** (12 / len(rets)) - 1) * 100.0 if rets else None


def study():
    import fund_performance as fp, fund_action_stack as fas
    U = fp.load_universe()
    with open(fp.UNIVERSE, encoding="utf-8") as _fh:      # top level, for verified_at
        uni = json.load(_fh)
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "method": {
             "regression": "r_fund - rf = alpha + beta*(r_bench - rf) + e, monthly, GBP",
             "standard_errors": "Newey-West HAC, lag 3 (monthly fund returns are autocorrelated; "
                                "OLS errors would overstate the significance of any alpha)",
             "rf": f"sensitivity run at {RF_SENSITIVITY}% annual — alpha's dependence on the "
                   f"risk-free assumption is REPORTED rather than assumed away",
             "currency": "all series converted to GBP; a USD-quoted benchmark against a GBP fund "
                         "would put the sterling move inside beta and alpha",
             "total_return": "auto_adjust=True — dividends reinvested on every benchmark",
           }, "funds": {}, "controls": {}, "warnings": []}

    for sd, u in U.items():
        if str(sd).startswith("_") or not isinstance(u, dict):
            continue
        mb = breg.mandate_for(sd, U)                 # RAISES if the fund has no recorded mandate
        bt, comp = breg.comparator_for(sd, U)
        rec = {"name": u.get("name"), "bucket": u.get("bucket"), "ocf": u.get("ocf"),
               "mandate_benchmark": mb["index_name"],
               "mandate_declared_by_fund": mb["declared"],
               "t4_mandate": breg.t4_mandate_for(sd, U)[0],
               "t4_basis": breg.t4_mandate_for(sd, U)[1],
               "mandate_source": f"{mb['source_doc']} ({mb['source_type']})",
               "mandate_accessibility": mb["accessibility"]["verdict"],
               "benchmark": comp["tracks_index"], "benchmark_ticker": bt,
               "comparator_basis": comp["basis"],
               "residual_caveat": comp["residual_caveat"] or None}
        # ⚑ ONE way to get a NAV series, and it takes the declared local route first (ISA-0307).
        try:
            nav = fp.nav_series_for(sd, u)
        except Exception as exc:
            nav = None
            out["warnings"].append(f"{sd} {u.get('name')}: NAV series unavailable — {exc}")
        fr = dict(fas._monthly_returns(nav)) if nav else {}
        if not fr:
            rec["status"] = "NOT_DECOMPOSED"
            rec["reason"] = "no NAV series"
            # ⚑ COUNTED, not silent. A reader that cannot produce a result must count it and fail
            # (R4.9). This study previously emitted a NOT_DECOMPOSED fund and `warnings: []`,
            # which is how 7.6% of the portfolio carried no measured beta for six days with a
            # reconciling series sitting on disk.
            out["warnings"].append(
                f"{sd} {u.get('name')}: NOT_DECOMPOSED — no NAV series. "
                f"{u.get('ocf') is not None and 'held position' or ''}")
            out["funds"][sd] = rec
            continue
        br = gbp_returns(bt)
        common = sorted(set(fr) & set(br))
        rec["n_months"] = len(common)
        rec["window"] = [f"{common[0][0]}-{common[0][1]:02d}", f"{common[-1][0]}-{common[-1][1]:02d}"] if common else None
        if len(common) < 30:
            rec["status"] = "INSUFFICIENT_OVERLAP"
            out["funds"][sd] = rec
            continue
        y = [fr[k] for k in common]
        x = [br[k] for k in common]
        rec["fund_ann_pct"] = round(annualise_series(y), 2)
        rec["bench_ann_pct"] = round(annualise_series(x), 2)

        # ── single factor, with the rf sensitivity ────────────────────────────────────
        sens = {}
        for rf_a in RF_SENSITIVITY:
            rfm = (1 + rf_a / 100) ** (1 / 12) - 1
            res = ols([v - rfm for v in y], [[v - rfm for v in x]])
            if not res:
                continue
            b, se, r2, n, resid = res
            a_ann = ann(b[0])
            sens[f"rf_{rf_a:g}pct"] = {"alpha_ann_pct": round(a_ann, 2), "beta": round(b[1], 3)}
            if rf_a == 2.0:
                rec["single_factor"] = {
                    "alpha_ann_pct": round(a_ann, 2),
                    "alpha_t": round(b[0] / se[0], 2) if se[0] == se[0] and se[0] > 0 else None,
                    "beta": round(b[1], 3),
                    "beta_se": round(se[1], 3),
                    "r_squared": round(r2, 4),
                    "n_months": n,
                    "beta_contribution_pct": round(rec["fund_ann_pct"] - a_ann, 2),
                    "alpha_contribution_pct": round(a_ann, 2),
                    "share_of_return_from_beta_pct": (
                        round(100 * (rec["fund_ann_pct"] - a_ann) / rec["fund_ann_pct"], 1)
                        if rec["fund_ann_pct"] else None),
                }
        # ── ⚑ ALPHA IS NEVER RENDERED AS A BARE POINT ESTIMATE (ISA-0330, 13-Aug-2026) ──
        # Raj challenged the published -0.38% for Polar against AJ Bell's +7.71% 3-year figure and
        # was right to. There was no methodological disagreement: the SAME method on the SAME
        # benchmark gives +10.80% over 36 months and -0.74% over 67. The window flipped the sign,
        # and the framework published one window and no interval — precisely the number that gets
        # quoted and should not be. Every alpha now ships its interval and its window sweep.
        res2 = ols(y, [x])
        if res2:
            b2c, se2c, r2c, n2c, _ = res2
            rec["alpha_interval"] = {
                "alpha_ann_pct": round(ann(b2c[0]), 2),
                "se_ann_pp": round(ann(b2c[0] + se2c[0]) - ann(b2c[0]), 2),
                "ci95_low_ann_pct": round(ann(b2c[0] - 1.96 * se2c[0]), 2),
                "ci95_high_ann_pct": round(ann(b2c[0] + 1.96 * se2c[0]), 2),
                "contains_zero": bool(ann(b2c[0] - 1.96 * se2c[0]) <= 0 <= ann(b2c[0] + 1.96 * se2c[0])),
                "basis": "rf = 0, Newey-West HAC lag 3, same window as single_factor",
                "read_this_first": "the interval is the finding. A point estimate whose interval "
                                   "spans zero cannot order two funds, however large it looks."}
            sweep, signs = {}, []
            for lbl, m in (("3y", 36), ("4y", 48), ("5y", 60), ("full", len(common))):
                if len(common) < m:
                    sweep[lbl] = {"status": "INSUFFICIENT", "months_available": len(common)}
                    continue
                kk = common[-m:]
                r3 = ols([fr[k] for k in kk], [[br[k] for k in kk]])
                if not r3:
                    sweep[lbl] = {"status": "DEGENERATE"}
                    continue
                b3, se3, r23, n3, _ = r3
                a3 = ann(b3[0])
                sweep[lbl] = {"n": n3, "alpha_ann_pct": round(a3, 2),
                              "alpha_t": round(b3[0] / se3[0], 2) if se3[0] > 0 else None,
                              "beta": round(b3[1], 3), "r_squared": round(r23, 4),
                              "ci95": [round(ann(b3[0] - 1.96 * se3[0]), 2),
                                       round(ann(b3[0] + 1.96 * se3[0]), 2)]}
                signs.append(a3 > 0)
            span = [v["alpha_ann_pct"] for v in sweep.values() if "alpha_ann_pct" in v]
            rec["window_sweep"] = {
                "windows": sweep,
                "sign_stable": (len(set(signs)) <= 1) if signs else None,
                "alpha_range_pp": round(max(span) - min(span), 2) if len(span) > 1 else None,
                "verdict": ("SIGN FLIPS ACROSS WINDOWS — this alpha may not be quoted as a single "
                            "number" if (len(set(signs)) > 1 and
                                         (max(span) - min(span) if len(span) > 1 else 0)
                                         >= MATERIAL_SIGN_FLIP_PP) else
                            "sign flips but by less than the materiality gate — noise around zero"
                            if len(set(signs)) > 1 else
                            "sign stable across the windows tested")}
            # ⚑ MATERIALITY GATE. On the first run the two index TRACKERS both tripped the
            # sign-flip warning — VUAG by 0.42pp, Vanguard Japan by 0.53pp — because an alpha
            # sitting on top of zero crosses it for free. A warning that fires on noise is a
            # warning nobody reads. 2.0pp is DECLARED judgement: it is twice the largest flip the
            # controls produce, and below the 1-point resolution of any FRS decision.
            if len(set(signs)) > 1 and (rec["window_sweep"]["alpha_range_pp"] or 0) >= MATERIAL_SIGN_FLIP_PP:
                out["warnings"].append(
                    f"{sd} {u.get('name')}: alpha SIGN FLIPS across windows "
                    f"(range {rec['window_sweep']['alpha_range_pp']}pp) — quote the interval, "
                    f"never the point estimate")
        rec["rf_sensitivity"] = sens
        rec["alpha_rf_spread_pp"] = round(
            max(v["alpha_ann_pct"] for v in sens.values())
            - min(v["alpha_ann_pct"] for v in sens.values()), 2) if sens else None

        # ── style-aware second regression where a tilt is plausible ───────────────────
        st = STYLE.get(sd)
        if st:
            sr = gbp_returns(st)
            c2 = [k for k in common if k in sr]
            if len(c2) >= 30:
                rfm = (1 + 0.02) ** (1 / 12) - 1
                res2 = ols([fr[k] - rfm for k in c2],
                           [[br[k] - rfm for k in c2], [sr[k] - br[k] for k in c2]])
                if res2:
                    b2, se2, r22, n2, _ = res2
                    rec["style_factor"] = {
                        "style_ticker": st,
                        "alpha_ann_pct": round(ann(b2[0]), 2),
                        "alpha_t": round(b2[0] / se2[0], 2) if se2[0] > 0 else None,
                        "beta_market": round(b2[1], 3),
                        "loading_style": round(b2[2], 3),
                        "loading_style_t": round(b2[2] / se2[2], 2) if se2[2] > 0 else None,
                        "r_squared": round(r22, 4), "n_months": n2,
                        "alpha_change_vs_single_pp": round(
                            ann(b2[0]) - rec["single_factor"]["alpha_ann_pct"], 2),
                    }

        # ── regime split: does the alpha survive in BOTH directions? ──────────────────
        up = [k for k in common if br[k] > 0]
        dn = [k for k in common if br[k] < 0]
        reg = {"n_up": len(up), "n_down": len(dn)}
        for lbl, ks in (("up", up), ("down", dn)):
            if len(ks) >= 12:
                rr = ols([fr[k] for k in ks], [[br[k] for k in ks]])
                if rr:
                    bb, ss, _, nn, _ = rr
                    reg[lbl] = {"alpha_ann_pct": round(ann(bb[0]), 2), "beta": round(bb[1], 3),
                                "alpha_t": round(bb[0] / ss[0], 2) if ss[0] > 0 else None, "n": nn}
            f_, b_ = 1.0, 1.0
            for k in ks:
                f_ *= (1 + fr[k]); b_ *= (1 + br[k])
            reg[f"{lbl}_capture_pct"] = (round(100 * (f_ - 1) / (b_ - 1), 1)
                                         if abs(b_ - 1) > 1e-9 else None)
        if isinstance(reg.get("up"), dict) and isinstance(reg.get("down"), dict):
            reg["beats_in_both"] = bool(reg["up"]["alpha_ann_pct"] > 0
                                        and reg["down"]["alpha_ann_pct"] > 0)
            reg["both_significant"] = bool((reg["up"].get("alpha_t") or 0) > 1.96
                                           and (reg["down"].get("alpha_t") or 0) > 1.96)
        rec["regime"] = reg

        # ── split-sample persistence: does first-half alpha predict second-half alpha? ─
        h = len(common) // 2
        halves = {}
        for lbl, ks in (("first_half", common[:h]), ("second_half", common[h:])):
            if len(ks) >= 15:
                rr = ols([fr[k] for k in ks], [[br[k] for k in ks]])
                if rr:
                    bb, ss, _, nn, _ = rr
                    halves[lbl] = {"alpha_ann_pct": round(ann(bb[0]), 2), "n": nn,
                                   "alpha_t": round(bb[0] / ss[0], 2) if ss[0] > 0 else None}
        rec["split_sample"] = halves
        rec["status"] = "OK"
        out["funds"][sd] = rec

    # ── CONTROL GATE ─────────────────────────────────────────────────────────────────
    for sd in ("VUAG", "B50MZ94"):
        r = out["funds"].get(sd) or {}
        sf = r.get("single_factor")
        if not sf:
            out["controls"][sd] = {"pass": False, "reason": "not decomposed"}
            continue
        ok_b = 0.90 <= sf["beta"] <= 1.10
        ok_a = abs(sf["alpha_ann_pct"]) <= 2.0
        out["controls"][sd] = {"pass": bool(ok_b and ok_a), "beta": sf["beta"],
                               "alpha_ann_pct": sf["alpha_ann_pct"], "ocf": r.get("ocf"),
                               "r_squared": sf["r_squared"],
                               "expectation": "an index tracker must show beta ~1.0 and alpha ~ -OCF"}
    if not all(v.get("pass") for v in out["controls"].values()):
        out["warnings"].append(
            "⚑ CONTROL FAILURE — a passive tracker did not reproduce beta ~1 / alpha ~ -OCF. "
            "The benchmark mapping or the method is wrong and NO alpha below is trustworthy.")

    # ── the artefact asserts its own fitness (R4.10) ──────────────────────────────────────
    dec = [s for s, r in out["funds"].items() if r.get("status") not in
           ("NOT_DECOMPOSED", "INSUFFICIENT_OVERLAP")]
    nd = [s for s, r in out["funds"].items() if r.get("status") == "NOT_DECOMPOSED"]
    io = [s for s, r in out["funds"].items() if r.get("status") == "INSUFFICIENT_OVERLAP"]
    out["coverage"] = {"funds": len(out["funds"]), "decomposed": len(dec),
                       "not_decomposed": nd, "insufficient_overlap": io,
                       "pct": round(100.0 * len(dec) / max(len(out["funds"]), 1), 1)}
    for s in io:
        out["warnings"].append(f"{s}: INSUFFICIENT_OVERLAP — fewer than 30 common months")

    # ── the freshness contract (ISA-0307 cause 2) ─────────────────────────────────────────
    # A stored study that predates a change to fund_universe.mandate_benchmark is a stale artefact
    # silently consumed by M*. Stamp what was read so a consumer can detect it, instead of
    # asserting agreement between two homes that should never have both existed.
    src = {s: (U[s]["mandate_benchmark"]["index_name"],
               U[s]["mandate_benchmark"]["comparator"]["ticker"])
           for s in out["funds"] if isinstance(U.get(s), dict) and "mandate_benchmark" in U[s]}
    out["provenance"] = {
      "fund_universe_verified_at": (uni.get("verified_at") if isinstance(uni, dict) else None),
      "mandate_fingerprint": hashlib.sha256(
          json.dumps(src, sort_keys=True).encode()).hexdigest()[:12],
      "benchmark_validation": breg.validate_all(U),
      "note": "a consumer that finds mandate_fingerprint different from the one it can compute "
              "from fund_universe today is reading a STALE artefact and must refuse it"}
    if out["provenance"]["benchmark_validation"]["errors"]:
        out["warnings"].append(
            "⚑ BENCHMARK SERIES FAILURE — " +
            "; ".join(out["provenance"]["benchmark_validation"]["errors"]))
    return out


if __name__ == "__main__":
    r = study()
    json.dump(r, open(os.path.join(HERE, "beta_alpha_study_aug2026.json"), "w"), indent=1)
    c = r["coverage"]
    print(f"=== COVERAGE {c['decomposed']}/{c['funds']} ({c['pct']}%) ===")
    print("=== CONTROLS (must pass before anything else is read) ===")
    for k, v in r["controls"].items():
        print(f"  {k:<9} pass={v['pass']}  beta {v.get('beta')}  alpha {v.get('alpha_ann_pct')}%  "
              f"OCF {v.get('ocf')}%  R2 {v.get('r_squared')}")
    for w in r["warnings"]:
        print(" ", w)
    # a fund the study could not decompose is a FAILURE, not a footnote (R4.9)
    sys.exit(1 if (r["coverage"]["not_decomposed"] or
                   not all(v.get("pass") for v in r["controls"].values()) or
                   r["provenance"]["benchmark_validation"]["errors"]) else 0)
