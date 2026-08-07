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
import csv, json, math, os, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "bench_cache")
sys.path.insert(0, HERE)

# ── fund -> asset-class benchmark. DECLARED, with the reason. Never inferred from the name. ──
MAP = {
 "VUAG":    ("CSP1.L", "S&P 500 (GBp)", "CONTROL — VUAG tracks the S&P 500. Alpha must be ~ -OCF."),
 "B50MZ94": ("VJPN.L", "FTSE Japan (GBP)", "CONTROL — a Japan index tracker. Note the fund tracks MSCI Japan and the benchmark is FTSE Japan; a small residual is index difference, not skill."),
 "BF93W97": ("VEVE.L", "FTSE Developed World (GBP)", "unconstrained global developed-market equity"),
 "BR2Q8G6": ("VEVE.L", "FTSE Developed World (GBP)", "global equity, explicit VALUE process — style regression is the primary read"),
 "B2PLJD7": ("VERX.L", "FTSE Dev Europe ex-UK (GBP)", "European mandate; SmartGARP is a value/GARP screen"),
 "B2PLJM6": ("FTAL.L", "FTSE All-Share (GBP)", "UK mandate; SmartGARP value/GARP screen"),
 "B55QSH0": ("FTAL.L", "FTSE All-Share (GBP)", "UK core mandate"),
 "B5TP8W8": ("VEVE.L", "FTSE Developed World (GBP)", "global opportunities mandate"),
 "B6SQYF4": ("AAXJ",   "MSCI AC Asia ex-Japan (USD->GBP)", "holds China/India/Korea/Taiwan — DEVELOPED Asia Pac ex-Japan would be the wrong peer"),
 "B8N44Q8": ("AAXJ",   "MSCI AC Asia ex-Japan (USD->GBP)", "same mandate as M&G Asian; must be benchmarked identically or their pair test is meaningless"),
 "SMT":     ("VWRP.L", "FTSE All-World (GBP)", "⚑ closed-end: this is a PRICE return, so 'alpha' here contains the discount move as well as the manager"),
 # ⚑ RESOLVED 06-Aug-2026 — Raj supplied an investing.com series for 0P0000OMTA. VALIDATED as the
 # held share class before use: 1m/3m/6m/1y reproduce the AJ Bell factsheet at its own 31-Jul-2026
 # strike date to 0.00pp, 3y/5y to within 0.19pp. Same-class confirmation, not an assumption —
 # register SC1 exists because HL figures for the Investor class were once taken for this one.
 "B42W4J8": ("XLKQ.L", "MSCI World Info Tech (GBp)", "global technology mandate — the SECTOR is the "
             "benchmark. Against a broad world index its beta reads 1.86, which prices the sector "
             "bet as manager skill."),
}
STYLE = {"BR2Q8G6": "IWVL.L", "B2PLJD7": "IWVL.L", "B2PLJM6": "IWVL.L", "B5TP8W8": "IWQU.L"}
USD_QUOTED = {"AAXJ", "IWVL.L", "IWQU.L", "IWMO.L"}
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
    """Monthly total return in GBP. A USD-quoted line is converted, because comparing a USD asset
    to a GBP fund embeds the currency move in beta and alpha and neither belongs there."""
    r = to_monthly_returns(read_series(tkr))
    if tkr not in USD_QUOTED:
        return r
    fx = to_monthly_returns(read_series("GBPUSD_X"))   # GBPUSD up = GBP stronger = USD asset worth less
    return {k: (1 + r[k]) / (1 + fx[k]) - 1.0 for k in r if k in fx}


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
    uni = fp.load_universe(); U = uni.get("funds", uni)
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
        bt, bn, why = MAP.get(sd, (None, None, "no declared benchmark"))
        rec = {"name": u.get("name"), "bucket": u.get("bucket"), "ocf": u.get("ocf"),
               "benchmark": bn, "benchmark_ticker": bt, "benchmark_rationale": why}
        sym = u.get("yf_symbol") or u.get("isin")
        try:
            nav = fp.fetch_nav_history(sym, use_cache=True)
        except Exception:
            nav = None
        fr = dict(fas._monthly_returns(nav)) if nav else {}
        if not fr or not bt:
            rec["status"] = "NOT_DECOMPOSED"
            rec["reason"] = ("no NAV series — factsheet-only fund" if not fr
                             else "no declared benchmark")
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
    return out


if __name__ == "__main__":
    r = study()
    json.dump(r, open(os.path.join(HERE, "beta_alpha_study_aug2026.json"), "w"), indent=1)
    print("=== CONTROLS (must pass before anything else is read) ===")
    for k, v in r["controls"].items():
        print(f"  {k:<9} pass={v['pass']}  beta {v.get('beta')}  alpha {v.get('alpha_ann_pct')}%  "
              f"OCF {v.get('ocf')}%  R2 {v.get('r_squared')}")
    for w in r["warnings"]:
        print(" ", w)
