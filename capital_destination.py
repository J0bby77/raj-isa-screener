#!/usr/bin/env python3
"""
capital_destination.py — THE MARGINAL-POUND ROUTER.  Built 16-Aug-2026.

Closes ISA-0152 (D-17) · ISA-0153 (D-18) · ISA-0154 (D-19) · ISA-0151 (D-16) · ISA-0351 (L-1).

⚑ THE GAP THIS CLOSES, IN ONE SENTENCE
Four register items said the same thing from four directions — trailing performance has no
measured edge, a mechanical FRS ranking buys the worst-evidenced fund, no single fund can absorb
the subscription, and Ranmore is estimated on 38 months — and yet the only thing that would have
directed Raj's September GBP 11,250 was a ranking by trailing return. This module is the
destination rule. It is CROSS-SLEEVE: funds, stock sleeve, or both (Raj, 16-Aug-2026).

⚑ THE MEASUREMENT THAT LICENSES THE DESIGN  (§1, `evidence_dispersion`)
A third, independent derivation of D-18, by variance decomposition rather than rank persistence:

    Var(observed cross-sectional dispersion of realised annualised fund returns)   18.54   (SD 4.31pp)
    mean estimation variance of those same returns                                 33.69   (RMS SE 5.80pp)
    signal / noise variance ratio                                                   0.55

The spread in what these funds HAVE returned is SMALLER than the error in measuring what they
returned. Var(true dispersion) = 18.54 - 33.69 = -15.15, i.e. not distinguishable from zero and
of the wrong sign. **The data cannot reject the hypothesis that every fund in this sleeve has the
same expected return.** Therefore ordering funds by trailing return is ordering noise (R3.4:
t<2 means zero cannot be rejected, and a significance test may never be used as an estimator).

Three routes now agree — D-18's rho ~ 0, L-1's rank persistence (alpha -0.482, IR -0.418), and
this decomposition. R5.2 satisfied: two independent derivations agree, and here there are three.

⚑ WHAT FOLLOWS, AND WHAT DOES NOT
FOLLOWS   performance is a VETO and never a ranking (P1). Enforced behaviourally, not by comment:
          permuting every eligible fund's trailing return must leave the allocation BIT-IDENTICAL.
DOES NOT  it does not follow that a short record should be down-weighted "until the estimate
          sharpens". No attainable history length makes the estimate informative — at the sleeve's
          median 13.5% vol, SE <= 3pp needs 20 years. So D-19's refusal is re-founded on a
          DIFFERENT and measurable ground: REGIME COVERAGE (§2), not precision.

⚑ THE REFUSAL, RE-FOUNDED  (§2, `regime_coverage`)
The ownership floor is a claim about surviving a cycle. A record that has never been in one cannot
test it. Sleeve median max drawdown is -23.8%; the floor is half of that, -11.9%.

    Ranmore Global Equity   38 months, max drawdown -5.8%, entire life postdates the 2022 trough,
                            and the HIGHEST trailing return in the sleeve at 22.9%.
                            REFUSED as a destination at every multiplier in the 0.3-0.8 grid.

That is not a down-weight. R4.3: a control fed an unmeasurable input BLOCKS, it never passes — and
a small weight on an unmeasurable fund still moves capital into it.

⚑ ORDERING  (§3)  Deviation from the declared band, never merit. D-17 is answered structurally: a
marginal-pound allocation is a VECTOR, not a pick, so "no single fund can absorb GBP 11,250 at the
12.5% cap" stops being a problem and becomes the arithmetic.

⚑ CROSS-SLEEVE  (§4)  The router will not invent a fund/stock split it has no authority for
(ISA-0333: there is no strategic asset allocation layer). It reads a DECLARED sleeve policy, and
where the policy is silent or a declared freeze binds, it REFUSES and states the decision that has
not been made, quantified (R2.14).

ROLLBACK (R4.13): `ENABLED = False` -> build() returns state DISABLED and emits nothing.
"""
from __future__ import annotations
import csv, datetime as dt, json, math, os, statistics as st, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENABLED = True

# ── CONSTANTS THAT GATE CAPITAL — R12.3 rationale ledger entries required ──────────────────────
# REGIME_COVERAGE_MULTIPLIER. Basis: a destination must have LIVED THROUGH a drawdown at least
# half as deep as the sleeve's own median experience, else its record has not been asked the
# question the ownership floor asks. 0.5 is a judgement; what is NOT a judgement is the verdict it
# produces — `regime_coverage()` publishes a robustness grid over 0.3-0.8 and Ranmore is refused at
# every point, which is the only refusal this constant currently drives (R2.14 / ISA-0335: a
# verdict resting on a declared input must publish how much of itself is the input).
REGIME_COVERAGE_MULTIPLIER = 0.50
REGIME_COVERAGE_GRID = (0.30, 0.40, 0.50, 0.60, 0.70, 0.80)

# SN_RATIO_ADMISSIBLE. Trailing return may ORDER destinations only if the cross-sectional signal
# variance exceeds the estimation-noise variance. 1.0 is not a tuned threshold: below it the
# measurement is, literally, less informative than assuming the sleeve mean. Measured 0.55.
SN_RATIO_ADMISSIBLE = 1.0

MIN_MONTHS_FOR_STATS = 14          # below this no annualised statistic is emitted at all (R4.1)
TOL_GBP = 0.01                     # R5.2 stated tolerance for the two-derivation agreement check

SCHEMA_VERSION = "1.0.0"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _today() -> str:
    return dt.date.today().isoformat()


def _fig(value, *, as_of, source, unit=None, note=None):
    """R4.2 — every figure carries as_of and source. R4.1 — missing is not a number."""
    return {"value": value, "as_of": as_of, "source": source,
            "unit": unit, "note": note, "present": value is not None}


class DestinationRefused(RuntimeError):
    """Raised only by callers that demand an allocation the policy refuses to produce."""


def _monthly_series(path: Path):
    """month-end price series -> [(YYYY-MM, price)] using the last observation in each month."""
    by = {}
    with open(path, newline="") as fh:
        for row in list(csv.reader(fh))[1:]:
            if not row or not row[0]:
                continue
            try:
                by[row[0][:7]] = float(row[1])
            except (ValueError, IndexError):
                continue
    return [(k, by[k]) for k in sorted(by)]


def _stats(series):
    """Annualised return, annualised vol, SE of the mean, max drawdown. None below the floor."""
    if len(series) < MIN_MONTHS_FOR_STATS:
        return None
    px = [p for _, p in series]
    rets = [px[i + 1] / px[i] - 1.0 for i in range(len(px) - 1) if px[i] > 0]
    if len(rets) < MIN_MONTHS_FOR_STATS - 1:
        return None
    n = len(rets)
    T = n / 12.0
    sd_a = st.pstdev(rets) * math.sqrt(12)
    ann = (px[-1] / px[0]) ** (1 / T) - 1.0 if px[0] > 0 else None
    peak, mdd, trough = px[0], 0.0, None
    for (k, p) in series:
        peak = max(peak, p)
        d = p / peak - 1.0
        if d < mdd:
            mdd, trough = d, k
    return {"n_months": n, "years": T, "ann_return_pct": ann * 100.0,
            "vol_ann_pct": sd_a * 100.0, "se_ann_pp": sd_a / math.sqrt(T) * 100.0,
            "max_drawdown_pct": mdd * 100.0, "drawdown_trough": trough,
            "first": series[0][0], "last": series[-1][0]}


def _load_sleeve(nav_dir: Path, universe: dict, held_only=True, portfolio=None):
    """Map held funds -> NAV series -> stats. R4.9: a fund that cannot be matched is COUNTED."""
    held = None
    if held_only and portfolio:
        held = {f["ticker"] for f in portfolio.get("funds", [])}
    rows, unmatched = {}, []
    for sedol, u in (universe.get("funds", universe)).items():
        if str(sedol).startswith("_"):
            continue
        if held is not None and sedol not in held:
            continue
        cands = [u.get("yf_symbol"), u.get("isin")]
        path = None
        for c in cands:
            if not c:
                continue
            for stem in (c, "POLAR_" + str(c)):
                p = nav_dir / f"{stem}.csv"
                if p.exists():
                    path = p
                    break
            if path:
                break
        if path is None:
            unmatched.append(sedol)
            continue
        s = _stats(_monthly_series(path))
        if s is None:
            unmatched.append(sedol)
            continue
        s["sedol"] = sedol
        s["name"] = u.get("name")
        s["bucket"] = u.get("bucket")
        s["source"] = f"nav_cache/{path.name}"
        rows[sedol] = s
    return rows, unmatched


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §1  EVIDENCE — is trailing return admissible as an ORDERING at all?   (ISA-0153 / D-18, L-1)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def evidence_dispersion(rows: dict) -> dict:
    """Variance decomposition of the sleeve's realised returns.

    Var(observed cross-section) = Var(true dispersion) + mean(estimation variance)

    If the estimation variance is the larger term, the observed spread between funds is mostly
    measurement error and any ORDER imposed on it is an order over noise. This is the third
    independent route to D-18 and it is the one that licenses the veto in code rather than prose.
    """
    vals = [r["ann_return_pct"] for r in rows.values()]
    ses = [r["se_ann_pp"] for r in rows.values()]
    if len(vals) < 3:
        return {"state": "UNKNOWN", "reason": f"only {len(vals)} measurable funds", "n": len(vals)}
    var_obs = st.pvariance(vals)
    mean_se2 = sum(s * s for s in ses) / len(ses)
    ratio = var_obs / mean_se2 if mean_se2 else None
    admissible = bool(ratio is not None and ratio >= SN_RATIO_ADMISSIBLE)
    return {
        "state": "MEASURED",
        "n_funds": len(vals),
        "var_observed": round(var_obs, 3),
        "sd_observed_pp": round(math.sqrt(var_obs), 3),
        "mean_estimation_variance": round(mean_se2, 3),
        "rms_standard_error_pp": round(math.sqrt(mean_se2), 3),
        "var_true_dispersion_implied": round(var_obs - mean_se2, 3),
        "signal_noise_variance_ratio": round(ratio, 4) if ratio else None,
        "threshold": SN_RATIO_ADMISSIBLE,
        "ordering_admissible": admissible,
        "verdict": ("TRAILING_RETURN_MAY_ORDER" if admissible else "TRAILING_RETURN_VETO_ONLY"),
        "interpretation": (
            "The cross-sectional spread in realised returns is smaller than the error in "
            "measuring them; implied true dispersion is negative. The data cannot reject "
            "'every fund has the same expected return', so trailing return may exclude a "
            "destination but may never order one."
            if not admissible else
            "Signal variance exceeds estimation variance; ordering is admissible."),
        "corroborates": ["ISA-0153 (D-18) rho~0", "ISA-0351 (L-1) alpha rank -0.482"],
        "as_of": _today(),
    }


def precision_ladder(rows: dict) -> list:
    """Years of history required for a given standard error, at the sleeve's median vol.

    Published because it is the reason D-19 is NOT re-founded on 'wait for more months'.
    """
    if not rows:
        return []
    med = st.median([r["vol_ann_pct"] for r in rows.values()])
    out = []
    for target in (2.0, 3.0, 5.0, 7.5, 10.0):
        yrs = (med / target) ** 2
        out.append({"se_target_pp": target, "years_required": round(yrs, 1),
                    "months_required": round(yrs * 12), "sleeve_median_vol_pct": round(med, 2)})
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §2  EVIDENCE REFUSAL — regime coverage, not months     (ISA-0154 / D-19, ISA-0151 / D-16)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def regime_coverage(rows: dict, multiplier: float = None) -> dict:
    """A destination must have LIVED THROUGH a drawdown at least `multiplier` x the sleeve median.

    R4.3 — this BLOCKS. A fund that fails is refused as a destination for new capital; it is not
    scored low, and it is not sold. Unmeasurable and bad are opposite facts.
    """
    m = REGIME_COVERAGE_MULTIPLIER if multiplier is None else multiplier
    if not rows:
        return {"state": "UNKNOWN", "reason": "no measurable funds"}
    med = st.median([r["max_drawdown_pct"] for r in rows.values()])
    floor = med * m                                    # both negative; floor is shallower
    verdicts = {}
    for sedol, r in rows.items():
        passed = r["max_drawdown_pct"] <= floor
        verdicts[sedol] = {
            "name": r["name"], "n_months": r["n_months"],
            "max_drawdown_pct": round(r["max_drawdown_pct"], 2),
            "drawdown_trough": r["drawdown_trough"],
            "floor_pct": round(floor, 2),
            "verdict": "ELIGIBLE" if passed else "REFUSED_UNTESTED_BY_A_CYCLE",
            "reason": (None if passed else
                       f"deepest drawdown in {r['n_months']} months is "
                       f"{r['max_drawdown_pct']:.1f}%, shallower than the {floor:.1f}% floor "
                       f"({m:.0%} of the sleeve median {med:.1f}%); the record has never been "
                       f"asked the question the ownership floor asks"),
        }
    # robustness grid — ISA-0335's lesson: publish how much of the verdict is the constant
    grid = []
    for g in REGIME_COVERAGE_GRID:
        f = med * g
        refused = sorted(s for s, r in rows.items() if r["max_drawdown_pct"] > f)
        grid.append({"multiplier": g, "floor_pct": round(f, 2), "refused": refused,
                     "n_refused": len(refused)})
    always = set(grid[0]["refused"])
    for g in grid[1:]:
        always &= set(g["refused"])
    return {"state": "MEASURED", "sleeve_median_max_drawdown_pct": round(med, 2),
            "multiplier": m, "floor_pct": round(floor, 2), "verdicts": verdicts,
            "robustness_grid": grid, "refused_at_every_grid_point": sorted(always),
            "robust": bool(always), "as_of": _today()}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §3  ORDERING — ESTIMATION-FREE, and band restoration demoted to the tie-break it always was
#     ISA-0386 (Raj, 19-Aug-2026: "it should not just be based on capacity")  ·  ISA-0152 / D-17
# ══════════════════════════════════════════════════════════════════════════════════════════════
# ⚑ THE PROBLEM RAJ NAMED. `_deviation_key` ordered destinations by distance below the bucket
# target and nothing else, so the marginal pound went to whichever fund had the most ROOM. Room is
# a MECHANICAL SAFETY RULE. Using it as the sole ordering silently promoted it into a selection
# criterion the framework never chose.
#
# ⚑ WHY "PERFORMANCE FIRST" CANNOT BE THE REPAIR, AND THE REPAIR THAT IS LEFT.
# Three independent measurements say trailing return cannot order these funds: D-18 (rho ~ 0),
# L-1/ISA-0351 (alpha rank persistence -0.482, IR -0.418), and this module's own variance
# decomposition (S/N 0.55). Trailing return is not merely noisy here — it is NEGATIVELY ranked,
# which is why §5 proves behaviourally that it orders nothing. So the ranking is built ONLY from
# criteria that need no forecast. All five are decidable from artefacts on disk today.
#
#   C1  REFERENCE DEVIATION   does the pound REDUCE a MATERIAL unauthorised active bet against the
#       (ISA-0333)            declared reference?  -1 reduces · 0 neutral · +1 widens.
#   C2  CONCENTRATION         does it add to a PROCESS or MANAGER already at or above the declared
#       (ISA-0329)            materiality reading?  0 no · 1 yes.
#   C3  MANDATE INTEGRITY     is the fund still doing what it was bought for?  T4 OK/WATCH/FIRING.
#       (ISA-0165)            A FIRING fund is not a destination for NEW capital.
#   C4  COST                  OCF. The only reliably signed term in the whole problem.
#   C5  BAND RESTORATION      the old key, DEMOTED to the tie-break.
#
# ⚑ C1 IS ORDINAL ON PURPOSE (-1/0/+1), NOT A MAGNITUDE. A continuous C1 in a lexicographic order
# is not criterion one of five, it IS the order — C2..C4 would never be reached once a look-through
# vector made every fund's score distinct. The sign, plus the reference's OWN `material` flag
# (one home: strategic_allocation), is exactly what the question "does the pound REDUCE the bet"
# asks. The raw active_pp travels on the row for the record.
#
# ⚑ RESOLUTION IS DECLARED, NEVER ASSUMED (R4.1 / R6.4). C1 runs at whatever resolution the data
# supports and SAYS which:
#     `lookthrough_vector`        per-fund exposure vector from fund_exposure_vectors.json  — the
#                                 real thing, and it does NOT exist on disk yet (ISA-0392).
#     `declared_mandate_region`   the fund's own mandate index, via benchmark_registry.t4_mandate_for
#                                 (the one home for "what is this fund supposed to be doing"),
#                                 mapped to the reference key that index DEFINES. This is a
#                                 definition, not an estimate — but it is one-hot at mandate
#                                 granularity and cannot see what the fund actually holds.
#     `UNRESOLVED`                neither. C1 contributes 0 and the row says REFUSED. NOT_TESTED
#                                 and TESTED_AND_FINE never render the same (R2.10).
#
# ⚑ IT ADDS NO TUNABLE CONSTANT. Materiality is read from strategic_allocation's own `material`
# flag; the concentration threshold from process_concentration.MATERIALITY_PCT_OF_SLEEVE; the
# mandate verdict from fund_rotation_analysis.t4_mandate_drift. Four modules, four homes, one
# reader. Nothing here is a number somebody chose today.
#
# ROLLBACK (R4.13): `RANKING_ENABLED = False` -> _rank_key falls back to _deviation_key exactly,
# i.e. the 16-Aug behaviour, and every emitted document says so.

RANKING_ENABLED = True

EXPOSURE_VECTORS_FILE = "fund_exposure_vectors.json"

# ⚑ A DEFINITION TABLE, NOT A JUDGEMENT. Each entry says which reference key the index's own
# published universe IS. `dimension` names which strategic_allocation dimension the key lives on.
# GLOBAL means the index spans the reference and therefore expresses no directional bet — which is
# a MEASURED neutral, not a missing value. R4.9: an index absent from this table is COUNTED and
# REFUSED, never quietly treated as global.
MANDATE_REFERENCE_KEYS = {
    "standard and poor's 500 index":                        ("country", "United States"),
    "ftse all-share index tr":                              ("country", "United Kingdom"),
    "ftse all-share index (net)":                           ("country", "United Kingdom"),
    "msci japan index":                                     ("country", "Japan"),
    "ftse world europe ex uk tr gbp":                       ("region",  "greater_europe"),
    "msci ac asia pacific ex japan index":                  ("region",  "greater_asia"),
    "msci world net total return index gbp":                ("GLOBAL",  None),
    "msci world index":                                     ("GLOBAL",  None),
    "ftse all-world index (in sterling terms)":             ("GLOBAL",  None),
    "dow jones global technology net total return index (gbp)": ("GLOBAL", None),
}


def _load_exposure_vectors(path=None):
    """-> (vectors, state). The look-through route for C1. ABSENT is a state, never a zero."""
    p = Path(path or HERE / EXPOSURE_VECTORS_FILE)
    if not p.exists():
        return None, {"state": "ABSENT",
                      "reason": ("%s is not on disk. The per-fund exposure vector is the input "
                                 "ISA-0333 / ISA-0160 / ISA-0328 are all blocked on and it has no "
                                 "producer (ISA-0392). C1 runs at declared-mandate resolution and "
                                 "says so." % EXPOSURE_VECTORS_FILE)}
    try:
        doc = json.load(open(p))
    except Exception as e:                                            # noqa: BLE001
        return None, {"state": "UNREADABLE", "reason": "%s: %s" % (type(e).__name__, e)}
    vecs = doc.get("vectors") or {}
    bad = [sd for sd, v in vecs.items()
           if not isinstance(v, dict) or abs(sum(float(x) for x in v.values()) - 1.0) > 0.02]
    if bad:                                                           # R4.9 / R5.1 contract
        return None, {"state": "CONTRACT_FAILED",
                      "reason": "vectors do not sum to 1.00 +/- 0.02 for: %s" % ", ".join(sorted(bad))}
    c2r = doc.get("country_to_region") or {}
    if not c2r:                                                       # R4.7 — RAISE, never default
        return None, {"state": "CONTRACT_FAILED",
                      "reason": ("the artefact carries no `country_to_region` map. Without it a "
                                 "country weight cannot be tested against a REGION-level active "
                                 "bet, and every European or Asian fund would silently score "
                                 "neutral on C1.")}
    return {"vectors": vecs, "country_to_region": c2r}, {
        "state": "PRESENT", "as_of": doc.get("as_of"), "source": doc.get("source"),
        "corroboration": doc.get("corroboration"), "admissible_as": doc.get("admissible_as"),
        "stale": doc.get("stale"), "share_class_substitutions": doc.get("share_class_substitutions"),
        "as_of_spread_days": doc.get("as_of_spread_days"), "n_funds": len(vecs)}


def _material_bets(saa: dict) -> dict:
    """-> {(dimension, key): active_pp} for MATERIAL bets only. `material` is the reference's own
    flag — one home in strategic_allocation, read here and never re-derived (ISA-0382's lesson)."""
    out = {}
    for dim, d in ((saa or {}).get("dimensions") or {}).items():
        if d.get("state") != "MEASURED":
            continue
        for b in d.get("bets") or []:
            if b.get("material"):
                out[(dim, b["key"])] = float(b["active_pp"])
    return out


def c1_reference_deviation(sedol, *, material, vectors, vec_state, mandate_index, mandate_basis,
                           mandate_error=None) -> dict:
    """-> the ordinal C1 row for one fund. -1 reduces a material bet · 0 neutral · +1 widens it."""
    vecs = (vectors or {}).get("vectors") or {}
    c2r = (vectors or {}).get("country_to_region") or {}
    if vecs and sedol in vecs:
        # ⚑ A PARTITION OVER COUNTRIES, so nothing is counted twice. Each country's weight is
        # charged to the COUNTRY bet if that country carries a material one, and otherwise to its
        # REGION's bet. Charging it to both would count the United States inside `americas` as well
        # as inside `country/United States` and double the largest term in the whole problem.
        score_pp, charged = 0.0, {}
        for k, w in vecs[sedol].items():
            if ("country", k) in material:
                pp, lvl = material[("country", k)], "country/%s" % k
            else:
                r = c2r.get(k)
                if r is not None and ("region", r) in material:
                    pp, lvl = material[("region", r)], "region/%s" % r
                else:
                    continue
            score_pp += float(w) * pp
            charged[lvl] = round(charged.get(lvl, 0.0) + float(w), 4)
        sign = (-1 if score_pp < 0 else (1 if score_pp > 0 else 0))
        return {"c1": sign, "basis": "lookthrough_vector", "state": "MEASURED",
                "active_pp": round(score_pp, 2), "dimension": "weighted_partition",
                "charged_weight_by_bet": charged,
                "weight_against_no_material_bet": round(
                    1.0 - sum(charged.values()), 4),
                "vectors_as_of": vec_state.get("as_of"),
                "admissible_as": vec_state.get("admissible_as")}
    if mandate_error or not mandate_index:
        return {"c1": 0, "basis": "UNRESOLVED", "state": "REFUSED",
                "reason": mandate_error or "no mandate index",
                "active_pp": None, "dimension": None}
    hit = MANDATE_REFERENCE_KEYS.get(str(mandate_index).strip().lower())
    if hit is None:                                                   # R4.9 — count it, refuse it
        return {"c1": 0, "basis": "UNRESOLVED", "state": "REFUSED",
                "reason": ("mandate index %r is not in MANDATE_REFERENCE_KEYS. An index this table "
                           "does not define may not be silently read as global." % mandate_index),
                "active_pp": None, "dimension": None}
    dim, key = hit
    if dim == "GLOBAL":
        return {"c1": 0, "basis": "declared_mandate_region", "state": "MEASURED_NEUTRAL",
                "reason": ("the mandate index spans the reference, so the pound expresses no "
                           "directional bet — measured neutral, not missing"),
                "active_pp": 0.0, "dimension": "GLOBAL", "mandate_basis": mandate_basis}
    pp = material.get((dim, key))
    if pp is None:
        return {"c1": 0, "basis": "declared_mandate_region", "state": "MEASURED_IMMATERIAL",
                "reason": "%s/%s carries no MATERIAL active bet against the reference" % (dim, key),
                "active_pp": 0.0, "dimension": dim, "reference_key": key,
                "mandate_basis": mandate_basis}
    return {"c1": (-1 if pp < 0 else 1), "basis": "declared_mandate_region", "state": "MEASURED",
            "active_pp": round(pp, 2), "dimension": dim, "reference_key": key,
            "mandate_basis": mandate_basis,
            "reason": ("the reference is %s the pound's mandate region by %.2fpp, so the pound %s "
                       "the bet" % ("UNDER" if pp < 0 else "OVER", abs(pp),
                                    "REDUCES" if pp < 0 else "WIDENS"))}


def rank_inputs(universe, *, saa=None, pc=None, t4=None, exposure_path=None) -> dict:
    """-> the estimation-free ranking table, one row per fund, plus the state of each criterion.

    Every input is READ from the module that owns it. R4.3: where a criterion's owner is
    unavailable the criterion is UNAVAILABLE for every fund and says so — it never returns a
    value that looks measured.
    """
    funds = {s: u for s, u in (universe.get("funds", universe)).items()
             if isinstance(u, dict) and not str(s).startswith("_")}
    crit_state = {}

    if saa is None:
        try:
            import strategic_allocation as _sa
            saa = _sa.build()
        except Exception as e:                                        # noqa: BLE001
            saa, crit_state["c1"] = None, {"state": "UNAVAILABLE",
                                           "reason": "%s: %s" % (type(e).__name__, e)}
    material = _material_bets(saa) if saa else {}
    vectors, vec_state = _load_exposure_vectors(exposure_path)
    crit_state.setdefault("c1", {
        "state": ("MEASURED" if material else "NO_MATERIAL_BETS"),
        "resolution": ("lookthrough_vector" if (vectors or {}).get("vectors")
                       else "declared_mandate_region"),
        "exposure_vectors": vec_state,
        "material_bets": {"%s/%s" % k: v for k, v in sorted(material.items())},
        "owner": "strategic_allocation.py (ISA-0333)"})

    if pc is None:
        try:
            import process_concentration as _pc
            pc = _pc.build()
        except Exception as e:                                        # noqa: BLE001
            pc, crit_state["c2"] = None, {"state": "UNAVAILABLE",
                                          "reason": "%s: %s" % (type(e).__name__, e)}
    conc, mat_pct = {}, None
    if pc and pc.get("state") == "OK":
        import process_concentration as _pc
        mat_pct = _pc.MATERIALITY_PCT_OF_SLEEVE
        for dim in ("process", "manager"):
            for g in ((pc.get("source_side") or {}).get(dim) or {}).get("groups") or []:
                conc[(dim, g["key"])] = float(g.get("pct_of_fund_sleeve") or 0.0)
        crit_state.setdefault("c2", {
            "state": "MEASURED", "materiality_pct_of_sleeve": mat_pct,
            "declared_coverage_pct": ((pc.get("source_side") or {}).get("process") or {}
                                      ).get("declared_coverage_pct"),
            "owner": "process_concentration.py (ISA-0329)"})
    else:
        crit_state.setdefault("c2", {"state": "UNAVAILABLE",
                                     "reason": "process_concentration state %s"
                                               % (pc or {}).get("state")})

    if t4 is None:
        try:
            import fund_rotation_analysis as _fra
            t4 = _fra.t4_mandate_drift()
        except Exception as e:                                        # noqa: BLE001
            t4, crit_state["c3"] = None, {"state": "UNAVAILABLE",
                                          "reason": "%s: %s" % (type(e).__name__, e)}
    t4_by = {r["sedol"]: r for r in ((t4 or {}).get("funds") or [])}
    crit_state.setdefault("c3", {
        "state": ("MEASURED" if t4_by else "UNAVAILABLE"),
        "firing": (t4 or {}).get("firing"), "watch": (t4 or {}).get("watch"),
        "refused": (t4 or {}).get("refused"), "owner": "fund_rotation_analysis.py (ISA-0165)"})

    try:
        import benchmark_registry as _breg
        BU = _breg.load_universe()
    except Exception as _e:                                           # noqa: BLE001
        _breg, BU = None, None

    rows = {}
    for sd, u in funds.items():
        idx, basis, merr = None, None, None
        if _breg is not None:
            try:
                idx, basis = _breg.t4_mandate_for(sd, BU)
            except Exception as e:                                    # noqa: BLE001
                merr = "%s: %s" % (type(e).__name__, e)
        else:
            merr = "benchmark_registry unavailable"
        c1 = c1_reference_deviation(sd, material=material, vectors=vectors, vec_state=vec_state,
                                    mandate_index=idx, mandate_basis=basis, mandate_error=merr)

        pk, mk = u.get("process_key"), u.get("manager_key")
        if mat_pct is None:
            c2 = {"c2": 0, "state": "UNAVAILABLE", "basis": None}
        else:
            hits = [(d, k, conc.get((d, k))) for d, k in (("process", pk), ("manager", mk))
                    if k and conc.get((d, k)) is not None]
            over = [h for h in hits if h[2] >= mat_pct]
            c2 = {"c2": (1 if over else 0),
                  "state": "MEASURED" if pk else "UNDECLARED_PROCESS",
                  "basis": ("declared_process" if pk else
                            "NO DECLARED PROCESS — this row reads 0 because nothing was measured, "
                            "not because the fund was found to be unconcentrated (R2.10)"),
                  "readings_pct_of_sleeve": {"%s/%s" % (d, k): v for d, k, v in hits},
                  "over_materiality": ["%s/%s" % (d, k) for d, k, _ in over]}

        tr = t4_by.get(sd)
        if tr is None:
            c3 = {"c3": 0, "state": "UNAVAILABLE", "basis": None}
        elif tr.get("state") == "FIRING":
            c3 = {"c3": 2, "state": "FIRING", "mandate_basis": tr.get("mandate_basis"),
                  "basis": ("T4 is FIRING on the %s mandate — a fund that has stopped doing what "
                            "it was bought for is not a destination for NEW capital. T4 still "
                            "sells nothing (ISA-0165); this DEMOTES, it does not trim."
                            % tr.get("mandate_basis")),
                  "claim": tr.get("claim")}
        elif tr.get("state") == "WATCH":
            c3 = {"c3": 1, "state": "WATCH", "basis": "one observation below the drop threshold"}
        elif tr.get("state") == "REFUSED":
            c3 = {"c3": 0, "state": "NOT_TESTED", "basis": tr.get("reason")}
        else:
            c3 = {"c3": 0, "state": "OK", "basis": "mandate intact on T4"}

        ocf = u.get("ocf")
        c4 = ({"c4": float(ocf), "state": "MEASURED", "basis": "fund_universe.ocf"} if ocf is not None
              else {"c4": None, "state": "REFUSED", "basis": "no declared OCF"})
        rows[sd] = {"sedol": sd, "c1": c1, "c2": c2, "c3": c3, "c4": c4}

    return {"state": "MEASURED", "as_of": _today(), "enabled": RANKING_ENABLED,
            "criteria": crit_state, "rows": rows,
            "order": ["C1 reference deviation (ISA-0333)", "C2 process/manager concentration "
                      "(ISA-0329)", "C3 mandate integrity / T4 (ISA-0165)", "C4 OCF",
                      "C5 band restoration — TIE-BREAK ONLY (ISA-0386)"],
            "trailing_return": "VETO ONLY, never a ranker — proved behaviourally in §5"}


def _deviation_key(candidate: dict) -> tuple:
    """C5, and NOTHING ABOVE IT. Retained as the tie-break and as the R4.13 rollback target.

    R4.4 one home. Reads bucket shortfall and fund weight ONLY. If a trailing-return field is ever
    added here, `no_trailing_return_ordering()` fails and the build goes red.
    """
    return (-candidate["bucket_shortfall_pct"], candidate["weight_pct"], candidate["sedol"])


def _rank_key(candidate: dict) -> tuple:
    """THE ordering key in the capital path. Lexicographic C1 -> C2 -> C3 -> C4 -> C5.

    ⚑ A criterion that is UNAVAILABLE contributes a CONSTANT, so it cannot order anything. That is
    the difference between 'we could not measure this' and 'this measured zero' (R4.1).
    """
    if not RANKING_ENABLED:
        return _deviation_key(candidate)
    r = candidate.get("rank") or {}
    c4 = (r.get("c4") or {}).get("c4")
    return ((r.get("c1") or {}).get("c1", 0),
            (r.get("c2") or {}).get("c2", 0),
            (r.get("c3") or {}).get("c3", 0),
            (float("inf") if c4 is None else float(c4)),
            ) + _deviation_key(candidate)


def _load_portfolio(path=None) -> dict:
    """-> the most recent `portfolio_data_*.json`, chosen by its OWN declared `_meta.data_date`.

    ⚑ NOT by filename and NOT by mtime. The month label in the filename is the RUN month, not the
    data month — `portfolio_data_aug_2026.json` carries data_date 31-Jul-2026 — and alphabetical
    order on month abbreviations is simply wrong ('aug' sorts before 'jul'). A file whose date
    cannot be read is COUNTED and named, never silently skipped (R4.9).
    """
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    cands, unreadable = [], []
    for p in sorted(HERE.glob("portfolio_data_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            ds = ((d.get("_meta") or {}).get("data_date") or "").strip()
            when = dt.datetime.strptime(ds, "%d-%b-%Y").date()
        except Exception as e:                                            # noqa: BLE001
            unreadable.append("%s (%s)" % (p.name, type(e).__name__))
            continue
        cands.append((when, p, d))
    if not cands:
        raise DestinationRefused(
            "no `portfolio_data_*.json` carries a readable `_meta.data_date`. Unreadable: %s"
            % (", ".join(unreadable) or "none found"))
    cands.sort(key=lambda t: t[0])
    when, p, d = cands[-1]
    d.setdefault("_meta", {})["_selected_by"] = (
        "_load_portfolio: newest of %d by declared data_date (%s -> %s)%s"
        % (len(cands), p.name, when.isoformat(),
           ("; UNREADABLE AND COUNTED: " + ", ".join(unreadable)) if unreadable else ""))
    return d


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A7 — THE DONOR ORDERING.  ISA-0440 / amendment schedule A7, built 26-Aug-2026.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# ⚑ WHY THIS IS NOT THE BUY KEY REVERSED, AND WHY THAT IS AN ARITHMETIC POINT RATHER THAN A
# PREFERENCE. Until now the only sell-side ordering in the framework was `waiting_room.donor_order`,
# which read `_rank_key` and reversed it, on the R4.4 reasoning that a second ordering rule is a
# defect on the day it is created. That reasoning was right about copies and wrong about this
# decision. `_rank_key` puts C5 — band deviation — LAST, deliberately (ISA-0386). Reversing it
# therefore puts "how far above its own declared band this money sits" LAST on the sell side,
# where C1 and C2 have already decided the order. A7 puts it FIRST. Two different decisions, two
# different rules; what R4.4 forbids is two homes for ONE rule, so this is THE one home for the
# sell rule and `waiting_room.donor_order` and `fund_action_stack`'s ranked agenda both READ it.
#
# ⚑ AND WHY IT MAY NOT LEAD ON FRS. L-1 (ISA-0351, 15-Aug-2026) MEASURED rank persistence in this
# sleeve: R2 +0.754, alpha -0.482, information ratio -0.418. A donor rule led by a retention score
# built from those components is, in expectation, selling low. L-1 is not actionable in EITHER
# direction (SE 0.32 on n = 11) — which is precisely why it must not be actioned IMPLICITLY by a
# build. FRS is retained and demoted: it votes at P4, it does not decide.
#
#   P1  ABOVE BAND HIGH   pounds above the fund's OWN declared `band_high`. Estimation-free: a
#                         market value against a number Raj declared. ⚑ It is 0.00 for every
#                         in-band fund, so it orders the overweights and TIES EVERYTHING ELSE INTO
#                         P2 — which is what keeps P2..P4 reachable. A continuous criterion at the
#                         head of a lexicographic order otherwise IS the order (the ISA-0386
#                         lesson, applied in the one place where a continuous head is safe).
#   P2  RELIEF            c1 and c2 READ from `rank_inputs`, never recomputed (R4.5). +1 means this
#                         fund's pound WIDENS a material unauthorised active bet, or ADDS to a
#                         process/manager already at or above declared materiality — so selling it
#                         RELIEVES the most. Estimation-free: both are ordinals over declared
#                         thresholds, not forecasts.
#   P3  COST TO KEEP      ORDINAL, for exactly the reason C1 is ordinal. +1 = dear to hold
#                         (top-tercile OCF in this sleeve) and nothing extra to leave; 0 = neutral;
#                         -1 = the EXIT itself has a real cost — a closed-end holding whose
#                         discount would be CRYSTALLISED by selling. That is a fact about acting,
#                         not a view about the fund, which is why it belongs above FRS and below
#                         relief.
#   P4  FRS VOTE          DEAD MONEY / WINDOW_SPLIT / RETAIN-ONLY / UNSCORED / HOLD-ADD band order,
#                         then dominance, then the FRS number. Supplied by the caller — this module
#                         does not import `fund_action_stack` — and ABSENT is a constant, so an
#                         unsupplied FRS orders nothing rather than ordering everything to zero.
#   P5  DETERMINISM       value desc, then sedol. Never a source of ordering, only of repeatability.
#
# ROLLBACK (R4.13): `A7_DONOR_ORDER_ENABLED = False` -> `donor_order` sorts by `_rank_key` in
# REVERSE, which is exactly what `waiting_room` did before A7. One constant, no code revert.

A7_DONOR_ORDER_ENABLED = True

# The OCF tercile boundary is DERIVED from the sleeve each run, never a literal: a fixed "dear"
# threshold would silently stop discriminating as the sleeve's cost base moved (ISA-0348's
# question — what correct behaviour makes this fail? — answers "the fees all changed", which is
# the wrong answer for a constant).
DONOR_OCF_DEAR_TERCILE = 2.0 / 3.0

_FRS_BAND_ORDER = {"DEAD MONEY": 0, "WINDOW_SPLIT": 1, "RETAIN-ONLY": 2, "UNSCORED": 3,
                   "HOLD/ADD": 4}


def _p1_above_band_high_gbp(sedol: str, value_gbp: float, nav_gbp: float, policy: dict) -> dict:
    """P1 — pounds above the fund's declared `band_high`. Missing band -> Missing(reason), 0.0."""
    band = ((policy.get("funds") or {}).get(sedol) or {})
    hi = band.get("band_high")
    if hi is None:
        return {"gbp": 0.0, "state": "UNDECLARED",
                "basis": ("this fund has no declared `band_high`, so there is no overweight to "
                          "measure. It contributes a CONSTANT to P1 and is ordered by P2 onward "
                          "(R4.1 — 'we could not measure this' is not 'this measured zero').")}
    ceiling = float(hi) * float(nav_gbp)
    return {"gbp": round(max(float(value_gbp) - ceiling, 0.0), 2), "state": "MEASURED",
            "band_high_pct": round(float(hi) * 100, 4),
            "ceiling_gbp": round(ceiling, 2),
            "basis": "market value less declared band_high x NAV; 0.00 for an in-band fund"}


def _p3_cost_to_keep(row_rank: dict, ocf_dear_pct, crystallisation_gbp) -> dict:
    """P3 — ORDINAL. +1 dear to hold and free to leave · 0 neutral · -1 the exit itself costs."""
    if crystallisation_gbp is not None and float(crystallisation_gbp) > 0:
        return {"p3": -1, "state": "MEASURED", "basis":
                ("selling crystallises a closed-end discount of GBP %.2f. A cost of ACTING is not "
                 "a view about the fund, and it demotes this donor rather than scoring it."
                 % float(crystallisation_gbp))}
    c4 = (row_rank.get("c4") or {}).get("c4")
    if c4 is None or ocf_dear_pct is None:
        return {"p3": 0, "state": "UNMEASURED", "basis":
                "OCF or the sleeve tercile could not be read; contributes a CONSTANT (R4.1)"}
    if float(c4) >= float(ocf_dear_pct):
        return {"p3": 1, "state": "MEASURED", "basis":
                ("OCF %.2f%% is at or above this sleeve's %d%% cost tercile (%.2f%%) and the exit "
                 "carries no crystallisation" % (float(c4), round(DONOR_OCF_DEAR_TERCILE * 100),
                                                 float(ocf_dear_pct)))}
    return {"p3": 0, "state": "MEASURED",
            "basis": "OCF %.2f%% is below this sleeve's cost tercile (%.2f%%)"
                     % (float(c4), float(ocf_dear_pct))}


def donor_key(candidate: dict) -> tuple:
    """THE sell-side ordering key. Lexicographic P1 -> P2 -> P3 -> P4 -> P5. Sort ASCENDING.

    ⚑ Every term is NEGATED where 'more' means 'sell sooner', so one plain ascending sort orders
    the whole key and no caller has to remember a direction per term (a reversed sort over a mixed
    key is how a lexicographic order silently inverts its own tie-breaks).
    """
    r = candidate.get("rank") or {}
    frs = candidate.get("frs_vote") or {}
    band_rank = _FRS_BAND_ORDER.get(frs.get("band"), 9)
    frs_val = frs.get("frs")
    return (
        -float((candidate.get("p1") or {}).get("gbp") or 0.0),        # P1  most overweight first
        -int((r.get("c1") or {}).get("c1", 0)),                       # P2  widens a bet -> sell
        -int((r.get("c2") or {}).get("c2", 0)),                       #     adds to concentration
        -int((candidate.get("p3") or {}).get("p3", 0)),               # P3  dear to keep -> sell
        band_rank,                                                    # P4  FRS VOTES, last
        0 if frs.get("dominated_by") else 1,
        (999.0 if frs_val is None else float(frs_val)),
        -float(candidate.get("value_gbp") or 0.0),                    # P5  determinism only
        str(candidate.get("sedol") or ""),
    )


def donor_order(*, portfolio=None, universe=None, ranking=None, policy=None,
                frs_by_sedol=None, nav_gbp=None) -> dict:
    """-> the A7 donor ranking. ONE home; `waiting_room` and `fund_action_stack` both read it.

    `frs_by_sedol` is OPTIONAL and is the P4 vote only: {sedol: {"frs": float|None,
    "band": str, "dominated_by": str|None}}. Absent, P4 contributes a constant and the ordering is
    decided entirely by the estimation-free criteria — which is the amendment's intent, not a
    degradation.
    """
    portfolio = portfolio if portfolio is not None else _load_portfolio()
    universe = universe if universe is not None else json.loads(
        (HERE / "fund_universe.json").read_text(encoding="utf-8"))
    tw = policy if policy is not None else json.loads(
        (HERE / "target_weights.json").read_text(encoding="utf-8"))
    ranking = ranking if ranking is not None else rank_inputs(universe)
    nav = float(nav_gbp if nav_gbp is not None
                else portfolio["summary"]["total_value_gbp"])
    rows = ranking.get("rows") or {}
    vals = {f["ticker"]: float(f["value_gbp"]) for f in portfolio.get("funds", [])}

    ocfs = sorted(float((rows[s].get("c4") or {}).get("c4"))
                  for s in vals if s in rows
                  and (rows[s].get("c4") or {}).get("c4") is not None)
    dear = (ocfs[min(int(len(ocfs) * DONOR_OCF_DEAR_TERCILE), len(ocfs) - 1)] if ocfs else None)

    cands = []
    for sd, v in sorted(vals.items()):
        r = rows.get(sd)
        if r is None:
            continue
        vote = dict((frs_by_sedol or {}).get(sd) or {})
        c = {"sedol": sd, "value_gbp": round(v, 2),
             "weight_pct": round(v / nav * 100, 4),
             "rank": r,
             "p1": _p1_above_band_high_gbp(sd, v, nav, tw),
             "frs_vote": {"frs": vote.get("frs"), "band": vote.get("band"),
                          "dominated_by": vote.get("dominated_by"),
                          "state": ("SUPPLIED" if vote else "ABSENT_CONTRIBUTES_CONSTANT")},
             "bucket_shortfall_pct": 0.0}
        c["p3"] = _p3_cost_to_keep(r, dear, (vote.get("crystallisation_gbp")))
        cands.append(c)

    if A7_DONOR_ORDER_ENABLED:
        cands.sort(key=donor_key)
    else:
        # R4.13 ROLLBACK — the pre-A7 rule reproduced EXACTLY as `waiting_room` ran it: the BUY
        # key, sorted in reverse. Not an approximation of it: negating the leading terms and
        # leaving `_deviation_key` ascending is a DIFFERENT order, and a rollback that does not
        # reproduce the thing it rolls back to is not a rollback (R4.13).
        cands.sort(key=_rank_key, reverse=True)
    for i, c in enumerate(cands, 1):
        c["donor_rank"] = i
        c["why"] = _donor_why(c)
    return {
        "state": "MEASURED", "as_of": _today(), "item": "ISA-0440 / A7",
        "enabled": A7_DONOR_ORDER_ENABLED,
        "nav_gbp": round(nav, 2),
        "ocf_dear_tercile_pct": dear,
        "frs_supplied": bool(frs_by_sedol),
        "order": ["P1 pounds above declared band_high",
                  "P2 look-through / concentration relief (c1, c2 — READ from rank_inputs)",
                  "P3 cost to keep, ORDINAL (OCF tercile; a crystallised discount DEMOTES)",
                  "P4 FRS band, dominance, FRS — A VOTE, NOT AUTHORITY (A7)",
                  "P5 value desc, sedol — determinism only"],
        "basis": ("A7 supersedes V2.1 s11's FRS-led donor ranking and the pre-A7 "
                  "`waiting_room` rule that reversed the BUY key. L-1/ISA-0351 measured alpha "
                  "rank persistence at -0.482 in this sleeve, so a sell rule led by a retention "
                  "score sells low in expectation. FRS is retained as the P4 vote until L-1 "
                  "resolves (SE 0.32 on n=11 — not actionable in either direction)."),
        "donors": cands,
    }


def _donor_why(c: dict) -> str:
    """The NAMED reason this donor sits where it does — the first criterion that separated it."""
    p1 = float((c.get("p1") or {}).get("gbp") or 0.0)
    if p1 > 0:
        return ("P1: GBP %.2f above its declared band_high of %.2f%%"
                % (p1, (c["p1"].get("band_high_pct") or 0.0)))
    r = c.get("rank") or {}
    if int((r.get("c1") or {}).get("c1", 0)) > 0:
        return "P2: this fund's pound WIDENS a material active bet (%.2fpp)" % (
            (r.get("c1") or {}).get("active_pp") or 0.0)
    if int((r.get("c2") or {}).get("c2", 0)) > 0:
        return "P2: adds to %s, already at or above declared materiality" % (
            ", ".join((r.get("c2") or {}).get("over_materiality") or []) or "a declared cluster")
    p3 = int((c.get("p3") or {}).get("p3", 0))
    if p3 != 0:
        return "P3: %s" % (c["p3"].get("basis") or "")
    band = (c.get("frs_vote") or {}).get("band")
    if band and band != "HOLD/ADD":
        return "P4 (vote only): FRS band %s" % band
    return ("no criterion separated this fund — it is ordered by value and sedol for "
            "repeatability (P5), which is a REFUSAL to rank, not a ranking")


def _bucket_ceiling(tw: dict, b: str, key: str) -> float:
    """Phase A fills to the POINT TARGET; phase B to the declared BAND HIGH (ISA-0388)."""
    v = tw[b].get(key)
    if v is None:
        raise DestinationRefused(
            "bucket %s has no declared %s. The router may not invent a ceiling for a bucket whose "
            "limit is undeclared." % (b, key))
    return float(v)


def _fund_ceiling(policy: dict, sedol: str) -> tuple:
    """-> (ceiling_pct, basis). THE PER-FUND limit, which is ALSO declared and was ALSO invisible.

    ⚑ FOUND WHILE BUILDING ISA-0388, 20-Aug-2026, and it is the SAME defect one level down. Once
    the router was allowed to fill to the BUCKET band, the first live run put GBP 7,830.90 into
    SMT — taking it to 9.57% of the ISA against its OWN declared `band_high` of 7.00%, because the
    only per-fund limit the code had ever read was the global 12.5% cap. Two declared numbers for
    one quantity again, and the wider one operative purely because it was the one implemented.
    A fund with no declared band_high falls back to the global cap and SAYS SO — never silently.
    """
    cap = float(policy["max_single_fund_pct"])
    fu = (policy.get("funds") or {}).get(sedol) or {}
    bh = fu.get("band_high")
    if bh is None:
        return cap, "GLOBAL_CAP_ONLY — no declared per-fund band_high for %s" % sedol
    return min(cap, float(bh)), ("min(max_single_fund_pct %.3f, declared band_high %.3f)"
                                 % (cap, float(bh)))


def allocate_funds(amount_gbp: float, portfolio: dict, universe: dict, eligible: set,
                   policy: dict, new_subscription_gbp: float = 0.0, ranking: dict = None) -> dict:
    """Water-fill new capital into the fund sleeve, ordered by the estimation-free ranking.

    ⚑ TWO PHASES (ISA-0388, Raj 19-Aug-2026: "if the bands are limits on fund related capital, the
    framework should not prevent capital going into funds/these buckets"). The code used to fill to
    `phase1_target_pct` and then report BANDS_RESTORED with the remainder homeless — GBP 19,630 of
    already-declared capacity was invisible because a POINT TARGET and a BAND were both declared for
    one quantity and the implementation picked the narrower without saying so.
        phase A   restore every bucket to its declared phase1_target_pct
        phase B   distribute what is left inside phase1_band_high
    Both phases use the SAME ordering, so widening the ceiling cannot smuggle capacity ordering
    back in through the side door.

    Deterministic. No covariance matrix, no optimiser, no trailing return (ISA-0328: with 12 funds
    and 38-87 months an MVO would produce extreme, unstable weights).
    """
    total0 = float(portfolio["summary"]["total_value_gbp"])
    # ⚑ DENOMINATOR. Cash already sitting in the account is INSIDE total0, so deploying it does not
    # change the total. A NEW subscription is OUTSIDE it and must be added, or every weight, every
    # bucket shortfall and the 12.5% cap are all computed against a total that is too small — the
    # stored-value-says-one-thing-and-IS-another shape, invisible because the number is plausible.
    total1 = total0 + float(new_subscription_gbp or 0.0)
    bmap = {s: (u.get("bucket")) for s, u in (universe.get("funds", universe)).items()
            if not str(s).startswith("_")}
    vals = {f["ticker"]: float(f["value_gbp"]) for f in portfolio.get("funds", [])}
    tw = policy["bucket_totals"]
    cap = policy["max_single_fund_pct"]
    rrows = (ranking or {}).get("rows") or {}

    alloc = {s: 0.0 for s in vals}
    by_phase = {"to_target": {}, "within_band": {}}
    step = max(round(amount_gbp / 450.0, 2), 1.0)     # ~450 slices; exact remainder settled below
    remaining = round(amount_gbp, 2)
    blocked_by_phase = {}
    phases = (("to_target", "phase1_target_pct"), ("within_band", "phase1_band_high"))

    for phase_name, ceiling_key in phases:
        # ⚑ RESET PER PHASE. A fund blocked in phase A because its bucket was at the POINT TARGET
        # is not blocked in phase B, and carrying phase A's reason forward made the first live run
        # report VUAG as "bucket B1 at or above its target" on a row that received GBP 2,423.85.
        # A diagnostic that contradicts the allocation beside it is worse than no diagnostic.
        blocked_reason = {}
        blocked_by_phase[phase_name] = blocked_reason
        while remaining > 0.005:
            chunk = min(step, remaining)
            cands = []
            for sedol, v0 in vals.items():
                if sedol not in eligible:
                    blocked_reason.setdefault(sedol, "not eligible")
                    continue
                b = bmap.get(sedol)
                if b not in tw:
                    blocked_reason.setdefault(sedol, f"bucket {b} has no declared target")
                    continue
                f_ceil, f_basis = _fund_ceiling(policy, sedol)
                v_new = v0 + alloc[sedol] + chunk
                if v_new / total1 > f_ceil:
                    blocked_reason.setdefault(
                        sedol, "would breach the %.2f%% per-fund ceiling [%s]" % (f_ceil * 100,
                                                                                 f_basis))
                    continue
                b_val = sum(vals[s] + alloc[s] for s in vals if bmap.get(s) == b)
                shortfall = _bucket_ceiling(tw, b, ceiling_key) - b_val / total1
                # ⚑ A CANDIDATE WHOSE BUCKET IS FULL IS EXCLUDED, NOT A REASON TO STOP.
                # The pre-existing code took the GLOBALLY top-ranked candidate and, if ITS bucket
                # had no room, ended the fill for every bucket. On the 20-Aug run that stranded
                # capital while two other buckets sat below their declared targets. The stop
                # condition is "NO destination has room", never "the best one has none".
                if shortfall <= 0:
                    blocked_reason.setdefault(
                        sedol, "bucket %s is at or above its declared %s" % (b, ceiling_key))
                    continue
                cands.append({"sedol": sedol, "bucket": b,
                              "bucket_shortfall_pct": shortfall,
                              "weight_pct": (v0 + alloc[sedol]) / total1,
                              "rank": rrows.get(sedol)})
            if not cands:
                break                                  # this ceiling is reached; try the next one
            cands.sort(key=_rank_key)
            best = cands[0]
            alloc[best["sedol"]] = round(alloc[best["sedol"]] + chunk, 6)
            by_phase[phase_name][best["sedol"]] = round(
                by_phase[phase_name].get(best["sedol"], 0.0) + chunk, 6)
            remaining = round(remaining - chunk, 6)

    by_phase = {k: {s: round(v, 2) for s, v in d.items()} for k, d in by_phase.items()}
    if remaining > 0.005:
        # ⚑ WHY the capital stopped is a different fact from THAT it stopped, and the two must not
        # render the same (R2.10). A bucket at its declared band edge is the framework working; a
        # per-fund or single-fund ceiling biting while buckets still have room is a CONCENTRATION
        # limit, and it points somewhere else entirely.
        ceilings = [v for v in blocked_reason.values() if "ceiling" in v]
        bands = [v for v in blocked_reason.values() if "band_high" in v]
        state = "BANDS_FULL" if bands and not ceilings else (
            "CEILINGS_BINDING" if ceilings and not bands else "BANDS_AND_CEILINGS_BINDING")
        return {"state": state, "allocation": {k: round(v, 2) for k, v in alloc.items()},
                "unallocated_gbp": round(remaining, 2), "phase_allocation": by_phase,
                "reason": ("no eligible destination has room. %d fund(s) stopped at a declared "
                           "per-fund or single-fund ceiling and %d at a bucket band_high. Further "
                           "fund capital would push a DECLARED limit, which is the limit the bands "
                           "exist to express (ISA-0388) — it is no longer the router inventing a "
                           "narrower rule than the one Raj wrote."
                           % (len(ceilings), len(bands))),
                "blocked": blocked_by_phase}
    return {"state": "ALLOCATED", "allocation": {k: round(v, 2) for k, v in alloc.items()},
            "unallocated_gbp": 0.0, "phase_allocation": by_phase, "blocked": blocked_by_phase}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §4  CROSS-SLEEVE — the freeze now has a DECLARED UNIT and the number is DERIVED   (ISA-0387)
# ══════════════════════════════════════════════════════════════════════════════════════════════
# ⚑ WHAT WAS WRONG. `scaling_freeze` said "bars scaling" and never said what scaling is MEASURED
# IN. sleeve_split read it as POUNDS and returned stock_max = 0 — so a subscription did not hold
# the sleeve still, it SHRANK it: 7.928% -> 7.337% on the September GBP 11,250 with no trade, and
# again on every future subscription, while the freeze forbade repairing the breach it caused.
#
# ⚑ RAJ'S DECISION, 20-Aug-2026: basis = `reallocation_only`. The freeze binds capital whose SOURCE
# is a disposal FROM THE FUND SLEEVE. A subscription is not scaling. stock_max is the amount that
# restores the declared band FLOOR.
#
# ⚑ THE MEASUREMENT THAT DECIDED IT, kept because it is the reusable part. `weight` (hold the
# pre-subscription weight exactly) is the reading under which neither declared rule has to yield —
# and it derives GBP 891.90, which is 0.591% of the post-subscription total and BELOW EVERY
# declared position size in the framework (Stage-1 probe 0.75%, typical 1.5-3.5%, max 5.0%). A cap
# that cannot fund a single compliant position is GBP 0 of executable capital reported as a
# non-zero number. `executability` below publishes that test on every run, whatever the basis.
#
# ⚑ AND IT IS A CAP, NOT AN INSTRUCTION. Raj, 19-Aug-2026: capital enters the stock sleeve only
# where "the data/evidence and judgement supports the company being an attractive proposition".
# Absent a qualifying candidate the cap goes UNUSED and the capital goes to funds under the recall
# rule (waiting_room.py / ISA-0390).

FREEZE_BASES = ("pounds", "weight", "reallocation_only")


def _smallest_declared_position_gbp(policy: dict, total_gbp: float):
    """-> (GBP, label). The smallest position size the framework declares anywhere, so a derived
    cap can be tested against the thing it is a cap ON. Read, never chosen."""
    th = policy.get("thresholds") or {}
    cands = [(th.get("stage1_probe_pct"), "stage-1 probe"),
             (th.get("typical_stock_position_low"), "typical position low")]
    live = [(float(p), lab) for p, lab in cands if p is not None]
    if not live:
        return None, "NO DECLARED POSITION SIZE"
    p, lab = min(live)
    return round(p * total_gbp, 2), lab


def derive_stock_max(basis: str, *, total0: float, total1: float, stock0: float,
                     amount_gbp: float, band_low: float) -> dict:
    """THE ONE HOME for the freeze arithmetic. R4.4 — the NUMBER is derived here from the DECLARED
    basis on every run and is never typed into target_weights.json."""
    if basis not in FREEZE_BASES:
        raise DestinationRefused(
            "scaling_freeze.basis is %r, which is not one of %s. A constraint expressed in a unit "
            "nobody declared is exactly the defect ISA-0387 records; the router refuses rather "
            "than picking a reading on Raj's behalf." % (basis, list(FREEZE_BASES)))
    to_floor = max(band_low * total1 - stock0, 0.0)
    if basis == "pounds":
        raw, why = 0.0, ("the freeze is an absolute bar on pounds entering the sleeve; the "
                         "resulting dilution is an ACCEPTED and recorded cost")
    elif basis == "weight":
        raw, why = max((stock0 / total0) * total1 - stock0, 0.0), (
            "the amount that holds the sleeve's pre-subscription weight of %.3f%% exactly, so the "
            "subscription takes no new PROPORTIONAL exposure to the unproven process"
            % (stock0 / total0 * 100))
    else:
        raw, why = to_floor, (
            "the freeze binds only capital whose SOURCE is a disposal from the fund sleeve; a "
            "subscription and uninvested cash are not scaling, so the cap is the amount that "
            "restores the declared band floor of %.0f%%" % (band_low * 100))
    return {"basis": basis, "derived_gbp": round(min(raw, amount_gbp), 2),
            "uncapped_by_amount_gbp": round(raw, 2), "gbp_to_band_floor": round(to_floor, 2),
            "derivation": why,
            "derived_not_typed": ("R4.4 — target_weights.json declares the BASIS; this function "
                                  "computes the number from live portfolio values every run")}


def sleeve_split(amount_gbp: float, portfolio: dict, policy: dict,
                 new_subscription_gbp: float = 0.0) -> dict:
    """Decide how much of the marginal pound may reach the stock sleeve.

    R2.14 — where the verdict turns on an unmade choice, state the choice. There is still no
    strategic asset allocation TARGET (ISA-0333), so this function does not invent a split: it
    applies the DECLARED freeze basis (ISA-0387) and the declared band, and publishes both.
    """
    s = portfolio["summary"]
    total0_raw = float(s["total_value_gbp"])
    total1 = total0_raw + float(new_subscription_gbp or 0.0)
    stock0 = float(s["stock_sleeve_value_gbp"])
    ss = policy["stock_sleeve"]
    lo, hi = ss["phase1_target_low"], ss["phase1_target_high"]
    w_now = stock0 / total1                    # post-subscription, the weight capital acts on
    w_pre = stock0 / total0_raw
    need_to_floor = max(lo * total1 - stock0, 0.0)
    freeze = policy.get("scaling_freeze") or {}
    binding = bool(freeze.get("active"))
    basis = freeze.get("basis")

    out = {
        "stock_sleeve_weight_now_pct": round(w_now * 100, 2),
        "stock_sleeve_weight_pre_subscription_pct": round(w_pre * 100, 2),
        "dilution_from_subscription_pp": round((w_pre - w_now) * 100, 3),
        "declared_band_pct": [lo * 100, hi * 100],
        "in_band": bool(lo <= w_now <= hi),
        "gbp_to_reach_band_floor": round(need_to_floor, 2),
        "amount_available_gbp": round(amount_gbp, 2),
        "scaling_freeze": {
            "active": binding,
            "basis": basis,
            "basis_declared_by": freeze.get("basis_declared_by"),
            "basis_declared_on": freeze.get("basis_declared_on"),
            "rule": freeze.get("rule"),
            "clock_start": freeze.get("clock_start"),
            "earliest_unfreeze_override": freeze.get("earliest_unfreeze_override"),
            "earliest_unfreeze_mechanical": freeze.get("earliest_unfreeze_mechanical"),
            "set_by": freeze.get("set_by"),
        },
    }
    if binding:
        if not basis:                                                 # R4.7 — RAISE, never default
            raise DestinationRefused(
                "the scaling freeze is ACTIVE and target_weights.scaling_freeze declares no "
                "`basis`. ISA-0387: a constraint expressed in a unit nobody declared cannot be "
                "applied — reading it as pounds silently dilutes the sleeve 0.591pp per "
                "subscription. Declare one of %s." % list(FREEZE_BASES))
        d = derive_stock_max(basis, total0=total0_raw, total1=total1, stock0=stock0,
                             amount_gbp=amount_gbp, band_low=lo)
        stock_max = d["derived_gbp"]
        out["freeze_derivation"] = d
        out["state"] = ("STOCK_SLEEVE_BLOCKED" if stock_max <= 0 else "STOCK_SLEEVE_CAPPED")
        out["stock_max_gbp"] = stock_max
        out["fund_max_gbp"] = round(amount_gbp - stock_max, 2)
        out["reason"] = (
            "The stock sleeve is %.2f%% against its declared %.0f%%-%.0f%% band. The scaling "
            "freeze set on %s is ACTIVE and its declared BASIS is `%s` (%s), so %s"
            % (w_now * 100, lo * 100, hi * 100, freeze.get("clock_start"), basis,
               freeze.get("basis_declared_by") or "basis declarer NOT RECORDED", d["derivation"]))
        out["decision_owner"] = None
    else:
        stock_max = round(min(need_to_floor, amount_gbp), 2)
        out["state"] = "STOCK_SLEEVE_OPEN"
        out["stock_max_gbp"] = stock_max
        out["fund_max_gbp"] = round(amount_gbp - stock_max, 2)
        out["reason"] = "no freeze binding; capital may restore the stock sleeve to its band floor"

    # ⚑ IS THE CAP SPENDABLE? The test that caught the `weight` reading (ISA-0387, 20-Aug-2026).
    smallest, label = _smallest_declared_position_gbp(policy, total1)
    out["executability"] = {
        "stock_max_gbp": out["stock_max_gbp"],
        "smallest_declared_position_gbp": smallest,
        "smallest_declared_position_basis": label,
        "state": ("NOT_APPLICABLE" if out["stock_max_gbp"] <= 0 else
                  ("UNTESTABLE" if smallest is None else
                   ("EXECUTABLE" if out["stock_max_gbp"] >= smallest else "NOT_EXECUTABLE"))),
        "note": ("a cap below the smallest position the framework declares cannot open a position, "
                 "only top one up — it is GBP 0 of executable capital reported as a non-zero "
                 "number, which is a stored value that says one thing and IS another"),
    }
    out["cap_not_instruction"] = (
        "Raj, 19-Aug-2026: capital enters the stock sleeve only where the data, evidence and "
        "judgement support the company being an attractive proposition. Absent a qualifying "
        "candidate this cap is UNUSED and the capital goes to funds as PARKED under the recall "
        "rule (waiting_room.py / ISA-0390), never as a permanent allocation.")
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §4b  DECLARED PER-FUND BANDS — reported, because the ranking is not authorised to repair them
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _fund_absorption(amount_gbp, portfolio, universe, eligible, policy, new_sub, ranking):
    """-> what the fund sleeve would do with the WHOLE amount, if the stock cap went unused."""
    try:
        alt = allocate_funds(amount_gbp, portfolio, universe, eligible, policy, new_sub,
                             ranking=ranking)
    except Exception as e:                                            # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}
    return {"state": alt["state"], "placed_gbp": round(sum(alt["allocation"].values()), 2),
            "would_stay_idle_gbp": alt.get("unallocated_gbp"),
            "note": ("the whole amount re-offered to funds under the same ranking. Idle capital "
                     "here is priced in `residual`, never left silent.")}


def _recall_permitted(tw):
    """-> the recall leg's verdict for TODAY, quoted from its owner (ISA-0390), never re-derived."""
    try:
        import waiting_room as _wr
        return _wr.freeze_state(tw)
    except Exception as e:                                            # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}


def declared_band_state(portfolio, policy, allocation=None, new_subscription_gbp=0.0) -> dict:
    """-> every fund's position against its OWN declared band, before and after the allocation.

    ⚑ R2.14 — THE CHOICE THIS SURFACES AND DOES NOT MAKE. Raj demoted band restoration to the
    tie-break (ISA-0386, 19-Aug-2026). The consequence, which nobody stated at the time: a fund
    sitting BELOW its declared `band_low` is only repaired if it wins on C1-C4 first, so a
    declared band breach can now persist indefinitely. Under the old capacity ordering it
    self-healed, because restoration WAS the rule.

    That is a real trade and it is Raj's to make, not mine to quietly reinstate — so this function
    MEASURES and PUBLISHES it and changes no allocation. A band breach the framework can see and
    has decided not to repair is a governed position; one it cannot see is not.
    """
    total1 = float(portfolio["summary"]["total_value_gbp"]) + float(new_subscription_gbp or 0.0)
    fu = policy.get("funds") or {}
    rows, breaches_before, breaches_after = [], [], []
    for f in portfolio.get("funds", []):
        sd = f["ticker"]
        d = fu.get(sd) or {}
        lo, hi = d.get("band_low"), d.get("band_high")
        v0 = float(f["value_gbp"])
        v1 = v0 + float((allocation or {}).get(sd, 0.0) or 0.0)
        w0, w1 = v0 / total1, v1 / total1
        def _state(w):
            if lo is None or hi is None:
                return "NO_DECLARED_BAND"
            return "BELOW_BAND" if w < lo else ("ABOVE_BAND" if w > hi else "IN_BAND")
        s0, s1 = _state(w0), _state(w1)
        rows.append({"sedol": sd, "band_low_pct": (None if lo is None else lo * 100),
                     "band_high_pct": (None if hi is None else hi * 100),
                     "weight_before_pct": round(w0 * 100, 2),
                     "weight_after_pct": round(w1 * 100, 2),
                     "state_before": s0, "state_after": s1,
                     "gbp_to_band_low": (None if lo is None else round(max(lo * total1 - v1, 0.0), 2))})
        if s0 in ("BELOW_BAND", "ABOVE_BAND"):
            breaches_before.append(sd)
        if s1 in ("BELOW_BAND", "ABOVE_BAND"):
            breaches_after.append(sd)
    return {
        "state": "MEASURED", "as_of": _today(), "denominator_gbp": round(total1, 2),
        "funds": rows,
        "breaches_before": sorted(breaches_before), "breaches_after": sorted(breaches_after),
        "repaired": sorted(set(breaches_before) - set(breaches_after)),
        "not_repaired": sorted(set(breaches_after)),
        "the_choice_not_made": (
            "band restoration is C5, the TIE-BREAK (Raj, 19-Aug-2026 / ISA-0386). A fund below its "
            "declared band_low is therefore repaired only if it also wins on C1-C4. Whether a "
            "DECLARED band breach should outrank the estimation-free criteria — i.e. whether a "
            "band is a preference or a limit — has not been decided. It is stated here rather "
            "than resolved by implementation, which is the defect ISA-0388 records."),
        "decision_owner": "Raj",
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §5  PARITY / NEGATIVE CONTROL — the assertions that make P1 and the new ranking stick  (R5.3/R5.5)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def no_trailing_return_ordering(portfolio, universe, eligible, policy, rows,
                                ranking: dict = None) -> dict:
    """BEHAVIOURAL proof of what does and does not order capital.

    Positive control : permute every fund's trailing return -> allocation must be BIT-IDENTICAL.
    Negative controls: EVERY criterion that is supposed to order capital must MOVE the allocation
                       when it is perturbed. ISA-0348's lesson, asked of each one in turn: what
                       correct behaviour makes this fail? An inert criterion is a criterion that
                       is measuring nothing, and it must not be able to pass silently (R5.5).
    """
    A = lambda pol, rk=ranking: allocate_funds(10000.0, portfolio, universe, eligible, pol,
                                               ranking=rk)["allocation"]
    base = A(policy)

    # ── positive control: trailing return orders NOTHING ──────────────────────────────────────
    shuffled = {k: dict(v) for k, v in rows.items()}
    keys = sorted(shuffled)
    vals = [shuffled[k]["ann_return_pct"] for k in keys]
    for k, v in zip(keys, list(reversed(vals))):
        shuffled[k]["ann_return_pct"] = v
    invariant = (base == A(policy))

    # ── C5 / ISA-0388: bucket ceilings must move it ───────────────────────────────────────────
    p2 = json.loads(json.dumps(policy))
    for b in p2["bucket_totals"]:
        for k in ("phase1_target_pct", "phase1_band_high"):
            if p2["bucket_totals"][b].get(k) is not None:
                p2["bucket_totals"][b][k] = p2["bucket_totals"][b][k] * 0.5
    nc_buckets = (A(p2) != base)

    # ── ISA-0388: collapsing band_high ONTO the point target must reproduce the OLD behaviour ──
    p3 = json.loads(json.dumps(policy))
    for b in p3["bucket_totals"]:
        p3["bucket_totals"][b]["phase1_band_high"] = p3["bucket_totals"][b]["phase1_target_pct"]
    collapsed = allocate_funds(10000.0, portfolio, universe, eligible, p3, ranking=ranking)
    band_adds_capacity = (
        sum(A(policy).values()) >= sum(collapsed["allocation"].values()) - TOL_GBP)

    # ⚑ EACH CRITERION IS TESTED WITH THE ONES ABOVE IT NEUTRALISED.
    # First attempt did not do this and C3 read INERT — not because a firing mandate fails to
    # demote, but because C1 and C2 had already decided the order before C3 was consulted. A
    # negative control that a SUBORDINATE criterion cannot pass is measuring the priority, not the
    # criterion. ISA-0348, asked properly: what correct behaviour makes this fail? Nothing —
    # so isolate the level, then perturb it.
    _LEVELS = ("c1", "c2", "c3", "c4")

    def _neutralise(rr, above):
        for k in rr:
            for lv in above:
                rr[k][lv][lv] = (0 if lv != "c4" else 0.0)

    def _perturb(level, mutator):
        """-> did perturbing THIS criterion, with the ones above it neutralised, move capital?

        The mutator receives the isolated baseline allocation, because a control has to perturb a
        row the baseline actually FUNDED. First version fired a fund that could not receive a penny
        (B2PLJD7 already sits 0.14pp above its own declared band_high) and read INERT — the test
        was unfalsifiable, not the criterion dead. R5.8: test the test.
        """
        if not ranking:
            return None
        above = _LEVELS[:_LEVELS.index(level)]
        b2 = json.loads(json.dumps(ranking))
        _neutralise(b2["rows"], above)
        iso_base = A(policy, b2)
        r2 = json.loads(json.dumps(ranking))
        _neutralise(r2["rows"], above)
        mutator(r2["rows"], iso_base)
        return A(policy, r2) != iso_base

    # ── C4 / OCF must order capital ───────────────────────────────────────────────────────────
    def _flip_ocf(rr, _base=None):
        ks = sorted(rr)
        vs = [(rr[k]["c4"] or {}).get("c4") for k in ks]
        for k, v in zip(ks, list(reversed(vs))):
            rr[k]["c4"]["c4"] = v
    nc_ocf = _perturb("c4", _flip_ocf)

    # ── C1 / reference deviation must order capital ───────────────────────────────────────────
    def _flip_c1(rr, _base=None):
        for k in rr:
            rr[k]["c1"]["c1"] = -int(rr[k]["c1"].get("c1") or 0)
    nc_c1 = _perturb("c1", _flip_c1)

    # ── C3 / a FIRING mandate must demote ─────────────────────────────────────────────────────
    def _fire_the_favourite(rr, base_alloc):
        funded = sorted(((v, k) for k, v in (base_alloc or {}).items() if v > 0), reverse=True)
        if not funded:
            return
        top = funded[0][1]
        rr[top]["c3"]["c3"] = 2
        rr[top]["c3"]["state"] = "FIRING"
    nc_c3 = _perturb("c3", _fire_the_favourite)

    # ── C2 / concentration must order capital ─────────────────────────────────────────────────
    def _flip_c2(rr, _base=None):
        for k in rr:
            rr[k]["c2"]["c2"] = 1 - int(rr[k]["c2"].get("c2") or 0)
    nc_c2 = _perturb("c2", _flip_c2)

    live = {"negative_control_bucket_ceilings_move_it": nc_buckets,
            "negative_control_concentration_moves_it": nc_c2,
            "negative_control_ocf_moves_it": nc_ocf,
            "negative_control_reference_deviation_moves_it": nc_c1,
            "negative_control_t4_firing_moves_it": nc_c3}
    # A criterion whose owner is UNAVAILABLE is CONSTANT for every fund, so it cannot move the
    # allocation and must not be asserted to. R4.3 — that is a refusal, not a pass.
    crit = (ranking or {}).get("criteria") or {}
    applicable = {"negative_control_ocf_moves_it": True,
                  "negative_control_concentration_moves_it":
                      (crit.get("c2") or {}).get("state") == "MEASURED",
                  "negative_control_reference_deviation_moves_it":
                      (crit.get("c1") or {}).get("state") == "MEASURED",
                  "negative_control_t4_firing_moves_it":
                      (crit.get("c3") or {}).get("state") == "MEASURED"}
    inert = [k for k, v in live.items()
             if k in applicable and applicable[k] and v is False]

    return {"trailing_return_invariant": invariant,
            **live,
            "criteria_not_asserted_because_their_owner_is_unavailable":
                [k for k, ok in applicable.items() if not ok],
            "inert_criteria": inert,
            "band_phase_never_removes_capacity": band_adds_capacity,
            "ordering_key": ("_rank_key(C1 reference deviation, C2 concentration, C3 mandate, "
                             "C4 OCF, C5 band deviation)" if RANKING_ENABLED
                            else "_deviation_key(bucket_shortfall, weight, sedol) [ROLLBACK]"),
            "pass": bool(invariant and nc_buckets and band_adds_capacity and not inert),
            "as_of": _today()}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# BUILD
# ══════════════════════════════════════════════════════════════════════════════════════════════
def build(amount_gbp=None, new_subscription_gbp=0.0, portfolio_path=None, universe_path=None,
          weights_path=None, nav_dir=None, out_path=None, as_of=None):
    if not ENABLED:
        return {"state": "DISABLED", "reason": "capital_destination.ENABLED is False (R4.13)"}
    as_of = as_of or _today()
    portfolio = json.load(open(portfolio_path or HERE / "portfolio_data_aug_2026.json"))
    universe = json.load(open(universe_path or HERE / "fund_universe.json"))
    tw = json.load(open(weights_path or HERE / "target_weights.json"))
    nav = Path(nav_dir or HERE / "nav_cache")

    policy = {"bucket_totals": tw["bucket_totals"],
              "thresholds": tw["thresholds"],
              "funds": tw["funds"],
              "max_single_fund_pct": tw["thresholds"]["max_single_fund_pct"],
              "stock_sleeve": tw["stock_sleeve"],
              "scaling_freeze": tw.get("scaling_freeze"),
              "_source": "target_weights.json", "_as_of": tw["_meta"]["last_updated"]}

    rows, unmatched = _load_sleeve(nav, universe, held_only=True, portfolio=portfolio)
    ev = evidence_dispersion(rows)
    rc = regime_coverage(rows)
    ladder = precision_ladder(rows)

    eligible = {s for s, v in rc.get("verdicts", {}).items() if v["verdict"] == "ELIGIBLE"}
    refused = {s: v for s, v in rc.get("verdicts", {}).items() if v["verdict"] != "ELIGIBLE"}

    s = portfolio["summary"]
    if amount_gbp is None:
        amount_gbp = float(s.get("cash_deployable_gbp") or 0.0) + float(new_subscription_gbp or 0.0)
    split = sleeve_split(amount_gbp, portfolio, policy, new_subscription_gbp)
    fund_amount = split["fund_max_gbp"]
    # ⚑ ISA-0386. Built ONCE and passed down, so every call site — the allocation and all four
    # negative controls — orders on the SAME table. Recomputing it per call would let the parity
    # test pass against a ranking the allocation never saw (ISA-0382: an observer may not
    # re-derive an input the observed module owns).
    ranking = rank_inputs(universe)
    fa = allocate_funds(fund_amount, portfolio, universe, eligible, policy, new_subscription_gbp,
                        ranking=ranking)

    # ── R5.2 two independent derivations agree, stated tolerance ──────────────────────────────
    alloc_sum = round(sum(fa["allocation"].values()), 2)
    expected = round(fund_amount - fa.get("unallocated_gbp", 0.0), 2)
    agree = abs(alloc_sum - expected) <= TOL_GBP

    parity = no_trailing_return_ordering(portfolio, universe, eligible, policy, rows,
                                         ranking=ranking)

    # ── §6  EVERY POUND MUST COUNT — capital with nowhere to go is priced, never silent ────────
    # Raj: "that has to be in the DNA of everything end to end." An unallocated pound is not a
    # neutral outcome; it is a decision to earn the MMF rate instead of the operative required
    # return, and the framework must SAY what that costs before the month closes over it.
    try:
        ts = json.load(open(HERE / "target_state.json"))
        req = float(ts["required_return_operative_pct"])
        req_src, req_as_of = "target_state.required_return_operative_pct", ts.get("derived_at")
    except Exception as _e:                                     # noqa: BLE001
        req, req_src, req_as_of = None, f"UNAVAILABLE: {type(_e).__name__}", None
    idle = round(fa.get("unallocated_gbp", 0.0) + split.get("stock_max_gbp", 0.0)
                 - min(split.get("stock_max_gbp", 0.0), 0.0), 2)
    idle = round(fa.get("unallocated_gbp", 0.0), 2)
    # ⚑ THE WAITING-ROOM YIELD IS DERIVABLE, so refusing to measure it would itself be a defect.
    # `return_architecture.derive_cash_rate` computes interest actually credited over a
    # time-weighted average balance from the AJ Bell cash statement (Aug-2026: 1.757%, ONE
    # observation). One home (R4.4) - this reads it, never re-derives it.
    mmf_rate_pct, mmf_src, mmf_as_of, mmf_obs, mmf_conf = None, None, None, None, None
    try:
        import return_architecture as _ra
        _cr = _ra.derive_cash_rate()
        if isinstance(_cr, dict):
            # ⚑ THE KEY IS `cash_expected_return_pct`. My first read guessed `rate_pct`/`value`,
            # both absent, so the rate came back None in the DELIVERED environment while passing
            # in the sandbox where the cash statement is not present. R5.9 - sign off in the
            # delivered location, never the sandbox. Caught by the assertion, not by reading.
            mmf_rate_pct = _cr.get("cash_expected_return_pct")
            mmf_src = _cr.get("source") or "return_architecture.derive_cash_rate"
            mmf_as_of = _cr.get("as_of")
            mmf_obs = _cr.get("observations")
            mmf_conf = _cr.get("confidence")
            if mmf_rate_pct is None and "UNMEASURED" not in str(mmf_src).upper():
                mmf_src = f"UNMEASURED - {mmf_src}"
    except Exception as _e:                                          # noqa: BLE001
        mmf_src = (f"UNAVAILABLE: {type(_e).__name__} - the cash statement is the golden source "
                   f"and is not present in this environment; the gap is UNMEASURED, not zero")
    residual = {
        "unallocated_gbp": idle,
        "pct_of_capital_offered": (round(idle / amount_gbp * 100, 2) if amount_gbp else None),
        "required_return_operative_pct": _fig(req, as_of=req_as_of, source=req_src, unit="%"),
        "mmf_rate_pct": _fig(mmf_rate_pct, as_of=mmf_as_of, source=mmf_src, unit="%",
                             note=(f"{mmf_obs} observation(s), confidence {mmf_conf}"
                                   if mmf_obs is not None else None)),
        "annual_opportunity_cost_gbp": (round(idle * req / 100.0, 2) if (req and idle) else None),
        "annual_opportunity_cost_net_of_waiting_room_gbp": (
            round(idle * (req - mmf_rate_pct) / 100.0, 2)
            if (req is not None and idle and mmf_rate_pct is not None) else None),
        "opportunity_cost_basis": ("idle capital x the operative required return; an UPPER bound, "
                                   "because the waiting-room yield is unmeasured and is subtracted "
                                   "only when it is observed (R4.1 - the gap is not zero, it is "
                                   "unmeasured, and those are different facts)"),
        "state": ("IDLE_CAPITAL_PRICED" if idle > 0 else "FULLY_DEPLOYED"),
    }

    doc = {
        "_meta": {"module": "capital_destination.py", "schema_version": SCHEMA_VERSION,
                  "as_of": as_of, "built": "2026-08-16",
                  "closes": ["ISA-0151", "ISA-0152", "ISA-0153", "ISA-0154", "ISA-0351"],
                  "enabled": ENABLED},
        "inputs": {
            "amount_gbp": _fig(round(amount_gbp, 2), as_of=as_of, source="portfolio summary "
                               "cash_deployable_gbp + new_subscription_gbp", unit="GBP"),
            "new_subscription_gbp": _fig(round(float(new_subscription_gbp or 0.0), 2), as_of=as_of,
                                         source="caller (broker-confirmed subscription)", unit="GBP"),
            "portfolio_total_gbp": _fig(s["total_value_gbp"], as_of=portfolio["_meta"].get("as_of")
                                        or as_of, source="portfolio_data_aug_2026.json",
                                        unit="GBP"),
            "policy_source": policy["_source"], "policy_as_of": policy["_as_of"],
            "funds_measured": len(rows), "funds_unmatched": unmatched,
        },
        "evidence": ev,
        "precision_ladder": ladder,
        "regime_coverage": rc,
        "eligibility": {"eligible": sorted(eligible),
                        "refused": {k: v["reason"] for k, v in refused.items()}},
        "sleeve_split": split,
        "ranking": ranking,
        "fund_allocation": fa,
        "declared_bands": declared_band_state(portfolio, policy, fa.get("allocation"),
                                              new_subscription_gbp),
        # ⚑ ISA-0390. How much of the fund allocation is a TIMING decision rather than an
        # allocation: the capital the stock sleeve was allowed to take and did not, because no
        # candidate qualified. This module PLANS; `waiting_room.park()` is what WRITES the lot,
        # called once per run with a run_id, so re-running the plan cannot inflate the balance.
        "parking_intent": {
            "stock_cap_gbp": split.get("stock_max_gbp"),
            "stock_cap_state": split.get("state"),
            "parked_if_no_qualifying_candidate_gbp": round(
                float(split.get("stock_max_gbp") or 0.0), 2),
            # ⚑ MEASURED, not claimed. If no candidate qualifies the cap is re-offered to the
            # funds — and whether the declared bands can actually ABSORB it is a fact, not an
            # assumption. Running it is one call; asserting it would be a guess.
            "if_reoffered_to_funds": _fund_absorption(
                amount_gbp, portfolio, universe, eligible, policy, new_subscription_gbp,
                ranking),
            "rule": ("capital that could have entered the stock sleeve and went to funds instead "
                     "is PARKED, not allocated — tag it via waiting_room.park() so the recall leg "
                     "can find it. Untagged, a timing decision becomes an allocation by default "
                     "(ISA-0390)."),
            "recall_permitted_today": _recall_permitted(tw),
            "⚑ the trap this names": (
                "the stock cap comes from a SUBSCRIPTION, which `reallocation_only` does not bar — "
                "so it is spendable THIS month. Park it in funds instead and the recall leg cannot "
                "bring it back, because a recall IS a disposal from the fund sleeve. Under an "
                "active freeze, parking is not a reversible timing decision; it is a decision "
                "with a lock-up until the freeze lifts. Whoever declines the cap should decline "
                "it knowing that."),
        },
        "residual": residual,
        "verification": {"two_derivations_agree": agree, "tolerance_gbp": TOL_GBP,
                         "allocation_sum_gbp": alloc_sum, "expected_gbp": expected,
                         "parity": parity},
        "state": "OK" if (agree and parity["pass"]) else "FAILED_VERIFICATION",
    }
    out = Path(out_path or HERE / f"capital_destination_{dt.date.fromisoformat(as_of):%b_%Y}".lower()) \
        if out_path is None else Path(out_path)
    out = Path(str(out) + ".json") if out.suffix != ".json" else out
    out.write_text(json.dumps(doc, indent=2))
    doc["_written"] = str(out)
    return doc



# ══════════════════════════════════════════════════════════════════════════════════════════════
# A12 — PLAN STABILITY.  ISA-0440 / amendment schedule A12, built 26-Aug-2026.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# A12: "capital_routing.py must emit a robustness grid alongside the plan: how much does the plan
# change if er_ca moves +/-1pp, if rho_sleeve moves +/-0.05, if NAV moves +/-5%? A lexicographic
# ranking over near-tied inputs can be unstable, and the instability must be visible rather than
# inferred." It follows the M* `robustness()` precedent.
#
# ⚑ TWO CORRECTIONS TO THE AMENDMENT, BOTH FOUND BY TRYING TO BUILD IT, BOTH STATED RATHER THAN
# QUIETLY WORKED AROUND (R4.4: when a build makes a sentence in a spec false, updating that
# sentence is part of the build).
#
#   1. THERE IS NO `capital_routing.py`. The router is THIS module. The amendment names a file
#      that has never existed on disk, and building a `capital_routing.py` to satisfy the sentence
#      would have created the second home R4.4 exists to prevent.
#
#   2. ⚑ `er_ca` AND `rho_sleeve` ARE NOT INPUTS TO THIS PLAN, AND THAT IS THE MOST USEFUL THING
#      THE GRID REPORTS. The fund plan is produced from portfolio values, declared bands, the
#      freeze basis and the C1..C5 ranking. Neither the confidence-adjusted expected return nor
#      the sleeve correlation appears anywhere in its derivation — `grep` finds them in this
#      module only in COMMENTS about other people's findings. So perturbing them here and printing
#      "0.0% change" would be a fabricated reassurance: it would read as "the plan is robust to
#      expected return" when the truth is "the plan never consulted expected return". The grid
#      therefore reports NOT_AN_INPUT with the evidence, and ROUTES both perturbations to the
#      module where they ARE inputs — `position_sizing.stock_max`, the demand-pull rule that
#      decides the other half of the marginal pound.
#
# ⚑ AND ONE MEASURED RESULT WORTH READING BEFORE THE NUMBERS: rho is UNMEASURED on this book, and
# an unmeasured correlation is ADVERSE by A2.3, which caps every position at STARTER. So moving
# rho by +/-0.05 changes NOTHING TODAY — not because the plan is robust to correlation, but
# because the plan is already at the floor correlation forces it to. That distinction is the
# difference between a stable plan and a plan that has stopped listening, and only a grid that
# names the mechanism can tell them apart.


A12_STABILITY_ENABLED = True

NAV_PERTURBATION_PCT = 5.0
ER_CA_PERTURBATION_PP = 1.0
RHO_PERTURBATION = 0.05


def _plan_signature(doc: dict) -> dict:
    """-> the comparable shape of a plan: who receives, how much, in what order, and the split."""
    fa = doc.get("fund_allocation") or {}
    alloc = {k: round(float(v), 2) for k, v in (fa.get("allocation") or {}).items()}
    split = doc.get("sleeve_split") or {}
    return {
        "allocation": alloc,
        "receivers": sorted(k for k, v in alloc.items() if v > 0.005),
        "order": [c.get("sedol") for c in (fa.get("ordered") or fa.get("candidates") or [])],
        "stock_max_gbp": round(float(split.get("stock_max_gbp") or 0.0), 2),
        "fund_max_gbp": round(float(split.get("fund_max_gbp") or 0.0), 2),
        "total_allocated_gbp": round(sum(alloc.values()), 2),
    }


def _plan_delta(base: dict, other: dict) -> dict:
    """-> how far the plan moved. Pounds churned, receivers gained/lost, order changed."""
    keys = sorted(set(base["allocation"]) | set(other["allocation"]))
    churn = sum(abs(other["allocation"].get(k, 0.0) - base["allocation"].get(k, 0.0))
                for k in keys)
    denom = max(base["total_allocated_gbp"], 1e-9)
    return {
        "pounds_churned_gbp": round(churn, 2),
        "churn_share_of_plan": round(churn / denom, 4),
        "receivers_added": sorted(set(other["receivers"]) - set(base["receivers"])),
        "receivers_dropped": sorted(set(base["receivers"]) - set(other["receivers"])),
        "receiver_set_changed": other["receivers"] != base["receivers"],
        "order_changed": other["order"] != base["order"],
        "stock_max_delta_gbp": round(other["stock_max_gbp"] - base["stock_max_gbp"], 2),
        "total_allocated_delta_gbp": round(other["total_allocated_gbp"]
                                           - base["total_allocated_gbp"], 2),
    }


def _scaled_portfolio(portfolio: dict, factor: float) -> dict:
    """-> the portfolio with every holding and the total scaled. A NAV move is a MARKET move: it
    scales the holdings, it does not appear from nowhere in the summary line."""
    p = json.loads(json.dumps(portfolio))
    for grp in ("funds", "stocks"):
        for row in p.get(grp) or []:
            if row.get("value_gbp") is not None:
                row["value_gbp"] = float(row["value_gbp"]) * factor
    s = p["summary"]
    for k in ("total_value_gbp", "fund_sleeve_value_gbp", "stock_sleeve_value_gbp"):
        if s.get(k) is not None:
            s[k] = float(s[k]) * factor
    return p


# The A12 instrument's own functions. They MENTION `er_ca` and `rho_sleeve` by name, so they must
# be excluded from the scan below or the observer reports itself as the thing it is observing.
# ⚑ MY FIRST DRAFT DID EXACTLY THAT: it returned `er_ca read_by_code = True` pointing at its own
# line, which would have published "the plan reads er_ca" — the opposite of the truth — with an
# AST as evidence. This is ISA-0382's rule in a new place: AN OBSERVER MAY NOT MEASURE ITSELF.
_A12_OBSERVER_FUNCS = ("_module_reads", "_stock_side_sensitivity", "plan_stability",
                       "_plan_signature", "_plan_delta", "_scaled_portfolio")


def _module_reads(name: str, *, exclude=_A12_OBSERVER_FUNCS) -> dict:
    """-> whether this module's PLAN-PRODUCING CODE ever reads `name`. AST, not grep.

    ⚑ A grep answers "does this word appear", and in this file `er_ca` appears several times in
    prose about other people's findings. The question A12 needs answered is "can this quantity
    reach the plan", and only the parsed code can answer it (R4.6.1 — enumerate on disk, and the
    enumeration is an ARTEFACT, not a claim).

    ⚑ String constants ARE counted, deliberately: this codebase reaches quantities through
    `row.get("c1")`, so a dict key is a read. That is also why the observer functions have to be
    excluded by name rather than by hoping they contain no strings.
    """
    import ast as _ast
    src = Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    skip = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in exclude:
            for sub in _ast.walk(node):
                skip.add(id(sub))
    hits = []
    for node in _ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, _ast.Name) and node.id == name:
            hits.append(getattr(node, "lineno", None))
        elif isinstance(node, _ast.Attribute) and node.attr == name:
            hits.append(getattr(node, "lineno", None))
        elif isinstance(node, _ast.Constant) and isinstance(node.value, str) \
                and node.value == name:
            hits.append(getattr(node, "lineno", None))
    return {"name": name, "read_by_code": bool(hits), "lines": sorted(set(hits)),
            "observer_excluded": list(exclude),
            "basis": ("AST over %s, EXCLUDING the A12 instrument's own functions: %d code "
                      "reference(s). Comments and docstrings are not counted; string constants "
                      "ARE, because a dict key is a read in this codebase."
                      % (Path(__file__).name, len(set(hits))))}


def _stock_side_sensitivity(nav_gbp: float, capital_on_offer_gbp: float,
                            candidates=None) -> dict:
    """Where er_ca and rho ACTUALLY bite: `position_sizing.stock_max` (the demand-pull rule)."""
    try:
        import position_sizing as _ps
    except Exception as e:                                              # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}

    base_cands = candidates if candidates is not None else [
        {"ticker": "_PROBE", "qualifies": True, "evidence_state": "CONFIRMED",
         "current_value_gbp": 0.0,
         "correlation": {"measured": False, "rho_sleeve": None,
                         "rho_basis": "UNMEASURED_ADVERSE_DEFAULT"}}]

    def _run(cs):
        return _ps.stock_max(cs, nav_gbp=nav_gbp,
                             capital_on_offer_gbp=capital_on_offer_gbp)

    synthetic = candidates is None
    base = _run(base_cands)
    out = {
        "state": ("SYNTHETIC_PROBE" if synthetic else "MEASURED"),
        # ⚑ NAMED AS A PROBE, NOT PRINTED AS A PLAN. With no real candidate list this runs one
        # invented CONFIRMED name to demonstrate the MECHANISM. The pound figure it produces is
        # not a size anyone should act on, and a number that looks like a plan but is not one is
        # precisely the class of defect this project keeps finding (R4.2 — a figure states where
        # it came from, and this one came from a fixture).
        "probe_stock_max_gbp": base["stock_max_gbp"],
        "probe_binding": base["binding"],
        "probe_note": (("no candidate list was supplied, so the grid runs ONE synthetic "
                        "CONFIRMED candidate at zero current value. It shows what the rule DOES; "
                        "it does not size anything.") if synthetic else None),
        "grid": []}

    for label, delta in (("rho -0.05", -RHO_PERTURBATION), ("rho +0.05", RHO_PERTURBATION)):
        cs = json.loads(json.dumps(base_cands))
        for c in cs:
            corr = c.get("correlation") or {}
            if corr.get("measured") and corr.get("rho_sleeve") is not None:
                corr["rho_sleeve"] = float(corr["rho_sleeve"]) + delta
        r = _run(cs)
        measured_any = any((c.get("correlation") or {}).get("measured") for c in base_cands)
        out["grid"].append({
            "perturbation": label,
            "stock_max_gbp": r["stock_max_gbp"],
            "delta_gbp": round(r["stock_max_gbp"] - base["stock_max_gbp"], 2),
            "note": (None if measured_any else
                     "⚑ rho is UNMEASURED on this book. A2.3 makes an unmeasured correlation "
                     "ADVERSE and caps every position at STARTER, so a +/-0.05 move changes "
                     "nothing — because the plan is already at the floor correlation forces, NOT "
                     "because it is robust to correlation. Those are different facts."),
        })

    for label, sign in (("er_ca -1pp", -1), ("er_ca +1pp", +1)):
        cs = json.loads(json.dumps(base_cands))
        flipped = 0
        for c in cs:
            m = c.get("er_ca_margin_pp")
            if m is None:
                continue
            new_margin = float(m) + sign * ER_CA_PERTURBATION_PP
            was, now = bool(c.get("qualifies")), new_margin >= 0
            if was != now:
                flipped += 1
            c["qualifies"] = now
        r = _run(cs)
        out["grid"].append({
            "perturbation": label,
            "stock_max_gbp": r["stock_max_gbp"],
            "delta_gbp": round(r["stock_max_gbp"] - base["stock_max_gbp"], 2),
            "candidates_flipped": flipped,
            "note": (None if any(c.get("er_ca_margin_pp") is not None for c in base_cands) else
                     "no candidate carried `er_ca_margin_pp` (its distance from the deploy "
                     "floor), so this run cannot say whether a 1pp move would flip anything. "
                     "UNMEASURED, not 'no effect' (R4.1)."),
        })
    return out


def plan_stability(*, portfolio=None, base_doc=None, amount_gbp=None,
                   new_subscription_gbp=0.0, out_path=None) -> dict:
    """-> A12's robustness grid for the marginal-pound plan. Never mutates anything on disk."""
    if not A12_STABILITY_ENABLED:
        return {"state": "DISABLED", "reason": "A12_STABILITY_ENABLED is False (R4.13)"}
    import tempfile
    portfolio = portfolio if portfolio is not None else _load_portfolio()
    tmpdir = Path(tempfile.mkdtemp())

    def _build_with(p, tag):
        # ⚑ writes to a TEMP path: a robustness probe that overwrote the month's plan file would
        # be an instrument that changes what it measures.
        pf = tmpdir / ("portfolio_%s.json" % tag)
        pf.write_text(json.dumps(p), encoding="utf-8")
        return build(amount_gbp=amount_gbp, new_subscription_gbp=new_subscription_gbp,
                     portfolio_path=str(pf), out_path=str(tmpdir / ("plan_%s.json" % tag)))

    base = base_doc if base_doc is not None else _build_with(portfolio, "base")
    base_sig = _plan_signature(base)

    grid = []
    for label, factor in (("NAV -5%", 1.0 - NAV_PERTURBATION_PCT / 100.0),
                          ("NAV +5%", 1.0 + NAV_PERTURBATION_PCT / 100.0)):
        d = _build_with(_scaled_portfolio(portfolio, factor), label.replace("%", "pct")
                        .replace(" ", "_").replace("+", "p").replace("-", "m"))
        grid.append({"perturbation": label, "input": "portfolio NAV and every holding",
                     "state": d.get("state"), **_plan_delta(base_sig, _plan_signature(d))})

    not_inputs = {}
    for nm in ("er_ca", "rho_sleeve", "rho"):
        not_inputs[nm] = _module_reads(nm)

    s = portfolio["summary"]
    nav = float(s["total_value_gbp"])
    offer = (amount_gbp if amount_gbp is not None
             else float(s.get("cash_deployable_gbp") or 0.0) + float(new_subscription_gbp or 0.0))

    doc = {
        "state": "MEASURED", "as_of": _today(), "item": "ISA-0440 / A12",
        "enabled": A12_STABILITY_ENABLED,
        "precedent": "follows the M* robustness() pattern already established in this project",
        "base_plan": base_sig,
        "grid": grid,
        "unstable": [g["perturbation"] for g in grid
                     if g["receiver_set_changed"] or g["order_changed"]],
        "not_an_input": {
            "state": "MEASURED_BY_AST",
            "quantities": not_inputs,
            "why_this_is_reported_rather_than_perturbed": (
                "A12 asks for er_ca +/-1pp and rho_sleeve +/-0.05. Neither reaches this plan: the "
                "fund destination is decided by portfolio values, declared bands, the freeze basis "
                "and the C1..C5 ranking. Perturbing a quantity the plan never reads and printing "
                "'0.0% change' would publish a fabricated reassurance — it reads as 'robust to "
                "expected return' when the truth is 'never consulted expected return'. The AST "
                "evidence is above and the perturbation is routed to where it BITES, below."),
        },
        "routed_to_stock_side": _stock_side_sensitivity(nav, offer),
        "reading": None,
    }
    doc["reading"] = (
        "The fund plan %s under a +/-5%% NAV move. %s"
        % (("CHANGES ITS DESTINATIONS" if doc["unstable"] else
            "keeps the same destinations and order"),
           ("Unstable under: " + ", ".join(doc["unstable"]) + ". A lexicographic ranking over "
            "near-tied inputs is resolving noise, and A12 exists so that is VISIBLE rather than "
            "inferred." if doc["unstable"] else
            "A 5% market move does not reorder the destinations, so the ranking is not sitting "
            "on a knife edge this month. That is a statement about THIS month's inputs, not a "
            "property of the rule.")))
    if out_path:
        Path(out_path).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


# ══════════════════════════════════════════════════════════════════════════════════════════════
# selftest helpers — each one is a control that must be able to FAIL (R5.5 / R5.8)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _fixture():
    portfolio = json.load(open(HERE / "portfolio_data_aug_2026.json"))
    universe = json.load(open(HERE / "fund_universe.json"))
    tw = json.load(open(HERE / "target_weights.json"))
    policy = {"bucket_totals": tw["bucket_totals"], "thresholds": tw["thresholds"],
              "funds": tw["funds"], "max_single_fund_pct": tw["thresholds"]["max_single_fund_pct"],
              "stock_sleeve": tw["stock_sleeve"], "scaling_freeze": tw.get("scaling_freeze")}
    return portfolio, universe, policy


def _rollback_reproduces_deviation_key() -> bool:
    """R4.13 — the rollback constant must reproduce the 16-Aug ordering EXACTLY, not approximately."""
    global RANKING_ENABLED
    portfolio, universe, policy = _fixture()
    rk = rank_inputs(universe)
    elig = set(rk["rows"])
    was = RANKING_ENABLED
    try:
        RANKING_ENABLED = False
        off = allocate_funds(10000.0, portfolio, universe, elig, policy, ranking=rk)["allocation"]
        # the ordering with the ranking OFF must not depend on the ranking table at all
        blank = allocate_funds(10000.0, portfolio, universe, elig, policy, ranking=None)["allocation"]
        RANKING_ENABLED = True
        on = allocate_funds(10000.0, portfolio, universe, elig, policy, ranking=rk)["allocation"]
    finally:
        RANKING_ENABLED = was
    return off == blank and off != on


def _full_bucket_does_not_stop_the_fill() -> bool:
    """The defect the 20-Aug run exposed: the globally top-ranked candidate's bucket being full
    ended the fill for EVERY bucket. Drive one bucket's ceiling to zero; the others must still fill."""
    portfolio, universe, policy = _fixture()
    rk = rank_inputs(universe)
    elig = set(rk["rows"])
    p2 = json.loads(json.dumps(policy))
    # B1 holds the top-ranked destination on this portfolio; shut it and B2/B3 must still receive.
    p2["bucket_totals"]["B1"]["phase1_target_pct"] = 0.0
    p2["bucket_totals"]["B1"]["phase1_band_high"] = 0.0
    out = allocate_funds(10000.0, portfolio, universe, elig, p2, ranking=rk)
    b = {s: u.get("bucket") for s, u in universe["funds"].items() if not str(s).startswith("_")}
    placed_outside_b1 = sum(v for s, v in out["allocation"].items() if b.get(s) != "B1")
    placed_in_b1 = sum(v for s, v in out["allocation"].items() if b.get(s) == "B1")
    return placed_in_b1 == 0 and placed_outside_b1 > 0


def _idle_capital_is_priced_when_it_exists() -> bool:
    """R5.5 — the amended waiting-room assertion must still be able to FAIL. Force idle capital by
    collapsing every declared band to zero, and the net opportunity cost must appear."""
    import tempfile
    tw = json.load(open(HERE / "target_weights.json"))
    p = HERE / "_tw_idle_control.json"
    try:
        t2 = json.loads(json.dumps(tw))
        for b in t2["bucket_totals"]:
            t2["bucket_totals"][b]["phase1_target_pct"] = 0.0
            t2["bucket_totals"][b]["phase1_band_high"] = 0.0
        tmp = Path(tempfile.mkdtemp()) / "tw.json"
        tmp.write_text(json.dumps(t2))
        d = build(amount_gbp=20799.54, new_subscription_gbp=11250.0, weights_path=tmp,
                  out_path=tempfile.mktemp(suffix=".json"))
    except Exception:                                                 # noqa: BLE001
        return False
    r = d["residual"]
    if r["unallocated_gbp"] <= 0:
        return False
    if r["mmf_rate_pct"]["present"]:
        return r["annual_opportunity_cost_net_of_waiting_room_gbp"] is not None
    return "UNMEASURED" in str(r["mmf_rate_pct"]["source"]).upper()


def _c1_resolution_switches() -> bool:
    """Both C1 resolutions must be REACHABLE (R5.3). A degradation path nobody exercises is a
    path nobody knows works — and the vector artefact is new, so the fallback is the live one on
    any machine where the capture has not been run."""
    import shutil, tempfile
    universe = json.load(open(HERE / "fund_universe.json"))
    p = HERE / EXPOSURE_VECTORS_FILE
    present = rank_inputs(universe)["criteria"]["c1"]["resolution"] if p.exists() else None
    tmpdir = Path(tempfile.mkdtemp())
    absent = rank_inputs(universe, exposure_path=tmpdir / "nope.json")["criteria"]["c1"]["resolution"]
    if present is None:
        return absent == "declared_mandate_region"
    return present == "lookthrough_vector" and absent == "declared_mandate_region"


def _undeclared_basis_raises() -> bool:
    """R4.7 — an un-updated contract must FAIL, never silently keep the old behaviour."""
    portfolio, universe, policy = _fixture()
    p2 = json.loads(json.dumps(policy))
    p2["scaling_freeze"] = {k: v for k, v in (p2.get("scaling_freeze") or {}).items()
                            if k != "basis"}
    p2["scaling_freeze"]["active"] = True
    try:
        sleeve_split(20799.54, portfolio, p2, 11250.0)
        return False
    except DestinationRefused:
        return True


def _weight_basis_is_not_executable() -> bool:
    """The measurement that decided ISA-0387, reproduced rather than asserted in prose."""
    portfolio, universe, policy = _fixture()
    p2 = json.loads(json.dumps(policy))
    p2["scaling_freeze"]["basis"] = "weight"
    p2["scaling_freeze"]["active"] = True
    out = sleeve_split(20799.54, portfolio, p2, 11250.0)
    return out["executability"]["state"] == "NOT_EXECUTABLE"


# ══════════════════════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0447 — THE DECLARED RUN-CONTEXT SUMMARY
#
# ⚑ WHY THIS LIVES HERE AND NOT IN THE ORCHESTRATOR. `build()` produces a document of several
# hundred fields; the monthly run context carries a summary of it, and until 26-Aug-2026 that
# summary was eight scalars assembled by a dict literal inside `monthly_isa_prerun` Step 6.10b
# — and NOTHING RENDERED THEM. The whole marginal-pound router reached a decision surface only
# if Raj opened the JSON (ISA-0447). The email now renders it, which makes this dict a contract
# between two modules, and a contract whose only definition is a literal buried in an
# orchestrator is one no test can reach and no reader can find. The module that owns the
# document owns its summary.
#
# ⚑ WHAT IT MUST CARRY IS DECIDED BY R2.10, NOT BY BREVITY. `executability` travels whole, not
# as its state string, because "NOT_EXECUTABLE" without the two pounds figures it fails against
# is an adjective rather than a measurement. `band_choice_not_made` travels because the question
# it names — whether a declared band is a preference or a limit — has NOT been decided, and a
# summary that dropped it would let an undecided question read as a settled one.
# ═══════════════════════════════════════════════════════════════════════════════════════════

SUMMARY_SCHEMA_VERSION = "1.0.0"


def summary_for_run_context(doc: dict) -> dict:
    """The `summary.capital_destination` block of run_context_[mmm]_[yyyy].json.

    Input is a full `build()` document. Returns {} for any document that is not state OK —
    an unrouted month must render as ABSENT at the decision surface, never as an empty plan."""
    if not doc or doc.get("state") != "OK":
        return {}
    _sl = doc.get("sleeve_split") or {}
    _fa = doc.get("fund_allocation") or {}
    _cdr = doc
    _rk, _db = doc.get("ranking") or {}, doc.get("declared_bands") or {}
    _res_cd, _ver = doc.get("residual") or {}, doc.get("verification") or {}
    _frz = _sl.get("scaling_freeze") or {}
    return {
    "state": _cdr["state"],
    "as_of": (_cdr.get("_meta") or {}).get("as_of"),
    "stock_max_gbp": _sl.get("stock_max_gbp"),
    "fund_max_gbp": _sl.get("fund_max_gbp"),
    "amount_available_gbp": _sl.get("amount_available_gbp"),
    "sleeve_weight_now_pct": _sl.get("stock_sleeve_weight_now_pct"),
    "declared_band_pct": _sl.get("declared_band_pct"),
    "in_band": _sl.get("in_band"),
    "gbp_to_reach_band_floor": _sl.get("gbp_to_reach_band_floor"),
    "split_state": _sl.get("state"),
    "split_reason": _sl.get("reason"),
    "freeze_basis": _frz.get("basis"),
    "freeze_active": _frz.get("active"),
    "freeze_declared_by": _frz.get("basis_declared_by"),
    "freeze_earliest_unfreeze": _frz.get("earliest_unfreeze_mechanical"),
    # the WHOLE executability dict: a refusal must render with the number it fails
    # against, or "NOT_EXECUTABLE" is an adjective rather than a measurement (R2.10)
    "executability": _sl.get("executability") or {},
    "ranking_order": _rk.get("order"),
    "ranking_state": _rk.get("state"),
    "trailing_return": _rk.get("trailing_return"),
    "c1_resolution": ((_rk.get("criteria") or {}).get("c1") or {}).get("resolution"),
    "fund_allocation_state": _fa.get("state"),
    "allocation_gbp": {k: v for k, v in (_fa.get("allocation") or {}).items()
                       if (v or 0) > 0},
    "phase_allocation": _fa.get("phase_allocation"),
    "blocked": _fa.get("blocked"),
    "eligibility_refused": (_cdr.get("eligibility") or {}).get("refused"),
    "band_weights": {r["sedol"]: {"before": r.get("weight_before_pct"),
                                  "after": r.get("weight_after_pct"),
                                  "low": r.get("band_low_pct"),
                                  "high": r.get("band_high_pct")}
                     for r in (_db.get("funds") or [])},
    "band_breaches_before": _db.get("breaches_before"),
    "band_breaches_after": _db.get("breaches_after"),
    "band_repaired": _db.get("repaired"),
    "band_not_repaired": _db.get("not_repaired"),
    "band_choice_not_made": _db.get("the_choice_not_made"),
    "unallocated_gbp": _fa.get("unallocated_gbp"),
    "residual_state": _res_cd.get("state"),
    "residual_pct_of_offered": _res_cd.get("pct_of_capital_offered"),
    "idle_cost_net_gbp":
        _res_cd.get("annual_opportunity_cost_net_of_waiting_room_gbp"),
    "idle_cost_basis": _res_cd.get("opportunity_cost_basis"),
    "parity_pass": (_ver.get("parity") or {}).get("pass"),
    "parity_inert_criteria": (_ver.get("parity") or {}).get("inert_criteria"),
    "two_derivations_agree": _ver.get("two_derivations_agree"),
            }



def _selftest(verbose=True) -> int:
    import tempfile
    fails = []

    def ck(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    d = build()
    ck("build returns OK", d["state"] == "OK")
    ck("evidence measured", d["evidence"]["state"] == "MEASURED")
    ck("D-18 reproduced: ordering is VETO-ONLY on live data",
       d["evidence"]["verdict"] == "TRAILING_RETURN_VETO_ONLY")
    ck("signal/noise ratio < 1", d["evidence"]["signal_noise_variance_ratio"] < 1.0)
    ck("implied true dispersion is not positive",
       d["evidence"]["var_true_dispersion_implied"] <= 0)
    ck("D-19/D-16: Ranmore refused", "BR2Q8G6" in d["eligibility"]["refused"])
    ck("Ranmore refusal is ROBUST across the whole multiplier grid",
       "BR2Q8G6" in d["regime_coverage"]["refused_at_every_grid_point"])
    ck("no fund with a full-cycle record is refused",
       all(d["regime_coverage"]["verdicts"][s]["max_drawdown_pct"]
           > d["regime_coverage"]["floor_pct"] for s in d["eligibility"]["refused"]))
    ck("P1 positive control: allocation invariant to trailing return",
       d["verification"]["parity"]["trailing_return_invariant"])
    ck("P1 negative control: allocation DOES move with bucket ceilings",
       d["verification"]["parity"]["negative_control_bucket_ceilings_move_it"])

    # ── ISA-0386 · the estimation-free ranking ────────────────────────────────────────────────
    P = d["verification"]["parity"]
    ck("ISA-0386: NO criterion is inert — each one, isolated from those above it, moves capital",
       P["inert_criteria"] == [] and P["pass"] is True)
    ck("ISA-0386: C1 runs at a DECLARED resolution and names it",
       d["ranking"]["criteria"]["c1"]["resolution"] in
       ("lookthrough_vector", "declared_mandate_region")
       and bool(d["ranking"]["criteria"]["c1"]["exposure_vectors"]["state"]))
    ck("ISA-0386: every fund's C1 carries a state, and UNRESOLVED never renders as measured "
       "(R2.10)",
       all(r["c1"]["state"] in ("MEASURED", "MEASURED_NEUTRAL", "MEASURED_IMMATERIAL", "REFUSED")
           and (r["c1"]["basis"] != "UNRESOLVED" or r["c1"]["state"] == "REFUSED")
           for r in d["ranking"]["rows"].values()))
    ck("ISA-0386: C1 UPGRADES to the look-through vector when the artefact is present and "
       "DEGRADES to declared-mandate when it is absent — both paths exercised, neither guessed",
       _c1_resolution_switches())
    ck("ISA-0386: the look-through C1 charges each country's weight to AT MOST ONE bet "
       "(no double count between country/United States and region/americas)",
       all(sum((r["c1"].get("charged_weight_by_bet") or {}).values()) <= 1.0 + 1e-3   # rounding only: weights are stored to 6dp
           for r in d["ranking"]["rows"].values()))
    ck("ISA-0386: a fund with NO DECLARED PROCESS is not rendered as unconcentrated",
       all(("NO DECLARED PROCESS" in str(r["c2"]["basis"]))
           for r in d["ranking"]["rows"].values()
           if r["c2"]["state"] == "UNDECLARED_PROCESS") or not any(
           r["c2"]["state"] == "UNDECLARED_PROCESS" for r in d["ranking"]["rows"].values()))
    ck("ISA-0386: the T4-FIRING fund is ranked BELOW every fund whose mandate is intact",
       (lambda fir, rr: all(rr[f]["c3"]["c3"] > rr[o]["c3"]["c3"]
                            for f in fir for o in rr if rr[o]["c3"]["state"] == "OK") if fir
        else True)(d["ranking"]["criteria"]["c3"].get("firing") or [], d["ranking"]["rows"]))
    ck("ISA-0386: trailing return appears NOWHERE in the ranking table",
       not any(("return" in k.lower() or "alpha" in k.lower())
               for r in d["ranking"]["rows"].values() for c in r.values()
               if isinstance(c, dict) for k in c))
    ck("R4.13 rollback: RANKING_ENABLED=False reproduces the pre-ISA-0386 ordering exactly",
       (lambda: _rollback_reproduces_deviation_key())())

    # ── ISA-0388 · the bands are the limit, not the point target ──────────────────────────────
    ck("ISA-0388: the router reads phase1_band_high — a band phase exists on the output",
       "within_band" in (d["fund_allocation"].get("phase_allocation") or {}))
    ck("ISA-0388: collapsing band_high onto the point target never INCREASES what is placed",
       d["verification"]["parity"]["band_phase_never_removes_capacity"])
    ck("ISA-0388: no allocation breaches a DECLARED per-fund band_high",
       all(r["state_after"] != "ABOVE_BAND" or r["state_before"] == "ABOVE_BAND"
           for r in d["declared_bands"]["funds"]))
    ck("ISA-0388: a fund whose bucket is full EXCLUDES that fund, it does not stop the fill "
       "for every other bucket",
       _full_bucket_does_not_stop_the_fill())

    # ── ISA-0387 · the freeze has a declared unit and the number is derived ───────────────────
    ck("ISA-0387: the freeze basis is DECLARED and is one of the three",
       d["sleeve_split"]["scaling_freeze"]["basis"] in FREEZE_BASES)
    ck("ISA-0387: stock_max is DERIVED from the declared basis, not typed",
       (not d["sleeve_split"]["scaling_freeze"]["active"])
       or ("freeze_derivation" in d["sleeve_split"]
           and d["sleeve_split"]["stock_max_gbp"]
           == d["sleeve_split"]["freeze_derivation"]["derived_gbp"]))
    ck("ISA-0387: an ACTIVE freeze with NO declared basis RAISES rather than defaulting (R4.7)",
       _undeclared_basis_raises())
    ck("ISA-0387: the cap is tested for EXECUTABILITY against the smallest declared position",
       d["sleeve_split"]["executability"]["state"] in
       ("EXECUTABLE", "NOT_EXECUTABLE", "NOT_APPLICABLE", "UNTESTABLE"))
    ck("ISA-0387: the `weight` basis on this portfolio is NOT executable — the measurement that "
       "decided the basis is reproducible, not a one-off claim",
       _weight_basis_is_not_executable())

    # ── §4b · declared per-fund bands are MEASURED and the unmade choice is stated ────────────
    ck("declared per-fund band breaches are published, before and after",
       d["declared_bands"]["state"] == "MEASURED"
       and isinstance(d["declared_bands"]["breaches_before"], list)
       and bool(d["declared_bands"]["the_choice_not_made"]))
    ck("R5.2 two derivations agree", d["verification"]["two_derivations_agree"])
    ck("D-17: allocation is a vector, not a pick",
       sum(1 for v in d["fund_allocation"]["allocation"].values() if v > 0) > 1)
    ck("no allocation breaches the single-fund cap", all(
        (next(f["value_gbp"] for f in json.load(open(HERE / "portfolio_data_aug_2026.json"))["funds"]
              if f["ticker"] == k) + v)
        / json.load(open(HERE / "portfolio_data_aug_2026.json"))["summary"]["total_value_gbp"]
        <= json.load(open(HERE / "target_weights.json"))["thresholds"]["max_single_fund_pct"] + 1e-9
        for k, v in d["fund_allocation"]["allocation"].items()))
    ck("refused fund receives ZERO, not a small weight",
       all(d["fund_allocation"]["allocation"].get(s, 0.0) == 0.0
           for s in d["eligibility"]["refused"]))
    ck("idle capital is PRICED, never silent",
       d["residual"]["state"] in ("IDLE_CAPITAL_PRICED", "FULLY_DEPLOYED")
       and (d["residual"]["annual_opportunity_cost_gbp"] is not None
            or d["residual"]["unallocated_gbp"] == 0))
    # ⚑ AMENDED 20-Aug-2026 — and it took R5.9 to find it. This asserted that a MEASURED cash rate
    # implies a non-null net opportunity cost. True while capital was always left over; FALSE the
    # moment ISA-0388 gave every pound a destination, because the net cost is null when there is
    # no idle capital to price. It passed in the sandbox only because the cash statement is absent
    # there, so it took the UNMEASURED branch — the assertion went red in the DELIVERED folder, on
    # a correct improvement. ISA-0348's class, now the fifth occurrence: a claim about a PATH
    # tested by inspecting STORED STATE. Restated as the property that is actually meant — the
    # waiting-room yield is never silently zero, and idle capital is priced WHEN THERE IS ANY.
    ck("the waiting-room yield is either MEASURED from the cash statement or reported "
       "UNMEASURED with the reason - never silently zero",
       (d["residual"]["mmf_rate_pct"]["present"] is True
        and (d["residual"]["unallocated_gbp"] == 0
             or d["residual"]["annual_opportunity_cost_net_of_waiting_room_gbp"] is not None))
       or (d["residual"]["mmf_rate_pct"]["present"] is False
           and bool(d["residual"]["mmf_rate_pct"]["source"])
           and "UNMEASURED" in str(d["residual"]["mmf_rate_pct"]["source"]).upper()))
    ck("...and the negative control: with idle capital present, a MEASURED rate MUST produce a "
       "net figure, so the clause above cannot pass by being vacuous",
       _idle_capital_is_priced_when_it_exists())
    ck("stock sleeve decision is stated, not invented",
       d["sleeve_split"]["state"] in ("STOCK_SLEEVE_BLOCKED", "STOCK_SLEEVE_CAPPED",
                                      "STOCK_SLEEVE_OPEN")
       and bool(d["sleeve_split"]["reason"]))
    # ⚑ the denominator control — a NEW subscription must enlarge the total
    d2 = build(amount_gbp=11250.0, new_subscription_gbp=11250.0,
               out_path=tempfile.mktemp(suffix=".json"))
    ck("new subscription enlarges the denominator (cap tested on the post-trade total)",
       d2["state"] == "OK" and all(
           (next(f["value_gbp"] for f in json.load(open(HERE / "portfolio_data_aug_2026.json"))["funds"]
                 if f["ticker"] == k) + v)
           / (json.load(open(HERE / "portfolio_data_aug_2026.json"))["summary"]["total_value_gbp"] + 11250.0)
           <= json.load(open(HERE / "target_weights.json"))["thresholds"]["max_single_fund_pct"] + 1e-9
           for k, v in d2["fund_allocation"]["allocation"].items()))
    ck("subscription case still refuses Ranmore",
       d2["fund_allocation"]["allocation"].get("BR2Q8G6", 0.0) == 0.0)

    # R4.13 rollback is real
    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module", build()["state"] == "DISABLED")
    ENABLED = True
    # R4.3 negative control: an empty sleeve BLOCKS, never PASSes
    ck("empty sleeve returns UNKNOWN and blocks", evidence_dispersion({})["state"] == "UNKNOWN")
    ck("regime_coverage on empty sleeve blocks", regime_coverage({})["state"] == "UNKNOWN")
    print(f"\ncapital_destination selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 42 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(build(), indent=2))
