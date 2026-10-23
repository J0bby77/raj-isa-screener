#!/usr/bin/env python3
"""
corporate_events.py - ISA-0817 (18-Oct-2026): offer-period and corporate-action state, MECHANICALLY.

⚑ THE DEFECT THIS CLOSES. October-2026: the Capital Decision Engine selected SPI.L (Spire Healthcare) for a
START of GBP 5,613.69 while the issuer was under a recommended, declared-final 250p cash offer. Nothing on the
capital path knew an offer existed, so ordinary growth worlds were applied to a price pinned to offer terms: a
merger-arbitrage name scored a 33.8% central ER12 whose true 12-month return was the spread (a few percent) with
a deal-break left tail. issuer_freshness types events for HELD names only; no candidate was ever asked.

⚑ WHAT IT PRODUCES (pre-run Step 5x-ce, after 5x-ie): GENERATED `corporate_events.json` - one typed verdict per
symbol of the verified symbol map plus every held stock, each with its evidence, source, retrieval time and
the bytes-hash of the source document; and an append-only `corporate_events_history.jsonl` (learning capture).

⚑ THE RULE (Raj 18-Oct-2026, decisions D1-D3 on ISA-0817 rev3):
  OFFEREE_IN_OFFER_PERIOD   UK: the ISIN-matched offeree in the Takeover Panel Disclosure Table (possible Rule 2.4
                            AND firm Rule 2.7 offers alike). US: a subject-company tender/merger filing (SC 14D9,
                            SC TO-T, SC 13E3, PREM14A/DEFM14A, PREM14C/DEFM14C) inside the window, not followed by
                            a terminating 8-K item 1.02.  -> flag `offer_period_offeree` = HARD_BLOCK of NEW capital
                            (buy and top-up). A held name is NEVER auto-sold: REVIEW only. The block lifts when the
                            name leaves the source. A typed flag_adjudication (RESOLVED_BENIGN) covers a stale source.
  CORPORATE_ACTION_PENDING  acquirer side, capital raise, material disposal, special distribution or an unconfirmed
                            offer headline -> flag `corporate_action_pending` = REVIEW_REQUIRED + case refresh.
  OFFER_STATE_UNSOURCED     a venue with no mechanical offer-state source (EU ex-UK, others) -> fundable, carried
                            to CONFIRM AT DEALING (the eligibility pattern). A price-pinning detector on the weekly
                            store adds `price_pinned_suspect` = REVIEW_REQUIRED.
  SOURCE_FAILED             a sourceable venue whose source could not be read this run -> flag
                            `offer_state_source_failed` = REVIEW_REQUIRED (the issuer_freshness asymmetry: an
                            unestablished mandatory state blocks NEW capital, never manufactures a SELL).
  NO_EVENT_FOUND            the source was read and holds nothing for this issuer.

⚑ COVERAGE LIMITS (stated, never hidden): the Panel table covers companies subject to the UK Takeover Code; an
LSE-listed non-Code company relies on the RNS headline backstop (last ~50 headlines). EDGAR forms are typed from
metadata only (form, date, 8-K items); DEFM14A is occasionally an ACQUIRER proxy - a false block is cleared by
adjudication. The pin detector uses WEEKLY levels and has low power (calibrated 18-Oct-2026: SPI.L ranked 2nd of
212 by 8-week weekly-return std); it is a REVIEW signal only.

⚑ NEVER: a network call from a consumer (step9_pre_builder / the engine read the GENERATED file only); a
refusal or a SELL on missing data; a guessed issuer (UK matches by ISIN->FIGI identity, never by name).
ROLLBACK (R4.13): isa_policy.V2_FLAGS['corporate_events'] = False -> verdict() returns DISABLED and no flag
is raised (pre-ISA-0817 behaviour); `corporate_events_refresh` = False -> no fetch, the file on disk is used.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"
STORE_FILE = "corporate_events.json"
HISTORY_FILE = "corporate_events_history.jsonl"
SYMBOL_MAP_FILE = "stock_symbol_map.json"
ELIGIBILITY_FILE = "instrument_eligibility.json"
WEEKLY_STORE_FILE = "stock_weekly_returns.json"

SOURCES = {
    "UK_TAKEOVER_PANEL": {
        "url": "https://www.thetakeoverpanel.org.uk/new/disclosuretable/v3/disclosuretable.xml",
        "quality": "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
        "scope": "offerees in an offer period, companies subject to the UK Takeover Code (ISIN-keyed, daily)"},
    "OPENFIGI_ISIN": {"url": "https://api.openfigi.com/v3/mapping", "quality": "IDENTIFIER_SERVICE",
                      "scope": "ISIN -> FIGI identity join to instrument_eligibility.json"},
    "SEC_TICKERS": {"url": "https://www.sec.gov/files/company_tickers.json", "quality": "REGULATOR",
                    "scope": "US ticker -> CIK"},
    "SEC_SUBMISSIONS": {"url": "https://data.sec.gov/submissions/CIK{cik:010d}.json", "quality": "REGULATOR",
                        "scope": "filing metadata (form, date, 8-K items) per issuer"},
    "CH_TAKEOVER_BOARD": {"url": "https://www.takeover.ch/transactions/all",
                          "detail": "https://www.takeover.ch/transactions/detail/nr/{nr}",
                          "quality": "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
                          "scope": "Swiss public offers and buybacks (Swiss Takeover Board register; name join, no ISIN)"},
    "NASDAQ_NORDIC_RELEASES": {"url": "https://api.news.eu.nasdaq.com/news/query.action?type=json&showAttachments=false"
                                      "&showCompany=true&countResults=false&freeText={q}&fromDate={frm}&toDate={to}"
                                      "&globalGroup=exchangeNotice&globalName=NordicAllMarkets&displayLanguage=en"
                                      "&language=en&limit=100&start=0&dir=DESC",
                               "quality": "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
                               "scope": "issuer regulatory releases, Nasdaq Stockholm/Helsinki/Copenhagen/Iceland (company match)"},
    "IE_TAKEOVER_PANEL": {"url": "https://irishtakeoverpanel.ie/disclosure-table/", "quality":
                          "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
                          "scope": "offerees in an offer period, Irish Takeover Rules (PDF, ISIN-keyed)"},
    "NEWS_SEARCH": {"url": "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en",
                    "quality": "APPROVED_SECONDARY_DATABASE_CORROBORATION_ONLY",
                    "scope": "per-issuer news headlines (60 days) for venues with no primary register - REVIEW only (R6.3)"},
    "UK_RNS_HEADLINES": {"url": "https://www.investegate.co.uk/company/{epic}", "quality":
                         "HIGH_QUALITY_INDEPENDENT_FINANCIAL_REPORTING",
                         "scope": "latest ~50 regulatory headlines per UK issuer (backstop + D3 events)"},
}
SEC_UA = "ISA research tool (personal, non-commercial) rjobanputra@sky.com"

UK_VENUES = ("LSE", "IOB", "AIM", "LON")
US_VENUES = ("NMS", "NYQ", "NCM", "NGM", "ASE", "PCX", "BTS", "NAS", "NYS", "PNK")

# ── typed states and flag codes (the engine's FLAG_TAXONOMY classifies the codes) ─────────────────────
OFFEREE_IN_OFFER_PERIOD = "OFFEREE_IN_OFFER_PERIOD"
CORPORATE_ACTION_PENDING = "CORPORATE_ACTION_PENDING"
OFFER_STATE_UNSOURCED = "OFFER_STATE_UNSOURCED"
SOURCE_FAILED = "SOURCE_FAILED"
NO_EVENT_FOUND = "NO_EVENT_FOUND"
NOT_APPLICABLE = "NOT_APPLICABLE"          # a fund / ETP (OpenFIGI securityType2 'Mutual Fund'): no offer period
DISABLED = "DISABLED"
STATES = (OFFEREE_IN_OFFER_PERIOD, CORPORATE_ACTION_PENDING, OFFER_STATE_UNSOURCED, SOURCE_FAILED,
          NO_EVENT_FOUND, NOT_APPLICABLE, DISABLED)
NOT_APPLICABLE_TYPES = ("Mutual Fund", "ETP")
FLAG_OFFEREE = "offer_period_offeree"
FLAG_CORP_ACTION = "corporate_action_pending"
FLAG_SOURCE_FAILED = "offer_state_source_failed"
FLAG_PINNED = "price_pinned_suspect"
FLAG_CODES = (FLAG_OFFEREE, FLAG_CORP_ACTION, FLAG_SOURCE_FAILED, FLAG_PINNED, "news_corporate_event_unconfirmed")
CONFIRM_OFFER_STATE = "OFFER_STATE_UNSOURCED"      # the CONFIRM AT DEALING reason code

# ── US filing classification (metadata only) ─────────────────────────────────────────────────────────
# ⚑ SC TO-T is filed under BOTH the bidder's and the subject's CIK, so on its own it does not say which side this
#   issuer is on (18-Oct-2026: ARGX and AUPH carried SC TO-T as BIDDERS for Forte / Kezar). The target must answer a
#   tender with SC 14D9, so: offeree = SC 14D9 / merger proxy / SC 13E3; SC TO-T without SC 14D9 = M&A party.
US_OFFEREE_FORMS = ("SC 14D9", "SC 14D9/A", "SC 13E3", "SC 13E3/A",
                    "PREM14A", "DEFM14A", "PREM14C", "DEFM14C")
US_TENDER_FORMS = ("SC TO-T", "SC TO-T/A")
US_MA_PARTY_FORMS = ("425", "S-4", "S-4/A", "F-4", "F-4/A", "SC TO-T", "SC TO-T/A")
US_FINANCING_FORMS = ("424B1", "424B4")             # priced underwritten offerings (not ATM shelf supplements)
OFFEREE_WINDOW_DAYS = 270
EVENT_WINDOW_DAYS = 60
STORE_MAX_AGE_DAYS = 4                               # a store older than this reads SOURCE_FAILED (sourceable venues)

# ── UK headline classification (REVIEW-only signals; the Panel table is the offeree authority) ─────────
UK_OFFER_HEADLINE = re.compile(r"rule 2\.7|rule 2\.4|firm offer|possible offer|recommended (?:final )?(?:cash )?"
                               r"(?:offer|acquisition)|scheme of arrangement|form 8\.3|form 8\.5|offer for ",
                               re.I)
UK_ACTION_HEADLINE = re.compile(r"acquisition|disposal|placing|fundrais|rights issue|open offer|"
                                r"special dividend|return of capital|capital raise|merger|demerger|"
                                r"retail offer|subscription", re.I)
UK_ACTION_EXCLUDE = re.compile(r"transaction in own shares|director|pdmr|total voting rights|block listing|"
                               r"holding\(s\) in company|form 8\.3|form 8\.5", re.I)

# ── price-pinning detector (weekly store; calibrated 18-Oct-2026 on 212 names) ────────────────────────
PIN_WEEKS = 8
PIN_STD_PCT = 1.15        # 8-week weekly log-return std below this (pct) ...
PIN_MAX_ABS_PCT = 3.5     # ... and no weekly move larger than this


def _flag(name: str, default: bool = True) -> bool:
    try:
        import isa_policy as _p
        return bool((getattr(_p, "V2_FLAGS", {}) or {}).get(name, default))
    except Exception:                                                   # noqa: BLE001
        return default


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _d(x) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _sha(b: bytes) -> str:
    return hashlib.sha256(b or b"").hexdigest()


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 1. PARSERS (pure, testable on retained fixtures)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
_PANEL_DT = re.compile(r"(\d{1,2}:\d{2})\s+(\d{1,2}-[A-Za-z]{3}-\d{4})")


def _panel_date(s) -> Optional[str]:
    m = _PANEL_DT.search(str(s or ""))
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(2), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def parse_panel_xml(data: bytes) -> dict:
    """Takeover Panel disclosuretable.xml -> {table_date, final, cases:{ISIN: case}}. The CURRENT table is
    `maintable` plus `additions`; `deletions` leave it (Rule 2.8 statements, lapses, completions). Raises
    ValueError on a document that is not the table - a layout change must fail loudly, never read as 'no
    offers' (R2.10)."""
    root = ET.fromstring(data)
    if root.tag != "disclosure_table":
        raise ValueError("not a Takeover Panel disclosure_table document (root %r)" % root.tag)
    secs = {c.tag: c for c in root}
    if "maintable" not in secs:
        raise ValueError("disclosure_table has no <maintable> - layout changed")
    cases, deleted = {}, set()
    for case in (secs.get("deletions") or []):
        o = case.find("offeree")
        for isin in (case.findall(".//ISIN") if o is not None else []):
            deleted.add((isin.text or "").strip())
    for sec in ("maintable", "additions"):
        for case in (secs.get(sec) or []):
            o = case.find("offeree")
            if o is None:
                continue
            offerors = [{"name": f.get("name"), "identified": _panel_date(f.get("offeror_identified")),
                         "rule_26_deadline": f.get("rule_26_deadline")} for f in case.findall("offeror")]
            for line in o.findall(".//twoten_line"):
                isin = (line.findtext("ISIN") or "").strip()
                if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", isin) or isin in deleted:
                    continue
                cases[isin] = {"isin": isin, "caseid": case.get("caseid"), "section": sec,
                               "offeree": o.get("name"), "lei": o.get("lei"),
                               "share_class": (line.findtext("name") or "").strip(),
                               "offer_period_commenced": _panel_date(o.get("offer_period_commenced")),
                               "offer_period_commenced_raw": o.get("offer_period_commenced"),
                               "offerors": offerors}
    return {"table_date": root.get("Date"), "final": root.get("Final"), "n_cases": len(cases), "cases": cases,
            "n_deleted_today": len(deleted)}


def classify_sec_filings(sub: dict, as_of: str, *, offeree_window_days: int = OFFEREE_WINDOW_DAYS,
                         event_window_days: int = EVENT_WINDOW_DAYS) -> dict:
    """data.sec.gov submissions JSON -> typed offeree / M&A-party / financing evidence. Metadata only."""
    rec = ((sub or {}).get("filings") or {}).get("recent") or {}
    forms, dates = rec.get("form") or [], rec.get("filingDate") or []
    items, accs = rec.get("items") or [], rec.get("accessionNumber") or []
    a = _d(as_of)
    off, party, fin, term, delist = [], [], [], [], []
    for i, f in enumerate(forms):
        fd = _d(dates[i] if i < len(dates) else None)
        if fd is None or a is None or fd > a:
            continue
        age = (a - fd).days
        row = {"form": f, "filed": fd.isoformat(), "accession": accs[i] if i < len(accs) else None}
        it = str(items[i] if i < len(items) else "")
        if f in US_OFFEREE_FORMS and age <= offeree_window_days:
            off.append(row)
        elif f in US_MA_PARTY_FORMS and age <= event_window_days:
            party.append(row)
        elif f in US_FINANCING_FORMS and age <= event_window_days:
            fin.append(row)
        if f.startswith("8-K") and "1.02" in it and age <= offeree_window_days:
            term.append(dict(row, items=it))
        if f.startswith("8-K") and ("2.01" in it) and age <= event_window_days:
            party.append(dict(row, items=it))
        if f.startswith("8-K") and ("3.02" in it) and age <= event_window_days:
            fin.append(dict(row, items=it))
        if f in ("25-NSE", "15-12B", "15-12G") and age <= offeree_window_days:
            delist.append(row)
    latest_off = max((r["filed"] for r in off), default=None)
    lapsed = bool(latest_off and any(t["filed"] >= latest_off for t in term))
    return {"offeree_filings": off, "ma_party_filings": party, "financing_filings": fin,
            "termination_8k_102": term, "delisting_filings": delist,
            "offeree_active": bool(off) and not lapsed, "offeree_lapsed": lapsed}


_IG_ROW = re.compile(r"<tr.*?</tr>", re.S)
_IG_CELL = re.compile(r"<td.*?</td>", re.S)


def parse_investegate(html_text: str) -> List[dict]:
    """Investegate company page -> [{date, time, source, headline}] (latest first). Raises on a page with no
    announcement table so a layout change is a SOURCE failure, not an empty history."""
    import html as _h
    s = re.sub(r"<script.*?</script>", "", html_text or "", flags=re.S)
    out = []
    for r in _IG_ROW.findall(s):
        cells = [_h.unescape(re.sub(r"<.*?>", "", c)).strip() for c in _IG_CELL.findall(r)]
        if len(cells) >= 4 and re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}", cells[0] or ""):
            try:
                d = datetime.datetime.strptime(cells[0], "%d %b %Y").date().isoformat()
            except ValueError:
                continue
            out.append({"date": d, "time": cells[1], "source": cells[2], "headline": cells[3]})
    if not out:
        raise ValueError("no dated announcement rows parsed - Investegate layout changed or page empty")
    return out


def classify_uk_headlines(rows: List[dict], as_of: str, window_days: int = EVENT_WINDOW_DAYS) -> dict:
    a = _d(as_of)
    offer, action = [], []
    for r in rows or []:
        d = _d(r.get("date"))
        if d is None or a is None or d > a or (a - d).days > window_days:
            continue
        h = r.get("headline") or ""
        if UK_OFFER_HEADLINE.search(h):
            offer.append(r)
        elif UK_ACTION_HEADLINE.search(h) and not UK_ACTION_EXCLUDE.search(h):
            action.append(r)
    return {"offer_headlines": offer[:10], "action_headlines": action[:10], "n_rows": len(rows or [])}


def pin_statistic(observations: dict, weeks: int = PIN_WEEKS) -> Optional[dict]:
    obs = sorted((observations or {}).items())
    lv = [o.get("level") for _, o in obs if isinstance(o, dict) and (o.get("level") or 0) > 0]
    if len(lv) < weeks + 1:
        return None
    lv = lv[-(weeks + 1):]
    r = [math.log(lv[i + 1] / lv[i]) for i in range(weeks)]
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / len(r)) * 100.0
    mx = max(abs(x) for x in r) * 100.0
    return {"weeks": weeks, "std_pct": round(sd, 3), "max_abs_pct": round(mx, 3), "last_obs": obs[-1][0],
            "pinned": bool(sd < PIN_STD_PCT and mx < PIN_MAX_ABS_PCT),
            "thresholds": {"std_pct": PIN_STD_PCT, "max_abs_pct": PIN_MAX_ABS_PCT}}


# ── ISA-0817 D2b (Raj 18-Oct-2026): primary sources for SIX / Nasdaq Nordic / Dublin; news corroborator elsewhere ──
CH_VENUES = ("EBS", "VTX", "SWX")
NORDIC_VENUES = ("STO", "HEL", "CPH", "ICE")
IE_VENUES = ("ISE",)
FLAG_NEWS = "news_corporate_event_unconfirmed"
NEWS_WINDOW_DAYS = 60
_LEGAL = {"AG", "SA", "SE", "NV", "AB", "ASA", "OYJ", "SPA", "PLC", "LTD", "INC", "AS", "BV", "PUBL", "REG", "ORD",
          "CORP", "CORPORATION", "LIMITED", "HOLDING", "HOLDINGS", "GROUP", "CO", "THE", "CIE", "DE", "ET", "LA",
          "LE", "DES", "DU", "AND", "OF", "SHS", "CLASS", "SER", "A", "B", "C", "BR", "ADR", "NA", "SPON", "INTL"}
_GENERIC = {"FINANCIERE", "INTERNATIONAL", "TECHNOLOGIES", "TECHNOLOGY", "SYSTEMS", "SISTEMAS", "INDUSTRIES",
            "ENERGY", "ENERGIA", "SERVICES", "GLOBAL", "BANK", "BANCA", "GROUPE", "COMPAGNIE", "SOCIETE", "INDUSTRIAL",
            "INDUSTRIE", "MANUFACTURING", "PHARMA", "PHARMACEUTICALS"}


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c)).upper()


def issuer_key(name) -> dict:
    """Name -> {tokens, core, distinctive}. Distinctive = the longest non-legal, non-generic token (identity for name
    joins where no ISIN exists). Deterministic; the population bridge states every key so a bad one is visible."""
    s = re.sub(r"\s*-\s*(REG|A|B|BR|SHS|ORD|CLASS [A-Z])\b.*$", "", _fold(name))
    toks = [t for t in re.split(r"[^A-Z0-9+]+", s) if t]
    core = [t for t in toks if t not in _LEGAL]
    dist = sorted([t for t in core if t not in _GENERIC], key=lambda x: (-len(x), x))
    return {"tokens": toks, "core": core, "distinctive": (dist[0] if dist else (core[0] if core else None))}


def _has_word(text: str, word: str) -> bool:
    """Word match on folded text; a token of >= 6 characters also matches as a word PREFIX, because OpenFIGI
    names are truncated (CIE FINANCIERE RICHEMO-A -> 'Richemont')."""
    if not word:
        return False
    tail = "" if len(word) >= 6 else r"(?![A-Z0-9])"
    return re.search(r"(?<![A-Z0-9])%s%s" % (re.escape(word), tail), _fold(text)) is not None


def parse_tob_list(html_text: str) -> List[dict]:
    """Swiss Takeover Board /transactions/all -> [{nr, company, year, lines, types, is_offer, is_buyback}].
    Raises on a page with no transaction articles (layout change is a SOURCE failure, R2.10)."""
    import html as _h
    out = []
    for art in re.findall(r'<article class="transaction list-item".*?</article>', html_text or "", re.S):
        nr = re.search(r'/transactions/detail/nr/(\d+)', art)
        h3 = re.search(r"<h3>(.*?)<span>\((\d{4})\)</span>", art, re.S)
        p = re.search(r"<p>(.*?)</p>", art, re.S)
        typ = re.search(r'class="col3 short" data-tooltip="([^"]*)"', art)
        lines = [_h.unescape(re.sub(r"<.*?>", "", x)).strip() for x in re.split(r"<br\s*/?>", p.group(1))] if p else []
        types = _h.unescape(typ.group(1)).replace("Transaktionseigenschaft:", "").split("<br>") if typ else []
        tj = " | ".join(types)
        out.append({"nr": nr.group(1) if nr else None,
                    "company": _h.unescape(re.sub(r"<.*?>", "", h3.group(1))).strip() if h3 else None,
                    "year": int(h3.group(2)) if h3 else None, "lines": [x for x in lines if x], "types": types,
                    "is_offer": ("Angebot" in tj and "Rückkauf" not in tj),
                    "is_buyback": "Rückkauf" in tj})
    if not out:
        raise ValueError("no Swiss Takeover Board transaction articles parsed - layout changed")
    return out


TOB_COMPLETION = re.compile(r"Vollzug|Settlement|Exécution|Endergebnis|Final Result|résultat final|"
                            r"Rückzug|Withdrawal|Retrait|Dekotierung|Delisting", re.I)


def tob_detail_completed(html_text: str) -> bool:
    import html as _h
    s = _h.unescape(re.sub(r"<.*?>", "\n", re.sub(r"<script.*?</script>|<style.*?</style>", "", html_text or "",
                                                   flags=re.S)))
    if "Transaktionseigenschaften" not in s:
        raise ValueError("Swiss Takeover Board detail page layout changed")
    return bool(TOB_COMPLETION.search(s))


def parse_irish_pdf(data: bytes) -> dict:
    """Irish Takeover Panel disclosure-table PDF -> {table_text_sha, cases:{ISIN: case}}. Raises on no 'Offeree:'."""
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    return parse_irish_text(txt)


def parse_irish_text(txt: str) -> dict:
    if "Disclosure Table" not in (txt or ""):
        raise ValueError("not an Irish Takeover Panel disclosure table")
    cases = {}
    for blk in re.split(r"\n(?=Offeree:)", txt):
        m = re.match(r"Offeree:\s*(.+)", blk)
        if not m:
            continue
        com = re.search(r"Offer period commenced:\s*(\d{1,2} \w+ \d{4})", blk)
        for isin in re.findall(r"ISIN:\s*([A-Z]{2}[A-Z0-9]{10})", blk):
            d = None
            if com:
                try:
                    d = datetime.datetime.strptime(com.group(1), "%d %B %Y").date().isoformat()
                except ValueError:
                    d = None
            cases[isin] = {"isin": isin, "offeree": m.group(1).strip(), "offer_period_commenced": d,
                           "offerors": re.findall(r"Offeror:\s*(.+)", blk)}
    return {"n_cases": len(cases), "cases": cases}


NORDIC_OFFER = re.compile(r"public (cash |takeover |tender )?offer|tender offer|offer to the shareholders|"
                          r"takeover bid|offentligt uppköpserbjudande|julkinen ostotarjous", re.I)
NORDIC_TARGET = re.compile(r"(shares (in|of)|for all (the )?shares|statement (by|of) the board|board of directors "
                           r"(of [^,]+ )?(unanimously )?recommends|recommends (the )?shareholders to accept)", re.I)
NORDIC_ACTION = re.compile(r"acqui|divest|disposal|directed (share )?issue|rights (issue|offering)|new share issue|"
                           r"extraordinary dividend|special dividend|merger|demerger|spin-?off", re.I)


def _offer_targets(headline: str, me: dict) -> bool:
    """The release is about an offer FOR this issuer's shares (not one it makes): the issuer's distinctive token
    follows 'shares in/of', or the issuer's board makes the statement/recommendation."""
    h = _fold(headline)
    k = re.escape(me.get("distinctive") or "\x00")
    return bool(re.search(r"(SHARES|AKTIER|AKTIERNA|OSAKKEET) (IN|OF|I) [^.;:]{0,40}%s" % k, h)
                or re.search(r"(STATEMENT|UTTALANDE) (BY|OF|FRAN) THE BOARD OF DIRECTORS OF [^.;:]{0,40}%s" % k, h)
                or re.search(r"BOARD OF DIRECTORS OF [^.;:]{0,40}%s[^.;:]{0,40}RECOMMENDS" % k, h)
                or re.search(r"OFFER (FOR|TO THE SHAREHOLDERS OF|TO THE SHAREHOLDERS IN) [^.;:]{0,40}%s" % k, h))


def classify_nordic(items: List[dict], name: str, as_of: str, *, offer_window_days: int = OFFEREE_WINDOW_DAYS,
                    event_window_days: int = EVENT_WINDOW_DAYS) -> dict:
    """Nasdaq Nordic issuer releases (company field == this issuer, folded) -> offeree / action evidence."""
    a = _d(as_of)
    me = issuer_key(name)
    mine, off, act = [], [], []
    for x in items or []:
        if issuer_key(x.get("company")).get("core") != me.get("core"):
            continue
        d = _d(x.get("releaseTime") or x.get("published"))
        if d is None or a is None or d > a:
            continue
        age = (a - d).days
        row = {"date": d.isoformat(), "category": x.get("cnsCategory"), "headline": x.get("headline"),
               "url": x.get("messageUrl"), "id": x.get("disclosureId")}
        mine.append(row)
        h = x.get("headline") or ""
        offerish = NORDIC_OFFER.search(h) or x.get("cnsCategory") == "Tender offer"
        if offerish and age <= offer_window_days and _offer_targets(h, me):
            off.append(row)
        elif (offerish or NORDIC_ACTION.search(h)) and age <= event_window_days:
            act.append(row)
    return {"n_releases": len(mine), "offeree_releases": off[:6], "action_releases": act[:6]}


# ⚑ Measured 18-Oct-2026 on the 27 venues: with 'acquire/acquisition' in the term set 19 of 27 names flagged, mostly
#   routine bolt-ons and aggregator lists - REVIEW on that is friction without information. The SECONDARY detector is
#   therefore scoped to events that change the PRICE BASIS of the issuer's own shares: offers / bids / mergers /
#   take-privates / issues (the primary sources still carry acquirer-side D3 events where they exist).
NEWS_EVENT = re.compile(r"takeover|tender offer|public offer|cash offer|\bbids? for\b|\bbid by\b|\bbid from\b|"
                        r"higher bid|rival bid|buyout|take[- ]private|\bmerger\b|to merge|rights issue|capital raise|"
                        r"special dividend", re.I)
NEWS_NOISE = re.compile(r"social media takeover|instagram takeover|f1\b|formula 1|grand prix|^list of \d+|"
                        r"talent acquisition|britannica|tracxn|dealroom|crunchbase", re.I)


def parse_news_rss(xml_bytes) -> List[dict]:
    import html as _h
    t = xml_bytes.decode("utf-8", "replace") if isinstance(xml_bytes, (bytes, bytearray)) else str(xml_bytes)
    if "<rss" not in t[:500]:
        raise ValueError("not an RSS document")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", t, re.S):
        ti = re.search(r"<title>(.*?)</title>", it, re.S)
        pd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        lk = re.search(r"<link>(.*?)</link>", it, re.S)
        src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        d = None
        if pd:
            try:
                d = datetime.datetime.strptime(pd.group(1).strip()[:16], "%a, %d %b %Y").date().isoformat()
            except ValueError:
                d = None
        out.append({"title": _h.unescape(ti.group(1)).strip() if ti else "", "date": d,
                    "link": lk.group(1).strip() if lk else None, "source": _h.unescape(src.group(1)) if src else None})
    return out


def _proper_noun(title: str, key: str) -> bool:
    """The issuer token appears CAPITALISED (Title or UPPER case) - 'file reply' / 'frontline community' are words,
    not the issuer (measured 18-Oct-2026: Reply, Frontline)."""
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z0-9+]+)", title or ""):
        w = m.group(1)
        if _fold(w).startswith(key) and (w[:1].isupper() or w[:1].isdigit()):
            return True
    return False


def classify_news(rows: List[dict], name: str, as_of: str, window_days: int = NEWS_WINDOW_DAYS) -> dict:
    """SECONDARY corroborator (R6.3: never HARD): headlines that name the issuer's distinctive token AND an
    offer/corporate-action term inside the window."""
    a = _d(as_of)
    key = issuer_key(name).get("distinctive")
    hits = []
    for r in rows or []:
        d = _d(r.get("date"))
        if d is None or a is None or d > a or (a - d).days > window_days:
            continue
        t = r.get("title") or ""
        if _has_word(t, key) and _proper_noun(t, key) and NEWS_EVENT.search(t) and not NEWS_NOISE.search(t):
            hits.append(r)
    return {"key": key, "n_rows": len(rows or []), "hits": hits[:6]}


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 2. THE VERDICT (pure)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
def venue_class(venue) -> str:
    v = str(venue or "").upper()
    if v in UK_VENUES:
        return "UK"
    if v in US_VENUES:
        return "US"
    if v in CH_VENUES:
        return "CH"
    if v in NORDIC_VENUES:
        return "NORDIC"
    if v in IE_VENUES:
        return "IE"
    return "UNSOURCED"


def decide(symbol: str, *, venue, figi=None, panel=None, panel_by_figi=None, sec=None, sec_error=None,
           uk=None, uk_error=None, pin=None, held=False, as_of=None, instrument_type=None,
           ch=None, ch_error=None, nordic=None, nordic_error=None, ie_by_figi=None, ie_doc=None,
           news=None, news_error=None) -> dict:
    """One typed verdict for one symbol. `panel`/`panel_by_figi` None = the table could not be read."""
    vc = venue_class(venue)
    flags, evidence, confirm, why = [], [], [], []
    state = NO_EVENT_FOUND
    if instrument_type in NOT_APPLICABLE_TYPES:
        return {"symbol": symbol, "venue": venue, "venue_class": vc, "figi": figi, "state": NOT_APPLICABLE,
                "flags": [], "confirm_required": [], "evidence": [], "held": bool(held), "held_note": None,
                "why": ["instrument type %s - a fund/ETP has no offer period" % instrument_type],
                "as_of": as_of, "may_sell_on_this_evidence": False}
    if vc == "UK":
        if panel_by_figi is None:
            state = SOURCE_FAILED
            why.append("Takeover Panel table unreadable this run")
        elif figi and figi in panel_by_figi:
            c = panel_by_figi[figi]
            state = OFFEREE_IN_OFFER_PERIOD
            evidence.append({"source": "UK_TAKEOVER_PANEL", "isin": c.get("isin"), "offeree": c.get("offeree"),
                             "offer_period_commenced": c.get("offer_period_commenced"),
                             "offerors": [o.get("name") for o in c.get("offerors") or []],
                             "table_date": (panel or {}).get("table_date")})
            why.append("ISIN %s offeree in the Panel table since %s" % (c.get("isin"),
                                                                        c.get("offer_period_commenced")))
        elif not figi:
            why.append("no FIGI on record - the Panel ISIN join cannot be made; RNS backstop only")
        if uk_error and state != OFFEREE_IN_OFFER_PERIOD:
            if state == NO_EVENT_FOUND and not figi:
                state = SOURCE_FAILED
            why.append("RNS headlines unreadable: %s" % uk_error)
        if uk and state not in (OFFEREE_IN_OFFER_PERIOD,):
            if uk.get("offer_headlines"):
                state = CORPORATE_ACTION_PENDING
                evidence.append({"source": "UK_RNS_HEADLINES", "offer_headlines": uk["offer_headlines"][:5]})
                why.append("offer-related headline(s) without a Panel-table match - REVIEW (non-Code or stale)")
            if uk.get("action_headlines"):
                if state == NO_EVENT_FOUND:
                    state = CORPORATE_ACTION_PENDING
                evidence.append({"source": "UK_RNS_HEADLINES", "action_headlines": uk["action_headlines"][:5]})
                why.append("corporate-action headline(s) in the last %d days" % EVENT_WINDOW_DAYS)
    elif vc == "US":
        if sec is None:
            state = SOURCE_FAILED
            why.append("EDGAR submissions unreadable: %s" % (sec_error or "no CIK / not fetched"))
        else:
            if sec.get("offeree_active"):
                state = OFFEREE_IN_OFFER_PERIOD
                evidence.append({"source": "SEC_SUBMISSIONS", "offeree_filings": sec["offeree_filings"][:5]})
                why.append("subject-company tender/merger filing(s) in the last %d days" % OFFEREE_WINDOW_DAYS)
            elif sec.get("ma_party_filings") or sec.get("financing_filings") or sec.get("offeree_lapsed"):
                state = CORPORATE_ACTION_PENDING
                evidence.append({"source": "SEC_SUBMISSIONS",
                                 "ma_party_filings": sec.get("ma_party_filings")[:5],
                                 "financing_filings": sec.get("financing_filings")[:5],
                                 "offeree_lapsed": sec.get("offeree_lapsed")})
                why.append("M&A-party / financing / lapsed-offer filing(s) in the window")
    elif vc == "IE":
        if ie_by_figi is None:
            state = SOURCE_FAILED
            why.append("Irish Takeover Panel disclosure table unreadable this run")
        elif figi and figi in ie_by_figi:
            c = ie_by_figi[figi]
            state = OFFEREE_IN_OFFER_PERIOD
            evidence.append({"source": "IE_TAKEOVER_PANEL", "isin": c.get("isin"), "offeree": c.get("offeree"),
                             "offer_period_commenced": c.get("offer_period_commenced"),
                             "offerors": c.get("offerors"), "table": (ie_doc or {}).get("url")})
            why.append("ISIN %s offeree in the Irish Panel table" % c.get("isin"))
        elif not figi:
            state = SOURCE_FAILED
            why.append("no FIGI on record - the Irish Panel ISIN join cannot be made")
    elif vc == "CH":
        if ch is None:
            state = SOURCE_FAILED
            why.append("Swiss Takeover Board transactions unreadable: %s" % (ch_error or "not fetched"))
        else:
            if ch.get("offeree"):
                state = OFFEREE_IN_OFFER_PERIOD
                evidence.append({"source": "CH_TAKEOVER_BOARD", "offeree_transactions": ch["offeree"][:4]})
                why.append("public offer for this issuer at the Swiss Takeover Board, not completed or withdrawn")
            elif ch.get("action"):
                state = CORPORATE_ACTION_PENDING
                evidence.append({"source": "CH_TAKEOVER_BOARD", "action_transactions": ch["action"][:4]})
                why.append("Swiss Takeover Board transaction naming this issuer as offeror / a completed recent "
                           "offer - REVIEW + case refresh (D3)")
    elif vc == "NORDIC":
        if nordic is None:
            state = SOURCE_FAILED
            why.append("Nasdaq Nordic issuer releases unreadable: %s" % (nordic_error or "not fetched"))
        else:
            if nordic.get("offeree_releases"):
                state = OFFEREE_IN_OFFER_PERIOD
                evidence.append({"source": "NASDAQ_NORDIC_RELEASES", "offeree_releases": nordic["offeree_releases"][:4]})
                why.append("issuer regulatory release(s) on a public offer for its shares")
            elif nordic.get("action_releases"):
                state = CORPORATE_ACTION_PENDING
                evidence.append({"source": "NASDAQ_NORDIC_RELEASES", "action_releases": nordic["action_releases"][:4]})
                why.append("issuer regulatory release(s) on an offer / acquisition / issue / distribution - REVIEW (D3)")
    else:
        state = OFFER_STATE_UNSOURCED
        confirm.append(CONFIRM_OFFER_STATE)
        why.append("venue %s has no machine-readable primary offer register - CONFIRM AT DEALING (Raj D2)" % venue)
        if news is not None and news.get("hits"):
            flags.append(FLAG_NEWS)
            evidence.append({"source": "NEWS_SEARCH", "key": news.get("key"), "hits": news["hits"][:4]})
            why.append("SECONDARY news search: offer/corporate-action headline(s) naming the issuer - REVIEW + case "
                       "refresh, never HARD (Raj D2b, R6.3)")
        elif news_error:
            why.append("news corroborator unreadable (%s) - the venue stays CONFIRM AT DEALING" % news_error)
    if pin and pin.get("pinned") and vc == "UNSOURCED":
        flags.append(FLAG_PINNED)
        evidence.append({"source": "WEEKLY_STORE", "pin": pin})
        why.append("price pinned: %d-week std %.2f%% < %.2f%%" % (pin["weeks"], pin["std_pct"], PIN_STD_PCT))
    if state == OFFEREE_IN_OFFER_PERIOD:
        flags.insert(0, FLAG_OFFEREE)
    elif state == CORPORATE_ACTION_PENDING:
        flags.insert(0, FLAG_CORP_ACTION)
    elif state == SOURCE_FAILED:
        flags.insert(0, FLAG_SOURCE_FAILED)
    held_note = ("HELD: never auto-sold on this evidence - REVIEW (hold to completion vs sell is Raj's "
                 "decision)" if held and flags else None)
    return {"symbol": symbol, "venue": venue, "venue_class": vc, "figi": figi, "state": state,
            "flags": flags, "confirm_required": confirm, "evidence": evidence, "why": why,
            "held": bool(held), "held_note": held_note, "as_of": as_of,
            "may_sell_on_this_evidence": False}


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 3. NETWORK (injected; default urllib)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
def _get(url: str, *, headers=None, timeout=30) -> bytes:
    import urllib.request
    h = {"User-Agent": "Mozilla/5.0 (ISA research)"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _post_json(url: str, body) -> object:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _isin_to_figi(isins, post, cache: dict, spacing_s=2.6, t_end=None) -> Dict[str, Optional[str]]:
    out, todo = {}, []
    for i in isins:
        if i in cache:
            out[i] = cache[i]
        else:
            todo.append(i)
    for k in range(0, len(todo), 10):
        if t_end and time.time() > t_end:
            break
        chunk = todo[k:k + 10]
        try:
            res = post(SOURCES["OPENFIGI_ISIN"]["url"], [{"idType": "ID_ISIN", "idValue": i} for i in chunk])
        except Exception:                                               # noqa: BLE001
            res = None
        for j, i in enumerate(chunk):
            data = ((res[j] or {}).get("data") if isinstance(res, list) and j < len(res) else None) or []
            figis = sorted({x.get("figi") for x in data if x.get("figi")})
            if res is not None:
                cache[i] = figis
            out[i] = figis
        if spacing_s:
            time.sleep(spacing_s)
    return out


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 4. THE PRODUCER (pre-run Step 5x-ce)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
def population(root: str) -> Dict[str, dict]:
    """Every verified symbol (with venue + FIGI from instrument_eligibility) plus held stocks."""
    smap = _read(os.path.join(root, SYMBOL_MAP_FILE), {}) or {}
    ie = (_read(os.path.join(root, ELIGIBILITY_FILE), {}) or {}).get("records") or {}
    pop = {}
    for v in smap.get("verified") or []:
        if isinstance(v, dict) and v.get("ticker"):
            t = v["ticker"]
            r = ie.get(t) or {}
            pop[t] = {"venue": v.get("exchange") or r.get("venue"),
                      "figi": (r.get("openfigi") or {}).get("figi"), "held": False,
                      "instrument_type": (r.get("openfigi") or {}).get("securityType2"),
                      "name": (r.get("openfigi") or {}).get("name")}
    for t, r in ie.items():
        pop.setdefault(t, {"venue": r.get("venue"), "figi": (r.get("openfigi") or {}).get("figi"), "held": False,
                           "instrument_type": (r.get("openfigi") or {}).get("securityType2"),
                           "name": (r.get("openfigi") or {}).get("name")})
    return pop


_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                     "nov", "dec"), 1)}


def _month_key(path: str):
    m = re.search(r"_([a-z]{3})_(\d{4})\.json$", os.path.basename(path))
    return (int(m.group(2)), _MON.get(m.group(1), 0)) if m else (0, 0)


def _held(root: str) -> set:
    """Held stocks from the NEWEST month's broker book, resolved archive-aware (ISA-0832 month_artefacts)."""
    import month_artefacts as _ma
    ps = sorted(_ma.month_glob(root, "portfolio_data_*_20[0-9][0-9].json"), key=_month_key)
    if not ps:
        return set()
    doc = _read(ps[-1], {}) or {}
    return {str(s.get("ticker")).upper() for s in (doc.get("stocks") or []) if s.get("ticker")}


CACHE_FILE = "corporate_events_cache.json"
REFETCH_HOURS = 18          # a per-issuer document fetched within this window is reused (the pre-run re-run protocol)
CHECKPOINT_EVERY = 15       # the fetch cache is persisted every N documents, so a cut-off call loses at most N


def _hours_since(ts: Optional[str]) -> Optional[float]:
    try:
        t = datetime.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600.0


def _save_cache(root: str, cache: dict) -> None:
    p = os.path.join(root, CACHE_FILE)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(cache, fh, default=str)
    os.replace(p + ".tmp", p)


def refresh(root: Optional[str] = None, today: Optional[str] = None, *, get: Optional[Callable] = None,
            post: Optional[Callable] = None, budget_s: float = 45.0, sec_spacing_s: float = 0.12,
            uk_spacing_s: float = 0.5, dry_run: bool = False, write_store: bool = True,
            refetch_hours: float = REFETCH_HOURS) -> dict:
    """THE STEP 5x-ce PRODUCER. INCREMENTAL and TIME-BOXED (the pre-run re-runs every stage inside one host call,
    ISA-0780): every source document carries its retrieval time in `corporate_events_cache.json`; a document fetched
    within `refetch_hours` is reused, the rest are fetched until the budget ends, and the cache is checkpointed every
    CHECKPOINT_EVERY documents so a cut-off call never loses the work. A name whose document is neither fresh nor
    fetched this call reads SOURCE_FAILED (REVIEW) - never 'no event' - and the next call resumes it."""
    root = root or HERE
    today = today or _today()
    get = get or _get
    post = post or _post_json
    t_end = time.time() + float(budget_s)
    cache = _read(os.path.join(root, CACHE_FILE), {}) or {}
    cache.setdefault("panel", None)
    cache.setdefault("isin_figi", {})
    cache.setdefault("cik", {})
    cache.setdefault("sec", {})
    cache.setdefault("uk", {})
    pop = population(root)
    held = _held(root)
    for t in held:
        pop.setdefault(t, {"venue": None, "figi": None, "held": True})
        pop[t]["held"] = True
    errors, fetch_log = [], []
    do_fetch = _flag("corporate_events_refresh") and not dry_run
    weekly = ((_read(os.path.join(root, WEEKLY_STORE_FILE), {}) or {}).get("names") or {})
    n_since = [0]

    def _tick():
        n_since[0] += 1
        if n_since[0] >= CHECKPOINT_EVERY and not dry_run:
            _save_cache(root, cache)
            n_since[0] = 0

    def _fresh(entry) -> bool:
        h = _hours_since((entry or {}).get("retrieved_at"))
        return h is not None and h <= refetch_hours

    # UK — the Panel table (one document) and its ISIN->FIGI join
    if do_fetch and not _fresh(cache.get("panel")):
        try:
            raw = get(SOURCES["UK_TAKEOVER_PANEL"]["url"])
            pnl = parse_panel_xml(raw)
            cache["panel"] = {"retrieved_at": _now(), "sha256": _sha(raw), "bytes": len(raw), "doc": pnl}
            fetch_log.append({"source": "UK_TAKEOVER_PANEL", "retrieved_at": cache["panel"]["retrieved_at"],
                              "sha256": _sha(raw), "bytes": len(raw), "table_date": pnl["table_date"],
                              "n_cases": pnl["n_cases"]})
            _tick()
        except Exception as exc:                                        # noqa: BLE001
            errors.append("UK_TAKEOVER_PANEL unreadable: %s: %s" % (type(exc).__name__, exc))
            cache["panel_error"] = {"at": _now(), "why": "%s: %s" % (type(exc).__name__, exc)}
    panel, panel_by_figi = None, None
    if _fresh(cache.get("panel")) or (cache.get("panel") and not do_fetch):
        panel = cache["panel"]["doc"]
        isins = sorted(panel["cases"])
        need = [i for i in isins if i not in cache["isin_figi"]]
        if need and do_fetch:
            _isin_to_figi(need, post, cache["isin_figi"], t_end=t_end)
        panel_by_figi = {}
        unmapped = []
        for isin in isins:
            figis = cache["isin_figi"].get(isin)
            if not figis:
                unmapped.append(isin)
            for f in figis or []:
                panel_by_figi[f] = panel["cases"][isin]
        if unmapped:
            errors.append("Panel ISINs without a FIGI (named, not matched): %s" % ", ".join(unmapped[:20]))

    # US — ticker->CIK (weekly) then per-issuer submissions (incremental)
    us = [t for t, p in sorted(pop.items()) if venue_class(p.get("venue")) == "US"
          and p.get("instrument_type") not in NOT_APPLICABLE_TYPES]
    cikm = cache.get("cik") or {}
    if do_fetch and us and (_hours_since(cikm.get("_retrieved_at")) or 1e9) > 24 * 7:
        try:
            raw = get(SOURCES["SEC_TICKERS"]["url"], headers={"User-Agent": SEC_UA})
            m = json.loads(raw)
            allm = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in m.values()}
            cikm = {t: allm.get(t.upper()) or allm.get(t.upper().replace("-", ".")) for t in us}
            cikm["_retrieved_at"] = _now()
            cikm["_sha256"] = _sha(raw)
            cache["cik"] = cikm
            fetch_log.append({"source": "SEC_TICKERS", "retrieved_at": cikm["_retrieved_at"], "sha256": _sha(raw),
                              "n": len(allm)})
            _tick()
        except Exception as exc:                                        # noqa: BLE001
            errors.append("SEC_TICKERS unreadable (cached CIKs used): %s: %s" % (type(exc).__name__, exc))
    sec_by, sec_err = {}, {}
    for t in us:
        ent = cache["sec"].get(t)
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            cik = cikm.get(t)
            if not cik:
                ent = {"retrieved_at": _now(), "error": "no CIK for ticker"}
            else:
                try:
                    raw = get(SOURCES["SEC_SUBMISSIONS"]["url"].format(cik=cik), headers={"User-Agent": SEC_UA})
                    cls = classify_sec_filings(json.loads(raw), today)
                    cls["cik"] = cik
                    ent = {"retrieved_at": _now(), "sha256": _sha(raw), "classification": cls,
                           "classified_as_of": today}
                except Exception as exc:                                # noqa: BLE001
                    ent = dict(cache["sec"].get(t) or {}, last_error="%s: %s" % (type(exc).__name__, exc),
                               last_error_at=_now())
                if sec_spacing_s:
                    time.sleep(sec_spacing_s)
            cache["sec"][t] = ent
            _tick()
        if _fresh(ent) and ent.get("classification") is not None:
            sec_by[t] = ent["classification"]
        else:
            sec_err[t] = ((ent or {}).get("error") or (ent or {}).get("last_error")
                          or ("not fetched this call (time box) and no document within %gh" % refetch_hours))
    if sec_err:
        errors.append("EDGAR not current for %d US names (SOURCE_FAILED, resumes next call): %s"
                      % (len(sec_err), ", ".join(sorted(sec_err)[:15])))

    # UK — RNS headlines per issuer (incremental)
    uk_by, uk_err = {}, {}
    ukn = [t for t, p in sorted(pop.items()) if venue_class(p.get("venue")) == "UK"
           and p.get("instrument_type") not in NOT_APPLICABLE_TYPES]
    for t in ukn:
        ent = cache["uk"].get(t)
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            try:
                raw = get(SOURCES["UK_RNS_HEADLINES"]["url"].format(epic=t.split(".")[0]))
                rows = parse_investegate(raw.decode("utf-8", "replace"))
                ent = {"retrieved_at": _now(), "sha256": _sha(raw), "rows": rows[:60]}
            except Exception as exc:                                    # noqa: BLE001
                ent = dict(cache["uk"].get(t) or {}, last_error="%s: %s" % (type(exc).__name__, exc),
                           last_error_at=_now())
            if uk_spacing_s:
                time.sleep(uk_spacing_s)
            cache["uk"][t] = ent
            _tick()
        if _fresh(ent) and ent.get("rows") is not None:
            uk_by[t] = classify_uk_headlines(ent["rows"], today)
        else:
            uk_err[t] = ((ent or {}).get("last_error")
                         or "not fetched this call (time box) and no page within %gh" % refetch_hours)
    if uk_err:
        errors.append("RNS headlines not current for %d UK names: %s" % (len(uk_err), ", ".join(sorted(uk_err)[:15])))
    # ── D2b (Raj 18-Oct-2026): Swiss Takeover Board / Nasdaq Nordic / Irish Panel / news corroborator ─────
    for _k in ("ch", "nordic", "news", "ch_detail"):
        cache.setdefault(_k, {})
    names = {t: ((p.get("name") or "")) for t, p in pop.items()}
    _ok_types = lambda p: p.get("instrument_type") not in NOT_APPLICABLE_TYPES        # noqa: E731
    # Swiss: one list document, detail pages only for issuer matches
    ch_by, ch_err = {}, {}
    chn = [t for t, p in sorted(pop.items()) if venue_class(p.get("venue")) == "CH" and _ok_types(p)]
    if chn:
        ent = cache["ch"].get("_list")
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            try:
                raw = get(SOURCES["CH_TAKEOVER_BOARD"]["url"])
                ent = {"retrieved_at": _now(), "sha256": _sha(raw),
                       "rows": parse_tob_list(raw.decode("utf-8", "replace"))}
            except Exception as exc:                                    # noqa: BLE001
                ent = dict(ent or {}, last_error="%s: %s" % (type(exc).__name__, exc))
            cache["ch"]["_list"] = ent
            _tick()
        if _fresh(ent) and ent.get("rows") is not None:
            yr = int(today[:4])
            for t in chn:
                k = issuer_key(names.get(t)).get("distinctive")
                offs, acts, err = [], [], None
                if not k:
                    ch_err[t] = "no issuer name on record - the name join cannot be made"
                    continue
                for r in ent["rows"]:
                    if not r.get("is_offer") or (r.get("year") or 0) < yr - 1:
                        continue
                    as_target = _has_word(r.get("company") or "", k)
                    as_bidder = any(_has_word(l, k) for l in r.get("lines") or [] if l.startswith("Anbieter"))
                    if not (as_target or as_bidder):
                        continue
                    if as_target:
                        de = cache["ch_detail"].get(r["nr"])
                        if not _fresh(de) and do_fetch and time.time() < t_end:
                            try:
                                rawd = get(SOURCES["CH_TAKEOVER_BOARD"]["detail"].format(nr=r["nr"]))
                                de = {"retrieved_at": _now(), "sha256": _sha(rawd),
                                      "completed": tob_detail_completed(rawd.decode("utf-8", "replace"))}
                            except Exception as exc:                    # noqa: BLE001
                                de = dict(de or {}, last_error="%s: %s" % (type(exc).__name__, exc))
                            cache["ch_detail"][r["nr"]] = de
                            _tick()
                        if not (_fresh(de) and "completed" in de):
                            err = "detail %s not current" % r["nr"]
                        elif de["completed"]:
                            acts.append(dict(r, completed=True))
                        else:
                            offs.append(dict(r, completed=False))
                    else:
                        acts.append(dict(r, role="offeror"))
                if err and not offs:
                    ch_err[t] = err
                else:
                    ch_by[t] = {"offeree": offs, "action": acts, "key": k}
        else:
            for t in chn:
                ch_err[t] = (ent or {}).get("last_error") or "transaction list not current"
    # Nordic: one releases query per issuer
    nordic_by, nordic_err = {}, {}
    frm = (datetime.date.fromisoformat(today) - datetime.timedelta(days=OFFEREE_WINDOW_DAYS)).isoformat()
    for t in [t for t, p in sorted(pop.items()) if venue_class(p.get("venue")) == "NORDIC" and _ok_types(p)]:
        ent = cache["nordic"].get(t)
        k = issuer_key(names.get(t))
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            try:
                import urllib.parse as _up
                raw = get(SOURCES["NASDAQ_NORDIC_RELEASES"]["url"].format(
                    q=_up.quote(" ".join(k["core"][:2])), frm=frm, to=today))
                items = ((json.loads(raw).get("results") or {}).get("item")) or []
                ent = {"retrieved_at": _now(), "sha256": _sha(raw), "items": [
                    {f: x.get(f) for f in ("company", "cnsCategory", "headline", "releaseTime", "published",
                                           "messageUrl", "disclosureId")} for x in items]}
            except Exception as exc:                                    # noqa: BLE001
                ent = dict(ent or {}, last_error="%s: %s" % (type(exc).__name__, exc))
            if uk_spacing_s:
                time.sleep(uk_spacing_s)
            cache["nordic"][t] = ent
            _tick()
        if not k.get("distinctive"):
            nordic_err[t] = "no issuer name on record - the company join cannot be made"
        elif _fresh(ent) and ent.get("items") is not None:
            nordic_by[t] = classify_nordic(ent["items"], names.get(t), today)
        else:
            nordic_err[t] = (ent or {}).get("last_error") or "releases not current"
    # Ireland: page -> newest PDF -> ISIN cases -> FIGI join
    ie_by_figi, ie_doc = None, None
    if any(venue_class(p.get("venue")) == "IE" for p in pop.values()):
        ent = cache.get("ie")
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            try:
                page = get(SOURCES["IE_TAKEOVER_PANEL"]["url"]).decode("utf-8", "replace")
                pdfs = sorted(set(re.findall(r"https://irishtakeoverpanel\.ie/wp-content/uploads/\d{4}/\d{2}/"
                                             r"Website-Disclosure-Table-[^\"' ]+?\.pdf", page)))
                if not pdfs:
                    raise ValueError("no disclosure-table PDF link on the page")

                def _pdate(u):
                    m = re.search(r"Table-(\d{1,2})-([A-Za-z]+)-(\d{4})", u)
                    try:
                        return datetime.datetime.strptime(" ".join(m.groups()), "%d %B %Y").date() if m else datetime.date.min
                    except ValueError:
                        return datetime.date.min
                url = max(pdfs, key=_pdate)
                raw = get(url)
                doc_ie = parse_irish_pdf(raw)
                ent = {"retrieved_at": _now(), "sha256": _sha(raw), "url": url, "table_date": _pdate(url).isoformat(),
                       "doc": doc_ie}
            except Exception as exc:                                    # noqa: BLE001
                ent = dict(ent or {}, last_error="%s: %s" % (type(exc).__name__, exc))
                errors.append("IE_TAKEOVER_PANEL unreadable: %s" % ent["last_error"])
            cache["ie"] = ent
            _tick()
        if _fresh(ent) and ent.get("doc"):
            ie_doc = {"url": ent.get("url"), "table_date": ent.get("table_date")}
            need = [i for i in ent["doc"]["cases"] if i not in cache["isin_figi"]]
            if need and do_fetch:
                _isin_to_figi(need, post, cache["isin_figi"], t_end=t_end)
            ie_by_figi = {}
            for isin, case in ent["doc"]["cases"].items():
                for f in cache["isin_figi"].get(isin) or []:
                    ie_by_figi[f] = case
    # News corroborator for the venues with no primary register (REVIEW only)
    news_by, news_err = {}, {}
    for t in [t for t, p in sorted(pop.items()) if venue_class(p.get("venue")) == "UNSOURCED" and _ok_types(p)]:
        ent = cache["news"].get(t)
        k = issuer_key(names.get(t))
        if not _fresh(ent) and do_fetch and time.time() < t_end:
            try:
                import urllib.parse as _up
                q = '"%s" (takeover OR "tender offer" OR "public offer" OR bid OR merger OR acquisition) when:%dd' % (
                    " ".join(k["core"][:2]).title(), NEWS_WINDOW_DAYS)
                raw = get(SOURCES["NEWS_SEARCH"]["url"].format(q=_up.quote(q)))
                ent = {"retrieved_at": _now(), "sha256": _sha(raw), "rows": parse_news_rss(raw)[:60]}
            except Exception as exc:                                    # noqa: BLE001
                ent = dict(ent or {}, last_error="%s: %s" % (type(exc).__name__, exc))
            if uk_spacing_s:
                time.sleep(uk_spacing_s)
            cache["news"][t] = ent
            _tick()
        if not k.get("distinctive"):
            news_err[t] = "no issuer name on record - the news join cannot be made"
        elif _fresh(ent) and ent.get("rows") is not None:
            news_by[t] = classify_news(ent["rows"], names.get(t), today)
        else:
            news_err[t] = (ent or {}).get("last_error") or "news not current"
    for _lbl, _e in (("Swiss Takeover Board", ch_err), ("Nasdaq Nordic releases", nordic_err)):
        if _e:
            errors.append("%s not current for %d names (SOURCE_FAILED, resumes next call): %s"
                          % (_lbl, len(_e), ", ".join(sorted(_e)[:15])))
    if not dry_run:
        _save_cache(root, cache)

    verdicts = {}
    for t, p in sorted(pop.items()):
        pin = pin_statistic((weekly.get(t) or {}).get("observations"))
        verdicts[t] = decide(t, venue=p.get("venue"), figi=p.get("figi"), panel=panel,
                             panel_by_figi=panel_by_figi, sec=sec_by.get(t), sec_error=sec_err.get(t),
                             uk=uk_by.get(t), uk_error=uk_err.get(t), pin=pin, held=p.get("held"), as_of=today,
                             instrument_type=p.get("instrument_type"), ch=ch_by.get(t), ch_error=ch_err.get(t),
                             nordic=nordic_by.get(t), nordic_error=nordic_err.get(t), ie_by_figi=ie_by_figi,
                             ie_doc=ie_doc, news=news_by.get(t), news_error=news_err.get(t))
    counts = {}
    for v in verdicts.values():
        counts[v["state"]] = counts.get(v["state"], 0) + 1
    _pc = cache.get("panel") or {}
    doc = {"_what": "ISA-0817 GENERATED (R14.3) offer-period / corporate-action state per symbol (Step 5x-ce, "
                    "corporate_events.refresh). Consumed by step9_pre_builder and the capital decision engine. "
                    "Never hand-edited.",
           "schema_version": SCHEMA_VERSION, "as_of": today, "generated_at": _now(),
           "state": ("FETCHED" if do_fetch else "OFFLINE_REEVALUATED") + ("_WITH_ERRORS" if errors else ""),
           "complete": not errors, "sources": SOURCES, "fetch_log": fetch_log, "errors": errors, "counts": counts,
           "n_symbols": len(verdicts),
           "offerees": sorted(t for t, v in verdicts.items() if v["state"] == OFFEREE_IN_OFFER_PERIOD),
           "corporate_action_pending": sorted(t for t, v in verdicts.items() if v["state"] == CORPORATE_ACTION_PENDING),
           "source_failed": sorted(t for t, v in verdicts.items() if v["state"] == SOURCE_FAILED),
           "panel_identity": {k: _pc.get(k) for k in ("retrieved_at", "sha256", "bytes")} |
                             {"table_date": (panel or {}).get("table_date"), "n_cases": (panel or {}).get("n_cases")},
           "document_identity": {"sec": {t: {k: (cache["sec"].get(t) or {}).get(k) for k in ("retrieved_at", "sha256")}
                                         for t in us},
                                 "uk": {t: {k: (cache["uk"].get(t) or {}).get(k) for k in ("retrieved_at", "sha256")}
                                        for t in ukn}},
           "verdicts": verdicts}
    if write_store and not dry_run:
        write(doc, root)
    return doc


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 5. STORE + CONSUMER API (consumers never fetch)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
def write(doc: dict, root: Optional[str] = None) -> str:
    root = root or HERE
    p = os.path.join(root, STORE_FILE)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, default=str)
    os.replace(p + ".tmp", p)
    # learning capture: one append-only row per flagged / changed verdict (R8.4)
    try:
        with open(os.path.join(root, HISTORY_FILE), "a", encoding="utf-8") as fh:
            for t, v in sorted((doc.get("verdicts") or {}).items()):
                if v.get("state") in (NO_EVENT_FOUND, OFFER_STATE_UNSOURCED) and not v.get("flags"):
                    continue
                fh.write(json.dumps({"as_of": doc.get("as_of"), "symbol": t, "state": v.get("state"),
                                     "flags": v.get("flags"), "held": v.get("held"),
                                     "evidence": v.get("evidence")}, default=str) + "\n")
    except OSError:
        pass
    return p


def load(root: Optional[str] = None) -> Optional[dict]:
    return _read(os.path.join(root or HERE, STORE_FILE))


def verdict(symbol: str, doc: Optional[dict], as_of: Optional[str] = None) -> dict:
    """THE consumer function. A missing/aged store on a sourceable venue reads SOURCE_FAILED (REVIEW), never
    'no event'; an unsourced venue reads OFFER_STATE_UNSOURCED (confirm at dealing)."""
    if not _flag("corporate_events"):
        return {"symbol": symbol, "state": DISABLED, "flags": [], "confirm_required": [],
                "why": ["isa_policy.V2_FLAGS['corporate_events'] is False (rollback)"]}
    as_of = as_of or _today()
    if not doc:
        return {"symbol": symbol, "state": SOURCE_FAILED, "flags": [FLAG_SOURCE_FAILED], "confirm_required": [],
                "why": ["corporate_events.json absent - offer state UNESTABLISHED (REVIEW, never 'no event')"]}
    v = (doc.get("verdicts") or {}).get(symbol)
    age = None
    if _d(doc.get("as_of")) and _d(as_of):
        age = (_d(as_of) - _d(doc.get("as_of"))).days
    if v is None:
        return {"symbol": symbol, "state": SOURCE_FAILED, "flags": [FLAG_SOURCE_FAILED], "confirm_required": [],
                "why": ["symbol not in the corporate_events population (as_of %s)" % doc.get("as_of")]}
    v = dict(v)
    if age is not None and age > STORE_MAX_AGE_DAYS and v.get("venue_class") in ("UK", "US") \
            and v.get("state") == NO_EVENT_FOUND:
        v.update(state=SOURCE_FAILED, flags=[FLAG_SOURCE_FAILED] + [f for f in v.get("flags") or []
                                                                     if f != FLAG_SOURCE_FAILED],
                 why=(v.get("why") or []) + ["store is %d days old (> %d) - NO_EVENT_FOUND is not current"
                                             % (age, STORE_MAX_AGE_DAYS)])
    v["store_as_of"] = doc.get("as_of")
    v["store_age_days"] = age
    return v


def section_for_step9(symbols, doc: Optional[dict], as_of: Optional[str] = None) -> dict:
    """The step9_pre `corporate_events` section: one verdict per symbol, plus the store identity."""
    vs = {t: verdict(t, doc, as_of) for t in sorted(set(symbols))}
    counts = {}
    for v in vs.values():
        counts[v["state"]] = counts.get(v["state"], 0) + 1
    return {"verdicts": vs, "counts": counts, "store_as_of": (doc or {}).get("as_of"),
            "store_state": (doc or {}).get("state"), "store_errors": (doc or {}).get("errors"),
            "rule": "ISA-0817 (Raj D1-D3 18-Oct-2026)"}


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 6. SELF-TEST (offline, deterministic)
# ═════════════════════════════════════════════════════════════════════════════════════════════════
_FIX_PANEL = b"""<?xml version="1.0" encoding="utf-8"?>
<disclosure_table Date="16 October 2026" Final="YES"><additions/><deletions>
<case caseid="1627"><offeree name="Gooch &amp; Housego plc" lei="X" offer_period_commenced="07:00 16-Jul-2026">
<twoten_information><twoten_line><name>20p ordinary</name><ISIN>GB0002259116</ISIN><NSI>1</NSI></twoten_line>
</twoten_information></offeree></case></deletions><amendments/><maintable>
<case caseid="1558"><offeree name="Spire Healthcare Group plc" lei="213800IBJPW3SE2RA350"
 offer_period_commenced="19:36 18-Sep-2025"><twoten_information><twoten_line><name>1p ordinary</name>
<ISIN>GB00BNLPYF73</ISIN><NSI>402,759,599</NSI></twoten_line></twoten_information></offeree>
<offeror name="Tulip UK Bidco Limited" rule_26_deadline="N/A" offeror_identified="07:00 14-May-2026"/></case>
<case caseid="1627"><offeree name="Gooch &amp; Housego plc" lei="X" offer_period_commenced="07:00 16-Jul-2026">
<twoten_information><twoten_line><name>20p ordinary</name><ISIN>GB0002259116</ISIN><NSI>1</NSI></twoten_line>
</twoten_information></offeree></case></maintable></disclosure_table>"""


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))

    p = parse_panel_xml(_FIX_PANEL)
    ok("CE1 Panel parse: SPI ISIN present, deleted case excluded",
       "GB00BNLPYF73" in p["cases"] and "GB0002259116" not in p["cases"], p["cases"].keys())
    ok("CE2 offer period date parsed", p["cases"]["GB00BNLPYF73"]["offer_period_commenced"] == "2025-09-18")
    try:
        parse_panel_xml(b"<html><body>moved</body></html>")
        ok("CE3 layout change raises (never 'no offers')", False)
    except ValueError:
        ok("CE3 layout change raises (never 'no offers')", True)
    pbf = {"BBG006S8NKB6": p["cases"]["GB00BNLPYF73"]}
    v = decide("SPI.L", venue="LSE", figi="BBG006S8NKB6", panel=p, panel_by_figi=pbf, uk={}, as_of="2026-10-18")
    ok("CE4 MUST-FIRE SPI.L -> OFFEREE + offer_period_offeree", v["state"] == OFFEREE_IN_OFFER_PERIOD
       and v["flags"][0] == FLAG_OFFEREE, v)
    v = decide("ONT.L", venue="LSE", figi="BBG000OTHER0", panel=p, panel_by_figi=pbf,
               uk={"offer_headlines": [], "action_headlines": []}, as_of="2026-10-18")
    ok("CE5 negative control: UK name not in table -> NO_EVENT_FOUND, no flag", v["state"] == NO_EVENT_FOUND
       and not v["flags"], v)
    v = decide("SPI.L", venue="LSE", figi="BBG006S8NKB6", panel=None, panel_by_figi=None, as_of="2026-10-18")
    ok("CE6 table unreadable -> SOURCE_FAILED (REVIEW), never NO_EVENT_FOUND",
       v["state"] == SOURCE_FAILED and FLAG_SOURCE_FAILED in v["flags"], v)
    sub = {"filings": {"recent": {"form": ["SC 14D9", "8-K", "10-Q"], "filingDate": ["2026-09-01", "2026-08-01",
                                                                                   "2026-08-05"],
                                  "items": ["", "1.01", ""], "accessionNumber": ["a", "b", "c"]}}}
    s = classify_sec_filings(sub, "2026-10-18")
    v = decide("XYZ", venue="NMS", sec=s, as_of="2026-10-18")
    ok("CE7 US SC 14D9 -> OFFEREE", v["state"] == OFFEREE_IN_OFFER_PERIOD and v["flags"] == [FLAG_OFFEREE], v)
    sub["filings"]["recent"]["form"].append("8-K")
    sub["filings"]["recent"]["filingDate"].append("2026-10-01")
    sub["filings"]["recent"]["items"].append("1.02,9.01")
    sub["filings"]["recent"]["accessionNumber"].append("d")
    s = classify_sec_filings(sub, "2026-10-18")
    v = decide("XYZ", venue="NMS", sec=s, as_of="2026-10-18")
    ok("CE8 terminated (8-K 1.02 after) -> lapsed -> CORPORATE_ACTION_PENDING (REVIEW), not HARD",
       v["state"] == CORPORATE_ACTION_PENDING and FLAG_OFFEREE not in v["flags"], v)
    s = classify_sec_filings({"filings": {"recent": {"form": ["10-Q", "SC 14D9"], "filingDate": ["2026-10-01",
                                                                                               "2025-01-01"]}}},
                             "2026-10-18")
    ok("CE9 stale offeree filing outside window ignored", not s["offeree_active"], s)
    v = decide("XYZ", venue="NMS", sec=None, sec_error="HTTP 403", as_of="2026-10-18")
    ok("CE10 EDGAR failure -> SOURCE_FAILED", v["state"] == SOURCE_FAILED, v)
    v = decide("ASML.AS", venue="AMS", as_of="2026-10-18",
               pin={"pinned": True, "weeks": 8, "std_pct": 0.5, "max_abs_pct": 1.0})
    ok("CE11 EU venue -> UNSOURCED + confirm + pin REVIEW", v["state"] == OFFER_STATE_UNSOURCED and
       v["confirm_required"] == [CONFIRM_OFFER_STATE] and v["flags"] == [FLAG_PINNED], v)
    h = classify_uk_headlines([{"date": "2026-10-10", "headline": "Proposed Placing to raise GBP20m"},
                               {"date": "2026-10-11", "headline": "Transaction in Own Shares"},
                               {"date": "2026-10-12", "headline": "Form 8.3 - X plc"}], "2026-10-18")
    ok("CE12 UK headlines: placing = action, buyback excluded, Form 8.3 = offer",
       len(h["action_headlines"]) == 1 and len(h["offer_headlines"]) == 1, h)
    v = decide("ZZZ.L", venue="LSE", figi="F", panel=p, panel_by_figi=pbf, uk=h, as_of="2026-10-18", held=True)
    ok("CE13 held name with events: REVIEW flag, never a SELL", v["flags"] == [FLAG_CORP_ACTION]
       and v["may_sell_on_this_evidence"] is False and v["held_note"], v)
    doc = {"as_of": "2026-10-10", "verdicts": {"MU": {"symbol": "MU", "state": NO_EVENT_FOUND, "flags": [],
                                                      "venue_class": "US"}}}
    ok("CE14 aged store: NO_EVENT_FOUND -> SOURCE_FAILED", verdict("MU", doc, "2026-10-18")["state"] == SOURCE_FAILED)
    ok("CE15 absent store -> SOURCE_FAILED", verdict("MU", None, "2026-10-18")["state"] == SOURCE_FAILED)
    ok("CE16 symbol outside population -> SOURCE_FAILED", verdict("ABC", doc, "2026-10-11")["state"] == SOURCE_FAILED)
    try:
        parse_investegate("<html>nothing</html>")
        ok("CE17 Investegate layout change raises", False)
    except ValueError:
        ok("CE17 Investegate layout change raises", True)
    pin = pin_statistic({("2026-%02d-01" % (i + 1)): {"level": 100.0 + 0.1 * (i % 2)} for i in range(10)})
    ok("CE18 pin statistic flags a flat series", pin and pin["pinned"], pin)
    s = classify_sec_filings({"filings": {"recent": {"form": ["SC TO-T", "SC TO-T/A"],
                                                     "filingDate": ["2026-10-01", "2026-10-05"]}}}, "2026-10-18")
    v = decide("ARGX", venue="NMS", sec=s, as_of="2026-10-18")
    ok("CE19 SC TO-T without SC 14D9 = BIDDER -> REVIEW, never HARD (ARGX/AUPH 18-Oct)",
       v["state"] == CORPORATE_ACTION_PENDING and FLAG_OFFEREE not in v["flags"], v)
    v = decide("VUAG.L", venue="LSE", figi="F", panel=p, panel_by_figi=pbf, instrument_type="Mutual Fund",
               as_of="2026-10-18")
    ok("CE20 an ETP is NOT_APPLICABLE, no flag", v["state"] == NOT_APPLICABLE and not v["flags"], v)
    # R5.14 five-point matrix at the store-freshness boundary (STORE_MAX_AGE_DAYS = 4 -> stale on day 5),
    # clock injected through as_of; the boundary T is the first day NO_EVENT_FOUND stops being current.
    st = {"as_of": "2026-10-10", "verdicts": {"MU": {"symbol": "MU", "state": NO_EVENT_FOUND, "flags": [],
                                                     "venue_class": "US"}}}
    ok("ISA-0817 R5.14 T-1: age 4 days -> NO_EVENT_FOUND still current", verdict("MU", st, "2026-10-14")["state"] == NO_EVENT_FOUND)
    ok("ISA-0817 R5.14 T: age 5 days -> SOURCE_FAILED (REVIEW)", verdict("MU", st, "2026-10-15")["state"] == SOURCE_FAILED)
    ok("ISA-0817 R5.14 T+1: age 6 days -> SOURCE_FAILED", verdict("MU", st, "2026-10-16")["state"] == SOURCE_FAILED)
    ok("ISA-0817 R5.14 repeat_same_occurrence: the same day read twice gives the same verdict",
       verdict("MU", st, "2026-10-15") == verdict("MU", st, "2026-10-15"))
    st2 = dict(st, as_of="2026-10-15")
    ok("ISA-0817 R5.14 next_occurrence: the next run's fresh store is current again (no sticky state)",
       verdict("MU", st2, "2026-10-15")["state"] == NO_EVENT_FOUND)
    sub2 = {"filings": {"recent": {"form": ["DEFM14A"], "filingDate": ["2026-01-21"]}}}
    ok("CE21 offeree window boundary: filed 270 days before -> active; 271 -> not",
       classify_sec_filings(sub2, "2026-10-18")["offeree_active"]
       and not classify_sec_filings(sub2, "2026-10-19")["offeree_active"])
    try:
        import capital_decision_engine as _cde
        missing = [c for c in FLAG_CODES if c not in _cde.FLAG_TAXONOMY]
        ok("CE22 every corporate-event flag code is classified in the engine FLAG_TAXONOMY (one vocabulary)",
           not missing, missing)
        ok("CE23 offer_period_offeree is HARD_BLOCK; the others REVIEW_REQUIRED (Raj D1/D3)",
           _cde.FLAG_TAXONOMY[FLAG_OFFEREE][0] == "HARD_BLOCK"
           and all(_cde.FLAG_TAXONOMY[c][0] == "REVIEW_REQUIRED" for c in FLAG_CODES if c != FLAG_OFFEREE))
    except ImportError:
        ok("CE22 engine importable for the vocabulary pair", False, "capital_decision_engine not importable")
    try:
        import email_prefill as _ep
        _ib = _ep.build_integrity_block(corporate_events={"state": "FETCHED", "as_of": "2026-10-18",
                                                          "counts": {"OFFEREE_IN_OFFER_PERIOD": 1, "NO_EVENT_FOUND": 3},
                                                          "offerees": ["SPI.L"], "corporate_action_pending": []})
        _l = " ".join(_ib.get("s7_lines") or [])
        ok("CE24 renderer pair: the email s7 states the offeree and the HARD block (renderer only, R20.2)",
           "SPI.L" in _l and "HARD-BLOCKED" in _l and "Step 5x-ce" in _l, _l[:300])
    except ImportError:
        ok("CE24 email_prefill importable for the renderer pair", False, "email_prefill not importable")
    # CE26-CE33 - D2b primary sources (Raj 18-Oct-2026), offline fixtures
    _tob = ('<article class="transaction list-item" id="t1"><div class="inner"><span class="col1">0946</span>'
            '<span class="col2"><a class="item-anchor" href="/transactions/detail/nr/0946"><h3>PolyPeptide Group AG '
            '<span>(2026)</span></a></h3><p>Anbieter: Samsung Biologics Co., Ltd., Incheon</p></span><span class="col3 short" '
            'data-tooltip="Transaktionseigenschaft: Freiwillige Angebote<br>Barangebot"></span></div></article>'
            '<article class="transaction list-item" id="t2"><div class="inner"><span class="col1">0945</span><span class="col2">'
            '<a class="item-anchor" href="/transactions/detail/nr/0945"><h3>UBS Group AG <span>(2026)</span></a></h3><p></p>'
            '</span><span class="col3 short" data-tooltip="Transaktionseigenschaft: Rückkaufprogramm"></span></div></article>')
    _rows = parse_tob_list(_tob)
    ok("CE26 Swiss TOB list: an offer is an offer, a buyback is not", _rows[0]["is_offer"] and not _rows[1]["is_offer"]
       and _rows[1]["is_buyback"] and _rows[0]["company"] == "PolyPeptide Group AG", _rows)
    try:
        parse_tob_list("<html>moved</html>")
        ok("CE27 Swiss TOB layout change raises", False)
    except ValueError:
        ok("CE27 Swiss TOB layout change raises", True)
    ok("CE28 TOB detail: interim result is NOT completion; settlement (Vollzug) is",
       not tob_detail_completed("<p>Transaktionseigenschaften:</p><p>Definitive Meldung des Zwischenergebnisses</p>")
       and tob_detail_completed("<p>Transaktionseigenschaften:</p><p>Meldung über den Vollzug</p>"))
    ok("CE29 truncated OpenFIGI name joins by prefix (RICHEMO -> Richemont); short tokens need a whole word",
       _has_word("Compagnie Financière Richemont SA", issuer_key("CIE FINANCIERE RICHEMO-A REG")["distinctive"])
       and _has_word("ABB Ltd", "ABB") and not _has_word("ABBEY plc", "ABB"))
    _nq = [{"company": "Aino Health AB (publ)", "cnsCategory": "Other information disclosed according to the rules of the Exchange",
            "headline": "Final outcome of HealthCo Oy's public cash offer for the shares in Aino Health AB (publ)",
            "releaseTime": "2026-10-14 16:00:00 +0000"},
           {"company": "Ambea AB", "cnsCategory": "Tender offer",
            "headline": "Ambea AB (publ) announces final outcome of the recommended public offer to the shareholders of Validia",
            "releaseTime": "2026-10-15 15:00:00 +0000"}]
    ok("CE30 Nordic MUST-FIRE: offer 'for the shares in <issuer>' -> offeree",
       len(classify_nordic(_nq, "AINO HEALTH AB", "2026-10-18")["offeree_releases"]) == 1)
    _am = classify_nordic(_nq, "AMBEA AB", "2026-10-18")
    ok("CE31 Nordic NEGATIVE CONTROL: the BIDDER's offer release (Ambea) is a corporate action, never offeree",
       not _am["offeree_releases"] and len(_am["action_releases"]) == 1, _am)
    _ie = parse_irish_text("Disclosure Table\nOfferee: DCC plc\nOffer period commenced: 29 April 2026\n"
                           "€0.25 ordinary shares; ISIN: IE0002424939; NSI: 85,424,135\nOfferor: Energy Capital Partners\n")
    ok("CE32 Irish Panel text: ISIN-keyed offeree with its offer period date",
       _ie["cases"]["IE0002424939"]["offer_period_commenced"] == "2026-04-29", _ie)
    _nw = classify_news([{"title": "Couche-Tard launches $8.6 billion Zabka tender offer", "date": "2026-10-01"},
                         {"title": "Ferrari issues apology after social media takeover", "date": "2026-10-01"},
                         {"title": "Zabka opens 500th store", "date": "2026-10-02"}], "ZABKA GROUP SA", "2026-10-18")
    ok("CE33 news: issuer + offer term is a hit; issuer without a term, and declared noise, are not",
       len(_nw["hits"]) == 1 and "tender" in _nw["hits"][0]["title"], _nw)
    v = decide("ZAB.WA", venue="WSE", news=_nw, as_of="2026-10-18")
    ok("CE34 R6.3: a secondary news hit is REVIEW only - never the HARD offeree flag",
       v["flags"] == ["news_corporate_event_unconfirmed"] and FLAG_OFFEREE not in v["flags"], v)
    v = decide("PGHN.SW", venue="EBS", ch={"offeree": [_rows[0]], "action": []}, as_of="2026-10-18")
    ok("CE35 Swiss primary offeree -> HARD (D1)", v["state"] == OFFEREE_IN_OFFER_PERIOD and v["flags"] == [FLAG_OFFEREE], v)
    v = decide("SOBI.ST", venue="STO", nordic=None, nordic_error="HTTP 500", as_of="2026-10-18")
    ok("CE36 Nordic source failure -> SOURCE_FAILED (REVIEW), not unsourced", v["state"] == SOURCE_FAILED, v)
    # CE25 - the incremental, checkpointed producer (the pre-run re-run protocol): injected network, temp root
    import tempfile as _tf
    import shutil as _sh
    _root = _tf.mkdtemp(prefix="ce_selftest_")
    try:
        with open(os.path.join(_root, SYMBOL_MAP_FILE), "w") as fh:
            json.dump({"verified": [{"ticker": "SPI.L", "exchange": "LSE"}, {"ticker": "AAA", "exchange": "NMS"},
                                    {"ticker": "BBB", "exchange": "NYQ"}, {"ticker": "ASML.AS", "exchange": "AMS"}]}, fh)
        with open(os.path.join(_root, ELIGIBILITY_FILE), "w") as fh:
            json.dump({"records": {"SPI.L": {"openfigi": {"figi": "BBG006S8NKB6", "securityType2": "Common Stock"}},
                                   "AAA": {"openfigi": {"figi": "F_A", "securityType2": "Common Stock"}},
                                   "BBB": {"openfigi": {"figi": "F_B", "securityType2": "Common Stock"}},
                                   "ASML.AS": {"openfigi": {"figi": "F_C", "securityType2": "Common Stock",
                                                           "name": "ASML HOLDING NV"}}}}, fh)
        calls = []

        def _g(url, headers=None):
            calls.append(url)
            if "disclosuretable" in url:
                return _FIX_PANEL
            if "company_tickers" in url:
                return json.dumps({"0": {"ticker": "AAA", "cik_str": 1}, "1": {"ticker": "BBB", "cik_str": 2}}).encode()
            if "CIK0000000001" in url:
                return json.dumps({"filings": {"recent": {"form": ["DEFM14A"], "filingDate": ["2026-09-01"]}}}).encode()
            if "CIK0000000002" in url:
                return json.dumps({"filings": {"recent": {"form": ["10-Q"], "filingDate": ["2026-09-01"]}}}).encode()
            if "news.google" in url:
                return (b"<rss><channel><item><title>Suitor weighs takeover bid for ASML - Reuters</title>"
                        b"<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>")
            if "investegate" in url:
                return b"<table><tr><td>10 Oct 2026</td><td>07:00</td><td>RNS</td><td>Form 8.3 - Spire</td></tr></table>"
            raise OSError("unexpected url %s" % url)

        def _p(url, body):
            return [{"data": [{"figi": "BBG006S8NKB6"}]} if b.get("idValue") == "GB00BNLPYF73" else {"data": []}
                    for b in body]
        d1 = refresh(_root, "2026-10-18", get=_g, post=_p, budget_s=0.0, sec_spacing_s=0, uk_spacing_s=0)
        ok("CE25a time box 0: unfetched US names read SOURCE_FAILED (never 'no event'), cache persisted",
           d1["verdicts"]["AAA"]["state"] == SOURCE_FAILED and os.path.exists(os.path.join(_root, CACHE_FILE)), d1["counts"])
        d2 = refresh(_root, "2026-10-18", get=_g, post=_p, budget_s=30, sec_spacing_s=0, uk_spacing_s=0)
        ok("CE25b the next call resumes: AAA OFFEREE (DEFM14A), BBB NO_EVENT, SPI.L OFFEREE, ASML.AS UNSOURCED",
           d2["verdicts"]["AAA"]["state"] == OFFEREE_IN_OFFER_PERIOD and d2["verdicts"]["BBB"]["state"] == NO_EVENT_FOUND
           and d2["verdicts"]["SPI.L"]["state"] == OFFEREE_IN_OFFER_PERIOD
           and d2["verdicts"]["ASML.AS"]["state"] == OFFER_STATE_UNSOURCED and d2["complete"], d2["counts"])
        n = len(calls)
        d3 = refresh(_root, "2026-10-18", get=_g, post=_p, budget_s=30, sec_spacing_s=0, uk_spacing_s=0)
        ok("CE25c ISA-0817 R5.14 repeat_same_occurrence: a re-run inside 18 h fetches NOTHING and gives the same verdicts",
           len(calls) == n and {t: v["state"] for t, v in d3["verdicts"].items()}
           == {t: v["state"] for t, v in d2["verdicts"].items()}, (len(calls), n))
        ok("CE25e D2b news corroborator: an unsourced-venue headline adds REVIEW, state stays UNSOURCED + confirm",
           d3["verdicts"]["ASML.AS"]["flags"] == ["news_corporate_event_unconfirmed"]
           and d3["verdicts"]["ASML.AS"]["state"] == OFFER_STATE_UNSOURCED
           and d3["verdicts"]["ASML.AS"]["confirm_required"] == [CONFIRM_OFFER_STATE], d3["verdicts"]["ASML.AS"])
        ok("CE25d the store binds every source document (retrieved_at + sha256)",
           d3["panel_identity"]["sha256"] and all(x.get("sha256") for x in d3["document_identity"]["sec"].values()))
    finally:
        _sh.rmtree(_root, ignore_errors=True)
    if verbose:
        print("%d failure(s)" % len(fails))
    return 1 if fails else 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget", type=float, default=120.0)
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.refresh:
        d = refresh(a.root, budget_s=a.budget, dry_run=a.dry_run, write_store=not a.dry_run)
        print(json.dumps({k: d.get(k) for k in ("as_of", "state", "counts", "offerees",
                                                "corporate_action_pending", "source_failed", "errors")},
                         indent=1, default=str))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
