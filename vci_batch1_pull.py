#!/usr/bin/env python3
"""
vci_batch1_pull.py — VCI Batch 1 quantitative data pull
Pulls all Part A metrics + 3yr price range for ACS8 from yfinance.
Always uses local bash Python — never yfinance MCP server.

Usage:
    python vci_batch1_pull.py RXRX ONT.L ABCL ALAB CRDO
    python vci_batch1_pull.py ONT.L  (single ticker)

GBp correction applied automatically for UK-listed stocks.
"""

import sys
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import math
import yfinance as yf

UK_SUFFIXES = ('.L', '.l')


def is_uk(sym):
    return any(sym.endswith(s) for s in UK_SUFFIXES)


def gbp_fix(val, sym):
    """Convert GBp to GBP if UK stock and raw value >500."""
    if is_uk(sym) and isinstance(val, (int, float)) and not math.isnan(val) and val > 500:
        return val / 100.0
    return val


def safe(val):
    if val is None:
        return None
    try:
        if math.isnan(float(val)):
            return None
    except (TypeError, ValueError):
        pass
    return val


def fmt_num(val, prefix=''):
    """Format large number as millions/billions."""
    if val is None:
        return 'N/A'
    v = float(val)
    if abs(v) >= 1e9:
        return f'{prefix}{v/1e9:.2f}B'
    if abs(v) >= 1e6:
        return f'{prefix}{v/1e6:.1f}M'
    if abs(v) >= 1e3:
        return f'{prefix}{v/1e3:.1f}k'
    return f'{prefix}{v:.2f}'


def fmt_row(d, label, prefix=''):
    """Format a dict of {year: value} into a row string."""
    if not d:
        return f'  {label:<18} N/A'
    parts = []
    for yr, val in sorted(d.items(), reverse=True)[:4]:
        if val is None:
            parts.append(f'{yr}: N/A')
        else:
            parts.append(f'{yr}: {fmt_num(val, prefix)}')
    return f'  {label:<18} {" | ".join(parts)}'


def pull(sym):
    t = yf.Ticker(sym)
    uk = is_uk(sym)
    ccy = 'GBP' if uk else 'USD'
    p = '£' if uk else '$'

    print(f'\n{"="*65}')
    print(f'  TICKER: {sym}  |  Currency assumed: {ccy}')
    print(f'{"="*65}')

    # --- info ---
    try:
        info = t.info
        price_raw = safe(info.get('currentPrice') or info.get('regularMarketPrice'))
        price = gbp_fix(price_raw, sym)
        target_raw = safe(info.get('targetMeanPrice'))
        target = gbp_fix(target_raw, sym)
        hi52_raw = safe(info.get('fiftyTwoWeekHigh'))
        lo52_raw = safe(info.get('fiftyTwoWeekLow'))
        hi52 = gbp_fix(hi52_raw, sym)
        lo52 = gbp_fix(lo52_raw, sym)
        mktcap = safe(info.get('marketCap'))
        n_analysts = safe(info.get('numberOfAnalystOpinions'))
        sector = safe(info.get('sector')) or 'N/A'
        industry = safe(info.get('industry')) or 'N/A'
        rev_growth_ttm = safe(info.get('revenueGrowth'))  # TTM YoY fraction
        gm_ttm = safe(info.get('grossMargins'))

        pos52 = None
        if hi52 and lo52 and price and hi52 != lo52:
            pos52 = (price - lo52) / (hi52 - lo52) * 100

        print(f'  Sector:    {sector} / {industry}')
        print(f'  Price:     {fmt_num(price, p)} | Target: {fmt_num(target, p)} | Analysts: {n_analysts}')
        hi52s = f'{p}{hi52:.2f}' if hi52 else 'N/A'
        lo52s = f'{p}{lo52:.2f}' if lo52 else 'N/A'
        pos52s = f'{pos52:.1f}%' if pos52 is not None else 'N/A'
        print(f'  52wk:      {lo52s} – {hi52s}  |  52wk position: {pos52s}')
        mcs = fmt_num(mktcap, p) if mktcap else 'N/A'
        print(f'  Mkt Cap:   {mcs}')
        if rev_growth_ttm is not None:
            print(f'  TTM Rev Growth (yfinance): {rev_growth_ttm*100:.1f}%  [NOTE: may reflect lumpiness — verify vs annual trend]')
        if gm_ttm is not None:
            print(f'  TTM Gross Margin (yfinance): {gm_ttm*100:.1f}%')
    except Exception as e:
        print(f'  [INFO ERROR] {e}')

    # --- 3yr price range for ACS8 ---
    print()
    try:
        hist = t.history(period='3y')
        note = '3yr'
        if len(hist) < 50:
            hist = t.history(period='max')
            note = f'max available ({len(hist)} days)'
        hi3_raw = hist['High'].max()
        lo3_raw = hist['Low'].min()
        hi3 = gbp_fix(hi3_raw, sym)
        lo3 = gbp_fix(lo3_raw, sym)
        pos3 = None
        if hi3 != lo3 and price:
            pos3 = (price - lo3) / (hi3 - lo3) * 100
        hi3s = f'{p}{hi3:.3f}' if hi3 else 'N/A'
        lo3s = f'{p}{lo3:.3f}' if lo3 else 'N/A'
        pos3s = f'{pos3:.1f}%' if pos3 is not None else 'N/A'
        print(f'  3yr Range ({note}): {lo3s} – {hi3s}  |  3yr Position: {pos3s}')
        if pos3 is not None:
            band = ('bottom 20% -> ACS8 base=10' if pos3 <= 20 else
                    '21-40% -> ACS8 base=8' if pos3 <= 40 else
                    '41-60% -> ACS8 base=6' if pos3 <= 60 else
                    '61-80% -> ACS8 base=4' if pos3 <= 80 else
                    'top 20% -> ACS8 base=2')
            print(f'  ACS8 band: {band}')
    except Exception as e:
        print(f'  [3YR RANGE ERROR] {e}')

    # --- income statement ---
    print()
    try:
        inc = t.income_stmt
        rev, gp, rnd, opinc, ni, shares = {}, {}, {}, {}, {}, {}
        for col in inc.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            rev[yr] = safe(inc.loc['Total Revenue', col] if 'Total Revenue' in inc.index else None)
            gp[yr] = safe(inc.loc['Gross Profit', col] if 'Gross Profit' in inc.index else None)
            for rk in ['Research And Development', 'ResearchAndDevelopment', 'Research Development']:
                if rk in inc.index:
                    rnd[yr] = safe(inc.loc[rk, col])
                    break
            else:
                rnd[yr] = None
            opinc[yr] = safe(inc.loc['Operating Income', col] if 'Operating Income' in inc.index else None)
            ni[yr] = safe(inc.loc['Net Income', col] if 'Net Income' in inc.index else None)
            for sk in ['Diluted Average Shares', 'Diluted Shares Issued', 'DilutedAverageShares']:
                if sk in inc.index:
                    shares[yr] = safe(inc.loc[sk, col])
                    break
            else:
                shares[yr] = None

        print(fmt_row(rev, 'Revenue:', p))
        print(fmt_row(gp, 'Gross Profit:', p))
        # gross margin %
        gm_rows = {}
        for yr in rev:
            if rev[yr] and gp[yr] and rev[yr] != 0:
                gm_rows[yr] = gp[yr] / rev[yr] * 100
        if gm_rows:
            parts = [f'{yr}: {v:.1f}%' for yr, v in sorted(gm_rows.items(), reverse=True)[:4]]
            print(f'  {"Gross Margin %":<18} {" | ".join(parts)}')
        print(fmt_row(rnd, 'R&D:', p))
        # R&D as % of revenue
        rd_pct = {}
        for yr in rnd:
            if rnd.get(yr) and rev.get(yr) and rev[yr] != 0:
                rd_pct[yr] = rnd[yr] / rev[yr] * 100
        if rd_pct:
            parts = [f'{yr}: {v:.1f}%' for yr, v in sorted(rd_pct.items(), reverse=True)[:4]]
            print(f'  {"R&D % Revenue":<18} {" | ".join(parts)}')
        print(fmt_row(opinc, 'Op Income:', p))
        print(fmt_row(ni, 'Net Income:', p))
        print(fmt_row(shares, 'Dil Shares:', ''))
    except Exception as e:
        print(f'  [INCOME STMT ERROR] {e}')

    # --- cash flow ---
    print()
    try:
        cf = t.cashflow
        fcf, ocf, capex = {}, {}, {}
        for col in cf.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            fcf[yr] = safe(cf.loc['Free Cash Flow', col] if 'Free Cash Flow' in cf.index else None)
            ocf[yr] = safe(cf.loc['Operating Cash Flow', col] if 'Operating Cash Flow' in cf.index else None)
            capex[yr] = safe(cf.loc['Capital Expenditure', col] if 'Capital Expenditure' in cf.index else None)
        print(fmt_row(fcf, 'Free Cash Flow:', p))
        print(fmt_row(ocf, 'Op Cash Flow:', p))
        print(fmt_row(capex, 'CapEx:', p))
    except Exception as e:
        print(f'  [CASHFLOW ERROR] {e}')

    # --- balance sheet ---
    print()
    try:
        bs = t.balance_sheet
        cash, debt, assets, cur_liab = {}, {}, {}, {}
        for col in bs.columns[:4]:
            yr = str(col.year) if hasattr(col, 'year') else str(col)[:4]
            c = safe(bs.loc['Cash And Cash Equivalents', col] if 'Cash And Cash Equivalents' in bs.index else None)
            sti = safe(bs.loc['Other Short Term Investments', col] if 'Other Short Term Investments' in bs.index else None)
            if c is not None and sti is not None:
                cash[yr] = c + sti
            elif c is not None:
                cash[yr] = c
            else:
                cash[yr] = None
            debt[yr] = safe(bs.loc['Total Debt', col] if 'Total Debt' in bs.index else None)
            assets[yr] = safe(bs.loc['Total Assets', col] if 'Total Assets' in bs.index else None)
            cur_liab[yr] = safe(bs.loc['Current Liabilities', col] if 'Current Liabilities' in bs.index else None)
        print(fmt_row(cash, 'Cash+STI:', p))
        print(fmt_row(debt, 'Total Debt:', p))
        print(fmt_row(assets, 'Total Assets:', p))
        # ROIC proxy for B11
        roic = {}
        for yr in assets:
            if opinc.get(yr) and assets.get(yr) and cur_liab.get(yr):
                ic = assets[yr] - cur_liab[yr]
                if ic and ic != 0:
                    roic[yr] = opinc[yr] / ic * 100
        if roic:
            parts = [f'{yr}: {v:.1f}%' for yr, v in sorted(roic.items(), reverse=True)[:4]]
            print(f'  {"ROIC proxy":<18} {" | ".join(parts)}  [OpInc/(Assets-CurLiab)]')
    except Exception as e:
        print(f'  [BALANCE SHEET ERROR] {e}')

    print()


if __name__ == '__main__':
    tickers = sys.argv[1:]
    if not tickers:
        print('Usage: python vci_batch1_pull.py TICKER1 TICKER2 ...')
        print('Example: python vci_batch1_pull.py RXRX ONT.L ABCL')
        sys.exit(1)
    for sym in tickers:
        try:
            pull(sym)
        except Exception as e:
            print(f'\n[FATAL ERROR for {sym}]: {e}')
