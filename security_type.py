#!/usr/bin/env python3
"""
security_type.py — ONE HOME for "what instrument is this line?" (extracted 05-Aug-2026).

Previously this lived inside `screener_core.py`, which imports yfinance, pandas and the whole
screening stack — so nothing that was not already a screener could ask the question, and in
practice **nothing asked it at all**: `classify_security_type` had zero callers. It was correct,
tested-by-inspection, and dead.

That mattered on the 24-Jul-2026 NASDAQ frame, where four lines reached CANDIDATE_RANKABLE that
are not growth equity at all:

    GOOGM / GOOGN   Alphabet mandatory convertible preferred depositary shares   source 22.6
    SMCIP           Super Micro mandatory convertible preferred depositary shares source 24.9
    NOVTU           Novanta TANGIBLE EQUITY UNITS                                 source 54.1

NOVTU is the one that shows why this is not cosmetic: a tangible equity unit is a bundle of a
purchase contract and an amortising senior note. It scored **54.1 against the actual company
(NOVT) at 38.6** — the wrapper outranked the business by 15 points.

Stdlib only, no pandas, no network. Import it from anywhere.

⚑ DOMAIN CONTRACT (unchanged from the original): this module OBSERVES. It is consulted by the
capture layer so the fact is on the record. It is NOT consulted by any gate, score, or universe
filter, because changing what the screen ranks is Raj's decision, not a refactor's side-effect.
Anything that wants to gate on it must say so explicitly at its own call site.
"""
from __future__ import annotations

# Security-type classification for the CAPTURE LAYER ONLY. Never consulted by a gate, a score
# or the universe filter (H7: the capture layer observes, it never calibrates).
#
# Why it exists: the NASDAQ constituent feed labelled "clean equities" contains preferred
# depositary shares and baby bonds (ACGLN, ACGLO, ADAML/M/N ...). yfinance reports quoteType
# "EQUITY" for all of them, so they cannot be told apart from the info payload — but the feed's
# own `company` description names the instrument exactly. These securities have no revenue line
# and no marketCap, so they depress every coverage statistic without any measurement having
# failed. Left unclassified they would make a universe-hygiene problem look like a data-quality
# problem, which is the specific confusion this framework keeps having.
_NON_COMMON_MARKERS = (
    "depositary share", "depositary receipt", "preferred", "pfd", "% series", "% notes",
    "senior note", "subordinated note", " notes due", "warrant", " unit", "units)", " right",
    " rights", "trust preferred", "capital security", "debenture",
)
_COMMON_MARKERS = ("common stock", "common share", "ordinary share", "ordinary stock",
                   "class a", "class b", "class c",
                   # BOTH spellings occur in the NASDAQ feed on the same day — "American
                   # Depositary Shares" (ABVX) and "American Depository Shares" (BZ, KSPI).
                   # Matching only the first left 16 of 356 rankable names as `unknown`
                   # on the 24-Jul-2026 frame, which is a coverage hole disguised as a
                   # legitimate "we don't know".
                   "american depositary share", "american depositary receipt",
                   "american depository share", "american depository receipt", " ads",
                   # A New York registry share IS the common equity — it is the SAME security
                   # as the home line (ASML vs ASML.AS), which is exactly the M3 dual-listing
                   # exposure. Classified common here; the duplication is a separate fact,
                   # reported by universe hygiene, not by this function.
                   "new york registry share",
                   "shares of beneficial interest")


def classify_security_type(company_desc, ticker=None):
    """'common' | 'non_common' | 'unknown' from the constituent feed's own description.

    American Depositary Shares are COMMON — they are the ordinary equity of a foreign issuer
    (Abivax/ABVX). Plain 'Depositary Shares' are the preferred wrapper (Arch Capital/ACGLN).
    That single distinction is why this reads the description rather than pattern-matching on
    the word 'depositary'.
    """
    d = str(company_desc or "").lower()
    if not d:
        return "unknown"
    if "american depositary" in d or "american depository" in d:
        return "common"
    for m in _NON_COMMON_MARKERS:
        if m in d:
            return "non_common"
    for m in _COMMON_MARKERS:
        if m in d:
            return "common"
    return "unknown"


COMMON, NON_COMMON, UNKNOWN = "common", "non_common", "unknown"

# ── ISA-0860 (12-Oct-2026) — INSTRUMENT FORM, a SECOND and ORTHOGONAL fact ─────────────────────────────
# `classify_security_type` answers the ECONOMIC question (is this the ordinary equity?) and correctly calls an
# ADS "common". ISA wrapper eligibility asks a LEGAL question it cannot answer: a depositary receipt qualifies
# only if its UNDERLYING shares would (HMRC). VEON ("VEON Ltd. ADS") read "common" and took an October capital
# slot AJ Bell will not hold in an ISA. The FORM is therefore recorded separately; neither function overrides
# the other. This is the corroborating source to OpenFIGI securityType2 (Raj 12-Oct-2026: OpenFIGI primary,
# feed description cross-checks; a disagreement reads CONFIRM_REQUIRED, never a refusal).
DIRECT, DEPOSITARY_RECEIPT = "direct", "depositary_receipt"
_DR_MARKERS = ("american depositary", "american depository", "global depositary", "global depository",
               "depositary receipt", "depository receipt", "new york registry share", "ny registry share")
_DR_WORDS = ("ads", "adr", "adrs", "gdr", "gdrs")
_DIRECT_MARKERS = ("common stock", "common share", "ordinary share", "ordinary stock",
                   "shares of beneficial interest")


def _words(d: str):
    import re as _re
    return set(_re.findall(r"[a-z0-9]+", d))


def classify_instrument_form(company_desc, ticker=None):
    """'depositary_receipt' | 'direct' | 'unknown' from the feed description. A NY registry share is reported
    as a depositary-form line (OpenFIGI types it Depositary Receipt) - its eligibility is then decided by the
    underlying listing or Raj's declaration, never assumed. An undescribed line stays UNKNOWN (never guessed)."""
    d = str(company_desc or "").lower()
    if not d.strip():
        return UNKNOWN
    if any(m in d for m in _DR_MARKERS) or (_words(d) & set(_DR_WORDS)):
        return DEPOSITARY_RECEIPT
    if "depositary share" in d or "depository share" in d:
        return DEPOSITARY_RECEIPT               # the preferred wrapper (ACGLN) - non_common anyway
    if any(m in d for m in _DIRECT_MARKERS):
        return DIRECT
    return UNKNOWN


def us_listed_ordinary_shares(company_desc) -> bool:
    """True when a US-venue feed line names 'Ordinary Shares' - the NASDAQ feed's wording for a non-US issuer
    whose shares (not a DR) trade in the US (GLBE, FROG, MNDY, CRDO ...). ⚑ A WEAK predictor of a broker custody
    or CREST-settlement refusal (GLBE, Raj 11-Oct-2026): consumers may FLAG on it, never refuse (ISA-0860)."""
    return "ordinary share" in str(company_desc or "").lower()


def _selftest():
    cases = [
        # (description, expected) — every one taken from a REAL constituent feed row
        ("Alphabet Inc. Depositary Shares representing a 1/20th Interest in a Share of "
         "Series A Mandatory Convertible Preferred Stock", NON_COMMON),
        ("Novanta Inc. Tangible Equity Units", NON_COMMON),
        ("Super Micro Computer Inc. Depositary Shares representing a 1/20th Interest in a "
         "Share of 7% Series A Mandatory Convertible Preferred Stock", NON_COMMON),
        ("Arch Capital Group Ltd. Depositary Shares", NON_COMMON),
        # American Depositary/Depository Shares ARE the ordinary equity of a foreign issuer.
        # Both spellings occur in the same feed on the same day.
        ("Abivax Societe Anonyme American Depositary Shares", COMMON),
        ("KANZHUN LIMITED American Depository Shares", COMMON),
        ("Baidu Inc. ADS", COMMON),
        ("CAE Inc. Common Shares", COMMON),
        ("Apple Inc. Common Stock", COMMON),
        ("Liberty Global Ltd. Class B Common Shares", COMMON),
        ("ASML Holding N.V. New York Registry Shares", COMMON),
        ("MGE Energy Inc", UNKNOWN),
        ("", UNKNOWN),
        (None, UNKNOWN),
    ]
    bad = [(d, classify_security_type(d), e) for d, e in cases
           if classify_security_type(d) != e]
    assert not bad, f"misclassified: {bad}"
    # The ADS rule must beat the 'depositary share' non-common rule, not merely coexist with it.
    assert classify_security_type("X American Depositary Shares") == COMMON
    assert classify_security_type("X Depositary Shares") == NON_COMMON
    # ISA-0860: the FORM is orthogonal - an ADS is economically common AND a depositary receipt
    _form = [("VEON Ltd. ADS", DEPOSITARY_RECEIPT), ("argenx SE American Depositary Shares", DEPOSITARY_RECEIPT),
             ("Genmab A/S ADS", DEPOSITARY_RECEIPT), ("BeOne Medicines Ltd. American Depositary Shares", DEPOSITARY_RECEIPT),
             ("ASML Holding N.V. New York Registry Shares", DEPOSITARY_RECEIPT), ("KANZHUN LIMITED American Depository Shares", DEPOSITARY_RECEIPT),
             ("Apple Inc. Common Stock", DIRECT), ("Global-E Online Ltd. Ordinary Shares", DIRECT),
             ("Matador Resources Company", UNKNOWN), ("Adsboard Corp Common Stock", DIRECT), ("", UNKNOWN), (None, UNKNOWN)]
    _badf = [(d, classify_instrument_form(d), e) for d, e in _form if classify_instrument_form(d) != e]
    assert not _badf, f"instrument form misclassified: {_badf}"
    assert classify_security_type("VEON Ltd. ADS") == COMMON and classify_instrument_form("VEON Ltd. ADS") == DEPOSITARY_RECEIPT
    assert us_listed_ordinary_shares("Global-E Online Ltd. Ordinary Shares") and not us_listed_ordinary_shares("Apple Inc. Common Stock")
    # R5.5 labelled controls for the ISA-0860 form classifier (a broken classifier must fail these)
    assert classify_instrument_form("Adsboard Corp Common Stock") == DIRECT, \
        "NEGATIVE CONTROL: 'ads' inside a word is not an ADS - a substring match must fail this"
    assert classify_instrument_form("Matador Resources Company") == UNKNOWN, \
        "NEGATIVE CONTROL: an undescribed line stays UNKNOWN - a classifier that defaults to direct must fail this"
    assert classify_instrument_form("VEON Ltd. ADS") == DEPOSITARY_RECEIPT, \
        "MUST-FIRE: VEON's own feed wording is a depositary receipt - the economic 'common' class must not mask it"
    assert not us_listed_ordinary_shares("Global-E Online Ltd. ADS"), \
        "NEGATIVE CONTROL: the custody signal is the 'Ordinary Shares' wording only, never any foreign issuer"
    print(f"SELFTEST PASS — {len(cases) + 2} assertions (preferred wrappers, tangible equity "
          f"units, ADS/ADR both spellings, NY registry shares, precedence of ADS over "
          f"'depositary share', undescribed names stay UNKNOWN and are never guessed)")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
