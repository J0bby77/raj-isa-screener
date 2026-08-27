#!/usr/bin/env python3
"""
correlation_engine.py — sleeve correlation, N_eff, and the admission gate's input.
V2.1-B (ISA-0355), SHADOW ONLY — this module moves no capital.
Authority: clean spec s7; amendments A2.1, A2.2, A2.3.

═══════════════════════════════════════════════════════════════════════════════════════════
THE ONE ASYMMETRY THAT MATTERS (A2.3)
═══════════════════════════════════════════════════════════════════════════════════════════
V2.1 invariant 4 says "missing cannot become numeric zero". That is right for RETURN inputs.
For RISK inputs the correct treatment is the OPPOSITE, and the framework had never written it
down:

    an unmeasured correlation is ADVERSE, not neutral:  rho = max(rho_bar_sleeve, 0.70)
    and the position is capped at STARTER until 52 weeks exist.

⚑ WHY THE SIGN MATTERS SO MUCH HERE. A missing return input treated as zero makes a name look
WORSE and is therefore self-limiting. A missing correlation treated as zero makes a name look
DIVERSIFYING and therefore BIGGER. Without A2.3 the newest and least-measurable names would
receive the largest multipliers — the error runs toward the risk, not away from it.

⚑ AND TODAY EVERY NAME IS UNMEASURED. No 104-week series exists and there is no network. So
this module's live output is a REFUSAL, and the refusal is the product: `rho_basis` reads
`UNMEASURED_ADVERSE_DEFAULT` on every name, `size_ceiling` reads STARTER, and `measured` is
False. Nothing here quietly returns 0.0 (R4.3, R2.10).

═══════════════════════════════════════════════════════════════════════════════════════════
ESTIMATION (A2.2)
═══════════════════════════════════════════════════════════════════════════════════════════
104 weeks Friday-to-Friday GBP total return, minimum 52.
Shrinkage: rho_used = 0.7 * rho_sample + 0.3 * rho_bar_sleeve  (constant-correlation target).
At n = 5 names one noisy pair must not drive a capital decision, and the shrinkage constant is
DECLARED rather than fitted (R3.9).
"""
from __future__ import annotations

import datetime
import json
import math
import os
import statistics
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# ── declared constants (A2.2 / A2.1). PROVISIONAL: must be measured before they gate ──────
SHRINK_W_SAMPLE = 0.70
SHRINK_W_TARGET = 0.30
RHO_UNMEASURED_FLOOR = 0.70          # A2.3 adverse default
RHO_MAX_PAIRWISE_GATE = 0.70         # A2.1 admission gate
RHO_SLEEVE_GATE = 0.60               # A2.1 admission gate
MIN_WEEKS = 52
RHO_BAR_FALLBACK = 0.70              # when the sleeve itself has no measured pair

ADMIT_AS_ADDITION = "ADMIT_AS_ADDITION"
REPLACEMENT_ONLY = "REPLACEMENT_ONLY"


def _pearson(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) != len(b) or len(a) < 8:
        return None
    try:
        sa, sb = statistics.pstdev(a), statistics.pstdev(b)
        if sa <= 0 or sb <= 0:
            return None
        ma, mb = statistics.fmean(a), statistics.fmean(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)
        r = cov / (sa * sb)
        return max(-1.0, min(1.0, r))
    except Exception:
        return None


def _aligned(r1: Dict[str, float], r2: Dict[str, float]):
    keys = sorted(set(r1) & set(r2))
    return [r1[k] for k in keys], [r2[k] for k in keys], keys


def pairwise_matrix(returns_by_name: Dict[str, Dict[str, float]]) -> dict:
    """Sample correlations on the COMMON window, plus rho_bar (the constant-correlation target).

    ⚑ Common window, not pairwise-maximum-overlap. Pairwise-max uses more data but need not
    produce a positive semi-definite matrix, and a decomposition of a non-covariance is
    arithmetic on an object that is not a risk model. `concentration_clusters` made the same
    choice for funds and states the same reason."""
    names = sorted(returns_by_name)
    pairs, measured_vals, skipped = {}, [], []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            xa, xb, keys = _aligned(returns_by_name[a], returns_by_name[b])
            r = _pearson(xa, xb)
            if r is None:
                skipped.append({"pair": [a, b], "overlap_weeks": len(keys),
                                "reason": f"overlap {len(keys)} < 8 weeks or zero variance"})
                continue
            pairs[f"{a}|{b}"] = {"rho": round(r, 6), "weeks": len(keys)}
            measured_vals.append(r)
    rho_bar = round(statistics.fmean(measured_vals), 6) if measured_vals else None
    return {"names": names, "pairs": pairs, "rho_bar": rho_bar,
            "n_pairs_measured": len(pairs), "pairs_skipped": skipped}


def shrink(rho_sample: float, rho_bar: Optional[float]) -> float:
    tgt = RHO_BAR_FALLBACK if rho_bar is None else rho_bar
    return SHRINK_W_SAMPLE * rho_sample + SHRINK_W_TARGET * tgt


def candidate_correlation(candidate: str, returns_by_name: Dict[str, Dict[str, float]],
                          weights: Dict[str, float], matrix: Optional[dict] = None) -> dict:
    """rho_max_pairwise and rho_sleeve for ONE candidate against the existing sleeve.

    Returns a record that always states its BASIS. `measured` False is a first-class outcome —
    it is not an error and it is not a zero."""
    holdings = [n for n in returns_by_name if n != candidate]
    cand_rets = returns_by_name.get(candidate) or {}
    m = matrix if matrix is not None else pairwise_matrix(returns_by_name)
    rho_bar = m.get("rho_bar")

    per, usable = {}, 0
    for h in holdings:
        xa, xb, keys = _aligned(cand_rets, returns_by_name.get(h) or {})
        r = _pearson(xa, xb)
        if r is None or len(keys) < MIN_WEEKS:
            per[h] = {"rho": None, "weeks": len(keys), "basis": "UNMEASURED"}
            continue
        per[h] = {"rho": round(shrink(r, rho_bar), 6), "rho_sample": round(r, 6),
                  "weeks": len(keys), "basis": "MEASURED_SHRUNK"}
        usable += 1

    if usable == 0:
        # A2.3 — the whole point of this module today.
        adverse = max(rho_bar if rho_bar is not None else RHO_BAR_FALLBACK,
                      RHO_UNMEASURED_FLOOR)
        return {
            "candidate": candidate, "measured": False,
            "rho_max_pairwise": round(adverse, 6), "rho_sleeve": round(adverse, 6),
            "rho_basis": "UNMEASURED_ADVERSE_DEFAULT",
            "size_ceiling": "STARTER",
            "per_holding": per, "rho_bar": rho_bar,
            "detail": (f"No holding has {MIN_WEEKS}+ overlapping weekly GBP returns with "
                       f"{candidate}. A2.3: an unmeasured correlation is ADVERSE, so rho is set "
                       f"to max(rho_bar, {RHO_UNMEASURED_FLOOR}) = {adverse:.2f} and the "
                       f"position is capped at STARTER until 52 weeks exist. This is a MEASURED "
                       f"REFUSAL, not an estimate of zero."),
        }

    vals = [v["rho"] for v in per.values() if v["rho"] is not None]
    tot_w = sum(weights.get(h, 0.0) for h in holdings if per[h]["rho"] is not None)
    rho_sleeve = (sum(weights.get(h, 0.0) * per[h]["rho"]
                      for h in holdings if per[h]["rho"] is not None) / tot_w
                  if tot_w > 0 else statistics.fmean(vals))
    n_unmeasured = sum(1 for v in per.values() if v["rho"] is None)
    return {
        "candidate": candidate, "measured": True,
        "rho_max_pairwise": round(max(vals), 6), "rho_sleeve": round(rho_sleeve, 6),
        "rho_basis": "MEASURED_SHRUNK", "size_ceiling": None,
        "per_holding": per, "rho_bar": rho_bar,
        "holdings_measured": usable, "holdings_unmeasured": n_unmeasured,
        "partial": n_unmeasured > 0,
        "detail": (f"{usable} of {len(holdings)} holdings measured"
                   + (f"; {n_unmeasured} unmeasured and EXCLUDED from rho_sleeve — the weighted "
                      f"mean therefore covers {tot_w:.1f}% of sleeve weight, not 100%"
                      if n_unmeasured else "")),
    }


def admission(candidate_rec: dict) -> dict:
    """A2.1. A candidate that fails is NOT blocked — it is reclassified as REPLACEMENT_ONLY.

    ⚑ 'If two names are 0.85 correlated you do not own both; you own the better one.' This also
    resolves the four-of-five-quantum VCI watchlist mechanically, with no theme-cap argument."""
    rmax = candidate_rec.get("rho_max_pairwise")
    rslv = candidate_rec.get("rho_sleeve")
    breaches = []
    if rmax is not None and rmax > RHO_MAX_PAIRWISE_GATE:
        breaches.append(f"rho_max_pairwise {rmax:.3f} > {RHO_MAX_PAIRWISE_GATE}")
    if rslv is not None and rslv > RHO_SLEEVE_GATE:
        breaches.append(f"rho_sleeve {rslv:.3f} > {RHO_SLEEVE_GATE}")
    verdict = REPLACEMENT_ONLY if breaches else ADMIT_AS_ADDITION
    return {"verdict": verdict, "breaches": breaches,
            "measured": candidate_rec.get("measured", False),
            "rho_basis": candidate_rec.get("rho_basis"),
            "size_ceiling": candidate_rec.get("size_ceiling"),
            "detail": ("admitted as an addition" if verdict == ADMIT_AS_ADDITION else
                       "reclassified REPLACEMENT_ONLY: " + "; ".join(breaches)
                       + ". Not blocked — it may enter as a replacement for the holding it "
                         "duplicates, via the s10 replacement engine.")}


def n_eff(weights: Dict[str, float], sigmas: Dict[str, float], matrix: dict) -> dict:
    """Diversification ratio and effective bets. DIAGNOSTIC ONLY — A3's ceiling ladder was
    REMOVED by the clean spec s2, so nothing gates on this. It is reported because it is the
    honest description of how concentrated the sleeve actually is."""
    names = [n for n in sorted(weights) if n in sigmas and sigmas[n] and weights.get(n)]
    if len(names) < 2:
        return {"n_eff": None, "dr": None, "measured": False, "names": names,
                "detail": "fewer than two names with both a weight and a measured sigma"}
    def rho(a, b):
        if a == b:
            return 1.0
        p = matrix["pairs"].get(f"{a}|{b}") or matrix["pairs"].get(f"{b}|{a}")
        return p["rho"] if p else None
    missing = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if rho(a, b) is None]
    if missing:
        return {"n_eff": None, "dr": None, "measured": False, "names": names,
                "missing_pairs": [list(p) for p in missing],
                "detail": (f"{len(missing)} pair(s) unmeasured. N_eff over a partly-filled "
                           f"matrix would be an overstatement of diversification, so it is "
                           f"REFUSED rather than approximated (A2.3's direction).")}
    tot = sum(weights[n] for n in names)
    w = {n: weights[n] / tot for n in names}
    wsum = sum(w[n] * sigmas[n] for n in names)
    var = sum(w[a] * w[b] * sigmas[a] * sigmas[b] * rho(a, b) for a in names for b in names)
    if var <= 0:
        return {"n_eff": None, "dr": None, "measured": False, "names": names,
                "detail": "non-positive portfolio variance"}
    dr = wsum / math.sqrt(var)
    return {"n_eff": round(dr ** 2, 4), "dr": round(dr, 4), "measured": True,
            "names": names, "n_names": len(names),
            "detail": (f"DR = sum(w*sigma)/sigma_p = {dr:.3f}; N_eff = DR^2 = {dr**2:.2f} "
                       f"effective bets across {len(names)} names. DIAGNOSTIC — the A3 ceiling "
                       f"ladder that would have consumed this was removed by clean spec s2.")}


def assess(returns_by_name, weights, sigmas=None, candidates=None) -> dict:
    """The run-level artefact. Every field states its basis (R4.2)."""
    m = pairwise_matrix(returns_by_name)
    sig = sigmas or {n: (statistics.pstdev(list(r.values())) if len(r) >= 8 else None)
                     for n, r in returns_by_name.items()}
    sig = {k: v for k, v in sig.items() if v}
    out = {
        "as_of": datetime.date.today().isoformat(),
        "policy_version": "ISA_V2_1", "shadow_only": True,
        "matrix": m, "n_eff": n_eff(weights, sig, m),
        "candidates": {}, "holdings": {},
    }
    for h in sorted(returns_by_name):
        rec = candidate_correlation(h, returns_by_name, weights, matrix=m)
        out["holdings"][h] = {k: rec[k] for k in
                              ("measured", "rho_sleeve", "rho_max_pairwise", "rho_basis")}
    for c in (candidates or []):
        rec = candidate_correlation(c, returns_by_name, weights, matrix=m)
        out["candidates"][c] = {**rec, "admission": admission(rec)}
    n_un = sum(1 for v in out["holdings"].values() if not v["measured"])
    out["summary"] = {
        "n_holdings": len(out["holdings"]), "n_unmeasured": n_un,
        "sleeve_measured": n_un == 0 and bool(out["holdings"]),
        "headline": ("sleeve correlation is MEASURED" if n_un == 0 and out["holdings"] else
                     f"{n_un} of {len(out['holdings'])} holdings UNMEASURED — A2.3 adverse "
                     f"default applies and every affected position is capped at STARTER"),
    }
    return out


def _selftest():
    import random
    random.seed(7)
    # two names driven by one factor -> high correlation; one independent -> low
    f = [random.gauss(0, 0.02) for _ in range(104)]
    keys = [(datetime.date(2025, 1, 3) + datetime.timedelta(weeks=i)).isoformat()
            for i in range(104)]
    A = {k: f[i] + random.gauss(0, 0.002) for i, k in enumerate(keys)}
    B = {k: f[i] + random.gauss(0, 0.002) for i, k in enumerate(keys)}
    C = {k: random.gauss(0, 0.02) for i, k in enumerate(keys)}
    rets = {"A": A, "B": B, "C": C}
    w = {"A": 3.5, "B": 3.5, "C": 3.5}
    m = pairwise_matrix(rets)
    ab = m["pairs"]["A|B"]["rho"]; ac = m["pairs"]["A|C"]["rho"]
    assert ab > 0.9, ab
    assert abs(ac) < 0.4, ac
    # shrinkage pulls toward rho_bar and never past the sample on the far side
    s = shrink(1.0, 0.0); assert abs(s - 0.70) < 1e-9, s
    # B against a sleeve containing A must breach and be RECLASSIFIED, not blocked
    recB = candidate_correlation("B", rets, w, matrix=m)
    adm = admission(recB)
    assert recB["measured"] and recB["rho_max_pairwise"] > 0.70, recB
    assert adm["verdict"] == REPLACEMENT_ONLY and adm["breaches"], adm
    # C should be admitted
    admC = admission(candidate_correlation("C", rets, w, matrix=m))
    assert admC["verdict"] == ADMIT_AS_ADDITION, admC
    # A2.3: a name with NO history gets the ADVERSE default and a STARTER cap — never 0.0
    rets2 = dict(rets); rets2["NEW"] = {}
    recN = candidate_correlation("NEW", rets2, {**w, "NEW": 0.0})
    assert recN["measured"] is False, recN
    assert recN["rho_sleeve"] >= 0.70, recN
    assert recN["size_ceiling"] == "STARTER", recN
    assert recN["rho_basis"] == "UNMEASURED_ADVERSE_DEFAULT"
    assert admission(recN)["verdict"] == REPLACEMENT_ONLY
    # N_eff refuses on a partly-filled matrix rather than overstating diversification
    sig = {"A": 0.02, "B": 0.02, "C": 0.02, "NEW": 0.02}
    m2 = pairwise_matrix(rets2)
    ne_bad = n_eff({**w, "NEW": 3.5}, sig, m2)
    assert ne_bad["n_eff"] is None and ne_bad["measured"] is False, ne_bad
    ne = n_eff(w, {"A": 0.02, "B": 0.02, "C": 0.02}, m)
    assert ne["measured"] and 1.0 < ne["n_eff"] < 3.0, ne
    # two identical names must give N_eff ~ 1 (the sanity anchor)
    m3 = pairwise_matrix({"A": A, "B2": dict(A)})
    ne3 = n_eff({"A": 1.0, "B2": 1.0}, {"A": 0.02, "B2": 0.02}, m3)
    assert abs(ne3["n_eff"] - 1.0) < 0.05, ne3
    a = assess(rets2, {**w, "NEW": 0.0}, candidates=["NEW"])
    assert a["shadow_only"] is True and a["summary"]["n_unmeasured"] >= 1
    print("correlation_engine selftest OK (16 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps(assess({}, {}), indent=1))
