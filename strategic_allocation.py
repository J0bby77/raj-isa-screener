#!/usr/bin/env python3
"""
strategic_allocation.py — the measurement half of ISA-0333.  Built 16-Aug-2026.
Closes ISA-0200 (M4, unintended exposures nobody authorised) and ISA-0203 (M7, largest active bets
unnamed and uncapped), which are INSTANCES of ISA-0333's class.

⚑ THE FINDING THIS MAKES VISIBLE. It was never true that the framework had no view on geography.
It has a very large one and nobody chose it. Measured against the X-Ray's own benchmark column at
31-Jul-2026: country active share 28.7%, driven by a 26.82pp UNDERWEIGHT to the United States and
a 13.64pp OVERWEIGHT to the United Kingdom. Neither appears in target_weights.json, neither has a
stated rationale, neither has a cap, neither is reviewed on any cadence. They are the emergent
residue of twelve fund choices.

⚑ THE REFERENCE PORTFOLIO WAS ALREADY ON DISK. Raj proposed a declared shadow reference portfolio.
The X-Ray publishes `benchmark_pct` beside `equity_pct` for every country, region and sector line
and `extract_xray.py` already parses it. Nothing needed sourcing. What was missing was a
DECLARATION that this column is the reference, and a module that reads it.

⚑ REFERENCE, NOT TARGET. The X-Ray benchmark is AJ Bell's chosen comparator, not one Raj selected,
and its composition is not published in the extract. It is admissible as a REFERENCE — the thing
active bets are measured against — and it is NOT a target. `reference.authority` says which it is
on every call, and a `raj_declared` vector overrides it whenever one exists.

⚑ IT MEASURES AND REPORTS. IT NEVER PROPOSES WEIGHTS. With twelve funds and 38-87 months the
covariance matrix is estimation-error dominated (ISA-0328: the cross-sectional dispersion of the
funds' realised returns is SMALLER than the estimation variance of those returns), so an optimiser
here would produce extreme, unstable weights that change monthly for no defensible reason. The
weights stay Raj's. This module's job is to make sure he is CHOOSING them rather than inheriting
them.

ROLLBACK (R4.13): `ENABLED = False` -> build() returns DISABLED and emits nothing.
"""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENABLED = True
SCHEMA_VERSION = "1.0.0"

# A bet at or above this size is one somebody should have decided. NOT capital-gating: nothing here
# blocks a trade, so it carries no R12.3 ledger entry - it sets what gets REPORTED as needing a
# rationale, not what gets refused.
MATERIALITY_PP = 5.0

# ⚑ NO DEFAULT CAP. An invented cap is worse than none: it would make an unauthorised bet look
# governed. Until Raj declares caps in `strategic_allocation_policy.json`, every material bet reads
# UNCAPPED and says so (R4.8 - an uninformed tie-break is REFUSED, not guessed).
POLICY_FILE = "strategic_allocation_policy.json"

# Both vectors describe the same universe, so their coverage must agree. They will NOT sum to 100:
# the X-Ray columns exclude cash, bonds and unclassified. What must hold is that the two columns
# cover the SAME thing to within this tolerance - otherwise a 89.90% portfolio column is being
# compared to an 89.47% benchmark column with nobody saying so.
COVERAGE_TOL_PP = 2.0


class ReferenceError(RuntimeError):
    """Raised when two vectors cannot be validly compared. Never downgraded to a warning."""


def _today() -> str:
    return dt.date.today().isoformat()


def _policy() -> dict:
    p = HERE / POLICY_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── the two vectors ───────────────────────────────────────────────────────────────────────────
def _vectors(xray: dict, dimension: str):
    """(portfolio, reference, source_label) for one dimension. R4.9 - a row that cannot be read is
    COUNTED, never dropped."""
    if dimension == "country":
        rows = xray.get("country_exposure") or []
        p = {r["country"]: r.get("equity_pct") for r in rows}
        b = {r["country"]: r.get("benchmark_pct") for r in rows}
        src = "xray.country_exposure"
    elif dimension == "region":
        # ISA-0367. world_regions is a TREE (parents contain their children and the whole
        # block sums to ~177pp), so the summable set is DECLARED by the extractor rather
        # than inferred here -- one home per rule. Before ISA-0367 the tree did not
        # reconcile at all: Western Europe, Central & Latin America and Emerging Asia
        # were silently absent, which is why this dimension was blocked on 16-Aug-2026.
        import extract_xray as _xr
        wr = _xr.world_regions_partition(xray.get("world_regions") or {})
        p = {k: v.get("equity_pct") for k, v in wr.items()}
        b = {k: v.get("benchmark_pct") for k, v in wr.items()}
        src = "xray.world_regions[depth=0 continents]"
    elif dimension == "sector":
        sw = xray.get("sector_weights") or {}
        p = {k: v.get("portfolio_pct") for k, v in sw.items()}
        b = {k: v.get("benchmark_pct") for k, v in sw.items()}
        src = "xray.sector_weights"
    else:
        raise ReferenceError(f"unknown dimension {dimension!r}")
    missing = sorted(k for k in p if p[k] is None or b.get(k) is None)
    return p, b, src, missing


def validate(p: dict, b: dict, dimension: str, expect_full=True) -> dict:
    """R5.1 - assert the contract at the artefact boundary, before any arithmetic.

    ⚑ Absent and zero are OPPOSITE FACTS. A key present in one vector and absent from the other is
    a comparison that cannot be made, not a bet of the full weight.
    """
    only_p, only_b = sorted(set(p) - set(b)), sorted(set(b) - set(p))
    sp, sb = sum(v for v in p.values() if v), sum(v for v in b.values() if v)
    errs = []
    if only_p or only_b:
        errs.append(f"dimension coverage differs: {len(only_p)} key(s) only in the portfolio "
                    f"{only_p}, {len(only_b)} only in the reference {only_b}")
    if abs(sp - sb) > COVERAGE_TOL_PP:
        errs.append(f"the two columns do not cover the same universe: portfolio sums to {sp:.2f}pp "
                    f"and the reference to {sb:.2f}pp, a {abs(sp-sb):.2f}pp gap against a "
                    f"{COVERAGE_TOL_PP}pp tolerance")
    # a dimension that should tile the equity book must roughly do so
    if expect_full and sp < 95.0 and dimension in ("sector",):
        errs.append(f"the {dimension} vector covers only {sp:.2f}pp of the book - a dimension that "
                    f"should tile it. Rows are missing from the extract, and a missing row reads "
                    f"as an absent exposure rather than an unread one")
    return {"ok": not errs, "errors": errs, "portfolio_sum_pp": round(sp, 2),
            "reference_sum_pp": round(sb, 2), "keys": len(set(p) | set(b))}


# ── the measurement ───────────────────────────────────────────────────────────────────────────
def active_bets(xray: dict, dimension: str, policy: dict = None) -> dict:
    policy = policy if policy is not None else _policy()
    p, b, src, missing = _vectors(xray, dimension)
    v = validate(p, b, dimension)
    if not v["ok"]:
        # R4.3 - a control fed an input it cannot trust BLOCKS. It does not publish a bet list
        # computed from a vector it has just said is broken.
        return {"state": "BLOCKED", "dimension": dimension, "source": src,
                "validation": v, "missing_values": missing,
                "reason": "the reference comparison is refused because the two vectors are not "
                          "comparable; a bet list computed here would be arithmetic on a known-bad "
                          "input and would look exactly like a good one"}
    caps = (policy.get("caps") or {}).get(dimension, {})
    ledger = set((policy.get("authorised") or {}).get(dimension, []))
    bets = []
    for k in sorted(set(p) | set(b)):
        a = (p.get(k) or 0.0) - (b.get(k) or 0.0)
        cap = caps.get(k)
        material = abs(a) >= MATERIALITY_PP
        bets.append({
            "key": k, "portfolio_pct": round(p.get(k) or 0.0, 2),
            "reference_pct": round(b.get(k) or 0.0, 2), "active_pp": round(a, 2),
            "material": material,
            "cap_pp": cap,
            "cap_state": ("UNCAPPED" if cap is None else
                          ("BREACH" if abs(a) > cap else "WITHIN_CAP")),
            "authorised": (k in ledger) if material else None,
            "authorisation_state": (None if not material else
                                    ("AUTHORISED" if k in ledger else "UNAUTHORISED")),
        })
    bets.sort(key=lambda r: -abs(r["active_pp"]))
    tot = sum(abs(r["active_pp"]) for r in bets)
    unauth = [r["key"] for r in bets if r["authorisation_state"] == "UNAUTHORISED"]
    return {"state": "MEASURED", "dimension": dimension, "source": src,
            "as_of": (xray.get("_meta") or {}).get("report_date"),
            "validation": v, "bets": bets,
            "active_share_pct": round(tot / 2.0, 2),
            "sum_abs_active_pp": round(tot, 2),
            "materiality_pp": MATERIALITY_PP,
            "material_bets": [r["key"] for r in bets if r["material"]],
            "unauthorised_material_bets": unauth,
            "breaches": [r["key"] for r in bets if r["cap_state"] == "BREACH"]}


def reference(policy: dict = None) -> dict:
    policy = policy if policy is not None else _policy()
    declared = policy.get("reference_vector")
    return {
        "authority": "raj_declared" if declared else "xray_benchmark",
        "as_of": policy.get("declared_on"),
        "source": ("strategic_allocation_policy.json" if declared else
                   "AJ Bell X-Ray `benchmark_pct` column, parsed by extract_xray.py"),
        "admissible_as": "REFERENCE",
        "not_admissible_as": ("TARGET - it is AJ Bell's chosen comparator, not one Raj selected, "
                              "and its composition is not published in the extract"
                              if not declared else None),
    }


def attribution(xray: dict, dimension: str, destinations: dict, exposures: dict,
                total_gbp: float) -> dict:
    """The marginal-pound question: what does each candidate destination do to each active bet?

    `destinations` {name: gbp}, `exposures` {name: {key: pct}}. This is the wire into
    capital_destination - an allocation that widens an unauthorised bet should be visible as such
    BEFORE it is placed, not discovered in next month's X-Ray.
    """
    base = active_bets(xray, dimension)
    if base["state"] != "MEASURED":
        return {"state": "BLOCKED", "reason": base["reason"]}
    cur = {r["key"]: r["active_pp"] for r in base["bets"]}
    delta = {k: 0.0 for k in cur}
    unpriced = []
    for name, gbp in (destinations or {}).items():
        ex = (exposures or {}).get(name)
        if not ex:
            if gbp:
                unpriced.append(name)
            continue
        w = gbp / total_gbp * 100.0 if total_gbp else 0.0
        for k, pct in ex.items():
            if k in delta:
                delta[k] += w * (pct / 100.0)
    return {"state": "MEASURED", "dimension": dimension,
            "delta_pp": {k: round(v, 3) for k, v in delta.items() if abs(v) >= 0.005},
            "post_trade_active_pp": {k: round(cur[k] + delta[k], 2) for k in cur},
            "unpriced_destinations": unpriced,
            "note": ("a destination with no declared exposure vector is UNPRICED and named - it is "
                     "not treated as having zero effect (R2.10)")}


def _attribution_block(xray, measured):
    """attribution(), per measured dimension, with its inputs and their provenance declared.

    Reported, never binding: this module MEASURES and REPORTS; it never proposes weights."""
    dest, exp, total, srcs = _attribution_inputs()
    if not dest:
        return {"state": "NO_DESTINATIONS",
                "reason": "no capital_destination allocation on disk, so there is no candidate "
                          "pound to attribute. Reported as absent, never as zero effect (R2.10)",
                "sources": srcs}
    if not total:
        return {"state": "BLOCKED", "reason": "no portfolio total to express the pp change against",
                "sources": srcs}
    out = {"state": "MEASURED", "binding": False, "sources": srcs,
           "destinations_gbp": {k: round(v, 2) for k, v in sorted(dest.items())},
           "total_gbp": total,
           "note": ("the marginal-pound question, one dimension at a time: what does each funded "
                    "destination do to each active bet? A destination with no declared exposure "
                    "vector is UNPRICED and NAMED — never treated as having zero effect."),
           "dimensions": {}}
    for dim in measured:
        # ⚑ A DESTINATION PRICED ON THE WRONG DIMENSION READS AS ZERO EFFECT, and that is exactly
        # the output an absent one should NOT produce (R2.10). `attribution` accumulates only keys
        # that exist in the bet set, so handing it a COUNTRY vector against a SECTOR bet returns a
        # full set of clean zeros. Each dimension is therefore run only against an exposure vector
        # that ADDRESSES it, and the rest REFUSE and say why.
        ex = (exp or {}).get(dim)
        if not ex:
            out["dimensions"][dim] = {
                "state": "NOT_PRICEABLE", "binding": False,
                "reason": ("no exposure vector addresses the %s dimension. A country vector run "
                           "against a %s bet would return a full set of zeros, which is the one "
                           "output an absent measurement must never produce (R2.10)." % (dim, dim)),
                "what_would_resolve_it": ("a per-fund %s breakdown in fund_exposure_sources.json, "
                                          "captured the same assisted way as the country tables"
                                          % dim)}
            continue
        try:
            out["dimensions"][dim] = attribution(xray, dim, dest, ex, float(total))
        except Exception as e:                                      # noqa: BLE001
            out["dimensions"][dim] = {"state": "ERROR",
                                      "reason": "%s: %s" % (type(e).__name__, e)}
    return out


def _attribution_inputs():
    """The two things `attribution` needs, read from the artefacts that already own them.

    ⚑ ISA-0404. `attribution` was BUILT, complete, with a docstring saying "This is the wire into
    capital_destination" — and NOTHING CALLED IT. ISA-0333's own `build_readiness_basis` named it
    as the item's one remaining unbuilt block, so the register believed it was unbuilt and the code
    said it was built, and both were wrong in the same place. Sixth occurrence of the second
    failure class in eight days, and the FIRST at FUNCTION rather than MODULE granularity: R4.6's
    enumeration passes a module that is imported and called even when the named deliverable inside
    it is unreached."""
    import re as _re
    dest, exp, total, srcs = {}, {}, None, {}
    mon = _re.compile(r"^_\d{4}_\d{2}$")
    cds = sorted(p for p in HERE.glob("capital_destination_*.json")
                 if mon.match(p.stem[len("capital_destination"):]))
    scen = sorted(HERE.glob("capital_destination_*_scenario.json"))
    pick = cds[-1] if cds else (scen[-1] if scen else None)
    if pick is not None:
        cd = json.loads(pick.read_text(encoding="utf-8"))
        dest = {k: v for k, v in
                (((cd.get("fund_allocation") or {}).get("allocation")) or {}).items() if v}
        total = (((cd.get("inputs") or {}).get("portfolio_total_gbp")) or {}).get("value")
        srcs["destinations"] = pick.name
    fev_p = HERE / "fund_exposure_vectors.json"
    if fev_p.exists():
        fev = json.loads(fev_p.read_text(encoding="utf-8"))
        # attribution() expects PERCENT per key, on the same dimension as active_bets
        exp = {"country": {sd: {c: round(w * 100.0, 6) for c, w in vec.items()}
                           for sd, vec in (fev.get("vectors") or {}).items()},
               "region": {sd: {r: round(w * 100.0, 6)
                               for r, w in ((m.get("region_rollup") or {}).items())}
                          for sd, m in (fev.get("per_fund") or {}).items()}}
        srcs["exposures"] = "fund_exposure_vectors.json (country vectors; region roll-ups)"
    if total is None:
        pd_ = sorted(HERE.glob("portfolio_data_*.json"))
        if pd_:
            total = ((json.loads(pd_[-1].read_text(encoding="utf-8")).get("summary")) or {}
                     ).get("total_value_gbp")
            srcs["total_gbp"] = pd_[-1].name
    return dest, exp, total, srcs


def build(xray_path=None, out_path=None, as_of=None):
    if not ENABLED:
        return {"state": "DISABLED", "reason": "strategic_allocation.ENABLED is False (R4.13)"}
    as_of = as_of or _today()
    xray = json.load(open(xray_path or HERE / "xray_data_aug_2026.json"))
    pol = _policy()
    dims = {d: active_bets(xray, d, pol) for d in ("country", "region", "sector")}
    measured = {d: v for d, v in dims.items() if v["state"] == "MEASURED"}
    doc = {
        "_meta": {"module": "strategic_allocation.py", "schema_version": SCHEMA_VERSION,
                  "as_of": as_of, "built": "2026-08-16", "enabled": ENABLED,
                  "closes": ["ISA-0200", "ISA-0203"], "partial_of": "ISA-0333"},
        "reference": reference(pol),
        "xray_as_of": (xray.get("_meta") or {}).get("report_date"),
        "dimensions": dims,
        "active_share_pct": {d: v["active_share_pct"] for d, v in measured.items()},
        "unauthorised_material_bets": {d: v["unauthorised_material_bets"]
                                       for d, v in measured.items()},
        "blocked_dimensions": {d: v["reason"] for d, v in dims.items()
                               if v["state"] != "MEASURED"},
        "attribution": _attribution_block(xray, measured),
        "policy_present": bool(pol),
        "policy_note": ("no strategic_allocation_policy.json on disk, so no caps are declared and "
                        "every material bet reads UNCAPPED. That is the honest state, not a "
                        "failure - an invented cap would make an unauthorised bet look governed."
                        if not pol else None),
        "state": "OK",
    }
    out = Path(out_path or HERE / f"strategic_allocation_{as_of[:7].replace('-','_')}.json")
    out.write_text(json.dumps(doc, indent=2))
    doc["_written"] = str(out)
    return doc


def report(doc=None) -> str:
    d = doc or build()
    L = [f"STRATEGIC ALLOCATION — reference: {d['reference']['authority']} "
         f"(X-Ray {d.get('xray_as_of')})", ""]
    for dim, v in d["dimensions"].items():
        if v["state"] != "MEASURED":
            L.append(f"{dim.upper():8s} BLOCKED — {v['validation']['errors'][0][:110]}")
            continue
        L.append(f"{dim.upper():8s} active share {v['active_share_pct']:.1f}%")
        for r in v["bets"][:5]:
            flag = "" if not r["material"] else f"  [{r['authorisation_state']} · {r['cap_state']}]"
            L.append(f"    {r['key'][:24]:24s} {r['portfolio_pct']:7.2f} vs "
                     f"{r['reference_pct']:6.2f}  {r['active_pp']:+7.2f}pp{flag}")
        L.append("")
    return "\n".join(L)


def selftest(verbose=True) -> int:
    import tempfile
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    d = build(out_path=tempfile.mktemp(suffix=".json"))
    ck("build returns OK", d["state"] == "OK")
    ck("the reference declares its authority and that it is NOT a target",
       d["reference"]["authority"] == "xray_benchmark"
       and "TARGET" in (d["reference"]["not_admissible_as"] or ""))

    c = d["dimensions"]["country"]
    ck("country dimension is MEASURED", c["state"] == "MEASURED")
    ck("ISA-0203 REPRODUCED: the largest bets are now NAMED",
       c["bets"][0]["key"] == "United States" and c["bets"][1]["key"] == "United Kingdom")
    ck("US underweight measured at -26.82pp", c["bets"][0]["active_pp"] == -26.82)
    ck("UK overweight measured at +13.64pp", c["bets"][1]["active_pp"] == 13.64)
    ck("country active share 28.7%", abs(c["active_share_pct"] - 28.7) < 0.05)
    ck("ISA-0200 REPRODUCED: material bets read UNAUTHORISED with no policy on disk",
       "United States" in c["unauthorised_material_bets"]
       and "United Kingdom" in c["unauthorised_material_bets"])
    ck("with no policy, caps read UNCAPPED - never an invented number",
       all(r["cap_pp"] is None and r["cap_state"] == "UNCAPPED" for r in c["bets"]))
    ck("an immaterial bet is not asked for a rationale",
       all(r["authorisation_state"] is None for r in c["bets"] if not r["material"]))

    # ⚑ REGION. `world_regions` is HIERARCHICAL, not a partition: `americas` contains
    # `united_states` and `canada`, `greater_europe` contains `united_kingdom`. Summing the
    # raw block double-counts and would have produced an active share roughly twice the truth
    # while looking entirely plausible. Until ISA-0367 the dimension was BLOCKED for that
    # reason; the extractor now DECLARES which nodes form a partition and asserts the tree
    # reconciles, so the dimension is measurable. The assertion below is therefore on the
    # PROPERTY (the raw block must never be summed; the declared partition must tile) and not
    # on the stored state BLOCKED -- an assertion that requires the framework to stay broken
    # is ISA-0348's class and it fails as soon as the defect is fixed.
    import extract_xray as _xr
    _x = json.load(open(HERE / "xray_data_aug_2026.json"))
    _wr = _x.get("world_regions") or {}
    ck("world_regions still declares itself a hierarchy, not a partition",
       (_wr.get("_structure") or {}).get("is_partition") is False)
    ck("the RAW world_regions block must never be summed - it still totals >150pp",
       sum(v["equity_pct"] for k, v in _wr.items() if k != "_structure") > 150.0)
    ck("the DECLARED region partition tiles the book",
       abs(sum(v["equity_pct"] for v in _xr.world_regions_partition(_wr).values()) - 100.0) <= 1.0)
    ck("the declared partition and the declared leaves are two derivations that agree",
       abs(sum(v["equity_pct"] for v in _xr.world_regions_partition(_wr).values())
           - sum(v["equity_pct"] for v in _xr.world_regions_leaves(_wr).values())) <= 0.25)
    r = d["dimensions"]["region"]
    ck("REGION IS MEASURED once the tree reconciles (ISA-0367)", r["state"] == "MEASURED")

    # ⚑ SECTOR. Blocked until ISA-0367 because the vector was 11.55pp short and two sectors
    # carried one row's numbers. Same reasoning as above: assert the CONTRACT, not the state.
    s_ = d["dimensions"]["sector"]
    ck("SECTOR IS MEASURED once the vector tiles (ISA-0367)", s_["state"] == "MEASURED")
    ck("the sector vector tiles to 100 on the portfolio column",
       abs(s_["validation"]["portfolio_sum_pp"] - 100.0) <= 1.5)
    ck("communication_services is present in the measured sector vector",
       "communication_services" in (_x.get("sector_weights") or {}))
    ck("consumer_cyclical and consumer_defensive are distinct rows",
       (_x["sector_weights"]["consumer_cyclical"]["portfolio_pct"]
        != _x["sector_weights"]["consumer_defensive"]["portfolio_pct"]))

    # negative controls
    x = json.load(open(HERE / "xray_data_aug_2026.json"))
    ident = {"country_exposure": [{"country": r["country"], "equity_pct": r["benchmark_pct"],
                                   "benchmark_pct": r["benchmark_pct"]}
                                  for r in x["country_exposure"]], "_meta": x["_meta"]}
    z = active_bets(ident, "country", {})
    ck("NEGATIVE CONTROL: a portfolio identical to the reference has active share 0 and no bets",
       z["active_share_pct"] == 0.0 and not z["material_bets"])

    # a 30pp bet, funded from another line so total coverage is unchanged and the contract passes
    big = json.loads(json.dumps(x))
    rows = {r["country"]: r for r in big["country_exposure"]}
    rows["United States"]["equity_pct"] = rows["United States"]["benchmark_pct"] + 30.0
    rows["Japan"]["equity_pct"] = rows["Japan"]["equity_pct"] - 30.0 - 26.82
    z2 = active_bets(big, "country", {"caps": {"country": {"United States": 10.0}}})
    ck("the breach fixture still satisfies the coverage contract", z2["state"] == "MEASURED")
    ck("NEGATIVE CONTROL: a 30pp bet against a 10pp cap reads BREACH",
       "United States" in z2["breaches"])

    gap = {"country_exposure": [{"country": "US", "equity_pct": 60.0, "benchmark_pct": 60.0},
                                {"country": "UK", "equity_pct": 30.0, "benchmark_pct": None}],
           "_meta": {}}
    z3 = active_bets(gap, "country", {})
    ck("NEGATIVE CONTROL: a reference missing a key BLOCKS - absent is not zero",
       z3["state"] == "BLOCKED")

    pol = {"caps": {"country": {"United States": 30.0, "United Kingdom": 15.0}},
           "authorised": {"country": ["United States", "United Kingdom"]},
           "reference_vector": None}
    z4 = active_bets(x, "country", pol)
    ck("NEGATIVE CONTROL: declaring caps and rationales clears both flags",
       not z4["unauthorised_material_bets"] and not z4["breaches"])

    a = attribution(x, "country", {"F1": 5000.0}, {"F1": {"United States": 100.0}}, 150988.39)
    ck("attribution prices a destination's effect on the bet",
       a["state"] == "MEASURED" and a["delta_pp"]["United States"] > 3.0)
    a2 = attribution(x, "country", {"F2": 5000.0}, {}, 150988.39)
    ck("NEGATIVE CONTROL: a destination with no exposure vector is UNPRICED and NAMED, not zero",
       a2["unpriced_destinations"] == ["F2"])

    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module", build()["state"] == "DISABLED")
    ENABLED = True

    print(f"\nstrategic_allocation selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 20 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--report" in sys.argv:
        print(report())
    else:
        print(json.dumps(build(), indent=2))
