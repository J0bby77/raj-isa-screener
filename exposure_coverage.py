#!/usr/bin/env python3
"""
exposure_coverage.py - B08-R01/R02 (October build P-B6, ISA-0842): the WHOLE-ISA exposure ledger.

One dated row per exposure dimension. Each row conserves the book:

    expected_gbp (= NAV of the dated portfolio file)
        = covered_gbp + unclassified_gbp + unvalued_gbp (+ rounding)

and says what KIND of answer it holds:

    COMPLETE      every pound of the dimension is attributed by a published whole-book source
    PROXY         complete coverage, but a stated mapping (e.g. region -> currency) stands in
    LOWER_BOUND   a measured or judged subset; the remainder is NOT zero. A subset below a cap
                  never establishes the whole book is under it (B08-R02)
    BOUNDED       the largest unseen member is bounded above by a published figure
    UNAVAILABLE   no source; owner named

What is deliberately NOT done here (R4.4, one home per rule):
  * no cap is applied - factor_lookthrough owns the B3 factor verdict, concentration_control the
    ISA-0465 sector/theme caps. This module only states how much of the book each answer covers.
  * funds and their underlying holdings are never both counted: every X-Ray figure is Morningstar's
    whole-portfolio look-through (direct stocks + fund holdings + cash in one partition), and the
    factor rows apply a fund's share to the fund's value only.

Usage: python3 exposure_coverage.py --portfolio portfolio_data_oct_2026.json --xray xray_data_oct_2026.json
       python3 exposure_coverage.py --selftest
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA = "exposure_coverage/1"
BASIS_TOL_PCT = 2.0           # X-Ray implied base vs NAV: larger gap = the % are of a different book
DATE_TOL_DAYS = 7

# region leaf -> currency bloc (PROXY: domicile of the equity, not its hedging or trading currency)
REGION_CCY = {
    "united_kingdom": "GBP", "united_states": "USD", "canada": "CAD", "japan": "JPY",
    "western_europe": "EUR_AND_OTHER_EUROPE", "australasia": "AUD_NZD",
    "emerging_europe": "EM_FX", "middle_east___africa": "EM_FX", "central_and_latin_america": "EM_FX",
    "emerging_4_tigers": "ASIA_FX", "emerging_asia": "ASIA_FX",
}


def _d(s):
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except Exception:                                                   # noqa: BLE001
            pass
    return None


def _row(dim, kind, basis, as_of, expected, covered, unclassified, unvalued=0.0, note="", **kw):
    r = {"dimension": dim, "verdict_type": kind, "basis": basis, "as_of": as_of,
         "expected_gbp": round(expected, 2), "covered_gbp": round(covered, 2),
         "unclassified_gbp": round(max(unclassified, 0.0), 2), "unvalued_gbp": round(unvalued, 2),
         "covered_pct": (round(covered / expected * 100.0, 1) if expected else None), "note": note}
    r.update(kw)
    return r


def _unvalued(portfolio):
    out = []
    for h in (portfolio.get("stocks") or []) + (portfolio.get("funds") or []):
        v = h.get("value_gbp")
        if v is None or (isinstance(v, (int, float)) and v <= 0 and (h.get("quantity") or 0) > 0):
            out.append(h.get("ticker"))
    return out


def _xray_basis(portfolio, xray, nav):
    """Are the X-Ray percentages of THIS book? Implied base = fund value / X-Ray weight, median."""
    fh = (xray or {}).get("fund_holdings") or []
    vals = {}
    for h in (portfolio.get("funds") or []) + (portfolio.get("stocks") or []):
        vals[str(h.get("full_name") or h.get("name") or "").upper()[:20]] = h.get("value_gbp")
    implied = []
    for r in fh:
        w = r.get("weight_pct")
        v = vals.get(str(r.get("name") or "").upper()[:20])
        if w and v:
            implied.append(v / (w / 100.0))
    implied.sort()
    base = implied[len(implied) // 2] if implied else None
    gap = (round((base - nav) / nav * 100.0, 2) if (base and nav) else None)
    xd = _d(((xray or {}).get("_meta") or {}).get("report_date"))
    pd_ = _d(((portfolio or {}).get("_meta") or {}).get("data_date"))
    days = (abs((xd - pd_).days) if (xd and pd_) else None)
    state = "UNVERIFIED"
    if gap is not None and days is not None:
        state = "MATCH" if (abs(gap) <= BASIS_TOL_PCT and days <= DATE_TOL_DAYS) else "MISMATCH"
    return {"state": state, "implied_base_gbp": (round(base, 2) if base else None), "nav_gbp": round(nav, 2),
            "gap_pct": gap, "n_matched": len(implied), "xray_date": str(xd) if xd else None,
            "portfolio_date": str(pd_) if pd_ else None, "days_apart": days}


def build(portfolio, xray=None, fmap=None):
    nav = float(((portfolio or {}).get("summary") or {}).get("total_value_gbp") or 0.0)
    as_of_p = ((portfolio or {}).get("_meta") or {}).get("data_date")
    as_of_x = ((xray or {}).get("_meta") or {}).get("report_date")
    unv = _unvalued(portfolio or {})
    rows = []
    if nav <= 0:
        return {"schema": SCHEMA, "state": "UNAVAILABLE", "why": "no portfolio NAV", "rows": [],
                "line": "Whole-ISA exposure coverage: UNAVAILABLE (no portfolio NAV)"}
    basis = _xray_basis(portfolio, xray, nav) if xray else {"state": "ABSENT"}
    xok = bool(xray) and basis["state"] != "MISMATCH"

    # 1. asset class - Morningstar partition of the whole book
    aa = (xray or {}).get("asset_allocation") or {}
    if xok and aa:
        net = {k: float((v or {}).get("net_pct") or 0.0) for k, v in aa.items()}
        cov = (net.get("stocks", 0) + net.get("bonds", 0) + net.get("cash", 0)) / 100.0 * nav
        unc = (net.get("other", 0) + net.get("not_classified", 0)) / 100.0 * nav
        rows.append(_row("asset_class", "COMPLETE" if unc < 0.5 else "LOWER_BOUND", "PUBLISHED_LOOKTHROUGH",
                         as_of_x, nav, cov, unc, note=("X-Ray 'Other'/'Not classified' %.2f%% is NOT equity, bond "
                         "or cash to Morningstar - it is unattributed here, never zero" % (net.get("other", 0) + net.get("not_classified", 0)))))
        equity_gbp = net.get("stocks", 0) / 100.0 * nav
        cash_gbp = net.get("cash", 0) / 100.0 * nav
        nonequity_unc = unc
    else:
        rows.append(_row("asset_class", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav,
                         note="no admissible X-Ray for this book"))
        equity_gbp = cash_gbp = nonequity_unc = None

    # 2. sector - % of the equity portion; the non-equity remainder is not 'no sector', it is unattributed
    sw = (xray or {}).get("sector_weights") or {}
    if xok and sw and equity_gbp is not None:
        s = sum(float((v or {}).get("portfolio_pct") or 0.0) for v in sw.values())
        cov = equity_gbp * min(s, 100.0) / 100.0
        unc = nav - cov - cash_gbp
        rows.append(_row("sector", "COMPLETE" if unc < 0.5 else "LOWER_BOUND", "PUBLISHED_LOOKTHROUGH", as_of_x,
                         nav, cov + cash_gbp, unc, note="equity by Morningstar sector + cash; unattributed = X-Ray 'Other' asset mass"
                         + ("" if s >= 99.5 else " + %.2f%% of equity with no sector row" % (100 - s))))
    else:
        rows.append(_row("sector", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav))

    # 3. region - leaf partition (complete), and 4. country - the printed TOP table only
    import extract_xray as _xr
    leaves = _xr.world_regions_leaves((xray or {}).get("world_regions") or {}) if xok else {}
    if leaves and equity_gbp is not None:
        s = sum(float(v.get("equity_pct") or 0.0) for v in leaves.values())
        cov = equity_gbp * min(s, 100.0) / 100.0
        rows.append(_row("region", "COMPLETE" if (nav - cov - cash_gbp) < 0.5 else "LOWER_BOUND", "PUBLISHED_LOOKTHROUGH",
                         as_of_x, nav, cov + cash_gbp, nav - cov - cash_gbp, note="Morningstar sub-region partition of equity + cash"))
        ccy = {}
        for k, v in leaves.items():
            b = REGION_CCY.get(k)
            ccy[b or "UNMAPPED"] = ccy.get(b or "UNMAPPED", 0.0) + equity_gbp * float(v.get("equity_pct") or 0.0) / 100.0
        ccy["GBP"] = ccy.get("GBP", 0.0) + cash_gbp
        unm = ccy.pop("UNMAPPED", 0.0)
        cov_c = sum(ccy.values())
        rows.append(_row("currency", "PROXY", "REGION_OF_DOMICILE (not hedging-aware; fund-held cash assumed GBP)", as_of_x,
                         nav, cov_c, nav - cov_c, note="a USD-listed fund share class hedged to GBP would read USD here",
                         blocs_gbp={k: round(v, 2) for k, v in sorted(ccy.items(), key=lambda kv: -kv[1])},
                         unmapped_gbp=round(unm, 2)))
    else:
        rows.append(_row("region", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav))
        rows.append(_row("currency", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav))
    cty = [r for r in ((xray or {}).get("country_exposure") or []) if r.get("country") not in ("Other", "Others")]
    if xok and cty and equity_gbp is not None:
        s = sum(float(r.get("equity_pct") or 0.0) for r in cty)
        cov = equity_gbp * s / 100.0
        rows.append(_row("country", "LOWER_BOUND", "PUBLISHED_TOP_TABLE", as_of_x, nav, cov + cash_gbp,
                         nav - cov - cash_gbp, note="X-Ray prints the top %d countries only (%.1f%% of equity); "
                         "use the region row for a complete partition" % (len(cty), s)))
    else:
        rows.append(_row("country", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav))

    # 5. issuer - published top-10 look-through; every unlisted issuer is strictly below the 10th entry
    lt = (xray or {}).get("lookthrough_top10") or {}
    hold = lt.get("holdings") or []
    if xok and hold:
        top = sum(float(h.get("weight_pct") or 0.0) for h in hold)
        floor = min(float(h.get("weight_pct") or 0.0) for h in hold)
        mx = max(hold, key=lambda h: float(h.get("weight_pct") or 0.0))
        rows.append(_row("issuer", "BOUNDED", "PUBLISHED_TOP10_LOOKTHROUGH", as_of_x, nav, top / 100.0 * nav,
                         nav - top / 100.0 * nav, note="largest issuer %s %.2f%% (published, direct + via funds); "
                         "every unlisted issuer < %.2f%%" % (mx.get("name"), float(mx.get("weight_pct") or 0), floor),
                         largest_issuer={"name": mx.get("name"), "pct": mx.get("weight_pct")}, unlisted_issuer_max_pct=floor))
    else:
        rows.append(_row("issuer", "UNAVAILABLE", "X-Ray " + basis["state"], as_of_x, nav, 0.0, nav))

    # 6/7. factor rows - from the ONE factor computation (factor_lookthrough)
    try:
        import factor_lookthrough as _flt
        fl = _flt.compute(portfolio, fmap if fmap is not None else _flt.load_map())
        for dim, src in (("factor_ai_complex", fl), ("factor_semis_complex", fl.get("semis") or {})):
            lo, hi = src.get("lower_bound_gbp"), src.get("upper_bound_gbp")
            rows.append(_row(dim, "LOWER_BOUND", "JUDGEMENT_FLOOR (factor_map.json fund shares are seeded estimates)",
                             as_of_p, nav, nav - (src.get("unclassified_gbp") or 0.0),
                             src.get("unclassified_gbp") or 0.0, note=src.get("verdict_note") or "",
                             lower_bound_pct=src.get("lower_bound_pct"), upper_bound_pct=src.get("upper_bound_pct"),
                             verdict=src.get("verdict")))
    except Exception as e:                                                   # noqa: BLE001
        rows.append(_row("factor_ai_complex", "UNAVAILABLE", "factor_lookthrough failed: %s" % e, as_of_p, nav, 0.0, nav))

    # 8. liquidity - no source in October scope
    rows.append(_row("liquidity", "UNAVAILABLE", "NO_SOURCE", None, nav, 0.0, nav,
                     note="order liquidity / spread at target scale is B08-R10 (ISA-0842); not measured this run",
                     owner="ISA-0842 B08-R10"))

    unvalued_names = [t for t in unv if t]
    # the largest UNATTRIBUTED mass among the partition rows (issuer is a bound, factors are judged floors)
    worst = sorted((r for r in rows if r["dimension"] in ("asset_class", "sector", "region", "currency")
                    and r["verdict_type"] != "UNAVAILABLE"), key=lambda r: -(r["unclassified_gbp"] or 0))[:1]
    _iss = next((r.get("largest_issuer") for r in rows if r["dimension"] == "issuer" and r.get("largest_issuer")), None)
    line = ("Whole-ISA exposure coverage (X-Ray %s, book %s; basis %s): " % (as_of_x, as_of_p, basis.get("state"))
            + "; ".join("%s %s%%%s" % (r["dimension"], r["covered_pct"],
                                       "" if r["verdict_type"] == "COMPLETE" else " " + r["verdict_type"])
                        for r in rows if r["dimension"] in ("asset_class", "sector", "region", "currency", "country"))
            + ((" - unattributed £{:,.0f} ({}: X-Ray 'Other' asset mass, not zero)".format(worst[0]["unclassified_gbp"], worst[0]["dimension"]))
               if worst and worst[0]["unclassified_gbp"] >= 1 else "")
            + ("; largest issuer {} {}% (published look-through)".format(_iss.get("name"), _iss.get("pct")) if _iss else "")
            + ("; UNVALUED holdings (no price): %s" % ", ".join(unvalued_names) if unvalued_names else "")
            + "; liquidity UNAVAILABLE (B08-R10).")
    return {"schema": SCHEMA, "state": "OK" if basis.get("state") in ("MATCH", "UNVERIFIED") else "DEGRADED",
            "nav_gbp": round(nav, 2), "portfolio_as_of": as_of_p, "xray_as_of": as_of_x, "xray_basis": basis,
            "unvalued_holdings": unvalued_names, "rows": rows, "line": line,
            "conservation_ok": all(abs(r["expected_gbp"] - r["covered_gbp"] - r["unclassified_gbp"] - r["unvalued_gbp"]) < 1.0
                                   for r in rows)}


def _selftest() -> int:
    fails = []

    def ok(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)
    port = {"_meta": {"data_date": "02-Oct-2026"}, "summary": {"total_value_gbp": 100000.0},
            "stocks": [{"ticker": "AAA", "name": "AAA", "value_gbp": 10000.0}],
            "funds": [{"ticker": "FND", "name": "FUND ONE", "value_gbp": 90000.0}]}
    xr = {"_meta": {"report_date": "2 Oct 2026"},
          "asset_allocation": {"stocks": {"net_pct": 80.0}, "bonds": {"net_pct": 0.0}, "cash": {"net_pct": 10.0},
                               "other": {"net_pct": 10.0}, "not_classified": {"net_pct": 0.0}},
          "sector_weights": {"technology": {"portfolio_pct": 60.0}, "energy": {"portfolio_pct": 40.0}},
          "world_regions": {"united_states": {"equity_pct": 70.0, "is_leaf": True}, "japan": {"equity_pct": 30.0, "is_leaf": True}},
          "country_exposure": [{"country": "United States", "equity_pct": 70.0}, {"country": "Other", "equity_pct": 10.0}],
          "lookthrough_top10": {"holdings": [{"name": "Big Co", "weight_pct": 5.0}, {"name": "Small Co", "weight_pct": 1.0}]},
          "fund_holdings": [{"name": "FUND ONE", "weight_pct": 90.0}]}
    fmap = {"stocks": {"AAA": 1.0}, "funds": {"FND": {"fund_ai_share": 0.1, "fund_semis_share": 0.05}}}
    r = build(port, xr, fmap)
    rows = {x["dimension"]: x for x in r["rows"]}
    ok("B08-R01 conservation: covered + unclassified + unvalued = expected on every row", r["conservation_ok"])
    ok("B08-R02 MUST-FIRE: X-Ray 'Other' £10,000 is UNATTRIBUTED in asset_class, never zero",
       rows["asset_class"]["unclassified_gbp"] == 10000.0 and rows["asset_class"]["verdict_type"] == "LOWER_BOUND")
    ok("B08-R02: sector carries the same £10,000 as unattributed (not 'no sector')",
       rows["sector"]["unclassified_gbp"] == 10000.0)
    ok("B08-R01: a country 'Other' row is not a country (top table = LOWER_BOUND)",
       rows["country"]["verdict_type"] == "LOWER_BOUND" and rows["country"]["covered_gbp"] == 66000.0)
    ok("B08-R01: currency is a PROXY whose blocs + unattributed conserve NAV",
       rows["currency"]["verdict_type"] == "PROXY" and rows["currency"]["blocs_gbp"]["GBP"] == 10000.0)
    ok("B08-R01: issuer BOUNDED by the 10th published entry", rows["issuer"]["unlisted_issuer_max_pct"] == 1.0)
    ok("B08-R01: liquidity is UNAVAILABLE with an owner, never omitted", rows["liquidity"]["owner"].startswith("ISA-0842"))
    ok("X-Ray basis MATCH when implied base = NAV and dates agree", r["xray_basis"]["state"] == "MATCH")
    # NEGATIVE CONTROL: the X-Ray of a DIFFERENT book (implied base 2x NAV) is not used
    xr2 = dict(xr, fund_holdings=[{"name": "FUND ONE", "weight_pct": 45.0}])
    r2 = build(port, xr2, fmap)
    ok("NEGATIVE CONTROL: an X-Ray whose implied base is 2x the NAV is MISMATCH and every X-Ray row UNAVAILABLE",
       r2["xray_basis"]["state"] == "MISMATCH" and
       all(x["verdict_type"] == "UNAVAILABLE" for x in r2["rows"] if x["dimension"] in ("asset_class", "sector", "region", "issuer")))
    # NEGATIVE CONTROL: an unpriced holding is named UNVALUED
    p3 = json.loads(json.dumps(port)); p3["stocks"].append({"ticker": "NOPX", "value_gbp": None, "quantity": 5})
    ok("NEGATIVE CONTROL: a holding with no value is named UNVALUED in the line", "NOPX" in build(p3, xr, fmap)["line"])
    ok("no NAV -> UNAVAILABLE, never a 0% exposure", build({"summary": {}}, xr, fmap)["state"] == "UNAVAILABLE")
    # the consistency pair that guards the published verdicts (consistency_check.pair_exposure_verdicts_honest)
    try:
        import consistency_check as _cc
        _bad = _cc.pair_exposure_verdicts_honest({"factor_lookthrough": {"verdict": "OK", "cap_pct": 30.0, "upper_bound_pct": 35.0,
                                                                        "email_line": "x"}})
        ok("B08-R02 MUST-FIRE (pair): a factor OK whose upper bound exceeds the cap is an ERROR", len(_bad) == 1 and isinstance(_bad[0], str))
        _good = _cc.pair_exposure_verdicts_honest({"factor_lookthrough": {"verdict": "NOT_ESTABLISHED", "cap_pct": 30.0,
                                                                         "upper_bound_pct": 72.3, "email_line": "NOT ESTABLISHED"},
                                                   "exposure_coverage": r})
        ok("NEGATIVE CONTROL (pair): an honest NOT_ESTABLISHED verdict + a conserving ledger raise nothing", _good == [])
        _old = _cc.pair_exposure_verdicts_honest({"factor_lookthrough": {"email_line": "AI-complex effective weight: 22.3% vs 30% cap — OK"}})
        ok("B08-R02 (pair): a pre-P-B6 run_context (no verdict) is a WARN, never a pass",
           len(_old) == 1 and isinstance(_old[0], dict) and _old[0].get("severity") == "WARN")
        _nc = _cc.pair_exposure_verdicts_honest({"exposure_coverage": {"conservation_ok": False, "rows": [
            {"dimension": "sector", "expected_gbp": 100.0, "covered_gbp": 50.0, "unclassified_gbp": 0.0, "unvalued_gbp": 0.0}]}})
        ok("B08-R01 MUST-FIRE (pair): a ledger row that loses £50 of the book is named", len(_nc) == 1 and "sector" in _nc[0])
    except ImportError as _e:                                              # noqa: F841
        ok("consistency_check importable for the pair tests", False)
    print("exposure_coverage selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio")
    ap.add_argument("--xray")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(1 if _selftest() else 0)
    p = json.load(open(a.portfolio, encoding="utf-8"))
    x = json.load(open(a.xray, encoding="utf-8")) if a.xray else None
    print(json.dumps(build(p, x), indent=1))
