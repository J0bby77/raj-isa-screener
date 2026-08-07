#!/usr/bin/env python3
"""
universe_hygiene.py — 05-Aug-2026. Detect-and-report: what is in the rankable universe that
should not be, and which lines are the same issuer twice.

⚑ WHY, AND WHAT IT IS NOT
-------------------------
This module CHANGES NOTHING. It does not gate, reorder, exclude or rescore. It measures two
things every weekly screen and writes them down, because both were previously invisible:

  1. NON-COMMON LINES IN THE RANKABLE SET. `security_type.classify_security_type()` has always
     been able to name these and had **zero callers**. On the 24-Jul-2026 NASDAQ frame four
     reached CANDIDATE_RANKABLE: GOOGM/GOOGN and SMCIP (mandatory convertible preferred
     depositary shares) and NOVTU (tangible equity units). NOVTU scored 54.1 against the actual
     company NOVT at 38.6 — the wrapper outranked the business.

  2. SAME-ISSUER MULTIPLE LINES. Register item M3, downgraded to MEDIUM on 02-Aug-2026 on the
     stated premise "only one collision exists across 58 names". On one NASDAQ frame there are
     several (Alphabet ×4, Liberty Global ×3, Rush ×2, Novanta ×2, Super Micro ×2), and in two
     cases the secondary line outranks the primary. The premise deserves re-testing with data;
     the SEVERITY CALL IS RAJ'S, which is exactly why this reports and does not act.

**Measured, so it is not overstated:** as at 05-Aug-2026 no non-common line and no secondary
share class has actually reached SUMMARY on either retained frame (SUMMARY_SOURCE_FLOOR = 70).
This is a live exposure with no realised cost — a reason to watch it weekly, not an emergency.

Stdlib + pandas.
"""
from __future__ import annotations
import argparse, json, os, re, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Legal-form and share-class tokens stripped before two company names are compared. Kept
# deliberately conservative: a FALSE collision is worse than a missed one, because it would
# invite collapsing two genuinely different issuers into one row.
_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|plc|nv|n\.v|sa|s\.a|ag|ab|as|a/s|oyj|"
    r"ltd|limited|lp|llc|holdings?|group|the|se|spa|s\.p\.a|bv|b\.v|kgaa|asa)\b\.?", re.I)
_CLASS = re.compile(
    r"\b(class\s+[a-z]|series\s+[a-z]|common\s+(stock|shares?)|ordinary\s+shares?|"
    r"american\s+deposit[ao]ry\s+(shares?|receipts?)|ads|adr|depositary\s+shares?|"
    r"new\s+york\s+registry\s+shares?|tangible\s+equity\s+units?|units?|"
    r"shares?\s+of\s+beneficial\s+interest)\b", re.I)
_NOISE = re.compile(r"[^a-z0-9 ]+")


def normalise_issuer(name: str) -> str:
    """Company description -> issuer key. Everything after the first descriptor phrase is
    dropped, because feed descriptions append arbitrary prose ('representing a 1/20th Interest
    in a Share of Series A Mandatory Convertible Preferred Stock')."""
    # ⚑ float('nan') is TRUTHY, so `name or ""` lets a missing company name through as the
    # string "nan" — which then becomes a shared issuer key and collides every undescribed row
    # with every other. Same null-vs-missing conflation the register names as a recurring class.
    if name is None or name != name or not str(name).strip():
        return ""
    n = str(name).lower()
    # TRUNCATE at the first descriptor rather than deleting descriptors in place. Deleting them
    # leaves the trailing prose behind ("Alphabet Inc. Depositary Shares representing a 1/20th
    # Interest..." -> "alphabet representing a"), which then fails to match the same issuer's
    # plain line. The issuer name is always the PREFIX before the first instrument descriptor.
    m = _CLASS.search(n)
    if m:
        n = n[:m.start()]
    n = _SUFFIX.sub(" ", n)
    n = _NOISE.sub(" ", n)
    toks = [t for t in n.split() if t]
    return " ".join(toks[:3])          # first three surviving tokens identify the issuer


def analyse(frame_path, summary_floor=None):
    import pandas as pd
    from security_type import classify_security_type
    try:
        import scoring_config as cfg
        floor = summary_floor if summary_floor is not None else \
            float(getattr(cfg, "SUMMARY_SOURCE_FLOOR", 70.0))
    except Exception:
        floor = summary_floor if summary_floor is not None else 70.0

    d = pd.read_csv(frame_path, low_memory=False)
    if "final_status" not in d.columns:
        return {"ok": False, "reason": "frame has no final_status column"}
    c = d[d["final_status"].astype(str).str.upper() == "CANDIDATE_RANKABLE"].copy()
    if not len(c):
        return {"ok": True, "rankable": 0, "note": "no rankable rows"}
    score_col = "screen_source" if "screen_source" in c.columns else None
    c["security_type"] = [classify_security_type(x, t) for x, t in zip(c.get("company"), c["ticker"])]
    c["issuer_key"] = [normalise_issuer(x) for x in c.get("company")]

    def _s(row):
        try:
            return float(row[score_col]) if score_col and row[score_col] == row[score_col] else None
        except Exception:
            return None

    non_common, above = [], []
    for _, r in c[c.security_type == "non_common"].iterrows():
        sc = _s(r)
        rec = {"ticker": r["ticker"], "company": str(r.get("company"))[:90], "source": sc}
        non_common.append(rec)
        if sc is not None and sc >= floor:
            above.append(rec)

    collisions = []
    for key, grp in c.groupby("issuer_key"):
        if key and len(grp) > 1:
            lines = sorted(({"ticker": r["ticker"], "source": _s(r),
                             "security_type": r["security_type"]} for _, r in grp.iterrows()),
                           key=lambda x: (x["source"] is None, -(x["source"] or 0)))
            collisions.append({"issuer_key": key, "n_lines": len(lines), "lines": lines,
                               "top_line": lines[0]["ticker"],
                               "top_is_non_common": lines[0]["security_type"] == "non_common",
                               "any_above_floor": any(l["source"] is not None and
                                                      l["source"] >= floor for l in lines)})
    collisions.sort(key=lambda x: -x["n_lines"])
    return {
        "ok": True, "frame": os.path.basename(frame_path),
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "summary_source_floor": floor, "rankable": int(len(c)),
        "security_type_counts": {k: int(v) for k, v in c.security_type.value_counts().items()},
        "non_common_rankable": non_common,
        "non_common_above_summary_floor": above,
        "issuer_collisions": collisions,
        "issuers_with_multiple_lines": len(collisions),
        "collisions_reaching_summary_floor": sum(1 for x in collisions if x["any_above_floor"]),
    }


def _selftest():
    import tempfile, pandas as pd
    assert normalise_issuer("Alphabet Inc. Depositary Shares representing a 1/20th Interest") == \
        normalise_issuer("Alphabet Inc. Class A Common Stock"), "Alphabet lines must collide"
    assert normalise_issuer("Liberty Global Ltd. Class B Common Shares") == \
        normalise_issuer("Liberty Global Ltd. Class C Common Shares"), "Liberty lines must collide"
    assert normalise_issuer("Novanta Inc. Tangible Equity Units") == \
        normalise_issuer("Novanta Inc. Common Stock"), "Novanta lines must collide"
    # false positives are the expensive error — two different issuers must NOT collide
    assert normalise_issuer("Apple Inc. Common Stock") != normalise_issuer("Apollo Inc. Common Stock")
    assert normalise_issuer("Monster Beverage Corporation") != normalise_issuer("Monster Digital Inc")
    assert normalise_issuer("") == "", "empty description must not become a shared key"

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "2026-01-01_T_full_data.csv")
        pd.DataFrame({
            "ticker": ["AAA", "AAAP", "BBB"],
            "company": ["Aaa Inc. Common Stock", "Aaa Inc. Depositary Shares", "Bbb Inc. Common Stock"],
            "final_status": ["CANDIDATE_RANKABLE"] * 3,
            "screen_source": [60.0, 80.0, 55.0]}).to_csv(p, index=False)
        r = analyse(p, summary_floor=70.0)
        assert r["rankable"] == 3
        assert [x["ticker"] for x in r["non_common_rankable"]] == ["AAAP"]
        assert [x["ticker"] for x in r["non_common_above_summary_floor"]] == ["AAAP"], \
            "a non-common line above the floor must be surfaced"
        assert r["issuers_with_multiple_lines"] == 1
        col = r["issuer_collisions"][0]
        assert col["top_line"] == "AAAP" and col["top_is_non_common"] is True, \
            "the wrapper outranking the company must be visible in the report"
        # empty descriptions must not group together into a phantom issuer
        pd.DataFrame({"ticker": ["X", "Y"], "company": [None, None],
                      "final_status": ["CANDIDATE_RANKABLE"] * 2,
                      "screen_source": [1.0, 2.0]}).to_csv(p, index=False)
        assert analyse(p, 70.0)["issuers_with_multiple_lines"] == 0, \
            "undescribed names must not collide with each other"
    print("SELFTEST PASS — 12 assertions (issuer normalisation ×3, false-positive guards ×3, "
          "non-common detection, above-floor escalation, collision grouping, wrapper-outranks "
          "detection, empty-description safety ×2)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    r = analyse(a.frame)
    if a.json:
        print(json.dumps(r, indent=2, default=str)); sys.exit(0)
    print(f"UNIVERSE_HYGIENE {r.get('frame')} | rankable {r.get('rankable')} | "
          f"types {r.get('security_type_counts')}")
    print(f"  non-common in rankable set: {len(r.get('non_common_rankable', []))} "
          f"({len(r.get('non_common_above_summary_floor', []))} at/above SUMMARY floor "
          f"{r.get('summary_source_floor')})")
    for x in r.get("non_common_rankable", []):
        print(f"    {x['ticker']:7s} src {x['source']}  {x['company']}")
    print(f"  issuers with >1 rankable line: {r.get('issuers_with_multiple_lines')} "
          f"({r.get('collisions_reaching_summary_floor')} reaching the SUMMARY floor)")
    for x in r.get("issuer_collisions", [])[:12]:
        flag = "  ⚑ wrapper outranks company" if x["top_is_non_common"] else ""
        print(f"    {x['issuer_key']:24s} {[ (l['ticker'], l['source']) for l in x['lines'] ]}{flag}")
