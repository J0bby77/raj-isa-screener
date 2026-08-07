#!/usr/bin/env python3
"""
fund_pair_test.py — is one holding actually better than a near-substitute? Tier-1 item 4.
Built 06-Aug-2026 to settle register H7 (RLGES vs VUAG) on evidence rather than on a window.

⚑ WHY A NEW TEST RATHER THAN ANOTHER OPINION
H7 says Royal London Global Equity Select is "strictly dominated" by Vanguard S&P 500 and is a
closet tracker: 17.73/13.77 against 18.01/13.37, correlation 0.91, ten times the fee. Every one
of those numbers is real. The verdict built from them is not, for two separate reasons that both
had to be found before the holding was sold.

**1. The dominance is a window choice wearing a verdict.** On the X-Ray's (inferred) three-year
basis VUAG wins on both axes exactly as H7 says. On five years of NAV history RLGES returns
15.82% against VUAG's 13.10% and the ranking REVERSES. RLGES's whole advantage sits in years four
and five. `fund_action_stack` already refuses to issue a dominance verdict when the windows
disagree; H7 predates that rule and was never restated.

**2. ⚑ THE FEE ARGUMENT DOUBLE-COUNTS, and it is the argument the register calls "stronger".**
"0.71% vs 0.07% at correlation 0.91, fee waste £62/yr" treats the OCF as a deduction still to
come. **Published NAV total returns are already net of the OCF.** RLGES's 15.82% is what an
investor actually received AFTER paying 0.71% a year; VUAG's 13.10% is after paying 0.07%. So the
£62 is not a further loss on top of the measured record — it is the hurdle RLGES has already
either cleared or failed, and the return comparison is the answer to whether it did. Subtracting
it again charges the same fee twice.

What the fee legitimately is: an ex-ante headwind, and a reason for a sceptical prior about the
active premium persisting. It is not evidence of destroyed value where the net return is higher.

WHAT THIS COMPUTES INSTEAD
The only question that survives both corrections: **is the excess return distinguishable from
noise?** Monthly excess, tracking error, information ratio and a t-statistic, per window, plus
the honest degrees of freedom. At a correlation of 0.91 the active part of RLGES is small, so the
right answer may well be "we cannot tell" — and saying that is the point. This does not issue a
verdict; it produces the decision pack.
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, statistics as st, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1
MIN_MONTHS = 24
# Two-sided 5% critical value. Normal rather than t because the months are not independent
# enough for the extra precision to mean anything -- the approximation is stated, not hidden.
T_CRIT_95 = 1.96


def _excess(a, b):
    da, db = dict(a), dict(b)
    common = sorted(set(da) & set(db))
    return [(k, da[k] - db[k]) for k in common]


def _ann_geom(monthly):
    p = 1.0
    for _, v in monthly:
        p *= (1.0 + v)
    yrs = len(monthly) / 12.0
    return (p ** (1.0 / yrs) - 1.0) * 100.0 if yrs > 0 else None


def category_declaration_status(universe=None):
    """⚑ Every fund whose peer-group block is still outstanding, with the exact one-line fix.

    Register L2: three redundancy pairs sit at UNKNOWN for want of a declared category, and
    "each is one declared block from a factsheet". That was true and it had been true for a
    while — because nothing named the funds. This does, every run."""
    import fund_performance as _fp
    universe = universe if universe is not None else _fp.load_universe()
    funds = universe.get("funds", universe)
    rows = []
    for sd, u in funds.items():
        if str(sd).startswith("_"):
            continue
        c = (u or {}).get("category") or {}
        basis = str(c.get("name_basis") or "")
        usable = bool(c.get("annualised_pct")) and basis != "inferred_from_mandate"
        rows.append({
            "sedol": sd, "fund": u.get("name"),
            "category_name": c.get("name"), "name_basis": basis or "absent",
            "usable_for_verdict": usable,
            "status": ("DECLARED" if usable else
                       "NAME_INFERRED_RETURNS_MISSING" if c.get("name") else "ABSENT"),
            "request": (None if usable else
                        (c.get("what_would_resolve_it")
                         or f"declare a `category` block for {u.get('name')} in "
                            f"fund_universe.json: peer-group name + trailing returns "
                            f"%(GBP) at 1y/3y/5y/10y + strike date + source")),
        })
    outstanding = [r for r in rows if not r["usable_for_verdict"]]
    return {"rows": rows, "n_declared": len(rows) - len(outstanding),
            "n_outstanding": len(outstanding), "of": len(rows),
            "consequence": ("a pair involving any outstanding fund can produce NO replacement "
                            "verdict. Absence BLOCKS a verdict rather than permitting one — but "
                            "it also means the redundancy test is currently unable to answer "
                            "the question it exists to answer for most of the sleeve.")}


def category_relative(u, years_label):
    """Each fund's trailing return minus its OWN peer group's, from the DECLARED category block.

    ⚑ WHY THIS EXISTS. The first version of this module reported JPM UK Equity Core as
    significantly worse than Artemis SmartGARP UK — corr 0.917, −10.57pp p.a. over five years,
    t −3.53, surviving multiple comparisons. Every number was right and the conclusion drawn
    from it was not: **JPM UK beats its own category at 1y (22.95 vs 18.24), 3y (16.77 vs
    14.04), 5y (12.06 vs 10.16) and 10y (9.11 vs 7.70).** It is a good fund. What the test had
    actually measured was that SmartGARP's value/GARP screen had an exceptional run.

    A high correlation makes two funds look substitutable. It does not make the winner of a
    style race the better manager. Without a peer-group anchor a pair test measures relative
    luck and reports it as skill — and would have put a fund that beats its benchmark at every
    horizon onto a redeploy agenda."""
    cat = (u or {}).get("category") or {}
    # ⚑ AN INFERRED PEER GROUP MAY NOT DECIDE ANYTHING (06-Aug-2026, L2).
    # Eleven funds now carry a `category.name` so that the request for the missing block can
    # name a specific peer group rather than ask an open question. Those names are INFERRED
    # FROM THE FUND'S MANDATE, and several are genuinely uncertain (is SmartGARP UK Large-Cap
    # or Flex-Cap? is M&G Asian "Asia ex-Japan" or "Asia-Pacific ex-Japan"?). Anchoring an
    # excess figure to a guessed peer group is the JPM UK defect wearing a new costume — a
    # correct calculation against the wrong reference — so an inferred name is usable for the
    # MESSAGE and never for the VERDICT.
    if str(cat.get("name_basis") or "") == "inferred_from_mandate":
        return (None, cat.get("name"), None, None)
    v = (cat.get("annualised_pct") or {}).get(years_label)
    return (v, cat.get("name"), cat.get("as_of"), cat.get("source")) if v is not None else \
           (None, cat.get("name"), None, None)


def evaluate_pair(a_monthly, b_monthly, a_name, b_name, years=None,
                  a_ocf=None, b_ocf=None, a_value_gbp=None, correlation=None,
                  a_universe=None, b_universe=None, years_label=None):
    """Excess / tracking error / information ratio for A relative to B."""
    ex = _excess(a_monthly, b_monthly)
    if years:
        ex = ex[-int(years * 12):]
    out = {"fund": a_name, "versus": b_name, "window_years": years,
           "n_months": len(ex), "correlation": correlation}
    if len(ex) < MIN_MONTHS:
        out["verdict"] = "INSUFFICIENT"
        out["reason"] = (f"{len(ex)} overlapping months, {MIN_MONTHS} required. A tracking error "
                         f"from fewer is a number without a distribution behind it.")
        return out
    aa = [v for k, v in a_monthly if k in dict(ex)]
    bb = [v for k, v in b_monthly if k in dict(ex)]
    e = [v for _, v in ex]
    ann_a = _ann_geom([(k, v) for k, v in a_monthly if k in dict(ex)])
    ann_b = _ann_geom([(k, v) for k, v in b_monthly if k in dict(ex)])
    te = st.pstdev(e) * math.sqrt(12) * 100.0
    yrs = len(e) / 12.0
    ann_excess = ann_a - ann_b
    ir = (ann_excess / te) if te else None
    tstat = (ir * math.sqrt(yrs)) if ir is not None else None
    out.update({
        "annualised_a_pct": round(ann_a, 2), "annualised_b_pct": round(ann_b, 2),
        "annualised_excess_pp": round(ann_excess, 2),
        "vol_a_pct": round(st.pstdev(aa) * math.sqrt(12) * 100.0, 2),
        "vol_b_pct": round(st.pstdev(bb) * math.sqrt(12) * 100.0, 2),
        "tracking_error_pct": round(te, 2),
        "information_ratio": (round(ir, 3) if ir is not None else None),
        "t_stat": (round(tstat, 2) if tstat is not None else None),
        "t_crit_95": T_CRIT_95,
        "months_beaten_pct": round(100.0 * sum(1 for v in e if v > 0) / len(e), 1),
        "significant_at_95": bool(tstat is not None and abs(tstat) >= T_CRIT_95),
    })
    out["verdict"] = (
        "A_BETTER_SIGNIFICANT" if (out["significant_at_95"] and ann_excess > 0) else
        "B_BETTER_SIGNIFICANT" if out["significant_at_95"] else
        "INDISTINGUISHABLE")
    head = (f"{a_name} returned {ann_a:.2f}% p.a. against {b_name}'s {ann_b:.2f}% over "
            f"{yrs:.1f} years — an excess of {ann_excess:+.2f}pp at a tracking error of "
            f"{te:.2f}%")
    if ir is None:
        # Zero tracking error means the two series differ by a constant every single month.
        # The information ratio is undefined, not infinite-and-therefore-conclusive, and a real
        # pair of funds never does this — so it is reported as a fixture condition, not a result.
        out["verdict"] = "DEGENERATE"
        out["reading"] = (head + ". Tracking error is exactly zero, so the information ratio is "
                          "undefined — the two series differ by a fixed amount every month, "
                          "which does not happen to real funds and indicates a synthetic or "
                          "duplicated input.")
        return out
    out["reading"] = (
        head + f", so an information ratio of {ir:.2f} and t = {tstat:+.2f}. "
        + ("That clears the 1.96 threshold, so the difference is unlikely to be chance."
           if out["significant_at_95"] else
           f"That is well inside ±{T_CRIT_95}, so on this window the two are NOT "
           f"distinguishable — the excess is consistent with noise. Note this cuts BOTH ways: "
           f"it is equally not evidence that {a_name} is worse."))

    # ── ⚑ THE CATEGORY GUARD — a relative loss is not an absolute fault ─────────────────
    yl = years_label or (f"{int(years)}y" if years else None)
    ca, cname_a, cas_a, csrc_a = category_relative(a_universe, yl)
    cb, cname_b, _, _ = category_relative(b_universe, yl)
    cat = {"window": yl, "a_category": cname_a, "b_category": cname_b}
    if ca is not None:
        cat.update({"a_category_return_pct": ca,
                    "a_excess_over_own_category_pp": round(ann_a - ca, 2),
                    "a_beats_own_category": bool(ann_a >= ca),
                    "as_of": cas_a, "source": csrc_a})
    if cb is not None:
        cat.update({"b_category_return_pct": cb,
                    "b_excess_over_own_category_pp": round(ann_b - cb, 2),
                    "b_beats_own_category": bool(ann_b >= cb)})
    if ca is None:
        cat["status"] = "UNKNOWN"
        cat["reason"] = (f"no declared category for {a_name}. A replacement verdict CANNOT be "
                         f"issued without it — the pair test alone cannot tell a poor fund from "
                         f"a fund that lost a style race.")
        cat["what_would_resolve_it"] = "a `category` block in fund_universe.json, stamped"
    elif cat.get("a_beats_own_category"):
        cat["status"] = "STYLE_DIFFERENCE"
        cat["reason"] = (
            f"{a_name} BEATS its own peer group ({cname_a}) by "
            f"{cat['a_excess_over_own_category_pp']:+.2f}pp over {yl}. It is not underperforming; "
            f"it lost a race against a different mandate. The gap to {b_name} is a STYLE and "
            f"ASSET-CLASS decision, not a manager-quality one, and this pair is NOT a "
            f"replacement candidate on these grounds.")
        if out["verdict"] == "B_BETTER_SIGNIFICANT":
            out["verdict_before_category_guard"] = out["verdict"]
            out["verdict"] = "B_BETTER_BUT_A_BEATS_ITS_CATEGORY"
    else:
        cat["status"] = "UNDERPERFORMS_OWN_CATEGORY"
        cat["reason"] = (f"{a_name} trails its own peer group ({cname_a}) by "
                         f"{cat['a_excess_over_own_category_pp']:+.2f}pp over {yl} AS WELL AS "
                         f"trailing {b_name}. Two independent references agree, which is what a "
                         f"replacement verdict requires.")
    out["category_check"] = cat

    # ── the fee, framed correctly ───────────────────────────────────────────────────────
    if a_ocf is not None and b_ocf is not None:
        gap = a_ocf - b_ocf
        out["fee"] = {
            "a_ocf_pct": a_ocf, "b_ocf_pct": b_ocf, "gap_pct": round(gap, 3),
            "annual_gbp_on_current_holding": (round(gap / 100.0 * a_value_gbp, 2)
                                              if a_value_gbp else None),
            "already_net_of_fees_in_the_returns": True,
            "framing": (
                f"the returns above are NAV TOTAL RETURNS and are already NET of both OCFs. "
                f"{a_name} delivered {ann_a:.2f}% AFTER paying {a_ocf}% a year; {b_name} "
                f"delivered {ann_b:.2f}% after paying {b_ocf}%. The "
                f"£{gap / 100.0 * a_value_gbp:,.0f}/yr fee gap is therefore NOT a further "
                f"deduction from the {ann_excess:+.2f}pp above — it is the hurdle that figure "
                f"already reflects. Counting it twice is what turns a close call into an "
                f"apparently obvious one." if a_value_gbp else
                "the returns above are net of both OCFs, so the fee gap is not a further "
                "deduction from the excess."),
            "what_it_legitimately_argues": (
                f"a {gap:.2f}pp annual headwind must be overcome by gross skill EVERY year, and "
                f"the base rate for that persisting is poor. It is a reason for a sceptical "
                f"prior about the excess repeating — not evidence that value was destroyed in a "
                f"period when the net return was higher."),
        }
    return out


def h7_decision_pack(as_of=None, portfolio=None, xray=None):
    """The RLGES-vs-VUAG pack: every window, both sources, and the fee stated correctly."""
    import fund_action_stack as fas, fund_performance as fp
    as_of = as_of or dt.date.today()
    uni = fp.load_universe()
    A, B = "BF93W97", "VUAG"                       # RLGES, Vanguard S&P 500

    def series(sedol):
        u = uni[sedol]
        return fas._monthly_returns(fp.fetch_nav_history(u["yf_symbol"], use_cache=True,
                                                         scale=fp._scale_for(u)))
    ma, mb = series(A), series(B)
    corr_nav = fas._corr(ma, mb)

    # the X-Ray's own correlation — a genuinely independent second derivation
    corr_xray = None
    if xray:
        for k, v in ((xray.get("correlation_matrix") or {}).get("pairs") or {}).items():
            if "ROYAL LONDON" in k and "VANGUARD S&P" in k:
                corr_xray = v
    val = next((f.get("value_gbp") for f in ((portfolio or {}).get("funds") or [])
                if f.get("ticker") == A), None)

    windows = {}
    for label, yrs in (("3y", 3), ("5y", 5), ("full", None)):
        windows[label] = evaluate_pair(ma, mb, uni[A]["name"], uni[B]["name"], yrs,
                                       uni[A].get("ocf"), uni[B].get("ocf"), val,
                                       corr_nav, uni[A], uni[B], label)

    # the X-Ray's 3y Mean / Std Dev table — the exact figures H7 quotes
    xstats = {}
    if xray:
        hs = ((xray.get("holdings_statistics") or {}).get("funds") or {})
        for nm, v in hs.items():
            if nm.startswith("ROYAL LONDON"):
                xstats["rlges"] = v
            if nm.startswith("VANGUARD S&P"):
                xstats["vuag"] = v

    # ── what the SWITCH would actually do, beyond the return comparison ─────────────────
    # ⚑ "Closet tracker" invites the conclusion that the two are interchangeable. They are not:
    # RLGES is a GLOBAL mandate and VUAG is US large-cap. A 0.91 correlation over a period in
    # which US equities led the world is partly a fact about the period. Switching is therefore
    # not a like-for-like fee saving; it is also a geographic and factor decision, and the H9
    # gate built for exactly this reason must be applied to it.
    switch = {"applicable": bool(val)}
    if val:
        try:
            import factor_lookthrough as flt
            fmap = flt.load_map()
            base = flt.compute(portfolio or {}, fmap)
            sa = float(((fmap.get("funds") or {}).get(A) or {}).get("fund_ai_share") or 0)
            sb = float(((fmap.get("funds") or {}).get(B) or {}).get("fund_ai_share") or 0)
            tot = ((portfolio or {}).get("summary") or {}).get("total_value_gbp") or 0
            after = ((base["effective_gbp"] - val * sa + val * sb) / tot * 100.0) if tot else None
            switch.update({
                "ai_complex_before_pct": base["ai_complex_effective_weight_pct"],
                "ai_complex_after_pct": (round(after, 2) if after else None),
                "ai_complex_delta_pp": (round(after - base["ai_complex_effective_weight_pct"], 2)
                                        if after else None),
                "cap_pct": base["cap_pct"],
                "h9_note": (f"the switch moves £{val:,.0f} from a {sa:.0%}-AI mandate to a "
                            f"{sb:.0%}-AI one. This is the H9 test applied to a SELL-and-BUY "
                            f"rather than to new money — the same act.")})
        except Exception as _e:                                     # noqa: BLE001
            switch["error"] = f"{type(_e).__name__}: {_e}"
        if xray:
            reg = (xray.get("world_regions") or {})
            switch["geography"] = {
                "regions_vs_benchmark": reg,
                "note": ("RLGES is a GLOBAL mandate; VUAG is US large-cap. The X-Ray shows the "
                         "portfolio at 42.52% Americas against a 70.19% benchmark, so it is "
                         "already UNDERWEIGHT the US. Switching adds to the US and removes a "
                         "global sleeve — a real allocation change that the 'closet tracker' "
                         "framing conceals, because 0.91 over a period of US leadership is "
                         "partly a fact about the period.")}
        switch["dealing_cost_gbp"] = {
            "estimate": 6.5,
            "basis": ("observed in transaction_ledger.json: fund deals settled at £1.47-£1.50 "
                      "and ETF deals at £5.00. A sell of an OEIC plus a buy of an ETF is "
                      "~£1.50 + £5.00. Stated from the dealing record, not from a rate card."),
            "as_share_of_holding_pct": round(6.5 / val * 100, 3)}

    verdicts = {k: v.get("verdict") for k, v in windows.items()}
    consistent = len(set(v for v in verdicts.values() if v != "INSUFFICIENT")) == 1
    return {
        "schema_version": SCHEMA_VERSION,
        "item": "register H7 — Royal London Global Equity Select vs Vanguard S&P 500",
        "as_of": as_of.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "holding_value_gbp": val,
        "correlation": {"from_nav_monthly": corr_nav, "from_xray_matrix": corr_xray,
                        "agree": (None if corr_xray is None or corr_nav is None
                                  else abs(corr_nav - corr_xray) <= 0.10),
                        "note": ("two independent derivations: monthly NAV returns computed here, "
                                 "and the X-Ray's own printed matrix. The X-Ray figure was "
                                 "quoted in H7 for a month before anything parsed the table it "
                                 "sits in — and the row it sits on was one place away from being "
                                 "read as MI Thornbridge's.")},
        "xray_3y_statistics": xstats,
        "switch_consequences": switch,
        "windows": windows,
        "verdict_consistent_across_windows": consistent,
        "h7_as_written": {
            "claim_1_strictly_dominated": {
                "status": "WINDOW-CONDITIONAL — not a dominance verdict",
                "detail": ("VUAG dominates on the X-Ray's inferred 3-year basis and RLGES leads "
                           "on 5 years of NAV history. `fund_action_stack` issues no dominance "
                           "verdict unless both windows agree; on live data it issues none for "
                           "this pair and records it in dominance_window_conflicts.")},
            "claim_2_closet_tracker": {
                "status": "SUPPORTED as a description, INSUFFICIENT as a reason to sell",
                "detail": ("correlation 0.91 means most of what RLGES does, VUAG also does. That "
                           "makes the marginal diversification value low and is already scored "
                           "in the FRS. It does not establish that the residual is worthless — "
                           "that is what the information ratio above tests.")},
            "claim_3_fee_waste_62_per_year": {
                "status": "⚑ DOUBLE-COUNTS — the register calls this the STRONGER argument, and "
                          "it is the weaker one",
                "detail": ("NAV total returns are net of the OCF. The £62 is already inside every "
                           "return comparison above; deducting it again charges the same fee "
                           "twice. What survives is a prior: a 0.64pp annual headwind has to be "
                           "beaten by gross skill every year, and usually is not.")},
        },
        "decision_for_raj": {
            "question": ("Is Royal London Global Equity Select earning the place it occupies "
                         f"(£{val:,.0f}, {'%.1f' % (val / 139738 * 100) if val else '?'}% of the "
                         "ISA) beside a Vanguard S&P 500 holding it moves with 0.91?"),
            "what_the_evidence_supports": "see windows[] — stated, not summarised into a verdict",
            "what_is_NOT_evidence": [
                "the £62/yr fee gap as an additional loss — it is already in the net returns",
                "a dominance verdict from either window alone — they disagree",
                "a 0.91 correlation as proof the two are interchangeable — RLGES is a GLOBAL "
                "mandate and VUAG is US large-cap; the correlation is partly a fact about a "
                "period of US leadership, and the switch is an allocation decision too",
            ],
            "not_escalated": ("no Category 7 recommendation is issued. A verdict that would sell "
                              "a holding must survive both windows and a correctly-framed fee "
                              "argument, and this one does not yet."),
        },
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=os.path.join(HERE, "portfolio_data_aug_2026.json"))
    ap.add_argument("--xray", default=os.path.join(HERE, "xray_data_aug_2026.json"))
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    pf = json.load(open(a.portfolio, encoding="utf-8")) if os.path.exists(a.portfolio) else {}
    xr = json.load(open(a.xray, encoding="utf-8")) if os.path.exists(a.xray) else {}
    r = h7_decision_pack(dt.date.fromisoformat(a.as_of) if a.as_of else None, pf, xr)
    if a.out:
        json.dump(r, open(a.out, "w", encoding="utf-8"), indent=1, default=str)
    print(json.dumps(r, indent=1, default=str))
