#!/usr/bin/env python3
"""
vci_prescore.py -- VCI Stage 0: Universal Quant Merit Score (QMS).

Pre-inflection mandate. Ranks the FULL multi-theme universe every month on fresh data so
EVERY name gets a fair, merit-based, non-stale shot at advancing to expensive ACS scoring.
Replaces the old "top 2-3 themes feed Section 2" gate: theme is now a WEIGHT (inside the
Opportunity pillar), not a discovery gate.

QMS = 0.40*Trajectory + 0.35*Opportunity + 0.25*Traction   (each pillar percentile-ranked 0-100)
  Trajectory  = Part A score as a fraction of its effective max -> quality of the asymmetric setup
  Opportunity = theme TAM tier x scarcity (theme_opportunity.json) x rev-runway (A1) x cap-headroom
  Traction    = leading FUNDAMENTAL signals that fire BEFORE price: A5 (R&D) + A2 (rev accel) + A7 (FCF investment)

Price momentum is deliberately NOT in QMS. Under a pre-inflection mandate strong sustained price
momentum is a GUARDRAIL (the name may have left the pre-inflection window), surfaced per-name as
`inflection_flag` from vci_acs_scorer -- it can warn/demote, never up-rank.

Resumable: each call pulls the requested themes/tickers, MERGES raw pillar values into
vci_prescore_cache_[mmm_yyyy].json, then recomputes ranks over everything cached and writes the
ranked table + audit log. Run per-theme to stay inside the local 45s batch budget.

Usage:
  python vci_prescore.py all
  python vci_prescore.py 1 9 10
  python vci_prescore.py --tickers ALAB POET IONQ
  python vci_prescore.py --rank-only            # re-rank + print existing cache (no fetch)
  python vci_prescore.py all --advance 12       # also print the Stage-1 advancement set
"""

import sys
try:
    import isa_env_guard  # noqa  (disk guardrail: temp + yfinance cache -> tmpfs /dev/shm)
except Exception:
    pass
import os
import json
import math
import argparse
from datetime import datetime

import vci_acs_scorer as sc
try:
    from vci_screener import UNIVERSE
except Exception:
    UNIVERSE = {}

INV_DIR = os.path.dirname(os.path.abspath(__file__))
THEME_OPP_PATH = os.path.join(INV_DIR, "theme_opportunity.json")
WEIGHTS = {"trajectory": 0.40, "opportunity": 0.35, "traction": 0.25}


# --------------------------------------------------------------------------
# Theme opportunity constants
# --------------------------------------------------------------------------
def load_theme_opp():
    try:
        with open(THEME_OPP_PATH, encoding="utf-8") as f:
            return json.load(f).get("themes", {})
    except Exception:
        return {}


def cap_factor(mktcap):
    """Smaller cap in a big-TAM theme = more multibagger headroom (bounded)."""
    if not mktcap:
        return 1.0
    if mktcap < 2e9:
        return 2.0
    if mktcap < 10e9:
        return 1.5
    if mktcap < 30e9:
        return 1.0
    return 0.7


# --------------------------------------------------------------------------
# Per-ticker raw pillar extraction (reuses vci_acs_scorer -- single source of truth)
# --------------------------------------------------------------------------
def pull_raw(sym, theme_id, theme_opp):
    """Score one ticker via vci_acs_scorer and return raw pillar values (pre-percentile)."""
    d, scores = sc.score_candidate(sym)
    if d.get("error"):
        return {"ticker": sym, "theme": theme_id, "error": d["error"]}
    totals = sc.compute_totals(scores)
    denom = totals["effective_denom"] or 26
    trajectory = totals["raw_score"] / denom if denom else 0.0   # 0..1

    # Traction: leading fundamental signals (sum of available, normalised by available max)
    avail, got = 0, 0
    for k in ("A2", "A5", "A7"):
        mr = scores.get(k)
        if mr is not None and not mr.na and mr.score is not None:
            avail += 2
            got += mr.score
    traction = (got / avail) if avail else 0.0                   # 0..1

    # Opportunity: theme base x rev runway x cap headroom
    t = theme_opp.get(str(theme_id), {})
    theme_base = (t.get("tam_tier", 1)) * (t.get("scarcity", 1))  # 1..9
    a1 = scores.get("A1")
    rev_runway = (a1.score + 1) if (a1 and a1.score is not None) else 1   # 1..3
    opportunity = theme_base * rev_runway * cap_factor(d.get("mktcap"))

    override = sc.check_pre_inflection_override(scores, d)
    pre_inflection = bool(override[0] and override[1] and override[2])

    return {
        "ticker": sym, "theme": theme_id, "name": d.get("name", sym),
        "mktcap": d.get("mktcap"), "part_a": totals["raw_score"], "part_a_denom": denom,
        "trajectory_raw": round(trajectory, 4),
        "traction_raw": round(traction, 4),
        "opportunity_raw": round(opportunity, 3),
        "mom_6m": d.get("mom_6m"),
        "inflection_flag": sc.inflection_flag(d),
        "pre_inflection_override": pre_inflection,
        "scored": datetime.now().strftime("%Y-%m-%d"),
        "scoring_venue": sc._scoring_venue(d, sym),     # ISA-0588: venue at SCORING
    }


# --------------------------------------------------------------------------
# Percentile + QMS
# --------------------------------------------------------------------------
def percentiles(values):
    """Map each value to its percentile rank 0-100 (ties share the higher rank). Robust to outliers."""
    clean = [v for v in values if v is not None]
    if not clean:
        return [50.0 for _ in values]
    srt = sorted(clean)
    n = len(srt)
    out = []
    for v in values:
        if v is None:
            out.append(0.0)
            continue
        cnt = sum(1 for x in srt if x <= v)
        out.append(round(cnt / n * 100, 1))
    return out


def rank_cache(cache):
    rows = [r for r in cache.values() if "error" not in r]
    if not rows:
        return []
    tp = percentiles([r["trajectory_raw"] for r in rows])
    op = percentiles([r["opportunity_raw"] for r in rows])
    cp = percentiles([r["traction_raw"] for r in rows])
    for r, a, b, c in zip(rows, tp, op, cp):
        r["traj_pct"], r["opp_pct"], r["tract_pct"] = a, b, c
        r["qms"] = round(WEIGHTS["trajectory"] * a + WEIGHTS["opportunity"] * b
                         + WEIGHTS["traction"] * c, 1)
    rows.sort(key=lambda r: -r["qms"])
    return rows


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------
def cache_path():
    return os.path.join(INV_DIR, f"vci_prescore_cache_{datetime.now().strftime('%b_%Y').lower()}.json")


def load_cache():
    p = cache_path()
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    json.dump(cache, open(cache_path(), "w", encoding="utf-8"), indent=2, default=str)


def print_table(rows, advance_n=None):
    print(f"\n{'='*94}")
    print(f"  VCI QMS -- UNIVERSAL PRE-SCORE  ({len(rows)} names ranked)  "
          f"QMS = .40 Trajectory + .35 Opportunity + .25 Traction")
    print(f"{'='*94}")
    print(f"  {'#':>3} {'Tkr':<8} {'Th':>3} {'QMS':>5} {'Traj':>5} {'Opp':>5} {'Tract':>6} "
          f"{'6mMom':>7} {'Flag/Note'}")
    print(f"  {'-'*3} {'-'*8} {'-'*3} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*7} {'-'*30}")
    for i, r in enumerate(rows, 1):
        note = ""
        if r.get("pre_inflection_override"):
            note = "PRE-INFLECTION OVERRIDE (auto-advance)"
        if r.get("inflection_flag"):
            note = "[GUARDRAIL] " + r["inflection_flag"][:48]
        m6 = f"{r['mom_6m']:+.0f}%" if r.get("mom_6m") is not None else "n/a"
        print(f"  {i:>3} {r['ticker']:<8} T{r['theme']:<2} {r['qms']:>5.1f} "
              f"{r['traj_pct']:>5.0f} {r['opp_pct']:>5.0f} {r['tract_pct']:>6.0f} {m6:>7} {note}")
    if advance_n:
        adv = stage1_advance(rows, advance_n)
        print(f"\n  QMS ADVANCEMENT (top-{advance_n} + pre-inflection; {len(adv)}): "
              + ", ".join(a["ticker"] for a in adv))
        print("  ⚑ The Stage-1 set to score is the REVIEW UNIVERSE below (ISA-0769), not this list.")
        flagged = [r["ticker"] for r in rows if r.get("inflection_flag")]
        if flagged:
            print(f"  GRADUATION REVIEW (price already moved -- verify still pre-inflection, do NOT chase): "
                  + ", ".join(flagged))


def stage1_advance(rows, n):
    """Top-N by QMS + any pre-inflection-override name (protects the flat NVDA-2010 archetype)."""
    adv = list(rows[:n])
    have = {r["ticker"] for r in adv}
    for r in rows:
        if r.get("pre_inflection_override") and r["ticker"] not in have:
            adv.append(r); have.add(r["ticker"])
    return adv


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def resolve_targets(args):
    """Return list of (ticker, theme_id)."""
    if args.tickers:
        # theme unknown for ad-hoc tickers -> tag theme 0
        return [(t, 0) for t in args.tickers]
    ids = []
    if args.themes == ["all"] or not args.themes:
        ids = list(UNIVERSE.keys())
    else:
        for a in args.themes:
            try:
                ids.append(int(a))
            except ValueError:
                pass
    out = []
    for tid in ids:
        for tk in UNIVERSE.get(tid, {}).get("tickers", []):
            out.append((tk, tid))
    return out


def main():
    ap = argparse.ArgumentParser(description="VCI Stage 0 Universal Quant Merit Score")
    ap.add_argument("themes", nargs="*", help="theme numbers or 'all'")
    ap.add_argument("--tickers", nargs="*", help="ad-hoc ticker list (overrides themes)")
    ap.add_argument("--rank-only", action="store_true", help="re-rank existing cache, no fetch")
    ap.add_argument("--advance", type=int, default=None, help="also print Stage-1 advancement set (top N)")
    ap.add_argument("--json-out", default=None, help="write ranked JSON to this path")
    ap.add_argument("--universe-out", default=None,
                    help="ISA-0769: write the canonical VCI REVIEW UNIVERSE (held + watchlist + QMS top-N + "
                         "pre-inflection + overrides, with provenance) to this path. Requires --advance.")
    ap.add_argument("--override", nargs="*", default=[],
                    help="ISA-0769: authorised additions as TICKER:reason (e.g. NBIS:'hyperscaler cascade')")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="Per-call fetch time budget in seconds; stop cleanly when exceeded so the "
                         "ephemeral 45s sandbox can resume next call. 0 = no limit (default).")
    ap.add_argument("--rescore", action="store_true",
                    help="Re-fetch names already cached OK (default behaviour is RESUME: skip "
                         "names already scored without error, so re-running continues where it left off).")
    args = ap.parse_args()

    theme_opp = load_theme_opp()
    cache = load_cache()

    if not args.rank_only:
        import time
        targets = resolve_targets(args)
        if not targets:
            print("No targets. Use theme numbers, 'all', or --tickers.")
            sys.exit(1)
        # RESUME by default: skip names already scored without error, so re-running the SAME
        # command continues where a timed-out call left off (the ephemeral 45s sandbox wipes
        # /dev/shm between calls but the cache persists on the mount).
        pending = [(tk, tid) for (tk, tid) in targets
                   if args.rescore or tk not in cache or "error" in cache.get(tk, {})]
        start = time.time()
        print(f"Pre-scoring {len(pending)} of {len(targets)} name(s) "
              f"(resume={'off' if args.rescore else 'on'}, budget={args.budget or 'none'}s)...")
        complete = True
        for tk, tid in pending:
            if args.budget and (time.time() - start) > args.budget:
                complete = False
                print(f"  [BUDGET {args.budget}s reached] partial progress saved; "
                      f"resume by re-running the identical command.")
                break
            try:
                row = pull_raw(tk, tid, theme_opp)
            except Exception as e:
                row = {"ticker": tk, "theme": tid, "error": str(e)[:120]}
            cache[tk] = row
            save_cache(cache)          # incremental: persist after EVERY ticker (survives a mid-call timeout)
            if "error" in row:
                print(f"  [SKIP] {tk}: {row['error'][:60]}")
        _ok = len([r for r in cache.values() if "error" not in r])
        print(f"  {'[COMPLETE]' if complete else '[PARTIAL]'} cached_ok={_ok} "
              f"cached_total={len(cache)} universe={len(targets)}")

    rows = rank_cache(cache)
    print_table(rows, advance_n=args.advance)
    if args.advance:
        # ISA-0769: THE Stage-1 population. A failure here is a HARD failure of the VCI review path -
        # the run cannot prove it reviews the capital already at risk.
        import vci_review_universe as _vru
        try:
            uni = _vru.build(rows, args.advance, overrides=args.override, root=INV_DIR)
        except _vru.ReviewUniverseError as exc:
            print(f"\n  ✖ REVIEW UNIVERSE REFUSED: {exc}")
            sys.exit(3)
        print(f"\n  REVIEW UNIVERSE ({uni['counts']['members']} members; population check "
              f"{uni['population_check']['state']}; held authority: {uni['held_why']}):")
        for m in uni["members"]:
            print(f"    {m['ticker']:<8} roles={'+'.join(m['roles'])}"
                  + (f"  holding={m.get('holding_state')}" if m.get('holding_state') else "")
                  + ("" if m["qms_row"] else "  [NOT IN QMS CACHE - score it: vci_prescore.py --tickers]"))
        if args.universe_out:
            json.dump(uni, open(args.universe_out, "w", encoding="utf-8"), indent=1, default=str)
            print(f"  Review universe written: {args.universe_out}")
    if args.json_out:
        json.dump(rows, open(args.json_out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\nRanked JSON written: {args.json_out}")
    print(f"\nCache: {cache_path()}  ({len([r for r in cache.values() if 'error' not in r])} scored)")


def _selftest():
    """ISA-0588 — offline. Every prescore row carries the scoring venue, typed when absent."""
    n = 0
    _orig = (sc.score_candidate, sc.compute_totals, sc.check_pre_inflection_override,
             sc.inflection_flag)
    try:
        sc.compute_totals = lambda scores: {"raw_score": 10, "effective_denom": 20}
        sc.check_pre_inflection_override = lambda scores, d: (False, False, False)
        sc.inflection_flag = lambda d, *a, **k: ""
        sc.score_candidate = lambda sym: ({"name": "X", "mktcap": 1e9, "exchange": "NMS",
                                           "listing_currency": "USD"}, {})
        r = pull_raw("QBTS", 1, {})
        assert r["scoring_venue"]["exchange"] == "NMS" and \
            r["scoring_venue"]["status"] == "CAPTURED_AT_SCORING", r; n += 1
        # MUST-FIRE: no venue in the pull -> typed UNKNOWN on the row, never a default
        sc.score_candidate = lambda sym: ({"name": "Y", "mktcap": 1e9}, {})
        r = pull_raw("SATL", 1, {})
        assert r["scoring_venue"]["exchange"] is None and \
            r["scoring_venue"]["status"] == "UNKNOWN_NOT_CAPTURED", \
            ("MUST-FIRE: a pull with no venue must type the row UNKNOWN_NOT_CAPTURED", r); n += 1
        # an errored pull carries no venue claim at all
        sc.score_candidate = lambda sym: ({"error": "boom"}, {})
        assert "scoring_venue" not in pull_raw("ERR", 1, {}), \
            "NEGATIVE CONTROL: an errored pull makes no venue claim"; n += 1
    finally:
        (sc.score_candidate, sc.compute_totals, sc.check_pre_inflection_override,
         sc.inflection_flag) = _orig
    # ISA-0769: the CLI's Stage-1 population is the review universe (held + watchlist always present)
    import vci_review_universe as _vru
    _h = {"state": "OK", "members": [{"key": "QBTS", "ticker": "QBTS", "holding_state": "HELD",
                                      "vci_provenance": ["fixture"]}], "authority": {}, "why": "fixture"}
    _u = _vru.build([{"ticker": "ALAB", "qms": 90}], 1, held=_h, watchlist=["IONQ"])
    assert {m["key"] for m in _u["members"]} == {"ALAB", "QBTS", "IONQ"}, \
        ("MUST-FIRE (ISA-0769): QMS top-N alone must NEVER be the Stage-1 set - held/watchlist are in", _u); n += 1
    print("vci_prescore selftest: %d assertions, 0 failed" % n)
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    main()
