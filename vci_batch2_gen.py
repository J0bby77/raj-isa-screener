#!/usr/bin/env python3
"""
vci_batch2_gen.py — VCI Batch 2 Part B Search Query Generator
Version: 1.0 | June 2026

Generates all B1-B12 Part B search queries for every confirmed shortlist candidate
in ONE pre-substituted block. Issue ALL queries simultaneously as a single parallel batch.

B11 is excluded — it is computed from yfinance data in vci_acs_scorer.py (ROIC proxy).
The remaining 11 queries (B1-B10, B12) are generated per candidate.

Usage:
    python vci_batch2_gen.py --candidates candidates.json
    python vci_batch2_gen.py --tickers "ONT.L:Oxford Nanopore:Van Parys" "RXRX:Recursion:Chris Gibson"

candidates.json format:
    [
      {"ticker": "ONT.L", "company": "Oxford Nanopore Technologies", "ceo": "Desmond Cheung"},
      {"ticker": "RXRX",  "company": "Recursion Pharmaceuticals",     "ceo": "Chris Gibson"}
    ]

Shorthand CLI format (--tickers):
    "TICKER:Company Name:CEO Name"  (space-separate multiple)

Output:
    Block of all queries for all candidates, ready to issue as a single parallel batch.
    Also saves to batch2_queries_YYYYMMDD.txt if --out specified.
"""

import argparse
import json
import os
import sys
from datetime import datetime

YEAR = datetime.now().year

# B11 is computed from yfinance (ROIC proxy in vci_acs_scorer.py) — excluded from web search
BATCH2_TEMPLATES = [
    ('B1',  '{T} developer ecosystem API SDK partner integration count {Y}'),
    ('B2',  '{T} structural bottleneck value chain non-substitutable removal {Y}'),
    ('B3',  '{T} customer co-development strategic partnership co-investment {Y}'),
    ('B4',  '{T} total addressable market TAM penetration analyst estimate {Y}'),
    ('B5',  '{T} S-curve adoption inflection product cycle early majority {Y}'),
    ('B6',  '{CEO} {C} platform vision long-term strategy R&D conviction {Y}'),
    ('B7',  '{T} catalyst timeline named event binary outcome {Y}'),
    ('B8',  '{T} competitive moat replication threat well-funded entrant {Y}'),
    ('B9',  '{T} regulatory risk government intervention export control FDA {Y}'),
    ('B10', '{T} institutional investor 13F stake Tier-1 growth fund insider {Y}'),
    # B11 skipped — computed from vci_acs_scorer.py (operatingIncome / (totalAssets - currentLiabilities), 3yr trend)
    ('B12', '{T} pricing power NRR net revenue retention renewal rate {Y}'),
]

B11_NOTE = (
    'B11 COMPUTED — do not search. Formula: operatingIncome / (totalAssets - currentLiabilities) '
    'over 3 annual periods. Already in vci_acs_scorer.py output under "ROIC proxy".'
)


def generate_queries(candidates):
    """Return formatted query block for all candidates."""
    lines = []
    lines.append(f'VCI BATCH 2 — Part B Search Queries')
    lines.append(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'Candidates: {len(candidates)}  |  Queries per candidate: 11 (B1-B10, B12)  |  B11 computed — see note')
    lines.append(f'Year substituted: {YEAR}')
    lines.append('')
    lines.append('INSTRUCTION: Issue ALL queries below as a SINGLE parallel batch — do NOT search sequentially.')
    lines.append('Review all results together before scoring any candidate.')
    lines.append('')
    lines.append('─' * 72)
    lines.append(f'  {B11_NOTE}')
    lines.append('─' * 72)
    lines.append('')

    for cand in candidates:
        ticker = cand.get('ticker', '').strip()
        company = cand.get('company', '').strip()
        ceo = cand.get('ceo', f'{ticker} CEO').strip()

        lines.append(f'  ┌─ {ticker} — {company}')
        for dim, template in BATCH2_TEMPLATES:
            query = (template
                     .replace('{T}', ticker)
                     .replace('{CEO}', ceo)
                     .replace('{C}', company)
                     .replace('{Y}', str(YEAR)))
            lines.append(f'  │  {dim}: "{query}"')
        lines.append(f'  └─ B11: COMPUTED — no search needed')
        lines.append('')

    lines.append('─' * 72)
    lines.append(f'TOTAL QUERIES TO ISSUE: {len(candidates) * len(BATCH2_TEMPLATES)}')
    lines.append('Issue as one parallel batch. Review all results. Then score all candidates.')
    lines.append('─' * 72)

    return '\n'.join(lines)


def parse_ticker_shorthand(args_list):
    """Parse 'TICKER:Company Name:CEO' shorthand into candidate dicts."""
    candidates = []
    for arg in args_list:
        parts = arg.split(':', 2)
        ticker = parts[0].strip() if len(parts) > 0 else ''
        company = parts[1].strip() if len(parts) > 1 else ticker
        ceo = parts[2].strip() if len(parts) > 2 else f'{ticker} CEO'
        if ticker:
            candidates.append({'ticker': ticker, 'company': company, 'ceo': ceo})
    return candidates


def main():
    parser = argparse.ArgumentParser(
        description='Generate all VCI Batch 2 Part B search queries in one block.'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--candidates', metavar='PATH',
                       help='JSON file: [{"ticker": "X", "company": "Y", "ceo": "Z"}, ...]')
    group.add_argument('--tickers', nargs='+', metavar='TICKER:Company:CEO',
                       help='Shorthand: "ONT.L:Oxford Nanopore:Van Parys" "RXRX:Recursion:Gibson"')
    parser.add_argument('--out', default=None, metavar='PATH',
                        help='Save output to file (optional)')
    args = parser.parse_args()

    if args.candidates:
        if not os.path.exists(args.candidates):
            print(f'ERROR: Candidates file not found: {args.candidates}')
            sys.exit(1)
        with open(args.candidates, encoding='utf-8') as f:
            candidates = json.load(f)
    else:
        candidates = parse_ticker_shorthand(args.tickers)

    if not candidates:
        print('ERROR: No candidates provided.')
        sys.exit(1)

    output = generate_queries(candidates)
    print(output)

    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'\nQueries saved: {args.out}')


if __name__ == '__main__':
    main()
