#!/usr/bin/env python3
"""
vci_acs_scorer.py — VCI Part A Auto-Scorer + ACS4 Calculator
Version: 1.0 | June 2026
Implements VCI_Asymmetric_Scorecard.md v2.0 (Part A, 13 metrics, max 26 points)

Automatable from yfinance (11 of 13):  A1 A2 A3 A4 A5 A6 A7 A8 A9 A12 A13
Manual input required:                 A10 (revenue quality — web search)
                                       A11 (analyst revisions — Finnhub)

Usage:
    python vci_acs_scorer.py RXRX ONT.L ABCL
    python vci_acs_scorer.py RXRX ONT.L --a10 "RXRX:1,ONT.L:2" --a11 "RXRX:2,ONT.L:2"
    python vci_acs_scorer.py RXRX --a9-strategic            (treats A9 5-10%/yr as strategic raise)
    python vci_acs_scorer.py RXRX ONT.L --json-out scores.json

GBp correction applied automatically for UK .L stocks.
All N/A fields excluded from denominator; threshold and ACS4 computed accordingly.

Threshold rules (VCI_Asymmetric_Scorecard.md v2.0):
  Standard:           raw_score >= 11  → proceed to Part B
  Pre-inflection:     raw_score >= 7   AND A5=2 AND mktcap <$5B AND no FCF+ in last 3 years
"""

import sys
import math
import json
import argparse
from datetime import datetime

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance --break-system-packages")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
UK_SUFFIXES = ('.L', '.l')
CURRENT_YEAR = datetime.now().year

# Sector → Tier mapping for A3 gross margin thresholds
TIER_A_SECTORS = {'Technology', 'Communication Services'}
HARDWARE_INDUSTRIES = {
    'Semiconductors', 'Semiconductor Equipment', 'Electronic Components',
    'Computer Hardware', 'Scientific & Technical Instruments',
    'Electronic Components & Parts',
}
# Sectors where R&D intensity (A5) is applicable
RD_APPLICABLE_SECTORS = {'Technology', 'Healthcare', 'Communication Services'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_uk(sym):
    return any(sym.upper().endswith(s.upper()) for s in UK_SUFFIXES)


def gbp_fix(val, sym):
    if is_uk(sym) and isinstance(val, (int, float)) and not math.isnan(val) and val > 500:
        return val / 100.0
    return val


def safe(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def cagr_from_sorted(values):
    """
    Compute CAGR from a list of values [oldest, ..., newest].
    Returns (rate, n_periods) or (None, 0) if insufficient data.
    """
    vals = [v for v in values if v is not None and v > 0]
    if len(vals) < 2:
        return None, 0
    n = len(vals) - 1
    rate = (vals[-1] / vals[0]) ** (1.0 / n) - 1
    return rate, n


def fmt_pct(val):
    if val is None:
        return 'N/A'
    return f'{val * 100:.1f}%'


def fmt_b(val):
    """Format in billions/millions."""
    if val is None:
        return 'N/A'
    v = float(val)
    if abs(v) >= 1e9:
        return f'{v / 1e9:.2f}B'
    if abs(v) >= 1e6:
        return f'{v / 1e6:.1f}M'
    return f'{v:.0f}'


def detect_tier(sector, industry):
    """Return 'A' (Software/SaaS/Platform) or 'B' (Hardware/Industrial/Bio)."""
    if sector in TIER_A_SECTORS and industry not in HARDWARE_INDUSTRIES:
        return 'A'
    return 'B'


def is_rd_applicable(sector):
    return sector in RD_APPLICABLE_SECTORS


# ---------------------------------------------------------------------------
# MetricResult — carries score + display info
# ---------------------------------------------------------------------------
class MR:
    def __init__(self, score, raw='', note='', na=False):
        self.score = score     # int: 0/1/2 (or None if NA)
        self.raw = raw         # display string of raw value(s)
        self.note = note       # extra note
        self.na = na           # True → excluded from denominator

    def display_score(self):
        if self.na:
            return 'N/A'
        return str(self.score)


# ---------------------------------------------------------------------------
# Data pull
# ---------------------------------------------------------------------------
def pull_data(sym):
    """Pull all yfinance data needed for Part A scoring. Returns dict."""
    t = yf.Ticker(sym)
    uk = is_uk(sym)
    d = {'sym': sym, 'uk': uk, 'error': None}

    try:
        info = t.info
        price_raw = safe(info.get('currentPrice') or info.get('regularMarketPrice'))
        d['price'] = gbp_fix(price_raw, sym)
        d['target'] = gbp_fix(safe(info.get('targetMeanPrice')), sym)
        d['hi52'] = gbp_fix(safe(info.get('fiftyTwoWeekHigh')), sym)
        d['lo52'] = gbp_fix(safe(info.get('fiftyTwoWeekLow')), sym)
        d['mktcap'] = safe(info.get('marketCap'))
        d['n_analysts'] = safe(info.get('numberOfAnalystOpinions'))
        d['sector'] = info.get('sector') or 'Unknown'
        d['industry'] = info.get('industry') or 'Unknown'
        d['name'] = info.get('longName') or info.get('shortName') or sym
    except Exception as e:
        d['error'] = f'info: {e}'
        return d

    # Income statement (annual)
    try:
        inc = t.income_stmt
        d['rev'] = {}
        d['gp'] = {}
        d['rnd'] = {}
        d['opinc'] = {}
        d['shares'] = {}
        for col in inc.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            d['rev'][yr] = safe(inc.loc['Total Revenue', col] if 'Total Revenue' in inc.index else None)
            d['gp'][yr] = safe(inc.loc['Gross Profit', col] if 'Gross Profit' in inc.index else None)
            for rk in ['Research And Development', 'ResearchAndDevelopment', 'Research Development']:
                if rk in inc.index:
                    d['rnd'][yr] = safe(inc.loc[rk, col])
                    break
            else:
                d['rnd'][yr] = None
            d['opinc'][yr] = safe(inc.loc['Operating Income', col] if 'Operating Income' in inc.index else None)
            for sk in ['Diluted Average Shares', 'Diluted Shares Issued', 'DilutedAverageShares']:
                if sk in inc.index:
                    d['shares'][yr] = safe(inc.loc[sk, col])
                    break
            else:
                d['shares'][yr] = None
    except Exception as e:
        d['rev'] = {}; d['gp'] = {}; d['rnd'] = {}
        d['opinc'] = {}; d['shares'] = {}

    # Cash flow
    try:
        cf = t.cashflow
        d['fcf'] = {}
        d['capex'] = {}
        d['ocf'] = {}
        for col in cf.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            d['fcf'][yr] = safe(cf.loc['Free Cash Flow', col] if 'Free Cash Flow' in cf.index else None)
            d['capex'][yr] = safe(cf.loc['Capital Expenditure', col] if 'Capital Expenditure' in cf.index else None)
            d['ocf'][yr] = safe(cf.loc['Operating Cash Flow', col] if 'Operating Cash Flow' in cf.index else None)
    except Exception:
        d['fcf'] = {}; d['capex'] = {}; d['ocf'] = {}

    # Balance sheet
    try:
        bs = t.balance_sheet
        d['cash'] = {}
        d['debt'] = {}
        d['assets'] = {}
        d['cur_liab'] = {}
        for col in bs.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            c = safe(bs.loc['Cash And Cash Equivalents', col] if 'Cash And Cash Equivalents' in bs.index else None)
            sti = safe(bs.loc['Other Short Term Investments', col] if 'Other Short Term Investments' in bs.index else None)
            d['cash'][yr] = (c or 0) + (sti or 0) if (c is not None or sti is not None) else None
            d['debt'][yr] = safe(bs.loc['Total Debt', col] if 'Total Debt' in bs.index else None)
            d['assets'][yr] = safe(bs.loc['Total Assets', col] if 'Total Assets' in bs.index else None)
            d['cur_liab'][yr] = safe(bs.loc['Current Liabilities', col] if 'Current Liabilities' in bs.index else None)
    except Exception:
        d['cash'] = {}; d['debt'] = {}; d['assets'] = {}; d['cur_liab'] = {}

    # Quarterly revenue for A2 (TTM acceleration)
    try:
        q_inc = t.quarterly_income_stmt
        q_rev = []
        for col in q_inc.columns[:8]:
            val = safe(q_inc.loc['Total Revenue', col] if 'Total Revenue' in q_inc.index else None)
            q_rev.append(val)
        d['q_rev'] = q_rev   # [most_recent, q-1, q-2, ..., q-7]
    except Exception:
        d['q_rev'] = []

    return d


# ---------------------------------------------------------------------------
# Scoring functions — one per metric
# ---------------------------------------------------------------------------

def score_a1(d):
    """A1: Revenue CAGR 3yr. >40%=2, 20-40%=1, <20%=0."""
    rev = d.get('rev', {})
    yrs = sorted([y for y, v in rev.items() if v and v > 0])
    if len(yrs) < 2:
        return MR(0, 'N/A — insufficient revenue history', 'Cannot compute CAGR')
    oldest, newest = yrs[0], yrs[-1]
    n = int(newest) - int(oldest)
    if n == 0:
        return MR(0, 'N/A — single year', '')
    rate, _ = cagr_from_sorted([rev[y] for y in yrs])
    raw = f'{fmt_pct(rate)} ({n}yr CAGR, {oldest}→{newest})'
    if rate is None:
        return MR(0, 'N/A', 'Cannot compute')
    if rate > 0.40:
        return MR(2, raw)
    elif rate >= 0.20:
        return MR(1, raw)
    else:
        return MR(0, raw)


def score_a2(d):
    """A2: Revenue Acceleration — TTM growth vs prior year growth. >5pp accel=2, within 5pp=1, decel=0."""
    q_rev = d.get('q_rev', [])
    rev = d.get('rev', {})
    yrs_sorted = sorted([y for y, v in rev.items() if v and v > 0])

    # Try TTM from quarterly data (need 8 quarters for TTM vs prior TTM)
    ttm_growth = None
    prior_growth = None
    method = ''

    if len(q_rev) >= 4 and all(v is not None for v in q_rev[:4]):
        ttm = sum(q_rev[:4])
        # Prior 12mo: quarters 4-7
        if len(q_rev) >= 8 and all(v is not None for v in q_rev[4:8]):
            prior_12mo = sum(q_rev[4:8])
            if prior_12mo > 0:
                ttm_growth = ttm / prior_12mo - 1
                # Prior year's growth: annual[0] / annual[1] as proxy
                if len(yrs_sorted) >= 2:
                    y0, y1 = yrs_sorted[-1], yrs_sorted[-2]
                    if rev[y1] and rev[y1] > 0:
                        prior_growth = rev[y0] / rev[y1] - 1
                    method = 'quarterly TTM vs prior-TTM'
        elif len(yrs_sorted) >= 2:
            # Fallback: TTM vs most recent annual
            y0, y1 = yrs_sorted[-1], yrs_sorted[-2]
            if rev[y0] and rev[y0] > 0:
                ttm_growth = ttm / rev[y0] - 1
                if rev[y1] and rev[y1] > 0:
                    prior_growth = rev[y0] / rev[y1] - 1
            method = 'quarterly TTM vs annual fallback'

    # Annual fallback if quarterly insufficient
    if ttm_growth is None and len(yrs_sorted) >= 3:
        y0, y1, y2 = yrs_sorted[-1], yrs_sorted[-2], yrs_sorted[-3]
        if rev[y0] and rev[y1] and rev[y0] > 0 and rev[y1] > 0 and rev[y2] > 0:
            ttm_growth = rev[y0] / rev[y1] - 1
            prior_growth = rev[y1] / rev[y2] - 1
            method = 'annual fallback'

    if ttm_growth is None or prior_growth is None:
        return MR(0, 'N/A — insufficient quarterly data', 'Cannot assess TTM acceleration')

    delta_pp = (ttm_growth - prior_growth) * 100
    raw = f'TTM growth {fmt_pct(ttm_growth)}, prior {fmt_pct(prior_growth)}, delta {delta_pp:+.1f}pp [{method}]'
    if delta_pp > 5:
        return MR(2, raw, 'Accelerating >5pp')
    elif delta_pp >= -5:
        return MR(1, raw, 'Stable within 5pp')
    else:
        return MR(0, raw, 'Decelerating')


def score_a3(d):
    """A3: Gross Margin — sector-adjusted. Tier A: >60%=2, 40-60%=1, <40%=0. Tier B: >35%=2, 20-35%=1, <20%=0."""
    rev = d.get('rev', {}); gp = d.get('gp', {})
    yrs = sorted([y for y in rev if rev.get(y) and gp.get(y) and rev[y] > 0])
    if not yrs:
        return MR(0, 'N/A — no gross profit data', na=False)
    yr = yrs[-1]  # most recent year
    gm = gp[yr] / rev[yr]
    tier = detect_tier(d.get('sector', ''), d.get('industry', ''))
    raw = f'{fmt_pct(gm)} [{yr}] (Tier {tier}: {d.get("sector","")}/{d.get("industry","")})'
    if tier == 'A':
        s = 2 if gm > 0.60 else (1 if gm >= 0.40 else 0)
    else:
        s = 2 if gm > 0.35 else (1 if gm >= 0.20 else 0)
    return MR(s, raw)


def score_a4(d):
    """A4: Gross Margin Trend 3yr. Expanding >=200bps=2, stable within 200bps=1, contracting >200bps=0."""
    rev = d.get('rev', {}); gp = d.get('gp', {})
    yrs = sorted([y for y in rev if rev.get(y) and gp.get(y) and rev[y] > 0])
    if len(yrs) < 2:
        return MR(1, 'N/A — single year, scoring stable', note='Insufficient history')
    # Use oldest and most recent available (up to 3yr)
    use_yrs = yrs[-min(4, len(yrs)):]  # up to 4 data points for 3yr trend
    oldest_gm = gp[use_yrs[0]] / rev[use_yrs[0]]
    newest_gm = gp[use_yrs[-1]] / rev[use_yrs[-1]]
    change_bps = (newest_gm - oldest_gm) * 10000
    raw = f'{change_bps:+.0f}bps over {int(use_yrs[-1]) - int(use_yrs[0])}yr ({fmt_pct(oldest_gm)} → {fmt_pct(newest_gm)})'
    if change_bps >= 200:
        return MR(2, raw)
    elif change_bps >= -200:
        return MR(1, raw)
    else:
        return MR(0, raw)


def score_a5(d):
    """A5: R&D Intensity. >15%=2, 8-15%=1, <8%=0. N/A if non-tech/non-biotech."""
    if not is_rd_applicable(d.get('sector', '')):
        return MR(None, f'N/A — sector {d.get("sector","")} (non-tech/non-biotech)', na=True)
    rev = d.get('rev', {}); rnd = d.get('rnd', {})
    yrs = sorted([y for y in rev if rev.get(y) and rnd.get(y) and rev[y] > 0])
    if not yrs:
        return MR(0, 'N/A — no R&D data')
    yr = yrs[-1]
    rd_pct = rnd[yr] / rev[yr]
    raw = f'{fmt_pct(rd_pct)} R&D/Rev [{yr}]'
    if rd_pct > 0.15:
        return MR(2, raw)
    elif rd_pct >= 0.08:
        return MR(1, raw)
    else:
        return MR(0, raw)


def score_a6(d):
    """A6: Operating Leverage. Gate: revenue growth >=15% most recent year. N/A if rev growth <15%."""
    rev = d.get('rev', {}); opinc = d.get('opinc', {})
    yrs = sorted([y for y in rev if rev.get(y) and rev[y] > 0])
    if len(yrs) < 2:
        return MR(None, 'N/A — insufficient revenue history', na=True)
    y0, y1 = yrs[-1], yrs[-2]
    rev_growth = rev[y0] / rev[y1] - 1
    if rev_growth < 0.15:
        return MR(None, f'N/A — rev growth {fmt_pct(rev_growth)} < 15% gate', na=True)
    # Compare op margin trend
    op_yrs = sorted([y for y in opinc if opinc.get(y) is not None and rev.get(y) and rev[y] > 0])
    if len(op_yrs) < 2:
        return MR(1, f'Rev growth {fmt_pct(rev_growth)} ≥15%; op margin trend: insufficient data (stable assumed)')
    om0 = opinc[op_yrs[-1]] / rev[op_yrs[-1]]
    om1 = opinc[op_yrs[-2]] / rev[op_yrs[-2]]
    delta_bps = (om0 - om1) * 10000
    raw = f'Rev growth {fmt_pct(rev_growth)}; Op margin {fmt_pct(om1)} → {fmt_pct(om0)} ({delta_bps:+.0f}bps)'
    if delta_bps > 0:
        return MR(2, raw, 'Op margin improving with revenue growth')
    elif delta_bps >= -100:
        return MR(1, raw, 'Op margin roughly flat')
    else:
        return MR(0, raw, 'Op margin deteriorating')


def score_a7(d):
    """A7: FCF Trajectory. FCF+ growing=2; turning positive=2; losses narrowing=1; widening+capex-intent=1; widening no intent=0."""
    fcf = d.get('fcf', {}); capex = d.get('capex', {}); rev = d.get('rev', {})
    yrs = sorted([y for y in fcf if fcf.get(y) is not None])
    if len(yrs) < 2:
        if len(yrs) == 1:
            yr = yrs[0]
            raw = f'FCF {fmt_b(fcf[yr])} [{yr}] (single year — trajectory N/A)'
            s = 2 if (fcf[yr] or 0) > 0 else 1
            return MR(s, raw, 'Single year — assumed improving')
        return MR(0, 'N/A — no FCF data')

    # Sort FCF values oldest → newest
    fcf_vals = [fcf[y] for y in yrs]
    newest_fcf = fcf_vals[-1]
    oldest_fcf = fcf_vals[0]
    all_negative = all(v is not None and v < 0 for v in fcf_vals)
    all_positive = all(v is not None and v > 0 for v in fcf_vals)
    turned_positive = oldest_fcf is not None and oldest_fcf < 0 and newest_fcf is not None and newest_fcf > 0

    raw_fcf_str = ' | '.join(f'{y}: {fmt_b(fcf[y])}' for y in yrs[-3:])

    if all_positive:
        # Growing?
        growing = newest_fcf > oldest_fcf
        return MR(2, f'FCF+ [{raw_fcf_str}]', 'Positive and ' + ('growing' if growing else 'stable'))

    if turned_positive:
        return MR(2, f'FCF turned positive [{raw_fcf_str}]', 'Was negative, now positive')

    # All or mostly negative — check if narrowing
    if all_negative:
        narrowing = abs(newest_fcf) < abs(oldest_fcf)
        if narrowing:
            return MR(1, f'Losses narrowing [{raw_fcf_str}]')
        else:
            # Widening — check capex/rev intent
            capex_intent = False
            capex_note = ''
            capex_yrs = sorted([y for y in capex if capex.get(y) and rev.get(y) and rev[y] > 0])
            if len(capex_yrs) >= 2:
                cr0 = abs(capex[capex_yrs[-1]]) / rev[capex_yrs[-1]]
                cr1 = abs(capex[capex_yrs[-2]]) / rev[capex_yrs[-2]]
                if cr0 > cr1:
                    capex_intent = True
                    capex_note = f'CapEx/Rev increasing: {fmt_pct(cr1)} → {fmt_pct(cr0)} (platform capex signal)'
            if capex_intent:
                return MR(1, f'Losses widening [{raw_fcf_str}]; {capex_note}', 'Widening with capex-intent evidence')
            else:
                return MR(0, f'Losses widening [{raw_fcf_str}]; no capex-intent evidence')

    # Mixed — use most recent trend
    recent = fcf_vals[-2:]
    if recent[-1] is not None and recent[0] is not None and recent[-1] > recent[0]:
        return MR(1, f'FCF improving trend [{raw_fcf_str}]')
    return MR(0, f'FCF mixed/deteriorating [{raw_fcf_str}]')


def score_a8(d):
    """A8: Cash Runway. FCF+: score 2. Negative: runway=(cash+STI)/|annual FCF burn|. >24mo=2, 12-24mo=1, <12mo=0."""
    fcf = d.get('fcf', {}); cash = d.get('cash', {})
    # Most recent FCF and cash
    fcf_yrs = sorted([y for y in fcf if fcf.get(y) is not None])
    cash_yrs = sorted([y for y in cash if cash.get(y) is not None])

    if not fcf_yrs:
        return MR(0, 'N/A — no FCF data')
    latest_fcf = fcf[fcf_yrs[-1]]
    latest_cash = cash[cash_yrs[-1]] if cash_yrs else None

    if latest_fcf is not None and latest_fcf >= 0:
        return MR(2, f'FCF positive ({fmt_b(latest_fcf)}) — no burn', 'No runway concern')

    if latest_cash is None:
        return MR(0, f'FCF negative ({fmt_b(latest_fcf)}); cash data unavailable')

    # Estimate annual burn rate (use average of negative FCF years)
    neg_fcf = [fcf[y] for y in fcf_yrs if fcf[y] is not None and fcf[y] < 0]
    if not neg_fcf:
        return MR(2, f'No negative FCF in history', 'No burn')
    avg_burn = abs(sum(neg_fcf) / len(neg_fcf))
    if avg_burn == 0:
        return MR(2, 'No meaningful burn rate')

    runway_months = (latest_cash / avg_burn) * 12
    raw = f'Cash+STI {fmt_b(latest_cash)}, avg burn {fmt_b(avg_burn)}/yr → {runway_months:.0f}mo runway'
    if runway_months > 24:
        return MR(2, raw)
    elif runway_months >= 12:
        return MR(1, raw)
    else:
        return MR(0, raw)


def score_a9(d, a9_strategic_tickers=None):
    """A9: Share Count Discipline. Flat/declining=2; <5%/yr=1 (SBC); 5-10%/yr strategic=1; >10% or unclear=0."""
    a9_strategic_tickers = a9_strategic_tickers or []
    shares = d.get('shares', {}); sym = d.get('sym', '')
    yrs = sorted([y for y in shares if shares.get(y) and shares[y] > 0])
    if len(yrs) < 2:
        return MR(1, 'N/A — single year shares data (stable assumed)', note='Insufficient history')
    oldest_s = shares[yrs[0]]
    newest_s = shares[yrs[-1]]
    n_yrs = int(yrs[-1]) - int(yrs[0])
    if n_yrs == 0:
        return MR(1, 'Single period', '')
    ann_change = (newest_s / oldest_s) ** (1.0 / n_yrs) - 1
    raw = f'{fmt_pct(ann_change)}/yr annualised ({yrs[0]}→{yrs[-1]}); {fmt_b(oldest_s)} → {fmt_b(newest_s)} shares'
    if ann_change <= 0.001:  # flat or declining
        return MR(2, raw, 'Flat or declining share count')
    elif ann_change < 0.05:
        return MR(1, raw, 'Increasing <5%/yr — SBC-assumed; verify driver via 10-K')
    elif ann_change < 0.10:
        if sym.upper().replace('.L', '') in [t.upper().replace('.L', '') for t in a9_strategic_tickers]:
            return MR(1, raw, '5-10%/yr — strategic capital raise flagged by user; verify >50% attributable')
        else:
            return MR(0, raw, '5-10%/yr — driver unverified; use --a9-strategic to override if strategic raise')
    else:
        if sym.upper().replace('.L', '') in [t.upper().replace('.L', '') for t in a9_strategic_tickers]:
            return MR(1, raw, '>10%/yr — strategic raise override applied; verify >50% attributable in 10-K')
        return MR(0, raw, '>10%/yr share dilution OR driver unclear')


def score_a10_manual(override_score):
    """A10: Revenue Quality — MANUAL (requires web search). Returns MR with MANUAL note if no override."""
    if override_score is not None:
        labels = {0: 'primarily transactional', 1: 'growing recurring <50%', 2: '>50% recurring/contracted or RPO signal'}
        return MR(override_score, f'MANUAL INPUT: {labels.get(override_score, "?")}',
                  note='Sourced via web search "[ticker] recurring revenue backlog RPO contracted revenue"')
    return MR(None, 'MANUAL — run web search: "[TICKER] recurring revenue backlog RPO contracted revenue"',
              note='Score: >50% recurring/contracted or RPO growing >20% faster than rev=2; growing <50%=1; transactional=0',
              na=True)


def score_a11_manual(override_score):
    """A11: Analyst Estimate Revisions 3mo — MANUAL (requires Finnhub). Returns MR with MANUAL note if no override."""
    if override_score is not None:
        labels = {0: 'downward revisions', 1: 'flat/no change', 2: 'upward revisions'}
        return MR(override_score, f'MANUAL INPUT: {labels.get(override_score, "?")}',
                  note='Sourced via Finnhub stock/eps-estimate 3mo comparison')
    return MR(None, 'MANUAL — Finnhub stock/eps-estimate: compare current vs 3mo prior',
              note='>=4 analysts: up=2, flat=1, down=0. <=3: up=2, flat/no change=1, down=0 (note thin coverage)',
              na=True)


def score_a12(d):
    """A12: Analyst Consensus Upside. >=4 analysts: >50%=2, 25-50%=1, <25%=0. <=3: N/A."""
    price = d.get('price'); target = d.get('target'); n_analysts = d.get('n_analysts')
    if n_analysts is None or n_analysts <= 3:
        n_str = str(int(n_analysts)) if n_analysts is not None else '0'
        return MR(None, f'N/A — thin/no analyst coverage ({n_str} analysts); excluded from denominator', na=True)
    if price is None or target is None or price == 0:
        return MR(None, 'N/A — price or target unavailable', na=True)
    upside = target / price - 1
    raw = f'{fmt_pct(upside)} upside (price {price:.2f}, target {target:.2f}, {int(n_analysts)} analysts)'
    if upside > 0.50:
        return MR(2, raw)
    elif upside >= 0.25:
        return MR(1, raw)
    else:
        return MR(0, raw, note='<25% upside or above analyst target')


def score_a13(d):
    """A13: GP Growth vs Revenue Growth (3yr CAGR). GP CAGR >1.5x rev CAGR=2; 1.0-1.5x=1; <1.0x=0."""
    rev = d.get('rev', {}); gp = d.get('gp', {})
    yrs = sorted([y for y in rev if rev.get(y) and gp.get(y) and rev[y] > 0])
    if len(yrs) < 2:
        return MR(1, 'N/A — single year (stable assumed)')
    # Check if GP is negative throughout — N/A in that case
    gp_all_neg = all(gp.get(y, 0) is not None and gp.get(y, 0) <= 0 for y in yrs)
    if gp_all_neg:
        return MR(None, 'N/A — gross profit negative throughout; excluded from denominator', na=True)

    rev_vals = [rev[y] for y in yrs]
    gp_vals = [gp[y] for y in yrs if gp.get(y) and gp[y] > 0]
    yrs_gp = [y for y in yrs if gp.get(y) and gp[y] > 0]

    rev_cagr, n_rev = cagr_from_sorted(rev_vals)
    gp_cagr, n_gp = cagr_from_sorted(gp_vals)

    if rev_cagr is None or gp_cagr is None:
        return MR(1, 'N/A — cannot compute CAGR (stable assumed)')

    ratio = gp_cagr / rev_cagr if rev_cagr != 0 else None
    raw = f'GP CAGR {fmt_pct(gp_cagr)} vs Rev CAGR {fmt_pct(rev_cagr)} = {ratio:.2f}x ratio' if ratio is not None else 'N/A'
    if ratio is None:
        return MR(1, raw)
    if ratio > 1.5:
        return MR(2, raw, 'GP growing >1.5x revenue — operating leverage building')
    elif ratio >= 1.0:
        return MR(1, raw, 'GP growing in line with or faster than revenue')
    else:
        return MR(0, raw, 'GP growing slower than revenue')


# ---------------------------------------------------------------------------
# Pre-inflection override check
# ---------------------------------------------------------------------------
def check_pre_inflection_override(scores, d):
    """Check if pre-inflection override conditions A/B/C are ALL met."""
    a5_score = scores.get('A5')
    mktcap = d.get('mktcap')
    fcf = d.get('fcf', {})

    a_met = (a5_score is not None and not getattr(a5_score, 'na', True) and a5_score.score == 2)
    b_met = (mktcap is not None and mktcap < 5e9)
    # Condition C: no positive FCF in any of last 3 years
    fcf_yrs = sorted(fcf.keys())[-3:]
    fcf_vals = [fcf.get(y) for y in fcf_yrs if fcf.get(y) is not None]
    c_met = len(fcf_vals) >= 1 and all(v <= 0 for v in fcf_vals)

    return a_met, b_met, c_met


# ---------------------------------------------------------------------------
# Main scoring orchestrator
# ---------------------------------------------------------------------------
def score_candidate(sym, manual_a10=None, manual_a11=None, a9_strategic_tickers=None):
    """Pull data and score all 13 Part A metrics. Returns (d, scores_dict, summary)."""
    print(f'\n  Fetching data for {sym}...', end='', flush=True)
    d = pull_data(sym)
    print(' done.')

    if d.get('error'):
        print(f'  [DATA ERROR] {d["error"]}')

    scores = {
        'A1':  score_a1(d),
        'A2':  score_a2(d),
        'A3':  score_a3(d),
        'A4':  score_a4(d),
        'A5':  score_a5(d),
        'A6':  score_a6(d),
        'A7':  score_a7(d),
        'A8':  score_a8(d),
        'A9':  score_a9(d, a9_strategic_tickers),
        'A10': score_a10_manual(manual_a10),
        'A11': score_a11_manual(manual_a11),
        'A12': score_a12(d),
        'A13': score_a13(d),
    }

    return d, scores


def compute_totals(scores):
    """Compute raw score, effective denominator, threshold checks, ACS4."""
    raw_score = sum(r.score for r in scores.values() if not r.na and r.score is not None)
    na_count = sum(1 for r in scores.values() if r.na)
    effective_denom = 26 - (2 * na_count)
    manual_pending = sum(1 for k, r in scores.items() if r.na and 'MANUAL' in r.raw)

    # Standard threshold: raw_score >= 11
    standard_pass = raw_score >= 11
    # Pre-inflection override: raw_score >= 7 (checked externally with A/B/C conditions)
    override_eligible = raw_score >= 7

    # ACS4: (raw_score / 26) * 15, rounded to nearest 0.5
    acs4 = round((raw_score / 26) * 15 * 2) / 2  # round to 0.5

    return {
        'raw_score': raw_score,
        'effective_denom': effective_denom,
        'na_count': na_count,
        'manual_pending': manual_pending,
        'standard_pass': standard_pass,
        'override_eligible': override_eligible,
        'acs4': acs4,
    }


# ---------------------------------------------------------------------------
# Output formatter
# ---------------------------------------------------------------------------
METRIC_LABELS = {
    'A1': 'Rev CAGR 3yr',
    'A2': 'Rev Acceleration',
    'A3': 'Gross Margin',
    'A4': 'GM Trend 3yr',
    'A5': 'R&D Intensity',
    'A6': 'Op Leverage',
    'A7': 'FCF Trajectory',
    'A8': 'Cash Runway',
    'A9': 'Share Count',
    'A10': 'Rev Quality',
    'A11': 'Est Revisions',
    'A12': 'Analyst Upside',
    'A13': 'GP vs Rev Growth',
}


def print_scorecard(sym, d, scores, totals, override_check=None):
    """Print formatted Part A scorecard for one candidate."""
    name = d.get('name', sym)
    sector = d.get('sector', 'Unknown')
    industry = d.get('industry', 'Unknown')
    mktcap = d.get('mktcap')
    mktcap_str = f'${mktcap/1e9:.1f}B' if mktcap else 'N/A'
    price = d.get('price')
    price_sym = '£' if d.get('uk') else '$'
    price_str = f'{price_sym}{price:.3f}' if price else 'N/A'

    W = 72
    print(f'\n{"=" * W}')
    print(f'  {sym} — {name}')
    print(f'  Sector: {sector} / {industry}')
    print(f'  Price: {price_str}  |  Mkt Cap: {mktcap_str}')
    print(f'{"=" * W}')
    print(f'  {"METRIC":<20} {"SCORE":>5}  {"RAW VALUE / DETAIL"}')
    print(f'  {"-" * 20} {"-" * 5}  {"-" * 42}')

    for key in ['A1','A2','A3','A4','A5','A6','A7','A8','A9','A10','A11','A12','A13']:
        r = scores[key]
        label = METRIC_LABELS[key]
        score_str = r.display_score()
        raw_disp = r.raw[:65] if len(r.raw) > 65 else r.raw
        print(f'  {key} {label:<17} {score_str:>5}  {raw_disp}')
        if r.note:
            note_disp = r.note[:68] if len(r.note) > 68 else r.note
            print(f'  {"":>24}       NOTE: {note_disp}')

    raw = totals['raw_score']
    denom = totals['effective_denom']
    na_n = totals['na_count']
    acs4 = totals['acs4']
    manual = totals['manual_pending']

    print(f'  {"─" * 70}')
    print(f'  PART A TOTAL:   {raw}/{denom}  (26 − {na_n * 2} for {na_n} N/A field{"s" if na_n != 1 else ""})')
    if manual > 0:
        print(f'  *** {manual} MANUAL METRIC{"S" if manual > 1 else ""} PENDING (A10/A11) — totals exclude these ***')
    print()
    print(f'  ACS4 FORMULA:   ({raw}/26) × 15 = {acs4:.1f}  (rounded to nearest 0.5)')
    print()

    # Threshold check
    if totals['standard_pass']:
        print(f'  ✓ STANDARD THRESHOLD PASSED  ({raw}/{denom} ≥ 11) — proceed to Part B and ACS')
    else:
        print(f'  ✗ STANDARD THRESHOLD NOT MET  ({raw}/{denom} < 11)')
        if totals['override_eligible'] and override_check:
            a_met, b_met, c_met = override_check
            all_met = a_met and b_met and c_met
            print(f'  PRE-INFLECTION OVERRIDE check ({raw} ≥ 7):')
            print(f'    (A) A5 = 2 (R&D >15%):         {"✓ MET" if a_met else "✗ NOT MET"}')
            print(f'    (B) Market cap < $5B:           {"✓ MET" if b_met else f"✗ NOT MET ({mktcap_str})"}')
            print(f'    (C) No positive FCF last 3yr:   {"✓ MET" if c_met else "✗ NOT MET"}')
            if all_met:
                print(f'    → PRE-INFLECTION OVERRIDE APPLIES — conditions A/B/C confirmed')
            else:
                print(f'    → Override NOT applicable — not all conditions met')
        elif raw < 7:
            print(f'  ✗ DISCARD — score {raw} < 7 (below pre-inflection override minimum)')

    print(f'{"=" * W}\n')


def build_json_output(sym, d, scores, totals, override_check=None):
    """Build JSON-serialisable dict for --json-out."""
    a_met, b_met, c_met = override_check if override_check else (False, False, False)
    return {
        'ticker': sym,
        'company': d.get('name', sym),
        'sector': d.get('sector', ''),
        'industry': d.get('industry', ''),
        'mktcap_usd': d.get('mktcap'),
        'price': d.get('price'),
        'currency': 'GBP' if d.get('uk') else 'USD',
        'part_a': {k: {'score': r.score, 'na': r.na, 'raw': r.raw, 'note': r.note}
                   for k, r in scores.items()},
        'raw_score': totals['raw_score'],
        'effective_denom': totals['effective_denom'],
        'na_count': totals['na_count'],
        'manual_pending': totals['manual_pending'],
        'standard_threshold_passed': totals['standard_pass'],
        'pre_inflection_override': {
            'eligible': totals['override_eligible'],
            'a_met': a_met, 'b_met': b_met, 'c_met': c_met,
            'applies': totals['override_eligible'] and a_met and b_met and c_met,
        },
        'acs4': totals['acs4'],
        'part_a_str': f'{totals["raw_score"]}/{totals["effective_denom"]}',
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_kv_arg(arg_str):
    """Parse 'TICKER1:2,TICKER2:1' → {'TICKER1': 2, 'TICKER2': 1}."""
    if not arg_str:
        return {}
    result = {}
    for part in arg_str.split(','):
        part = part.strip()
        if ':' in part:
            k, v = part.split(':', 1)
            try:
                result[k.strip().upper()] = int(v.strip())
            except ValueError:
                pass
    return result


def main():
    parser = argparse.ArgumentParser(
        description='VCI Part A Auto-Scorer — applies VCI_Asymmetric_Scorecard.md v2.0 rules'
    )
    parser.add_argument('tickers', nargs='+', help='Ticker symbols e.g. RXRX ONT.L ABCL')
    parser.add_argument('--a10', default=None,
                        help='Manual A10 scores: "RXRX:1,ONT.L:2" (0/1/2 per ticker)')
    parser.add_argument('--a11', default=None,
                        help='Manual A11 scores: "RXRX:2,ONT.L:1" (0/1/2 per ticker)')
    parser.add_argument('--a9-strategic', nargs='*', default=None, metavar='TICKER',
                        help='Tickers where A9 5-10%%/yr dilution is a strategic capital raise')
    parser.add_argument('--json-out', default=None, metavar='PATH',
                        help='Write scored JSON to file (for build_vci_email.py candidates section)')
    args = parser.parse_args()

    a10_map = parse_kv_arg(args.a10) if args.a10 else {}
    a11_map = parse_kv_arg(args.a11) if args.a11 else {}
    a9_strategic = [t.upper() for t in args.a9_strategic] if args.a9_strategic else []

    print(f'\nVCI Part A Auto-Scorer | {len(args.tickers)} candidate(s)')
    print(f'Scorecard version: VCI_Asymmetric_Scorecard.md v2.0')
    print(f'Manual fields: A10 (revenue quality), A11 (analyst revisions)')
    if a10_map:
        print(f'A10 overrides provided: {a10_map}')
    if a11_map:
        print(f'A11 overrides provided: {a11_map}')
    if a9_strategic:
        print(f'A9 strategic tickers: {a9_strategic}')

    all_results = []

    for sym in args.tickers:
        sym_up = sym.upper()
        a10_override = a10_map.get(sym_up) or a10_map.get(sym_up.replace('.L', ''))
        a11_override = a11_map.get(sym_up) or a11_map.get(sym_up.replace('.L', ''))

        d, scores = score_candidate(sym, a10_override, a11_override, a9_strategic)
        totals = compute_totals(scores)
        override_check = check_pre_inflection_override(scores, d)
        print_scorecard(sym, d, scores, totals, override_check)

        if args.json_out:
            all_results.append(build_json_output(sym, d, scores, totals, override_check))

    # Summary table
    if len(args.tickers) > 1:
        print(f'\n{"─" * 72}')
        print(f'  SUMMARY — {len(args.tickers)} CANDIDATES')
        print(f'  {"Ticker":<10} {"Score":>8}  {"Threshold":>14}  ACS4  Manual pending')
        print(f'  {"─" * 9} {"─" * 8}  {"─" * 14}  {"─" * 4}  {"─" * 14}')

    if args.json_out:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True) if os.path.dirname(args.json_out) else None
        with open(args.json_out, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f'\nJSON output written: {args.json_out}')
        print(f'Use with build_vci_email.py --json to populate E2 candidates section.')


if __name__ == '__main__':
    main()
