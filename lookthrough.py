#!/usr/bin/env python3
"""
lookthrough.py — register H9 + H10. Tier-1 item 2, built 06-Aug-2026.

TWO DEFECTS, ONE MISSING CAPABILITY
-----------------------------------
**H10 — the overlap check was hand-computed and wrong.** `portfolio_analytics.
build_overlap_check_structure` handed Claude an instruction: look each stock up in each fund's
top ten, multiply, add to the direct weight. On the August run that produced **AVGO 4.04%**, and
it was wrong — it missed MI Thornbridge's 3.84% position in Broadcom. The X-Ray publishes the
answer, **4.31%**, computed by Morningstar over every fund's actual underlying holdings. A
hand-calc standing in for a machine-readable source is not a fallback, it is a second source of
truth that will drift, and this one already had.

**H9 — nothing tested what putting money INTO a fund does to the look-through.** Overlap ran one
way only: stock -> fund. So the August report could decline ASML and IESC to avoid adding to the
AI complex and, in the same document, recommend Royal London Global Equity Select — whose top
five are NVDA/GOOGL/MSFT/AMZN/TSMC at ~28.8% — which would have added roughly 0.8pp to the very
concentration it was avoiding. Both decisions were defensible alone. Together they cancel, and
nothing in the framework could see the pair.

⚑ WHAT THIS DOES NOT PRETEND TO KNOW
Per-fund name-level holdings are not available to the framework: the X-Ray publishes the
PORTFOLIO's top ten underlying names, never each fund's. So the name-level marginal test returns
UNKNOWN with the reason, and the FACTOR-level test — which runs on the declared shares in
`factor_map.json` — does the work. Inventing plausible fund holdings to fill the gap would
produce exactly the confident-wrong-number this register is a list of.

⚑ AND WHERE "UNKNOWN" CAN BE TURNED INTO A BOUND, IT IS
A name absent from a top-TEN table is not unknown-without-limit: it must weigh less than the
tenth entry. On August data that is 0.72%, which resolves the 5% overlap question decisively for
every direct holding without needing a single fund's holdings list. An upper bound derived from
the table's own structure is evidence; a guess dressed as an estimate is not.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1
OVERLAP_FLAG_PCT = 5.0          # combined effective weight in one issuer
DEFAULT_AI_CAP_PCT = 30.0


def _cap():
    try:
        import scoring_config as c
        return float(getattr(c, "FACTOR_AI_SOFT_CAP_PCT", DEFAULT_AI_CAP_PCT))
    except Exception:
        return DEFAULT_AI_CAP_PCT


# ── name normalisation: issuer identity across a broker ticker and a Morningstar row ────
_SUFFIX = (" plc", " inc", " corp", " corporation", " ltd", " limited", " co", " company",
           " sa", " nv", " ag", " holdings", " holding", " group", " class a", " class b",
           " ordinary shares", " ord", " technologies", " technology")


def _norm(name):
    s = " ".join(str(name or "").lower().replace(".", " ").replace(",", " ").split())
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIX:
            if s.endswith(suf):
                s, changed = s[: -len(suf)].strip(), True
    return s


def match_holding(stock, lt_rows):
    """Join a direct holding to a look-through row. Requires the normalised issuer names to be
    EQUAL or one to contain the other as a whole prefix — never a token-overlap score.

    Token overlap is what joined "Vanguard Jpn Stk Idx GBP Acc" to "VANGUARD S&P 500 ETF USD ACC
    GBP" in the fund cross-check and published the false pair as a verdict. A mis-joined overlap
    would understate or overstate a concentration in exactly the same way, so the same rule
    applies: an ambiguous match is NO match, and it says so."""
    a = _norm(stock.get("name"))
    cands = []
    for r in lt_rows:
        b = _norm(r["name"])
        if a == b or (a and b and (a.startswith(b + " ") or b.startswith(a + " "))):
            cands.append(r)
    if len(cands) == 1:
        return cands[0], "exact_issuer_name"
    if not cands:
        return None, "not_in_top10"
    return None, f"ambiguous — {len(cands)} look-through rows match {stock.get('name')!r}"


# ── H10 ─────────────────────────────────────────────────────────────────────────────────
def overlap_check(portfolio, xray):
    """Published look-through concentration per direct holding. Replaces the hand-calc."""
    lt = (xray or {}).get("lookthrough_top10") or {}
    rows = lt.get("holdings") or []
    out = {"as_of": ((xray or {}).get("_meta") or {}).get("report_date"),
           "source": "AJ Bell / Morningstar X-Ray, Top 10 Underlying Holdings",
           "flag_threshold_pct": OVERLAP_FLAG_PCT, "checks": [], "flags": [],
           "top10_covered_pct": lt.get("covered_pct"),
           "table_floor_pct": (min(r["weight_pct"] for r in rows) if rows else None)}
    if not rows:
        out["status"] = "UNAVAILABLE"
        out["note"] = ("the X-Ray look-through table did not parse. The check is REPORTED AS "
                       "ABSENT — it is not silently replaced by the hand-calculation it exists "
                       "to retire, which produced AVGO 4.04% against a published 4.31%.")
        return out
    out["status"] = "OK"
    floor = out["table_floor_pct"]
    for s in (portfolio.get("stocks") or []):
        hit, how = match_holding(s, rows)
        direct = s.get("weight_pct")
        rec = {"ticker": s.get("ticker"), "name": s.get("name"),
               "direct_weight_pct": direct, "match": how}
        if hit:
            rec.update({
                "lookthrough_total_pct": hit["weight_pct"],
                "via_funds_pct": (round(hit["weight_pct"] - (direct or 0), 2)
                                  if direct is not None else None),
                "basis": "published",
                "exceeds_flag": bool(hit["weight_pct"] > OVERLAP_FLAG_PCT),
                "note": ("the published figure is the TOTAL across direct and fund holdings, so "
                         "the fund contribution is the residual — it is not added to the direct "
                         "weight a second time")})
        else:
            # ⚑ a bound, not a shrug
            ub = round((direct or 0) + floor, 2)
            rec.update({
                "lookthrough_total_pct": None, "basis": "bounded",
                "upper_bound_pct": ub,
                "exceeds_flag": bool(ub > OVERLAP_FLAG_PCT),
                "note": (f"absent from a top-TEN table, so its total look-through weight is "
                         f"strictly below the tenth entry ({floor}%). Upper bound "
                         f"{ub}% — {'ABOVE' if ub > OVERLAP_FLAG_PCT else 'below'} the "
                         f"{OVERLAP_FLAG_PCT}% flag, decided without needing any fund's holdings "
                         f"list.")})
        out["checks"].append(rec)
        if rec["exceeds_flag"]:
            out["flags"].append(rec)
    out["summary"] = {
        "n_checked": len(out["checks"]),
        "resolved_from_published_table": sum(1 for c in out["checks"]
                                             if c["basis"] == "published"),
        "resolved_by_bound": sum(1 for c in out["checks"] if c["basis"] == "bounded"),
        "unresolved": sum(1 for c in out["checks"] if c["match"].startswith("ambiguous")),
        "flags": len(out["flags"])}
    return out


# ── H9 ──────────────────────────────────────────────────────────────────────────────────
def marginal_allocation(sedol, amount_gbp, portfolio, factor_map=None, xray=None,
                        fund_name=None):
    """What does putting `amount_gbp` into fund `sedol` do to the portfolio's factor exposure?"""
    import factor_lookthrough as flt
    factor_map = factor_map if factor_map is not None else flt.load_map()
    before = flt.compute(portfolio, factor_map)
    total = (portfolio.get("summary") or {}).get("total_value_gbp") or 0.0
    fm = (factor_map.get("funds") or {}).get(sedol)
    out = {"sedol": sedol, "name": fund_name, "amount_gbp": amount_gbp,
           "cap_pct": before["cap_pct"], "before_pct": before["ai_complex_effective_weight_pct"]}

    if fm is None or fm.get("fund_ai_share") is None:
        out.update({
            "verdict": "UNKNOWN", "after_pct": None, "delta_pp": None,
            "reason": (f"{sedol} has no declared `fund_ai_share` in factor_map.json, so the "
                       f"marginal effect on the AI-complex concentration cannot be computed. "
                       f"UNKNOWN blocks the allocation from being blessed; it does not silently "
                       f"pass as zero — a fund with no declared share is exactly where an "
                       f"unnoticed concentration would enter."),
            "what_would_resolve_it": f"one line in factor_map.json: funds.{sedol}.fund_ai_share"})
        return out

    share = float(fm["fund_ai_share"])
    eff_after = before["effective_gbp"] + amount_gbp * share
    total_after = total + amount_gbp        # new money; a switch is modelled by the caller
    after = round(eff_after / total_after * 100.0, 2) if total_after else None
    delta = round(after - (before["ai_complex_effective_weight_pct"] or 0), 2) if after else None
    cap = before["cap_pct"]
    in_breach_before = bool(before["breach"])
    in_breach_after = bool(after is not None and after > cap)

    if delta is not None and delta <= 0:
        verdict, why = "PASS", (f"this allocation LOWERS the AI-complex weight by {abs(delta)}pp "
                                f"— the fund's {share:.0%} share is below the portfolio's "
                                f"{before['ai_complex_effective_weight_pct']}%")
    elif in_breach_before:
        verdict, why = "BLOCK", (f"the AI-complex weight is already in breach "
                                 f"({before['ai_complex_effective_weight_pct']}% vs a {cap:.0f}% "
                                 f"cap) and this allocation adds {delta}pp more. Checkpoint-D "
                                 f"blocks a BUY that raises a factor while in breach; a fund "
                                 f"allocation is the same act with a wrapper on it.")
    elif in_breach_after:
        verdict, why = "BLOCK", (f"this allocation CROSSES the cap: "
                                 f"{before['ai_complex_effective_weight_pct']}% -> {after}% "
                                 f"against {cap:.0f}%")
    else:
        verdict, why = "FLAG", (f"adds {delta}pp of AI-complex exposure "
                                f"({before['ai_complex_effective_weight_pct']}% -> {after}%, cap "
                                f"{cap:.0f}%). Permitted, but it must be STATED in the "
                                f"recommendation — the August report declined two direct AI names "
                                f"and recommended a fund that would have added the same exposure, "
                                f"and neither decision could see the other.")
    out.update({"fund_ai_share": share, "after_pct": after, "delta_pp": delta,
                "verdict": verdict, "reason": why,
                "headroom_pp_before": (round(cap - before["ai_complex_effective_weight_pct"], 2)
                                       if before["ai_complex_effective_weight_pct"] is not None
                                       else None),
                "coverage_caveat": before["coverage_note"],
                "unclassified_funds": before["unclassified"]})

    # semis is the second declared factor and the one that re-priced MU
    semis = before.get("semis") or {}
    ssh = (fm or {}).get("fund_semis_share")
    if semis.get("semis_complex_pct") is not None and ssh is not None:
        s_after = round((semis["semis_complex_pct"] / 100.0 * total + amount_gbp * float(ssh))
                        / total_after * 100.0, 2) if total_after else None
        out["semis"] = {"before_pct": semis["semis_complex_pct"], "after_pct": s_after,
                        "delta_pp": (round(s_after - semis["semis_complex_pct"], 2)
                                     if s_after is not None else None),
                        "watch": semis.get("watch")}
    elif semis.get("semis_complex_pct") is not None:
        out["semis"] = {"before_pct": semis["semis_complex_pct"], "after_pct": None,
                        "reason": f"no declared fund_semis_share for {sedol}"}

    # ── name-level: now answerable WHERE DECLARED, bounded everywhere else ───────────
    # The store closes register H9's name-level gap for the funds that have an entry. It does
    # NOT pretend to close it for the rest: a fund with no declared holdings is UNKNOWN, and a
    # fund with PARTIAL coverage yields a BOUND — because "we recorded 3.84% of this fund" and
    # "this fund holds 3.84% of that name and nothing else relevant" are different statements,
    # and only the first one is true.
    decl = _declared_holdings()
    dh = (decl.get("funds") or {}).get(sedol)
    if dh:
        cov = float(dh.get("coverage_pct") or 0.0)
        priced = [h for h in (dh.get("holdings") or []) if h.get("weight_pct") is not None]
        named = [h.get("ticker") or h.get("name") for h in (dh.get("holdings") or [])]
        out["name_level"] = {
            "status": "DECLARED_PARTIAL" if cov < 99.0 else "DECLARED",
            "as_of": dh.get("as_of"), "source": dh.get("source"),
            "coverage_pct_of_fund": cov, "coverage_basis": dh.get("coverage_basis"),
            "names_declared": named,
            "marginal_effect_pp": ({h.get("ticker") or h.get("name"):
                                    round((amount_gbp / total) * float(h["weight_pct"]) / 100.0 * 100.0, 4)
                                    for h in priced}
                                   if (amount_gbp and total) else None),
            "bound_note": (f"only {cov:.2f}% of this fund is declared. Any name NOT listed could "
                           f"still sit inside the undeclared {100.0 - cov:.2f}%, so a zero here "
                           f"means NOT RECORDED, never NOT HELD."),
            "weights_missing_for": [h.get("ticker") or h.get("name")
                                    for h in (dh.get("holdings") or [])
                                    if h.get("weight_pct") is None],
        }
        return out
    out["name_level"] = {
        "status": "UNKNOWN",
        "reason": ("the X-Ray publishes the PORTFOLIO's top ten underlying holdings, never each "
                   "fund's. Without per-fund holdings the name-level marginal effect cannot be "
                   "computed, and estimating it would be inventing the data."),
        "store": "fund_holdings_declared.json (built 06-Aug-2026) — this fund has no entry",
        "what_would_resolve_it": ("a declared per-fund top-10 store (manager factsheet, stamped "
                                  "with as_of and source) — the same declared-not-inferred "
                                  "pattern as fund_universe.xray_name"),
        "partial_evidence": ("the portfolio-level table below shows where the concentration "
                             "already sits, which bounds how much any single allocation can "
                             "move it")}
    if xray:
        out["name_level"]["portfolio_top10"] = ((xray.get("lookthrough_top10") or {})
                                                .get("holdings") or [])[:10]
    return out


_DECL_CACHE = {}


def _declared_holdings(path=None):
    path = path or os.path.join(HERE, "fund_holdings_declared.json")
    if path not in _DECL_CACHE:
        try:
            with open(path, encoding="utf-8") as f:
                _DECL_CACHE[path] = json.load(f)
        except Exception:                                      # noqa: BLE001
            _DECL_CACHE[path] = {"funds": {}}
    return _DECL_CACHE[path]


def declaration_status(universe=None):
    """Which funds still have no declared holdings — named every run, not remembered."""
    import fund_performance as _fp
    universe = universe if universe is not None else _fp.load_universe()
    funds = universe.get("funds", universe)
    decl = (_declared_holdings().get("funds") or {})
    rows = []
    for sd, u in funds.items():
        if str(sd).startswith("_"):
            continue
        e = decl.get(sd)
        cov = float((e or {}).get("coverage_pct") or 0.0)
        rows.append({"sedol": sd, "fund": u.get("name"),
                     "status": ("ABSENT" if not e else
                                "DECLARED" if cov >= 99.0 else "PARTIAL"),
                     "coverage_pct": cov if e else None,
                     "as_of": (e or {}).get("as_of")})
    return {"rows": rows,
            "n_absent": sum(1 for r in rows if r["status"] == "ABSENT"),
            "n_partial": sum(1 for r in rows if r["status"] == "PARTIAL"),
            "of": len(rows),
            "request": ("for each ABSENT fund, one manager factsheet: top 10 by weight with the "
                        "strike date, into fund_holdings_declared.json. Until then the H9 "
                        "name-level test cannot run for that fund, and its absence BLOCKS a "
                        "blessing rather than permitting one.")}


def assess_candidates(portfolio, stack=None, amount_gbp=None, factor_map=None, xray=None):
    """Run the H9 test for every fund the action stack would allow new money into, BEFORE any
    recommendation is written. A gate consulted after the recommendation is a rationalisation."""
    amount_gbp = amount_gbp or 1000.0
    elig = [r for r in ((stack or {}).get("fund_retention_score") or [])
            if r.get("band") == "HOLD/ADD"]
    res = [marginal_allocation(r["sedol"], amount_gbp, portfolio, factor_map, xray, r.get("name"))
           for r in elig]
    return {"probe_amount_gbp": amount_gbp,
            "n_eligible_by_frs": len(elig),
            "blocked": [r["sedol"] for r in res if r["verdict"] == "BLOCK"],
            "flagged": [r["sedol"] for r in res if r["verdict"] == "FLAG"],
            "unknown": [r["sedol"] for r in res if r["verdict"] == "UNKNOWN"],
            "passed": [r["sedol"] for r in res if r["verdict"] == "PASS"],
            "assessments": res,
            "note": ("the probe amount only scales the deltas; the verdicts turn on the fund's "
                     "declared factor share against the current headroom")}


def build(portfolio_path=None, xray_path=None, stack_path=None, amount_gbp=None, out_path=None):
    portfolio_path = portfolio_path or os.path.join(HERE, "portfolio_data_aug_2026.json")
    xray_path = xray_path or os.path.join(HERE, "xray_data_aug_2026.json")
    stack_path = stack_path or os.path.join(HERE, "fund_action_stack_aug_2026.json")
    pf = json.load(open(portfolio_path, encoding="utf-8"))
    xr = json.load(open(xray_path, encoding="utf-8")) if os.path.exists(xray_path) else {}
    st = json.load(open(stack_path, encoding="utf-8")) if os.path.exists(stack_path) else {}
    res = {"schema_version": SCHEMA_VERSION,
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "portfolio_source": os.path.basename(portfolio_path),
           "xray_source": os.path.basename(xray_path) if xr else None,
           "h10_overlap_check": overlap_check(pf, xr),
           "h9_marginal_allocation": assess_candidates(pf, st, amount_gbp, None, xr)}
    if out_path:
        json.dump(res, open(out_path, "w", encoding="utf-8"), indent=1, default=str)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio"); ap.add_argument("--xray"); ap.add_argument("--stack")
    ap.add_argument("--amount", type=float, default=None); ap.add_argument("--out")
    a = ap.parse_args()
    print(json.dumps(build(a.portfolio, a.xray, a.stack, a.amount, a.out), indent=1, default=str))
