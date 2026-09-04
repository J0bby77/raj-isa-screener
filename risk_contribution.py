#!/usr/bin/env python3
"""
risk_contribution.py — V2.1-D s8, the position-size-vs-risk hybrid (Raj decision D10).
Plus M1/M2/M3, the three instruments that measure whether the rule is worth having.

═══════════════════════════════════════════════════════════════════════════════════════════
THE RULE (clean spec s8)
═══════════════════════════════════════════════════════════════════════════════════════════
    rc_i        = w_i * sigma_i / SUM(w_j * sigma_j)          # share of sleeve risk
    risk_weight = (SUM(w_j * sigma_j) / N) / sigma_i          # the weight that equalises risk
    FLAG when risk_weight < 0.75 * 3.5% (= 2.625%) for 2 CONSECUTIVE runs

⚑ THE FLOOR WINS. A flagged position is NEVER trimmed below STARTER. A persistent flag makes it
a REPLACEMENT CANDIDATE in the s10 pairwise comparison: it is not sold for being volatile, it is
asked to justify a disproportionate risk share against the best alternative.

⚑ WHY A HYBRID AND NOT RISK PARITY. Sizing purely on inverse volatility would trim the sleeve's
best names for being volatile — and on this book that is measurably the wrong trade: MU never
flags (risk-implied 2.70-3.45%) while QBTS flags hard (2.19%). Raj chose the hybrid precisely so
that volatility RAISES A QUESTION rather than ISSUING AN ORDER.

═══════════════════════════════════════════════════════════════════════════════════════════
s8.1 — THREE MEASURES, THREE FAILURE MODES. All log from the FIRST run.
═══════════════════════════════════════════════════════════════════════════════════════════
M1 BINDINGNESS   fraction flagged each run. Fails if 0% or 100% over 6 runs -> non-informative,
                 the same defect as the 15.8% hurdle that excluded 0 of 13 names.
M2 PREDICTIVE    realised 3m vol and max drawdown of flagged vs unflagged. Fails if flagged
                 names are not measurably riskier -> the sigma estimate carries no information
                 and the rule is theatre.
M3 DECISION      for every flag, log incumbent AND the challenger it was compared against; mark
                 BOTH forward at 3/6/12m. Flags that did NOT result in a swap are the CONTROL
                 GROUP. Fails if mean excess of challenger over incumbent <= 0 -> the flag
                 identifies risk correctly but rotating on it destroys value.

⚑ "Anything that accrues a time series starts now; anything that analyses can wait." So this
module WRITES from run one and only READS once the series exists — and `evaluate()` refuses to
render a verdict before its declared minimum, rather than reporting a verdict on n=1.
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
from typing import Dict, List, Optional

# ── P0.1 LIVE-PATH EXECUTION LEDGER (framework_integrity) ──────────────────────────────
# ⚑ ONE LINE at the head of each capital-path function. `_mark` is a NO-OP when
# isa_policy.V2_FLAGS["execution_ledger"] is False, and it never raises into the caller — a
# monitoring hook that can break a capital run is a worse risk than the risk it monitors.
# The CALLS STAY IN THE CODE when the flag is off; removing them is what makes it droppable.
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None


HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(os.environ.get("ISA_OUT", HERE), "risk_contribution_ledger.json")

# ⚑ THE **REVIEW** FRACTION — and it is DELIBERATELY DISTINCT from
# `position_sizing.MIN_ENTRY_FRACTION_OF_STARTER` (0.80), which is the **ENTRY** floor.
#     0.75 REVIEW : "below this a HELD position is not carrying its risk share"
#     0.80 ENTRY  : "below this, do not OPEN a position at all"
# Two rules, two populations, two questions. ⚑ A future session WILL read them as a duplicate
# and unify them, silently moving the entry floor — which is why
# `consistency_check.pair_entry_and_review_fractions_distinct()` asserts both exist, are
# unequal, and each carries prose naming its own rule (P4.4).
FLAG_FRACTION_OF_STARTER = 0.75
FLAG_CONSECUTIVE_RUNS = 2
M1_MIN_RUNS = 6
M2_MIN_NAMES_PER_ARM = 3
M3_MIN_PAIRS = 5


def contributions(weights: Dict[str, float], sigmas: Dict[str, float],
                  starter_pct: float, matrix: Optional[Dict[str, float]] = None) -> dict:
    """rc_i and risk_weight_i for every name with BOTH a weight and a measured sigma.

    ⚑ A name with no measured sigma is EXCLUDED AND NAMED, never given a default. Defaulting a
    missing sigma would decide its risk share by the default, and the whole point of this
    instrument is that risk share is measured.

    ═══════════════════════════════════════════════════════════════════════════════════════
    P2 (28-Aug-2026) — `matrix` IS THE CORRELATION MATRIX, AND THE OLD FORMULA IS ITS rho==1
    SPECIAL CASE
    ═══════════════════════════════════════════════════════════════════════════════════════
        sigma_p       = sqrt( SUM_i SUM_j w_i w_j sigma_i sigma_j rho_ij )
        MCTR_i        = ( SUM_j w_j sigma_i sigma_j rho_ij ) / sigma_p
        rc_i          = w_i * MCTR_i / sigma_p
        risk_weight_i = (sigma_p / N) / MCTR_i

    ⚑⚑ WITH EVERY rho = 1: sigma_p = SUM(w*sigma), MCTR_i = sigma_i, and
    risk_weight_i = (SUM(w*sigma)/N)/sigma_i — **which is the existing formula, identically.**
    That reduction is not an ARGUMENT that the change is safe; **it IS the test**, and it is
    asserted (P2-A1) against an all-ones matrix to 1e-9.

    ⚑ WHY IT MATTERS, measured on this book: the correlation-blind `w*sigma` proxy is not
    merely imprecise, THE ERROR RUNS TOWARD THE RISK. It **overstates the diversifiers and
    understates the correlated core** — ONT.L overstated 4.4x, MU understated 8.9pp — so a
    replacement rule driven by it preferentially challenges the names that are REDUCING sleeve
    risk. `matrix=None` reproduces present behaviour exactly, stamped `w_sigma_proxy`.

    `matrix` is {"A|B": rho} as `stock_price_fetch.matrix()` emits it, in either key order."""
    _fi_mark("risk_contribution", "contributions")
    usable = {k: float(sigmas[k]) for k in weights
              if sigmas.get(k) is not None and float(sigmas.get(k) or 0) > 0
              and float(weights.get(k) or 0) > 0}
    excluded = [{"ticker": k, "weight_pct": weights.get(k),
                 "reason": "no measured sigma — excluded from the risk decomposition rather "
                           "than assigned a default (R4.1)"}
                for k in weights if k not in usable]
    if not usable:
        return {"measured": False, "rows": {}, "excluded": excluded,
                "threshold_pct": round(FLAG_FRACTION_OF_STARTER * starter_pct, 4),
                "detail": "no name has both a weight and a measured sigma"}
    n = len(usable)
    names = sorted(usable)
    thr = FLAG_FRACTION_OF_STARTER * float(starter_pct)

    rho, missing_pairs = _rho_lookup(names, matrix)
    basis = "covariance_mctr" if matrix is not None else "w_sigma_proxy"

    # sigma_p and MCTR. With rho == 1 everywhere this reduces EXACTLY to sum(w*sigma).
    var = 0.0
    for a in names:
        for b in names:
            var += (float(weights[a]) * float(weights[b]) * usable[a] * usable[b]
                    * rho(a, b))
    sigma_p = var ** 0.5
    if sigma_p <= 0:
        return {"measured": False, "rows": {}, "excluded": excluded, "rc_basis": basis,
                "threshold_pct": round(thr, 4),
                "detail": ("sigma_p is not positive — the decomposition REFUSES rather than "
                           "dividing by it (P2-A4)")}
    mctr = {a: sum(float(weights[b]) * usable[a] * usable[b] * rho(a, b)
                   for b in names) / sigma_p for a in names}
    mean_risk = sigma_p / n

    # ⚑ P2.3 PARTIAL COVERAGE, NOT REFUSAL (C3 — a correction to the 26-Aug design).
    # "Any missing pair ⇒ refuse the whole decomposition" is a CLIFF: add a 7th name and the
    # instrument goes dark for 52 weeks, exactly when the book changes, and M1/M2/M3 stop
    # accruing. The correct split is to refuse the CAPITAL CONSEQUENCE (the FLAG) for a name
    # whose pairs are unmeasured, while still PUBLISHING the decomposition with coverage as
    # the FIRST figure and every exclusion named with its weight — the concentration_clusters
    # precedent, which reports 77.5% rather than refusing.
    unmeasured_for = {a for (a, b) in missing_pairs} | {b for (a, b) in missing_pairs}
    rows = {}
    for k in names:
        s, w, m = usable[k], float(weights[k]), mctr[k]
        flag_suppressed = k in unmeasured_for and matrix is not None
        rows[k] = {
            "weight_pct": round(w, 4), "sigma": round(s, 6),
            "mctr": round(m, 6),
            "rc_share": round(w * m / sigma_p, 6),
            "fair_share": round(1.0 / n, 6),
            "risk_weight_pct": round(mean_risk / m, 4) if m > 0 else None,
            "below_tolerance": (bool((mean_risk / m) < thr) if (m > 0 and not flag_suppressed)
                                else False),
            "flag_suppressed_unmeasured_pair": flag_suppressed,
        }
    n_pairs = n * (n - 1) // 2
    coverage = round(1.0 - len(missing_pairs) / n_pairs, 4) if n_pairs else 1.0
    return {"measured": True,
            # ⚑ COVERAGE IS THE FIRST FIGURE, NOT A FOOTNOTE. A diversification statistic
            # quoted over 78% of a book and read as covering it is how a concentration goes
            # unnoticed.
            "pair_coverage": coverage,
            "n_pairs_measured": n_pairs - len(missing_pairs), "n_pairs_total": n_pairs,
            "unmeasured_pairs": ["%s|%s" % p for p in sorted(missing_pairs)],
            "flags_suppressed_for": sorted(unmeasured_for) if matrix is not None else [],
            "rc_basis": basis, "sigma_p": round(sigma_p, 6),
            "n_eff": round(1.0 / sum(r["rc_share"] ** 2 for r in rows.values()), 4)
                     if rows else None,
            "rows": rows, "excluded": excluded,
            "n_names": n, "threshold_pct": round(thr, 4),
            "sum_w_sigma": round(sum(float(weights[k]) * usable[k] for k in names), 6),
            "detail": (f"rc_basis={basis}; risk_weight = (sigma_p/{n}) / MCTR_i; FLAG below "
                       f"{FLAG_FRACTION_OF_STARTER} x STARTER {starter_pct}% = {thr:.3f}% "
                       f"for {FLAG_CONSECUTIVE_RUNS} consecutive runs. "
                       + ("With matrix=None every rho is 1 and this is the incumbent "
                          "w*sigma formula, identically (P2-A1)."
                          if matrix is None else
                          f"Pair coverage {coverage:.1%}; a name with any unmeasured pair "
                          f"is PUBLISHED and its FLAG is SUPPRESSED (P2.3/C3)."))}


def _rho_lookup(names, matrix):
    """-> (rho(a,b), missing_pairs). `matrix=None` ⇒ every rho is 1.0, which is the incumbent.

    ⚑ A missing pair defaults to 1.0 — the MOST ADVERSE value, not the most convenient. It
    maximises sigma_p and therefore never flatters diversification (A2.3's direction), and the
    pair is recorded so the FLAG is suppressed rather than issued on a guess."""
    if matrix is None:
        return (lambda a, b: 1.0), set()
    lut = {}
    for k, v in (matrix or {}).items():
        if v is None:
            continue
        if "|" in k:
            a, b = k.split("|", 1)
            lut[(a, b)] = float(v)
            lut[(b, a)] = float(v)
    missing = set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (a, b) not in lut:
                missing.add((a, b))

    def rho(a, b):
        if a == b:
            return 1.0
        return lut.get((a, b), 1.0)
    return rho, missing


def _load() -> dict:
    if not os.path.exists(LEDGER):
        return {"_what": "M1/M2/M3 ledger for the s8 risk-contribution hybrid (V2.1-D).",
                "runs": [], "flags": {}, "m3_pairs": []}
    with open(LEDGER, encoding="utf-8") as fh:
        return json.load(fh)


def _save(doc) -> str:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, LEDGER)
    return LEDGER


def _rollback_run(doc: dict, rd: str) -> bool:
    """Remove every trace of run_date `rd` and REDERIVE the flag state from what is left.

    ⚑ Rolling back the runs[] row alone would leave the `consecutive` counters advanced, so the
    second write of a month would still count twice. The counters are therefore recomputed from
    `history` rather than decremented — a decrement assumes the row being removed was the last
    one, and on a re-run of an older month it is not (R5.2: derive, do not adjust)."""
    seen = any(r.get("run") == rd for r in (doc.get("runs") or []))
    if not seen:
        return False
    doc["runs"] = [r for r in doc["runs"] if r.get("run") != rd]
    for k, st in (doc.get("flags") or {}).items():
        st["history"] = [h for h in st.get("history", []) if h.get("run") != rd]
        c = 0
        for h in st["history"]:
            c = c + 1 if h.get("below") else 0
        st["consecutive"] = c
        st["flagged"] = c >= FLAG_CONSECUTIVE_RUNS
        st["newly_flagged"] = False
    doc["m3_pairs"] = [p for p in (doc.get("m3_pairs") or []) if p.get("run_date") != rd]
    return True


def record_run(contrib: dict, *, run_date: Optional[str] = None, doc=None,
               persist: bool = True) -> dict:
    """M1 capture. Writes EVERY run, including runs where nothing flags — a zero is the
    observation that makes M1's 'never fires' failure mode detectable."""
    doc = doc if doc is not None else _load()
    rd = run_date or datetime.date.today().isoformat()
    # ⚑⚑ ISA-0549 (02-Sep-2026). record_run APPENDED unconditionally, so re-running a pre-run
    # for the same run_date added a SECOND observation of one month AND advanced every name's
    # `consecutive` counter a second time — inflating M1's series and bringing a position to
    # FLAG_CONSECUTIVE_RUNS on one month's evidence. A monthly ledger must be idempotent in its
    # own period: a re-run REPLACES that date, it does not accrue against it (R4.11).
    _rollback_run(doc, rd)
    rows = contrib.get("rows") or {}
    below = sorted(k for k, v in rows.items() if v["below_tolerance"])
    flagged = []
    for k in rows:
        st = doc["flags"].setdefault(k, {"consecutive": 0, "flagged": False, "history": []})
        st["consecutive"] = st["consecutive"] + 1 if k in below else 0
        was = st["flagged"]
        st["flagged"] = st["consecutive"] >= FLAG_CONSECUTIVE_RUNS
        st["history"].append({"run": rd, "below": k in below,
                              "risk_weight_pct": rows[k]["risk_weight_pct"],
                              "rc_share": rows[k]["rc_share"]})
        if st["flagged"]:
            flagged.append(k)
        st["newly_flagged"] = st["flagged"] and not was
    doc["runs"].append({
        # ⚑ P2.4 LEDGER CONTINUITY. Every entry carries its rc_basis, and M1/M2/M3 SEGMENT by
        # basis and REFUSE to pool across a basis change: "3 runs on covariance_mctr, 2 on
        # w_sigma_proxy; the minimum of 6 applies PER BASIS and these are not one series."
        # Pooling them would answer a question about one instrument using observations from
        # two. Prior proxy entries are RETAINED and MARKED, never rewritten (R2.13).
        "rc_basis": contrib.get("rc_basis", "w_sigma_proxy"),
        "pair_coverage": contrib.get("pair_coverage"),
        "sigma_p": contrib.get("sigma_p"), "n_eff": contrib.get("n_eff"),
        "run": rd, "n_names": len(rows), "n_below_tolerance": len(below),
        "n_flagged": len(flagged), "below": below, "flagged": sorted(flagged),
        "fraction_flagged": round(len(flagged) / len(rows), 4) if rows else None,
        "threshold_pct": contrib.get("threshold_pct"),
        "excluded": [e["ticker"] for e in contrib.get("excluded", [])],
    })
    if persist:
        _save(doc)
    return {"run": rd, "flagged": sorted(flagged), "below_tolerance": below,
            "floor_protects": ("a flagged position is NEVER trimmed below STARTER — it becomes "
                               "a REPLACEMENT CANDIDATE in the s10 comparison"),
            "doc": doc}


def record_m3_pair(doc, *, run_date, incumbent, challenger, swapped: bool,
                   incumbent_price=None, challenger_price=None, persist=True) -> dict:
    """M3 capture. ⚑ Flags that did NOT result in a swap are the CONTROL GROUP — logging only
    the swaps would measure the swaps against nothing and could never falsify the rule."""
    doc["m3_pairs"].append({
        "run": run_date, "incumbent": incumbent, "challenger": challenger,
        "swapped": bool(swapped), "arm": "treatment" if swapped else "control",
        "incumbent_price_at_flag": incumbent_price,
        "challenger_price_at_flag": challenger_price,
        "marks": {"3m": None, "6m": None, "12m": None},
    })
    if persist:
        _save(doc)
    return doc["m3_pairs"][-1]


def evaluate(doc=None, *, rc_basis: Optional[str] = None) -> dict:
    """M1/M2/M3 verdicts. REFUSES to render one before its declared minimum sample (R3.5).

    ⚑ P2.4 — SEGMENTED BY `rc_basis`, AND IT REFUSES TO POOL ACROSS ONE. `w_sigma_proxy` and
    `covariance_mctr` are two different instruments measuring the same thing differently; six
    runs made of three of each is not six observations of either. The minimum applies PER
    BASIS, and the refusal SAYS SO with the counts rather than quietly reporting the pooled
    verdict. Prior proxy entries are RETAINED and MARKED, never rewritten (R2.13)."""
    doc = doc if doc is not None else _load()
    all_runs = doc.get("runs") or []
    by_basis = {}
    for r in all_runs:
        by_basis.setdefault(r.get("rc_basis", "w_sigma_proxy"), []).append(r)
    if rc_basis is None:
        # the CURRENT basis is the one the most recent run used
        rc_basis = (all_runs[-1].get("rc_basis", "w_sigma_proxy") if all_runs
                    else "w_sigma_proxy")
    runs = by_basis.get(rc_basis, [])
    out = {"rc_basis": rc_basis,
           "runs_by_basis": {k: len(v) for k, v in sorted(by_basis.items())},
           "basis_note": ("M1/M2/M3 are evaluated on the %r series ONLY. %s — the minimum of "
                          "%d applies PER BASIS and these are not one series."
                          % (rc_basis,
                             "; ".join("%d run(s) on %s" % (len(v), k)
                                       for k, v in sorted(by_basis.items())) or "no runs",
                             M1_MIN_RUNS))}

    # ── M1 bindingness ────────────────────────────────────────────────────────────────
    if len(runs) < M1_MIN_RUNS:
        out["M1"] = {"verdict": "INSUFFICIENT_DATA", "runs": len(runs),
                     "detail": (f"{len(runs)} of {M1_MIN_RUNS} runs. A bindingness verdict on "
                                f"fewer runs would be a verdict about the sample.")}
    else:
        fr = [r["fraction_flagged"] for r in runs[-M1_MIN_RUNS:]
              if r["fraction_flagged"] is not None]
        allz, allone = all(f == 0 for f in fr), all(f == 1 for f in fr)
        out["M1"] = {
            "verdict": "NON_INFORMATIVE" if (allz or allone) else "BINDING",
            "runs": len(runs), "recent_fractions": fr,
            "detail": (("the flag fired on NO position in "
                        f"{M1_MIN_RUNS} consecutive runs — non-informative, the same defect as "
                        "the 15.8% hurdle that excluded 0 of 13 names. Rescale or remove it."
                        if allz else
                        f"the flag fired on EVERY position in {M1_MIN_RUNS} consecutive runs — "
                        "it is not discriminating. Rescale it.")
                       if (allz or allone) else
                       f"fires on {statistics.fmean(fr):.1%} of positions on average — binding "
                       f"without being universal")}

    # ── M2 predictive validity ────────────────────────────────────────────────────────
    fl = {k: v for k, v in (doc.get("flags") or {}).items() if v.get("flagged")}
    unfl = {k: v for k, v in (doc.get("flags") or {}).items() if not v.get("flagged")}
    if len(fl) < M2_MIN_NAMES_PER_ARM or len(unfl) < M2_MIN_NAMES_PER_ARM:
        out["M2"] = {"verdict": "INSUFFICIENT_DATA",
                     "n_flagged": len(fl), "n_unflagged": len(unfl),
                     "detail": (f"need >= {M2_MIN_NAMES_PER_ARM} names per arm; a comparison of "
                                f"{len(fl)} vs {len(unfl)} would not survive one outlier")}
    else:
        out["M2"] = {"verdict": "PENDING_REALISED_DATA", "n_flagged": len(fl),
                     "n_unflagged": len(unfl),
                     "detail": "arms are populated; realised 3m vol and max drawdown are "
                               "marked forward and compared once the marks land"}

    # ── M3 decision value ─────────────────────────────────────────────────────────────
    pairs = doc.get("m3_pairs") or []
    marked = [p for p in pairs if p["marks"].get("3m") is not None]
    if len(marked) < M3_MIN_PAIRS:
        out["M3"] = {"verdict": "INSUFFICIENT_DATA", "pairs": len(pairs), "marked": len(marked),
                     "treatment": sum(1 for p in pairs if p["arm"] == "treatment"),
                     "control": sum(1 for p in pairs if p["arm"] == "control"),
                     "detail": (f"{len(marked)} of {M3_MIN_PAIRS} marked pairs. ⚑ The control "
                                f"arm (flags that did NOT lead to a swap) must be populated too "
                                f"— measuring only the swaps compares them against nothing.")}
    else:
        ex = [p["marks"]["3m"] for p in marked]
        mean_ex = statistics.fmean(ex)
        out["M3"] = {"verdict": "ACT" if mean_ex > 0 else "STOP_ACTING",
                     "pairs": len(marked), "mean_excess_3m": round(mean_ex, 4),
                     "detail": ("challengers beat incumbents on average — acting on the flag pays"
                                if mean_ex > 0 else
                                "⚑ mean excess <= 0: the flag identifies risk correctly but "
                                "rotating on it DESTROYS value. Keep M1/M2 as diagnostics and "
                                "STOP ACTING on it.")}
    out["_note"] = ("All three log from the first run. A verdict is REFUSED below its declared "
                    "minimum rather than rendered on a thin sample (R3.5).")
    return out


def _selftest():
    import tempfile
    global LEDGER
    LEDGER = os.path.join(tempfile.mkdtemp(), "rc.json")

    # the worked example from clean spec s8: five holdings at 3.5%, one at sigma 0.90
    w = {"A": 3.5, "B": 3.5, "C": 3.5, "D": 3.5, "E": 3.5}
    s = {"A": 0.40, "B": 0.55, "C": 0.30, "D": 0.45, "E": 0.90}
    c = contributions(w, s, starter_pct=3.5)
    assert c["measured"] and abs(c["threshold_pct"] - 2.625) < 1e-9, c["threshold_pct"]
    e = c["rows"]["E"]
    assert abs(e["rc_share"] - 0.346) < 0.005, e["rc_share"]      # 34.6% of sleeve risk
    assert abs(e["fair_share"] - 0.20) < 1e-9
    assert abs(e["risk_weight_pct"] - 2.02) < 0.02, e["risk_weight_pct"]
    assert e["below_tolerance"] is True
    assert c["rows"]["C"]["below_tolerance"] is False

    # MU never flags (2.70-3.45%); QBTS flags hard (2.19%)
    live_w = {"MU": 3.45, "QBTS": 2.19, "AVGO": 3.52}
    live_s = {"MU": 0.52, "QBTS": 0.95, "AVGO": 0.38}
    lc = contributions(live_w, live_s, starter_pct=3.5)
    assert lc["rows"]["QBTS"]["below_tolerance"] is True, lc["rows"]["QBTS"]
    assert lc["rows"]["MU"]["below_tolerance"] is False, lc["rows"]["MU"]

    # a missing sigma is EXCLUDED AND NAMED, never defaulted
    mc = contributions({"A": 3.5, "X": 3.5}, {"A": 0.4, "X": None}, starter_pct=3.5)
    assert [x["ticker"] for x in mc["excluded"]] == ["X"], mc["excluded"]
    assert "X" not in mc["rows"]

    # a flag needs TWO consecutive runs
    doc = {"runs": [], "flags": {}, "m3_pairs": []}
    r1 = record_run(c, run_date="2026-09-01", doc=doc, persist=False)
    assert r1["flagged"] == [] and r1["below_tolerance"] == ["E"], r1
    r2 = record_run(c, run_date="2026-10-01", doc=doc, persist=False)
    assert r2["flagged"] == ["E"], r2
    # ...and a clean run RESETS the counter
    clean = contributions(w, {**s, "E": 0.40}, starter_pct=3.5)
    r3 = record_run(clean, run_date="2026-11-01", doc=doc, persist=False)
    assert r3["flagged"] == [], r3
    assert doc["flags"]["E"]["consecutive"] == 0

    # M1 refuses a verdict on a thin sample, then reports NON_INFORMATIVE on all-zero
    assert evaluate(doc)["M1"]["verdict"] == "INSUFFICIENT_DATA"
    d0 = {"runs": [{"run": f"r{i}", "n_names": 5, "n_below_tolerance": 0, "n_flagged": 0,
                    "below": [], "flagged": [], "fraction_flagged": 0.0}
                   for i in range(6)], "flags": {}, "m3_pairs": []}
    m1 = evaluate(d0)["M1"]
    assert m1["verdict"] == "NON_INFORMATIVE" and "15.8" in m1["detail"], m1
    d1 = {"runs": [{"run": f"r{i}", "n_names": 5, "n_below_tolerance": 1, "n_flagged": 1,
                    "below": ["E"], "flagged": ["E"], "fraction_flagged": 0.2}
                   for i in range(6)], "flags": {}, "m3_pairs": []}
    assert evaluate(d1)["M1"]["verdict"] == "BINDING"

    # M3 records the CONTROL arm too
    d3 = {"runs": [], "flags": {}, "m3_pairs": []}
    record_m3_pair(d3, run_date="2026-09-01", incumbent="E", challenger="F",
                   swapped=False, persist=False)
    record_m3_pair(d3, run_date="2026-09-01", incumbent="G", challenger="H",
                   swapped=True, persist=False)
    arms = {p["arm"] for p in d3["m3_pairs"]}
    assert arms == {"control", "treatment"}, arms
    m3 = evaluate(d3)["M3"]
    assert m3["verdict"] == "INSUFFICIENT_DATA" and m3["control"] == 1, m3
    # a populated, negative M3 says STOP ACTING
    d4 = {"runs": [], "flags": {}, "m3_pairs": [
        {"run": "r", "incumbent": "I", "challenger": "C", "swapped": True, "arm": "treatment",
         "marks": {"3m": -2.0, "6m": None, "12m": None}} for _ in range(5)]}
    assert evaluate(d4)["M3"]["verdict"] == "STOP_ACTING"
    print("risk_contribution selftest OK (20 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps(evaluate(), indent=1))
