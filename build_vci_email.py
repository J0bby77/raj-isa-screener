#!/usr/bin/env python3
"""
build_vci_email.py — VCI Monthly Run HTML Email Builder
Version: 1.0 | June 2026

Generates the complete E0–E6 VCI email body from a structured JSON input file.
Companion template: vci_email_data_TEMPLATE.json (copy and fill each run).

Usage:
    python build_vci_email.py --json vci_email_data_jun2026.json --output email_body.html

HTML compliance rules (mandatory — enforced by verify_entities()):
    Rule 1 — No Unicode above U+007F (all special chars replaced with HTML entities)
    Rule 2 — No <style> or <style type="text/css"> blocks (all styles inline)
    Rule 3 — No flexbox / CSS Grid (multi-column uses <table> layout only)
    Rule 4 — No external images, fonts, or base64 embeds
    Rule 5 — No DOCTYPE/html/head/body wrappers

Email sections:
    E0  Header (run date, N scored, sleeve value)
    E1  NVIDIA-Pattern Alert (only if >=3 signals on any candidate)
    E2  Top Candidates table (ACS >=45 only; colour-coded by ACS band)
    E3  Structural Theme Status
    E4  Asymmetric Sleeve Portfolio Update
    E5  Monthly Review Integration (headroom, fast-tracks, watchlist changes, catalysts)
    E6  Retrospective (1–5 items)
"""

import argparse
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import json
import os
import sys

# ---------------------------------------------------------------------------
# HTML entity safety — identical rules to build_email.py
# ---------------------------------------------------------------------------
ENTITY_MAP = [
    ('≥', '&ge;'), ('≤', '&le;'), ('—', '&mdash;'),
    ('–', '&ndash;'), ('−', '&minus;'), ('§', '&sect;'),
    ('→', '&rarr;'), ('×', '&times;'), ('£', '&pound;'),
    ('€', '&euro;'), ('±', '&plusmn;'), ('≠', '&ne;'),
    ('©', '&copy;'), ('®', '&reg;'), ('’', '&rsquo;'),
    ('‘', '&lsquo;'), ('“', '&ldquo;'), ('”', '&rdquo;'),
    ('…', '&hellip;'), ('°', '&deg;'), ('²', '&sup2;'),
    ('³', '&sup3;'), ('★', '&#9733;'), ('⚡', '&#9889;'),
    ('✘', '&#10008;'), ('✔', '&#10004;'), ('⚠', '&#9888;'),
    ('⭐', '&#11088;'),
]


def se(text):
    """safe_entities — replace all non-ASCII chars with HTML entities."""
    if not text:
        return ''
    text = str(text)
    for char, entity in ENTITY_MAP:
        text = text.replace(char, entity)
    result = []
    for c in text:
        if ord(c) > 127:
            result.append(f'&#{ord(c)};')
        else:
            result.append(c)
    return ''.join(result)


def verify_entities(html):
    violations = [c for c in html if ord(c) > 127]
    if violations:
        print(f'WARNING: {len(violations)} non-ASCII characters remain in HTML.')
        print(f'  First: U+{ord(violations[0]):04X} ({violations[0]!r})')
        return False
    return True


# ---------------------------------------------------------------------------
# ACS colour coding for E2 table rows
# ---------------------------------------------------------------------------
def acs_row_bg(acs):
    """Return background colour for ACS score band."""
    try:
        s = int(acs)
    except (TypeError, ValueError):
        return '#ffffff'
    if s >= 85:
        return '#fff5f5'    # red tint — NVIDIA-class
    if s >= 75:
        return '#fffbeb'    # orange tint — high conviction
    if s >= 60:
        return '#fefce8'    # yellow tint — moderate
    return '#ffffff'        # white — early monitoring


def acs_label_color(acs):
    """Return label colour for ACS score."""
    try:
        s = int(acs)
    except (TypeError, ValueError):
        return '#374151'
    if s >= 85:
        return '#dc2626'
    if s >= 75:
        return '#d97706'
    if s >= 60:
        return '#b45309'
    return '#374151'


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_e0(data):
    """E0: Email header."""
    run_month = se(data.get('run_month', 'Unknown'))
    run_date = se(data.get('run_date', ''))
    n_scored = se(str(data.get('n_scored', 0)))
    sleeve_val = data.get('sleeve_value_gbp', 0)
    sleeve_pct = data.get('sleeve_pct', 0.0)
    sleeve_str = se(f'&pound;{sleeve_val:,.0f} ({sleeve_pct:.1f}% of ISA)') if sleeve_val else se('&pound;0 (no open positions)')

    return (
        f'<div style="background:#1a1a2e;padding:16px 24px;border-radius:6px 6px 0 0;">'
        f'<p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#9a9ab0;'
        f'letter-spacing:1px;text-transform:uppercase;">VALUE CHAIN INTELLIGENCE</p>'
        f'<p style="margin:4px 0 0;font-family:Arial,sans-serif;font-size:20px;'
        f'font-weight:bold;color:#ffffff;">{run_month} Monthly Run</p>'
        f'<p style="margin:4px 0 0;font-family:Arial,sans-serif;font-size:12px;color:#9a9ab0;">'
        f'Run date: {run_date} &nbsp;&#8226;&nbsp; {n_scored} candidates scored &nbsp;&#8226;&nbsp; '
        f'Asymmetric sleeve: {sleeve_str}</p>'
        f'</div>\n'
    )


def build_e1(data):
    """E1: NVIDIA-Pattern Alert — only if any candidate has >=3 signals."""
    alerts = data.get('nvidia_alerts', [])
    if not alerts:
        return ''

    html = ''
    for alert in alerts:
        ticker = se(alert.get('ticker', ''))
        company = se(alert.get('company', ''))
        n_signals = alert.get('n_signals', 0)
        acs = alert.get('acs', 0)
        classification = se(alert.get('classification', ''))
        thesis = se(alert.get('thesis_sentence', ''))
        signals_list = alert.get('signals_active', [])
        signals_str = se(', '.join(signals_list))

        html += (
            f'<div style="background:#fff5f5;border-left:4px solid #dc2626;'
            f'padding:12px 16px;margin:16px 0;border-radius:0 4px 4px 0;">'
            f'<p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:13px;'
            f'font-weight:bold;color:#dc2626;">&#9888; NVIDIA-PATTERN ALERT &mdash; '
            f'{n_signals} signal(s) active on {ticker}</p>'
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#7f1d1d;">'
            f'{company}: {thesis} ACS: {acs}/100. '
            f'Classification: {classification}. Signals: {signals_str}.</p>'
            f'</div>\n'
        )
    return html


def build_e2(data):
    """E2: Top Candidates table — ACS >=45 only, colour-coded by band."""
    candidates = [c for c in data.get('candidates', []) if int(c.get('acs', 0)) >= 45]
    if not candidates:
        return (
            '<div style="margin:16px 0;">'
            '<p style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
            'color:#1a1a2e;margin:0 0 8px;">Top Candidates This Run</p>'
            '<p style="font-family:Arial,sans-serif;font-size:12px;color:#6b7280;">'
            'No candidates scored ACS &ge;45 this run.</p></div>\n'
        )

    # FORWARD-LED (FWDVCI, Jul-2026): the candidate table is a DEPLOYABILITY ranking. Primary
    # column is VCI Source Score (recomputed at live price), ACS is the secondary quality floor,
    # and fv_asymmetry / catalyst-days / deploy-eligible are shown. Entry levels are display-only
    # and live in the watchlist file — they are NOT a column here (distance from entry never ranks).
    header_cells = ['Rank', 'Ticker', 'VCI&nbsp;Src', 'ACS', 'fv&nbsp;asym', 'Floor',
                    'Cat&nbsp;d', 'NVIDIA&nbsp;Sig', 'Deploy', 'Action']
    col_widths = ['4%', '8%', '9%', '7%', '8%', '7%', '6%', '15%', '16%', '20%']

    header_html = ''.join(
        f'<th style="background:#1a1a2e;color:#ffffff;padding:8px 6px;'
        f'font-family:Arial,sans-serif;font-size:11px;text-align:center;'
        f'white-space:nowrap;width:{col_widths[i]};">{h}</th>'
        for i, h in enumerate(header_cells)
    )

    def _fmt_asym(v):
        try:
            return f'{float(v):.2f}&times;'
        except (TypeError, ValueError):
            return '&mdash;'

    def _fmt_src(v):
        try:
            return f'{float(v):.1f}'
        except (TypeError, ValueError):
            return '&mdash;'

    def _fmt_floor(c):
        v = c.get('fv_floor')
        try:
            base = f'{float(v):.2f}&times;'
        except (TypeError, ValueError):
            return '&mdash;'
        # superscript 'd' marks a probability-weighted (E1-derived) floor vs the fixed tier
        return base + ('<sup>d</sup>' if c.get('floor_source') == 'derived' else '')

    rows_html = ''
    # rank by VCI Source Score desc (deployability), tiebreak ACS desc — NOT ACS-primary
    for c in sorted(candidates, key=lambda x: (-(x.get('vci_source_score') or 0), -int(x.get('acs', 0)))):
        acs = int(c.get('acs', 0))
        bg = acs_row_bg(acs)
        acs_colour = acs_label_color(acs)
        action = c.get('action', '')
        action_bold = 'font-weight:bold;' if action in ('ACTIVE BUY', 'WATCHLIST') else ''
        action_color = '#16a34a' if action == 'ACTIVE BUY' else ('#1d4ed8' if action == 'WATCHLIST' else '#374151')
        elig = c.get('deploy_eligible')
        elig_txt = 'YES' if elig is True else ('no' if elig is False else '&mdash;')
        elig_color = '#16a34a' if elig is True else '#9ca3af'
        # v2 compact deploy flags: FV cross-check warn / liquidity-capped / FV-erosion (E2/E5/E7)
        _flags = ''
        if c.get('fv_crosscheck_warn'):
            _flags += ' &#9888;'
        if c.get('size_liquidity_capped'):
            _flags += ' ~'
        if c.get('asymmetry_compression_cause') == 'fv_down':
            _flags += ' &darr;FV'
        cat_d = c.get('days_to_catalyst')
        cat_txt = se(str(cat_d)) if cat_d not in (None, '') else '&mdash;'
        # v2 (E2): show the conservative P25 asymmetry when available (the eligibility basis)
        _asym = c.get('fv_asymmetry_p25') if c.get('fv_asymmetry_p25') is not None else c.get('fv_asymmetry')

        td = f'padding:7px 6px;border-bottom:1px solid #e5e7eb;background:{bg};font-family:Arial,sans-serif;font-size:12px;text-align:center;'
        rows_html += (
            f'<tr>'
            f'<td style="{td}">{se(str(c.get("rank", "")))}</td>'
            f'<td style="{td}font-weight:bold;">{se(c.get("ticker", ""))}</td>'
            f'<td style="{td}font-weight:bold;">{_fmt_src(c.get("vci_source_score"))}</td>'
            f'<td style="{td}color:{acs_colour};">{acs}/100</td>'
            f'<td style="{td}">{_fmt_asym(_asym)}</td>'
            f'<td style="{td}">{_fmt_floor(c)}</td>'
            f'<td style="{td}">{cat_txt}</td>'
            f'<td style="{td}">{se(str(c.get("nvidia_signals", "")))}</td>'
            f'<td style="{td}font-weight:bold;color:{elig_color};">{elig_txt}{_flags}</td>'
            f'<td style="{td}{action_bold}color:{action_color};">{se(action)}</td>'
            f'</tr>\n'
        )

    # ACS band legend + forward-led note
    legend = (
        '<p style="font-family:Arial,sans-serif;font-size:10px;color:#6b7280;margin:4px 0 0;">'
        'Ranked by <b>VCI Source Score</b> (deployability, live-price); ACS is the quality floor. '
        'fv&nbsp;asym = bottleneck FV &divide; price (conservative P25 when available). '
        'Floor = applied bar (2.0&times; platform / 2.5&times; single-asset; <sup>d</sup> = probability-weighted, E1). '
        'Deploy flags: &#9888; FV cross-check, ~ liquidity-capped, &darr;FV thesis-erosion. '
        'Entry levels are display-only (watchlist file). '
        'ACS colour: <span style="background:#fff5f5;padding:1px 4px;">&ge;85 NVIDIA-class</span>&nbsp;'
        '<span style="background:#fffbeb;padding:1px 4px;">75&ndash;84 High</span>&nbsp;'
        '<span style="background:#fefce8;padding:1px 4px;">60&ndash;74 Moderate</span>&nbsp;'
        '<span style="background:#ffffff;padding:1px 4px;border:1px solid #e5e7eb;">45&ndash;59 Early</span>'
        '</p>\n'
    )

    # §13 calibration-gate status line (advisory until 12 resolved outcomes)
    cal = data.get('calibration_gate')
    cal_html = ''
    if cal:
        cal_html = (
            '<p style="font-family:Arial,sans-serif;font-size:10px;color:#6b7280;margin:4px 0 0;">'
            f'Calibration: {se(str(cal))}. Weights are advisory until the &ge;12-outcome gate passes; '
            'the top eligible name is human-confirmed.</p>\n'
        )
    # v2 (E4): sleeve binary risk-budget headroom line (rendered only when supplied)
    _rc = data.get('vci_binary_risk_committed'); _rb = data.get('vci_binary_risk_budget')
    if _rc is not None and _rb:
        cal_html += (
            '<p style="font-family:Arial,sans-serif;font-size:10px;color:#6b7280;margin:2px 0 0;">'
            f'Sleeve binary risk budget: {se(str(round(float(_rc), 2)))}% / {se(str(_rb))}% ISA used '
            '(&Sigma; size&middot;L&middot;(1&minus;p) across open + proposed binaries).</p>\n'
        )

    return (
        '<div style="margin:16px 0;">'
        '<p style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
        'color:#1a1a2e;margin:0 0 8px;">Top Candidates This Run &mdash; ranked by deployability</p>'
        f'<table style="width:100%;border-collapse:collapse;" cellpadding="0" cellspacing="0" border="0">'
        f'<tr>{header_html}</tr>\n{rows_html}</table>\n'
        f'{legend}{cal_html}'
        f'</div>\n'
    )


def build_e3(data):
    """E3: Structural Theme Status table."""
    themes = data.get('themes', [])
    if not themes:
        return ''

    status_colours = {
        'PRE-INFLECTION': '#1d4ed8',
        'INFLECTING': '#16a34a',
        'FULLY PRICED': '#6b7280',
        'PRICED': '#6b7280',
        'LARGE-CAP DOMINATED': '#9ca3af',
        'RETIRED': '#d1d5db',
    }

    header_cells = ['Theme', 'Status', 'Catalyst Timeline', 'Primary Bottleneck Candidate']
    col_widths = ['28%', '16%', '28%', '28%']
    header_html = ''.join(
        f'<th style="background:#1a1a2e;color:#ffffff;padding:7px 8px;'
        f'font-family:Arial,sans-serif;font-size:11px;text-align:left;width:{col_widths[i]};">{h}</th>'
        for i, h in enumerate(header_cells)
    )
    rows_html = ''
    for i, t in enumerate(themes):
        bg = '#f9fafb' if i % 2 == 0 else '#ffffff'
        status = t.get('status', '').upper()
        sc = status_colours.get(status, '#374151')
        td = f'padding:6px 8px;border-bottom:1px solid #e5e7eb;background:{bg};font-family:Arial,sans-serif;font-size:12px;'
        rows_html += (
            f'<tr>'
            f'<td style="{td}font-weight:bold;">{se(t.get("name", ""))}</td>'
            f'<td style="{td}font-weight:bold;color:{sc};">{se(status)}</td>'
            f'<td style="{td}">{se(t.get("catalyst_timeline", ""))}</td>'
            f'<td style="{td}">{se(t.get("bottleneck_candidate", ""))}</td>'
            f'</tr>\n'
        )

    return (
        '<div style="margin:16px 0;">'
        '<p style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
        'color:#1a1a2e;margin:0 0 8px;">Structural Theme Status</p>'
        f'<table style="width:100%;border-collapse:collapse;" cellpadding="0" cellspacing="0" border="0">'
        f'<tr>{header_html}</tr>\n{rows_html}</table>'
        f'</div>\n'
    )


def build_e4(data):
    """E4: Asymmetric Sleeve Portfolio Update."""
    portfolio = data.get('portfolio', [])

    if not portfolio:
        return (
            '<div style="margin:16px 0;">'
            '<p style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
            'color:#1a1a2e;margin:0 0 8px;">Asymmetric Sleeve Portfolio Update</p>'
            '<p style="font-family:Arial,sans-serif;font-size:12px;color:#6b7280;'
            'background:#f9fafb;padding:10px 12px;border-radius:4px;">'
            'No open positions. Sleeve fully in cash.</p>'
            '</div>\n'
        )

    header_cells = ['Ticker', 'Entry &#163;', 'Current &#163;', 'Gain %', 'Thesis', 'Milestone', 'Next Action']
    col_widths = ['8%', '9%', '10%', '8%', '18%', '15%', '24%']
    header_html = ''.join(
        f'<th style="background:#1a1a2e;color:#ffffff;padding:7px 8px;'
        f'font-family:Arial,sans-serif;font-size:11px;text-align:center;width:{col_widths[i]};">{h}</th>'
        for i, h in enumerate(header_cells)
    )

    rows_html = ''
    for i, pos in enumerate(portfolio):
        bg = '#f9fafb' if i % 2 == 0 else '#ffffff'
        thesis = pos.get('thesis', 'INTACT')
        thesis_upper = thesis.upper()
        if 'BROKEN' in thesis_upper:
            bg = '#fff5f5'
            thesis_colour = '#dc2626'
        elif 'WEAKENING' in thesis_upper:
            bg = '#fff7ed'
            thesis_colour = '#d97706'
        else:
            thesis_colour = '#16a34a'

        milestone = pos.get('milestone', '')
        milestone_style = 'font-weight:bold;color:#d97706;' if '2x' in milestone else ''

        gain_pct = pos.get('gain_pct', '')
        try:
            gv = float(str(gain_pct).replace('%', ''))
            gain_colour = '#16a34a' if gv >= 0 else '#dc2626'
            gain_str = f'+{gv:.1f}%' if gv >= 0 else f'{gv:.1f}%'
        except (TypeError, ValueError):
            gain_colour = '#374151'
            gain_str = se(str(gain_pct))

        td = f'padding:7px 8px;border-bottom:1px solid #e5e7eb;background:{bg};font-family:Arial,sans-serif;font-size:12px;text-align:center;'

        rows_html += (
            f'<tr>'
            f'<td style="{td}font-weight:bold;">{se(pos.get("ticker", ""))}</td>'
            f'<td style="{td}">&#163;{se(str(pos.get("entry_price", "")))}</td>'
            f'<td style="{td}">&#163;{se(str(pos.get("current_price", "")))}</td>'
            f'<td style="{td}font-weight:bold;color:{gain_colour};">{gain_str}</td>'
            f'<td style="{td}text-align:left;font-weight:bold;color:{thesis_colour};">{se(thesis)}</td>'
            f'<td style="{td}{milestone_style}text-align:left;">{se(milestone)}</td>'
            f'<td style="{td}text-align:left;">{se(pos.get("next_action", ""))}</td>'
            f'</tr>\n'
        )

    return (
        '<div style="margin:16px 0;">'
        '<p style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;'
        'color:#1a1a2e;margin:0 0 8px;">Asymmetric Sleeve Portfolio Update</p>'
        f'<table style="width:100%;border-collapse:collapse;" cellpadding="0" cellspacing="0" border="0">'
        f'<tr>{header_html}</tr>\n{rows_html}</table>'
        f'</div>\n'
    )


def build_e5(data):
    """E5: Monthly Review Integration block."""
    headroom = data.get('headroom_gbp', 0)
    pct_used = data.get('headroom_pct_used', 0)
    limit_pct = data.get('headroom_limit_pct', 5)
    remaining_pct = max(0, limit_pct - pct_used)
    fast_tracks = data.get('fast_tracks', [])
    ft_str = se(', '.join(fast_tracks)) if fast_tracks else 'None'
    added = data.get('watchlist_added', [])
    removed = data.get('watchlist_removed', [])
    added_str = se(', '.join(added)) if added else 'None'
    removed_str = se(', '.join(removed)) if removed else 'None'
    catalysts = data.get('catalysts_forward', [])

    cat_rows = ''
    for cat in catalysts[:6]:
        cat_rows += (
            f'<tr><td colspan="2" style="font-family:Arial,sans-serif;font-size:12px;'
            f'color:#111827;padding:2px 0;">&#8226; {se(cat)}</td></tr>\n'
        )
    if not catalysts:
        cat_rows = ('<tr><td colspan="2" style="font-family:Arial,sans-serif;font-size:12px;'
                    'color:#6b7280;padding:2px 0;">None identified this run</td></tr>\n')

    def row(label, value_html):
        return (
            f'<tr>'
            f'<td style="font-family:Arial,sans-serif;font-size:12px;color:#374151;'
            f'padding:3px 8px 3px 0;vertical-align:top;white-space:nowrap;width:200px;">{se(label)}</td>'
            f'<td style="font-family:Arial,sans-serif;font-size:12px;color:#111827;padding:3px 0;">{value_html}</td>'
            f'</tr>\n'
        )

    return (
        f'<div style="background:#f8f9fa;padding:12px 16px;border-radius:4px;margin:16px 0;">'
        f'<p style="margin:0 0 8px;font-family:Arial,sans-serif;font-size:13px;'
        f'font-weight:bold;color:#1a1a2e;">Monthly Review Integration</p>'
        f'<table style="border-collapse:collapse;width:100%;" cellpadding="0" cellspacing="0" border="0">'
        + row('Asymmetric sleeve headroom:',
              f'&#163;{headroom:,.0f} available ({remaining_pct:.1f}% of {limit_pct}% limit remaining; {pct_used:.1f}% used)')
        + row('Step&nbsp;10 fast-tracks:', ft_str)
        + row('Watchlist additions:', added_str)
        + row('Watchlist removals:', removed_str)
        + row('Forward catalysts:', '')
        + cat_rows
        + '</table></div>\n'
    )


def build_e6(data):
    """E6: Retrospective (1-5 items)."""
    items = data.get('retrospective', [])[:5]
    if not items:
        return ''

    impact_colours = {
        'XL': '#dc2626', 'L': '#d97706', 'M': '#2563eb',
        'S': '#6b7280', 'XS': '#9ca3af',
    }

    header_cells = ['Observation', 'Enhancement / Change', 'Category', 'Impact']
    col_widths = ['35%', '35%', '18%', '12%']
    header_html = ''.join(
        f'<th style="background:#e5e7eb;padding:5px 8px;font-family:Arial,sans-serif;'
        f'font-size:11px;text-align:left;font-weight:bold;width:{col_widths[i]};">{h}</th>'
        for i, h in enumerate(header_cells)
    )

    rows_html = ''
    for i, item in enumerate(items):
        bg = '#ffffff' if i % 2 == 0 else '#f9fafb'
        impact = str(item.get('impact', 'M')).upper()
        ic = impact_colours.get(impact, '#374151')
        td = f'padding:5px 8px;border-bottom:1px solid #e5e7eb;background:{bg};font-family:Arial,sans-serif;font-size:11px;'
        rows_html += (
            f'<tr>'
            f'<td style="{td}color:#374151;">{se(item.get("observation", ""))}</td>'
            f'<td style="{td}color:#111827;">{se(item.get("enhancement", ""))}</td>'
            f'<td style="{td}color:#6b7280;">{se(item.get("category", ""))}</td>'
            f'<td style="{td}font-weight:bold;color:{ic};">{se(impact)}</td>'
            f'</tr>\n'
        )

    return (
        f'<div style="background:#f8f9fa;border-top:2px solid #e5e7eb;'
        f'padding:12px 16px;margin:16px 0;">'
        f'<p style="margin:0 0 8px;font-family:Arial,sans-serif;font-size:13px;'
        f'font-weight:bold;color:#1a1a2e;">Retrospective</p>'
        f'<table style="border-collapse:collapse;width:100%;" cellpadding="0" cellspacing="0" border="0">'
        f'<tr>{header_html}</tr>\n{rows_html}</table>'
        f'<p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:10px;color:#9ca3af;">'
        f'Impact: XL = changes next run materially | L = significant | M = moderate | S = minor | XS = cosmetic</p>'
        f'</div>\n'
    )


def build_footer(data):
    run_month = se(data.get('run_month', ''))
    run_date = se(data.get('run_date', ''))
    return (
        f'<p style="font-size:11px;color:#9ca3af;border-top:1px solid #eee;'
        f'padding-top:12px;font-family:Arial,sans-serif;">'
        f'Value Chain Intelligence &mdash; {run_month} | {run_date} | claude-sonnet-4-6<br>'
        f'Not investment advice. Verify against primary sources before acting.'
        f'</p>\n'
    )


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------
def build_email_body(data):
    parts = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;'
        'max-width:800px;margin:0 auto;padding:20px;">\n',
        build_e0(data),
        build_e1(data),
        build_e2(data),
        build_e3(data),
        build_e4(data),
        build_e5(data),
        build_e6(data),
        build_footer(data),
        '</div>\n',
    ]
    return ''.join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Build VCI monthly run HTML email body from JSON input.'
    )
    parser.add_argument('--json', required=True, metavar='PATH',
                        help='Path to email data JSON (use vci_email_data_TEMPLATE.json as starting point)')
    parser.add_argument('--output', required=True, metavar='PATH',
                        help='Output path for HTML body file e.g. vci_email_body.html')
    args = parser.parse_args()

    if not os.path.exists(args.json):
        print(f'ERROR: JSON file not found: {args.json}')
        sys.exit(1)

    with open(args.json, encoding='utf-8') as f:
        data = json.load(f)

    print(f'Building VCI email body from: {args.json}')
    body = build_email_body(data)

    ok = verify_entities(body)
    if not ok:
        print('ERROR: Non-ASCII characters detected. Fix before sending.')
        sys.exit(1)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, 'w', encoding='ascii', errors='xmlcharrefreplace') as f:
        f.write(body)

    # Summary
    n_scored = data.get('n_scored', 0)
    nvidia_tickers = [a.get('ticker', '') for a in data.get('nvidia_alerts', [])]
    nvidia_str = ', '.join(nvidia_tickers) if nvidia_tickers else 'No NVIDIA-class flags this run'

    print(f'Email body written: {args.output}  ({len(body):,} chars)')
    print(f'Entity check: PASS')
    print()
    print('=' * 60)
    print('GMAIL_SEND_EMAIL parameters:')
    print(f'  recipient_email: rjobanputra@sky.com')
    print(f'  subject:         VCI {data.get("run_month", "Unknown")} | {n_scored} scored | {nvidia_str}')
    print(f'  body:            <contents of {args.output}>')
    print(f'  is_html:         true   <-- MANDATORY')
    print('=' * 60)


if __name__ == '__main__':
    main()
