#!/usr/bin/env python3
"""
position_alerts.py — WP-C (29-Jul-2026): between-run early warning over data already collected.

THE GAP THIS CLOSES
-------------------
`isa-weekly-eps-snapshot` has fetched forward-EPS and target-price series every Friday since
10-Jul-2026 into eps_trend_snapshots.json. Nothing reads it between monthly runs. Held-position
thesis health is therefore only assessed at the monthly review (Step 5) or the VCI §4 pass, so a
consensus collapse or target-price cut on a held name can sit unseen for up to four weeks.

WP-3 already wrote the right ESCALATION RULE into the intra-month contract ("MIN-HOLD: floor
breach noted -- sell only on thesis-break"), but that rule only fires when someone manually
launches the intra-month run -- last done 08-May-2026. The rule existed; nothing invoked it.
This module supplies the missing automatic trigger.

DESIGN RULE -- DETECTION ONLY, NEVER EXECUTION
----------------------------------------------
Audit finding C-1: Raj's realised stock losses came from exiting TOO FAST (three 3-5yr theses
killed in 21-56 days, -GBP1,097), which is why MIN_HOLD_DAYS=182 exists. An alert that invites a
trade would make C-1 worse. So:
  * output is a STORE, not an instruction, and not an email per event;
  * every alert carries the position's min-hold state and, inside the window, is explicitly
    tagged NOT_ACTIONABLE;
  * alerts are consumed by the NEXT SCHEDULED REVIEW (monthly pre-run / mid-month brief),
    which is where a decision may legitimately be made;
  * silence is the normal output.

SCOPE: held stock-sleeve positions + T1 watchlist names. NOT the ~1,000-name screen universe.

CLI
---
  python3 position_alerts.py --snapshots eps_trend_snapshots.json \\
      --watchlist watchlist_tickers.json --trades-log project_isa_trades_log.md \\
      --out position_alerts.json [--quiet]
Exit 0 = no alerts (normal). Exit 1 = one or more alerts recorded.
"""
from __future__ import annotations
import argparse, datetime, json, os, re, sys

SCHEMA_VERSION = "1.0"

# Thresholds live in scoring_config so there are no magic numbers here (framework invariant 6).
DEFAULTS = {
    "ALERT_EPS_WOW_DROP_PCT": -3.0,      # +1y consensus EPS week-on-week fall
    "ALERT_EPS_TRAJ_DROP_PCT": -6.0,     # cumulative fall over the trajectory window
    "ALERT_TARGET_CUT_PCT": -8.0,        # mean analyst target cut over the window
    "ALERT_CONSEC_DOWN_WEEKS": 2,        # consecutive down weeks = direction change
    "ALERT_TRAJECTORY_WEEKS": 6,
    # WP5 (29-Jul-2026) — PRICE rules. The module previously monitored ONLY consensus EPS and mean
    # analyst target. On 29-Jul-2026 it returned zero alerts while MU was -31.7% from its 29-Jun
    # close, because MU's mean target ROSE throughout the fall ($1,486 -> $1,507). Run_Context
    # L1003 already states analyst targets "systematically lag a stock's direction of travel" —
    # the early-warning layer was nonetheless built entirely on them. Price closes that gap.
    "ALERT_PRICE_DROP_PCT": -10.0,        # Raj 29-Jul: >10% fall on a held name -> advise review
    "ALERT_PRICE_DROP_WINDOW_D": 30,
    "ALERT_PRICE_VS_TARGET_DIVERGENCE_PP": 15.0,  # price down while target flat/up by this much
    # ISA-0417 (23-Aug-2026) — THE UPSIDE TWIN. The 29-Jul-2026 rewrite above corrected the
    # anti-correlation in ONE DIRECTION. It is symmetric: a RISING stock whose target rises
    # with it ALSO holds implied upside high, so nothing can fire when a position WINS. On
    # 22-Aug-2026 MU carried seven exit rules — four thesis breaks and three deterioration
    # detectors — and ZERO that could fire because it was +144%, having given back 19.5% from
    # a $1,200 peak with no rule firing at any point. A correction to a SIGNED quantity must be
    # tested on both signs. PROVISIONAL: the rise threshold is wider than the fall threshold
    # because upside moves are larger, not because the evidence says so — recalibrate once the
    # detector has fired over a few runs.
    "ALERT_PRICE_RISE_PCT": 15.0,         # >15% rise on a held name -> advise review
    "ALERT_PRICE_RISE_WINDOW_D": 30,
}


def _cfg(name):
    try:
        import scoring_config as cfg
        return getattr(cfg, name, DEFAULTS[name])
    except Exception:
        return DEFAULTS[name]


def _series(store, ticker, key, weeks):
    ser = [p for p in store.get("series", {}).get((ticker or "").upper(), [])
           if p.get(key) is not None]
    return sorted(ser, key=lambda p: p["date"])[-weeks:]


def _pct(new, old):
    try:
        if old in (None, 0):
            return None
        return round((float(new) - float(old)) / abs(float(old)) * 100.0, 2)
    except Exception:
        return None


def price_move_pct(ticker, days, price_csv=None, asof=None):
    """Trailing price move % over `days` calendar days. Reads a cached close matrix when supplied
    (offline/testable); falls back to yfinance. Returns None when price cannot be resolved —
    a missing price must never fabricate an alert."""
    try:
        import pandas as pd
        if price_csv and os.path.exists(price_csv):
            px = pd.read_csv(price_csv, index_col=0, parse_dates=True).sort_index()
            if ticker not in px.columns:
                return None
            ser = px[ticker].dropna()
            if asof:
                ser = ser[ser.index <= pd.Timestamp(asof)]
        else:
            import yfinance as yf
            ser = yf.Ticker(ticker).history(period="3mo", auto_adjust=False)["Close"].dropna()
        if len(ser) < 5:
            return None
        end = ser.index[-1]
        window = ser[ser.index >= (end - pd.Timedelta(days=days))]
        if len(window) < 2:
            return None
        return round((float(window.iloc[-1]) / float(window.iloc[0]) - 1) * 100.0, 2)
    except Exception:
        return None


def _consecutive_down(ser, key):
    n = 0
    for i in range(len(ser) - 1, 0, -1):
        try:
            if float(ser[i][key]) < float(ser[i - 1][key]):
                n += 1
            else:
                break
        except Exception:
            break
    return n


def load_min_hold(trades_log_path):
    """-> {TICKER: 'YYYY-MM-DD'} from the trades log `min_hold_until` fields. Absent file is fine."""
    out = {}
    if not trades_log_path or not os.path.exists(trades_log_path):
        return out
    try:
        txt = open(trades_log_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return out
    cur = None
    for line in txt.splitlines():
        m = re.match(r"^#{1,4}\s*([A-Z][A-Z0-9.\-]{0,9})\b", line.strip())
        if m:
            cur = m.group(1)
        m2 = re.search(r"min_hold_until[^0-9]{0,12}(\d{4}-\d{2}-\d{2})", line, re.I)
        if m2:
            tk = cur
            m3 = re.search(r"\b([A-Z][A-Z0-9.\-]{0,9})\b\s*[:|]", line)
            if m3 and not tk:
                tk = m3.group(1)
            if tk:
                out[tk.upper()] = m2.group(1)
    return out


def scope_tickers(watchlist_path):
    """Held stock-sleeve positions + T1/watchlist names. Never the screen universe."""
    held, watch = [], []
    try:
        d = json.load(open(watchlist_path, encoding="utf-8"))
    except Exception:
        return held, watch
    for x in (d.get("stock_sleeve") or []):
        t = x.get("ticker") if isinstance(x, dict) else x
        if t and t not in held:
            held.append(t)
    for section in ("watchlist", "vci_watchlist"):
        for x in (d.get(section) or []):
            t = x.get("ticker") if isinstance(x, dict) else x
            if t and t not in watch and t not in held:
                watch.append(t)
    return held, watch


def load_position_gain(trades_log, price_csv=None, asof=None):
    """Current gain % per held ticker, for the winners/losers min-hold rule. Cost basis is parsed
    from the trades log; live value from the price cache or yfinance. Returns {} on any failure —
    an unresolved cost basis must degrade to NOT_ACTIONABLE, never to a permissive verdict."""
    out = {}
    if not trades_log or not os.path.exists(trades_log):
        return out
    try:
        txt = open(trades_log, encoding="utf-8").read()
    except OSError:
        return out
    # "### TICK — ..." section headers, then "| **Shares** | N |" and a GBP total cost
    for m in re.finditer(r"^###\s+([A-Z][A-Z0-9.\-]{0,9})\s+[—-]", txt, re.M):
        tk = m.group(1)
        seg = txt[m.end(): m.end() + 2500]
        if re.search(r"SOLD|CLOSED|NEVER PURCHASED", seg[:400], re.I):
            continue
        sh = re.search(r"\*\*Shares\*\*\s*\|\s*\**([0-9,.]+)", seg)
        cost = re.search(r"\*\*Total cost\*\*\s*\|\s*\**\s*£([0-9,.]+)", seg)
        if not (sh and cost):
            continue
        try:
            shares = float(sh.group(1).replace(",", ""))
            total = float(cost.group(1).replace(",", ""))
        except ValueError:
            continue
        px = _last_close(tk, price_csv=price_csv, asof=asof)
        if px is None or shares <= 0 or total <= 0:
            continue
        out[tk.upper()] = round((px * shares / total - 1) * 100, 1)   # FX-naive: see caveat
    return out


def _last_close(ticker, price_csv=None, asof=None):
    try:
        import pandas as pd
        if price_csv and os.path.exists(price_csv):
            px = pd.read_csv(price_csv, index_col=0, parse_dates=True).sort_index()
            if ticker not in px.columns:
                return None
            ser = px[ticker].dropna()
            if asof:
                ser = ser[ser.index <= pd.Timestamp(asof)]
            return float(ser.iloc[-1]) if len(ser) else None
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="5d", auto_adjust=False)["Close"].dropna()
        return float(h.iloc[-1]) if len(h) else None
    except Exception:
        return None


def _actionability(in_window, is_held, gain):
    """Raj rule (29-Jul-2026) — TRIMS ALLOWED ON WINNERS, BLOCKED ON LOSERS.

    Audit finding C-1 is about CAPITULATING ON LOSSES: three 3-5yr theses killed in 21-56 days for
    -GBP1,097, PCTY sold day 52 at -3.5% and now +31.6% vs cost. That is the behaviour MIN_HOLD_DAYS
    =182 exists to stop. Taking profit on a position that is up 103% after a 30%+ drawdown is a
    DIFFERENT action and the min-hold was never meant to prevent it. So inside the window:
      * position in PROFIT   -> trim/profit-take permitted, subject to the normal review
      * position at a LOSS   -> not actionable; exit still requires a genuine thesis-break
    Full exits inside the window remain thesis-break-only in BOTH cases. Detection, never execution.
    """
    if not is_held:
        return "REVIEW_AT_NEXT_SCHEDULED_RUN — detection only; this is not a sell signal"
    if not in_window:
        return "REVIEW_AT_NEXT_SCHEDULED_RUN — outside min-hold; Step 8 discipline applies at the next scheduled review"
    if gain is not None and gain > 0:
        return ("PROFIT_TAKING_REVIEW_PERMITTED — inside min-hold BUT the position is in profit "
                "(+%.1f%%). Raj rule 29-Jul-26: min-hold blocks loss capitulation (C-1), not profit "
                "taking. Any reduction is a REVIEW OUTCOME, never this module's instruction; a full exit "
                "still requires thesis-break. Run the intra-month stock review." % gain)
    if gain is not None:
        return ("NOT_ACTIONABLE — inside min-hold and the position is at a LOSS (%.1f%%). This is "
                "exactly the C-1 failure mode; exit requires a genuine thesis-break." % gain)
    return ("NOT_ACTIONABLE — inside framework min-hold window and cost basis unresolved; carry to "
            "the next scheduled review as context only (C-1 / MIN-HOLD escalation)")


def evaluate(store, tickers, held_set, min_hold, today=None, price_csv=None, asof=None, gain_pct=None):
    today = today or datetime.date.today().isoformat()
    weeks = int(_cfg("ALERT_TRAJECTORY_WEEKS"))
    wow_lim = float(_cfg("ALERT_EPS_WOW_DROP_PCT"))
    traj_lim = float(_cfg("ALERT_EPS_TRAJ_DROP_PCT"))
    tgt_lim = float(_cfg("ALERT_TARGET_CUT_PCT"))
    consec_lim = int(_cfg("ALERT_CONSEC_DOWN_WEEKS"))
    alerts, skipped = [], []
    for tk in tickers:
        eps = _series(store, tk, "eps_fwd1y", weeks)
        tgt = _series(store, tk, "target_mean", weeks)
        fired = []
        # ---- PRICE rules (WP5). Evaluated FIRST and INDEPENDENTLY of estimate history, so a name
        # with too little snapshot history can still raise a drawdown alert. This is the specific
        # failure that let MU fall 31.7% unflagged.
        drop_lim = float(_cfg("ALERT_PRICE_DROP_PCT"))
        win_d = int(_cfg("ALERT_PRICE_DROP_WINDOW_D"))
        pmove = price_move_pct(tk, win_d, price_csv=price_csv, asof=asof)
        if pmove is not None and pmove <= drop_lim:
            fired.append({"rule": "price_drawdown", "value_pct": pmove,
                          "threshold_pct": drop_lim, "window_days": win_d,
                          "advice": "RUN_INTRAMONTH_STOCK_REVIEW"})
            if len(tgt) >= 2:
                tc = _pct(tgt[-1]["target_mean"], tgt[0]["target_mean"])
                if tc is not None and (tc - pmove) >= float(_cfg("ALERT_PRICE_VS_TARGET_DIVERGENCE_PP")):
                    fired.append({"rule": "price_target_divergence", "price_pct": pmove,
                                  "target_pct": tc, "divergence_pp": round(tc - pmove, 2),
                                  "note": "price fell while consensus target held or rose — "
                                          "consensus is lagging, do not read the target as support"})
        # ---- ISA-0417: the UPSIDE twin of the two rules above. Same shape, opposite sign.
        # Neither is a sell instruction; both advise the same review. The point is that until
        # 23-Aug-2026 the detector set could only fire on deterioration.
        rise_lim = float(_cfg("ALERT_PRICE_RISE_PCT"))
        rise_win = int(_cfg("ALERT_PRICE_RISE_WINDOW_D"))
        rmove = pmove if rise_win == win_d else price_move_pct(tk, rise_win, price_csv=price_csv,
                                                               asof=asof)
        if rmove is not None and rmove >= rise_lim:
            fired.append({"rule": "price_appreciation", "value_pct": rmove,
                          "threshold_pct": rise_lim, "window_days": rise_win,
                          "advice": "RUN_INTRAMONTH_STOCK_REVIEW",
                          "note": "position has moved materially in its favour — re-underwrite "
                                  "against its entry case; this is a REVIEW, never a sell"})
            if len(tgt) >= 2:
                tc = _pct(tgt[-1]["target_mean"], tgt[0]["target_mean"])
                if tc is not None and (rmove - tc) >= float(_cfg("ALERT_PRICE_VS_TARGET_DIVERGENCE_PP")):
                    fired.append({"rule": "price_target_convergence", "price_pct": rmove,
                                  "target_pct": tc, "divergence_pp": round(rmove - tc, 2),
                                  "note": "price rose while the consensus target held or fell — "
                                          "the implied upside has been CONSUMED rather than "
                                          "earned; re-underwrite before treating the position "
                                          "as still carrying its entry case"})
        if len(eps) < 2:
            if not fired:
                skipped.append({"ticker": tk, "reason": "insufficient_history", "points": len(eps)})
                continue
            eps = []
        wow = _pct(eps[-1]["eps_fwd1y"], eps[-2]["eps_fwd1y"]) if len(eps) >= 2 else None
        if wow is not None and wow <= wow_lim:
            fired.append({"rule": "eps_wow_drop", "value_pct": wow, "threshold_pct": wow_lim})
        traj = _pct(eps[-1]["eps_fwd1y"], eps[0]["eps_fwd1y"]) if len(eps) >= 2 else None
        if traj is not None and traj <= traj_lim:
            fired.append({"rule": "eps_trajectory_drop", "value_pct": traj, "threshold_pct": traj_lim,
                          "weeks": len(eps)})
        cd = _consecutive_down(eps, "eps_fwd1y") if len(eps) >= 2 else 0
        if cd >= consec_lim:
            fired.append({"rule": "eps_consecutive_down", "weeks": cd, "threshold_weeks": consec_lim})
        if len(tgt) >= 2:
            tc = _pct(tgt[-1]["target_mean"], tgt[0]["target_mean"])
            if tc is not None and tc <= tgt_lim:
                fired.append({"rule": "target_mean_cut", "value_pct": tc, "threshold_pct": tgt_lim,
                              "weeks": len(tgt)})
        if not fired:
            continue
        is_held = tk in held_set
        mh_until = min_hold.get(tk.upper())
        in_window = bool(mh_until and mh_until > today)
        alerts.append({
            "ticker": tk,
            "class": "HELD" if is_held else "WATCHLIST",
            "as_of": eps[-1]["date"] if eps else (asof or today),
            "rules_fired": fired,
            "min_hold_until": mh_until,
            "in_min_hold_window": in_window,
            # C-1 protection: an alert is never a sell instruction, and inside the framework
            # min-hold window it is explicitly not actionable at all.
            "position_gain_pct": (gain_pct or {}).get(tk.upper()),
            "actionability": _actionability(in_window, is_held, (gain_pct or {}).get(tk.upper())),
        })
    return alerts, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default="eps_trend_snapshots.json")
    ap.add_argument("--watchlist", default="watchlist_tickers.json")
    ap.add_argument("--trades-log", dest="trades_log", default=None)
    ap.add_argument("--out", default="position_alerts.json")
    ap.add_argument("--price-cache", dest="price_cache", default=None,
                    help="cached close matrix (CSV) — offline/reproducible price rules; omit for live yfinance")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.snapshots):
        print("NO_SNAPSHOTS %s — weekly EPS task has not written yet; nothing to evaluate." % a.snapshots)
        return 0
    store = json.load(open(a.snapshots, encoding="utf-8"))
    held, watch = scope_tickers(a.watchlist)
    min_hold = load_min_hold(a.trades_log)
    gain = load_position_gain(a.trades_log, price_csv=a.price_cache, asof=a.asof)
    alerts, skipped = evaluate(store, held + watch, set(held), min_hold, today=a.today,
                               price_csv=a.price_cache, asof=a.asof, gain_pct=gain)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "as_of_date": a.today or datetime.date.today().isoformat(),
        "scope": {"held": held, "watchlist": watch,
                  "n_evaluated": len(held) + len(watch), "n_insufficient_history": len(skipped)},
        "thresholds": {k: _cfg(k) for k in DEFAULTS},
        "alerts": alerts,
        "insufficient_history": skipped,
        "consumed_by": ["monthly_isa_prerun (Step 5 context)", "isa-mid-month-intelligence-brief"],
        "doctrine": ("Detection only. Never an execution trigger. Alerts are context for the next "
                     "SCHEDULED review; the framework 182-day min-hold (C-1) is unaffected."),
    }
    json.dump(payload, open(a.out, "w", encoding="utf-8"), indent=2)

    if not a.quiet:
        if not alerts:
            print("POSITION_ALERTS none (evaluated %d names; %d lack history) -> %s"
                  % (len(held) + len(watch), len(skipped), a.out))
        else:
            print("POSITION_ALERTS %d fired -> %s" % (len(alerts), a.out))
            for al in alerts:
                print("  %-6s %-9s %s%s" % (al["ticker"], al["class"],
                      ", ".join(r["rule"] for r in al["rules_fired"]),
                      "  [IN MIN-HOLD WINDOW - not actionable]" if al["in_min_hold_window"] else ""))
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
