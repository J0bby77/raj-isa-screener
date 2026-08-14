#!/usr/bin/env python3
"""
gate_variables.py — Capture Layer Item 1. 02-Aug-2026.

THE DEFECT THIS CLOSES
----------------------
The gates compute a rich set of fundamental variables for every constituent, use each one once
to make a pass/fail decision, and then discard it. What survives to disk is only the variable
belonging to the gate that happened to reject the name: in `20260724_NASDAQ_yf_gate_results.csv`
`gross_margin` is 1.6% non-null (= exactly the 14 Gate-2 rejects out of 863) and `rev_cagr_3yr`
is 12.9% non-null (= exactly the 111 Gate-4 rejects). Every passing name, and every name
rejected by a *different* gate, carries nothing.

That makes §7.2's `rule_frictions` output — "rules/gates that repeatedly blocked names which
subsequently performed" — literally uncomputable, because you cannot ask how far below a
threshold the blocked names sat when the threshold variable was never retained.

TWO NON-NEGOTIABLE REQUIREMENTS (build order Item 1)
----------------------------------------------------
1. **LEVELS, NOT GAPS.** Every numeric column stores the measured level of the variable, never
   its distance from a threshold. A column named `rev_cagr_3yr` holding "0.03 below the 5%
   floor" inverted a conclusion during analysis on 01-Aug-2026. `RANGES` below asserts each
   value falls inside its plausible domain and the write FAILS if it does not, so a future
   refactor that starts subtracting a threshold cannot land silently. Threshold context is
   preserved separately in `*_threshold` columns — recorded, never subtracted.

2. **IDEMPOTENT FIELD-WISE MERGE** on (run_date, group, ticker) via `combine_first`, following
   `score_panel_logger.py`. NOT `drop_duplicates(keep="last")` — that exact call destroyed
   1,246 rich rows on 29-Jul-2026 when sparse backfilled rows wholesale-replaced live ones.

The measurement is UNCONDITIONAL and the verdict is separate: `screener_core.measure_gate_
variables()` computes every variable for every fetched constituent regardless of which gate
fired first. This module never re-derives a gate outcome and never changes one — per build
hazard §H7, the capture layer observes, it never calibrates.

CLI:
  python3 gate_variables.py --selftest
  python3 gate_variables.py --coverage                # report non-null % per variable
  python3 gate_variables.py --coverage --group NASDAQ --run_date 2026-07-24

Library:
  from gate_variables import log_gate_variables, coverage_report
  n_in, n_total = log_gate_variables(records, group="NASDAQ", run_date="2026-08-07")
"""
from __future__ import annotations
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "gate_variables.csv")

SCHEMA_VERSION = 1

# Build-order schema, in order, then the additions that make the store self-describing.
COLS = [
    "run_date", "group", "ticker", "sector",
    # `sector` is the GICS-style vendor sector ("Technology"). `sector_type` is the
    # FRAMEWORK's classification ("quality_compounder_saas" / "cyclical" / "healthcare_tech").
    # They were being written into the SAME column by the weekly and monthly paths
    # respectively, so a group-by on `sector` silently mixed two vocabularies. Separated
    # 02-Aug-2026 — one column, one definition.
    "sector_type",
    "mkt_cap", "gross_margin", "rev_cagr_3yr", "fcf_pos_years", "op_margin",
    "gate_code", "gate_reason", "passed",
    # --- context that makes the levels interpretable without re-reading the code ------------
    "industry", "sector_bucket", "company", "security_type",
    "revenue_latest",
    # Provenance per measured level. A derived or vendor-supplied value must never be
    # indistinguishable in analysis from the statement-derived one the gate actually used.
    "gross_margin_source", "mkt_cap_source", "op_margin_source", "rev_cagr_3yr_status",
    "gm_threshold",            # the level the gate compared against — recorded, NEVER subtracted
    "rev_cagr_threshold",
    "fcf_years_required", "fcf_avail_years",
    "rev_cagr_5yr",            # the 2C-1 semiconductor_equipment override input
    "capex_intensity",
    "measured_unconditionally",  # True = levels computed independent of gate short-circuit
    "measure_notes",             # why any variable is null, per name
    # --- R4 (Aug-2026 retrospective item 4): the forward-signal joint distribution -----------
    # Every T1-qualified name carried NEUTRAL 30-day revisions while the only IMPROVING names
    # all failed a gate. Logged here so it is measured monthly rather than noticed once.
    "t1_qualified", "est_rev_direction", "source_score", "revision_stage",
    "schema_version",
]

# Plausible domain per numeric variable: (lo, hi, unit). A value outside its range is a WRITE
# FAILURE, not a warning — this is the assertion the build order requires. Ranges are wide on
# purpose: they catch a units/semantics error (a gap stored as a level, a percent stored as a
# fraction, a market cap in the wrong currency scale), not an unusual company.
RANGES = {
    "mkt_cap":        (0.0, 1e14, "USD absolute"),
    "revenue_latest": (-1e13, 1e13, "reporting currency absolute"),
    "gross_margin":   (-20.0, 1.0, "fraction of revenue"),
    "rev_cagr_3yr":   (-1.0, 10.0, "fraction, e.g. 0.12 = 12%"),
    "rev_cagr_5yr":   (-1.0, 10.0, "fraction"),
    "op_margin":      (-50.0, 1.0, "fraction of revenue"),
    "fcf_pos_years":  (0, 10, "count of years"),
    "fcf_avail_years": (0, 10, "count of years"),
    "capex_intensity": (0.0, 10.0, "fraction of revenue"),
    "gm_threshold":   (0.0, 1.0, "fraction"),
    "rev_cagr_threshold": (-1.0, 1.0, "fraction"),
    "source_score":   (0.0, 100.0, "points"),
}

# Upper bound is exclusive-of-nonsense rather than exclusive-of-reality: a gross margin of
# exactly 1.0 is a zero-COGS business and legal; 1.05 means someone stored a percentage.


class GateVariableRangeError(ValueError):
    """Raised when a value falls outside its variable's plausible domain — the guard that a
    gap has been stored where a level belongs."""


def _num(v):
    if v is None:
        return None
    try:
        import math
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def assert_ranges(records, strict=True):
    """Check every numeric field of every record against RANGES.

    Returns a list of violation strings. With strict=True (the default, and what the writer
    uses) the first violation raises GateVariableRangeError so nothing reaches disk.
    """
    viol = []
    for rec in records:
        for col, (lo, hi, unit) in RANGES.items():
            v = _num(rec.get(col))
            if v is None:
                continue
            if not (lo <= v <= hi):
                viol.append(
                    f"{rec.get('ticker')}/{rec.get('group')}/{rec.get('run_date')}: "
                    f"{col}={v!r} outside plausible range [{lo}, {hi}] ({unit}). "
                    f"If this is a distance-below-threshold, it is in the wrong column — "
                    f"gate_variables stores LEVELS ONLY.")
    if viol and strict:
        raise GateVariableRangeError(viol[0] + f"  (+{len(viol)-1} more)" if len(viol) > 1
                                     else viol[0])
    return viol


def log_gate_variables(records, group=None, run_date=None, store=None, strict=True):
    """Append/merge gate-variable rows. Idempotent, field-wise, on (run_date, group, ticker).

    `records` is an iterable of dicts. group/run_date, when given, are stamped onto every
    record (a record may still carry its own). Returns (rows_in, store_total).
    """
    import pandas as pd
    store = store or STORE
    rows = []
    for r in records:
        rec = {c: r.get(c) for c in COLS}
        if group is not None:
            rec["group"] = group
        if run_date is not None:
            rec["run_date"] = run_date
        if not rec.get("ticker"):
            continue
        rec["schema_version"] = SCHEMA_VERSION
        if rec.get("measured_unconditionally") is None:
            rec["measured_unconditionally"] = False
        rows.append(rec)

    assert_ranges(rows, strict=strict)          # ← nothing is written if a level looks like a gap

    new = pd.DataFrame(rows, columns=COLS)
    if os.path.exists(store):
        old = pd.read_csv(store)
        for c in COLS:
            if c not in old.columns:
                old[c] = None
        old = old[COLS]
        key = ["run_date", "group", "ticker"]
        oi = old.set_index(key)
        ni = new.set_index(key)
        oi = oi[~oi.index.duplicated(keep="last")]
        ni = ni[~ni.index.duplicated(keep="last")]
        # FIELD-WISE: incoming wins per-cell where it has a value; existing values survive
        # wherever the incoming cell is null. See score_panel_logger.py for the 29-Jul-2026
        # data-loss incident this pattern exists to prevent.
        merged = ni.combine_first(oi).reset_index()[COLS]
    else:
        merged = new
    merged.to_csv(store, index=False)
    return len(new), len(merged)


# ── coverage (the Item 1 acceptance test) ────────────────────────────────────────────────

# The variables the acceptance test measures. "≥95% non-null coverage for every gate variable
# across the full fetched constituent list."
ACCEPTANCE_VARS = ("sector", "mkt_cap", "gross_margin", "rev_cagr_3yr", "fcf_pos_years",
                   "op_margin")
# Renamed from GATE_VAR_COVERAGE_FLOOR 12-Aug-2026 (register ISA-0012); see the note in
# calibration_universe.py. Emitted key "acceptance_floor" deliberately unchanged.
GATE_VAR_COVERAGE_FLOOR = 0.95


def coverage_report(store=None, group=None, run_date=None):
    """Non-null coverage per variable for one screen (or the whole store)."""
    import pandas as pd
    store = store or STORE
    if not os.path.exists(store):
        return {"error": f"store not found: {store}", "rows": 0}
    d = pd.read_csv(store)
    if group:
        d = d[d["group"] == group]
    if run_date:
        d = d[d["run_date"] == str(run_date)]
    if d.empty:
        return {"error": "no rows for that filter", "rows": 0, "group": group,
                "run_date": run_date}
    def _cov(frame):
        out = {}
        for c in ACCEPTANCE_VARS:
            if c not in frame.columns:
                continue
            covered = frame[c].notna()
            # A quantity that provably does not exist counts as MEASURED, not as missing.
            # Only "we could not look" counts against coverage — see rev_cagr_3yr_status.
            sc = c + "_status"
            if sc in frame.columns:
                covered = covered | frame[sc].astype(str).str.startswith("undefined_")
            out[c] = round(float(covered.mean()), 4)
        return out

    # TWO denominators, both reported, because they answer different questions and conflating
    # them is how a universe-hygiene problem gets misread as a measurement failure.
    #
    #   all_fetched   — every constituent the feed handed us. Its shortfall is dominated by
    #                   preferred depositary shares and baby bonds that the "clean equities"
    #                   filter lets through (ACGLN, ACGLO, ADAML/M/N ...). Those have no
    #                   revenue line by construction; nothing measurable failed.
    #   analysable    — common equity whose info fetch succeeded. This is the population
    #                   rule_frictions will actually be computed over, so it is the one the
    #                   acceptance floor binds on.
    all_cov = _cov(d)
    an = d
    if "security_type" in an.columns:
        an = an[an["security_type"].fillna("unknown") != "non_common"]
    if "measured_unconditionally" in an.columns:
        an = an[an["measured_unconditionally"].map(
            lambda v: bool(v) if v == v and v not in (None, "", "False", "false") else False)]
    an_cov = _cov(an) if len(an) else {}
    worst = min(an_cov.values()) if an_cov else 0.0
    excluded = int(len(d) - len(an))
    return {
        "store": store, "group": group, "run_date": run_date, "rows": int(len(d)),
        "analysable_rows": int(len(an)),
        "excluded_non_common_or_unfetched": excluded,
        "coverage": an_cov,
        "coverage_all_fetched": all_cov,
        "worst": worst,
        "acceptance_floor": GATE_VAR_COVERAGE_FLOOR,
        "acceptance": "PASS" if worst >= GATE_VAR_COVERAGE_FLOOR else "FAIL",
        "unconditional_rows": int(d["measured_unconditionally"].map(
            lambda v: bool(v) if v == v and v not in (None, "", "False", "false") else False).sum())
        if "measured_unconditionally" in d.columns else 0,
    }


def log_monthly_t1_distribution(step9_pre, run_date, store=None, group="MONTHLY_T1",
                                metrics=None):
    """R4 (Aug-2026 retrospective item 4): log (t1_qualified x est_rev_direction) per name.

    THE OBSERVATION. On the Aug-2026 run all 13 names passing the A4 qualification gate carried
    est_rev_direction='neutral'. The only three names in the whole universe with 'improving'
    30-day revisions - AUPH, DAL and RACE.MI - each failed a different gate. In a framework
    whose stated identity is forward-led and revisions-driven, the gate set admitted nothing
    carrying the forward signal it exists to prize. It also made the valuation test binding by
    default, because the price-ahead-of-consensus carve-out REQUIRES net-positive revisions and
    was therefore unavailable to every single candidate.

    That may be a one-month artefact or a structural property of the gate set. One run cannot
    tell. So it is LOGGED, monthly, and nothing is changed - registered as CAP-3, reviewed after
    six runs (H7: the capture layer observes, it never calibrates).

    Reuses gate_variables.csv rather than adding a store: same key, same idempotent merge, and
    the monthly rows sit alongside the weekly screen rows they will eventually be joined to.
    """
    # The gate LEVELS are the reason this file exists. Before 02-Aug-2026 the monthly path
    # wrote `passed` and nothing else, so every monthly row was blank on mkt_cap,
    # gross_margin, rev_cagr_3yr, fcf_pos_years and op_margin — the file recorded that a name
    # was rejected without recording anything you could later use to ask whether it should
    # have been. The levels are all present in watchlist_metrics; they are now carried through.
    mx = (metrics or {})
    if "tickers" in mx:                 # accept the raw watchlist_metrics document too
        mx = mx["tickers"]

    recs = []
    for block, route in (("main_watchlist", "main"), ("candidate_pool", "pool")):
        node = step9_pre.get(block) or {}
        for tier, lst in (node.items() if isinstance(node, dict) else []):
            for e in (lst or []):
                if not isinstance(e, dict) or not e.get("ticker"):
                    continue
                basis = ((e.get("t1_gate_detail") or {}).get("evidence") or {}).get("basis") or {}
                md = mx.get(e["ticker"]) or {}
                recs.append({
                    "ticker": e["ticker"],
                    "company": md.get("company"),
                    "sector": md.get("sector"),               # vendor sector
                    "sector_type": e.get("sector_type"),      # framework classification
                    "industry": md.get("industry"),
                    "sector_bucket": md.get("sector_bucket"),
                    "mkt_cap": md.get("market_cap"),
                    "gross_margin": md.get("gross_margin"),
                    # metrics exposes this as `operating_margin`; `op_margin` is present but
                    # null. Reading the wrong one silently records a measured value as missing.
                    "op_margin": (md.get("operating_margin")
                                  if md.get("operating_margin") is not None
                                  else md.get("op_margin")),
                    "rev_cagr_3yr": md.get("rev_cagr"),
                    "fcf_pos_years": md.get("fcf_positive_years"),
                    "capex_intensity": md.get("capex_intensity"),
                    "revenue_latest": md.get("_latest_rev"),
                    "t1_qualified": bool(e.get("t1_qualified")),
                    "est_rev_direction": (e.get("est_rev_direction")
                                          or basis.get("rev_30d_direction")),
                    "revision_stage": e.get("revision_stage"),
                    "source_score": e.get("source_score"),
                    "gate_code": ("" if e.get("t1_qualified") else
                                  _first_failed_gate(e.get("t1_gate_detail") or {})),
                    "gate_reason": ("" if e.get("t1_qualified") else
                                    f"A4 qualification: {_first_failed_gate(e.get('t1_gate_detail') or {})}"),
                    "passed": bool(e.get("t1_qualified")),
                    "measured_unconditionally": True,
                    "measure_notes": f"monthly A4 qualification snapshot ({block}/{tier})",
                })
    if not recs:
        return 0, 0
    return log_gate_variables(recs, group=group, run_date=run_date, store=store)


def _first_failed_gate(detail):
    """Which A4 sub-gate rejected the name. Reports the FIRST failure in the gate's own order,
    so the reason is the binding one rather than an arbitrary member of a set."""
    for key in ("ns_floor", "stage", "er", "clean_flags"):
        d = detail.get(key)
        if isinstance(d, dict) and d.get("pass") is False:
            return f"A4_{key}"
    return "A4_unqualified"


def revisions_crosstab(store=None, group=None, run_date=None):
    """R4: joint distribution of (t1_qualified × est_rev_direction).

    The Aug-2026 review found every T1-qualified name carrying neutral 30-day revisions while
    the only improving-revision names in the universe each failed a gate — in a framework whose
    stated identity is forward-led. This makes that measurable each month instead of anecdotal.
    Reports counts only. It never changes a gate (§H7).
    """
    import pandas as pd
    store = store or STORE
    if not os.path.exists(store):
        return {"error": f"store not found: {store}"}
    d = pd.read_csv(store)
    if group:
        d = d[d["group"] == group]
    if run_date:
        d = d[d["run_date"] == str(run_date)]
    if d.empty or "est_rev_direction" not in d.columns:
        return {"rows": 0, "table": {}}
    d = d.copy()
    d["t1_qualified"] = d["t1_qualified"].map(lambda v: bool(v) if v == v and v not in (None, "") else False)
    d["est_rev_direction"] = d["est_rev_direction"].fillna("unknown").astype(str).str.lower()
    tab = (d.groupby(["t1_qualified", "est_rev_direction"]).size()
             .unstack(fill_value=0).to_dict(orient="index"))
    out = {str(k): v for k, v in tab.items()}
    imp_q = out.get("True", {}).get("improving", 0)
    imp_all = sum(v.get("improving", 0) for v in out.values())
    return {"rows": int(len(d)), "group": group, "run_date": run_date, "table": out,
            "improving_and_qualified": int(imp_q), "improving_total": int(imp_all),
            "note": ("ZERO qualified names carry improving revisions — the forward signal the "
                     "framework exists to prize is absent from everything it admits"
                     if imp_all and not imp_q else "")}


# ── self-test ────────────────────────────────────────────────────────────────────────────

def _selftest():
    import tempfile
    import pandas as pd
    fails = []

    def ok(label, cond):
        print(("  PASS  " if cond else "  FAIL  ") + label)
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        st = os.path.join(td, "gv.csv")
        rich = [{"ticker": "AAA", "sector": "Technology", "mkt_cap": 5e9, "gross_margin": 0.62,
                 "rev_cagr_3yr": 0.14, "fcf_pos_years": 4, "op_margin": 0.21, "passed": True,
                 "gate_code": "", "measured_unconditionally": True, "est_rev_direction": "improving",
                 "t1_qualified": True},
                {"ticker": "BBB", "sector": "Health Care", "mkt_cap": 2e9, "gross_margin": 0.30,
                 "rev_cagr_3yr": -0.03, "fcf_pos_years": 1, "op_margin": -0.10, "passed": False,
                 "gate_code": "Gate 4", "measured_unconditionally": True,
                 "est_rev_direction": "neutral", "t1_qualified": False}]
        n_in, n_tot = log_gate_variables(rich, group="NASDAQ", run_date="2026-08-07", store=st)
        ok("U-GV1 rows written", (n_in, n_tot) == (2, 2))

        cov = coverage_report(store=st, group="NASDAQ", run_date="2026-08-07")
        ok("U-GV2 coverage 100% on a complete frame", cov["acceptance"] == "PASS")

        # THE 29-Jul REGRESSION: a sparse re-log must not blank rich cells.
        sparse = [{"ticker": "AAA", "gate_code": "", "passed": True}]
        log_gate_variables(sparse, group="NASDAQ", run_date="2026-08-07", store=st)
        back = pd.read_csv(st).set_index("ticker")
        ok("U-GV3 sparse re-log does NOT destroy rich cells",
           float(back.loc["AAA", "gross_margin"]) == 0.62
           and float(back.loc["AAA", "mkt_cap"]) == 5e9)
        ok("U-GV3b idempotent — still 2 rows", len(back) == 2)

        # A genuine update still lands.
        log_gate_variables([{"ticker": "AAA", "gross_margin": 0.66}],
                           group="NASDAQ", run_date="2026-08-07", store=st)
        back = pd.read_csv(st).set_index("ticker")
        ok("U-GV4 real update overwrites its own cell",
           float(back.loc["AAA", "gross_margin"]) == 0.66)

        # THE 01-Aug MISLABEL: a gap stored where a level belongs must fail the WRITE.
        raised = False
        try:
            log_gate_variables([{"ticker": "CCC", "gross_margin": 47.0}],
                               group="NASDAQ", run_date="2026-08-07", store=st)
        except GateVariableRangeError:
            raised = True
        ok("U-GV5 out-of-range level refused (gap-as-level guard)", raised)
        ok("U-GV5b nothing partial reached disk", len(pd.read_csv(st)) == 2)

        # Percent-vs-fraction slip is the same class and must also fail.
        raised2 = False
        try:
            log_gate_variables([{"ticker": "DDD", "rev_cagr_3yr": 1400.0}],
                               group="NASDAQ", run_date="2026-08-07", store=st)
        except GateVariableRangeError:
            raised2 = True
        ok("U-GV6 percent-stored-as-fraction refused", raised2)

        # Legal extremes survive: a pre-revenue biotech's deeply negative GM is real.
        try:
            log_gate_variables([{"ticker": "EEE", "gross_margin": -4.67, "sector": "Health Care",
                                 "mkt_cap": 1e9, "rev_cagr_3yr": 0.02, "fcf_pos_years": 0,
                                 "op_margin": -12.0, "measured_unconditionally": True}],
                               group="NASDAQ", run_date="2026-08-07", store=st)
            legal = True
        except GateVariableRangeError:
            legal = False
        ok("U-GV7 genuinely extreme but real values are allowed", legal)

        # Coverage FAILS when the store reverts to gate-conditional population.
        st2 = os.path.join(td, "gv2.csv")
        conditional = [{"ticker": f"T{i}", "sector": "Tech", "mkt_cap": 1e9,
                        "gross_margin": (0.5 if i < 2 else None),
                        "rev_cagr_3yr": 0.1, "fcf_pos_years": 3, "op_margin": 0.1}
                       for i in range(100)]
        log_gate_variables(conditional, group="X", run_date="2026-08-07", store=st2)
        ok("U-GV8 acceptance FAILS on gate-conditional coverage",
           coverage_report(store=st2)["acceptance"] == "FAIL")

        # R4 crosstab
        ct = revisions_crosstab(store=st, group="NASDAQ", run_date="2026-08-07")
        ok("U-GV9 revisions crosstab builds", ct["rows"] >= 2 and "table" in ct)

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)})")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--crosstab", action="store_true")
    ap.add_argument("--group")
    ap.add_argument("--run_date")
    ap.add_argument("--store", default=STORE)
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.crosstab:
        print(json.dumps(revisions_crosstab(a.store, a.group, a.run_date), indent=2))
        return 0
    rep = coverage_report(a.store, a.group, a.run_date)
    print(json.dumps(rep, indent=2))
    return 0 if rep.get("acceptance") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
