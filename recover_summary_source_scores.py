#!/usr/bin/env python3
"""
recover_summary_source_scores.py — register item M1, 03-Aug-2026.

THE PROBLEM
-----------
465 rows in `score_panel.csv` carry no `source_score`, all from the four June-2026 runs.
`backfill_score_panel.py` reconstructed those rows from each workbook's **SCORES** sheet, and
SCORES carries no source column at all. Only **SUMMARY** does, under the name `Source Score`.

WHY THIS RECOVERS, AND DOES NOT RECOMPUTE
-----------------------------------------
The obvious alternative — recompute `source_score` from the stored components via
`source_score.source_score_for_row()` — is WRONG and must not be done. `SOURCE_WEIGHTS` and
`FORWARD_AXIS_BUCKET_WEIGHTS` changed on 29-Jul-2026 (WP-M: price momentum 42% -> 20%).
Recomputing a June row under the August config produces a number that *claims* to be "the
Source Score the screen assigned in June" and *is* "what today's config would assign to June's
inputs". That is the exact defect class the open-items register names as recurring, and it would
be undetectable afterwards because the value would look entirely plausible.

So this recovers the **stored, point-in-time** value and nothing else.

THE CEILING IS REAL AND MUST BE STATED
--------------------------------------
SUMMARY holds only the SUMMARY-eligible selection (~15 names per run), not the full scored set.
So most of the 465 are **not recoverable at any effort** — the value was never written down.
That is a data-reality limit, not a bug. This script closes the recoverable part and reports the
irrecoverable remainder explicitly rather than leaving it looking like an oversight.

CLI
---
  python3 recover_summary_source_scores.py --dry-run
  python3 recover_summary_source_scores.py --apply
  python3 recover_summary_source_scores.py --selftest
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys, datetime as _dt

HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
GROUP_ALIASES = {
    "midcap400": "MIDCAP400", "nasdaq": "NASDAQ", "sp500": "SP500",
    "stoxx600": "STOXX600", "stoxx 600": "STOXX600",
    "f250 & spi": "F250SPI", "f250&spi": "F250SPI", "f250spi": "F250SPI",
    "energy": "ENERGY", "other": "OTHER",
}
FNAME_RE = re.compile(r"Growth Stock Analysis (?P<group>.+?) W-e (?P<d>\d{1,2})-(?P<m>[A-Za-z]{3})-(?P<y>\d{2})",
                      re.IGNORECASE)


def parse_workbook_name(path):
    m = FNAME_RE.search(os.path.basename(path))
    if not m:
        return None, None
    g = GROUP_ALIASES.get(m.group("group").strip().lower())
    if not g:
        return None, None
    try:
        d = _dt.date(2000 + int(m.group("y")), MONTHS[m.group("m").title()], int(m.group("d")))
    except (KeyError, ValueError):
        return None, None
    return d.isoformat(), g


def read_summary_source(path):
    """Return {ticker: source_score} from a workbook's SUMMARY sheet. Header sits at row index 3
    (row 0 title, row 1 blank, row 2 group bands, row 3 column names)."""
    import pandas as pd
    try:
        df = pd.read_excel(path, sheet_name="SUMMARY", header=3)
    except Exception as e:
        return {}, f"SUMMARY unreadable: {e}"
    cols = {str(c).strip().lower(): c for c in df.columns}
    tcol = cols.get("ticker")
    scol = next((cols[k] for k in cols if k in ("source score", "screen_source", "source")), None)
    if not tcol or not scol:
        return {}, f"no ticker/source column (saw {list(cols)[:8]})"
    out = {}
    for _, r in df.iterrows():
        tk, sv = r.get(tcol), r.get(scol)
        if not isinstance(tk, str) or not tk.strip():
            continue
        try:
            out[tk.strip()] = float(str(sv).replace("%", "").strip())
        except (TypeError, ValueError):
            continue
    return out, None


def recover(here=HERE, apply=False, panel="score_panel.csv"):
    import pandas as pd
    ppath = os.path.join(here, panel)
    d = pd.read_csv(ppath)
    before = int(d.source_score.isna().sum())
    gaps = d[d.source_score.isna()].groupby(["run_date", "group"]).size().to_dict()

    books = sorted(glob.glob(os.path.join(here, "Growth Stock Analysis*.xlsx"))) + \
        sorted(glob.glob(os.path.join(here, "archive", "*", "Growth Stock Analysis*.xlsx")))
    filled, per_run, skipped = 0, {}, []
    for b in books:
        rd, gp = parse_workbook_name(b)
        if not rd or (rd, gp) not in gaps:
            continue
        src, err = read_summary_source(b)
        if err:
            skipped.append({"workbook": os.path.basename(b), "reason": err})
            continue
        mask = (d.run_date == rd) & (d.group == gp) & (d.source_score.isna()) & (d.ticker.isin(src))
        n = int(mask.sum())
        if n:
            d.loc[mask, "source_score"] = d.loc[mask, "ticker"].map(src)
            filled += n
            per_run[f"{rd}|{gp}"] = {"recovered": n, "still_missing": int(gaps[(rd, gp)]) - n,
                                     "summary_names_available": len(src)}
    after = before - filled
    man = {"schema_version": 1, "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "method": "recovered stored point-in-time 'Source Score' from workbook SUMMARY sheets",
           "explicitly_not_done": "recomputation from components — SOURCE_WEIGHTS changed 29-Jul-2026 (WP-M), "
                                  "so a recomputed June value would not be the value June's screen assigned",
           "missing_before": before, "recovered": filled, "missing_after": after,
           "irrecoverable_reason": "SUMMARY holds only the SUMMARY-eligible selection (~15/run); for all other "
                                   "scored names the Source Score was never written to any retained artefact",
           "per_run": per_run, "skipped_workbooks": skipped, "applied": bool(apply)}
    if apply and filled:
        tmp = ppath + ".tmp"
        d.to_csv(tmp, index=False)
        os.replace(tmp, ppath)
    with open(os.path.join(here, "m1_source_score_recovery_manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    return man


def _selftest():
    import tempfile, pandas as pd
    with tempfile.TemporaryDirectory() as td:
        pd.DataFrame({"run_date": ["2026-06-27"] * 3, "group": ["STOXX600"] * 3,
                      "ticker": ["AAA", "BBB", "CCC"], "source_score": [None, None, 71.0],
                      "total_score": [40, 41, 42]}).to_csv(os.path.join(td, "score_panel.csv"), index=False)
        wb = os.path.join(td, "Growth Stock Analysis Stoxx 600 W-e 27-Jun-26.xlsx")
        with pd.ExcelWriter(wb) as w:
            head = pd.DataFrame([["title"], [None], ["bands"], ["Ticker", "Source Score"],
                                 ["AAA", 83.4]])
            head.to_excel(w, sheet_name="SUMMARY", header=False, index=False)
        rd, gp = parse_workbook_name(wb)
        assert (rd, gp) == ("2026-06-27", "STOXX600"), f"filename parse failed: {rd} {gp}"
        m = recover(td, apply=True)
        assert m["recovered"] == 1, f"expected 1 recovered, got {m['recovered']}"
        assert m["missing_after"] == 1, "BBB has no SUMMARY row and must stay missing"
        out = pd.read_csv(os.path.join(td, "score_panel.csv"))
        assert float(out[out.ticker == "AAA"].source_score.iloc[0]) == 83.4, "value not written"
        assert pd.isna(out[out.ticker == "BBB"].source_score.iloc[0]), "unrecoverable row was invented"
        assert float(out[out.ticker == "CCC"].source_score.iloc[0]) == 71.0, "existing value overwritten"
    print("SELFTEST PASS — 6 assertions (filename parse, recovery, unrecoverable left null, "
          "existing value untouched, manifest counts)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    m = recover(a.dir, apply=a.apply)
    print(f"M1 RECOVERY {'APPLIED' if a.apply else 'DRY-RUN'}: "
          f"missing {m['missing_before']} -> {m['missing_after']} (recovered {m['recovered']})")
    for k, v in m["per_run"].items():
        print(f"  {k}: +{v['recovered']} recovered, {v['still_missing']} still missing "
              f"(SUMMARY held {v['summary_names_available']} names)")
    if m["skipped_workbooks"]:
        print(f"  skipped: {len(m['skipped_workbooks'])} workbook(s) — see manifest")
    print(f"  irrecoverable: {m['irrecoverable_reason']}")


if __name__ == "__main__":
    main()
