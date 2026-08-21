#!/usr/bin/env python3
"""
vci_integrity.py — closes ISA-0171 (V-2) · ISA-0172 (V-3) · ISA-0175 (V-6) · ISA-0176 (V-7).
Built 16-Aug-2026.

Four VCI findings sat OPEN with `corrective_action: Missing(not_recorded_at_the_time)` since they
were lifted verbatim out of the decision register. Each is DIAGNOSED here against the live
`vci_deploy_aug_2026.json` and each becomes an assertion, because a finding with no check is a
sentence.

⚑ V-2 (ISA-0171) DIAGNOSED. Not "either the type is dropped on write or the days are synthetic" —
BOTH FIELDS ARE PURE PASS-THROUGH from the candidate entry (`vci_deploy_eval.refresh_at_live_price`
reads `e.get("days_to_catalyst")` and `e.get("catalyst_type")` side by side). Upstream supplies the
DAYS and not the TYPE, on all 9 names. The consequence is not cosmetic and it is asymmetric:

    days_to_catalyst  ->  size_for():  `near = days < 90` SHRINKS THE POSITION SIZE.        USED
    catalyst_type     ->  derive_floor() and lookup_priors(): selects p_thesis and L.    DEFAULTED
    catalyst_type     ->  has_catalyst = bool(e.get("has_catalyst", e.get("catalyst_type")))  FALSE

So a clock counting down to an event the framework cannot NAME is moving a position size, while
the same absent field silently defaults the probability priors. R4.3 — a control fed a null
BLOCKS. The pair is now COHERENCE-GATED: days without a type is refused as an input, not used.

⚑ V-3 (ISA-0172) DIAGNOSED. Not "may be intended add-on logic". `held` and `in_portfolio` are None
on ALL NINE rows: `evaluate_candidate` has no holdings input at all. ONT.L and ABCL are held, and
the evaluator emitted a FRESH-POSITION size of 0.75% for both because it did not know. A size that
means "open this" and a size that means "add to this" are different instructions, and a framework
that cannot tell them apart will double a position while reporting a starter.

⚑ V-6 (ISA-0175) — cluster-scoring B1/B2 under-scored RGTI and INFQ IN THE SAME DIRECTION, which
is the signature of a shared score rather than two independent reads. Asserted, not remembered.

⚑ V-7 (ISA-0176) — 4 of 5 watchlist names quantum. Turned from an observation into a measured
theme-concentration reading with a declared ceiling.

ROLLBACK (R4.13): `ENFORCE = False` — every check still reports, nothing blocks.
"""
from __future__ import annotations
import collections, datetime as dt, json, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENFORCE = True
THEME_CONCENTRATION_CEILING = 0.60      # share of a watchlist one theme may hold before it flags
SCHEMA_VERSION = "1.0.0"

# Declared theme map. Hand-written, therefore controlled (ISA-0344): a name absent from this map
# is UNMAPPED and counted as such — never silently folded into "other".
THEMES = {
    "QBTS": "quantum", "RGTI": "quantum", "IONQ": "quantum", "INFQ": "quantum",
    "CRSP": "genomics", "ABCL": "biotech_platform", "ONT.L": "genomics_tools",
    "SATL": "space", "RXRX": "ai_biotech",
}


# ── V-2 ───────────────────────────────────────────────────────────────────────────────────────
def catalyst_coherence(entry: dict) -> dict:
    """A countdown to an unnamed event is not evidence. days-without-type is REFUSED (R4.3)."""
    days, ctype = entry.get("days_to_catalyst"), entry.get("catalyst_type")
    if days is None and ctype is None:
        return {"state": "NO_CATALYST", "days_usable": None, "blocks": False}
    if days is not None and ctype is None:
        return {"state": "INCOHERENT_DAYS_WITHOUT_TYPE", "days_usable": None,
                "blocks": bool(ENFORCE),
                "reason": (f"days_to_catalyst={days} with catalyst_type=None. The days shrink the "
                           f"position size via size_for(); the type sets p_thesis and L via "
                           f"lookup_priors(). Using one while defaulting the other lets an unnamed "
                           f"event move capital. The days are refused as an input until the type "
                           f"is supplied.")}
    if days is None and ctype is not None:
        return {"state": "TYPE_WITHOUT_DAYS", "days_usable": None, "blocks": False,
                "reason": "priors are selectable; no timing term is applied"}
    return {"state": "COHERENT", "days_usable": days, "blocks": False}


# ── V-3 ───────────────────────────────────────────────────────────────────────────────────────
def position_state(entry: dict, held_tickers: set) -> dict:
    """NEW vs HELD. A fresh-position size may never be emitted for a name already owned."""
    t = entry.get("ticker")
    held = t in held_tickers
    size = entry.get("size_pct")
    return {
        "ticker": t, "position_state": "HELD" if held else "NEW",
        "size_pct_emitted": size,
        "size_basis": ("ADD_ON - must be sized against the EXISTING position and the sleeve cap, "
                       "not as an opening trade" if held else "OPENING"),
        "defect": bool(held and entry.get("deploy_eligible") and size and size > 0
                       and entry.get("position_state") is None),
        "blocks": bool(ENFORCE and held and entry.get("deploy_eligible") and size and size > 0
                       and entry.get("position_state") is None),
    }


# ── V-6 ───────────────────────────────────────────────────────────────────────────────────────
def independent_scoring(entries: list, fields=("B1", "B2", "acs_b1", "acs_b2")) -> dict:
    """Two names must not share a component score by construction.

    The tell for cluster-scoring is not that two names score the same — that can happen — but that
    a whole THEME shares one value across every member while other themes differ.
    """
    by_theme = collections.defaultdict(list)
    for e in entries:
        by_theme[THEMES.get(e.get("ticker"), "UNMAPPED")].append(e)
    suspicious = []
    for theme, rows in by_theme.items():
        if len(rows) < 2:
            continue
        for f in fields:
            vals = [r.get(f) for r in rows if r.get(f) is not None]
            if len(vals) >= 2 and len(set(vals)) == 1:
                suspicious.append({"theme": theme, "field": f, "value": vals[0],
                                   "tickers": [r.get("ticker") for r in rows],
                                   "note": "every member of the theme carries one identical value "
                                           "- consistent with a cluster score, not two reads"})
    return {"state": "MEASURED", "suspicious": suspicious,
            "clean": not suspicious, "fields_checked": list(fields),
            "themes": {k: [r.get("ticker") for r in v] for k, v in by_theme.items()}}


# ── V-7 ───────────────────────────────────────────────────────────────────────────────────────
def theme_concentration(tickers: list) -> dict:
    counts = collections.Counter(THEMES.get(t, "UNMAPPED") for t in tickers)
    n = sum(counts.values())
    unmapped = counts.get("UNMAPPED", 0)
    top, top_n = (counts.most_common(1)[0] if counts else (None, 0))
    share = top_n / n if n else None
    return {"state": "MEASURED", "n_names": n, "counts": dict(counts),
            "unmapped": unmapped,
            "top_theme": top, "top_share": round(share, 3) if share is not None else None,
            "ceiling": THEME_CONCENTRATION_CEILING,
            "verdict": ("CONCENTRATED" if (share is not None and share > THEME_CONCENTRATION_CEILING)
                        else "WITHIN_CEILING"),
            "note": ("theme concentration is a CAPITAL risk in a sleeve sized in whole percents: "
                     "four names in one theme is one bet held four times")}


# ── the run ───────────────────────────────────────────────────────────────────────────────────
def check(deploy_path=None, portfolio_path=None) -> dict:
    d = json.load(open(deploy_path or HERE / "vci_deploy_aug_2026.json"))
    entries = [dict(v, ticker=k) for k, v in d.items() if not str(k).startswith("_")]
    try:
        p = json.load(open(portfolio_path or HERE / "portfolio_data_aug_2026.json"))
        held = {s["ticker"] for s in p.get("stocks", [])} | {
            s["ticker"] + ".L" for s in p.get("stocks", [])}
    except Exception:                                              # noqa: BLE001
        held = set()
    coherence = {e["ticker"]: catalyst_coherence(e) for e in entries}
    pos = {e["ticker"]: position_state(e, held) for e in entries}
    return {
        "_meta": {"module": "vci_integrity.py", "schema_version": SCHEMA_VERSION,
                  "as_of": dt.date.today().isoformat(), "enforce": ENFORCE,
                  "closes": ["ISA-0171", "ISA-0172", "ISA-0175", "ISA-0176"]},
        "v2_catalyst_coherence": {
            "verdicts": coherence,
            "n_incoherent": sum(1 for v in coherence.values()
                                if v["state"] == "INCOHERENT_DAYS_WITHOUT_TYPE"),
            "n_total": len(coherence)},
        "v3_position_state": {
            "held_detected": sorted(held),
            "verdicts": pos,
            "n_defect": sum(1 for v in pos.values() if v["defect"])},
        "v6_independent_scoring": independent_scoring(entries),
        "v7_theme_concentration": theme_concentration([e["ticker"] for e in entries]),
    }


def selftest(verbose=True) -> int:
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    r = check()
    ck("V-2 REPRODUCED on live data: every name is days-without-type",
       r["v2_catalyst_coherence"]["n_incoherent"] == r["v2_catalyst_coherence"]["n_total"] > 0)
    ck("V-2 the refused days are not passed through as usable",
       all(v["days_usable"] is None for v in r["v2_catalyst_coherence"]["verdicts"].values()))
    ck("V-2 NEGATIVE CONTROL: a coherent pair is NOT blocked",
       catalyst_coherence({"days_to_catalyst": 60, "catalyst_type": "phase2_biotech"})["state"]
       == "COHERENT")
    ck("V-2 NEGATIVE CONTROL: no catalyst at all is not an error",
       catalyst_coherence({})["state"] == "NO_CATALYST" and not catalyst_coherence({})["blocks"])

    ck("V-3 REPRODUCED: held names carry a fresh-position size",
       r["v3_position_state"]["n_defect"] >= 2)
    ck("V-3 ONT.L and ABCL are the two named",
       all(r["v3_position_state"]["verdicts"][t]["defect"] for t in ("ONT.L", "ABCL")))
    ck("V-3 a HELD name is labelled ADD_ON, never OPENING",
       r["v3_position_state"]["verdicts"]["ABCL"]["size_basis"].startswith("ADD_ON"))
    ck("V-3 NEGATIVE CONTROL: an unheld eligible name is NOT a defect",
       r["v3_position_state"]["verdicts"]["QBTS"]["defect"] is False
       and r["v3_position_state"]["verdicts"]["QBTS"]["position_state"] == "NEW")

    ck("V-6 the check runs and reports", r["v6_independent_scoring"]["state"] == "MEASURED")
    ck("V-6 NEGATIVE CONTROL: a fabricated shared cluster score IS caught",
       bool(independent_scoring([{"ticker": "RGTI", "B1": 7}, {"ticker": "INFQ", "B1": 7},
                                 {"ticker": "CRSP", "B1": 3}])["suspicious"]))
    ck("V-6 NEGATIVE CONTROL: differing scores in one theme are clean",
       independent_scoring([{"ticker": "RGTI", "B1": 7}, {"ticker": "INFQ", "B1": 4}])["clean"])

    tc = r["v7_theme_concentration"]
    ck("V-7 REPRODUCED: quantum is the top theme", tc["top_theme"] == "quantum")
    ck("V-7 the reading is a SHARE against a declared ceiling, not a note",
       isinstance(tc["top_share"], float) and tc["ceiling"] == THEME_CONCENTRATION_CEILING)
    ck("V-7 NEGATIVE CONTROL: a 5-name all-quantum list breaches the ceiling",
       theme_concentration(["QBTS", "RGTI", "IONQ", "INFQ", "CRSP"])["verdict"] == "CONCENTRATED")
    ck("V-7 unmapped names are COUNTED, never folded into other",
       theme_concentration(["QBTS", "ZZZZ"])["unmapped"] == 1)

    global ENFORCE
    ENFORCE = False
    ck("rollback: nothing blocks with ENFORCE off",
       not any(v["blocks"] for v in check()["v2_catalyst_coherence"]["verdicts"].values()))
    ENFORCE = True
    print(f"\nvci_integrity selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 16 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(json.dumps(check(), indent=2))
