#!/usr/bin/env python3
"""
update_vci_watchlist.py — VCI Watchlist Surgical Updater
Version: 1.0 | June 2026

Reads a JSON update file and rewrites the project_isa_vci_watchlist.md memory file.
Regenerates the full file from structured state — avoids format drift from manual editing.
Preserves structurally-excluded and AMBA monitor sections unless explicitly overridden.

Usage:
    python update_vci_watchlist.py --watchlist PATH_TO_WATCHLIST.md --update PATH_TO_UPDATE.json

JSON update file schema — see update_vci_watchlist_TEMPLATE.json for a complete example.

Required fields in JSON:
    run_month          str  e.g. "June 2026"
    run_note           str  e.g. "v2.0 scorecard"
    watchlist          list of watchlist entry objects (max 5)
    change_log_entry   object with month/additions/removals/acs_changes/notes

Optional fields (preserve current file content if omitted):
    nvidia_class_note  str  — paragraph under NVIDIA-Class section
    rescore_calendar   list of {ticker, next_rescore, catalyst}
    excluded           list of {ticker, reason}  — replaces existing if provided
    amba_monitor       list of {ticker, company, acs_at_removal, nvidia_signals, current_price, re_entry_trigger, notes}

Watchlist entry fields:
    rank               int
    ticker             str
    company            str
    exchange           str  (LSE / NASDAQ / NYSE)
    acs                int
    3yr_pos_pct        str  e.g. "22.9%"
    nvidia_signals     str  e.g. "5/6 NVIDIA-CLASS; S5-MULT"
    classification     str  e.g. "HIGH CONVICTION"
    entry_level        str  e.g. "£1.20-1.35 ACTIVE BUY"
    last_scored        str  e.g. "2026-06-08"
    entry_trigger      str  e.g. "ACTIVE BUY NOW — NHS products shipping"
    status             str  e.g. "ACTIVE BUY" or "WATCHLIST" or "FLOOR-CAPPED — 3mo re-score"
    note               str  — paragraph appended under Notes section
"""

import argparse
import json
import os
import re
import sys
from datetime import date


# ---------------------------------------------------------------------------
# Status badge map
# ---------------------------------------------------------------------------
STATUS_BADGE = {
    'ACTIVE BUY':  'ACTIVE BUY ⭐',
    'CATALYST ALERT': 'CATALYST ALERT ⚡',
    'WATCHLIST':   'WATCHLIST',
    'MONITOR':     'MONITOR',
}


def status_badge(status_str):
    for k, v in STATUS_BADGE.items():
        if k in status_str.upper():
            return v
    return status_str


# ---------------------------------------------------------------------------
# Table rendering helpers
# ---------------------------------------------------------------------------
def row_sep(widths):
    return '|' + '|'.join('-' * (w + 2) for w in widths) + '|'


def md_row(cells, widths):
    parts = []
    for i, cell in enumerate(cells):
        cell_str = str(cell) if cell is not None else ''
        parts.append(f' {cell_str:<{widths[i]}} ')
    return '|' + '|'.join(parts) + '|'


def watchlist_table(entries):
    """Render the main watchlist markdown table."""
    headers = ['Rank', 'Ticker', 'Company', 'Exchange', 'ACS', '3yr Pos%',
               'NVIDIA Signals', 'Classification', 'Entry Level', 'Last Scored',
               'Entry Trigger', 'Status']
    # Compute column widths dynamically
    widths = [max(len(h), 4) for h in headers]
    rows_data = []
    for e in entries:
        badge = status_badge(e.get('status', ''))
        row = [
            str(e.get('rank', '')),
            e.get('ticker', ''),
            e.get('company', ''),
            e.get('exchange', ''),
            str(e.get('acs', '')),
            e.get('3yr_pos_pct', ''),
            e.get('nvidia_signals', ''),
            e.get('classification', ''),
            e.get('entry_level', ''),
            e.get('last_scored', ''),
            e.get('entry_trigger', ''),
            badge,
        ]
        rows_data.append(row)
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    lines = []
    lines.append(md_row(headers, widths))
    lines.append(row_sep(widths))
    for row in rows_data:
        lines.append(md_row(row, widths))
    return '\n'.join(lines)


def notes_section(entries):
    """Render the Notes on Current Rankings section."""
    notes = []
    for e in entries:
        note = e.get('note', '').strip()
        if not note:
            continue
        ticker = e.get('ticker', '')
        rank = e.get('rank', '')
        classification = e.get('classification', '')
        status = e.get('status', '')
        # Build status label
        badge = status_badge(status)
        notes.append(f'**{ticker} (#{rank} — {badge.replace("⭐","").replace("⚡","").strip()}):** {note}\n')
    return '\n'.join(notes) if notes else '*No additional notes this run.*\n'


def rescore_table(entries):
    """Render the Re-Score Calendar markdown table."""
    headers = ['Ticker', 'Next Re-Score Trigger', 'Catalyst to Watch']
    widths = [max(len(h), 6) for h in headers]
    rows_data = []
    for e in entries:
        row = [e.get('ticker', ''), e.get('next_rescore', ''), e.get('catalyst', '')]
        rows_data.append(row)
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = [md_row(headers, widths), row_sep(widths)]
    for row in rows_data:
        lines.append(md_row(row, widths))
    return '\n'.join(lines)


def excluded_table(entries):
    """Render the Structurally Excluded table."""
    headers = ['Ticker', 'Reason']
    widths = [max(len(h), 6) for h in headers]
    rows_data = [[e.get('ticker', ''), e.get('reason', '')] for e in entries]
    for row in rows_data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = [md_row(headers, widths), row_sep(widths)]
    for row in rows_data:
        lines.append(md_row(row, widths))
    return '\n'.join(lines)


def amba_table(entries):
    """Render the AMBA Re-Entry Monitor table."""
    headers = ['Ticker', 'Company', 'ACS at Removal', 'NVIDIA Signals',
               'Current Price', 'Re-Entry Trigger', 'Notes']
    widths = [max(len(h), 4) for h in headers]
    rows_data = []
    for e in entries:
        row = [
            e.get('ticker', ''), e.get('company', ''), str(e.get('acs_at_removal', '')),
            e.get('nvidia_signals', ''), e.get('current_price', ''),
            e.get('re_entry_trigger', ''), e.get('notes', ''),
        ]
        rows_data.append(row)
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = [md_row(headers, widths), row_sep(widths)]
    for row in rows_data:
        lines.append(md_row(row, widths))
    return '\n'.join(lines)


def changelog_table_append(existing_lines, new_entry):
    """Parse existing change log table and append a new row."""
    if not existing_lines:
        # Build fresh table
        headers = ['Month', 'Additions', 'Removals', 'ACS Changes', 'Notes']
        widths = [7, 12, 12, 12, 8]
        for h in headers:
            for i, _ in enumerate(headers):
                widths[i] = max(widths[i], len(h))
        lines = [md_row(headers, widths), row_sep(widths)]
        row = [
            new_entry.get('month', ''),
            new_entry.get('additions', 'None'),
            new_entry.get('removals', 'None'),
            new_entry.get('acs_changes', '—'),
            new_entry.get('notes', ''),
        ]
        lines.append(md_row(row, widths))
        return '\n'.join(lines)

    # Append to existing table: just add a new row matching column pattern
    # Parse widths from the separator line
    sep_line = None
    for line in existing_lines:
        if re.match(r'\|[-| ]+\|', line):
            sep_line = line
            break

    new_row = [
        new_entry.get('month', ''),
        new_entry.get('additions', 'None'),
        new_entry.get('removals', 'None'),
        new_entry.get('acs_changes', '—'),
        new_entry.get('notes', ''),
    ]

    if sep_line:
        # Compute widths from separator
        col_widths = [len(seg) - 2 for seg in sep_line.split('|')[1:-1]]
        while len(new_row) < len(col_widths):
            new_row.append('')
        row_str = md_row(new_row[:len(col_widths)], col_widths)
        return '\n'.join(existing_lines) + '\n' + row_str

    # Fallback: just join existing + new row
    new_row_str = '| ' + ' | '.join(new_row) + ' |'
    return '\n'.join(existing_lines) + '\n' + new_row_str


# ---------------------------------------------------------------------------
# Section extractor — reads existing file to preserve sections not being updated
# ---------------------------------------------------------------------------
def extract_section(content, section_marker_start, section_marker_end=None):
    """Extract content between two markdown heading markers. Returns list of lines."""
    lines = content.split('\n')
    inside = False
    result = []
    for line in lines:
        if section_marker_start in line and line.startswith('#'):
            inside = True
            continue
        if inside:
            if section_marker_end and section_marker_end in line and line.startswith('#'):
                break
            result.append(line)
    # Strip trailing blank lines
    while result and not result[-1].strip():
        result.pop()
    return result


def extract_table_lines(content, section_marker):
    """Extract just the markdown table lines from a section."""
    lines = extract_section(content, section_marker, '##')
    table_lines = [l for l in lines if l.strip().startswith('|')]
    return table_lines


# ---------------------------------------------------------------------------
# File generator
# ---------------------------------------------------------------------------
def generate_watchlist_file(update, existing_content=''):
    """Generate the complete watchlist markdown file from update JSON + existing content."""
    run_month = update.get('run_month', 'Unknown')
    run_note = update.get('run_note', 'v2.0 scorecard')
    watchlist = update.get('watchlist', [])
    change_entry = update.get('change_log_entry', {})
    today = date.today().strftime('%Y-%m-%d')

    # Validate max 5 entries
    if len(watchlist) > 5:
        print(f'WARNING: {len(watchlist)} entries provided — max is 5. Using top 5 by ACS.')
        watchlist = sorted(watchlist, key=lambda x: -int(x.get('acs', 0)))[:5]
        for i, e in enumerate(watchlist, 1):
            e['rank'] = i

    # NVIDIA-class note
    nvidia_note = update.get('nvidia_class_note', '')
    if not nvidia_note:
        nvidia_class = [e for e in watchlist if 'NVIDIA-CLASS' in str(e.get('nvidia_signals', '')).upper()]
        if nvidia_class:
            tickers = ', '.join(f'{e["ticker"]} ({e["acs"]})' for e in nvidia_class)
            nvidia_note = f'*{tickers} — NVIDIA-CLASS pattern (see watchlist above for signal count and S5-MULT status).*\n'
        else:
            nvidia_note = '*None yet. Monitor for candidates reaching >=5 signals or ACS >=85.*\n'

    # Re-score calendar
    if 'rescore_calendar' in update:
        rescore_lines = rescore_table(update['rescore_calendar'])
    elif existing_content:
        existing_rescore = extract_table_lines(existing_content, 'Re-Score Calendar')
        if existing_rescore:
            rescore_lines = '\n'.join(existing_rescore)
        else:
            rescore_lines = '*No re-score calendar entries.*'
    else:
        rescore_lines = '*No re-score calendar entries.*'

    # Structurally excluded
    if 'excluded' in update:
        excl_lines = excluded_table(update['excluded'])
    elif existing_content:
        existing_excl = extract_table_lines(existing_content, 'Structurally Excluded')
        if existing_excl:
            excl_lines = '\n'.join(existing_excl)
        else:
            excl_lines = '| Ticker | Reason |\n|--------|--------|\n| — | None yet |'
    else:
        excl_lines = '| Ticker | Reason |\n|--------|--------|\n| — | None yet |'

    # AMBA monitor
    if 'amba_monitor' in update:
        amba_lines = amba_table(update['amba_monitor'])
    elif existing_content:
        existing_amba = extract_table_lines(existing_content, 'AMBA Re-Entry Monitor')
        if existing_amba:
            amba_lines = '\n'.join(existing_amba)
        else:
            amba_lines = '*No re-entry monitor entries.*'
    else:
        amba_lines = '*No re-entry monitor entries.*'

    # Change log
    if existing_content:
        existing_cl = extract_table_lines(existing_content, 'Watchlist Change Log')
    else:
        existing_cl = []

    if change_entry:
        changelog_lines = changelog_table_append(existing_cl, change_entry)
    else:
        changelog_lines = '\n'.join(existing_cl) if existing_cl else '*No changes logged.*'

    # Assemble file
    lines = [
        '---',
        'name: ISA Asymmetric Watchlist — VCI Pipeline',
        'description: Ranked pipeline of asymmetric multi-bagger candidates identified by the VCI monthly task. Max 5 names. Updated at every VCI run. Read at VCI pre-run (pre-run read 2) and at monthly ISA review (6th pre-run read via VCI output file).',
        'type: project',
        f'originSessionId: f7637f5f-1fa6-4075-a7d9-50bc4a878712',
        f'lastUpdated: {today}',
        '---',
        '',
        '## How to Use This File',
        '',
        '- Read at start of every VCI run (pre-run read 2) and at every monthly ISA review as part of VCI output integration',
        '- Update at end of every VCI run: refresh ACS scores, add new names (ACS >=60), remove purchased/broken-thesis names',
        '- Max 5 names at any time — if a 6th qualifies, remove the lowest-ACS name unless it has an imminent catalyst',
        '- Positions opened: remove from list, record in project_isa_vci_portfolio.md and VCI output file',
        '',
        '---',
        '',
        f'## Current Asymmetric Watchlist ({run_month} run — {run_note})',
        '',
        f'*Updated {today}.*',
        '',
        watchlist_table(watchlist),
        '',
        '---',
        '',
        '## Notes on Current Rankings',
        '',
        notes_section(watchlist),
        '',
        '---',
        '',
        '## NVIDIA-Class Candidates (ACS >=85, >=5 signals)',
        '',
        nvidia_note,
        '',
        '---',
        '',
        '## Re-Score Calendar',
        '',
        rescore_lines,
        '',
        '---',
        '',
        '## Structurally Excluded (do not score)',
        '',
        excl_lines,
        '',
        '---',
        '',
        '## AMBA Re-Entry Monitor',
        '',
        amba_lines,
        '',
        '---',
        '',
        '## Watchlist Change Log',
        '',
        changelog_lines,
    ]

    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Update project_isa_vci_watchlist.md from a JSON update file.'
    )
    parser.add_argument('--watchlist', required=True, metavar='PATH',
                        help='Path to project_isa_vci_watchlist.md (the memory file to update)')
    parser.add_argument('--update', required=True, metavar='PATH',
                        help='Path to JSON update file (see update_vci_watchlist_TEMPLATE.json)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print generated file to stdout without writing')
    args = parser.parse_args()

    # Load update JSON
    if not os.path.exists(args.update):
        print(f'ERROR: Update JSON not found: {args.update}')
        sys.exit(1)
    with open(args.update, encoding='utf-8') as f:
        update = json.load(f)

    # Load existing watchlist (for preserving sections)
    existing_content = ''
    if os.path.exists(args.watchlist):
        with open(args.watchlist, encoding='utf-8') as f:
            existing_content = f.read()
        print(f'Loaded existing watchlist: {args.watchlist}')
    else:
        print(f'NOTE: Watchlist file not found — generating from scratch: {args.watchlist}')

    # Generate new content
    new_content = generate_watchlist_file(update, existing_content)

    if args.dry_run:
        print('\n' + '=' * 72)
        print('DRY RUN — generated content (not written):')
        print('=' * 72)
        print(new_content)
        return

    # Write back
    with open(args.watchlist, 'w', encoding='utf-8') as f:
        f.write(new_content)

    n_entries = len(update.get('watchlist', []))
    run_month = update.get('run_month', 'Unknown')
    print(f'Watchlist updated: {args.watchlist}')
    print(f'  Run: {run_month} | Entries: {n_entries}')
    print(f'  Change log entry appended.')
    tickers = [e.get('ticker', '') for e in update.get('watchlist', [])]
    print(f'  Current watchlist: {", ".join(tickers)}')


if __name__ == '__main__':
    main()
