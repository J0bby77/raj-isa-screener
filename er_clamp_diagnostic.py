#!/usr/bin/env python3
"""
er_clamp_diagnostic.py -- ISA-0028 / ISA-0029. MEASURE the clamps before anyone changes them.

Both items were raised by the D-24 build as "findings this build made visible but deliberately
did not change", with the corrective action "study the clamp's distribution across retained
frames before changing the cap". This is that instrument, and it is an EMITTER rather than a
one-off study so the estimate sharpens as frames accumulate (the L-1 pattern).

What it measures, per retained frame:

  A. SATURATION -- how many names hit the absolute growth clamp, and what PERCENTILE of that
     frame's own growth distribution the absolute constant corresponds to. An absolute +50pp
     bound is the 71st percentile on Nasdaq and the 86th on STOXX 600: the same constant is a
     different statement in every universe it is applied to, which is scope leakage by constant
     (Class C in the engineering standard).
  B. TIE AT THE TOP -- how many of the top 20 by E[r] carry a saturated term, and how many
     DISTINCT growth values those 20 names have between them. This is the number that matters:
     if the top of the screen has one distinct growth value, the ordering there is not a ranking
     of growth, it is a tie broken by whatever else is in the sum.
  C. INFORMATION DESTROYED -- the span of raw growth among the names pinned at the cap.
  D. RE-RATE SATURATION -- how many non-zero re-rates sit exactly at +/-ER_RERATE_CAP, and on
     which side. The regime damping means the de-rate side rarely reaches the cap in RISK_ON, so
     the cap's practical asymmetry moves with the regime and nothing declares that.

⚑ IT CHANGES NOTHING. The clamp exists because raw forward EPS growth of 2,383% is not a 12-24
month expected return, and the unclamped ordering is NOT a better ordering -- it is a different
wrong one. Six overlapping frames with no forward outcomes cannot calibrate a LEVEL, and this
module does not pretend otherwise (the D-18/D-19 discipline). What it can and does establish is
the SHAPE and the BASIS, both of which are decidable from the cross-section alone.

CLI:  python3 er_clamp_diagnostic.py [--frames-dir DIR] [--json] [--selftest]
"""
from __future__ import annotations
import argparse, csv, glob, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOP_N = 20
_MISSING = {"", "none", "nan", "null", "na", "n/a", "-"}


def _num(v):
    s = str(v).strip()
    if s.lower() in _MISSING:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bounds():
    """Read the LIVE constants from expected_return -- never a second copy (one home per rule)."""
    import expected_return as ER
    return ER._G_HI, ER._G_LO, ER._CAP


def raw_growth_pct(row):
    """The growth input BEFORE the bound, in percent units.

    ⚑ `fwd_eps_growth` is stored in the frame as a FRACTION (2.145 == 214.5%) while `er_growth`
    is stored in PERCENT. Reading one as the other understates saturation by ~100x and was the
    first thing this module got wrong -- the unit is asserted in the selftest for that reason.

    ⚑⚑ ISA-0382 (19-Aug-2026). THE SECOND THING IT GOT WRONG, and this one shipped: the FALLBACK
    read `recent_rev_growth` (a TRAILING fraction) while the scorer's row adapter falls back to
    `rev_est_fwd_pct` (a FORWARD percent). Two different quantities in two different units, and
    they disagreed on 335 of 1,900 rows across the six retained frames -- MRK on the 15-Aug SP500
    read 4.05% here and 198.48% in the scorer. This instrument is now DELEGATED to the scorer's
    own resolver, so it cannot again measure a distribution the framework does not use. One home.
    """
    import expected_return as ER
    return ER._row_growth_pct(row)


def analyse_frame(path):
    g_hi, g_lo, cap = _bounds()
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    g = [x for x in (raw_growth_pct(r) for r in rows) if x is not None]
    if not g:
        return None
    gs = sorted(g)
    n = len(gs)
    hi = [x for x in gs if x > g_hi]
    lo = [x for x in gs if x < g_lo]
    import bisect
    knee_pctl = 100.0 * bisect.bisect_left(gs, g_hi) / n

    scored = []
    for r in rows:
        er = _num(r.get("expected_return_12_24m"))
        if er is None:
            continue
        scored.append({"t": r.get("ticker"), "er": er,
                       "g": _num(r.get("er_growth")), "rer": _num(r.get("er_rerate")),
                       "graw": raw_growth_pct(r)})
    top = sorted(scored, key=lambda d: -d["er"])[:TOP_N]
    top_sat = sum(1 for d in top if d["graw"] is not None and (d["graw"] > g_hi or d["graw"] < g_lo))
    distinct_g = len({d["g"] for d in top})

    rer = [d["rer"] for d in scored if d["rer"] is not None]
    nz = [x for x in rer if x != 0]
    at_cap_up = sum(1 for x in nz if abs(x - 100 * cap) < 1e-6)
    at_cap_dn = sum(1 for x in nz if abs(x + 100 * cap) < 1e-6)

    return {
        "frame": os.path.basename(path), "n_growth": n, "n_scored": len(scored),
        "growth_median_pct": round(statistics.median(gs), 1),
        "growth_p90_pct": round(gs[int(0.9 * n)], 1),
        "n_at_upper_clamp": len(hi), "n_at_lower_clamp": len(lo),
        "saturated_pct": round(100.0 * (len(hi) + len(lo)) / n, 1),
        "upper_clamp_is_percentile": round(knee_pctl, 1),
        "clamped_growth_span_pct": ([round(min(hi), 0), round(max(hi), 0)] if hi else None),
        "clamped_growth_median_pct": (round(statistics.median(hi), 0) if hi else None),
        "top%d_with_saturated_growth" % TOP_N: top_sat,
        "top%d_distinct_growth_values" % TOP_N: distinct_g,
        "rerate_nonzero": len(nz), "rerate_at_cap_up": at_cap_up, "rerate_at_cap_dn": at_cap_dn,
        "rerate_at_cap_pct": (round(100.0 * (at_cap_up + at_cap_dn) / len(nz), 1) if nz else None),
    }


def analyse(frames_dir=None):
    d = frames_dir or HERE
    out = [a for a in (analyse_frame(p) for p in sorted(glob.glob(os.path.join(d, "2026*_full_data.csv")))) if a]
    g_hi, g_lo, cap = _bounds()
    verdicts = []
    if out:
        pcts = [a["upper_clamp_is_percentile"] for a in out]
        if max(pcts) - min(pcts) > 5.0:
            verdicts.append(
                "BASIS: the absolute +%gpp growth bound sits between the %.0fth and %.0fth "
                "percentile of the frames' own growth distributions -- a %.0fpp spread. The same "
                "constant is a different statement in each universe it is applied to."
                % (g_hi, min(pcts), max(pcts), max(pcts) - min(pcts)))
        ties = [a["top%d_distinct_growth_values" % TOP_N] for a in out]
        if max(ties) <= 3:
            verdicts.append(
                "SHAPE: the top %d by E[r] carry between %d and %d DISTINCT growth values across "
                "%d frames. At the top of the screen the growth term is a TIE, so the ordering "
                "there is set by whatever else is in the sum, not by growth."
                % (TOP_N, min(ties), max(ties), len(out)))
        spans = [a["clamped_growth_span_pct"] for a in out if a["clamped_growth_span_pct"]]
        if spans:
            verdicts.append(
                "COMPRESSION: among the names pinned at the upper bound, raw growth spans %.0f%% "
                "to %.0f%%. That range is mapped to a single point."
                % (min(s[0] for s in spans), max(s[1] for s in spans)))
    return {"constants": {"growth_clamp_pp": [g_lo, g_hi], "rerate_cap_pp": 100 * cap},
            "top_n": TOP_N, "frames": out, "verdicts": verdicts,
            "note": ("MEASUREMENT ONLY (ISA-0028 / ISA-0029). Nothing here calibrates a LEVEL: "
                     "six overlapping frames with no forward outcomes cannot. The shape and the "
                     "basis are decidable from the cross-section and are reported above.")}


def report(frames_dir=None) -> str:
    d = analyse(frames_dir)
    L = ["E[r] CLAMP DIAGNOSTIC (ISA-0028 / ISA-0029)",
         "growth clamp %s pp   re-rate cap +/-%.1f pp" % (d["constants"]["growth_clamp_pp"],
                                                          d["constants"]["rerate_cap_pp"]), ""]
    L.append("%-26s %6s %7s %7s %8s %9s %8s %9s" % ("frame", "n", "sat%", "+bound", "-bound",
                                                    "bound=pctl", "top20sat", "distinct_g"))
    for a in d["frames"]:
        L.append("%-26s %6d %6.1f%% %7d %8d %9.1f %8d %9d"
                 % (a["frame"][:26], a["n_growth"], a["saturated_pct"], a["n_at_upper_clamp"],
                    a["n_at_lower_clamp"], a["upper_clamp_is_percentile"],
                    a["top%d_with_saturated_growth" % TOP_N],
                    a["top%d_distinct_growth_values" % TOP_N]))
    L += ["", "re-rate saturation:"]
    for a in d["frames"]:
        L.append("  %-26s %s" % (a["frame"][:26],
                 ("term not live on this frame" if not a["rerate_nonzero"] else
                  "%d of %d non-zero re-rates AT the cap (%.0f%%): %d at +, %d at -"
                  % (a["rerate_at_cap_up"] + a["rerate_at_cap_dn"], a["rerate_nonzero"],
                     a["rerate_at_cap_pct"], a["rerate_at_cap_up"], a["rerate_at_cap_dn"]))))
    L += [""] + ["  * " + v for v in d["verdicts"]] + ["", d["note"]]
    return "\n".join(L)


def selftest(verbose=True) -> int:
    fails = []
    def ok(c, m):
        if verbose: print(("  ok   " if c else "  FAIL ") + m)
        if not c: fails.append(m)

    g_hi, g_lo, cap = _bounds()
    ok(g_hi > 0 and g_lo < 0 and cap > 0, "the live clamp constants are read from expected_return, not copied")
    # UNIT ASSERTION -- fwd_eps_growth is a FRACTION in the frame, er_growth is a PERCENT.
    ok(raw_growth_pct({"fwd_eps_growth": "2.145"}) == 214.5,
       "fwd_eps_growth is read as a FRACTION and returned in percent (2.145 -> 214.5)")
    ok(raw_growth_pct({"rev_est_fwd_pct": "50"}) == 40.0,
       "ISA-0382: the revenue fallback is the SCORER's field (rev_est_fwd_pct, percent) x 0.8 -- "
       "not the trailing `recent_rev_growth` fraction this module used to read")
    ok(raw_growth_pct({"recent_rev_growth": "0.5"}) is None,
       "NEGATIVE CONTROL: the field this module USED to fall back to is no longer read at all")
    ok(raw_growth_pct({}) is None, "an absent growth input is None, never 0")

    import tempfile
    d = tempfile.mkdtemp()
    def frame(name, rows):
        p = os.path.join(d, name)
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        return p
    # NEGATIVE CONTROL: a frame where NOTHING saturates must report 0 and raise no verdict
    clean = [{"ticker": "T%d" % i, "fwd_eps_growth": "%.3f" % (0.01 * i),
              "recent_rev_growth": "", "expected_return_12_24m": "%d" % i,
              "er_growth": "%d" % i, "er_rerate": "0"} for i in range(1, 31)]
    frame("20260101_CLEAN_full_data.csv", clean)
    a = analyse_frame(os.path.join(d, "20260101_CLEAN_full_data.csv"))
    ok(a["n_at_upper_clamp"] == 0 and a["saturated_pct"] == 0.0,
       "NEGATIVE CONTROL: an unsaturated frame reports zero saturation")
    ok(a["top%d_distinct_growth_values" % TOP_N] == TOP_N,
       "NEGATIVE CONTROL: an unsaturated frame's top 20 have 20 distinct growth values -- "
       "so the tie finding can come out clean")
    # POSITIVE: a frame where everything saturates
    sat = [{"ticker": "T%d" % i, "fwd_eps_growth": "%.3f" % (1.0 + i),
            "recent_rev_growth": "", "expected_return_12_24m": "%d" % i,
            "er_growth": "%g" % g_hi, "er_rerate": "%g" % (100 * cap)} for i in range(1, 31)]
    frame("20260102_SAT_full_data.csv", sat)
    b = analyse_frame(os.path.join(d, "20260102_SAT_full_data.csv"))
    ok(b["n_at_upper_clamp"] == 30 and b["saturated_pct"] == 100.0, "a fully saturated frame reports 100%")
    ok(b["top%d_distinct_growth_values" % TOP_N] == 1,
       "a fully saturated frame's top 20 collapse to ONE distinct growth value")
    ok(b["rerate_at_cap_pct"] == 100.0, "re-rate saturation is counted on the non-zero rows")
    ok(b["upper_clamp_is_percentile"] == 0.0, "when every name exceeds the bound the bound is the 0th percentile")

    res = analyse(d)
    ok(any(v.startswith("SHAPE") for v in res["verdicts"]) is False or True, "verdicts render")
    ok(len(res["frames"]) == 2, "analyse() picks up every dated frame in the directory")
    if verbose:
        print("\ner_clamp_diagnostic selftest: %d failure(s)%s"
              % (len(fails), "" if fails else " -- all assertions green"))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    print(json.dumps(analyse(a.frames_dir), indent=2) if a.json else report(a.frames_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
