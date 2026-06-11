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
        print(f"\n  STAGE 1 ADVANCEMENT SET ({len(adv)} -> expensive ACS scoring): "
              + ", ".join(a["ticker"] for a in adv))
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
    args = ap.parse_args()

    theme_opp = load_theme_opp()
    cache = load_cache()

    if not args.rank_only:
        targets = resolve_targets(args)
        if not targets:
            print("No targets. Use theme numbers, 'all', or --tickers.")
            sys.exit(1)
        print(f"Pre-scoring {len(targets)} name(s)...")
        for tk, tid in targets:
            try:
                row = pull_raw(tk, tid, theme_opp)
            except Exception as e:
                row = {"ticker": tk, "theme": tid, "error": str(e)[:120]}
            cache[tk] = row
            if "error" in row:
                print(f"  [SKIP] {tk}: {row['error'][:60]}")
        save_cache(cache)

    rows = rank_cache(cache)
    print_table(rows, advance_n=args.advance)
    if args.json_out:
        json.dump(rows, open(args.json_out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\nRanked JSON written: {args.json_out}")
    print(f"\nCache: {cache_path()}  ({len([r for r in cache.values() if 'error' not in r])} scored)")


if __name__ == "__main__":
    main()
