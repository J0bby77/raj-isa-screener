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


# ---- M2 entry reachability (03-Aug-2026) -----------------------------------------
# Emitted by entry_reachability.py. Import is optional and failure is silent-but-labelled:
# an unavailable classifier yields "unknown", never a default of "reachable", so an unchecked
# entry can never quietly present itself as a valid target.
try:
    from entry_reachability import classify as _reach_classify
except Exception:                                       # pragma: no cover
    _reach_classify = None

_REACH_SUFFIX = {
    "reachable":   "",
    "stretch":     " (drawdown only)",
    "unreachable": " — NOT REACHABLE",
    "unknown":     " (reachability unchecked)",
}


def _entry_reach_for(ticker, row):
    if _reach_classify is None:
        return {"reachability": "unknown", "basis": "entry_reachability unavailable"}
    return _reach_classify(row.get("current_price") or row.get("price"),
                           row.get("entry_level"), row.get("realised_vol"))


def _entry_display(entry_level, reach):
    if entry_level in (None, "", "—"):
        return "—"
    return f"{entry_level}{_REACH_SUFFIX.get((reach or {}).get('reachability', 'unknown'), '')}"




# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from extract_cash_statement import (  # ONE HOME (R4.4)  # noqa: E402
    STANDING_ORDER, STANDING_ORDER_ACTIVE, STANDING_ORDER_PAUSED_FROM)

# Tax year 26/27 start
TAX_YEAR_START = date(2026, 4, 6)
TAX_YEAR_ANNUAL = 20000.0
TAX_YEAR_LABEL  = "2026/27"

# MS rating integer → star string for email
def _pf_trade_note_hint():
    """Placeholder hint for the trade `note` field, regime-aware (compliance.py authoritative)."""
    try:
        import compliance
        if compliance.active():
            return "[Preclearance required? / %d-day hold / etc.]" % compliance.min_hold_days()
        return "[Timing / limit / dealing cost — no preclearance or regulatory hold while PAD regime paused]"
    except Exception:
        return "[Preclearance required? / 30-day hold / etc.]"


def _pf_earliest_sale_hint():
    try:
        import compliance
        if compliance.active():
            return "[Day %d from preclearance: DD-Mon-YYYY or N/A]" % (compliance.min_hold_days() + 1)
        return "N/A (no regulatory holding period — PAD regime paused)"
    except Exception:
        return "[Day 31 from preclearance: DD-Mon-YYYY or N/A]"

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


def calc_allowance_used(portfolio: dict):
    """RETIRED 05-Aug-2026 — it returned a number that was never an allowance.

    `total_value - cash` is the value of the invested holdings. The ISA allowance used is the
    sum of MONEY PAID IN during the tax year. Those two quantities are unrelated: the first
    moves with markets, includes every prior year's contributions, and would report an
    "allowance" of six figures on a £139k portfolio against a £20,000 annual limit.

    It was never wired into the email (Fix Pack A22 routes §10 through broker-reconciled
    contributions, and the dashboard explicitly refuses this function by name) — but it sat
    here returning a plausible float to anyone who called it, which is precisely the Class-B
    defect the engineering standard names. A function whose value cannot be right does not get
    to keep returning one.

    The real figure comes from `extract_cash_statement.parse()["allowance"]`, which reads the
    only document that contains contributions at all.
    """
    raise NotImplementedError(
        "calc_allowance_used() has been retired: total_value - cash is not an allowance. "
        "Use extract_cash_statement.parse()['allowance'] (broker cash statement), or "
        "extract_portfolio.parse_contributions() for the dealing-record cross-check.")


# ---------------------------------------------------------------------------
# Fix Pack P2 helpers — A19 anchor, B5 trajectory, B1 ladder, A14 counterfactual
# ---------------------------------------------------------------------------
def _latest_run_context_path():
    import glob as _g
    fs = sorted(_g.glob(os.path.join(SCRIPT_DIR, "run_context_*.json")), key=os.path.getmtime)
    return fs[-1] if fs else None


def load_json_optional(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build_b5_trajectory(target_state: dict, run_date: date) -> str:
    """B5 — standing §2 line, pure function of target_state.json (A19).
    'S/O: PAUSED month N | pause cost to date: £X terminal (@required-return) |
     required return now: Y% floor / Z% stretch | drift since derivation: +W pp'"""
    if not target_state:
        return "Trajectory: target_state.json unavailable — anchor line PENDING (A19)"
    sched = target_state.get("contribution_schedule") or []
    # pick the entry EFFECTIVE at run_date (from <= today), not a future assumed-resume row
    cur = {}
    _today_s = run_date.isoformat()
    for _e in sorted(sched, key=lambda e: str(e.get("from") or "")):
        if str(_e.get("from") or "9999")[:10] <= _today_s:
            cur = _e
    monthly = float(cur.get("monthly_gbp") or 0)
    floor = float(target_state.get("required_return_floor_pct") or 0)
    stretch = float(target_state.get("required_return_stretch_pct") or 0)
    if monthly > 0:
        so_part = f"S/O: ACTIVE £{monthly:,.0f}/mo"
        cost_part = "pause cost: n/a"
    else:
        frm = str(cur.get("from") or "")[:10]
        try:
            y, m = int(frm[:4]), int(frm[5:7])
            months_paused = max(0, (run_date.year - y) * 12 + (run_date.month - m) + 1)
        except Exception:
            months_paused = 0
        # terminal cost = FV difference of the missed contributions at required_return_floor
        so = 1250.0
        r_m = (1 + floor / 100.0) ** (1 / 12.0) - 1 if floor else 0.0
        try:
            td = str(target_state.get("target_date") or "2037-12-31")[:10]
            months_left = max(0, (int(td[:4]) - run_date.year) * 12 + (int(td[5:7]) - run_date.month))
        except Exception:
            months_left = 0
        cost = sum(so * ((1 + r_m) ** (months_left + i)) for i in range(months_paused))
        so_part = f"S/O: PAUSED month {months_paused}"
        cost_part = f"pause cost to date: £{cost:,.0f} terminal (@{floor:.1f}%)"
    derived_at = str(target_state.get("derived_at") or "?")
    return (f"{so_part} | {cost_part} | required return now: {floor:.1f}% floor / "
            f"{stretch:.1f}% stretch | derived {derived_at} "
            f"(guardrail: {target_state.get('guardrail_state', 'OK')})")


# ISA-0429 (CRITICAL). A BACKSTOP on the OUTPUT, independent of the input contract in
# monthly_isa_prerun._reconcile_price_unit. A challenger counterfactual is a broad-index
# total return over the sleeve's life; a value outside this band is not a market move, it
# is a data fault. -98.9% for an S&P 500 tracker was published for 5+ months and no one
# saw it, because nothing was looking. REFUSE, never publish.
CF_RETURN_BAND_PCT = (-60.0, 150.0)


def _cf_return_admissible(pct):
    lo, hi = CF_RETURN_BAND_PCT
    return pct is not None and lo <= pct <= hi


def compute_vuag_counterfactual(trades: list, vuag_price_now: float,
                                sleeve_value_now: float) -> dict:
    """A14 — cash-flow-matched VUAG counterfactual (U-A14). trades: [{date, amount_gbp,
    vuag_price}] = actual sleeve buys (+) / sells (-) with the SAME-date VUAG price.
    Counterfactual = same £, same dates, bought VUAG units instead."""
    units = 0.0
    invested = 0.0
    for t in trades:
        px = t.get("vuag_price")
        amt = t.get("amount_gbp")
        if not px or amt is None:
            return {"status": "PENDING_BACKFILL",
                    "note": "trade-date VUAG price missing — backfill from statements/xlsx (A14)"}
        units += float(amt) / float(px)
        invested += float(amt)
    if invested <= 0 or not vuag_price_now:
        return {"status": "NO_DATA"}
    cf_value = units * float(vuag_price_now)
    actual_ret = (float(sleeve_value_now) / invested - 1) * 100.0
    cf_ret = (cf_value / invested - 1) * 100.0
    if not _cf_return_admissible(cf_ret):
        return {"status": "REFUSED_IMPLAUSIBLE",
                "counterfactual_return_pct": round(cf_ret, 2),
                "note": ("VUAG counterfactual return %.1f%% is outside the admissible band "
                         "%s - this is a data fault, not a market move. Check "
                         "vuag_price_now against the trade-date prices in the same file "
                         "(ISA-0429)." % (cf_ret, CF_RETURN_BAND_PCT)),
                "line": "Sleeve vs VUAG counterfactual: REFUSED - implausible input (ISA-0429)"}
    return {"status": "OK", "invested_gbp": round(invested, 2),
            "sleeve_value_gbp": round(float(sleeve_value_now), 2),
            "counterfactual_value_gbp": round(cf_value, 2),
            "sleeve_vs_vuag_pp": round(actual_ret - cf_ret, 1),
            "line": (f"Sleeve vs VUAG counterfactual since inception: "
                     f"{actual_ret - cf_ret:+.1f}pp (net of friction)")}


def compute_challenger_counterfactuals(trades, vuag_price_now, iwmo_price_now,
                                       sleeve_value_now, mu_value_now=None):
    """WP-1 (audit #1, 26-Jul-26) - dual-challenger cash-flow-matched counterfactual
    (VUAG.L + IWMO.L, all-trades + ex-MU). Missing iwmo_price on any trade -> IWMO legs
    INCOMPLETE(n), line degrades to VUAG-only; never raises. Ticker from the backfilled
    field, falling back to first word of note."""
    trades = trades or []
    n_missing = sum(1 for t in trades if not t.get("iwmo_price"))

    def _tk(t):
        v = t.get("ticker")
        return str(v).strip().upper() if v else str(t.get("note", "")).strip().split(" ")[0].upper()

    def _leg(pk, pnow, exmu):
        units = invested = 0.0
        for t in trades:
            if exmu and _tk(t) == "MU":
                continue
            px, amt = t.get(pk), t.get("amount_gbp")
            if not px or amt is None:
                return None, None
            units += float(amt) / float(px)
            invested += float(amt)
        if invested <= 0 or not pnow:
            return None, None
        return (units * float(pnow) / invested - 1.0) * 100.0, invested

    def _slv(inv, exmu):
        if inv is None or not sleeve_value_now:
            return None
        sv = float(sleeve_value_now)
        if exmu:
            if mu_value_now is None:
                return None
            sv -= float(mu_value_now)
        return (sv / inv - 1.0) * 100.0

    out = {"status": "OK", "iwmo_missing": n_missing, "refused": []}
    for key, pk, pnow, exmu in (("vs_vuag_pp", "vuag_price", vuag_price_now, False),
                                ("vs_iwmo_pp", "iwmo_price", iwmo_price_now, False),
                                ("vs_vuag_exmu_pp", "vuag_price", vuag_price_now, True),
                                ("vs_iwmo_exmu_pp", "iwmo_price", iwmo_price_now, True)):
        if "iwmo" in key and n_missing:
            out[key] = None
            continue
        cf, inv = _leg(pk, pnow, exmu)
        # ISA-0429: refuse an implausible CHALLENGER return before it can become a pp
        # figure. The subtraction hides the fault - a -98.9% challenger reads as a
        # spectacular +88pp for the sleeve, which is how this survived 5+ months.
        if cf is not None and not _cf_return_admissible(cf):
            out[key] = None
            out["refused"].append({"leg": key, "challenger_return_pct": round(cf, 2)})
            continue
        sr = _slv(inv, exmu)
        out[key] = round(sr - cf, 1) if (cf is not None and sr is not None) else None
    if out["refused"]:
        out["status"] = "REFUSED_IMPLAUSIBLE"
        out["note"] = ("%d challenger leg(s) refused: return outside %s. This is a data "
                       "fault, not a market move - check *_price_now against the "
                       "trade-date prices in the same file (ISA-0429)."
                       % (len(out["refused"]), CF_RETURN_BAND_PCT))

    def _f(v):
        return ("%+.1fpp" % v) if v is not None else "n/a"
    if n_missing:
        ip, iep = "IWMO: INCOMPLETE (%d missing)" % n_missing, "vs IWMO n/a"
    else:
        ip, iep = "vs IWMO " + _f(out["vs_iwmo_pp"]), "vs IWMO " + _f(out["vs_iwmo_exmu_pp"])
    if out["refused"]:
        out["line"] = ("Sleeve counterfactual: REFUSED - %d leg(s) implausible, input "
                       "fault suspected (ISA-0429). No verdict published this run."
                       % len(out["refused"]))
    else:
        out["line"] = ("Sleeve counterfactual: vs VUAG %s | %s | ex-MU: vs VUAG %s, %s"
                       % (_f(out["vs_vuag_pp"]), ip, _f(out["vs_vuag_exmu_pp"]), iep))
    return out


def compute_freeze_status(freeze_history):
    """WP-4 (audit #4; Raj 22-Jul-26: 4-month window, override at 3). Trailing consecutive
    months beating BOTH challengers ex-MU. Reports only - unfreeze is Raj's A13 decision."""
    n = 0
    unmeasured = 0
    for e in reversed(freeze_history or []):
        # ISA-0429: an UNMEASURED month cannot be a pass. It breaks the streak - which is
        # conservative and correct - but it must be SURFACED, or a data fault is
        # indistinguishable from underperformance in the one log that gates the freeze.
        if e.get("measured") is False:
            unmeasured += 1
            break
        if e.get("beats_vuag_exmu") is True and e.get("beats_iwmo_exmu") is True:
            n += 1
        else:
            break
    status = "CLEARED-eligible" if n >= 4 else ("CLEARING (3/4)" if n == 3 else "ACTIVE")
    note = "mechanical unfreeze at 4 consecutive; A13 override permitted at 3"
    if unmeasured:
        status = "ACTIVE-UNMEASURED"
        note = ("streak broken by an UNMEASURED month, not by underperformance - the "
                "counterfactual was refused. Fix the input before reading this as a "
                "verdict on the sleeve (ISA-0429). " + note)
    return {"status": status, "consecutive": n, "unmeasured_latest": bool(unmeasured),
            "note": note}


def append_freeze_history_entry(store, month_str, challenger_out):
    """WP-4 - idempotent monthly append to sleeve_counterfactual.json['freeze_history']."""
    hist = store.setdefault("freeze_history", [])
    if any(h.get("month") == month_str for h in hist):
        return hist
    v, i = challenger_out.get("vs_vuag_exmu_pp"), challenger_out.get("vs_iwmo_exmu_pp")
    entry = {"month": month_str, "beats_vuag_exmu": (v is not None and v > 0),
             "beats_iwmo_exmu": (i is not None and i > 0)}
    # ISA-0429: an UNMEASURABLE month is not a FAILED month and must not look like one.
    # The freeze clock counts consecutive passes; a refusal has to be visible in the log
    # or a data fault becomes indistinguishable from underperformance.
    if challenger_out.get("status") == "REFUSED_IMPLAUSIBLE" or v is None or i is None:
        entry["measured"] = False
        entry["reason"] = challenger_out.get("note") or "challenger leg unavailable"
    else:
        entry["measured"] = True
    hist.append(entry)
    return hist


def compute_pilot_line(pilots, price_now_map):
    """WP-5 (audit #5) - gold-pilot counterfactual vs funding source; None when no pilots."""
    if not pilots:
        return None
    p = pilots[0]
    ut = us = inv = 0.0
    first = src = None
    for tr in p.get("trades", []):
        amt = float(tr["amount_gbp"])
        ut += amt / float(tr["sgln_price"])
        us += amt / float(tr["funded_from_price"])
        inv += amt
        first = first or tr["date"]
        src = tr["funded_from"]
    if inv <= 0:
        return None
    tn, sn = (price_now_map or {}).get("SGLN.L"), (price_now_map or {}).get(src)
    if not tn or not sn:
        return {"status": "INCOMPLETE", "line": "Gold pilot: price refresh missing - no verdict"}
    pp = round((ut * float(tn) / inv - 1.0) * 100.0 - (us * float(sn) / inv - 1.0) * 100.0, 1)
    return {"status": "OK", "pp_vs_funding": pp, "since": first,
            "line": "Gold pilot: %+.1fpp vs funding source since %s" % (pp, first)}


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
        # A19: the email header states the anchor + derivation date (one hurdle, everywhere)
        "anchor_line":      build_b5_trajectory(load_json_optional(
                                os.path.join(SCRIPT_DIR, "target_state.json")), run_date),
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
            "sub":   ((f"+£{STANDING_ORDER:,.0f} unprocessed S/O | " if STANDING_ORDER_ACTIVE
                       else f"S/O PAUSED since {STANDING_ORDER_PAUSED_FROM} | ")
                      + f"Deployable: £{s['cash_deployable_gbp']:,.2f}"),
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
        + (f"Adjusted for unprocessed standing order: +£{STANDING_ORDER:,.0f} = "
           f"£{s['cash_effective_gbp']:,.2f} effective. " if STANDING_ORDER_ACTIVE else
           f"Standing order PAUSED since {STANDING_ORDER_PAUSED_FROM}: no adjustment, "
           f"effective = stated £{s['cash_effective_gbp']:,.2f}. ")
        + f"Deployable after £{150:.0f} buffer: £{s['cash_deployable_gbp']:,.2f}. "
        + f"[Claude to fill: MoM change vs prior month, benchmark comparison for portfolio performance.]"
    )

    return {
        # B3 (P2): factor look-through line — computed by prerun step 9d
        "factor_line": (analytics.get("factor_lookthrough") or {}).get("email_line"),
        "factor_unclassified": (analytics.get("factor_lookthrough") or {}).get("unclassified"),
        "semis_line": ((analytics.get("factor_lookthrough") or {}).get("semis") or {}).get("line"),
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


def _fas_bands():
    """Read the FRS band thresholds from their ONE HOME. They were rebased 06-Aug-2026 and a
    hard-coded copy in the email prose would have gone on stating the old numbers."""
    try:
        import fund_action_stack as _f
        return _f.FRS_HOLD_ADD, _f.FRS_RETAIN_ONLY
    except Exception:
        return 58.0, 43.0


def _overlap_line(analytics):
    """Render the PUBLISHED look-through overlap check (H10). Absence is stated, not estimated."""
    oc = (analytics or {}).get("overlap_check") or {}
    if oc.get("status") != "OK":
        return (f"Overlap check UNAVAILABLE this run ({oc.get('status', 'missing')}) — "
                f"{oc.get('note', 'no detail recorded')}")
    parts = []
    for c in oc.get("checks", []):
        if c.get("basis") == "published":
            parts.append(f"{c['ticker']} {c['lookthrough_total_pct']:.2f}% effective "
                         f"(direct {c['direct_weight_pct']:.2f}% + funds "
                         f"{c['via_funds_pct']:.2f}%)"
                         + (" — FLAG >5%" if c["exceeds_flag"] else ""))
        else:
            parts.append(f"{c['ticker']} <={c['upper_bound_pct']:.2f}% "
                         f"(direct {c['direct_weight_pct']:.2f}%; absent from the top ten, whose "
                         f"floor is {oc['table_floor_pct']:.2f}%)"
                         + (" — FLAG >5%" if c["exceeds_flag"] else ""))
    n = (oc.get("summary") or {}).get("flags", 0)
    return ("OVERLAP (AJ Bell / Morningstar published look-through, as at "
            f"{oc.get('as_of')}): " + " \u00b7 ".join(parts)
            + (f" — {n} above the 5% flag" if n else " — none above the 5% flag")
            + ". Source: X-Ray Top 10 Underlying Holdings. The hand-calculated estimate this "
              "replaces reported AVGO 4.04% against the published 4.31%.")


# ── return architecture (Step 6.08) helpers — module level, because the fund table and the
# Step 8A summary are built by DIFFERENT functions and both must read ONE document. ───────
def _ra_load(analytics):
    """⚑ Read the architecture from the ANALYTICS document, which the pre-run wrote it into.

    The obvious implementation — open `return_architecture_{month_label}.json` — is wrong, and
    wrong in a way that would have been invisible: the pre-run keys that file on the RUN month
    ("aug_2026") while `portfolio_data._meta.month_label` is the DATA month ("jul_2026", from a
    31-Jul valuation). Two variables with the same name meaning different things. Resolving by
    label silently returned `{}`, and every consumer degraded to the retired `est_return`
    basis while reporting nothing wrong.
    """
    ra = (analytics or {}).get("return_architecture") or {}
    sc = (analytics or {}).get("section_c") or {}
    if ra or sc.get("source"):
        return {"as_of": ra.get("as_of"),
                "operative_basis": ra.get("operative_basis") or sc.get("basis"),
                "anchor": ra.get("anchor"), "thresholds": ra.get("thresholds"),
                "expected_return_inputs": ra.get("expected_return_inputs") or [],
                "basis_study": ra.get("basis_study"),
                "section_c": {"value_pct": sc.get("total_return"),
                              "anchor_pct": sc.get("anchor_pct"),
                              "verdict": sc.get("verdict"),
                              "shortfall_pp": sc.get("shortfall_pp"),
                              "coverage": (None if sc.get("coverage_pct") is None
                                           else sc["coverage_pct"] / 100.0)},
                "shortfall_attribution": {"rows": sc.get("shortfall_attribution") or []},
                "levers": sc.get("levers") or [],
                "not_summable_note": sc.get("levers_note")}
    return {}


def _er_cell(dr, ra):
    """The DECLARED long-run expectation Section A is computed from, with the REALISED figure
    the FRS uses beside it. Two numbers, both named — the column previously held one unnamed
    number that had been typed by hand a month earlier."""
    row = {i.get("asset_id"): i for i in ((ra or {}).get("expected_return_inputs") or [])}.get(
        dr.get("ticker"))
    if not row:
        v = dr.get("est_return_pct")
        return ("\u2014" if v is None else
                f"{v:.1f}% (est \u2014 RETIRED basis, register C4; continuity only)")
    p, r = row.get("prior_pct"), row.get("realised_pct")
    if p is None:
        return f"UNMEASURED \u2014 {row.get('unmeasured_reason') or 'no declared expectation'}"
    out = f"{p:.1f}% declared"
    if r is not None:
        out += f" \u00b7 {r:.1f}% realised"
    return out


def _section_c_value(ra, ana):
    sc = (ra.get("section_c") or {})
    v, a = sc.get("value_pct"), sc.get("anchor_pct")
    if v is None or a is None:
        return "[X.X%] vs required-return anchor (target_state.json, A19)"
    gap, cov = sc.get("shortfall_pp"), sc.get("coverage")
    return (f"{v:.2f}% vs a required {a:.1f}% \u2014 "
            + (f"short by {gap:.2f}pp" if gap and gap > 0 else f"ahead by {abs(gap or 0):.2f}pp")
            + f"; basis '{ra.get('operative_basis')}'"
            + (f", coverage {cov:.0%}" if cov is not None else ""))


def _perf_cell(golden_pct, golden_as_of, xray_pct, xray_as_of):
    """⚑ NEVER A BARE FIGURE. Golden source first, X-Ray beside it, each with ITS OWN date."""
    parts = []
    if golden_pct is not None:
        parts.append(f"{golden_pct:+.1f}%" + (f" ({_short_date(golden_as_of)})" if golden_as_of else ""))
    if xray_pct is not None:
        parts.append("X-Ray " + f"{xray_pct:+.1f}%"
                     + (f" ({_short_date(xray_as_of)})" if xray_as_of else " (date not captured)"))
    if not parts:
        return "\u2014 no return on record from either source"
    return " \u00b7 ".join(parts)


def _short_date(v):
    try:
        from datetime import date as _d
        y, m, dd = str(v)[:10].split("-")
        return f"{int(dd):02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1]}-{y[2:]}"
    except Exception:                                          # noqa: BLE001
        return str(v)[:10] if v else ""


def build_s8(portfolio: dict, analytics: dict, xray: dict) -> dict:
    # ── return architecture (Step 6.08) — the authority for Section A/B/C ────────────
    _ra_doc = _ra_load(analytics)

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
        # ── golden-source step 7: DATED columns, never a bare figure ──────────────────────
        # This cell read `xf.get("return_1yr")` — the X-Ray's 1-year number, rendered as a bare
        # "+17.7%". Two things were wrong with that and both are register items: the X-Ray is
        # struck at its OWN date, up to 61 days stale and NOT uniform across funds (M6), and it
        # is the DEMOTED source — the golden NAV series is primary (FP1). A figure with no date
        # is a figure that cannot be reconciled with anything, which is how a month-stale return
        # came to be published as current (D1).
        _ra_row = {i.get("asset_id"): i for i in (_ra_doc.get("expected_return_inputs") or [])}.get(ticker) or {}
        _g1y = ((_ra_row.get("corroborators") or {}).get("realised_windows") or {}).get("1y")
        # ⚑ The golden source's OWN evaluation date. The first cut read
        # `_ra_doc["anchor"]["as_of"]`, a key that does not exist — it fell through to the run
        # date and produced the right answer for the wrong reason. A date that is correct by
        # accident is the same defect as a value that is: nothing would have caught it moving.
        _g_asof = (_ra_doc.get("as_of") or (analytics.get("_meta") or {}).get("run_date"))

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
            "perf_1yr":     _perf_cell(_g1y, _g_asof, perf_1yr, xf.get("as_of")),
            "perf_1yr_basis": ("golden NAV series (primary) with the X-Ray beside it as the "
                               "independent second derivation; each carries its own strike date "
                               "because they are NOT struck on the same day (register M6)"),
            "ms_rating":    str(ms_rating) if ms_rating else "—",
            "bucket":       dr.get("bucket", "—"),
            "target_pct":   f"{dr.get('target_pct', '—')}%" if dr.get("target_pct") is not None else "—",
            "drift":        f"{dr.get('drift_pp', 0):+.1f}pp",
            "band":         (
                f"{dr.get('band_low_pct')}–{dr.get('band_high_pct')}%"
                if dr.get("band_low_pct") is not None else "—"
            ),
            # ⚑ NO LONGER A FILL-IN (build item #1, 06-Aug-2026). This column carried
            # "[Claude fills]" and was filled with prose typed by hand — on the August run it
            # still held July's strings. It now renders the DECLARED long-run expectation that
            # Section A is actually computed from, with the realised figure beside it so the
            # two can visibly disagree.
            "est_return":   _er_cell(dr, _ra_doc),
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
            # A11/D8 (P2): prerun step 9d computes the banded verdict mechanically
            "result": analytics.get("section_a", {}).get("verdict")
                      or "[Claude fills — PASS / INCONCLUSIVE / FAIL per D8 bands]",
            "value":  (f"{analytics['section_a']['weighted_avg_return']:.1f}% weighted avg "
                       f"(coverage {analytics['section_a'].get('coverage_pct', '?')}%)"
                       if analytics.get("section_a", {}).get("weighted_avg_return") is not None
                       else "[X.X%] — PENDING fund returns"),
            "bands_note": (lambda _b: f"D8 bands: PASS >= {_b.get('pass')}% / INCONCLUSIVE "
                                      f"{_b.get('inconclusive')}-{_b.get('pass')}% / FAIL < "
                                      f"{_b.get('inconclusive')}% (anchor-derived, A19)")(
                          analytics.get("section_a", {}).get("verdict_bands") or {}),
            "fund_cache_status": analytics.get("section_a", {}).get("fund_cache_status")
                                 or analytics.get("fund_cache_status"),
            "status": "computed" if analytics.get("section_a", {}).get("verdict") else "pending",
        },
        "section_b": {
            "result":     analytics["section_b"].get("status_label", "Indicative"),
            "value":      analytics["section_b"].get("result", "—"),
            "status":     analytics["section_b"].get("status", "indicative"),
        },
        "section_c": {
            # ⚑ WAS "[Claude fills after Section A complete]". Section C is the one number that
            # answers whether the portfolio reaches £1m, and it was a hand-typed sentence.
            "result": (_ra_doc.get("section_c", {}).get("verdict")
                       or analytics.get("section_c", {}).get("verdict")
                       or "[Claude fills — On track / Watch / Flag]"),
            "value": _section_c_value(_ra_doc, analytics),
            "shortfall_pp": (_ra_doc.get("section_c", {}) or {}).get("shortfall_pp"),
            "basis": _ra_doc.get("operative_basis"),
            "attribution": (_ra_doc.get("shortfall_attribution", {}) or {}).get("rows", [])[:6],
            "levers": [l for l in (_ra_doc.get("levers") or []) if l.get("feasible")],
            "levers_blocked": [l for l in (_ra_doc.get("levers") or []) if not l.get("feasible")],
            "levers_note": _ra_doc.get("not_summable_note"),
            "anchor_note": build_b5_trajectory(load_json_optional(
                               os.path.join(SCRIPT_DIR, "target_state.json")),
                               date.today()).split(" | ")[2]
                           if load_json_optional(os.path.join(SCRIPT_DIR, "target_state.json"))
                           else "see target_state.json (A19)",
            "status": "pending",
        },
        # H10 (06-Aug-2026): no longer a fill-in. The PUBLISHED X-Ray look-through table is the
        # source; the hand-calculation it replaced reported AVGO 4.04% against a published 4.31%.
        # An absent table renders as a STATED absence, never as a request to estimate.
        "overlap_check": _overlap_line(analytics),
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

    # ── FUND ACTION STACK (C4/C5) — 05-Aug-2026 ──────────────────────────────────────
    # It was built, tested and running in the pre-run, and NONE of it reached the report.
    # FRS bands, the anchor rule, window splits and the dead-money agenda existed only in
    # fund_action_stack_[mmm].json. Analysis Raj cannot see cannot change a decision, and the
    # fund sleeve is 85% of the ISA — so this is the step that makes C4/C5 real rather than
    # merely correct.
    fas_block = _fund_action_stack_block()

    return {
        "funds":          fund_rows,
        "step8a_summary": step8a_summary,
        "xray_summary":   xray_summary,
        "action_stack":   fas_block,
    }


def _fund_action_stack_block(path=None):
    """Read the pre-run's `fund_action_stack_[mmm].json` for the email.

    Absent file -> a STATED absence, never silence: if the fund sleeve went unassessed this
    month, the report says so rather than simply omitting the section, because an omitted
    section reads as 'nothing to report'.
    """
    import glob as _g
    try:
        cands = sorted(_g.glob(os.path.join(SCRIPT_DIR, "fund_action_stack_*.json")),
                       key=os.path.getmtime)
        if path:
            cands = [path]
        if not cands:
            return {"available": False,
                    "note": ("Fund Action Stack not produced this month (pre-run Step 6.05 did "
                             "not run or failed). The fund sleeve is 85% of the ISA and has "
                             "NOT been assessed against the anchor rule this month.")}
        with open(cands[-1], encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return {"available": False, "note": f"Fund Action Stack unreadable: {type(e).__name__}: {e}"}

    sm = d.get("summary", {})
    rows = []
    for x in d.get("fund_action_stack", []):
        r = next((y for y in d.get("fund_retention_score", [])
                  if y["sedol"] == x["sedol"]), {})
        wins = r.get("windows") or {}
        rows.append({
            "rank": x["rank"], "name": x["name"], "band": x["band"],
            "frs": x["frs"], "value_gbp": x.get("value_gbp"),
            "windows": " · ".join(f"{k} {v:.1f}%" for k, v in
                                  sorted(wins.items(), key=lambda kv: int(kv[0][:-1]))),
            "window_split": bool(r.get("window_split")),
            "bucket_minimum_pct": x.get("bucket_minimum_pct"),
            "anchor_rule_pass": x.get("anchor_rule_pass"),
            "action": x.get("action_required"),
            # ⚑ A7 (ISA-0440): the RANK and the WHY are now two different statements. The rank is
            # the sell order; `donor_why` names the criterion that put the fund THERE. Without it
            # the reader infers "rank 1 = worst fund", which is exactly the FRS-led reading A7
            # supersedes — B2PLJD7 heads the list because it is GBP 197 above its own declared
            # band_high, not because it scored badly.
            "donor_why": x.get("donor_why"),
            "why": "; ".join((r.get("rationale") or [])[:2]),
        })
    return {
        "available": True,
        "as_of": d.get("as_of"),
        "summary": sm,
        "headline": (f"{sm.get('n_funds', 0)} funds — HOLD/ADD {sm.get('hold_add', 0)} · "
                     f"RETAIN-ONLY {sm.get('retain_only', 0)} · WINDOW-SPLIT "
                     f"{sm.get('window_split', 0)} · DEAD MONEY {sm.get('dead_money', 0)}"
                     + (f" (£{sm.get('dead_money_value_gbp', 0):,.0f})"
                        if sm.get('dead_money_value_gbp') else "")),
        "rows": rows,
        # ⚑ RENDER THE ORDERING RULE, NOT JUST THE ORDER (ISA-0439's lesson: a computed thing
        # nobody renders reaches Raj as silence, and silence reads as its opposite). A DEGRADED
        # donor ordering means the list below is in the PRE-A7 FRS-led order, and the reader has
        # to be told that in the same breath as the list.
        "donor_ordering": (lambda o: {
            "state": o.get("state", "ABSENT"),
            "headline": ("Sell order: %s" % " > ".join(
                x.split(" ", 1)[0] + " " + x.split(" ", 1)[1][:34] for x in (o.get("order") or []))
                if o.get("state") == "MEASURED" else
                "⚑ SELL ORDER NOT REORDERED (%s) — the list below is in the PRE-A7 FRS-led order"
                % o.get("state", "ABSENT")),
            "basis": o.get("basis"),
            "frs_role": o.get("frs_role"),
        })(d.get("donor_ordering") or {}),
        "anchor_failures": d.get("anchor_rule_failures", []),
        "window_conflicts": d.get("dominance_window_conflicts", []),
        "disputed": (d.get("xray_cross_check") or {}).get("disputed", []),
        "method_note": (
            "FRS is the fund analogue of the stock Source Score: return adequacy 35 / "
            "risk-adjusted efficiency 25 / marginal diversification 20 / fee efficiency 10 / "
            "mandate integrity 10. Bands: >=%.0f HOLD/ADD, %.0f-%.0f RETAIN-ONLY (no new "
            "money), " % (_fas_bands()[0], _fas_bands()[1], _fas_bands()[0] - 1) +
            "<%.0f DEAD MONEY. " % _fas_bands()[1] +
            "Return adequacy is measured across 1y/3y/5y/10y and scored on the "
            "MEDIAN, because a single window is a bet on a start date, not a measurement -- "
            "Scottish Mortgage reads 0.2% over 5y and 22% over 3y purely because the 2022 "
            "-45.7% year is inside one window and outside the other. A fund whose windows "
            "DISAGREE is banded WINDOW-SPLIT and is explicitly NOT dead money. Points above "
            "the floor are scaled to the sleeve's 75th percentile, so clearing the minimum and "
            "beating it are no longer the same score; the bands were rebased in the same change "
            "so the ownership floor did not move as a side effect."),
    }


def build_s10(portfolio: dict, analytics: dict, run_date: date) -> dict:
    """Section 10 — Tax Year and ISA Allowance Tracker.
    Fix Pack A22 (P2): allowance comes from BROKER-RECONCILED contributions
    (extract_portfolio.parse_contributions), NEVER the assumed S/O schedule. If unreconciled,
    print "UNRECONCILED — verify AJ Bell" — no confident figure. A21: no S/O-resumption
    gating language anywhere."""
    tax_month = calc_tax_year_month(run_date)
    contrib = portfolio.get("contributions") or {}
    reconciled = bool(contrib.get("allowance_reconciled"))
    used = contrib.get("allowance_used_gbp")
    remaining = contrib.get("allowance_remaining_gbp")
    partial = contrib.get("allowance_used_partial_gbp") or 0.0
    note = contrib.get("coverage_note") or "no contributions data"

    if reconciled:
        kpis = [
            {"label": "Allowance Used", "value": f"£{used:,.0f}",
             "sub": f"Broker-reconciled from transaction history ({note})", "style": "normal"},
            {"label": "Allowance Remaining", "value": f"£{remaining:,.0f}",
             "sub": f"Of £{TAX_YEAR_ANNUAL:,.0f} annual allowance ({TAX_YEAR_LABEL})", "style": "info"},
            {"label": "Tax Year Month", "value": tax_month,
             "sub": f"{TAX_YEAR_LABEL} (started 6 Apr 2026)", "style": "normal"},
        ]
    else:
        kpis = [
            {"label": "Allowance Used", "value": "UNRECONCILED — verify AJ Bell",
             "sub": (f"{note}. Partial sum from available files: £{partial:,.0f}. Export the "
                     f"full tax-year 'ISA Transaction History' xlsx to reconcile (A22)."),
             "style": "warning"},
            {"label": "Allowance Remaining", "value": "UNRECONCILED",
             "sub": f"Cannot state confidently without reconciliation ({TAX_YEAR_LABEL})",
             "style": "warning"},
            {"label": "Tax Year Month", "value": tax_month,
             "sub": f"{TAX_YEAR_LABEL} (started 6 Apr 2026)", "style": "normal"},
        ]

    items = []
    for d in contrib.get("contributions_detail") or []:
        if d.get("type") == "PARSE_ERROR":
            items.append({"component": f"PARSE ERROR: {d.get('transaction')}", "amount": "—",
                          "status": "Error", "status_type": "pending"})
            continue
        items.append({
            "component": (f"{d.get('date')} — {d.get('transaction')} "
                          f"({'S/O' if d.get('type') == 'S/O' else 'Lump sum'})"),
            "amount": f"£{(d.get('amount_gbp') or 0):,.0f}",
            "status": "Reconciled" if reconciled else "From partial file",
            "status_type": "done" if reconciled else "pending",
        })
    if not items:
        items.append({"component": "No contribution transactions found in available files",
                      "amount": "—", "status": "Verify", "status_type": "pending"})

    notes = (
        (f"Broker-reconciled: £{used:,.0f} used, £{remaining:,.0f} remaining ({TAX_YEAR_LABEL}). "
         if reconciled else
         f"{note}. Do NOT state a confident figure until the transaction export covers the "
         f"full tax year. ")
        + "S/O status affects the CONVICTION BAR and position sizing only (A21) — it never "
          "gates deployment timing. "
          "[Claude: add running dealing/FX costs this tax year; dividend reinvestment "
          "reminders if any.]"
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
                "note":             _pf_trade_note_hint(),
            }
        ],
        "net_effect": "[Claude fills: stock sleeve rises/falls from X% to Y% post-trade.]",
    }


def skeleton_s2(analytics=None) -> dict:
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
        # Doc B standing lines (computed; render verbatim in §2)
        "standing_lines": _s2_standing_lines(analytics),
    }


def _s2_north_star_lines(analytics=None) -> list:
    """One line for the gap, one for the largest drags, one for the cheapest lever."""
    ra = _ra_load(analytics or {})
    sc = (ra.get("section_c") or {})
    v, a = sc.get("value_pct"), sc.get("anchor_pct")
    if v is None or a is None:
        return ["Total ISA expected return: NOT COMPUTED this run — Step 6.08 "
                "(return_architecture) did not produce a Section C. This is the number that "
                "says whether the plan is on track; its absence is a run failure, not a gap "
                "in the data."]
    gap = sc.get("shortfall_pp") or 0.0
    out = [f"Total ISA expected return: {v:.2f}% vs a required {a:.1f}% — "
           f"{'SHORT by ' + format(gap, '.2f') + 'pp' if gap > 0 else 'AHEAD by ' + format(abs(gap), '.2f') + 'pp'} "
           f"({sc.get('verdict')}), basis '{ra.get('operative_basis')}'"]
    rows = (ra.get("shortfall_attribution") or {}).get("rows") or []
    drags = [r for r in rows if (r.get("contribution_to_shortfall_pp") or 0) > 0][:3]
    if drags:
        out.append("Largest drags on the required return: " + " · ".join(
            f"{r['asset_id']} {r['contribution_to_shortfall_pp']:+.2f}pp "
            f"({r['weight_of_covered_pct']:.1f}% at {r['er_pct']:.1f}%)" for r in drags))
    lev = sorted((l for l in (ra.get("levers") or [])
                  if l.get("feasible") and l.get("delta_pp") is not None),
                 key=lambda l: -l["delta_pp"])[:2]
    if lev:
        out.append("Largest priced levers: " + " · ".join(
            f"{l['lever']} {l['delta_pp']:+.2f}pp" for l in lev)
            + " — priced independently, NOT additive; see §8 Section C.")
    blocked = [l for l in (ra.get("levers") or []) if not l.get("feasible")]
    for b in blocked:
        out.append(f"Lever BLOCKED — {b['lever']}: {b.get('blocked_reason')}")
    return out


def _s2_standing_lines(analytics=None) -> list:
    """B5 trajectory + B1 drawdown-ladder standing lines for §2 (+ A21 policy note)."""
    lines = []
    ts = load_json_optional(os.path.join(SCRIPT_DIR, "target_state.json"))
    lines.append(build_b5_trajectory(ts, date.today()))
    # ── ⚑ THE NORTH-STAR LINE, BESIDE THE ANCHOR IT IS MEASURED AGAINST ──────────────
    # Section A/B/C lives inside §8, which is the largest section in the report and sits 5th
    # in the lean triage order. On a full month §8 is dropped from the emailed copy — which
    # would take the answer to "am I on track for £1m" out of the email while leaving the
    # required return in it. The trajectory line states the BAR; this states where the
    # portfolio actually is against it, and the two belong together. Same reasoning that
    # raised §10 on 05-Aug: highest value per byte in the report.
    lines.extend(_s2_north_star_lines(analytics))
    ds = load_json_optional(os.path.join(SCRIPT_DIR, "drawdown_state.json"))
    if ds and ds.get("last_check"):
        lines.append(f"Drawdown ladder: {ds.get('drawdown_pct', 0):+.1f}% from 252d high | "
                     f"tranches fired {sum(1 for v in (ds.get('tranches_fired') or {}).values() if v)}/3 | "
                     f"reserve £{(ds.get('reserve_gbp') or 0):,.0f} | regime {ds.get('regime_state') or 'n/a'}")
    else:
        lines.append("Drawdown ladder: state not yet seeded — first monitor run populates (B1)")
    # A21: paused S/O RAISES the bar (+5 conviction, one size notch down); NEVER gates timing.
    lines.append("Paused-S/O policy (A21): conviction floor +5, starter size one notch down — "
                 "existing cash deploys whenever a name clears the RAISED bar; deployment "
                 "timing is NEVER conditioned on S/O resumption.")
    return lines


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
                "earliest_sale":  _pf_earliest_sale_hint(),
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


def build_s5_from_scored(scored: dict, step9: dict = None) -> dict:
    """
    Build s5 watchlist section from watchlist_scored.json output.
    Items are pre-populated with quantitative fields from normalise_adapter.py.
    Claude fills: detail_items paragraphs (thesis for top 3), excluded notes, conviction scores.

    Fix Pack P2 (P5/P7b + A2/A3/A4/D7): the PRIMARY column is the metric that orders the list —
    the unified Source Score (deployment_priority_rank). Conviction/part-scores are secondary.
    Growth rows only here — VCI rows render in their own sleeve table (no interleaved ranks).
    Each row carries E[r], stage, implied upside (FV) and ONE deploy verdict (t1_qualified).
    """
    raw_items = scored.get("s5_watchlist_rows", [])
    if not raw_items:
        return skeleton_s5()

    # Source-Score / gate-anatomy lookup from step9_pre (deployment_priority_rank + tiers)
    _s9row = {}
    for _sect in ("main_watchlist", "candidate_pool"):
        for _lst in ((step9 or {}).get(_sect) or {}).values():
            for _e in _lst or []:
                if _e.get("ticker"):
                    _s9row[_e["ticker"]] = _e

    # Map scored rows to email s5 table format
    items = []
    for row in raw_items:
        in_win = row.get("in_window", False)
        status = row.get("status", "Watchlist")
        # Add in-window marker to status
        if in_win:
            status = f"IN RANGE — {status}"
        _t = row.get("ticker", "—")
        _s9 = _s9row.get(_t, {})
        _er = _s9.get("expected_return_12_24m", row.get("expected_return_12_24m"))
        _t1q = _s9.get("t1_qualified")
        _reach = _entry_reach_for(_t, row)
        items.append({
            "rank":         row.get("rank", "—"),
            "ticker":       _t,
            "name":         row.get("name", "—"),
            # P5: PRIMARY column — the ranking metric (unified Source Score, 0-100)
            "source_score": _s9.get("source_score", "—"),
            "score":        row.get("score", "—"),          # secondary: raw parts (legacy display)
            "score_level":  row.get("score_level", "normal"),
            "conviction":   _s9.get("strategic_conviction_score", "—"),   # secondary
            "tier":         _s9.get("tier", "—"),
            "revision_stage": _s9.get("revision_stage", row.get("revision_stage", "—")),  # A3
            "expected_return_12_24m": (f"{_er:.1f}%" if isinstance(_er, (int, float)) else "—"),
            "implied_upside_fv": row.get("implied_upside_fv", "—"),       # D7 canonical
            "sector":       row.get("sector", "—"),
            # M2 (03-Aug-26): 19 of 49 August entries sat >100% BELOW the live price, every one
            # via the return-hurdle anchor — prices at which the required return is
            # ARITHMETICALLY GUARANTEED, not prices the market is offering. Printing them under
            # "Target buy (display)" reads as an instruction to wait for a level that will not
            # come. The value is still shown (hiding it would conceal what the anchor produced);
            # it now carries its reachability so the reader is not misled. Label only — this is
            # display-only under the A6 path and reorders nothing.
            "entry_level":  _entry_display(row.get("entry_level", "—"), _reach),
            "entry_reachability": (_reach or {}).get("reachability", "unknown"),
            "entry_reach_note":   (_reach or {}).get("basis"),
            "status":       status,
            # P7b/P5-T4: ONE verdict field drives the badge — never two contradicting texts.
            # ⚑ ISA-0442: the verdict no longer carries a size. It used to read "DEPLOY-ELIGIBLE
            # (starter)", where `starter` was t1_gates' own 1.5% cap — a size from a module that
            # is no longer allowed to have an opinion about pounds, and a number BELOW the V2.1
            # ladder's 3.5% STARTER. Eligibility and size are two answers from two places, and
            # printing them as one string is how they came to disagree.
            "deploy_verdict": (("DEPLOY-ELIGIBLE" if _t1q else
                                ("BLOCKED — see gates" if _t1q is False else "—"))),
            "evidence_confirmed": _s9.get("evidence_confirmed"),
            "size_authority": (_s9.get("size_authority")
                               or "position_sizing.target_pct(evidence_state) — see the V2.1 "
                                  "engine block for this name's ladder rung (ISA-0442)"),
            "status_type":  ("buy" if _t1q else "watchlist"),
        })
    # P5-T1: primary column must be monotonically non-increasing down the growth ranking
    items.sort(key=lambda r: (-(r["source_score"] if isinstance(r["source_score"], (int, float))
                                else -1), str(r["ticker"])))
    for _i, _r in enumerate(items, 1):
        _r["rank"] = _i

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
                f"<strong>Impl upside (FV):</strong> {row.get('implied_upside_fv','—')} | "
                f"<strong>Target gap (display):</strong> {row.get('target_upside','—') if 'target_upside' in row else '—'}",
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
        # P5-T3 legend: all three scores + what each governs
        "legend": ("Ranking is by SOURCE SCORE (0-100, unified screen=deploy — deployability "
                   "order). Conviction (45/60/75 bands) = decision readiness, secondary. "
                   "ACS = asymmetric-sleeve track (separate VCI table). Entry level is "
                   "'Target buy (display)' — never a ranking input. E[r] = expected 12-24m "
                   "return pa (A2); Stage = revision stage (A3); one deploy verdict per row "
                   "(T1 gate set: ns/stage/E[r]/clean-flags). Size mode (A5v3): full = "
                   "evidence-confirmed + conviction >= 75; starter = thin evidence, capped 1.5% "
                   "with a recorded scale-up trigger — tenure never gates or caps."),
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
    # A13 (P2): override P&L one-liner — filled from run_context override_log / ledger
    # reconcile counts; A9: ledger-path echo so the write is auditable from the email.
    _rc = load_json_optional(_latest_run_context_path()) if _latest_run_context_path() else {}
    _ov = (_rc.get("summary") or {}).get("override_log") or []
    _ov_line = (f"Overrides on record: {len(_ov)} — cumulative P&L vs framework: "
                f"{sum((o.get('pnl_vs_framework_gbp') or 0) for o in _ov):+,.0f} GBP to date"
                if _ov else "Overrides on record: none (A13 log active from this run)")
    return {
        "override_summary": _ov_line,
        "ledger_echo": f"Decision ledger: {os.path.join(SCRIPT_DIR, 'decision_ledger.json')} (A9 verified write)",
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
    # Base s7 from portfolio — BROKER TRUTH. Every share count, price, value, cost, gain and
    # weight in this section comes from portfolio_data and from nowhere else.
    base = build_s7(portfolio)
    sleeve_rows_scored = scored.get("s7_sleeve_rows", [])
    if not sleeve_rows_scored:
        return base

    # Build lookup by ticker
    scored_map = {r["ticker"]: r for r in sleeve_rows_scored}

    # ── 02-Aug-2026 (Aug retrospective item 5): PROVENANCE IS NOW ENFORCED, NOT ASSUMED ────
    #
    # watchlist_scored.s7_sleeve_rows priced AVGO at GBP 6,617.76 against the broker's
    # 4,915.78, MU at 5,761.21 against 4,277.67, and ONT at 18,471.20 against 997.92 -- the
    # last because ticker "ONT" resolved to "Onterris, Inc." rather than ONT.L Oxford Nanopore.
    # The errors are FX and GBp conversions applied inconsistently, plus one outright identity
    # error. Section 7 happened to be built from portfolio_data, so the emitted email was
    # right; a run that had trusted the scored file would have reported a stock sleeve roughly
    # 2.5x its real size.
    #
    # Two changes, because "it happened to be correct" is not a control:
    #   1. an ALLOW-LIST -- only non-monetary, non-identity fields may cross from the scored
    #      file. A future edit cannot widen the merge by accident;
    #   2. a RECONCILIATION -- where the scored file does carry a value or a name, it is
    #      compared against broker truth and any disagreement is SURFACED. Broker truth still
    #      wins; the point is that the discrepancy stops being invisible.
    MERGEABLE_FROM_SCORED = ("analyst_rating", "display_target_gap", "next_earnings",
                             "total_score", "total_max")
    MONETARY_OR_IDENTITY = ("name", "value_gbp", "market_value", "position_value", "cost_gbp",
                            "current_price", "price", "quantity", "shares", "weight_pct")
    discrepancies = []

    def _num(v):
        try:
            return float(str(v).replace("GBP", "").replace("\u00a3", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    for h in base["holdings"]:
        ticker = h.get("ticker", "")
        s = scored_map.get(ticker, {})
        if not s:
            continue
        # identity
        s_name, b_name = s.get("name"), h.get("name")
        if s_name and b_name and s_name.split()[0].lower() != str(b_name).split()[0].lower():
            discrepancies.append(
                f"{ticker}: scored file names it '{s_name}' but the broker file says "
                f"'{b_name}' -- the ticker has resolved to the wrong company. Broker truth used.")
        # monetary
        for f in ("value_gbp", "market_value", "position_value"):
            sv, bv = _num(s.get(f)), _num(str(h.get("value", "")).replace("\u00a3", ""))
            if sv is not None and bv and abs(sv - bv) / bv > 0.05:
                discrepancies.append(
                    f"{ticker}: scored {f}={sv:,.2f} vs broker {bv:,.2f} "
                    f"({(sv/bv - 1)*100:+.0f}%). Broker truth used.")
                break
        if s:
            # Override status note with analyst rating and target upside
            analyst = s.get("analyst_rating", "—")
            upside  = s.get("display_target_gap") or "—"   # D7; P3: target_upside fallback deleted
            ne      = s.get("next_earnings", "—")
            score   = s.get("total_score")
            # Only the allow-listed fields are consumed, and only into a TEXT note. No
            # monetary or identity field from the scored file reaches the rendered section.
            assert all(f not in MERGEABLE_FROM_SCORED for f in MONETARY_OR_IDENTITY), \
                "s7 merge allow-list must never contain a monetary or identity field"
            h["status_note"] = (
                f"Analyst: {analyst} | Target upside: {upside} | "
                f"Next earnings: {ne}"
                + (f" | Score: {score}/{s.get('total_max') or 50}" if score else "")
                + " | [Claude: update thesis status at Step 8]"
            )

    if discrepancies:
        base["provenance_warnings"] = discrepancies
        base["notes"] = (base.get("notes") or "") + (
            "\n\nDATA PROVENANCE: all share counts, prices, values and weights above are from "
            "the AJ Bell broker file. The scored file disagrees on the following and was NOT "
            "used for any of them: " + " ".join(discrepancies))
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
def build_vci_sleeve_from_step9(step9: dict) -> dict:
    """FWDVCI §14.8: build the monthly email's dedicated Asymmetric-Sleeve (VCI) table from the
    step9_pre vci_watchlist pass-through (T1_A -> T2_A -> T3_A, already ranked by VCI Source Score
    within tier). Returns None when absent so the renderer omits the table (safe no-op)."""
    if not step9:
        return None
    vw = step9.get("vci_watchlist", {}) or {}
    rows = []
    for tier in ("T1_A", "T2_A", "T3_A"):
        rows.extend(vw.get(tier, []) or [])
    if not rows:
        return None
    items = []
    for i, e in enumerate(rows, 1):
        items.append({
            "rank": i,
            "ticker": e.get("ticker", ""),
            "name": e.get("company", e.get("name", "")),
            "vci_source_score": e.get("vci_source_score"),
            "acs": e.get("acs_score", e.get("acs")),
            "fv_asymmetry": e.get("fv_asymmetry"),
            "fv_asymmetry_p25": e.get("fv_asymmetry_p25"),
            "fv_floor": e.get("fv_floor"),
            "days_to_catalyst": e.get("days_to_catalyst"),
            "deploy_eligible": e.get("deploy_eligible"),
            "status": e.get("decision_bucket", e.get("tier", "Watchlist")),
            "status_type": "watchlist",
        })
    out = {"items": items}
    meta = step9.get("_meta", {}) or {}
    if meta.get("calibration_gate"):
        out["calibration_gate"] = meta["calibration_gate"]
    if meta.get("vci_binary_risk_committed") is not None:
        out["risk_committed"] = meta["vci_binary_risk_committed"]
        out["risk_budget"] = meta.get("vci_binary_risk_budget")
    return out


# ---------------------------------------------------------------------------

# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0439 — THE V2.1 RENDER BLOCK
#
# ⚑ WHY THIS EXISTS. `monthly_isa_prerun` Step 6.12 computes the whole V2.1 stack every run —
# ladder targets, stock_max and its qualifying uses, correlation coverage, the ratchet
# population gate, realised_fraction, M1/M2/M3 — and writes it to `summary.v21`. Until this
# block, NOTHING RENDERED ANY OF IT. That is the MIRROR of this project's dominant failure
# class: not an absent execution reporting success, but a PRESENT execution reporting to
# nobody. `orchestrator_parity` and `pair_v21_modules_executed` prove a module RAN; neither
# proves its output was CONSUMED, and a number computed for a decision surface that never
# reaches the decision surface has not been computed at all.
#
# ⚑ EVERY FIGURE STATES ITS BASIS (R4.2) AND EVERY REFUSAL IS RENDERED AS A REFUSAL (R2.10).
# The most important lines this block prints today are the ones that say a thing could NOT be
# measured — "every position capped at STARTER because correlation is unmeasured" and "the
# ratchet cannot fire at n=1" — because both are live, both are correct, and both would
# otherwise reach Raj as silence.
# ═══════════════════════════════════════════════════════════════════════════════════════════

# The declared renderer map. `consistency_check.pair_v21_summary_has_renderer()` asserts that
# every key `summary.v21` carries is either named here or declared out of scope, so a future
# Step 6.12 addition cannot be computed and silently discarded (the FIELD_MAP hazard).
V21_RENDERED_KEYS = (
    "policy", "golden_fixture", "correlation_coverage", "ladder", "hard_caps",
    "ratchet_eligibility", "risk_monitors", "min_hold_exempt",
    "fund_active_drawdown",          # §9 / A7, added 26-Aug-2026 (ISA-0440)
    "plan_stability",                # A12 grid, added 26-Aug-2026 (ISA-0440)
    "slot_competition",              # A20 shadow, added 26-Aug-2026 (ISA-0440)
)
# ⚑ `slot_candidates` is an INPUT to Step 6.12g, not an output of it. Declared out of scope with
# its reason rather than left to fail the renderer check — silence is not a decision (R4.6.2).
V21_INPUT_ONLY_KEYS = ("slot_candidates",)
V21_OUT_OF_SCOPE_KEYS = V21_INPUT_ONLY_KEYS   # add here WITH A REASON, never by dropping a key


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0447 — THE MARGINAL-POUND ROUTER BLOCK (§2)
#
# ⚑ WHY §2 AND NOT §7 OR §8. The router does not belong to a sleeve: it decides the SPLIT
# BETWEEN sleeves, then ranks fund destinations for whatever is left. §7 is the stock sleeve and
# §8 is the fund sleeve, so neither owns the question. §2 — Monthly Capital Allocation — is the
# only section that owns both, and it is the section where Raj writes his OWN ranking of the
# eight action categories. Putting the machine's routing beside the human's decision in one
# place is the point: where they disagree, the disagreement is visible rather than inferred.
#
# ⚑ ONE SOURCE, AND IT IS THE RUN CONTEXT. This reads `summary.capital_destination` and
# `summary.waiting_room`, written by monthly_isa_prerun Step 6.10. It deliberately does NOT
# read `capital_destination_[mmm]_[yyyy].json`: a glob over that name also matches
# `capital_destination_sep_2026_scenario.json`, and a scenario rendered as the live plan is a
# stored value that says one thing and is another — this project's first failure class, and the
# exact shape of ISA-0398. The run context is written BY the run; the scenario file is not.
#
# ⚑ EVERY REFUSAL RENDERS AS A REFUSAL (R2.10). On the August book the single most important
# line here says the stock cap CANNOT BE EXECUTED — GBP 2,895.34 against a smallest declared
# position of GBP 4,890.84 — and the second says a declared band breach was NOT repaired and
# that whether a declared band is a preference or a limit HAS NOT BEEN DECIDED. Both are
# statements that the framework did not do something. Omitted for being negative they would
# reach Raj as silence, and silence reads as its opposite.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _gbp(v):
    return "UNAVAILABLE" if v is None else ("GBP %s" % format(float(v), ",.2f"))


def build_capital_router_block(cd: dict, wr: dict = None) -> dict:
    """summary.capital_destination (+ summary.waiting_room) -> the rendered §2 sub-block.

    Returns {} when the pre-run predates Step 6.10 or the router did not produce a state; the
    caller renders an explicit ABSENT notice in that case rather than an empty section, because
    a section that renders nothing is indistinguishable from a router that found nothing to do.
    """
    if not cd or not cd.get("state"):
        return {}
    out, warns = {}, []
    wr = wr or {}

    # ── 1. the split between sleeves ────────────────────────────────────────────────────────
    band = cd.get("declared_band_pct") or []
    out["split_line"] = (
        "Marginal-pound router (%s, as of %s): %s offered. Stock sleeve %s, cap %s; fund sleeve "
        "%s. Stock sleeve is %s%% against its declared %s band, %s to reach the floor."
        % (cd.get("state"), cd.get("as_of", "?"),
           _gbp(cd.get("amount_available_gbp")), cd.get("split_state", "?"),
           _gbp(cd.get("stock_max_gbp")), _gbp(cd.get("fund_max_gbp")),
           cd.get("sleeve_weight_now_pct", "?"),
           ("%s-%s%%" % (band[0], band[1])) if len(band) == 2 else "?",
           _gbp(cd.get("gbp_to_reach_band_floor"))))
    if cd.get("split_reason"):
        out["split_reason_line"] = cd["split_reason"]

    # ── 2. executability — a cap that cannot open a position is GBP 0 of executable capital ──
    ex = cd.get("executability") or {}
    if ex.get("state"):
        if ex["state"] == "NOT_EXECUTABLE":
            out["executability_line"] = (
                "STOCK CAP IS NOT EXECUTABLE: %s is below the smallest position the framework "
                "declares (%s, %s). This is GBP 0 of executable stock capital reported as a "
                "non-zero number, and it is the binding fact about this month's split "
                "(ISA-0387). It can top up an existing position; it cannot open one."
                % (_gbp(ex.get("stock_max_gbp")),
                   _gbp(ex.get("smallest_declared_position_gbp")),
                   ex.get("smallest_declared_position_basis", "?")))
            warns.append(out["executability_line"])
        else:
            out["executability_line"] = (
                "Stock cap %s is EXECUTABLE against the smallest declared position (%s, %s)."
                % (_gbp(ex.get("stock_max_gbp")),
                   _gbp(ex.get("smallest_declared_position_gbp")),
                   ex.get("smallest_declared_position_basis", "?")))

    # ── 3. the freeze, and WHOSE decision its basis is ──────────────────────────────────────
    if cd.get("freeze_basis"):
        out["freeze_line"] = (
            "Scaling freeze %s on basis `%s` (%s). The freeze binds only capital whose SOURCE is "
            "a disposal from the fund sleeve, so a subscription is not frozen by it; earliest "
            "mechanical unfreeze %s."
            % ("ACTIVE" if cd.get("freeze_active") else "inactive", cd["freeze_basis"],
               cd.get("freeze_declared_by", "declared"),
               cd.get("freeze_earliest_unfreeze", "?")))

    # ── 4. the ordering, and what is NOT allowed to order it ────────────────────────────────
    if cd.get("ranking_order"):
        out["ranking_line"] = (
            "Fund ordering (%s), C1 at %s resolution: %s. Trailing return: %s."
            % (cd.get("ranking_state", "?"), cd.get("c1_resolution", "?"),
               " -> ".join(cd["ranking_order"]),
               cd.get("trailing_return", "not stated")))

    # ── 5. where the money actually went ────────────────────────────────────────────────────
    alloc = cd.get("allocation_gbp") or {}
    phase = cd.get("phase_allocation") or {}
    bw = cd.get("band_weights") or {}
    phase_of = {}
    for ph, names in phase.items():
        for n in (names or {}):
            phase_of[n] = ph
    rows = []
    for sedol, amt in sorted(alloc.items(), key=lambda kv: -(kv[1] or 0)):
        w = bw.get(sedol) or {}
        rows.append({"sedol": sedol, "gbp": amt, "phase": phase_of.get(sedol, "-"),
                     "weight_before": w.get("before"), "weight_after": w.get("after"),
                     # ⚑ ROUNDED. The stored band_low is 4.569999999999999 - a float artefact
                     # of a derivation, not a declared precision. Printing it raw invites the
                     # reader to believe the framework declares bands to 15 significant figures.
                     "band": ("%.2f-%.2f%%" % (w.get("low"), w.get("high"))
                              if w.get("low") is not None and w.get("high") is not None
                              else "-")})
    out["allocation_rows"] = rows
    out["allocation_line"] = (
        "Fund allocation %s: %s across %d destination(s); %s unallocated."
        % (cd.get("fund_allocation_state", "?"),
           _gbp(sum((r["gbp"] or 0) for r in rows)), len(rows),
           _gbp(cd.get("unallocated_gbp"))))
    if not rows:
        out["allocation_line"] += (" NO fund received capital this run — that is a routing "
                                   "outcome, not an absence of data.")

    # ⚑ THE BLOCKED LIST IS THE HALF THAT EXPLAINS THE OTHER HALF. A destination table without
    # the reasons the other eleven funds received nothing reads as a preference; with them it
    # reads as a rule.
    blocked = {}
    for ph, names in (cd.get("blocked") or {}).items():
        for n, why in (names or {}).items():
            blocked.setdefault(n, []).append("%s: %s" % (ph, why))
    out["blocked_rows"] = ["%s - %s" % (n, " | ".join(v)) for n, v in sorted(blocked.items())]
    refused = cd.get("eligibility_refused") or {}
    out["refused_rows"] = ["%s - %s" % (k, v) for k, v in sorted(refused.items())]

    # ── 6. declared bands: what broke, what was repaired, and what was NOT decided ──────────
    nb = cd.get("band_not_repaired") or []
    out["band_line"] = (
        "Declared per-fund bands: %d breach(es) before this allocation, %d after; repaired %s; "
        "NOT repaired %s."
        % (len(cd.get("band_breaches_before") or []), len(cd.get("band_breaches_after") or []),
           ", ".join(cd.get("band_repaired") or []) or "none",
           ", ".join(nb) or "none"))
    if nb:
        warns.append("DECLARED BAND BREACH NOT REPAIRED: %s. Band restoration is C5, the "
                     "tie-break, so a breach is repaired only if the fund also wins on C1-C4."
                     % ", ".join(nb))
    if cd.get("band_choice_not_made"):
        out["band_choice_line"] = ("The choice not made: " + cd["band_choice_not_made"])

    # ── 7. idle capital is a DECISION with a stated price, never a default ──────────────────
    un = cd.get("unallocated_gbp")
    cost = cd.get("idle_cost_net_gbp")
    if (un or 0) > 0:
        out["idle_line"] = (
            "Idle capital %s (%.1f%% of what was offered), priced at %s/yr net of the "
            "waiting-room yield. %s"
            % (_gbp(un), cd.get("residual_pct_of_offered") or 0.0,
               (_gbp(cost) if cost is not None else "UNMEASURED - the waiting-room yield has not "
                "been observed, so the cost is not known and is NOT zero"),
               cd.get("idle_cost_basis", "")))
        warns.append(out["idle_line"])
    else:
        out["idle_line"] = ("Residual: %s - every pound offered reached a destination."
                            % cd.get("residual_state", "NONE"))

    # ── 8. the waiting room / recall leg ────────────────────────────────────────────────────
    if wr:
        out["waiting_room_line"] = (
            "Waiting room: %s parked across %s lot(s); recall %s.%s"
            % (_gbp(wr.get("parked_gbp")), wr.get("lots_live", "?"), wr.get("recall", "?"),
               (" " + wr["recall_reason"]) if wr.get("recall_reason") else ""))
        if wr.get("recall") in ("BARRED", "REFUSED") and (wr.get("parked_gbp") or 0) > 0:
            warns.append("PARKED CAPITAL IS LOCKED IN: %s sits in funds as a TIMING decision "
                         "and the recall leg is %s. Parking under an active freeze is not a "
                         "reversible decision." % (_gbp(wr.get("parked_gbp")), wr.get("recall")))

    # ── 9. did the ranking actually rank? ───────────────────────────────────────────────────
    inert = cd.get("parity_inert_criteria") or []
    if cd.get("parity_pass") is not None:
        out["parity_line"] = (
            "Router parity: %s. %s Two independent derivations of the allocation total %s."
            % ("PASS" if cd.get("parity_pass") else "FAILED",
               ("Every criterion was shown to move capital with the others neutralised."
                if not inert else "INERT criteria (they changed nothing under their own "
                                  "negative control): %s." % ", ".join(inert)),
               "agree" if cd.get("two_derivations_agree") else "DISAGREE"))
        if not cd.get("parity_pass") or inert:
            warns.append(out["parity_line"])

    out["warnings"] = warns
    out["_basis"] = ("Rendered from summary.capital_destination and summary.waiting_room, "
                     "written by monthly_isa_prerun Step 6.10 (a, b, c). Never read from "
                     "capital_destination_*.json - that glob also matches the September "
                     "scenario file.")
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0447 — THE DISPOSITION OF EVERY `summary[...]` KEY THE PRE-RUN WRITES
#
# ⚑ WHY THIS EXISTS AND WHY IT IS NOT `V21_RENDERED_KEYS` AGAIN. ISA-0439 built
# `pair_v21_summary_has_renderer` as a CLASS-KILLER for "a present execution reporting to
# nobody". It is scoped to the keys of `summary.v21`. Seventeen days later the whole
# marginal-pound router was found writing `summary.capital_destination` — a different key of the
# same dict — and reaching no surface at all. THE CHECK COULD NOT HAVE CAUGHT IT.
#
#   ⚑⚑ A CLASS-KILLER SCOPED TO ONE KEY IS AN INSTANCE-KILLER.
#
# So the disposition is now declared for EVERY top-level key, and `consistency_check.
# pair_summary_key_disposition()` fails the build on any key that is written and not declared.
# A new Step-anything output therefore cannot be computed and silently discarded; someone has to
# decide where it goes, which is the whole mechanism (R4.6.2 — silence is not a decision).
#
# ⚑ WHAT THE CHECK HONESTLY VERIFIES, AND WHAT IT DOES NOT. It verifies that a decision was
# RECORDED for every key, that a RENDERED key names a builder that exists, that an ESCALATED key
# names a warning prefix that a `warnings.append` in the pre-run actually emits, that an
# OUT_OF_SCOPE key carries a real reason, and that an UNADJUDICATED key names a LIVE register
# item. It does NOT trace the data path and does not claim to: an observer that re-derived the
# consumption it is checking would be ISA-0382 in a new place. The declaration is the decision;
# the check is that the decision exists and is internally consistent.
#
# ⚑ AND THE FOURTH BUCKET IS THE HONEST ONE. Adjudicating forty keys on the evidence available
# in one build would mean asserting "this is escalated" or "this is out of scope" where the true
# answer is "nobody has looked". UNADJUDICATED says exactly that, and it costs a LIVE register
# item to say it — so the backlog is visible in the register rather than laundered into a
# reassuring category here. When the item is closed, the check goes RED until each of its keys
# has moved to a real bucket. That is the pressure, and it is deliberate.
# ═══════════════════════════════════════════════════════════════════════════════════════════

# key -> the builder in THIS module that renders the quantity. Note the question is about the
# QUANTITY, not about who reads this dict (ISA-0442's discipline): several of these reach the
# email from analytics/scored rather than from the run context, and that is still rendered.
SUMMARY_RENDERED = {
    "capital_destination":        "build_capital_router_block",   # s2 — ISA-0447
    "waiting_room":               "build_capital_router_block",   # s2 — ISA-0447
    "v21":                        "build_v21_block",              # s7 — ISA-0439
    "plan_stability":             "build_v21_block",              # s7 — routed into summary.v21
    "fund_action_stack":          "_fund_action_stack_block",     # s8
    "donor_ordering":             "_fund_action_stack_block",     # s8 — A7 sell order
    "factor_lookthrough":         "build_s6",                     # s6
    "regime_state":               "_s2_standing_lines",           # s2 standing line
    "drawdown":                   "_s2_standing_lines",           # s2 B1 ladder line
    "return_architecture":        "_ra_load",                     # s8 Section C
    "section_a_verdict":          "build_s8",                     # s8 Step 8A
    "section_c_verdict":          "build_s8",                     # s8 Step 8A
    "section_c_pct":              "build_s8",                     # s8 Step 8A
    "fund_cache_status":          "build_s8",                     # s8 DEGRADED banner
    "allowance_used_gbp":         "build_s10",                    # s10 tax tracker
    "allowance_remaining_gbp":    "build_s10",
    "allowance_reconciled":       "build_s10",
    "allowance_note":             "build_s10",
    "vci_binary_risk_committed":  "build_vci_sleeve_from_step9",  # s5 VCI sleeve
    "vci_binary_risk_budget":     "build_vci_sleeve_from_step9",
    "vci_deploy_eligible":        "build_vci_sleeve_from_step9",
    "override_log":               "skeleton_s11",                 # s11 override P&L line
}

# key -> the warning-stage prefix that carries its decision content into the pre-run warning
# list, which the review reads before writing the email. ⚑ ADMISSIBLE ONLY where the key is a
# run-health status whose decision content IS the exception. A key with positive analytical
# content does not belong here — "it warns when it breaks" is not "its output reaches a
# surface", and treating the two as the same is the misreading ISA-0439 was raised about.
SUMMARY_ESCALATED = {
    "fund_exposure_vectors":      "Step 6.10a",   # age/provenance of a capture, not a finding
    "calibration_files":          "Calibration",  # stale/unstamped file list
    "calibration_fingerprint":    "Calibration",  # live-config hash match
    "calibration_preflight":      "Calibration",  # pool drift verdict
    "ledger_reconcile":           "Step 1.5",     # counts of confirmed/unconfirmed executions
    "ledger_reconcile_source":    "Step 1.5",
    "ledger_reconcile_confirmed": "Step 1.5",
    "off_framework_trades":       "Step 1.5",     # trades with no framework decision behind them
    "anchor_rederived":           "A19",          # the anchor moved this run
    "anchor_operative_moved":     "A19",
}

# key -> why the email is not its surface. A reason, never an empty string: a key parked here
# without one is a key nobody decided about wearing the costume of a decision.
SUMMARY_OUT_OF_SCOPE = {
    "run_manifest":            "run liveness metadata — consumed by the manifest and by "
                               "consistency_check, and a stage table is not a decision figure",
    "watchlist_tickers_scored": "a COUNT of the rows Section 5 renders in full; the table is the "
                                "render and a count beside it would be a second home for it",
    "in_window_names":          "the ticker list behind the same Section 5 table, for the same "
                                "reason",
    "vci_repriced":             "a COUNT of the rows the Section 5 VCI table renders in full",
    "calibration":              "paths to the calibration report and IC table — a pointer to an "
                                "artefact read during the run, not a figure to publish",
}

# key -> the LIVE register item tracking the decision that has not been made. ⚑ THIS IS NOT A
# WAIVER. The check asserts the item exists and is still open; closing it without moving these
# keys to a real bucket turns the check RED.
# ⚑ A TUPLE AND A SINGLE ITEM ID, NOT A COMPREHENSION. `consistency_check` reads this file with
# `ast.literal_eval` rather than a regex — ISA-0446 was a regex that truncated the declaration it
# validated at a paren inside a comment, and a checker whose false positive is indistinguishable
# from the defect it hunts teaches the reader to disbelieve it. A dict comprehension is not a
# literal, so it would have forced the regex back.
SUMMARY_UNADJUDICATED_ITEM = "ISA-0448"
SUMMARY_UNADJUDICATED = (
        "cash_statement", "transactions", "concentration", "process_concentration",
        "strategic_allocation", "t4_mandate_drift", "lookthrough", "fund_holdings_declared",
        "fund_categories", "regional_m", "fund_expected_return", "position_alerts",
        "missed_opportunity", "regimes", "shadow_ledger", "t1_revisions_crosstab",
        "conviction_capture", "watchlist_promotion_log", "reversal_worklist",
        "reversal_flag_tickers", "mmf_sweep", "phase_status", "rebalancing_candidates",
        "universe_price_coverage", "xray_1yr_return_pct", "xray_1yr_benchmark_pct",
        "vci_calibration_state",
)


def build_v21_block(v21: dict) -> dict:
    """summary.v21 -> the rendered s7 sub-block. Returns {} when the pre-run predates V2.1."""
    if not v21:
        return {}
    out, lines, warns = {}, [], []

    pol = v21.get("policy") or {}
    if pol:
        anchor = pol.get("anchor_operative_pct")
        out["policy_line"] = (
            "Policy %s. Required-return anchor %s%%, derived %s from a portfolio value of %s; "
            "next re-derivation %s. The anchor is a FUNCTION OF THE PORTFOLIO VALUE and the "
            "contribution schedule - it moves when they move, and no threshold derived from it "
            "is ever a stored constant."
            % (pol.get("policy_version", "?"),
               ("%.2f" % anchor) if anchor is not None else "UNAVAILABLE",
               pol.get("anchor_derived_at", "?"),
               ("GBP %s" % format(pol.get("anchor_portfolio_value_gbp"), ",.0f"))
               if pol.get("anchor_portfolio_value_gbp") else "?",
               pol.get("anchor_next_due", "?")))

    gf = v21.get("golden_fixture") or {}
    if gf.get("status") == "CHECKED":
        out["fixture_line"] = ("V2 behaviour-neutrality fixture: HOLDS (frozen %s)."
                               % gf.get("frozen_on")) if gf.get("holds") else             ("V2 GOLDEN FIXTURE BROKEN - %s. A declared policy constant or a DERIVATION moved. "
             "This is a decision, not a build." % "; ".join(gf.get("diffs", [])[:2]))
        if not gf.get("holds"):
            warns.append(out["fixture_line"])

    lad = v21.get("ladder") or {}
    if lad:
        out["ladder_line"] = ("Position ladder (FIXED): STARTER %s%% / NORMAL %s%% / HIGH %s%% / "
                              "EARNED_MAX %s%%. A position reaches STARTER or it does not exist."
                              % (lad.get("STARTER"), lad.get("NORMAL"),
                                 lad.get("HIGH"), lad.get("EARNED_MAX")))

    cov = v21.get("correlation_coverage") or {}
    if cov:
        n, meas = cov.get("n_names", 0), cov.get("n_measured", 0)
        if n == 0:
            out["correlation_line"] = (
                "Correlation: UNMEASURED - the weekly GBP total-return store is EMPTY. Under "
                "A2.3 an unmeasured correlation is ADVERSE (rho = max(rho_bar, 0.70)), so EVERY "
                "position is capped at STARTER until 52 weeks of Friday-to-Friday closes exist. "
                "This is a MEASURED REFUSAL, not an estimate of zero - and it is the binding "
                "constraint on sizing today.")
            warns.append(out["correlation_line"])
        else:
            out["correlation_line"] = ("Correlation coverage: %d of %d names measured (minimum "
                                       "%d weekly returns)." % (meas, n, cov.get("min_weeks", 52)))
            short = [(t, r.get("weeks_to_minimum")) for t, r in (cov.get("names") or {}).items()
                     if r.get("status") == "UNMEASURED"]
            if short:
                out["correlation_short"] = [
                    "%s: %d more weekly closes needed before it can be sized above STARTER"
                    % (t, w or 0) for t, w in sorted(short)]

    ratch = v21.get("ratchet_eligibility") or {}
    if ratch:
        if not ratch.get("eligible"):
            out["ratchet_line"] = (
                "Step-down ratchet: CANNOT FIRE - %d forward-led decision(s) against %d required "
                "(%s). This is correct, not a loophole: measuring the framework on a book it did "
                "not assemble is measuring the wrong thing, in either direction."
                % (ratch.get("n_forward_led", 0), ratch.get("min_required", 3),
                   ", ".join(ratch.get("forward_led") or []) or "none"))
        else:
            out["ratchet_line"] = ("Step-down ratchet: population is real (%d forward-led "
                                   "decisions) and the rule may be evaluated."
                                   % ratch.get("n_forward_led", 0))
        out["ratchet_excluded"] = ["%s - %s" % (e.get("ticker"), e.get("reason"))
                                   for e in (ratch.get("excluded") or [])]

    mon = v21.get("risk_monitors") or {}
    if mon:
        rows = []
        for k, label in (("M1", "Bindingness"), ("M2", "Predictive validity"),
                         ("M3", "Decision value")):
            m = mon.get(k) or {}
            rows.append({"measure": "%s %s" % (k, label),
                         "verdict": m.get("verdict", "?"),
                         "detail": (m.get("detail") or "")[:240]})
            if m.get("verdict") in ("NON_INFORMATIVE", "STOP_ACTING"):
                warns.append("%s %s: %s" % (k, m.get("verdict"), m.get("detail")))
        out["risk_monitor_rows"] = rows

    # ── §9 ACTIVE-FUND DRAWDOWN, behind the A7 benchmark precondition (ISA-0440) ────────────
    # ⚑ THE UNMEASURED READINGS ARE PRINTED, and that is a deliberate choice rather than clutter.
    # Eleven of twelve funds have fewer completed own-history episodes than the declared minimum,
    # so no state can be assigned to them — but SMT is 56.6% behind VWRL.L on the active index and
    # printing only the one fund that clears the sample threshold would tell Raj the sleeve is
    # fine. "We cannot measure this" and "there is nothing here" are different sentences (R2.10).
    fad = v21.get("fund_active_drawdown") or {}
    if fad:
        bm = fad.get("benchmark_precondition") or {}
        out["s9_precondition_line"] = (
            "s9 active-fund drawdown: benchmark registry %s (%d comparators, %d error(s)). %s"
            % (bm.get("state", "?"), bm.get("n_comparators", 0), len(bm.get("errors") or []),
               ("A dividend-less benchmark OVERSTATES a fund's relative return, so a dirty "
                "registry would SUPPRESS this flag - every fund reads UNMEASURED until it is "
                "clean (A7)." if not bm.get("clean") else
                "Clean, so the flag is entitled to run.")))
        if not bm.get("clean"):
            warns.append(out["s9_precondition_line"])
        rows = fad.get("funds") or []
        meas = [r for r in rows if r.get("state") != "UNMEASURED"]
        out["s9_line"] = (
            "s9 read %d of %d funds; %d carry enough completed own-history episodes for a state, "
            "%d do not and are CAPPED AT CURRENT rather than assigned one from a thin sample "
            "(A6/R4.10). Measured on MONTHS, the resolution the NAV series is published at."
            % (fad.get("n_read", 0), fad.get("n_funds", 0), len(meas), len(rows) - len(meas)))
        out["s9_rows"] = [
            {"sedol": r["sedol"], "comparator": r["comparator"], "state": r["state"],
             "drawdown_pct": r["current_active_drawdown_pct"],
             "months": r["months_since_peak"], "episodes": r["n_completed_episodes"],
             "action": r["size_action"]}
            for r in sorted(rows, key=lambda x: x["current_active_drawdown_pct"])[:6]]
        for r in meas:
            if r.get("size_action") in ("TRIM_CANDIDATE", "REVIEW"):
                warns.append("s9 %s: %s vs %s, %.1f%% over %d months"
                             % (r["state"], r["sedol"], r["comparator"],
                                r["current_active_drawdown_pct"], r["months_since_peak"]))
        for u in fad.get("unreadable", []):
            warns.append("s9 UNREADABLE (counted, not dropped): %s - %s"
                         % (u["sedol"], u["error"]))

    # ── A12 PLAN STABILITY (ISA-0440) ───────────────────────────────────────────────────────
    # ⚑ A STABLE PLAN AND AN UNCONSULTED INPUT LOOK IDENTICAL IN A GRID OF ZEROES, so this
    # renders the mechanism and not only the numbers. "rho +/-0.05 changes nothing" is reported
    # together with WHY — rho is unmeasured, and an unmeasured rho already caps every position at
    # STARTER, so the plan is at a floor rather than indifferent.
    ps = v21.get("plan_stability") or {}
    if ps:
        out["stability_line"] = (
            "Plan stability (A12): %s"
            % (ps.get("reading") or "grid produced no reading"))
        out["stability_rows"] = [
            {"perturbation": g.get("perturbation"),
             "churn_gbp": g.get("pounds_churned_gbp"),
             "churn_pct": (round((g.get("churn_share_of_plan") or 0) * 100, 1)),
             "destinations_changed": bool(g.get("receiver_set_changed")),
             "order_changed": bool(g.get("order_changed"))}
            for g in (ps.get("grid") or [])]
        if ps.get("unstable"):
            warns.append("A12 PLAN UNSTABLE under %s - the lexicographic ranking is resolving "
                         "noise rather than economics." % ", ".join(ps["unstable"]))
        nai = [k for k, v in (ps.get("not_an_input") or {}).items() if not v]
        if nai:
            out["stability_not_an_input_line"] = (
                "%s are NOT inputs to the fund plan (measured by AST, not asserted), so they are "
                "not perturbed here - printing '0%% change' for a quantity the plan never reads "
                "would publish a fabricated reassurance. They are perturbed where they DO bite, "
                "in the demand-pull sizing rule." % ", ".join("`%s`" % k for k in sorted(nai)))
        _sk = ps.get("stock_side") or {}
        if _sk.get("state") == "SYNTHETIC_PROBE":
            out["stability_probe_line"] = (
                "The stock-side leg ran on a SYNTHETIC probe candidate, not on this month's "
                "book: it shows what the rule does, and sizes nothing. " + (_sk.get("probe_note") or ""))
        for gg in (_sk.get("grid") or []):
            if gg.get("note"):
                warns.append("A12 %s: %s" % (gg.get("perturbation"), gg["note"]))

    # ── A20 SHADOW SLOT COMPETITION (ISA-0440) ──────────────────────────────────────────────
    # ⚑ "NOTHING WAS PROPOSED" AND "NO COMPARISON HAPPENED" ARE DIFFERENT SENTENCES and this
    # prints whichever is true. A shadow rule that renders as an empty section reads as a rule
    # that looked and found nothing, which is the most flattering possible misreading of a rule
    # that has not run.
    sc = v21.get("slot_competition") or {}
    if sc:
        log = sc.get("shadow_log") or {}
        out["a20_line"] = (
            "A20 slot competition: %s. Ceiling %s this run. %d candidate pair(s) compared; "
            "shadow run %d of %d recorded. %s"
            % (sc.get("mode", "SHADOW"),
               ("BINDS" if sc.get("binding_ceiling") else "does NOT bind - a qualified "
                "challenger is funded from capital, so there is no slot to compete for"),
               sc.get("n_candidates", 0), log.get("runs_recorded", 0),
               max(log.get("runs_recorded", 0), 2),
               ("Nothing was compared, which is NOT the same as nothing qualifying."
                if not sc.get("n_candidates") else "")))
        out["a20_rows"] = [
            {"incumbent": v.get("incumbent"), "challenger": v.get("challenger"),
             "verdict": v.get("verdict"), "raw_gap": v.get("raw_gap_pp"),
             "bar": v.get("bar_pp"), "advantage": v.get("advantage_pp")}
            for v in (sc.get("verdicts") or [])]
        for v in (sc.get("verdicts") or []):
            if v.get("verdict") == "WOULD_REPLACE":
                warns.append("A20 SHADOW would replace %s with %s: %s"
                             % (v.get("incumbent"), v.get("challenger"), v.get("detail")))

    if v21.get("min_hold_exempt"):
        out["min_hold_line"] = ("Min-hold exemptions: %s. `evidence_reversal` was added 26-Aug-2026 "
                                "- without it the DEGRADED machine would be inert until the first "
                                "position clears 182 days."
                                % ", ".join(v21["min_hold_exempt"]))

    out["warnings"] = warns
    out["_basis"] = ("Rendered from summary.v21, written by monthly_isa_prerun Step 6.12. "
                     "Every figure carries the run that produced it; a refusal is rendered as a "
                     "refusal and never as a zero.")
    return out


def build_prefilled_email(
    portfolio: dict,
    analytics: dict,
    xray: dict,
    scored: dict,
    run_date: date,
    step9: dict = None,
) -> dict:
    has_scored = bool(scored and scored.get("s5_watchlist_rows"))
    _s5 = build_s5_from_scored(scored, step9) if has_scored else skeleton_s5()
    _vci_sleeve = build_vci_sleeve_from_step9(step9)
    if _vci_sleeve:
        _s5["vci_sleeve"] = _vci_sleeve
    # A14 (WP-1/WP-4, 26-Jul-26): dual-challenger counterfactual + scaling-freeze status;
    # code authoritative (email_prefill), prices live in sleeve_counterfactual.json.
    _cf_src = load_json_optional(os.path.join(SCRIPT_DIR, "sleeve_counterfactual.json"))
    _sleeve_now = (_cf_src.get("sleeve_value_now")
                   or portfolio["summary"].get("stock_sleeve_value_gbp"))
    _mu_now = next((x.get("value_gbp") for x in (portfolio.get("stocks") or [])
                    if str(x.get("ticker", "")).upper() == "MU"), 0.0)
    if _cf_src.get("trades"):
        _ch = compute_challenger_counterfactuals(
            _cf_src["trades"], _cf_src.get("vuag_price_now"),
            _cf_src.get("iwmo_price_now"), _sleeve_now, _mu_now)
        _frz = compute_freeze_status(_cf_src.get("freeze_history"))
        _ch["line"] += " - scaling freeze: %s (WP-4)" % _frz["status"]
        _cf = {"status": "OK", "sleeve_vs_vuag_pp": _ch.get("vs_vuag_pp"),
               "line": _ch["line"], "challengers": _ch, "freeze": _frz}
    else:
        _cf = {"status": "PENDING_BACKFILL",
               "line": ("Sleeve vs VUAG counterfactual: PENDING backfill - create "
                        "sleeve_counterfactual.json with {trades:[{date, amount_gbp, "
                        "vuag_price}], vuag_price_now} from statements/xlsx (A14)")}
    _pilot = compute_pilot_line(_cf_src.get("pilots"), _cf_src.get("pilot_prices_now") or {})
    _s7_out = build_s7_from_scored(portfolio, scored) if has_scored else build_s7(portfolio)
    _s7_out["vuag_counterfactual"] = _cf
    if _pilot:
        _s7_out["gold_pilot_line"] = _pilot.get("line")
    # ── ISA-0439: the V2.1 block. Read from the run_context the pre-run wrote. ───────────
    _rc = {}
    _ml2 = (portfolio.get("_meta", {}) or {}).get("month_label")
    if _ml2:
        _rc = load_json_optional(os.path.join(SCRIPT_DIR, f"run_context_{_ml2}.json")) or {}
    _v21 = ((_rc.get("summary") or {}).get("v21")) or {}
    _v21_block = build_v21_block(_v21)
    if _v21_block:
        _s7_out["v21"] = _v21_block
    else:
        _s7_out["v21"] = {"absent_line": (
            "V2.1 engine output ABSENT from this run's context. Step 6.12 either did not run or "
            "did not write summary.v21, so the ladder target, stock_max, correlation coverage and "
            "the retention verdicts are NOT available for this review. Do not infer them.")}

    # ── ISA-0447: the marginal-pound router block, read from the SAME run context ────────
    _s2_out = skeleton_s2(analytics)
    _cap = build_capital_router_block(((_rc.get("summary") or {}).get("capital_destination")),
                                      ((_rc.get("summary") or {}).get("waiting_room")))
    if _cap:
        _s2_out["capital_router"] = _cap
    else:
        _s2_out["capital_router"] = {"absent_line": (
            "Marginal-pound router output ABSENT from this run's context. Step 6.10 either did "
            "not run or did not write summary.capital_destination, so the stock/fund split, the "
            "stock cap and its executability, the fund allocation, the declared-band state and "
            "the price of idle capital are NOT available for this review. Do not infer them, and "
            "do not read capital_destination_*.json in their place - that glob also matches the "
            "September scenario file, which is not this run.")}

    _s7_out["probation_rule"] = ("D6: if the sleeve trails the VUAG counterfactual by >5pp "
                                 "cumulative after 12mo with >=3 positions, Phase-1 target "
                                 "reverts to the 10% floor and increments route to VUAG (A14)")

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
        "s2_capital_allocation":  _s2_out,
        "s3_investment_cases":    build_s3_from_scored(scored) if has_scored else skeleton_s3(),
        "s4_liquidation_tracker": skeleton_s4(),
        "s5_watchlist":           _s5,
        "s6_portfolio_snapshot":  build_s6(portfolio, analytics, xray),
        "s7_stock_sleeve":        _s7_out,
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
    parser.add_argument("--step9",     default=None,
                        help="Path to step9_pre_mmm_yyyy.json (VCI sleeve table); auto-discovered beside if omitted")
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
    # FWDVCI §14.8: auto-discover step9_pre for the VCI sleeve table (optional; safe no-op if absent)
    _ml = (portfolio.get("_meta", {}) or {}).get("month_label")
    step9 = {}
    _s9p = args.step9 if args.step9 else (os.path.join(SCRIPT_DIR, f"step9_pre_{_ml}.json") if _ml else None)
    if _s9p and os.path.exists(_s9p):
        step9 = load(_s9p, "step9_pre JSON", required=False)

    run_date = date.today()
    data = build_prefilled_email(portfolio, analytics, xray, scored, run_date, step9=step9)

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
