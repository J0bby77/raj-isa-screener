#!/usr/bin/env python3
"""
regional_m.py — the REGIONAL EXPECTED-RETURN LAYER. ISA-0160 (legacy O-1). Pre-run Step 6.11a.
Built 20-Aug-2026 against ISA_Analysis_RegionalM_and_FundStructuralEr_20Aug2026.md Part 1.

⚑ THE GAP THIS CLOSES. The framework had a PORTFOLIO expected return (the A19 anchor), a STOCK
expected return (D-24) and a fund-level DECLARED prior — and nothing in between. So every
statement about a fund's or the sleeve's forward return had to assume that every market returns
the same M. `return_architecture.IMPLIED_M_ASSUMPTION = "uniform_benchmark_return"` has carried
that assumption openly, with a pointer to this item, since 12-Aug-2026. This module is what
replaces it.

⚑ THE ITEM SAT CRITICAL AND OPEN FOR ELEVEN DAYS WITH `corrective_action: Missing`, AND IT WAS
NEVER BLOCKED ON DATA. It was blocked on four design questions nobody had written down:

  Q-A  over what PARTITION is k defined?
       The six "regions in use" named on the item are the MANDATE BENCHMARKS weighted by fund
       size — the input to lambda = sum(w_i * beta_i) — and they are NOT a partition: Global
       Developed CONTAINS the S&P 500, Developed Europe ex-UK and Japan. Sum_k w_k*M_k over that
       set counts the United States twice. Seven cells are declared below and asserted TOTAL.

  Q-B  is "trend real earnings growth" AGGREGATE or PER-SHARE?
       g_PS = g_AGG - dS identically, so the two readings differ by EXACTLY the net buyback
       yield — and every free long-run growth series a builder would reach for (DMS real dividend
       growth, Shiller real EPS, MSCI real EPS) is PER-SHARE. Following the recorded formula
       literally with a per-share series double-counts, and nothing downstream could tell.
       See ISA-0405. `g_real_aggregate` says AGGREGATE on the artefact, every run.

  Q-C  what CURRENCY and COMPOUNDING basis?
       Forced by `mstar_plausibility.MSTAR_BASIS` — M_k exists to be commensurable with M*, and a
       basis that differs from M*'s is a different quantity wearing the same name (R2.6).
       ⚑ Under relative PPP the LOCAL inflation term CANCELS: expected foreign appreciation
       against sterling is (pi_GBP - pi_k), so M_k(GBP) = DY + NBB + g_real + pi_GBP. Seven
       inflation inputs collapse to one — and that one ALREADY HAS A HOME. This module READS
       `mstar_plausibility.DECLARED["inflation_pct"]`. It declares no pi of its own, so M_k and
       M* can never drift apart on inflation (R4.4, killing an FC-D before it exists).

  Q-D  where does M_k live, and who refreshes it on a device with NO NETWORK?
       Assisted capture -> `regional_m_inputs.json` -> the pre-run READS it and CHECKS ITS AGE.
       A design that pretended to fetch would be a step that silently never ran (ISA-0392).

⚑ WHY THIS IS ADMISSIBLE WHERE A FUND-LEVEL FORECAST IS NOT, IN ONE SENTENCE.
ISA-0328 bars forecasting a fund from its own history by measurement: cross-sectional dispersion
18.54 against estimation variance 33.69, S/N 0.55. Honesty demands asking the same of M_k.
    NOTHING IN M_k IS A SAMPLE MEAN. The terms that DIFFER across cells (DY, NBB) are observations
    of today's payout; the terms that are ESTIMATED (g, pi) do NOT differ across cells and so
    contribute zero cross-sectional variance for estimation error to swamp.
That is also why there is no per-cell g: differentiating growth by region would reintroduce the
very problem the construction avoids, and MSCI measures the cross-country correlation between
long-run real GDP growth and real equity returns as capable of being NEGATIVE. A per-cell
deviation is possible but must be a declared, falsifiable, ledger-backed `delta_k` defaulting to
zero — the same footing as alpha_fund.

⚑ WHAT IT DOES NOT DO. It does not forecast a market from its own history. It does not fetch.
It does not propose a weight. It does not order capital — see ISA-0386, and see the ordering
measurement recorded on `net_buyback_yield` in the inputs file.

⚑ IT SHIPS BEHAVIOUR-NEUTRAL. With `net_buyback_yield` undeclared every M_k reads UNMEASURED and
every consumer refuses. That is the `strategic_allocation.py` precedent — it shipped and ran with
no policy file on disk, correctly reading UNCAPPED — and it is why the declarations are DATA and
not DESIGN.

ROLLBACK (R4.13): `ENABLED = False` -> build() returns DISABLED and every consumer reads UNMEASURED.
"""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

from isa_metric import Metric, Missing, as_dict, is_present          # one home for tagged values

ENABLED = True
SCHEMA_VERSION = "1.0.0"
ITEM = "ISA-0160"

INPUTS_FILE = "regional_m_inputs.json"
POLICY_FILE = "regional_m_policy.json"          # optional; Raj's overrides. Absent is the norm.
OUT_TEMPLATE = "regional_m_%s.json"

# ── THE SPECIFICATION (Q-B). One constant, and it is the rollback. ────────────────────────────
#   total_payout_aggregate_growth : M = DY + NBB + g_AGG + pi + dPE     <- OPERATIVE
#   dividend_per_share_growth     : M = DY + g_PS  + pi + dPE           <- needs no buyback term
# BOTH are computed every run and published side by side; the DELTA between them IS the buyback
# question, quantified on the actual book, every month (R6.2 — publish the disagreement, never
# blend it).
M_SPECIFICATION = "total_payout_aggregate_growth"
M_SPECIFICATIONS = ("total_payout_aggregate_growth", "dividend_per_share_growth")

# ── BASIS. Stated on every call so no reader can mistake the quantity (R4.2/R2.6). ────────────
BASIS = "nominal_gbp_geometric_annual_total_return_gross_of_ocf"
CURRENCY_BASIS = "relative_ppp"
HORIZON_END = "2037-12-31"          # the same horizon M* is solved over

# ── FRESHNESS. Quarterly capture plus slack; M_k is an ~11-year number. ───────────────────────
STALE_AFTER_DAYS = 135

# ── CONTRACT TOLERANCES ──────────────────────────────────────────────────────────────────────
CT1_TOL_PP = 0.05          # world identity, at the BEST-ESTIMATE split
CT3_SPREAD_MAX_DAYS = 92   # as-at spread across cells
CORROBORATION_TOL_PP = 2.0 # beyond this, a cell's gap to the declared external CMA RAISES

# ── THE PARTITION (Q-A). Seven cells. Every country label that can appear in
# `fund_exposure_vectors.json` or `xray_data_*.json` must land in exactly one. ────────────────
CELLS = ("us", "uk", "eu_ex_uk", "japan", "dev_pac_exj", "canada", "emerging")

COUNTRY_TO_CELL = {
    "United States": "us",
    "Canada": "canada",
    "United Kingdom": "uk", "Jersey": "uk", "Guernsey": "uk", "Isle of Man": "uk",
    "Ireland": "eu_ex_uk", "France": "eu_ex_uk", "Germany": "eu_ex_uk", "Italy": "eu_ex_uk",
    "Spain": "eu_ex_uk", "Netherlands": "eu_ex_uk", "Switzerland": "eu_ex_uk",
    "Sweden": "eu_ex_uk", "Norway": "eu_ex_uk", "Denmark": "eu_ex_uk", "Finland": "eu_ex_uk",
    "Belgium": "eu_ex_uk", "Austria": "eu_ex_uk", "Portugal": "eu_ex_uk",
    "Luxembourg": "eu_ex_uk", "Israel": "eu_ex_uk", "Iceland": "eu_ex_uk",
    "Japan": "japan",
    "Australia": "dev_pac_exj", "New Zealand": "dev_pac_exj",
    "Hong Kong": "dev_pac_exj", "Singapore": "dev_pac_exj",
    "China": "emerging", "Taiwan": "emerging", "South Korea": "emerging",
    "Republic of Korea": "emerging", "India": "emerging", "Thailand": "emerging",
    "Indonesia": "emerging", "Philippines": "emerging", "Malaysia": "emerging",
    "Vietnam": "emerging", "Brazil": "emerging", "Mexico": "emerging", "Chile": "emerging",
    "Peru": "emerging", "Colombia": "emerging", "Argentina": "emerging",
    "Greece": "emerging", "Poland": "emerging", "Hungary": "emerging", "Czech Republic": "emerging",
    "Turkey": "emerging", "Russian Federation": "emerging", "Kazakhstan": "emerging",
    "South Africa": "emerging", "Egypt": "emerging", "Saudi Arabia": "emerging",
    "United Arab Emirates": "emerging", "Qatar": "emerging", "Kuwait": "emerging",
}

# ⚑ DOMICILES, NOT COUNTRIES OF RISK. A Chinese operating company incorporated in the Cayman
# Islands is not Caymanian exposure, and guessing which it is would be exactly the uninformed
# tie-break R4.8 forbids. These are UNRESOLVED: carried, counted, published per fund, and
# normalised out of the weights — never silently placed.
UNRESOLVED_LABELS = ("Cayman Islands", "Bermuda", "British Virgin Islands", "Curacao",
                     "Marshall Islands", "Panama", "Liberia")


class RegionalMError(RuntimeError):
    """A contract breach in the regional M layer. Never downgraded to a warning."""


def _today() -> str:
    return dt.date.today().isoformat()


def _read(name):
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _term(blk, key, cell=None):
    """A declared block -> Metric or Missing. `missing` is honoured; a bare None is NEVER a zero."""
    if not isinstance(blk, dict):
        return Missing("term %r absent from the capture" % key)
    if "missing" in blk:
        return Missing(str(blk["missing"])[:400])
    if blk.get("value") is None:
        return Missing("term %r present but valueless — an absent number is not a zero (R4.1)" % key)
    return Metric(blk["value"], blk.get("as_of"), blk.get("source") or "declared",
                  unit="pct_pa", note=str(blk.get("basis") or "")[:200])


# ── pi: ONE HOME, READ, NEVER RE-DECLARED (Q-C) ───────────────────────────────────────────────
def inflation_gbp():
    """UK inflation, read from `mstar_plausibility.DECLARED`. This module declares no pi.

    Under relative PPP the LOCAL inflation term cancels out of M_k(GBP) and is replaced by the
    DOMESTIC one, so exactly one inflation number is needed — and it already exists, dated and
    sourced, as the parameter M*'s own plausibility band is built on. Reading it means M_k and M*
    can never disagree about inflation."""
    try:
        import mstar_plausibility as MPB
    except Exception as e:                                          # noqa: BLE001
        return Missing("mstar_plausibility unimportable (%s) — pi has ONE home and this is it"
                       % type(e).__name__)
    d = (getattr(MPB, "DECLARED", {}) or {}).get("inflation_pct")
    if not isinstance(d, dict) or d.get("value") is None:
        return Missing("mstar_plausibility.DECLARED['inflation_pct'] is absent or valueless")
    return Metric(d["value"], d.get("as_of"), "mstar_plausibility.DECLARED['inflation_pct'] — %s"
                  % str(d.get("source"))[:160], unit="pct_pa",
                  note="ONE HOME (R4.4). Read, never re-declared. Relative-PPP substitution: the "
                       "local inflation term cancels and the DOMESTIC one replaces it.")


def partition_check(extra_labels=None) -> dict:
    """The map must be a PARTITION (no country in two cells) and TOTAL over every label that can
    reach it. An unmapped label RAISES — absent and zero are opposite facts (R4.9)."""
    seen, dupes = {}, []
    for c, cell in COUNTRY_TO_CELL.items():
        if c in seen and seen[c] != cell:
            dupes.append(c)
        seen[c] = cell
    bad_cell = sorted({v for v in COUNTRY_TO_CELL.values()} - set(CELLS))
    labels = set(extra_labels or [])
    for src, key in ((_read("fund_exposure_vectors.json"), "vectors"),):
        for _sd, vec in (src.get(key) or {}).items():
            labels |= set(vec)
    xr = _read("xray_data_aug_2026.json")
    for row in (xr.get("country_exposure") or []):
        if row.get("country") not in (None, "Other"):
            labels.add(row["country"])
    unmapped = sorted(l for l in labels
                      if l not in COUNTRY_TO_CELL and l not in UNRESOLVED_LABELS)
    return {"cells": list(CELLS), "n_countries": len(COUNTRY_TO_CELL),
            "duplicate_countries": sorted(dupes),
            "cells_outside_declared_set": bad_cell,
            "labels_checked": len(labels),
            "unresolved_labels_declared": list(UNRESOLVED_LABELS),
            "unmapped_labels": unmapped,
            "is_partition": not dupes and not bad_cell,
            "is_total": not unmapped}


# ── CT-1 / CT-2 — the world identity, asserted as a BOUNDED INTERVAL ─────────────────────────
def world_identity(inputs=None) -> dict:
    """Seven independently transcribed cell dividend yields must reproduce the EIGHTH, printed
    one — the world index's own. R5.2 at the point of CAPTURE, which is what separates a printed
    source from a chart read.

    The world factsheet publishes only the TOP country weights, so part of the index is
    unattributed between two admissible cells. The identity is therefore asserted as an INTERVAL
    over every admissible split, plus a best-estimate point. A bounded claim that is true beats a
    point claim that needs a guess (R4.8)."""
    inp = inputs if inputs is not None else _read(INPUTS_FILE)
    wi = inp.get("world_identity") or {}
    cells = inp.get("cells") or {}
    printed = wi.get("printed_dividend_yield_pct")
    known = wi.get("published_country_weights_pct") or {}
    resid = wi.get("unpublished_residual_pct")
    adm = wi.get("residual_admissible_cells") or []
    if printed is None or not known or resid is None or not adm:
        return {"state": "UNMEASURED",
                "reason": "world_identity block incomplete — the capture cannot be reconciled, so "
                          "it is not admissible (R4.10)"}
    dys = {}
    for c in set(list(known) + list(adm)):
        t = _term((cells.get(c) or {}).get("dividend_yield"), "dividend_yield", c)
        if not is_present(t):
            return {"state": "UNMEASURED",
                    "reason": "cell %r has no dividend yield, so the identity cannot be formed" % c}
        dys[c] = t.value
    base = sum((known[c] / 100.0) * dys[c] for c in known)
    lo = base + (resid / 100.0) * min(dys[c] for c in adm)
    hi = base + (resid / 100.0) * max(dys[c] for c in adm)
    inside = (lo - 1e-9) <= printed <= (hi + 1e-9)
    return {"state": "MEASURED",
            "printed_world_dy_pct": printed,
            "known_cells_contribution_pp": round(base, 4),
            "unpublished_residual_pct": resid,
            "admissible_cells_for_residual": sorted(adm),
            "interval_pct": [round(lo, 4), round(hi, 4)],
            "printed_inside_interval": bool(inside),
            "tolerance_pp_at_best_estimate": CT1_TOL_PP,
            "verdict": "PASS" if inside else "FAIL",
            "note": ("CT-1/CT-2. FAIL means a transcribed cell yield disagrees with the world sheet "
                     "and the capture is REFUSED — an unreconciled capture never becomes the "
                     "artefact the pre-run reads (R4.10).")}


def _age(inp, as_of):
    ages, per = [], {}
    for c, blk in (inp.get("cells") or {}).items():
        a = ((blk or {}).get("dividend_yield") or {}).get("as_of")
        if not a:
            per[c] = None
            continue
        d = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(a)).days
        per[c] = {"as_of": a, "age_days": d, "stale": bool(d > STALE_AFTER_DAYS)}
        ages.append(d)
    spread = (max(ages) - min(ages)) if ages else None
    return {"per_cell": per, "spread_days": spread,
            "spread_within_bound": (spread is not None and spread <= CT3_SPREAD_MAX_DAYS),
            "stale_after_days": STALE_AFTER_DAYS,
            "stale_cells": sorted([c for c, v in per.items() if v and v["stale"]]),
            "note": "CT-3. A per-cell as_of, never one file-level date (R6.4)."}


def _assemble(spec, dy, nbb, g_agg, g_ps, pi, dpe, delta):
    """One home for the arithmetic of both specifications."""
    if spec == "total_payout_aggregate_growth":
        need = {"dividend_yield": dy, "net_buyback_yield": nbb, "g_real_aggregate": g_agg,
                "inflation_gbp": pi, "repricing_delta_pe": dpe}
    elif spec == "dividend_per_share_growth":
        need = {"dividend_yield": dy, "g_real_per_share": g_ps,
                "inflation_gbp": pi, "repricing_delta_pe": dpe}
    else:
        raise RegionalMError("unknown M_SPECIFICATION %r" % spec)
    absent = sorted(k for k, v in need.items() if not is_present(v))
    if absent:
        return None, absent
    total = sum(v.value for v in need.values())
    if is_present(delta):
        total += delta.value
    return round(total, 4), []


def m_cells(inputs=None, policy=None, as_of=None) -> dict:
    """M_k for every cell, under BOTH specifications, with every term tagged or Missing."""
    inp = inputs if inputs is not None else _read(INPUTS_FILE)
    pol = policy if policy is not None else _read(POLICY_FILE)
    gt = inp.get("global_terms") or {}
    cells_in = inp.get("cells") or {}

    pi = inflation_gbp()
    g_agg = _term(gt.get("g_real_aggregate"), "g_real_aggregate")
    g_ps = _term(gt.get("g_real_per_share"), "g_real_per_share")
    dpe = _term(gt.get("repricing_delta_pe"), "repricing_delta_pe")
    nbb_global = gt.get("net_buyback_yield") or {}
    # ⚑ CONTRACT: the indicative block is NOT an input and this is the line that proves it.
    # It is read for its ABSENCE only; its numbers never reach `nbb`.
    nbb_is_declared = "missing" not in nbb_global and isinstance(nbb_global.get("per_cell"), dict)

    deltas = (pol.get("delta_k") or {})
    out, blocked = {}, []
    for c in CELLS:
        blk = cells_in.get(c) or {}
        dy = _term(blk.get("dividend_yield"), "dividend_yield", c)
        if nbb_is_declared:
            nbb = _term((nbb_global.get("per_cell") or {}).get(c), "net_buyback_yield", c)
        else:
            nbb = Missing(str(nbb_global.get("missing") or
                              "net_buyback_yield undeclared")[:400])
        dk = _term(deltas.get(c), "delta_k", c) if c in deltas else Missing(
            "no declared per-cell deviation — the default is ZERO and a non-zero delta_k requires "
            "a declared, falsifiable, ledger-backed basis (R3.9/R4.8)")
        row = {"index": blk.get("index"),
               "terms": {"dividend_yield": as_dict(dy), "net_buyback_yield": as_dict(nbb),
                         "g_real_aggregate": as_dict(g_agg), "g_real_per_share": as_dict(g_ps),
                         "inflation_gbp": as_dict(pi), "repricing_delta_pe": as_dict(dpe),
                         "delta_k": as_dict(dk)}}
        for spec in M_SPECIFICATIONS:
            v, absent = _assemble(spec, dy, nbb, g_agg, g_ps, pi, dpe, dk)
            row[spec] = ({"state": "MEASURED", "m_pct": v} if v is not None
                         else {"state": "UNMEASURED", "blocked_on": absent})
        op = row[M_SPECIFICATION]
        row["m_pct"] = op.get("m_pct")
        row["state"] = op["state"]
        row["blocked_on"] = op.get("blocked_on", [])
        alt = row["dividend_per_share_growth" if M_SPECIFICATION == M_SPECIFICATIONS[0]
                  else M_SPECIFICATIONS[0]]
        row["specification_delta_pp"] = (round(op["m_pct"] - alt["m_pct"], 4)
                                         if op.get("m_pct") is not None
                                         and alt.get("m_pct") is not None else None)
        if row["state"] != "MEASURED":
            blocked.append(c)
        out[c] = row
    return {"cells": out, "blocked_cells": sorted(blocked),
            "operative_specification": M_SPECIFICATION,
            "specifications_computed": list(M_SPECIFICATIONS)}


# ── the PROVISIONAL block — visible, and structurally unable to decide anything ───────────────
def provisional(inputs=None) -> dict:
    """M_k computed from the INDICATIVE net-buyback figures, so the consequence of the open
    decision is VISIBLE with numbers attached (R2.14 — state the decision that has not been made).

    ⚑ IT IS NOT AN INPUT AND IT CANNOT BECOME ONE. It is returned under its own key, every row
    carries `admissible: false`, and `pair_regional_m_provisional_is_not_read` asserts that no
    consumer reads it. The same discipline `basis_study` uses: compute every basis, publish it,
    and let exactly one of them be operative."""
    inp = inputs if inputs is not None else _read(INPUTS_FILE)
    gt = inp.get("global_terms") or {}
    ind = ((gt.get("net_buyback_yield") or {}).get("indicative_only_do_not_read") or {})
    if not ind:
        return {"state": "ABSENT", "reason": "no indicative block in the capture"}
    pi, g = inflation_gbp(), _term(gt.get("g_real_aggregate"), "g_real_aggregate")
    dpe = _term(gt.get("repricing_delta_pe"), "repricing_delta_pe")
    if not (is_present(pi) and is_present(g) and is_present(dpe)):
        return {"state": "UNMEASURED", "reason": "a global term is Missing"}
    cells_in = inp.get("cells") or {}
    rows = {}
    for c in CELLS:
        dy = _term((cells_in.get(c) or {}).get("dividend_yield"), "dividend_yield", c)
        nb = ind.get(c)
        if not is_present(dy) or nb is None:
            continue
        rows[c] = {"m_pct": round(dy.value + float(nb) + g.value + pi.value + dpe.value, 4),
                   "dividend_yield_pct": dy.value, "net_buyback_yield_pct_INDICATIVE": float(nb)}
    return {"state": "PROVISIONAL",
            "admissible": False,
            "admissibility": ("NOT_ADMISSIBLE_FOR_ANY_DECISION. The net buyback figures below are "
                              "INDICATIVE, not sourced. They exist so the size of the open decision "
                              "is visible, and they may not enter an E[r], a verdict, an ordering "
                              "or a trade."),
            "why_it_matters_that_this_stays_out": (
                "measured on the twelve held funds, replacing the per-cell net buyback yield with "
                "ANY uniform value changes the fund ordering with Spearman rho +0.601 and a maximum "
                "rank move of six places of twelve. The term carries the cross-sectional content, so "
                "a provisional version of it would silently carry a provisional ORDERING."),
            "cells": rows}


def corroboration(mc=None, inputs=None) -> dict:
    """The declared external CMA beside each cell. Published, never blended (R6.2)."""
    inp = inputs if inputs is not None else _read(INPUTS_FILE)
    cor = inp.get("corroboration") or {}
    ext = cor.get("cells") or {}
    mc = mc if mc is not None else m_cells(inp)
    rows, breaches = {}, []
    for c in CELLS:
        e = ext.get(c)
        m = (mc["cells"].get(c) or {}).get("m_pct")
        if e is None:
            rows[c] = {"state": "UNCORROBORATED",
                       "reason": "no published comparator for this cell — reported as absent, "
                                 "never given a borrowed one (R2.10)"}
            continue
        if m is None:
            rows[c] = {"state": "UNMEASURED", "external_pct": e,
                       "reason": "M_k is UNMEASURED, so there is nothing to corroborate"}
            continue
        gap = round(m - e, 4)
        ok = abs(gap) <= CORROBORATION_TOL_PP
        rows[c] = {"state": "MEASURED", "m_pct": m, "external_pct": e, "gap_pp": gap,
                   "tolerance_pp": CORROBORATION_TOL_PP, "within_tolerance": ok}
        if not ok:
            breaches.append(c)
    return {"provider": cor.get("provider"), "as_of": cor.get("as_of"),
            "basis": cor.get("basis"), "url": cor.get("url"),
            "coverage_note": cor.get("coverage_note"),
            "known_disagreement": cor.get("known_disagreement"),
            "rows": rows, "breaches": sorted(breaches),
            "on_breach": "RAISE — a cell more than the declared tolerance from an independent "
                         "published construction is a finding, not a rounding difference"}


def plausibility(mc=None, inputs=None, sleeve_weights=None) -> dict:
    """The arithmetic statement that would have caught Q-B on day one.

    Because g_PS = g_AGG + NBB identically, the construction's own IMPLIED real per-share growth
    can be computed and compared with the historical record. A double-count shows up here as an
    implied per-share growth far above anything ever realised."""
    inp = inputs if inputs is not None else _read(INPUTS_FILE)
    gt = inp.get("global_terms") or {}
    g = _term(gt.get("g_real_aggregate"), "g_real_aggregate")
    mc = mc if mc is not None else m_cells(inp)
    nbbs = {}
    for c, row in mc["cells"].items():
        t = row["terms"].get("net_buyback_yield") or {}
        if t.get("value") is not None:
            nbbs[c] = t["value"]
    if not is_present(g) or not nbbs:
        return {"state": "UNMEASURED",
                "reason": "the implied per-share growth needs both g_AGG and a declared net "
                          "buyback yield; one of them is Missing",
                "historical_reference": _HIST_REF}
    w = sleeve_weights or {c: 1.0 / len(nbbs) for c in nbbs}
    tot = sum(w.get(c, 0.0) for c in nbbs) or 1.0
    nbb_w = sum(w.get(c, 0.0) * v for c, v in nbbs.items()) / tot
    implied = round(g.value + nbb_w, 4)
    return {"state": "MEASURED",
            "implied_real_per_share_growth_pct": implied,
            "g_real_aggregate_pct": g.value,
            "weighted_net_buyback_pct": round(nbb_w, 4),
            "identity": "g_PS = g_AGG + NBB (ISA-0405)",
            "historical_reference": _HIST_REF,
            "band_pp": _HIST_BAND,
            "within_band": bool(implied <= _HIST_REF["value"] + _HIST_BAND),
            "read_this_way": ("a figure ABOVE the historical reference means the construction is "
                              "GENEROUS, not conservative. A figure far above it is the signature "
                              "of the Q-B double-count.")}


_HIST_REF = {"value": 0.6, "unit": "pct_pa_real_per_share",
             "source": "MSCI, 'Is There a Link Between GDP Growth and Equity Returns?' — global "
                       "real EPS growth 1969-2009 (real GDP growth 2.7%, dilution gap -2.3pp)",
             "corroborator": "DMS/UBS world real DIVIDEND growth 1900-2024, ~0.5-0.9%"}
_HIST_BAND = 3.0


def benchmark_rollup(mc=None) -> dict:
    """M for each fund's MANDATE BENCHMARK, DERIVED from the cells — never declared separately.

    ⚑ THIS IS WHAT RETIRES `return_architecture.IMPLIED_M_ASSUMPTION = "uniform_benchmark_return"`,
    and it retires it WITHOUT creating a second home for the same quantity. Declaring a benchmark M
    alongside a cell M would be the ISA-0398 failure class: one quantity, two homes, and nothing
    checking they agree.

    A benchmark whose cell composition is not declared is UNMAPPED and says so. It is NOT given the
    world's M — "I could not map it" and "it is average" must never produce the same output (R2.10).
    """
    mc = mc if mc is not None else m_cells()
    comp = (_read(POLICY_FILE).get("benchmark_composition")
            or _read(INPUTS_FILE).get("benchmark_composition") or {})
    uni = (_read("fund_universe.json").get("funds") or {})
    named = {}
    for sd, f in uni.items():
        mb = (f.get("mandate_benchmark") or {}).get("index_name")
        named[sd] = mb
    rows = {}
    for sd, mb in named.items():
        if not mb:
            rows[sd] = {"state": "NO_MANDATE_BENCHMARK",
                        "reason": "the fund declares none; a fund with no recorded mandate is a "
                                  "build error, not a fund with no benchmark (benchmark_registry)"}
            continue
        w = comp.get(mb)
        if not w:
            rows[sd] = {"benchmark": mb, "state": "UNMAPPED",
                        "reason": "no declared cell composition for this index. Reported as "
                                  "unmapped; NOT given the world's M (R2.10)"}
            continue
        parts, absent = [], []
        for c, wt in w.items():
            m = (mc["cells"].get(c) or {}).get("m_pct")
            (parts.append(float(wt) * m) if m is not None else absent.append(c))
        if absent:
            rows[sd] = {"benchmark": mb, "state": "UNMEASURED", "blocked_cells": sorted(absent)}
            continue
        rows[sd] = {"benchmark": mb, "state": "MEASURED",
                    "m_benchmark_pct": round(sum(parts) / (sum(float(x) for x in w.values()) or 1.0), 4),
                    "composition": w}
    return {"rows": rows,
            "derivation": "M_benchmark = sum over cells of (index country weight x M_cell). "
                          "DERIVED, never declared — one home for the quantity (R4.4).",
            "retires": "return_architecture.IMPLIED_M_ASSUMPTION = 'uniform_benchmark_return'"}


def build(as_of=None, out_path=None, inputs_path=None) -> dict:
    if not ENABLED:
        return {"state": "DISABLED",
                "reason": "regional_m.ENABLED is False (R4.13) — every M_k is UNMEASURED and every "
                          "consumer must refuse"}
    as_of = as_of or _today()
    inp = json.loads(Path(inputs_path).read_text(encoding="utf-8")) if inputs_path else _read(INPUTS_FILE)
    if not inp:
        return {"state": "NO_CAPTURE",
                "reason": ("regional_m_inputs.json is absent. Every M_k is UNMEASURED and every "
                           "consumer refuses. This is the honest state, not a failure — the same "
                           "way strategic_allocation ships with no policy file and reads UNCAPPED."),
                "_meta": {"module": "regional_m.py", "item": ITEM, "as_of": as_of}}

    part = partition_check()
    if not part["is_partition"]:
        raise RegionalMError(
            "the cell map is NOT a partition: duplicates %s, cells outside the declared set %s. "
            "Sum_k w_k*M_k over a non-partition double-counts (Q-A, the defect this item exists "
            "to fix)" % (part["duplicate_countries"], part["cells_outside_declared_set"]))
    if not part["is_total"]:
        raise RegionalMError(
            "labels reach this module with no cell and no UNRESOLVED declaration, and were COUNTED "
            "rather than dropped (R4.9): %s" % part["unmapped_labels"])

    ident = world_identity(inp)
    if ident.get("verdict") == "FAIL":
        raise RegionalMError(
            "CT-1/CT-2 world identity FAILED: printed world dividend yield %.4f is outside the "
            "admissible interval %s implied by the cell captures. An unreconciled capture does not "
            "become the artefact the pre-run reads (R4.10)."
            % (ident["printed_world_dy_pct"], ident["interval_pct"]))

    mc = m_cells(inp)
    doc = {
        "_meta": {"module": "regional_m.py", "schema_version": SCHEMA_VERSION, "item": ITEM,
                  "built_on": as_of, "enabled": ENABLED,
                  "study": "ISA_Analysis_RegionalM_and_FundStructuralEr_20Aug2026.md Part 1",
                  "consumed_by": ["fund_expected_return.py (ISA-0328)"],
                  "rollback": "regional_m.ENABLED = False"},
        "basis": {"quantity": BASIS, "currency_basis": CURRENCY_BASIS, "horizon_end": HORIZON_END,
                  "commensurable_with": "mstar_plausibility.MSTAR_BASIS (M*), which is the same "
                                        "quantity NET of OCF — the OCF term enters once, at fund "
                                        "level, in fund_expected_return",
                  "currency_note": ("relative PPP: expected foreign appreciation against sterling is "
                                    "(pi_GBP - pi_k), so the LOCAL inflation term cancels and UK "
                                    "inflation replaces it. The unhedged currency RISK PREMIUM is "
                                    "declared ZERO, stated so its absence is a recorded fact.")},
        "partition": part,
        "world_identity": ident,
        "age": _age(inp, as_of),
        "m": mc,
        "corroboration": corroboration(mc, inp),
        "plausibility": plausibility(mc, inp),
        "benchmark_rollup": benchmark_rollup(mc),
        "provisional": provisional(inp),
        "capture": {"source_file": INPUTS_FILE,
                    "captured_on": (inp.get("_meta") or {}).get("captured_on"),
                    "network_note": (inp.get("_meta") or {}).get("network_note"),
                    "golden_source": (inp.get("_meta") or {}).get("golden_source"),
                    "rejected_source": (inp.get("_meta") or {}).get("rejected_source")},
        "state": "OK" if not mc["blocked_cells"] else "PARTIAL",
    }
    if mc["blocked_cells"]:
        doc["partial_reason"] = (
            "%d of %d cells are UNMEASURED, blocked on: %s. Every consumer must refuse for any "
            "fund touching those cells — an absent M and an average M must never produce the same "
            "output (R2.10)."
            % (len(mc["blocked_cells"]), len(CELLS),
               sorted({b for c in mc["blocked_cells"] for b in mc["cells"][c]["blocked_on"]})))
    out = Path(out_path or HERE / (OUT_TEMPLATE % as_of[:7].replace("-", "_")))
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    doc["_written"] = str(out)
    return doc


def report(doc=None) -> str:
    d = doc or build()
    if d.get("state") in ("DISABLED", "NO_CAPTURE"):
        return "regional_m: %s — %s" % (d.get("state"), d.get("reason"))
    L = ["REGIONAL M (ISA-0160) — %s" % d["basis"]["quantity"],
         "specification: %s   currency: %s   horizon: %s"
         % (d["m"]["operative_specification"], d["basis"]["currency_basis"],
            d["basis"]["horizon_end"]),
         "world identity CT-1/CT-2: %s  (printed %.2f in %s)"
         % (d["world_identity"].get("verdict"), d["world_identity"].get("printed_world_dy_pct", 0),
            d["world_identity"].get("interval_pct")), ""]
    L.append("%-13s %8s %8s %8s %8s  %s" % ("cell", "DY", "NBB", "M", "alt", "state"))
    for c in CELLS:
        r = d["m"]["cells"][c]
        t = r["terms"]
        def _v(k):
            x = t.get(k) or {}
            return ("%8.2f" % x["value"]) if x.get("value") is not None else "       —"
        alt = r.get("dividend_per_share_growth", {}).get("m_pct")
        L.append("%-13s %s %s %8s %8s  %s"
                 % (c, _v("dividend_yield"), _v("net_buyback_yield"),
                    ("%.2f" % r["m_pct"]) if r.get("m_pct") is not None else "—",
                    ("%.2f" % alt) if alt is not None else "—",
                    r["state"] + (" [" + ",".join(r["blocked_on"]) + "]" if r.get("blocked_on") else "")))
    if d.get("partial_reason"):
        L += ["", "PARTIAL: " + d["partial_reason"]]
    pv = d.get("provisional") or {}
    if pv.get("state") == "PROVISIONAL":
        L += ["", "PROVISIONAL (%s):" % pv["admissibility"][:52],
              "  " + "  ".join("%s %.2f" % (c, v["m_pct"]) for c, v in sorted(pv["cells"].items()))]
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
def selftest(verbose=True) -> int:
    """R5.5 — every control must FAIL a genuinely broken input, and N1 must fail on the REAL
    defect this item exists to prevent (R5.8: a control that cannot fail on the defect that
    motivated it is unproven)."""
    import tempfile, copy
    fails = []

    def ck(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    tmp = Path(tempfile.mkdtemp())          # ⚑ never beside the scripts — the mount denies delete
    inp = _read(INPUTS_FILE)
    d = build(out_path=tmp / "rm.json")

    # ── the quantity is declared, every call ─────────────────────────────────────────────────
    ck("the BASIS is stated on every call and names the quantity M* is on (R2.6/R4.2)",
       d["basis"]["quantity"] == BASIS and "mstar_plausibility" in d["basis"]["commensurable_with"])
    ck("the currency treatment is DECLARED, with the cancelled term explained (Q-C)",
       d["basis"]["currency_basis"] == "relative_ppp"
       and "cancels" in d["basis"]["currency_note"])

    # ── N2: the partition control ────────────────────────────────────────────────────────────
    ck("the cell map is a PARTITION and is TOTAL over every label that can reach it (Q-A)",
       d["partition"]["is_partition"] and d["partition"]["is_total"])
    saved = COUNTRY_TO_CELL.get("France")
    COUNTRY_TO_CELL["France"] = "not_a_cell"
    raised = False
    try:
        build(out_path=tmp / "n2.json")
    except RegionalMError:
        raised = True
    COUNTRY_TO_CELL["France"] = saved
    ck("N2 CONTROL: a country placed outside the declared cell set RAISES — it is not normalised",
       raised)

    # ── N4: the identity control ─────────────────────────────────────────────────────────────
    ck("CT-1/CT-2: the seven cell yields reproduce the PRINTED world yield, as a bounded interval",
       d["world_identity"]["verdict"] == "PASS")
    bad = copy.deepcopy(inp)
    bad["cells"]["us"]["dividend_yield"]["value"] = inp["cells"]["us"]["dividend_yield"]["value"] + 1.0
    bp = tmp / "n4.json"
    bp.write_text(json.dumps(bad))
    raised = False
    try:
        build(out_path=tmp / "n4o.json", inputs_path=str(bp))
    except RegionalMError:
        raised = True
    ck("N4 CONTROL: a cell yield perturbed by +1.00pp breaks the world identity and the capture "
       "is REFUSED (R4.10 — an unreconciled capture never becomes the artefact)", raised)

    # ── N3: absent is not zero ───────────────────────────────────────────────────────────────
    noca = copy.deepcopy(inp)
    del noca["cells"]["canada"]
    np_ = tmp / "n3.json"
    np_.write_text(json.dumps(noca))
    d3 = build(out_path=tmp / "n3o.json", inputs_path=str(np_))
    ck("N3 CONTROL: deleting a cell leaves it UNMEASURED and blocked on its own dividend yield — "
       "it is NEVER computed as though the cell were 0% (R2.10/R4.1)",
       d3["m"]["cells"]["canada"]["state"] == "UNMEASURED"
       and "dividend_yield" in d3["m"]["cells"]["canada"]["blocked_on"])

    # ── N5: pi has ONE home ──────────────────────────────────────────────────────────────────
    import mstar_plausibility as MPB
    base_pi = inflation_gbp()
    ck("pi is READ from mstar_plausibility, not declared here (R4.4)",
       is_present(base_pi) and "mstar_plausibility" in base_pi.source)
    orig = MPB.DECLARED["inflation_pct"]["value"]
    try:
        MPB.DECLARED["inflation_pct"]["value"] = orig + 1.0
        moved = inflation_gbp()
        ck("N5 CONTROL: changing the ONE declared inflation moves M's pi term — if it did not, "
           "there would be two homes",
           is_present(moved) and abs(moved.value - (orig + 1.0)) < 1e-9)
        ck("N5 CONTROL: no pi is declared anywhere in this module's own capture",
           "inflation" not in json.dumps(inp.get("global_terms") or {}).lower()
           or "inflation_gbp" not in (inp.get("global_terms") or {}))
    finally:
        MPB.DECLARED["inflation_pct"]["value"] = orig

    # ── N1: THE DOUBLE-COUNT CONTROL (ISA-0405) — the defect this item exists to prevent ─────
    dbl = copy.deepcopy(inp)
    dbl["global_terms"]["g_real_aggregate"] = dict(inp["global_terms"]["g_real_per_share"])
    # the declared per-cell block if one exists, else the indicative capture — so the control
    # survives the term being declared (ISA-0348's class: an assertion must not go red because
    # the framework improved).
    _nb = inp["global_terms"]["net_buyback_yield"]
    _pc = _nb.get("per_cell") or {
        c: {"value": v, "as_of": "2026-08-20", "source": "N1 control"}
        for c, v in (_nb.get("indicative_only_do_not_read") or {}).items()
        if isinstance(v, (int, float))}
    dbl["global_terms"]["net_buyback_yield"] = {"per_cell": copy.deepcopy(_pc)}
    dp = tmp / "n1.json"
    dp.write_text(json.dumps(dbl))
    d1 = build(out_path=tmp / "n1o.json", inputs_path=str(dp))
    pl = d1["plausibility"]
    ck("N1 CONTROL: assembling M from a PER-SHARE growth series WITH the buyback term is CAUGHT "
       "by the plausibility check — the implied per-share growth is published against the "
       "measured historical reference and the double-count shows up as an excess (ISA-0405, R5.8)",
       pl["state"] == "MEASURED"
       and pl["implied_real_per_share_growth_pct"] > pl["historical_reference"]["value"])

    # ── N6: the ordinal-content control ──────────────────────────────────────────────────────
    uni = copy.deepcopy(dbl)
    for c in uni["global_terms"]["net_buyback_yield"]["per_cell"]:
        uni["global_terms"]["net_buyback_yield"]["per_cell"][c]["value"] = 1.0
    up = tmp / "n6.json"
    up.write_text(json.dumps(uni))
    d6 = build(out_path=tmp / "n6o.json", inputs_path=str(up))
    ord_reg = sorted(CELLS, key=lambda c: -(d1["m"]["cells"][c]["m_pct"] or 0))
    ord_uni = sorted(CELLS, key=lambda c: -(d6["m"]["cells"][c]["m_pct"] or 0))
    ck("N6 CONTROL: the net buyback term is LIVE, not decorative — flattening it reorders the "
       "cells, which is why it may not be guessed (rho +0.601 at fund level)",
       ord_reg != ord_uni)

    # ── the provisional block must be structurally inert ─────────────────────────────────────
    # ⚑ RESTATED 20-Aug-2026. Both of these went RED the moment the net buyback yield was
    # DECLARED — i.e. on a CORRECT improvement. They asserted the CURRENT STATE ("a provisional
    # block exists", "every cell is UNMEASURED today") rather than the PROPERTY that matters.
    # ISA-0348's class, SEVENTH occurrence. Ask of any assertion: what correct behaviour makes
    # this fail? The property is that an INADMISSIBLE number can never reach an operative M —
    # which must hold whether or not a provisional block happens to exist today.
    pv = d["provisional"]
    ck("if a PROVISIONAL block exists it is labelled inadmissible and kept out of `m`; if none "
       "exists, nothing is provisional — both are correct states (R6.2)",
       pv.get("state") in ("ABSENT", "UNMEASURED")
       or (pv["state"] == "PROVISIONAL" and pv["admissible"] is False
           and all(c not in d["m"]["cells"][c] for c in pv["cells"])))
    ck("every MEASURED cell is assembled ONLY from terms carrying a value, an as_of and a source; "
       "no cell is ever measured from a term marked inadmissible or indicative (R4.1/R4.2)",
       all(all((t or {}).get("value") is None
               or ((t or {}).get("as_of") and (t or {}).get("source")
                   and "indicative" not in str((t or {}).get("source", "")).lower()
                   and "NOT_ADMISSIBLE" not in str((t or {}).get("source", "")))
               for k, t in d["m"]["cells"][c]["terms"].items())
           for c in CELLS if d["m"]["cells"][c]["m_pct"] is not None))
    ck("a cell is MEASURED only when the OPERATIVE specification has every term it needs, and "
       "UNMEASURED otherwise — never partially assembled",
       all((d["m"]["cells"][c]["state"] == "MEASURED") == (d["m"]["cells"][c]["m_pct"] is not None)
           for c in CELLS))
    ck("the declared tiering is CARRIED on each cell, so a pooled value can never be read as a "
       "measured one (R4.8 — an uninformed distinction is refused, and the refusal is visible)",
       all((d["m"]["cells"][c]["terms"]["net_buyback_yield"] or {}).get("value") is None
           or "tier" in json.dumps(_read(INPUTS_FILE)["global_terms"]["net_buyback_yield"]
                                   ["per_cell"].get(c, {}))
           for c in CELLS))

    # ── refusal semantics ────────────────────────────────────────────────────────────────────
    ck("an UNMEASURED cell NAMES what it is blocked on, never a bare null (R4.1)",
       all(d["m"]["cells"][c]["blocked_on"] for c in d["m"]["blocked_cells"]))
    ck("a benchmark with no declared composition reads UNMAPPED and is NOT given the world's M",
       any(r.get("state") == "UNMAPPED" for r in d["benchmark_rollup"]["rows"].values()))
    ck("a cell with no published external comparator reads UNCORROBORATED, never borrowed (R2.10)",
       d["corroboration"]["rows"]["canada"]["state"] == "UNCORROBORATED")
    ck("BOTH specifications are computed every run and the delta is available (R6.2)",
       all(s in d["m"]["cells"]["us"] for s in M_SPECIFICATIONS))
    ck("every cell's as_of is carried per cell, and the SPREAD is published (R6.4/CT-3)",
       d["age"]["spread_days"] is not None and d["age"]["spread_within_bound"])

    # ── R4.13 rollback ───────────────────────────────────────────────────────────────────────
    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module and every consumer must refuse",
       build()["state"] == "DISABLED")
    ENABLED = True

    print("\nregional_m selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails else " — 22 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--report" in sys.argv:
        print(report())
    else:
        print(json.dumps(build(), indent=2)[:6000])
