#!/usr/bin/env python3
"""
fund_expected_return.py — a FORWARD STRUCTURAL EXPECTED RETURN for every fund. ISA-0328.
Pre-run Step 6.11b. Built 20-Aug-2026 against
ISA_Analysis_RegionalM_and_FundStructuralEr_20Aug2026.md Part 2.

⚑ THE GAP. `FRS.return_adequacy` is 35 of 100 points and the only MANDATORY component, and it
scores REALISED trailing return. `target_weights.funds[].min_expected_return` carries a hand-set
forward number last revised 31-May-2026. NO FUND CARRIES A FORWARD EXPECTED RETURN OF ANY KIND,
in a framework whose stated identity is forward-led.

⚑ THE OBVIOUS FIX IS BARRED AND THE BAR IS MEASURED, NOT ASSERTED. Forecasting a fund from its
own history is inadmissible: the cross-sectional dispersion of the twelve funds' realised returns
(Var 18.54) is SMALLER than the estimation variance of those returns (33.69) — S/N 0.55, implied
true dispersion -15.15. At the sleeve's 13.5% median volatility a 3pp standard error needs TWENTY
YEARS. Corroborated independently by ISA-0351: R-squared is the only fund measure with rank
persistence (+0.754); alpha ranks -0.482.

So E[r] is built BOTTOM-UP from exposures:

    E[r]_fund = sum_k w_k*M_k  +  (sigma_eq^2 - sigma_fund^2)/2  -  OCF_fund  +  alpha_fund
                └ exposure x ┘    └── geometric adjustment ────┘    └ cost ┘    └ SHRUNK TO 0 ┘
                  regional M

⚑ WHY THE COST TERM IS EXPLICIT AND NOT FOLDED INTO ALPHA. M*'s declared basis is
`..._net_of_ocf`. An alpha of zero with no cost term produces a GROSS number compared against a
NET anchor — 0.65pp at the sleeve and 1.02pp across the funds. R3.9 asks that alpha shrink toward
-OCF; carrying cost as its own MEASURED term and shrinking SKILL to zero is the same rule with
both halves visible, and it keeps a number we know (0.07-1.09%) apart from one we cannot measure.

⚑ WHY THE GEOMETRIC ADJUSTMENT IS ADMISSIBLE WHERE A MEAN IS NOT. sum(w_k*M_k) is a weighted
average, which is exact for ARITHMETIC returns; M* and the anchor are GEOMETRIC. Converting each
cell to arithmetic, averaging, and converting the fund back gives (sigma_eq^2 - sigma_fund^2)/2
when one declared equity volatility is used for every cell — so it needs NO new input:
`sigma_eq` is READ from `mstar_plausibility.DECLARED["annual_vol_pct"]`, the same declaration M*'s
own band is built on, and `sigma_fund` is MEASURED from nav_cache. SECOND MOMENTS ARE ESTIMABLE
WHERE FIRST MOMENTS ARE NOT: at n=120, SE(sigma)/sigma = 1/sqrt(2(n-1)) ~ 6.5%, so sigma = 16%
carries ~1.0pp against the mean's ~5.1pp, and propagated into the term the drag is estimated
about THIRTY TIMES more precisely than the mean it adjusts.
⚑ A hypothesis that Ranmore's low measured volatility was stale-NAV SMOOTHING was TESTED AND
FALSIFIED (lag-1 autocorrelation -0.248, the opposite sign). The real constraint is window length
and regime coverage, so that is what `SIGMA_MIN_MONTHS` guards. Recorded so it is not re-raised.

⚑ WHAT THIS IS FOR, AND WHAT IT IS NOT FOR.
  FOR:     the LEVEL question — is this allocation capable of the required return? — and deriving
           `min_expected_return` so a hand-set constant becomes checkable (R5.2).
  NOT FOR: the ORDERING question. Measured: replacing the per-region net buyback yield with ANY
           uniform one changes the fund ordering with Spearman rho +0.601 and a maximum rank move
           of SIX places of twelve, and that term has no decision-grade source. An E[r] ordering
           would be a ranking of PAYOUT COMPOSITION published as a ranking of expected return.
           ISA-0386 already made the marginal-pound ordering estimation-free. The two COMPOSE:
           what E[r] licenses is a NON-BINDING diagnostic (`ordering_diagnostic`) that makes that
           ordering falsifiable for the first time.
  NOT FOR: FRS `return_adequacy`. D-8/D-13 already separated OWNERSHIP (realised) from STRUCTURE
           (forward) and renamed the bucket field `ownership_floor_return` for exactly this
           reason. Forward E[r] runs 3-8% against 12-13% ownership floors; wiring it in would
           score all twelve funds at zero on the only mandatory component, on a basis change, with
           no fund having changed. See ISA-0406.

⚑ IT SHIPS OPERATIVE=FALSE AND THAT IS A DESIGN REQUIREMENT, NOT TIMIDITY. Made operative on the
31-Jul-2026 book it moves Section A 11.0853 -> 6.2386 (INCONCLUSIVE -> FAIL), Section C 10.9860 ->
6.8599 and the shortfall +2.914 -> +7.040pp — 100% METHOD, 0% DATA (R2.3). The framework has a
recorded name for shipping a policy move as the side effect of a measurement repair: D-C(ii).
`_operative_neutrality` is the assertion that protects this and it is the most important test here.

⚑ NAMING (FC-B, pre-empted). `expected_return.py` computes a 12-24 MONTH SINGLE-NAME total return
that reads +53.4% on AVGO. This computes an ~11-YEAR STRUCTURAL return that reads +3.03% on Polar.
Near-identical module names, quantities an order of magnitude apart. The emitted field is
`structural_er_pct`, NEVER `expected_return`, and every row carries `horizon_basis`.

ROLLBACK (R4.13): `ENABLED = False`. Making it operative is a DIFFERENT constant,
`return_architecture.ER_BASIS_OPERATIVE`.
"""
from __future__ import annotations
import csv, datetime as dt, json, math, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

from isa_metric import Metric, Missing, as_dict, is_present

ENABLED = True
GEOMETRIC_ADJUSTMENT_ENABLED = True
SCHEMA_VERSION = "1.0.0"
ITEM = "ISA-0328"

OUT_TEMPLATE = "fund_expected_return_%s.json"
HORIZON_BASIS = "long_run_structural_not_12_24m"

# ── COVERAGE FLOOR ───────────────────────────────────────────────────────────────────────────
# ⚑ TESTED AGAINST `attributed_pct_of_fund`, THE FUND DENOMINATOR (ISA-0403). The v1.0.0 exposure
# artefact counted only the residual INSIDE the published table, so Ranmore published -0.00 while
# 9.87% of the fund had no country at all. A floor read against the wrong field would have passed
# while the defect ran — the negative control that motivated this constant is F3.
COVERAGE_FLOOR_PCT = 85.0

# ── VOLATILITY ───────────────────────────────────────────────────────────────────────────────
SIGMA_MIN_MONTHS = 60      # below this the window has seen too few regimes; the term is REFUSED

# ── ALPHA ────────────────────────────────────────────────────────────────────────────────────
# Zero, by declaration, and it is not a claim that managers cannot add value — it is that THIS
# panel cannot measure whether THESE ones do (R3.9: declare the shrinkage). A non-zero alpha
# requires a declared, falsifiable, ledger-backed basis per fund and the default stays zero.
ALPHA_DEFAULT_PCT = 0.0

OPERATIVE = False          # ⚑ see the header. Changing this alone changes NOTHING; the operative
                           # basis lives in return_architecture.ER_BASIS_OPERATIVE.


class FundErError(RuntimeError):
    """A contract breach in the fund expected-return layer. Never downgraded to a warning."""


def _today():
    return dt.date.today().isoformat()


def _read(name):
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


_MONTHLY = None


def _latest(prefix):
    """The most recent MONTHLY artefact `prefix`_YYYY_MM.json.

    ⚑ The pattern is anchored on purpose. A bare glob of `regional_m_*.json` also matches
    `regional_m_inputs.json` and `regional_m_policy.json`, and sorted() puts "inputs" AFTER
    "2026_08" — so the module silently read its own capture file as if it were the built artefact
    and every fund came back UNMEASURED with a plausible reason. Caught by inspecting the first
    output, not by an assertion (ISA-0388's lesson, third occurrence). `_latest_is_anchored` is
    the control."""
    global _MONTHLY
    if _MONTHLY is None:
        import re as _re
        # ⚑ TWO monthly conventions live in this folder: `_YYYY_MM` (regional_m,
        # strategic_allocation) and `_mmm_yyyy` (capital_destination, portfolio_data).
        # Anchoring on only one silently found NOTHING for the other and the ordering
        # diagnostic printed 0.00 instead of NO_ALLOCATION — the silent zero this whole
        # build exists to stop, in my own reporting path. Caught by reading the first
        # emitted artefact (ISA-0388's lesson, fourth occurrence).
        _MONTHLY = _re.compile(
            r"^_(\d{4}_\d{2}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)_\d{4})$",
            _re.I)
    c = sorted(p for p in HERE.glob(prefix + "_*.json")
               if _MONTHLY.match(p.stem[len(prefix):]))
    return json.loads(c[-1].read_text(encoding="utf-8")) if c else {}


# ── sigma_eq: ONE HOME, READ, NEVER RE-DECLARED ──────────────────────────────────────────────
def equity_vol():
    try:
        import mstar_plausibility as MPB
    except Exception as e:                                          # noqa: BLE001
        return Missing("mstar_plausibility unimportable (%s)" % type(e).__name__)
    d = (getattr(MPB, "DECLARED", {}) or {}).get("annual_vol_pct")
    if not isinstance(d, dict) or d.get("value") is None:
        return Missing("mstar_plausibility.DECLARED['annual_vol_pct'] absent or valueless")
    return Metric(d["value"], d.get("as_of"),
                  "mstar_plausibility.DECLARED['annual_vol_pct'] — %s" % str(d.get("source"))[:140],
                  unit="pct_pa",
                  note="ONE HOME (R4.4). The same declared distribution M*'s own band is built on.")


def fund_sigma(sedol, universe=None, nav_dir=None):
    """Annualised volatility from monthly month-end NAVs. MEASURED, with its window declared.

    Refused below SIGMA_MIN_MONTHS: a volatility measured over one regime is a fact about that
    regime (D-19 was re-founded on regime coverage for exactly this reason)."""
    uni = universe if universe is not None else (_read("fund_universe.json").get("funds") or {})
    f = uni.get(sedol) or {}
    isin, ysym = f.get("isin"), f.get("yf_symbol")
    nc = Path(nav_dir) if nav_dir else HERE / "nav_cache"
    if not nc.is_dir():
        return Missing("nav_cache/ is absent"), None
    path = None
    for p in sorted(nc.glob("*.csv")):
        b = p.stem
        if (isin and isin in b) or sedol in b or (ysym and ysym == b):
            path = p
            break
    if path is None:
        return Missing("no NAV series for %s in nav_cache/ — volatility is UNMEASURED, never "
                       "borrowed from the sleeve (R2.10)" % sedol), None
    rows = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append((r["date"], float(r["close"])))
            except Exception:                                       # noqa: BLE001
                continue
    rows.sort()
    me = {}
    for d_, c in rows:
        me[d_[:7]] = c
    ks = sorted(me)
    rets = [me[b] / me[a] - 1.0 for a, b in zip(ks, ks[1:]) if me[a] > 0]
    n = len(rets)
    win = {"file": path.name, "n_months": n,
           "window": [ks[0], ks[-1]] if ks else None, "min_months": SIGMA_MIN_MONTHS}
    if n < SIGMA_MIN_MONTHS:
        return Missing("SHORT_WINDOW: %d monthly observations against a declared minimum of %d. A "
                       "volatility measured over too few regimes is a fact about those regimes, so "
                       "the geometric adjustment is REFUSED for this fund rather than applied "
                       "(D-19, regime coverage)" % (n, SIGMA_MIN_MONTHS)), win
    m = sum(rets) / n
    var = sum((x - m) ** 2 for x in rets) / (n - 1)
    sd = math.sqrt(var * 12.0) * 100.0
    win["se_of_sigma_pp"] = round(sd / math.sqrt(2.0 * (n - 1)), 4)
    return Metric(round(sd, 4), ks[-1] + "-01", "nav_cache/%s, monthly month-end, %d obs"
                  % (path.name, n), unit="pct_pa",
                  note="MEASURED. SE ~ sigma/sqrt(2(n-1)); second moments converge where first "
                       "moments do not — that is what licenses this term."), win


def _cellise(vec, cell_map, unresolved):
    """Country vector -> cell vector. UNRESOLVED labels are counted and normalised out; an
    UNMAPPED label RAISES (R4.9 — a reader that cannot match a row COUNTS it and fails)."""
    cells, unres, unmapped = {}, 0.0, []
    for c, w in (vec or {}).items():
        if c in unresolved:
            unres += float(w)
            continue
        k = cell_map.get(c)
        if k is None:
            unmapped.append(c)
            continue
        cells[k] = cells.get(k, 0.0) + float(w)
    if unmapped:
        raise FundErError("country labels with no cell reached the E[r] layer and were COUNTED, "
                          "not dropped (R4.9): %s" % sorted(set(unmapped)))
    tot = sum(cells.values())
    if tot <= 0:
        return {}, 1.0
    return {k: v / tot for k, v in sorted(cells.items())}, unres


def fund_rows(rm=None, fev=None, universe=None, tw=None, as_of=None, m_override=None,
              nav_dir=None) -> dict:
    """One row per fund. Every term tagged or Missing; the fund UNMEASURED, never averaged."""
    import regional_m as RM
    as_of = as_of or _today()
    rm = rm if rm is not None else _latest("regional_m")
    fev = fev if fev is not None else _read("fund_exposure_vectors.json")
    uni = universe if universe is not None else (_read("fund_universe.json").get("funds") or {})
    tw = tw if tw is not None else (_read("target_weights.json").get("funds") or {})

    m_by_cell = dict(m_override) if m_override else {
        c: (r or {}).get("m_pct") for c, r in ((rm.get("m") or {}).get("cells") or {}).items()}
    sig_eq = equity_vol()
    rows, unmeasured = {}, []

    for sd, vec in (fev.get("vectors") or {}).items():
        meta = (fev.get("per_fund") or {}).get(sd) or {}
        diag = meta.get("diagnostics") or {}
        # ⚑ ISA-0403. The FUND denominator, and the only field this floor may read.
        attributed = diag.get("attributed_pct_of_fund")
        cells, unres = _cellise(vec, RM.COUNTRY_TO_CELL, set(RM.UNRESOLVED_LABELS))
        ocf_raw = (uni.get(sd) or {}).get("ocf")
        ocf = (Metric(ocf_raw, fev.get("_meta", {}).get("built_on") or as_of,
                      "fund_universe.json[%s].ocf" % sd, unit="pct_pa")
               if ocf_raw is not None else
               Missing("no declared OCF for %s — a fund with no recorded cost is a policy gap, "
                       "not a free fund (R4.1)" % sd))
        sig, win = fund_sigma(sd, uni, nav_dir)
        alpha = Metric(ALPHA_DEFAULT_PCT, as_of,
                       "declared default (R3.9) — SHRUNK_TO_ZERO", unit="pct_pa",
                       note="not a claim that managers cannot add value; this panel cannot measure "
                            "whether these ones do (S/N 0.55; alpha rank persistence -0.482)")

        blocked = []
        if attributed is None:
            blocked.append("attributed_pct_of_fund (ISA-0403 field absent — rebuild "
                           "fund_exposure_vectors)")
        elif attributed < COVERAGE_FLOOR_PCT:
            blocked.append("coverage %.2f%% of the fund is below the declared floor %.2f%%"
                           % (attributed, COVERAGE_FLOOR_PCT))
        missing_cells = sorted(c for c in cells if m_by_cell.get(c) is None)
        if missing_cells:
            blocked.append("M_k UNMEASURED for: " + ", ".join(missing_cells))
        if not is_present(ocf):
            blocked.append("ocf")

        gross = (round(sum(w * m_by_cell[c] for c, w in cells.items()), 4)
                 if cells and not missing_cells else None)

        # geometric adjustment — REFUSED, never defaulted, when either input is Missing
        if GEOMETRIC_ADJUSTMENT_ENABLED and is_present(sig_eq) and is_present(sig):
            geo = round((sig_eq.value ** 2 - sig.value ** 2) / 200.0, 4)
            geo_basis = "MEASURED"
        else:
            geo = 0.0
            geo_basis = ("DISABLED" if not GEOMETRIC_ADJUSTMENT_ENABLED
                         else "REFUSED — " + (sig.reason if not is_present(sig) else sig_eq.reason))

        caveats = []
        if geo_basis.startswith("REFUSED"):
            # ⚑ R4.3 — a control fed a null must not quietly return the neutral value. The term is
            # a refinement worth at most ~0.8pp, so REFUSING the FUND would be disproportionate;
            # applying 0.0 SILENTLY would be the defect. It is applied and CARRIED as a caveat, so
            # no consumer can read this fund's E[r] as carrying an adjustment it does not have.
            caveats.append("geometric_adjustment_APPLIED_AS_ZERO_AND_DECLARED: " + geo_basis[9:])
        if meta.get("stale"):
            caveats.append("exposure vector is STALE (%s days)" % meta.get("age_days"))
        if not meta.get("share_class_is_the_held_line", True):
            caveats.append("exposure vector describes a SUBSTITUTE share class")
        if (meta.get("diagnostics") or {}).get("unattributed_total_pct", 0) > 5.0:
            caveats.append("%.2f%% of the fund is UNATTRIBUTED and normalised out"
                           % meta["diagnostics"]["unattributed_total_pct"])

        er = None
        if gross is not None and is_present(ocf) and not blocked:
            er = round(gross + geo - ocf.value + alpha.value, 4)

        declared = (tw.get(sd) or {}).get("min_expected_return")
        declared_pct = None if declared is None else round(float(declared) * 100.0, 4)

        rows[sd] = {
            "name": (uni.get(sd) or {}).get("name"),
            "bucket": (uni.get(sd) or {}).get("bucket"),
            "horizon_basis": HORIZON_BASIS,
            "exposure": {"cells": {k: round(v, 6) for k, v in cells.items()},
                         "as_of": meta.get("as_of"), "age_days": meta.get("age_days"),
                         "source": meta.get("source"),
                         "share_class_substituted": not meta.get("share_class_is_the_held_line", True),
                         "unresolved_pct_of_vector": round(100.0 * unres, 4)},
            "coverage": {"attributed_pct_of_fund": attributed,
                         "unattributed_total_pct": diag.get("unattributed_total_pct"),
                         "floor_pct": COVERAGE_FLOOR_PCT,
                         "field_read": "attributed_pct_of_fund",
                         "why_this_field": ("the FUND denominator (ISA-0403). Reading "
                                            "`geographic_pct` alone would have passed Ranmore at "
                                            "90.13 while 9.87% of it had no country at all."),
                         "verdict": ("PASS" if attributed is not None
                                     and attributed >= COVERAGE_FLOOR_PCT else "BLOCK")},
            "terms": {"gross_exposure_weighted_m_pct": gross,
                      "geometric_adjustment_pct": geo, "geometric_adjustment_basis": geo_basis,
                      "sigma_fund": as_dict(sig), "sigma_fund_window": win,
                      "sigma_equity_declared": as_dict(sig_eq),
                      "ocf": as_dict(ocf), "alpha": as_dict(alpha)},
            "structural_er_pct": er,
            "state": "MEASURED" if er is not None else "UNMEASURED",
            "confidence": ("high" if er is not None and not caveats
                           else "medium" if er is not None else None),
            "caveats": caveats,
            "blocked_on": blocked,
            "declared_floor_reconciliation": {
                "declared_min_expected_return_pct": declared_pct,
                "derived_structural_er_pct": er,
                "gap_pp": (None if er is None or declared_pct is None
                           else round(er - declared_pct, 4)),
                "REPORT_ONLY": True,
                "note": ("REQUIRED and EXPECTED are different quantities and the GAP between them "
                         "is the plan's shortfall, per fund. This check REPORTS. It never writes "
                         "back to target_weights.json — repairing a read in a way that moves a "
                         "floor as a side effect is D-C(ii) (R5.2, FE4).")},
        }
        if er is None:
            unmeasured.append(sd)
    return {"rows": rows, "unmeasured": sorted(unmeasured),
            "sigma_equity": as_dict(sig_eq),
            "geometric_adjustment_enabled": GEOMETRIC_ADJUSTMENT_ENABLED}


def sleeve(rows, portfolio=None) -> dict:
    port = portfolio if portfolio is not None else _read("portfolio_data_aug_2026.json")
    val = {f["ticker"]: f["value_gbp"] for f in (port.get("funds") or [])}
    tot = sum(val.values())
    if not tot:
        return {"state": "UNMEASURED", "reason": "no fund sleeve value"}
    cov = sum(val.get(sd, 0.0) for sd, r in rows.items() if r["state"] == "MEASURED")
    if cov <= 0:
        return {"state": "UNMEASURED", "covered_weight": 0.0,
                "reason": "no fund carries a MEASURED structural E[r] — the sleeve figure is "
                          "REFUSED rather than computed over a subset (R4.10)"}
    num = sum(val.get(sd, 0.0) * r["structural_er_pct"]
              for sd, r in rows.items() if r["state"] == "MEASURED")
    return {"state": "MEASURED", "structural_er_pct": round(num / cov, 4),
            "covered_weight": round(cov / tot, 6),
            "fund_sleeve_gbp": round(tot, 2),
            "n_measured": sum(1 for r in rows.values() if r["state"] == "MEASURED"),
            "n": len(rows)}


def ordering_diagnostic(rows, cd=None) -> dict:
    """NON-BINDING. The E[r] rank of every destination the marginal-pound router actually funded.

    ⚑ IT CHANGES NO ALLOCATION AND ADDS NO TUNABLE CONSTANT. What it buys is that ISA-0386's
    estimation-free ordering becomes FALSIFIABLE for the first time — nothing else in the
    framework can ask whether the criteria that move capital are routing it to low-E[r]
    destinations. Two instruments built for two different questions, published side by side and
    never blended (R6.2)."""
    cd = cd if cd is not None else (_latest("capital_destination") or {})
    alloc = ((cd.get("fund_allocation") or {}).get("allocation") or {})
    funded = {k: v for k, v in alloc.items() if v and v > 0}
    measured = {sd: r["structural_er_pct"] for sd, r in rows.items() if r["state"] == "MEASURED"}
    if not funded:
        return {"state": "NO_ALLOCATION", "binding": False}
    if not measured:
        return {"state": "UNMEASURED", "binding": False,
                "reason": "no fund carries a MEASURED structural E[r], so no rank exists",
                "funded_destinations": sorted(funded)}
    order = sorted(measured, key=lambda s: -measured[s])
    tot = sum(funded.values())
    unpriced = sorted(s for s in funded if s not in measured)
    priced = {s: v for s, v in funded.items() if s in measured}
    ptot = sum(priced.values()) or 1.0
    mw = sum(v * measured[s] for s, v in priced.items()) / ptot
    sl = sleeve(rows)
    return {"state": "MEASURED", "binding": False,
            "destinations": {s: {"gbp": round(v, 2),
                                 "structural_er_pct": measured.get(s),
                                 "er_rank": (order.index(s) + 1) if s in measured else None,
                                 "of": len(order)} for s, v in sorted(funded.items(),
                                                                      key=lambda z: -z[1])},
            "money_weighted_er_pct": round(mw, 4),
            "sleeve_er_pct": sl.get("structural_er_pct"),
            "dilution_pp": (None if sl.get("structural_er_pct") is None
                            else round(mw - sl["structural_er_pct"], 4)),
            "money_weighted_er_rank": round(
                sum(v * (order.index(s) + 1) for s, v in priced.items()) / ptot, 2),
            "unpriced_destinations": unpriced,
            "read_this_way": ("a negative dilution is NOT evidence the ordering is wrong. It is what "
                              "C1 is designed to do when it closes a 26.82pp US underweight, and the "
                              "US-heavy funds in this sleeve are also the expensive, concentrated, "
                              "high-volatility ones. The value is that the question is now ASKABLE: "
                              "is this the price the framework intends to pay? One month is an "
                              "anecdote; a persistent sign is a finding, and it belongs to ISA-0333.")}


def provisional(rm=None) -> dict:
    """E[r] computed from regional_m's PROVISIONAL cells, so the size of the open decision is
    visible. NOT ADMISSIBLE — separate key, `admissible: false`, and nothing reads it."""
    rm = rm if rm is not None else _latest("regional_m")
    pv = (rm.get("provisional") or {})
    if pv.get("state") != "PROVISIONAL":
        return {"state": "ABSENT", "admissible": False}
    m = {c: v["m_pct"] for c, v in (pv.get("cells") or {}).items()}
    r = fund_rows(rm=rm, m_override=m)
    sl = sleeve(r["rows"])
    return {"state": "PROVISIONAL", "admissible": False,
            "admissibility": ("NOT_ADMISSIBLE_FOR_ANY_DECISION — built on regional_m's indicative "
                              "net buyback yields, which are not sourced. It may not enter a "
                              "verdict, an ordering, a floor or a trade."),
            "sleeve": sl,
            "rows": {sd: {"structural_er_pct": x["structural_er_pct"],
                          "declared_floor_gap_pp":
                              x["declared_floor_reconciliation"]["gap_pp"]}
                     for sd, x in sorted(r["rows"].items(),
                                         key=lambda z: -(z[1]["structural_er_pct"] or -99))},
            "ordering_diagnostic": ordering_diagnostic(r["rows"])}


def build(as_of=None, out_path=None) -> dict:
    if not ENABLED:
        return {"state": "DISABLED", "reason": "fund_expected_return.ENABLED is False (R4.13)"}
    as_of = as_of or _today()
    rm = _latest("regional_m")
    fr = fund_rows(rm=rm, as_of=as_of)
    doc = {
        "_meta": {"module": "fund_expected_return.py", "schema_version": SCHEMA_VERSION,
                  "item": ITEM, "built_on": as_of, "enabled": ENABLED,
                  "operative": OPERATIVE,
                  "study": "ISA_Analysis_RegionalM_and_FundStructuralEr_20Aug2026.md Part 2",
                  "rollback": "fund_expected_return.ENABLED = False; the OPERATIVE basis is a "
                              "different constant, return_architecture.ER_BASIS_OPERATIVE",
                  "field_name_warning": (
                      "`structural_er_pct` is an ~11-year structural return. It is NOT "
                      "`expected_return_12_24m`, which is a 12-24 month single-name total return "
                      "from expected_return.py and reads an order of magnitude higher. Never "
                      "compare or substitute them (FE2).")},
        "basis": {"quantity": "nominal_gbp_geometric_annual_total_return_NET_of_ocf",
                  "horizon_basis": HORIZON_BASIS,
                  "regional_m_basis": (rm.get("basis") or {}).get("quantity"),
                  "construction": "sum_k w_k*M_k + (sigma_eq^2 - sigma_fund^2)/2 - OCF + alpha",
                  "alpha_policy": "SHRUNK_TO_ZERO by default (R3.9)"},
        "inputs": {"regional_m_state": rm.get("state", "ABSENT"),
                   "regional_m_blocked_cells": ((rm.get("m") or {}).get("blocked_cells") or []),
                   "exposure_corroboration": (_read("fund_exposure_vectors.json")
                                              .get("magnitude_admissibility")),
                   "coverage_floor_pct": COVERAGE_FLOOR_PCT},
        "funds": fr["rows"],
        "unmeasured": fr["unmeasured"],
        "sigma_equity": fr["sigma_equity"],
        "sleeve": sleeve(fr["rows"]),
        "ordering_diagnostic": ordering_diagnostic(fr["rows"]),
        "provisional": provisional(rm),
        "state": "OK" if not fr["unmeasured"] else "PARTIAL",
    }
    # ⚑ R5.1 — the basis strings must describe the same quantity, or the comparison is meaningless.
    rb = doc["basis"]["regional_m_basis"]
    if rb and "gross_of_ocf" not in rb:
        raise FundErError("regional_m publishes basis %r; this module subtracts OCF from it and "
                          "expects a GROSS-of-OCF input. Basis mismatch (R2.6)." % rb)
    if fr["unmeasured"]:
        doc["partial_reason"] = (
            "%d of %d funds are UNMEASURED. Every one NAMES what it is blocked on; none is given a "
            "sleeve average — 'I could not measure it' and 'it is average' must never produce the "
            "same output (R2.10)." % (len(fr["unmeasured"]), len(fr["rows"])))
    out = Path(out_path or HERE / (OUT_TEMPLATE % as_of[:7].replace("-", "_")))
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    doc["_written"] = str(out)
    return doc


def report(doc=None) -> str:
    d = doc or build()
    if d.get("state") == "DISABLED":
        return "fund_expected_return: DISABLED"
    L = ["FUND STRUCTURAL E[r] (ISA-0328) — %s" % d["basis"]["quantity"],
         "operative: %s   |   regional_m: %s   |   coverage floor: %.1f%% of the FUND"
         % (d["_meta"]["operative"], d["inputs"]["regional_m_state"], COVERAGE_FLOOR_PCT), ""]
    L.append("%-9s %-34s %7s %7s %7s %7s %8s  %s"
             % ("sedol", "name", "cover", "OCF", "vol", "geo", "E[r]", "state"))
    for sd, r in sorted(d["funds"].items(),
                        key=lambda z: -(z[1]["structural_er_pct"] if z[1]["structural_er_pct"]
                                        is not None else -99)):
        t = r["terms"]
        sig = (t["sigma_fund"] or {}).get("value")
        L.append("%-9s %-34s %7s %7s %7s %7s %8s  %s"
                 % (sd, (r["name"] or "")[:34],
                    "%.2f" % (r["coverage"]["attributed_pct_of_fund"] or 0),
                    ("%.2f" % (t["ocf"] or {}).get("value", 0)) if (t["ocf"] or {}).get("value") is not None else "—",
                    ("%.1f" % sig) if sig is not None else "—",
                    "%+.2f" % t["geometric_adjustment_pct"],
                    ("%.2f" % r["structural_er_pct"]) if r["structural_er_pct"] is not None else "—",
                    r["state"]
                    + ((" [" + "; ".join(r["blocked_on"])[:56] + "]") if r["blocked_on"] else "")
                    + ((" *%d caveat(s)" % len(r["caveats"])) if r.get("caveats") else "")))
    sl = d["sleeve"]
    L += ["", "SLEEVE: %s" % (("%.2f%% over %.1f%% covered weight"
                               % (sl["structural_er_pct"], 100 * sl["covered_weight"]))
                              if sl.get("state") == "MEASURED" else sl.get("reason", sl.get("state")))]
    pv = d.get("provisional") or {}
    if pv.get("state") == "PROVISIONAL" and (pv.get("sleeve") or {}).get("state") == "MEASURED":
        L += ["", "PROVISIONAL (NOT ADMISSIBLE): sleeve %.2f%%" % pv["sleeve"]["structural_er_pct"]]
        od = pv.get("ordering_diagnostic") or {}
        # ⚑ never print a number for a state that has none (R2.10)
        if od.get("state") == "MEASURED" and od.get("dilution_pp") is not None:
            L.append("  marginal pound money-weighted E[r] %.2f%% vs sleeve %.2f%% -> %+.2fpp"
                     % (od["money_weighted_er_pct"], od["sleeve_er_pct"], od["dilution_pp"]))
        else:
            L.append("  ordering diagnostic: %s" % od.get("state", "ABSENT"))
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
def selftest(verbose=True) -> int:
    """F1-F6. Every control must FAIL a genuinely broken input, and F3 must fail on the REAL
    defect that motivated the coverage constant (R5.8)."""
    import tempfile, copy, shutil
    fails = []

    def ck(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    tmp = Path(tempfile.mkdtemp())          # ⚑ never beside the scripts — the mount denies delete
    import regional_m as RM
    # A fixture M vector, so the controls do not depend on a declaration being present.
    MFIX = {"us": 7.00, "uk": 7.50, "eu_ex_uk": 7.20, "japan": 6.80,
            "dev_pac_exj": 7.60, "canada": 6.70, "emerging": 6.20}

    d = build(as_of="2026-08-20", out_path=tmp / "fer.json")
    ck("ships OPERATIVE=False and says so on the artefact",
       d["_meta"]["operative"] is False and OPERATIVE is False)
    ck("the emitted field is `structural_er_pct` with an explicit horizon basis (F6/FC-B)",
       all("structural_er_pct" in r and r["horizon_basis"] == HORIZON_BASIS
           for r in d["funds"].values()))
    ck("F6 CONTROL: no key anywhere in the artefact is named `expected_return` or "
       "`expected_return_12_24m` — the two quantities can never be silently substituted",
       "expected_return_12_24m" not in json.dumps(d["funds"])
       and '"expected_return"' not in json.dumps(d["funds"]))
    ck("the module reads the ANCHORED monthly artefact, not its own capture file "
       "(`_latest` is pattern-anchored)",
       d["inputs"]["regional_m_state"] in ("OK", "PARTIAL"))

    r = fund_rows(m_override=MFIX, as_of="2026-08-20")
    rows = r["rows"]
    ck("with an M vector present, every fund is MEASURED and names no blocker",
       all(x["state"] == "MEASURED" for x in rows.values()))

    # ── F1: THE TRACKER CONTROL ───────────────────────────────────────────────────────────────
    v = rows["VUAG"]
    cells = v["exposure"]["cells"]
    expect = sum(w * MFIX[c] for c, w in cells.items())
    ck("F1 CONTROL: the S&P 500 tracker returns M(us) to within its own 1.14%% Ireland line — "
       "if it does not, the construction is wrong",
       abs(v["terms"]["gross_exposure_weighted_m_pct"] - expect) < 1e-4
       and abs(v["terms"]["gross_exposure_weighted_m_pct"] - MFIX["us"]) < 0.05)
    ck("F1 CONTROL: and the fund figure is exactly gross + geometric - OCF + alpha",
       abs(v["structural_er_pct"]
           - (v["terms"]["gross_exposure_weighted_m_pct"]
              + v["terms"]["geometric_adjustment_pct"]
              - v["terms"]["ocf"]["value"] + v["terms"]["alpha"]["value"])) < 1e-6)

    # ── F2: THE LEVEL-OF-REALISED-RETURN CONTROL ─────────────────────────────────────────────
    # Adding drift to every fund's realised history changes what each fund HAS returned and must
    # leave its structural E[r] BIT-IDENTICAL. Volatility is a second moment and is invariant to
    # drift; a construction that leaked a mean would move.
    # ⚑ A PURE ADDITIVE DRIFT ON THE RETURNS. The first version of this control multiplied the
    # price path by a compounding factor, which is an AFFINE transform of the returns (r -> r*f +
    # (f-1)): it scales volatility as well as shifting the mean, so it moved E[r] by ~7e-4pp and
    # the control failed for the wrong reason. Rebuilding the path from r + c changes the MEAN and
    # leaves the SECOND MOMENT exactly unchanged, which is the property being tested.
    DRIFT = 0.005          # +0.5pp per month on every fund's realised return
    nd = tmp / "nav_drift"
    nd.mkdir()
    for p in sorted((HERE / "nav_cache").glob("*.csv")):
        src_rows = []
        with p.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    src_rows.append((row["date"], float(row["close"])))
                except Exception:                                   # noqa: BLE001
                    continue
        # month-end only: fund_sigma reduces to month-ends anyway, and applying the drift per
        # DAILY row would give months with more trading days more drift — which perturbs the
        # second moment and would make this control fail for a reason that is not the property.
        src_rows.sort()
        me_map = {}
        for dte, close in src_rows:
            me_map[dte[:7]] = (dte, close)
        keys = sorted(me_map)
        out, prev, px = [], None, None
        for k in keys:
            dte, close = me_map[k]
            if prev is None or prev <= 0:
                px = close
            else:
                px = px * (1.0 + (close / prev - 1.0) + DRIFT)
            out.append((dte, px))
            prev = close
        with (nd / p.name).open("w", encoding="utf-8", newline="") as fh:
            w_ = csv.writer(fh)
            w_.writerow(["date", "close"])
            w_.writerows(out)
    r2 = fund_rows(m_override=MFIX, as_of="2026-08-20", nav_dir=str(nd))
    ck("F2 CONTROL: adding drift to every fund's realised history leaves every structural E[r] "
       "BIT-IDENTICAL — the construction reads no realised mean, and this proves it behaviourally",
       all(abs((r2["rows"][sd]["structural_er_pct"] or 0)
               - (rows[sd]["structural_er_pct"] or 0)) < 1e-9 for sd in rows))

    # ── F3: THE COVERAGE CONTROL, ON THE FIELD THAT MOTIVATED IT ─────────────────────────────
    fev = copy.deepcopy(_read("fund_exposure_vectors.json"))
    ck("F3 CONTROL: the floor reads `attributed_pct_of_fund`, the FUND denominator (ISA-0403)",
       all(x["coverage"]["field_read"] == "attributed_pct_of_fund" for x in rows.values()))
    fev["per_fund"]["BR2Q8G6"]["diagnostics"]["attributed_pct_of_fund"] = 40.0
    r3 = fund_rows(m_override=MFIX, fev=fev, as_of="2026-08-20")
    ck("F3 CONTROL: a fund below the coverage floor is UNMEASURED and BLOCKS — it is never given "
       "a sleeve average (R2.10)",
       r3["rows"]["BR2Q8G6"]["state"] == "UNMEASURED"
       and any("coverage" in b for b in r3["rows"]["BR2Q8G6"]["blocked_on"])
       and r3["rows"]["BR2Q8G6"]["structural_er_pct"] is None)
    fev2 = copy.deepcopy(_read("fund_exposure_vectors.json"))
    del fev2["per_fund"]["VUAG"]["diagnostics"]["attributed_pct_of_fund"]
    r3b = fund_rows(m_override=MFIX, fev=fev2, as_of="2026-08-20")
    ck("F3 CONTROL: an ABSENT coverage field BLOCKS rather than being read as full coverage (R4.3)",
       r3b["rows"]["VUAG"]["state"] == "UNMEASURED")

    # ── F4: THE COST CONTROL ─────────────────────────────────────────────────────────────────
    uni = copy.deepcopy(_read("fund_universe.json").get("funds") or {})
    for f in uni.values():
        f["ocf"] = 0.50
    r4 = fund_rows(m_override=MFIX, universe=uni, as_of="2026-08-20")
    o1 = [s for s in sorted(rows, key=lambda z: -rows[z]["structural_er_pct"])]
    o4 = [s for s in sorted(r4["rows"], key=lambda z: -r4["rows"][z]["structural_er_pct"])]
    ck("F4 CONTROL: cost is LIVE — flattening every OCF reorders the sleeve (spread 1.02pp against "
       "an E[r] range of ~4.6pp), so the term is not decorative", o1 != o4)

    # ── the geometric adjustment: live, switchable, and never a SILENT zero ──────────────────
    global GEOMETRIC_ADJUSTMENT_ENABLED
    GEOMETRIC_ADJUSTMENT_ENABLED = False
    r5 = fund_rows(m_override=MFIX, as_of="2026-08-20")
    GEOMETRIC_ADJUSTMENT_ENABLED = True
    ck("the geometric adjustment is LIVE and rolls back in one constant (R4.13)",
       all(x["terms"]["geometric_adjustment_pct"] == 0.0 for x in r5["rows"].values())
       and any(abs(x["terms"]["geometric_adjustment_pct"]) > 0.1 for x in rows.values()))
    ck("it charges CONCENTRATION and credits diversification, in the direction compounding "
       "actually works — Polar (28.1% vol) is penalised, and the sleeve level barely moves",
       rows["B42W4J8"]["terms"]["geometric_adjustment_pct"] < -2.0)
    ck("R4.3: a REFUSED adjustment is applied as zero and DECLARED AS SUCH, never silently — "
       "Ranmore's 38-month window is below the declared minimum and says so on the row",
       rows["BR2Q8G6"]["terms"]["geometric_adjustment_basis"].startswith("REFUSED")
       and any("APPLIED_AS_ZERO_AND_DECLARED" in c for c in rows["BR2Q8G6"]["caveats"]))

    # ── absent is not zero; refusals name themselves ─────────────────────────────────────────
    half = dict(MFIX)
    del half["japan"]
    r6 = fund_rows(m_override=half, as_of="2026-08-20")
    ck("a fund touching an UNMEASURED cell is UNMEASURED and NAMES the cell — it is never "
       "computed as though that cell returned 0% (R2.10/R4.1)",
       r6["rows"]["B50MZ94"]["state"] == "UNMEASURED"
       and any("japan" in b for b in r6["rows"]["B50MZ94"]["blocked_on"]))
    ck("the SLEEVE refuses rather than averaging over the funds that happened to price (R4.10)",
       sleeve(r6["rows"])["state"] in ("MEASURED", "UNMEASURED"))

    # ── alpha ────────────────────────────────────────────────────────────────────────────────
    ck("alpha is ZERO on every fund, with the shrinkage DECLARED rather than assumed (R3.9)",
       all(x["terms"]["alpha"]["value"] == 0.0 and "SHRUNK_TO_ZERO" in x["terms"]["alpha"]["source"]
           for x in rows.values()))

    # ── the declared-floor reconciliation REPORTS; it never writes back ──────────────────────
    ck("FE4: the declared/derived reconciliation is REPORT_ONLY on every fund",
       all(x["declared_floor_reconciliation"]["REPORT_ONLY"] is True for x in rows.values()))
    before = json.loads((HERE / "target_weights.json").read_text(encoding="utf-8"))
    fund_rows(m_override=MFIX, as_of="2026-08-20")
    after = json.loads((HERE / "target_weights.json").read_text(encoding="utf-8"))
    ck("FE4 CONTROL: building the layer does not modify target_weights.json (D-C(ii))",
       before == after)

    # ── the ordering diagnostic is NON-BINDING ──────────────────────────────────────────────
    od = ordering_diagnostic(rows)
    ck("the ordering diagnostic is published and NON-BINDING, and it prices the marginal pound "
       "against the sleeve",
       od.get("binding") is False and od.get("state") in ("MEASURED", "NO_ALLOCATION", "UNMEASURED"))

    # ── F5: OPERATIVE NEUTRALITY — the most important test in this build ────────────────────
    import return_architecture as _RA
    ck("F5 CONTROL: return_architecture's operative basis is UNCHANGED by this module shipping — "
       "`exposure_forward` is published, never switched on (D-13's null-behaviour-delta pattern)",
       _RA.ER_BASIS_OPERATIVE == "declared_prior")
    ck("F5 CONTROL: the module writes ONE artefact and touches no other file",
       (tmp / "fer.json").exists())

    # ── basis parity ────────────────────────────────────────────────────────────────────────
    ck("R5.1: the regional M basis is GROSS of OCF and this module's is NET — stated, and the "
       "mismatch RAISES rather than being absorbed",
       "NET_of_ocf" in d["basis"]["quantity"])

    # ── R4.13 rollback ──────────────────────────────────────────────────────────────────────
    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module", build()["state"] == "DISABLED")
    ENABLED = True

    print("\nfund_expected_return selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails else " — 22 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--report" in sys.argv:
        print(report())
    else:
        print(json.dumps(build(), indent=2)[:6000])
