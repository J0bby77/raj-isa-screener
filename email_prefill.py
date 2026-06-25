#!/usr/bin/env python3
"""
email_prefill.py  --  ISA Review Email JSON Pre-populator
Version: 2.0  |  2026-05-31

Reads portfolio_data, analytics_data, xray_data, and watchlist_scored JSONs and
pre-populates an email_data_mmm_yyyy.json. Called by monthly_isa_prerun.py.

What this script fills (deterministic data only):
  meta             — run date, data date, tax year month
  s5_watchlist     — ranked watchlist table rows with scores, entry levels, in-window flags
                     (from watchlist_scored.s5_watchlist_rows — quantitative fields pre-filled)
  s6_portfolio_snapshot  — KPI cards, holdings table, performance table (returns from xray)
  s7_stock_sleeve  — holdings table rows with current metrics from watchlist_scored.s7_sleeve_rows
  s8_fund_review   — fund table rows with drift/target/signal columns pre-filled
  s10_tax_tracker  — ISA allowance KPI cards and contribution table

What Claude fills at runtime (judgment-dependent):
  s1_decision_summary    — action decision and rationale
  s2_capital_allocation  — ranked action categories
  s3_investment_cases    — full investment case(s) — quantitative scorecard PRE-POPULATED
                           from watchlist_scored.s3_case_skeletons; Claude fills narrative paragraphs only
  s4_liquidation_tracker — liquidation decisions with reasoning
  s5 detail_items        — thesis paragraph for top 3 watchlist names
  s8 fund paragraphs     — Step 8A narrative (estimated returns, overlap, regime tilt)
  s9_macro               — macro and geopolitical context
  s11_retrospective      — lessons and improvements

The output is a COMPLETE template file. Claude fills [Claude fills] placeholders and
then calls build_monthly_isa_email.py.
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STANDING_ORDER = 1250.0

# Tax year 26/27 start
TAX_YEAR_START = date(2026, 4, 6)
TAX_YEAR_ANNUAL = 20000.0
TAX_YEAR_LABEL  = "2026/27"

# MS rating integer → star string for email
def ms_stars_str(rating) -> str:
    if rating is None:
        return "—"
    try:
        n = int(rating)
        return "★" * n
    except (TypeError, ValueError):
        return str(rating)


# ---------------------------------------------------------------------------
# Tax year helpers
# ---------------------------------------------------------------------------
def calc_tax_year_month(run_date: date) -> str:
    """Returns 'Month N' for the current month within tax year 26/27."""
    if run_date < TAX_YEAR_START:
        return "Pre-tax year"
    months = (
        (run_date.year - TAX_YEAR_START.year) * 12
        + run_date.month - TAX_YEAR_START.month
        + 1
    )
    return f"Month {min(months, 12)}"


def calc_allowance_used(portfolio: dict) -> float:
    """
    Estimate total ISA allowance used in tax year 26/27.
    This is the total cost basis added since 6 Apr 2026 — we approximate
    from the portfolio total cost minus prior-year cost.
    Conservative approach: use portfolio summary total_value minus cash as proxy.
    Claude should verify the exact figure from AJ Bell at runtime.
    """
    # Best proxy: total invested value - cash
    return portfolio["summary"]["total_value_gbp"] - portfolio["summary"]["cash_effective_gbp"]


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def build_meta(portfolio: dict, run_date: date) -> dict:
    run_month   = portfolio["_meta"]["run_month"]
    data_date   = portfolio["_meta"]["data_date"]
    tax_month   = calc_tax_year_month(run_date)

    # run_month is e.g. "May 2026" → format as "Saturday 1 May 2026"
    month_label = portfolio["_meta"]["month_label"]  # e.g. "may_2026"
    parts = month_label.split("_")
    month_abbr = parts[0].capitalize() if parts else run_month

    return {
        "run_date_display": run_date.strftime("%A %-d %B %Y"),
        "data_date":        data_date,
        "tax_year":         TAX_YEAR_LABEL,
        "tax_year_month":   tax_month,
        "broker":           "AJ Bell (ACB8G2I)",
        "run_month_label":  run_month,
    }


def build_s6(portfolio: dict, analytics: dict, xray: dict) -> dict:
    """Section 6 — Portfolio Snapshot."""
    s = portfolio["summary"]
    total = s["total_value_gbp"]
    cash_eff = s["cash_effective_gbp"]
    stock_pct = s["stock_sleeve_pct"]
    fund_pct  = s["fund_sleeve_pct"]

    # KPI cards (Claude to fill prior month value and MoM change — not derivable without prior file)
    kpis = [
        {
            "label": "Total ISA Value",
            "value": f"£{total:,.2f}",
            "sub":   f"As at {portfolio['_meta']['data_date']}",
            "style": "normal",
        },
        {
            "label": "Cash (Effective)",
            "value": f"£{cash_eff:,.2f}",
            "sub":   f"+£{STANDING_ORDER:,.0f} unprocessed S/O | Deployable: £{s['cash_deployable_gbp']:,.2f}",
            "style": "info",
        },
        {
            "label": "Stock Sleeve",
            "value": f"{stock_pct:.1f}%",
            "sub":   f"£{s['stock_sleeve_value_gbp']:,.2f} | Target: 10–15% (Phase 1)",
            "style": "info",
        },
        {
            "label": "MoM Change",
            "value": "[Claude to fill]",
            "sub":   "vs prior month AJ Bell file",
            "style": "normal",
        },
    ]

    # Holdings table (all positions)
    all_holdings = []

    # Stocks first
    for stock in portfolio.get("stocks", []):
        gain_sign = "positive" if stock["gain_pct"] >= 0 else "negative"
        all_holdings.append({
            "name":       stock["name"],
            "value":      f"£{stock['value_gbp']:,.2f}",
            "cost":       f"£{stock['cost_gbp']:,.2f}",
            "gain_pct":   f"{stock['gain_pct']:+.1f}%",
            "gain_sign":  gain_sign,
            "weight_pct": f"{stock.get('weight_pct', 0):.2f}%",
            "ms_rating":  "—",
            "is_stock":   True,
        })

    # Funds — merge with xray MS ratings if available
    xray_fund_ratings = {}
    for xf in xray.get("fund_holdings", []):
        key = xf["name"][:20].upper()
        xray_fund_ratings[key] = xf.get("ms_rating")

    for fund in portfolio.get("funds", []):
        # Try to match xray rating by name prefix
        name_key = fund["name"][:20].upper()
        ms_rating = xray_fund_ratings.get(name_key)
        gain_sign = "positive" if fund["gain_pct"] >= 0 else "negative"
        all_holdings.append({
            "name":       fund["name"],
            "value":      f"£{fund['value_gbp']:,.2f}",
            "cost":       f"£{fund['cost_gbp']:,.2f}",
            "gain_pct":   f"{fund['gain_pct']:+.1f}%",
            "gain_sign":  gain_sign,
            "weight_pct": f"{fund.get('weight_pct', 0):.2f}%",
            "ms_rating":  str(ms_rating) if ms_rating else "—",
            "is_stock":   False,
        })

    # Cash row
    all_holdings.append({
        "name":       "Cash GBP",
        "value":      f"£{portfolio['cash']['value_gbp']:,.2f}",
        "cost":       "—",
        "gain_pct":   "—",
        "gain_sign":  "neutral",
        "weight_pct": f"{s['cash_pct']:.2f}%",
        "ms_rating":  "—",
        "is_stock":   False,
    })

    # Performance table from X-Ray
    perf = []
    tr = xray.get("trailing_returns", {})
    for key, label in [
        ("1m",      "1 Month"),
        ("3m",      "3 Months"),
        ("6m",      "6 Months"),
        ("1yr",     "1 Year"),
        ("3yr_ann", "3 Years (Ann)"),
        ("5yr_ann", "5 Years (Ann)"),
        ("ytd",     "YTD"),
    ]:
        if key in tr:
            r = tr[key]
            port_pct  = r.get("portfolio_pct")
            bench_pct = r.get("benchmark_pct")
            alpha     = r.get("relative_pct")
            def fmt_pct(v):
                return f"{v:+.2f}%" if v is not None else "—"
            perf.append({
                "period":    label,
                "portfolio": fmt_pct(port_pct),
                "benchmark": fmt_pct(bench_pct) if bench_pct is not None else "—",
                "alpha":     fmt_pct(alpha),
            })

    notes = (
        f"Cash per AJ Bell file: £{s['cash_stated_gbp']:,.2f}. "
        f"Adjusted for unprocessed standing order: +£{STANDING_ORDER:,.0f} = £{s['cash_effective_gbp']:,.2f} effective. "
        f"Deployable after £{150:.0f} buffer: £{s['cash_deployable_gbp']:,.2f}. "
        f"[Claude to fill: MoM change vs prior month, benchmark comparison for portfolio performance.]"
    )

    return {
        "kpis":               kpis,
        "holdings":           all_holdings,
        "performance_header": "Portfolio vs MSCI World (Benchmark: Global Large-Cap Blend Equity)",
        "performance":        perf,
        "notes":              notes,
    }


def build_s7(portfolio: dict) -> dict:
    """Section 7 — Existing Stock Sleeve Review."""
    stocks = portfolio.get("stocks", [])

    total_value = portfolio["summary"]["total_value_gbp"]
    stock_total = portfolio["summary"]["stock_sleeve_value_gbp"]

    kpis = [
        {
            "label": "Stock Sleeve",
            "value": f"{portfolio['summary']['stock_sleeve_pct']:.1f}%",
            "sub":   f"£{stock_total:,.2f} of £{total_value:,.2f} total",
            "style": "info",
        },
        {
            "label": "Positions",
            "value": str(len(stocks)),
            "sub":   "Active stock sleeve holdings",
            "style": "normal",
        },
    ]

    holdings = []
    for s in stocks:
        gain_sign = "positive" if s["gain_pct"] >= 0 else "negative"
        holdings.append({
            "ticker":     s["ticker"],
            "name":       s["name"],
            "shares":     str(s.get("quantity", "—")),
            "value":      f"£{s['value_gbp']:,.2f}",
            "cost":       f"£{s['cost_gbp']:,.2f}",
            "gain_pct":   f"{s['gain_pct']:+.1f}%",
            "gain_sign":  gain_sign,
            "weight_pct": f"{s.get('weight_pct', 0):.2f}%",
            "status":     "Hold",
            "status_type": "hold",
            "status_note": "[Claude: update thesis status, earnings summary, analyst changes]",
        })

    notes = "[Claude: fill thesis status (strengthening/unchanged/weakening), earnings vs consensus, analyst rating changes, and company news for each holding per Step 8 review.]"

    return {"kpis": kpis, "holdings": holdings, "notes": notes}


def build_s8(portfolio: dict, analytics: dict, xray: dict) -> dict:
    """Section 8 — Fund Portfolio Review (incl. Step 8A pre-computed data)."""
    total_value = portfolio["summary"]["total_value_gbp"]

    # Merge: portfolio funds + drift table from analytics + xray fund holdings
    drift_map = {r["ticker"]: r for r in analytics.get("fund_drift_table", {}).get("rows", [])}

    xray_fund_map = {}
    for xf in xray.get("fund_holdings", []):
        key = xf["name"][:20].upper()
        xray_fund_map[key] = xf

    fund_rows = []
    for fund in portfolio.get("funds", []):
        ticker = fund["ticker"]
        dr = drift_map.get(ticker, {})

        # Match xray data
        name_key = fund["name"][:20].upper()
        xf = xray_fund_map.get(name_key, {})

        ms_rating = xf.get("ms_rating")
        perf_1yr  = xf.get("return_1yr")
        ongoing_cost = xf.get("ongoing_cost")

        signal = dr.get("signal", "—")
        signal_map = {
            "Hold":                  "hold",
            "Watch":                 "watch",
            "Rebalancing candidate": "rebalancing candidate",
            "Research trigger":      "research trigger",
        }

        fund_rows.append({
            "name":         fund["name"],
            "ticker":       ticker,
            "value":        f"£{fund['value_gbp']:,.2f}",
            "weight_pct":   f"{fund.get('weight_pct', 0):.2f}%",
            "perf_1yr":     f"{perf_1yr:+.1f}%" if perf_1yr is not None else "—",
            "ms_rating":    str(ms_rating) if ms_rating else "—",
            "bucket":       dr.get("bucket", "—"),
            "target_pct":   f"{dr.get('target_pct', '—')}%" if dr.get("target_pct") is not None else "—",
            "drift":        f"{dr.get('drift_pp', 0):+.1f}pp",
            "band":         (
                f"{dr.get('band_low_pct')}–{dr.get('band_high_pct')}%"
                if dr.get("band_low_pct") is not None else "—"
            ),
            "est_return":   "[Claude fills]",
            "signal":       signal,
            "status_level": "ok",
            "status_html":  (
                f"Drift {dr.get('drift_pp', 0):+.1f}pp | "
                + (f"Band breach: {dr.get('band_breach', 'N/A')} | " if dr.get("band_breach") == "Yes" else "")
                + f"[Claude: thesis/performance note]"
            ),
        })

    # Step 8A summary skeleton — Claude fills after retrieving estimated returns
    step8a_summary = {
        "section_a": {
            "result": "[Claude fills after retrieving estimated returns — PASS or FAIL]",
            "value":  "[X.X%] vs 12% threshold",
            "status": "pending",
        },
        "section_b": {
            "result":     analytics["section_b"].get("status_label", "Indicative"),
            "value":      analytics["section_b"].get("result", "—"),
            "status":     analytics["section_b"].get("status", "indicative"),
        },
        "section_c": {
            "result": "[Claude fills after Section A complete — On track / Watch / Flag]",
            "value":  "[X.X%] vs 14% working target",
            "status": "pending",
        },
        "overlap_check": "[Claude fills at Step 8A — checks each stock vs fund top-10 holdings]",
        "regime":        "[Claude fills at Step 8A — REGIME: [X] — Watch: [factor] — Tilt effect: ...]",
        "alt_research":  "[Claude fills if triggered — Confirm hold / Recommend replacement / Watchlist]",
    }

    xray_summary = ""
    sw = xray.get("sector_weights", {})
    if sw:
        top_sectors = sorted(sw.items(), key=lambda x: abs(x[1].get("vs_benchmark", 0)), reverse=True)[:5]
        parts = []
        for k, v in top_sectors:
            p = v.get("portfolio_pct", 0)
            b = v.get("benchmark_pct", 0)
            vs = v.get("vs_benchmark", 0)
            sign = "+" if vs >= 0 else ""
            parts.append(f"{v['name']}: {p:.1f}% ({sign}{vs:.1f}pp vs benchmark)")
        xray_summary = "Largest deviations: " + " | ".join(parts)

    return {
        "funds":          fund_rows,
        "step8a_summary": step8a_summary,
        "xray_summary":   xray_summary,
    }


def build_s10(portfolio: dict, analytics: dict, run_date: date) -> dict:
    """Section 10 — Tax Year and ISA Allowance Tracker."""
    total = portfolio["summary"]["total_value_gbp"]
    cash_eff = portfolio["summary"]["cash_effective_gbp"]
    tax_month = calc_tax_year_month(run_date)

    # Allowance used — approximate from total invested
    # Claude should verify exact figure from AJ Bell account summary
    # We can compute: months since tax year start × £1,250 S/O + any lump sums
    months_since_start = max(0,
        (run_date.year - TAX_YEAR_START.year) * 12
        + run_date.month - TAX_YEAR_START.month
    )
    so_contributions = months_since_start * STANDING_ORDER
    allowance_used_approx = so_contributions   # approximate — Claude to verify from AJ Bell

    kpis = [
        {
            "label": "Allowance Used",
            "value": f"~£{allowance_used_approx:,.0f}",
            "sub":   f"Approx {months_since_start} × £{STANDING_ORDER:,.0f} S/O + any lump sums. Claude to verify from AJ Bell.",
            "style": "normal",
        },
        {
            "label": "Allowance Remaining",
            "value": f"~£{TAX_YEAR_ANNUAL - allowance_used_approx:,.0f}",
            "sub":   f"Of £{TAX_YEAR_ANNUAL:,.0f} annual allowance ({TAX_YEAR_LABEL})",
            "style": "info",
        },
        {
            "label": "Tax Year Month",
            "value": tax_month,
            "sub":   f"{TAX_YEAR_LABEL} (started 6 Apr 2026)",
            "style": "normal",
        },
    ]

    items = [
        {
            "component":   f"Monthly standing order × {months_since_start} months",
            "amount":      f"£{so_contributions:,.0f}",
            "status":      "Processed",
            "status_type": "done",
        },
        {
            "component":   "Additional lump sum contributions",
            "amount":      "[Claude to verify from AJ Bell]",
            "status":      "Verify",
            "status_type": "pending",
        },
        {
            "component":   "Remaining monthly S/Os (to 1 Mar 2027)",
            "amount":      f"£{max(0, (12 - months_since_start)) * STANDING_ORDER:,.0f}",
            "status":      "Scheduled",
            "status_type": "scheduled",
        },
    ]

    notes = (
        f"Pace check: £{TAX_YEAR_ANNUAL:,.0f} annual allowance. "
        f"Remaining: ~£{TAX_YEAR_ANNUAL - allowance_used_approx:,.0f} across {max(0, 12 - months_since_start)} remaining months. "
        f"[Claude: add running total costs (dealing fees + FX charges) paid in this tax year. "
        f"Add dividend reinvestment reminders if any dividends received.]"
    )

    return {"kpis": kpis, "items": items, "notes": notes}


# ---------------------------------------------------------------------------
# Skeleton sections (Claude fills entirely)
# ---------------------------------------------------------------------------
def skeleton_s1() -> dict:
    return {
        "intro": "[Claude fills: one-sentence summary of this month's best action.]",
        "actions": [
            {
                "action":           "BUY / SELL / HOLD",
                "name":             "[Stock/Fund name]",
                "size":             "[£X (~N shares/units)]",
                "conviction":       "[XX/100 High/Medium]",
                "conviction_level": "high",
                "timing":           "[This week / Wait for entry / etc.]",
                "note":             "[Preclearance required? / 30-day hold / etc.]",
            }
        ],
        "net_effect": "[Claude fills: stock sleeve rises/falls from X% to Y% post-trade.]",
    }


def skeleton_s2() -> dict:
    return {
        "kpis": [
            {"label": "Capital Available", "value": "[Claude fills]", "sub": "Effective cash", "style": "normal"},
            {"label": "Deploy Now",         "value": "[Claude fills]", "sub": "This month",    "style": "positive"},
            {"label": "Retain",             "value": "[Claude fills]", "sub": "Buffer + future", "style": "normal"},
        ],
        "items": [
            {
                "priority":       "1",
                "allocation":     "[Action type]",
                "allocation_type": "buy",
                "amount":         "[£X]",
                "rationale":      "[Claude fills: case for and against, rank rationale]",
            }
        ],
        "notes": "[Claude fills: explicit statement of capital deployed now vs retained, and why.]",
    }


def skeleton_s3() -> list:
    return [
        {
            "action":        "BUY",
            "ticker":        "[TICKER]",
            "name":          "[Company Name]",
            "conviction":    "[XX/100]",
            "metrics_table": [
                {
                    "label":      "[Metric]",
                    "value":      "[Value]",
                    "assessment": "[Claude: Strong / Acceptable / Weak]",
                    "signal":     "green",
                }
            ],
            "paragraphs": [
                "[Claude fills: full investment case — valuation, growth driver, moat, management, portfolio fit, execution, risks.]"
            ],
            "separator_after": False,
        }
    ]


def skeleton_s4() -> dict:
    return {
        "items": [
            {
                "ticker":         "[TICKER]",
                "shares":         "[N shares]",
                "current_value":  "[£X]",
                "cost":           "[£Y]",
                "gain_loss":      "[+/-£Z]",
                "gain_loss_sign": "positive",
                "action":         "[SELL / HOLD / MONITOR]",
                "action_type":    "sell",
                "earliest_sale":  "[Day 31 from preclearance: DD-Mon-YYYY or N/A]",
                "reason":         "[Claude: thesis trigger, concentration, size too small, etc.]",
            }
        ],
        "notes": "[Claude fills: redeployment of proceeds, concentration/simplicity improvement.]",
    }


def skeleton_s5() -> dict:
    """Fallback s5 skeleton when scored data is unavailable."""
    return {
        "items": [
            {
                "rank":         1,
                "ticker":       "[TICKER]",
                "name":         "[Company]",
                "score":        "[XX/50 | Conv: XX/100]",
                "score_level":  "high",
                "sector":       "[Sector]",
                "entry_level":  "[$XX or £XX]",
                "status":       "Watch",
                "status_type":  "watchlist",
            }
        ],
        "excluded":     "[Claude: any names removed from watchlist and reason]",
        "detail_items": [
            {
                "ticker":     "[Top 3 ticker]",
                "name":       "[Company]",
                "paragraphs": ["[Claude fills: thesis summary, key metrics, entry triggers, risks for top 3 names]"],
            }
        ],
    }


def build_s5_from_scored(scored: dict) -> dict:
    """
    Build s5 watchlist section from watchlist_scored.json output.
    Items are pre-populated with quantitative fields from normalise_adapter.py.
    Claude fills: detail_items paragraphs (thesis for top 3), excluded notes, conviction scores.
    """
    raw_items = scored.get("s5_watchlist_rows", [])
    if not raw_items:
        return skeleton_s5()

    # Map scored rows to email s5 table format
    items = []
    for row in raw_items:
        in_win = row.get("in_window", False)
        status = row.get("status", "Watchlist")
        # Add in-window marker to status
        if in_win:
            status = f"IN RANGE — {status}"
        items.append({
            "rank":         row.get("rank", "—"),
            "ticker":       row.get("ticker", "—"),
            "name":         row.get("name", "—"),
            "score":        row.get("score", "—"),
            "score_level":  row.get("score_level", "normal"),
            "sector":       row.get("sector", "—"),
            "entry_level":  row.get("entry_level", "—"),
            "status":       status,
            "status_type":  "buy" if in_win else "watchlist",
        })

    # Top 3 detail items — quantitative pre-populated, narrative for Claude
    detail_items = []
    for row in raw_items[:3]:
        ticker = row.get("ticker", "")
        detail_items.append({
            "ticker": ticker,
            "name":   row.get("name", ticker),
            "paragraphs": [
                f"<strong>Entry level:</strong> {row.get('entry_level','—')} | "
                f"<strong>Current:</strong> {row.get('current_price','—')} | "
                f"<strong>Gap:</strong> {row.get('gap_pct','—')} | "
                f"<strong>Target upside:</strong> {row.get('target_upside','—') if 'target_upside' in row else '[Claude: from watchlist_scored]'}",
                "[Claude fills: thesis summary — structural growth driver, moat, why now, key risks, entry and exit triggers]",
            ]
        })

    # Conviction ranking note
    in_window = [r.get("ticker") for r in raw_items if r.get("in_window")]
    ranking_note = (
        f"[Claude fills Step 11 conviction scores. "
        f"In-window names requiring Step 11 scoring: {in_window if in_window else 'none at entry level this month'}. "
        f"Analyst disparity flags: "
        f"{[r.get('ticker') for r in raw_items if r.get('analyst_disparity')]}]"
    )

    return {
        "items":        items,
        "excluded":     "[Claude: any names removed from watchlist this month and reason]",
        "detail_items": detail_items,
        "_conviction_ranking_note": ranking_note,
        "_conviction_ranking":      scored.get("conviction_ranking", []),
    }


def skeleton_s9() -> dict:
    return {
        "items": [
            {
                "title":      "[Topic: Rates / Inflation / USD-GBP / Geopolitics / etc.]",
                "paragraphs": ["[Claude fills: development, portfolio implication, whether action warranted]"],
            }
        ]
    }


def skeleton_s11() -> dict:
    return {
        "items": [
            {
                "title":    "[Claude fills: specific problem identified this run]",
                "problem":  "[Claude fills: what went wrong or could be better]",
                "action":   "[Claude fills: concrete improvement for next run]",
                "category": "accuracy",
                "impact":   "M",
            }
        ]
    }


# ---------------------------------------------------------------------------
# s7 update using scored sleeve data
# ---------------------------------------------------------------------------
def build_s7_from_scored(portfolio: dict, scored: dict) -> dict:
    """
    Build s7 section merging portfolio data (weights/AJ Bell values) with
    scored sleeve data (current price, metrics, analyst rating, target upside).
    Falls back to portfolio-only if scored data is unavailable.
    """
    # Base s7 from portfolio
    base = build_s7(portfolio)
    sleeve_rows_scored = scored.get("s7_sleeve_rows", [])
    if not sleeve_rows_scored:
        return base

    # Build lookup by ticker
    scored_map = {r["ticker"]: r for r in sleeve_rows_scored}

    # Merge: portfolio row gets enriched with scored metrics
    for h in base["holdings"]:
        ticker = h.get("ticker", "")
        s = scored_map.get(ticker, {})
        if s:
            # Override status note with analyst rating and target upside
            analyst = s.get("analyst_rating", "—")
            upside  = s.get("target_upside", "—")
            ne      = s.get("next_earnings", "—")
            score   = s.get("total_score")
            h["status_note"] = (
                f"Analyst: {analyst} | Target upside: {upside} | "
                f"Next earnings: {ne}"
                + (f" | Score: {score}/{s.get('total_max') or 50}" if score else "")
                + " | [Claude: update thesis status at Step 8]"
            )

    return base


# ---------------------------------------------------------------------------
# s3 from scored case skeletons
# ---------------------------------------------------------------------------
def build_s3_from_scored(scored: dict) -> list:
    """
    Build s3 investment cases from pre-scored skeletons.
    If no in-window names, falls back to generic skeleton.
    """
    skeletons = scored.get("s3_case_skeletons", [])
    if not skeletons:
        return skeleton_s3()

    cases = []
    for skel in skeletons:
        # Only include watchlist names as investment cases (not existing sleeve for s3)
        if skel.get("_for_step", "").startswith("Step 8"):
            continue  # sleeve members go in s7, not s3
        case = {
            "action":        skel.get("action", "BUY"),
            "ticker":        skel.get("ticker", ""),
            "name":          skel.get("name", ""),
            "conviction":    skel.get("conviction", "[Claude fills /100]"),
            "metrics_table": skel.get("metrics_table", []),
            "_part_a_table": skel.get("part_a_table", []),
            "_part_b_table": skel.get("part_b_table", []),
            "_analyst":      skel.get("analyst", {}),
            "_overlays":     skel.get("overlays", {}),
            "paragraphs":    skel.get("paragraphs", [
                "[Claude fills at Step 12: thesis, valuation, portfolio fit, execution]"
            ]),
            "separator_after": False,
        }
        cases.append(case)

    return cases if cases else skeleton_s3()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_prefilled_email(
    portfolio: dict,
    analytics: dict,
    xray: dict,
    scored: dict,
    run_date: date,
) -> dict:
    has_scored = bool(scored and scored.get("s5_watchlist_rows"))
    return {
        "_instructions": (
            "Pre-populated by email_prefill.py v2. "
            "Fields marked '[Claude fills]' or '[Claude: ...]' must be completed during the run. "
            "s3, s5, s7 sections have quantitative data pre-populated from watchlist_scored.json. "
            "All string values must be plain text (no Unicode above U+007F). "
            "HTML sub-tags allowed in 'paragraphs': <strong>, <em>, <a href>, <code>, <span style>."
        ),
        "meta":                   build_meta(portfolio, run_date),
        "s1_decision_summary":    skeleton_s1(),
        "s2_capital_allocation":  skeleton_s2(),
        "s3_investment_cases":    build_s3_from_scored(scored) if has_scored else skeleton_s3(),
        "s4_liquidation_tracker": skeleton_s4(),
        "s5_watchlist":           build_s5_from_scored(scored) if has_scored else skeleton_s5(),
        "s6_portfolio_snapshot":  build_s6(portfolio, analytics, xray),
        "s7_stock_sleeve":        build_s7_from_scored(portfolio, scored) if has_scored else build_s7(portfolio),
        "s8_fund_review":         build_s8(portfolio, analytics, xray),
        "s9_macro":               skeleton_s9(),
        "s10_tax_tracker":        build_s10(portfolio, analytics, run_date),
        "s11_retrospective":      skeleton_s11(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Pre-populate ISA review email JSON from portfolio, analytics, xray, and scored watchlist data."
    )
    parser.add_argument("--portfolio", required=True)
    parser.add_argument("--analytics", required=True)
    parser.add_argument("--xray",      required=True)
    parser.add_argument("--scored",    default=None,
                        help="Path to watchlist_scored_mmm_yyyy.json from normalise_adapter.py")
    parser.add_argument("--out",       default=None)
    args = parser.parse_args()

    def load(path, name, required=True):
        if not os.path.exists(path):
            if required:
                print(f"ERROR: {name} not found: {path}")
                sys.exit(1)
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    portfolio = load(args.portfolio, "portfolio JSON")
    analytics = load(args.analytics, "analytics JSON")
    xray      = load(args.xray,      "xray JSON")
    scored    = load(args.scored, "watchlist_scored JSON", required=False) if args.scored else {}

    run_date = date.today()
    data = build_prefilled_email(portfolio, analytics, xray, scored, run_date)

    if args.out:
        out_path = args.out
    else:
        month_label = portfolio["_meta"]["month_label"]
        out_path = os.path.join(SCRIPT_DIR, f"email_data_{month_label}.json")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    has_scored = bool(scored and scored.get("s5_watchlist_rows"))
    print(f"Email JSON pre-populated: {out_path}")
    print(f"  Run month:              {portfolio['_meta']['run_month']}")
    print(f"  Holdings rows (s6):     {len(data['s6_portfolio_snapshot']['holdings'])}")
    print(f"  Stock sleeve rows (s7): {len(data['s7_stock_sleeve']['holdings'])}")
    print(f"  Fund rows (s8):         {len(data['s8_fund_review']['funds'])}")
    print(f"  Watchlist rows (s5):    {len(data['s5_watchlist'].get('items', []))}")
    print(f"  Investment cases (s3):  {len(data['s3_investment_cases'])} "
          f"({'pre-scored' if has_scored else 'skeleton'})")
    if has_scored:
        in_win = [r.get('ticker') for r in scored.get('s5_watchlist_rows', []) if r.get('in_window')]
        print(f"  In-window names:        {in_win if in_win else 'none'}")
    print('  Claude fills: s1/s2/s3 narratives/s4/s5 detail/s7 thesis/s8 est_returns/s9/s11/conviction scores.')

if __name__ == '__main__':
    main()
