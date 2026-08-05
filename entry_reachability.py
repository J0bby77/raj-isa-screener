#!/usr/bin/env python3
"""
entry_reachability.py — register item M2, 03-Aug-2026.

THE PROBLEM
-----------
19 of 49 August entry levels sit **>100% below the current price**, every one selected via the
"return-hurdle anchor (most conservative)": TER entry $80.76 against $367.69, STRL $138.06
against $596.77, KLAC $47.49 against $182.82.

These are not target buy prices. They are the prices at which the required return becomes
**arithmetically guaranteed** — a floor derived from the return hurdle, not a level the market
is offering. The email nonetheless prints them under the column heading **"Target buy
(display)"**, which reads as an actionable instruction to wait for a price that will not come.

It does not distort Source Score (entry_window is display-only under the A6 path), so this is a
presentation defect — but presentation is what Raj acts on.

THE FIX IS A LABEL, NOT A FILTER
--------------------------------
Dropping unreachable entries would hide the fact that the anchor produced one, and the anchor's
own behaviour is something the framework should be able to see. So every entry is classified and
the classification travels with it:

    reachable   — required move is within normal trading range
    stretch     — plausible only in a drawdown
    unreachable — requires a fall the thesis would not survive; NOT a buy target

TWO TESTS, EITHER SUFFICIENT
----------------------------
1. **Absolute** — required fall > 50% (unreachable) / > 25% (stretch). A name that must halve
   before entry is not being bought on this thesis; it is a different investment case.
2. **Volatility-relative** — required fall expressed in annualised sigma, using the
   `realised_vol` already in the audit. > 2.0 sigma unreachable, > 1.0 sigma stretch.

Both are reported so the two can disagree visibly rather than one silently overriding the other:
MSM needs −64% on 25% vol (2.59 sigma) while TER needs −78% on 53% vol (1.47 sigma). The
absolute test says TER is worse; the statistical test says MSM is. Neither is wrong, and a
single blended number would have concealed the disagreement.

CLI
---
  python3 entry_reachability.py --month aug_2026
  python3 entry_reachability.py --selftest
"""
from __future__ import annotations
import argparse, json, os, sys, datetime as _dt

HERE = os.path.dirname(os.path.abspath(__file__))
ABS_UNREACHABLE, ABS_STRETCH = 50.0, 25.0     # required fall, %
SIG_UNREACHABLE, SIG_STRETCH = 2.0, 1.0       # required fall, annualised sigma

LABELS = {
    "reachable":   "Target buy",
    "stretch":     "Target buy (drawdown only)",
    "unreachable": "No reachable entry — return-hurdle floor, not a market price",
}


def classify(current_price, entry_level, realised_vol=None):
    """Returns a dict; never raises. Missing inputs yield 'unknown' with a stated reason —
    never a default of 'reachable', which would silently pass an unchecked entry."""
    out = {"required_move_pct": None, "required_move_sigma": None,
           "reachability": "unknown", "basis": None, "display_label": None}
    try:
        cp, el = float(current_price), float(entry_level)
    except (TypeError, ValueError):
        out["basis"] = "missing current_price or entry_level"
        return out
    if cp <= 0 or el <= 0:
        out["basis"] = "non-positive price"
        return out
    req = (el - cp) / cp * 100.0                      # negative => price must FALL
    out["required_move_pct"] = round(req, 1)
    fall = -req if req < 0 else 0.0
    sig = None
    try:
        v = float(realised_vol)
        if v > 0:
            sig = fall / (v * 100.0)
            out["required_move_sigma"] = round(sig, 2)
    except (TypeError, ValueError):
        pass
    by_abs = ("unreachable" if fall > ABS_UNREACHABLE else
              "stretch" if fall > ABS_STRETCH else "reachable")
    by_sig = None
    if sig is not None:
        by_sig = ("unreachable" if sig > SIG_UNREACHABLE else
                  "stretch" if sig > SIG_STRETCH else "reachable")
    rank = {"reachable": 0, "stretch": 1, "unreachable": 2}
    worst = by_abs if (by_sig is None or rank[by_abs] >= rank[by_sig]) else by_sig
    out["reachability"] = worst
    out["basis"] = (f"absolute={by_abs} (needs {fall:.0f}%)" +
                    (f" | sigma={by_sig} ({sig:.2f}σ)" if by_sig else " | sigma=unavailable") +
                    (" | tests DISAGREE, worse taken" if by_sig and by_sig != by_abs else ""))
    out["display_label"] = LABELS[worst]
    return out


def run(month, here=HERE, write=True):
    path = os.path.join(here, f"entry_level_audit_{month}.json")
    with open(path) as f:
        doc = json.load(f)
    entries = doc.get("entries", doc)
    rows = entries if isinstance(entries, list) else list(entries.values())
    res, counts = [], {"reachable": 0, "stretch": 0, "unreachable": 0, "unknown": 0}
    for r in rows:
        c = classify(r.get("current_price"), r.get("entry_level"), r.get("realised_vol"))
        counts[c["reachability"]] += 1
        res.append({"ticker": r.get("ticker"), "current_price": r.get("current_price"),
                    "entry_level": r.get("entry_level"),
                    "selected_entry_reason": r.get("selected_entry_reason"), **c})
    res.sort(key=lambda x: (x["required_move_pct"] is None, x["required_move_pct"] or 0))
    doc_out = {"schema_version": 1, "month": month,
               "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "thresholds": {"abs_unreachable_pct": ABS_UNREACHABLE, "abs_stretch_pct": ABS_STRETCH,
                              "sigma_unreachable": SIG_UNREACHABLE, "sigma_stretch": SIG_STRETCH},
               "rule": "worse of the absolute and volatility-relative tests; unknown is never "
                       "defaulted to reachable",
               "counts": counts, "entries": res}
    if write:
        p = os.path.join(here, f"entry_reachability_{month}.json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc_out, f, indent=1)
        os.replace(tmp, p)
    return doc_out


def _selftest():
    c = classify(100, 95, 0.30)
    assert c["reachability"] == "reachable", c
    c = classify(100, 70, 0.30)                       # −30% fall, 1.0σ
    assert c["reachability"] == "stretch", c
    c = classify(367.69, 80.76, 0.53)                 # TER
    assert c["reachability"] == "unreachable" and c["required_move_pct"] == -78.0, c
    c = classify(123.40, 43.95, 0.25)                 # MSM — both tests agree unreachable
    assert c["reachability"] == "unreachable" and c["required_move_sigma"] == 2.58, c
    c = classify(100, 40, None)                       # no vol -> absolute only, still decides
    assert c["reachability"] == "unreachable" and "sigma=unavailable" in c["basis"], c
    c = classify(None, 50, 0.3)
    assert c["reachability"] == "unknown" and c["display_label"] is None, c
    c = classify(100, 74, 0.60)                       # abs=stretch(26%), sigma=reachable(0.43)
    assert c["reachability"] == "stretch" and "DISAGREE" in c["basis"], c
    c = classify(100, 120, 0.3)                       # entry ABOVE price — already in range
    assert c["reachability"] == "reachable" and c["required_move_pct"] == 20.0, c
    print("SELFTEST PASS — 8 assertions (reachable, stretch, TER/MSM unreachable, vol-missing "
          "fallback, unknown never defaults to reachable, disagreement flagged, entry-above-price)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=None); ap.add_argument("--dir", default=HERE)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    if not a.month:
        print("--month required (e.g. aug_2026)", file=sys.stderr); sys.exit(2)
    d = run(a.month, a.dir)
    c = d["counts"]
    print(f"ENTRY_REACHABILITY {a.month}: reachable {c['reachable']} | stretch {c['stretch']} | "
          f"unreachable {c['unreachable']} | unknown {c['unknown']}")
    for e in d["entries"][:12]:
        if e["reachability"] == "unreachable":
            print(f"  {e['ticker']:<10} {e['required_move_pct']:>7.0f}%  "
                  f"{(str(e['required_move_sigma']) + 'σ'):>7}  -> {e['display_label']}")


if __name__ == "__main__":
    main()
