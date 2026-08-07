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
    print(f"SELFTEST PASS — {len(cases) + 2} assertions (preferred wrappers, tangible equity "
          f"units, ADS/ADR both spellings, NY registry shares, precedence of ADS over "
          f"'depositary share', undescribed names stay UNKNOWN and are never guessed)")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
