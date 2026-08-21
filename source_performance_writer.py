#!/usr/bin/env python3
"""
source_performance_writer.py -- ISA-0370. THE WRITER `source_performance_log.json` NEVER HAD.

The log is read at the start of every weekly screen to reorder the source waterfall, and it is
read by `degradation_bands.py` to derive every R15.2 coverage floor. A grep across the tree on
19-Aug-2026 found it READ by four modules and WRITTEN by none: the only instruction to update it
was a prose line in each weekly SKILL.md ("...and any updated source_performance_log.json"), so
every count in it was typed by hand, once a week, by whoever ran the screen.

Two consequences, both measured:

  * the counts are not the run's. The log says `overlay_quotesummary` ran at 42.1% on the 25-Jul
    STOXX600 screen; the 25-Jul frame shows overlay_status='complete' on 19 of 19 enriched rows.
  * a metric with no row is not merely unreported -- it is removed from the waterfall's field of
    view. `forward_pe` and `ev_ebitda` have no row for FTSE 250 or SPI and were therefore read as
    "never measured" (ISA-0349), when in fact they are 97% and 96% populated on the 08-Aug frame.
    The hole was in the LOG, not the data.

R4.11 already says capture is a PROPERTY of producing the artefact, and `save_full_data` was
converted to that model on 05-Aug for the SectionQ capture. This module does the same job for
source performance: it MEASURES coverage from the frame the run just produced, and it carries a
CONTROL that fails when a metric the run populated has no row in the log.

Every row this module writes is stamped `basis="measured_from_frame"` with the frame it was
measured from; rows it did not write keep `basis="hand_entered"`. A number that cannot say where
it came from does not get to sit next to one that can.

CLI
    python3 source_performance_writer.py --frame 20260815_SP500_full_data.csv --index "S&P 500"
    python3 source_performance_writer.py --frame ... --index ... --write
    python3 source_performance_writer.py --selftest
"""
from __future__ import annotations
import argparse, csv, datetime as dt, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "source_performance_log.json")

# ── ONE HOME for metric -> frame column. `degradation_bands.py` and the waterfall both speak
# the log's metric names; the frame speaks the screener's column names. The translation lives
# here and nowhere else (R-STD-5). A metric whose column is absent from a frame is UNMEASURABLE
# on that frame -- which is a different fact from a metric measured at 0% and is reported as such.
METRIC_COLUMN = {
    "sector":              "sector",
    "gross_margin":        "gross_margin",
    "revenue_cagr":        "rev_cagr",
    "fcf_positivity":      "fcf_positive_years",
    "fcf_margin":          "fcf_margin",
    "roic":                "roic",
    "forward_pe":          "fwd_pe",
    "ev_ebitda":           "ev_ebitda",
    "net_debt_ebitda":     "net_debt_ebitda",
    "target_price":        "target_price_mean",
    "52w_range":           "position_52wk",
}
# Overlay coverage is a STATUS column, not a value column: success means the overlay resolved,
# not that some number is non-null.
OVERLAY_METRIC = "overlay_quotesummary"
OVERLAY_COLUMN = "overlay_status"
OVERLAY_SUCCESS = {"complete"}

_MISSING = {"", "none", "nan", "null", "na", "n/a", "-"}

# ONE HOME for the frame's short index code -> the log's index name. A combined frame
# (FTSE 250 + SPI ship in one file) MUST be split before coverage is recorded: pooling them
# is what makes an index-specific breach invisible, and the 08-Aug FTSE 250 sector breach
# ISA-0349 found is exactly that shape.
INDEX_ALIAS = {
    "SP500": "S&P 500", "NASDAQ": "Nasdaq 100", "MIDCAP400": "S&P MidCap 400",
    "FTSE100": "FTSE 100", "FTSE250": "FTSE 250", "SX5E": "EURO STOXX 50",
    "STOXX600": "STOXX Europe 600", "SMI": "SMI 20", "SPI": "SPI",
    "TSX": "S&P/TSX Composite", "IBRX50": "IBrX 50", "IPC35": "S&P/BMV IPC 35",
}


def _populated(v) -> bool:
    return str(v).strip().lower() not in _MISSING


def read_frame(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def frame_run_date(path: str, rows=None) -> str:
    """The run date, taken from the frame's filename (yyyymmdd prefix) -- never today's date.
    A coverage figure stamped with the date it was WRITTEN rather than the date it was TRUE is
    the semantic-drift class this project has hit repeatedly."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
    if not m:
        raise ValueError("cannot read a run date from %r -- refusing to stamp coverage with "
                         "today's date" % os.path.basename(path))
    return "%s-%s-%s" % m.groups()


def measure(frame_path: str, index_code: str | None = None) -> dict:
    """Per-metric attempts/successes MEASURED from a retained frame.

    attempts  = rows in the frame (every scored name was an attempt at every metric)
    successes = rows whose column is populated
    A metric whose column is absent from the frame returns status='column_absent' and is NOT
    counted as a 0% success rate -- absent and zero are opposite facts.
    """
    rows = read_frame(frame_path)
    if index_code is not None:
        rows = [r for r in rows if (r.get("index") or "").strip() == index_code]
    if not rows:
        raise ValueError("frame %r has no rows%s" % (frame_path,
                         "" if index_code is None else " for index %r" % index_code))
    cols = set(rows[0].keys())
    out = {}
    for metric, col in METRIC_COLUMN.items():
        if col not in cols:
            out[metric] = {"status": "column_absent", "column": col,
                           "attempts": None, "successes": None, "rate": None}
            continue
        succ = sum(1 for r in rows if _populated(r.get(col)))
        out[metric] = {"status": "measured", "column": col, "attempts": len(rows),
                       "successes": succ, "rate": round(succ / len(rows), 4)}
    if OVERLAY_COLUMN in cols:
        att = [r for r in rows if _populated(r.get(OVERLAY_COLUMN))]
        succ = sum(1 for r in att if str(r.get(OVERLAY_COLUMN)).strip().lower() in OVERLAY_SUCCESS)
        out[OVERLAY_METRIC] = {"status": "measured", "column": OVERLAY_COLUMN,
                               "attempts": len(att), "successes": succ,
                               "rate": (round(succ / len(att), 4) if att else None)}
    else:
        out[OVERLAY_METRIC] = {"status": "column_absent", "column": OVERLAY_COLUMN,
                               "attempts": None, "successes": None, "rate": None}
    return {"frame": os.path.basename(frame_path), "run_date": frame_run_date(frame_path),
            "index_code": index_code, "rows": len(rows), "metrics": out}


# ── THE CONTROL ───────────────────────────────────────────────────────────────────────────────
# ISA-0370's failure mode is silence: a metric the run obtained but never logged simply drops out
# of the waterfall's field of view and the run proceeds as though the metric were never wanted.
# The control turns that silence into a failure.
UNLOGGED_MIN_COVERAGE = 0.50   # a metric this well populated in the frame is plainly being obtained


def unlogged_metrics(frame_path: str, index_name: str, log=None, index_code=None) -> list:
    log = log if log is not None else load_log()
    logged = set((log.get("indices", {}).get(index_name) or {}).keys())
    m = measure(frame_path, index_code)
    out = []
    for metric, s in m["metrics"].items():
        if s["status"] != "measured" or s["rate"] is None:
            continue
        if s["rate"] >= UNLOGGED_MIN_COVERAGE and metric not in logged:
            out.append("%s / %s: populated on %.1f%% of %d rows in %s but has NO ROW in the log, "
                       "so the waterfall cannot see it" %
                       (index_name, metric, 100 * s["rate"], s["attempts"], m["frame"]))
    return out


def load_log(path=None) -> dict:
    with open(path or LOG_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def record(frame_path: str, index_name: str, log=None, index_code=None) -> dict:
    """Fold one frame's MEASURED coverage into the log. Returns the updated log (does not write).

    Lifetime counts accumulate, as the file's own contract requires ("never reset counts"), but a
    frame is folded in ONCE: re-recording the same frame for the same index is a no-op, because a
    writer that double-counts on a re-run is how a hand-maintained file drifts in the first place.
    """
    log = json.loads(json.dumps(log if log is not None else load_log()))
    m = measure(frame_path, index_code)
    idx = log.setdefault("indices", {}).setdefault(index_name, {})
    for metric, s in m["metrics"].items():
        if s["status"] != "measured":
            continue
        row = idx.setdefault(metric, {"primary_source": "yfinance", "attempts": 0,
                                      "successes": 0, "basis": "measured_from_frame"})
        if row.get("last_frame") == m["frame"] and row.get("last_index_code") == m["index_code"]:
            continue                      # already folded in -- idempotent per (frame, index)
        row["attempts"] = int(row.get("attempts") or 0) + s["attempts"]
        row["successes"] = int(row.get("successes") or 0) + s["successes"]
        row["success_rate"] = round(row["successes"] / row["attempts"], 4) if row["attempts"] else None
        row["last_run"] = m["run_date"]
        row["last_run_attempts"] = s["attempts"]
        row["last_run_successes"] = s["successes"]
        row["last_run_rate"] = s["rate"]
        # R-STD-2: every figure states when it was true and where it came from.
        row["basis"] = "measured_from_frame"
        row["last_frame"] = m["frame"]
        row["last_index_code"] = m["index_code"]
        row["measured_column"] = s["column"]
    # any row this writer has never touched is still hand-entered, and says so
    for metric, row in idx.items():
        row.setdefault("basis", "hand_entered")
    log["last_updated"] = m["run_date"]
    log.setdefault("_basis_note", (
        "Rows with basis='measured_from_frame' were computed by source_performance_writer.py from "
        "the named retained frame. Rows with basis='hand_entered' predate that writer (ISA-0370) "
        "and were typed up by hand from a prose step in the weekly SKILL.md; they are NOT "
        "measurements and no band should be derived from them without saying so."))
    return log


def write(log: dict, path=None) -> str:
    path = path or LOG_FILE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=2, ensure_ascii=False)
    return path


# ── selftest ──────────────────────────────────────────────────────────────────────────────────
def selftest(verbose=True) -> int:
    fails = []
    def ok(cond, msg):
        if verbose: print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond: fails.append(msg)

    import tempfile
    rows = [{"ticker": "A", "sector": "Tech", "fwd_pe": "20", "roic": "0.2", "overlay_status": "complete"},
            {"ticker": "B", "sector": "",     "fwd_pe": "",   "roic": "0.1", "overlay_status": "partial"},
            {"ticker": "C", "sector": "Tech", "fwd_pe": "18", "roic": "",    "overlay_status": ""}]
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "20260815_TEST_full_data.csv")
    with open(fp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    m = measure(fp)
    ok(m["run_date"] == "2026-08-15", "the run date comes from the FRAME, not from today")
    ok(m["metrics"]["sector"]["successes"] == 2 and m["metrics"]["sector"]["attempts"] == 3,
       "sector measured 2/3 from the frame")
    ok(m["metrics"]["forward_pe"]["rate"] == round(2/3, 4), "forward_pe measured 2/3 -- the metric ISA-0349 called 'never measured'")
    ok(m["metrics"]["gross_margin"]["status"] == "column_absent"
       and m["metrics"]["gross_margin"]["rate"] is None,
       "an ABSENT column is 'column_absent', never a 0% success rate (absent != zero)")
    ok(m["metrics"][OVERLAY_METRIC]["attempts"] == 2 and m["metrics"][OVERLAY_METRIC]["successes"] == 1,
       "overlay coverage counts STATUS, and an unstamped row is not an attempt")

    # NEGATIVE CONTROL 1 -- the control must fire on a metric the frame populated and the log omits
    empty = {"indices": {"TEST": {"sector": {"attempts": 1, "successes": 1}}}}
    hits = unlogged_metrics(fp, "TEST", log=empty)
    ok(any("forward_pe" in h for h in hits),
       "NEGATIVE CONTROL: a metric populated in the frame with no row in the log FAILS the control")
    ok(not any("sector" in h for h in hits), "a metric that IS logged does not fire the control")
    ok(unlogged_metrics(fp, "TEST", log={"indices": {"TEST": {k: {} for k in list(METRIC_COLUMN) + [OVERLAY_METRIC]}}}) == [],
       "NEGATIVE CONTROL: with every metric logged the control is silent -- so it can pass as well as fail")

    # NEGATIVE CONTROL 2 -- folding the same frame twice must not double-count
    l1 = record(fp, "TEST", log={"indices": {}})
    l2 = record(fp, "TEST", log=l1)
    ok(l1["indices"]["TEST"]["sector"]["attempts"] == 3, "first fold accumulates the frame's rows")
    ok(l2["indices"]["TEST"]["sector"]["attempts"] == 3,
       "NEGATIVE CONTROL: re-recording the SAME frame is a no-op, not a double count")
    l3 = record(fp, "TEST", log={"indices": {"TEST": {"sector": {"attempts": 100, "successes": 90}}}})
    ok(l3["indices"]["TEST"]["sector"]["attempts"] == 103,
       "a DIFFERENT prior state does accumulate -- so the idempotence check can fail")
    ok(l3["indices"]["TEST"]["sector"]["basis"] == "measured_from_frame"
       and l3["indices"]["TEST"]["sector"]["last_frame"] == "20260815_TEST_full_data.csv",
       "every written row states the frame it was measured from (R-STD-2)")

    # NEGATIVE CONTROL 3 -- a frame with no date in its name must REFUSE
    bad = os.path.join(d, "no_date_full_data.csv")
    with open(bad, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    try:
        frame_run_date(bad); ok(False, "NEGATIVE CONTROL: an undateable frame must refuse")
    except ValueError:
        ok(True, "NEGATIVE CONTROL: an undateable frame refuses rather than stamping today")

    if verbose:
        print("\nsource_performance_writer selftest: %d failure(s)%s"
              % (len(fails), "" if fails else " -- all assertions green"))
    return 1 if fails else 0


def rebuild(out_path=None) -> int:
    """Build the log from RETAINED FRAMES ONLY.

    The hand-entered counts are not merely unmeasured, they are contradicted: the log carries
    FTSE 250 `sector` at 84.6% lifetime and 75.1% last run, and the 08-Aug frame it refers to has
    the column populated on 73 of 73 rows. Accumulating measured counts on top of contradicted
    ones would launder them, so a rebuild starts clean -- and MOVES the old rows into
    `_superseded_hand_entered` rather than deleting them, because they are the evidence for
    ISA-0370 and for whatever the waterfall did while they were live.
    """
    import glob
    old = load_log()
    new = {k: v for k, v in old.items() if not k.startswith("indices")}
    new["indices"] = {}
    new["_superseded_hand_entered"] = old.get("indices", {})
    new["_rebuild_note"] = (
        "ISA-0370. Every row below was MEASURED from a retained *_full_data.csv frame by "
        "source_performance_writer.py. The rows this replaced were typed by hand from a prose "
        "step in the weekly SKILL.md and are preserved under _superseded_hand_entered. Lifetime "
        "counts here cover only the frames still on disk, so they are SHALLOWER than the hand "
        "counts and any floor derived from them must say so.")
    frames = sorted(glob.glob(os.path.join(HERE, "2026*_full_data.csv")))
    used = []
    for fp in frames:
        for code in sorted({(r.get("index") or "").strip() for r in read_frame(fp)} - {""}):
            name = INDEX_ALIAS.get(code)
            if not name:
                continue
            new = record(fp, name, log=new, index_code=code)
            used.append((os.path.basename(fp), code, name))
    new["_rebuilt_from"] = [{"frame": f, "index_code": c, "index": n} for f, c, n in used]
    new["frames_used"] = len(frames)
    p = write(new, out_path or os.path.join(HERE, "source_performance_log_measured.json"))
    print("rebuilt from %d frames / %d (frame,index) pairs -> %s" % (len(frames), len(used), p))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame"); ap.add_argument("--index")
    ap.add_argument("--index-code", default=None,
                    help="restrict to one value of the frame's `index` column (a combined "
                         "FTSE250+SPI frame MUST be split, or an index-specific breach is pooled away)")
    ap.add_argument("--auto", action="store_true",
                    help="split the frame by its `index` column and record every index in it")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--control", action="store_true", help="run the unlogged-metric control only")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="build a log from the retained frames ONLY, quarantining (never deleting) "
                         "the hand-entered rows it supersedes")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.rebuild:
        return rebuild(a.out)
    if a.auto:
        if not a.frame:
            ap.error("--auto requires --frame")
        codes = sorted({(r.get("index") or "").strip() for r in read_frame(a.frame)} - {""})
        log = load_log()
        rc = 0
        for code in codes:
            name = INDEX_ALIAS.get(code)
            if not name:
                print("SKIP  index code %r has no INDEX_ALIAS entry -- refusing to guess" % code)
                rc = 1
                continue
            mm = measure(a.frame, code)
            hits = unlogged_metrics(a.frame, name, log=log, index_code=code)
            print("%-18s %-22s rows=%4d  %s" % (code, name, mm["rows"],
                  "  ".join("%s=%s" % (k, ("--" if v["rate"] is None else "%.1f%%" % (100*v["rate"])))
                            for k, v in sorted(mm["metrics"].items()))))
            for h in hits:
                print("   UNLOGGED: " + h)
            log = record(a.frame, name, log=log, index_code=code)
        if a.write:
            print("\nwritten: " + write(log))
        return rc
    if not (a.frame and a.index):
        ap.error("--frame and --index are required")
    m = measure(a.frame, a.index_code)
    print(json.dumps(m, indent=2))
    hits = unlogged_metrics(a.frame, a.index, index_code=a.index_code)
    if hits:
        print("\nUNLOGGED METRICS (ISA-0370 control):")
        for h in hits: print("  - " + h)
    if a.control:
        return 1 if hits else 0
    if a.write:
        p = write(record(a.frame, a.index, index_code=a.index_code))
        print("\nwritten: " + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
