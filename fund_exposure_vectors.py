#!/usr/bin/env python3
"""
fund_exposure_vectors.py — ISA-0392.  THE PER-FUND EXPOSURE VECTOR.  Built 20-Aug-2026.

⚑ WHY THIS EXISTS AS ITS OWN MODULE AND ITS OWN REGISTER ITEM.
Three items are blocked on one missing input and NONE of them owned it:

    ISA-0333  (CRITICAL)  strategic allocation — the `attribution` half cannot be wired
    ISA-0160  (CRITICAL)  same input
    ISA-0328  (HIGH)      same input
    ISA-0386  (HIGH)      C1 runs at declared-mandate resolution until this lands

The SAA spec asserted the vector was "already on disk, per fund, monthly". It is not, and it never
was: the AJ Bell X-Ray publishes exposure at PORTFOLIO level only, and the fund KIDs in the ISA
folder carry no geographic table at all. The acquisition was buried inside three corrective actions
and therefore had no owner, no cadence and no contract — which is exactly how an input goes missing
for months while three items sit BLOCKED and nobody can say on what.

⚑ THE CONSTRAINT THAT SHAPES THE DESIGN, AND IT IS NOT NEGOTIABLE.
The monthly pre-run executes on Raj's device, which has NO NETWORK. The only route found to a
per-fund geographic breakdown is aggregator pages. Therefore the refresh CANNOT be a pre-run step.
It is an ASSISTED capture into `fund_exposure_sources.json`, and the pre-run READS the normalised
artefact and checks its age. A design that pretended otherwise would produce a step that silently
never runs — the framework's second failure class, four times over.

⚑ SINGLE SOURCE, AND SAID SO (R6.1 / R6.3). Hargreaves Lansdown for ten funds, justETF for the
S&P 500 ETF, the AIC for the investment trust. No corroborator has been reconciled. So the artefact
declares `corroboration: SINGLE_SOURCE`, and consumers are told in the file what that licenses: an
ORDINAL criterion, not a magnitude verdict that moves capital on its own.

⚑ POINT-IN-TIME IS PER FUND, NOT PER FILE (R6.4). The sources are as at dates from 31-Mar-2026 to
31-Jul-2026 — a 122-day spread. One `as_of` on the file would be a lie about eleven of them. Each
vector carries its own, the spread is published, and anything older than the declared staleness
horizon is REPORTED, never silently used as though it were current.

⚑ WHAT IS NOT ATTRIBUTED IS NOT SPREAD (R2.10 / R4.1). Where a source's country table does not sum
to 100 the residual is carried as `UNATTRIBUTED`, never distributed pro-rata across the countries
that were named. Pro-rata would make an unmeasured 6% look like evidence.

ROLLBACK (R4.13): `ENABLED = False` -> build() writes nothing and every consumer falls back to the
declared-mandate resolution it used before this module existed.

CLI:  python3 fund_exposure_vectors.py [--build] [--report] [--selftest]
"""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENABLED = True
SCHEMA_VERSION = "1.1.0"

# ── ISA-0403 (20-Aug-2026) ────────────────────────────────────────────────────────────────────
# ⚑ THE DEFECT THIS CLOSES. A fund can be unattributed in TWO ways and v1.0.0 measured only one.
#   (a) a label in the published table that is not a country  -> counted as `unattributed_pct`
#   (b) the published table not summing to 100 at all         -> NOT COUNTED ANYWHERE
# Ranmore's country table sums to 90.13 with every label geographic, so it published
# `unattributed_pct: -0.00` while 9.87% of the fund had no country at all — and `report()`'s own
# `> 2.0` flag therefore fired on 0 of the 5 funds whose true unattributed share exceeds 2%,
# including the worst. `unattributed_total_pct` is the honest measure and is what everything
# flags and gates on from v1.1.0; `attributed_pct_of_fund` is the same number stated positively
# and is the ONLY field a coverage floor may read (ISA-0328 §2.10 contract ii).
#
# ⚑ AND THE RESIDUAL POLICY WAS TRUE OF ONE CASE AND FALSE OF THE OTHER. "Normalised OUT, never
# spread pro-rata" is exactly right when the residual is genuinely non-geographic (cash, "Other"):
# renormalising over the named countries then says nothing about where the cash is. It is WRONG
# when the SOURCE ITSELF LOCATES the residual, because for a weighted sum normalising out IS
# spreading pro-rata — identically. Hargreaves Lansdown's REGIONAL table for Royal London carries
# Japan at 6.02 which its own COUNTRY table does not itemise, so v1.0.0 published Royal London
# holding ZERO Japan and re-allocated that 6.02 across the United States (62.51 of 93.77) and the
# rest. A located residual is now ATTRIBUTED, and the attribution must RECONCILE (R5.2).
LOCATED_RECONCILE_TOL_PP = 1.0   # published table + located residual must reach 100 within this
UNATTRIBUTED_REPORT_PP   = 2.0   # report() names any fund above this on the HONEST measure
SOURCES_FILE = "fund_exposure_sources.json"
OUT_FILE = "fund_exposure_vectors.json"

# A vector older than this is REPORTED as stale. It is a reporting horizon, not a gate: nothing is
# blocked by it, so it is not a capital-gating constant. Basis: fund factsheets publish monthly to
# quarterly, so two quarters is the point at which "the source has not been refreshed" stops being
# a normal publication lag and becomes a fact about the capture.
STALE_AFTER_DAYS = 183

# ⚑ TAXONOMY, NOT JUDGEMENT. Morningstar's own three-way partition, as parsed by extract_xray.py
# (`_REGION_PARENT`). Read from there where possible so there is ONE home for the tree; the country
# leaves below are the mapping from an aggregator's country label INTO that tree.
COUNTRY_TO_REGION = {
    # americas
    "United States": "americas", "Canada": "americas", "Brazil": "americas", "Mexico": "americas",
    "Bermuda": "americas", "Cayman Islands": "americas", "Chile": "americas", "Peru": "americas",
    # greater_europe  (Morningstar puts the UK, Europe, emerging Europe, Middle East & Africa here)
    "United Kingdom": "greater_europe", "Jersey": "greater_europe", "Ireland": "greater_europe",
    "France": "greater_europe", "Germany": "greater_europe", "Italy": "greater_europe",
    "Spain": "greater_europe", "Netherlands": "greater_europe", "Switzerland": "greater_europe",
    "Sweden": "greater_europe", "Norway": "greater_europe", "Denmark": "greater_europe",
    "Finland": "greater_europe", "Belgium": "greater_europe", "Austria": "greater_europe",
    "Portugal": "greater_europe", "Luxembourg": "greater_europe", "Greece": "greater_europe",
    "Poland": "greater_europe", "Hungary": "greater_europe", "Turkey": "greater_europe",
    "Russian Federation": "greater_europe", "Kazakhstan": "greater_europe",
    "Israel": "greater_europe", "South Africa": "greater_europe",
    # greater_asia
    "Japan": "greater_asia", "China": "greater_asia", "Hong Kong": "greater_asia",
    "Taiwan": "greater_asia", "South Korea": "greater_asia", "Republic of Korea": "greater_asia",
    "Singapore": "greater_asia", "India": "greater_asia", "Thailand": "greater_asia",
    "Indonesia": "greater_asia", "Philippines": "greater_asia", "Malaysia": "greater_asia",
    "Vietnam": "greater_asia", "Australia": "greater_asia", "New Zealand": "greater_asia",
}

# The aggregators' label -> the X-Ray's own country key. A label absent from BOTH this map and
# COUNTRY_TO_REGION is COUNTED and reported UNMAPPED — never dropped, never folded into "Other".
LABEL_ALIASES = {
    "South Korea": "Republic of Korea",
    "Korea": "Republic of Korea",
    "USA": "United States",
    "UK": "United Kingdom",
}

# Labels that are NOT a geography. They are excluded from the vector and reported, because
# "I could not attribute 1.7%" and "1.7% is in Other" must not produce the same output.
NON_GEOGRAPHIC = {"Other", "Others", "Non-Classified", "Cash and Equiv.", "Cash and Equivalents",
                  "Cash & Cash Equivalents", "Managed Fund", "Managed Funds",
                  "Direct Property and REITs", "Property", "Unclassified"}


class ContractError(RuntimeError):
    """An artefact that cannot be validly built. Raised, never downgraded."""


def _today() -> str:
    return dt.date.today().isoformat()


def _canon(label: str) -> str:
    lab = str(label).strip()
    return LABEL_ALIASES.get(lab, lab)


def normalise(raw: dict, located: dict = None) -> dict:
    """-> (weights summing to 1.0 over ATTRIBUTED geographic labels, diagnostics).

    `raw` is the source's published table, in percent OF THE FUND.
    `located` is the portion of the fund the source itself places somewhere the published table
    does not itemise (ISA-0403). It is ATTRIBUTED, not normalised out — see the module header —
    and its arrival is reconciled against the published table reaching 100.
    """
    geo, nongeo, unmapped = {}, {}, []
    for label, pct in raw.items():
        lab = _canon(label)
        if lab in NON_GEOGRAPHIC:
            nongeo[lab] = float(pct)
            continue
        if lab not in COUNTRY_TO_REGION:
            unmapped.append(lab)
            nongeo[lab] = float(pct)
            continue
        geo[lab] = geo.get(lab, 0.0) + float(pct)
    published = round(sum(raw.values()), 4)
    # ⚑ ISA-0403. A located residual enters the vector; an UNLOCATED one never does.
    located_total = 0.0
    for label, pct in (located or {}).items():
        lab = _canon(label)
        if lab in NON_GEOGRAPHIC:
            raise ContractError(
                "located_residual names %r, which is NON-GEOGRAPHIC. A residual may only be "
                "located to a place (R4.1)" % lab)
        if lab not in COUNTRY_TO_REGION:
            unmapped.append(lab)
            continue
        geo[lab] = geo.get(lab, 0.0) + float(pct)
        located_total += float(pct)
    if located_total:
        reach = published + located_total
        if abs(reach - 100.0) > LOCATED_RECONCILE_TOL_PP:
            raise ContractError(
                "located residual does not reconcile: published table %.2f + located %.2f = %.2f, "
                "which is %.2fpp from 100. Two tables from one source must account for the whole "
                "fund before either is used to place capital (R5.2)"
                % (published, located_total, reach, abs(reach - 100.0)))
    geo_total = sum(geo.values())
    if geo_total <= 0:
        raise ContractError("no geographic weight survived normalisation from %r" % list(raw))
    # ⚑ NORMALISED OVER THE GEOGRAPHIC PORTION ONLY, and the portion is published. The vector is a
    # statement about where the EQUITY exposure sits; cash is not a country and pretending it is
    # would put a bet on a line item.
    vec = {k: round(v / geo_total, 6) for k, v in sorted(geo.items())}
    unpublished = round(100.0 - published - located_total, 4)
    nongeo_pct = round(published - (geo_total - located_total), 4)
    return vec, {
        "published_total_pct": published,
        "located_residual_pct": round(located_total, 4),
        "geographic_pct": round(geo_total, 4),
        # ⚑ ISA-0403. THE ONLY FIELD A COVERAGE FLOOR MAY READ. Percent OF THE FUND, so
        # "I could not place 9.87% of this fund" and "this fund is 90.13% attributed" are the
        # same sentence and neither can be read as the other.
        "attributed_pct_of_fund": round(geo_total, 4),
        # the residual inside the published table — a label that is not a country
        "unattributed_pct": nongeo_pct,
        # the residual OUTSIDE it — the table simply did not reach 100. v1.0.0 counted this
        # NOWHERE, which is why Ranmore published -0.00 on 9.87%.
        "unpublished_pct": unpublished,
        # ⚑ the honest total, and what every flag and gate reads
        "unattributed_total_pct": round(nongeo_pct + unpublished, 4),
        "non_geographic": {k: round(v, 4) for k, v in sorted(nongeo.items())},
        "unmapped_labels": sorted(set(unmapped)),
        "residual_policy": (
            "a residual the source ITSELF LOCATES is ATTRIBUTED and reconciled against the "
            "published table reaching 100 (R5.2). A residual the source does NOT locate is "
            "carried as UNATTRIBUTED and normalised out — which for a weighted sum IS spreading "
            "it pro-rata across the countries that were named, so the fraction it applies to is "
            "published as `unattributed_total_pct` and gated, never hidden (ISA-0403, R4.9)"),
    }


def build(sources_path=None, out_path=None, as_of=None) -> dict:
    if not ENABLED:
        return {"state": "DISABLED", "reason": "fund_exposure_vectors.ENABLED is False (R4.13)"}
    as_of = as_of or _today()
    src = json.load(open(sources_path or HERE / SOURCES_FILE))
    vectors, meta, ages, unmapped_all = {}, {}, [], {}
    for sd, blk in (src.get("funds") or {}).items():
        vec, diag = normalise(blk["raw"], (blk.get("located_residual") or {}).get("weights"))
        vectors[sd] = vec
        d = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(blk["as_of"])).days
        ages.append(d)
        if diag["unmapped_labels"]:
            unmapped_all[sd] = diag["unmapped_labels"]
        meta[sd] = {
            "as_of": blk["as_of"], "age_days": d,
            "stale": bool(d > STALE_AFTER_DAYS),
            "source": blk["source"], "url": blk.get("url"),
            "share_class_described": blk.get("share_class_described"),
            "share_class_is_the_held_line": ("NOT the held" not in
                                             str(blk.get("share_class_described"))),
            "dimension": blk.get("dimension"),
            "note": blk.get("note"),
            "located_residual": blk.get("located_residual"),
            "diagnostics": diag,
            "region_rollup": _rollup(vec),
        }
    # ── R5.1 contract, asserted as the artefact is written ────────────────────────────────────
    bad = [sd for sd, v in vectors.items() if abs(sum(v.values()) - 1.0) > 0.02]
    if bad:
        raise ContractError("vectors do not sum to 1.00 +/- 0.02 for: %s" % ", ".join(sorted(bad)))
    if unmapped_all:
        raise ContractError(
            "labels with no place in the region tree were COUNTED and must be mapped before this "
            "artefact is admissible (R4.9): %s"
            % json.dumps(unmapped_all))

    doc = {
        "_meta": {"module": "fund_exposure_vectors.py", "schema_version": SCHEMA_VERSION,
                  "built_on": as_of, "item": "ISA-0392", "enabled": ENABLED,
                  "unblocks": ["ISA-0333", "ISA-0160", "ISA-0328", "ISA-0386"]},
        "as_of": "PER FUND — see per_fund[].as_of. A single file-level as_of would be false for "
                 "11 of 12 vectors (R6.4).",
        "as_of_spread_days": {"min": min(ages), "max": max(ages),
                              "note": "age at build, in days"},
        "source": "fund_exposure_sources.json (assisted capture — see its _meta.network_note)",
        "corroboration": (src.get("_meta") or {}).get("corroboration"),
        "admissible_as": (
            "(1) an ORDINAL input — the SIGN of a fund's contribution to a material active bet "
            "(capital_destination C1); and, from 20-Aug-2026 (ISA-0407), (2) a MAGNITUDE input to "
            "an exposure-weighted expected return (fund_expected_return, ISA-0328), because a "
            "SECOND, INDEPENDENT derivation now exists and agrees: aggregated by GBP weight these "
            "vectors reproduce the AJ Bell X-Ray's whole-portfolio Morningstar country table to a "
            "mean absolute difference of 0.40pp over eleven matched countries (US 40.57 vs 40.38, "
            "UK 18.47 vs 17.66), asserted every run by "
            "consistency_check.pair_lookthrough_xray_reconciliation (XR1). "
            "⚑ THE ADMISSIBILITY RESTS ON THAT MEASUREMENT AND IS WITHDRAWN WITH IT: if XR1 fails "
            "its declared tolerance the magnitude use lapses automatically, rather than by anyone "
            "remembering (R6.1/R6.3). The corroboration is at PORTFOLIO level and NOT per fund — "
            "no single fund's vector has been checked against a second source, the as-at spread "
            "against the X-Ray is 19-141 days, and two share classes are substitutes. A per-fund "
            "magnitude verdict is NOT licensed by this."),
        "magnitude_admissibility": {
            "granted": True, "item": "ISA-0407", "granted_on": "2026-08-20",
            "basis": "portfolio-level reconciliation against xray_data country_exposure",
            "contract": "consistency_check.pair_lookthrough_xray_reconciliation (XR1)",
            "scope": "portfolio and sleeve aggregates only — NOT a per-fund magnitude verdict",
            "withdrawn_if": "XR1 fails its declared tolerance on any run"},
        "country_to_region": COUNTRY_TO_REGION,
        "stale_after_days": STALE_AFTER_DAYS,
        "stale": sorted([sd for sd, m in meta.items() if m["stale"]]),
        "share_class_substitutions": sorted(
            [sd for sd, m in meta.items() if not m["share_class_is_the_held_line"]]),
        "vectors": vectors,
        "per_fund": meta,
        "state": "OK",
    }
    out = Path(out_path or HERE / OUT_FILE)
    out.write_text(json.dumps(doc, indent=2))
    doc["_written"] = str(out)
    return doc


def _rollup(vec: dict) -> dict:
    out = {}
    for k, w in vec.items():
        r = COUNTRY_TO_REGION.get(k)
        if r:
            out[r] = round(out.get(r, 0.0) + w, 6)
    return dict(sorted(out.items()))


def report(doc=None) -> str:
    d = doc or build()
    if d.get("state") != "OK":
        return "fund_exposure_vectors: %s" % d.get("state")
    L = ["PER-FUND EXPOSURE VECTORS (ISA-0392) — %d funds" % len(d["vectors"]),
         "corroboration: %s" % d["corroboration"],
         "as-at spread: %d-%d days old at build" % (d["as_of_spread_days"]["min"],
                                                    d["as_of_spread_days"]["max"]), ""]
    L.append("%-9s %-11s %5s  %-28s %s" % ("sedol", "as_of", "age", "region roll-up", "flags"))
    for sd, m in sorted(d["per_fund"].items(), key=lambda kv: kv[1]["as_of"]):
        # ⚑ `r[:2].upper()` printed "GR" for BOTH greater_europe and greater_asia — an
        # abbreviation that collides is a label that means nothing. Named explicitly.
        _ABBR = {"americas": "AM", "greater_europe": "EU", "greater_asia": "AS"}
        roll = " ".join("%s %.0f%%" % (_ABBR.get(r, r[:2].upper()), w * 100)
                        for r, w in sorted(m["region_rollup"].items(), key=lambda x: -x[1]))
        flags = []
        if m["stale"]:
            flags.append("STALE")
        if not m["share_class_is_the_held_line"]:
            flags.append("SHARE-CLASS SUBSTITUTE")
        # ⚑ ISA-0403. This read `unattributed_pct` and therefore fired on 0 of the 5 funds whose
        # true unattributed share exceeds the threshold — including Ranmore, the worst at 9.87%,
        # which published -0.00 because its whole shortfall was OUTSIDE the published table.
        if m["diagnostics"]["unattributed_total_pct"] > UNATTRIBUTED_REPORT_PP:
            flags.append("UNATTRIBUTED %.1f%% (of the FUND)"
                         % m["diagnostics"]["unattributed_total_pct"])
        if m["diagnostics"]["located_residual_pct"] > 0:
            flags.append("LOCATED RESIDUAL %.2f%%" % m["diagnostics"]["located_residual_pct"])
        L.append("%-9s %-11s %5d  %-28s %s" % (sd, m["as_of"], m["age_days"], roll,
                                               ", ".join(flags)))
    return "\n".join(L)


def selftest(verbose=True) -> int:
    import tempfile
    fails = []

    def ck(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    tmp = Path(tempfile.mkdtemp())          # ⚑ never beside the scripts — the mount denies delete
    d = build(out_path=tmp / OUT_FILE)
    ck("build returns OK for all 12 held funds",
       d["state"] == "OK" and len(d["vectors"]) == 12)
    ck("every vector sums to 1.00 within the stated tolerance (R5.1 contract)",
       all(abs(sum(v.values()) - 1.0) <= 0.02 for v in d["vectors"].values()))
    ck("every fund carries its OWN as_of and source (R4.2/R6.4)",
       all(m["as_of"] and m["source"] for m in d["per_fund"].values()))
    ck("the as-at SPREAD is published, not averaged away",
       d["as_of_spread_days"]["max"] > d["as_of_spread_days"]["min"])
    ck("share-class SUBSTITUTIONS are named, not silently equated",
       set(d["share_class_substitutions"]) == {"BR2Q8G6", "B55QSH0"})
    # ⚑ RESTATED 20-Aug-2026 (ISA-0348's class, and this is the SIXTH occurrence). The original
    # asserted the literal string "SINGLE_SOURCE", i.e. a claim about the CURRENT STATE of the
    # evidence rather than about the PROPERTY that matters — so it went red the moment ISA-0407
    # correctly upgraded the corroboration. The property is: whatever the corroboration status,
    # the artefact says what that status LICENSES, and any magnitude grant names the contract
    # that withdraws it. Ask of any assertion: what CORRECT behaviour makes this fail?
    _ma = d.get("magnitude_admissibility") or {}
    ck("corroboration status is declared AND what it licenses is stated (R6.3)",
       bool(d["corroboration"]) and "ORDINAL" in d["admissible_as"]
       and (("NOT admissible" in d["admissible_as"] and not _ma.get("granted"))
            or (_ma.get("granted") and _ma.get("contract") and _ma.get("withdrawn_if"))))
    ck("cash and 'Other' are EXCLUDED from the vector, not treated as a country",
       all(k not in v for v in d["vectors"].values() for k in NON_GEOGRAPHIC))
    # ⚑ RESTATED 20-Aug-2026 (ISA-0403). The original asserted the WORDS "never spread pro-rata",
    # which were false of the operation the code performs: for a weighted sum, normalising a
    # residual out IS spreading it pro-rata. The property is that the fraction this applies to is
    # COUNTED and PUBLISHED against the FUND, never hidden.
    ck("an UNATTRIBUTED residual is counted against the FUND and published, never hidden (R4.9)",
       all("unattributed_total_pct" in m["diagnostics"] and "attributed_pct_of_fund" in m["diagnostics"]
           for m in d["per_fund"].values())
       and any(m["diagnostics"]["unattributed_total_pct"] > 0.5 for m in d["per_fund"].values()))
    # ── ISA-0403 ─────────────────────────────────────────────────────────────────────────────
    ck("ISA-0403: attributed + unattributed accounts for the WHOLE fund, every fund (R5.2)",
       all(abs(m["diagnostics"]["attributed_pct_of_fund"]
               + m["diagnostics"]["unattributed_total_pct"] - 100.0) < 0.05
           for m in d["per_fund"].values()))
    ck("ISA-0403: the fund whose shortfall is entirely OUTSIDE the published table is caught — "
       "Ranmore reads 9.87% unattributed, not -0.00",
       abs(d["per_fund"]["BR2Q8G6"]["diagnostics"]["unattributed_total_pct"] - 9.87) < 0.02
       and abs(d["per_fund"]["BR2Q8G6"]["diagnostics"]["unattributed_pct"]) < 0.01)
    ck("ISA-0403: report() flags on the HONEST measure — the 5 funds above the threshold are "
       "named, where v1.0.0 named 0 of them",
       sum(1 for m in d["per_fund"].values()
           if m["diagnostics"]["unattributed_total_pct"] > UNATTRIBUTED_REPORT_PP) == 5)
    ck("ISA-0403: a residual the SOURCE ITSELF LOCATES is ATTRIBUTED — Royal London carries "
       "Japan at HL's own regional figure, not zero",
       abs(d["vectors"]["BF93W97"].get("Japan", 0.0) - 0.060894) < 1e-4
       and d["per_fund"]["BF93W97"]["diagnostics"]["located_residual_pct"] == 6.02)
    ck("every country in every vector maps into the region tree",
       all(k in COUNTRY_TO_REGION for v in d["vectors"].values() for k in v))
    ck("the region roll-up reproduces the vector (R5.2, two derivations)",
       all(abs(sum(m["region_rollup"].values()) - 1.0) <= 0.02 for m in d["per_fund"].values()))

    # ── negative controls: a genuinely broken input must FAIL (R5.5) ──────────────────────────
    broken = {"schema_version": "1.0.0", "_meta": {},
              "funds": {"XX": {"source": "s", "as_of": "2026-01-01", "dimension": "country",
                               "raw": {"Atlantis": 100.0}}}}
    bp = tmp / "broken.json"
    bp.write_text(json.dumps(broken))
    raised = False
    try:
        build(sources_path=bp, out_path=tmp / "b.json")
    except ContractError:
        raised = True
    ck("an UNMAPPED country label RAISES rather than being folded into Other (R4.9)", raised)

    empty = {"schema_version": "1.0.0", "_meta": {},
             "funds": {"XX": {"source": "s", "as_of": "2026-01-01", "dimension": "country",
                              "raw": {"Cash and Equivalents": 100.0}}}}
    ep = tmp / "empty.json"
    ep.write_text(json.dumps(empty))
    raised2 = False
    try:
        build(sources_path=ep, out_path=tmp / "e.json")
    except ContractError:
        raised2 = True
    ck("a fund with NO geographic weight RAISES, it does not emit an empty vector (R4.3)", raised2)

    # ── ISA-0403 negative controls: a located residual that does NOT reconcile must FAIL ─────
    bad_loc = {"schema_version": "1.0.0", "_meta": {},
               "funds": {"XX": {"source": "s", "as_of": "2026-01-01", "dimension": "country",
                                "raw": {"United States": 60.0, "France": 10.0},
                                "located_residual": {"weights": {"Japan": 5.0}}}}}
    lp = tmp / "badloc.json"
    lp.write_text(json.dumps(bad_loc))
    raised3 = False
    try:
        build(sources_path=lp, out_path=tmp / "l.json")
    except ContractError:
        raised3 = True
    ck("ISA-0403 CONTROL: a located residual that does not bring the table to 100 RAISES — "
       "70 + 5 = 75 is not an account of a fund (R5.2)", raised3)

    nongeo_loc = {"schema_version": "1.0.0", "_meta": {},
                  "funds": {"XX": {"source": "s", "as_of": "2026-01-01", "dimension": "country",
                                   "raw": {"United States": 94.0},
                                   "located_residual": {"weights": {"Cash and Equivalents": 6.0}}}}}
    np_ = tmp / "nongeo.json"
    np_.write_text(json.dumps(nongeo_loc))
    raised4 = False
    try:
        build(sources_path=np_, out_path=tmp / "n.json")
    except ContractError:
        raised4 = True
    ck("ISA-0403 CONTROL: a residual may only be located to a PLACE — locating it to cash RAISES",
       raised4)

    # ── the sanity check that makes the whole artefact worth having ───────────────────────────
    ck("the S&P 500 tracker reads overwhelmingly AMERICAS and the Japan tracker JAPAN — if these "
       "two are wrong, nothing downstream is trustworthy",
       d["per_fund"]["VUAG"]["region_rollup"].get("americas", 0) > 0.95
       and d["per_fund"]["B50MZ94"]["region_rollup"].get("greater_asia", 0) > 0.95)
    ck("the UK mandates read majority greater_europe and the Asia mandates greater_asia",
       min(d["per_fund"][s]["region_rollup"].get("greater_europe", 0)
           for s in ("B2PLJM6", "B55QSH0")) > 0.85
       and min(d["per_fund"][s]["region_rollup"].get("greater_asia", 0)
               for s in ("B6SQYF4", "B8N44Q8")) > 0.70)

    # ── R4.13 rollback ───────────────────────────────────────────────────────────────────────
    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module", build()["state"] == "DISABLED")
    ENABLED = True

    print("\nfund_exposure_vectors selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails else " — 21 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--report" in sys.argv:
        print(report())
    else:
        print(json.dumps(build(), indent=2)[:4000])
