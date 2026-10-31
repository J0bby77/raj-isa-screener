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
      --watchlist watchlist_tickers.json --out position_alerts.json [--quiet]
  (the min-hold entry date is the FIRST BUY in transaction_ledger.json; the retired trades-log
   flag is no longer accepted as an input and is ignored with a warning if a caller passes it —
   ISA-0761)
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
    # B09-R10 (P-C, 24-Oct-2026) — POINT-IN-TIME AND ELAPSED-TIME CONTRACT (detection-only; DECLARED
    # engineering bounds that decide what an observation IS, not how sensitive the alert is):
    "ALERT_SNAPSHOT_MAX_AGE_D": 14,      # newest estimate older than this vs the as-of -> STALE, not an alert
    "ALERT_WEEKLY_GAP_MAX_D": 10,        # two observations more than this apart are not 'week-on-week'
    "ALERT_PRICE_MAX_STALE_D": 7,        # newest close older than this vs the as-of -> no price move
}


def _cfg(name):
    try:
        import scoring_config as cfg
        return getattr(cfg, name, DEFAULTS[name])
    except Exception:
        return DEFAULTS[name]


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def _d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _series(store, ticker, key, weeks, asof=None):
    """B09-R10 (P-C, 24-Oct-2026): observations of `key` that are FINITE and dated ON OR BEFORE `asof`,
    collapsed to ONE per ISO week (the last), newest `weeks`. A snapshot after the as-of (EV-0224), a NaN
    (EV-0229) or six daily points (EV-0230) used to enter the series as if they were weekly evidence."""
    lim = _d(asof) if asof else None
    ser = [p for p in store.get("series", {}).get((ticker or "").upper(), [])
           if _finite(p.get(key)) and _d(p.get("date")) is not None
           and (lim is None or _d(p.get("date")) <= lim)]
    ser = sorted(ser, key=lambda p: p["date"])
    wk = {}
    for p in ser:
        wk[_d(p["date"]).isocalendar()[:2]] = p
    return sorted(wk.values(), key=lambda p: p["date"])[-weeks:]


def _weekly_pairs(ser):
    """Consecutive observation pairs no more than ALERT_WEEKLY_GAP_MAX_D apart (true week-on-week)."""
    gap = int(_cfg("ALERT_WEEKLY_GAP_MAX_D"))
    return [(ser[i - 1], ser[i]) for i in range(1, len(ser))
            if 0 < (_d(ser[i]["date"]) - _d(ser[i - 1]["date"])).days <= gap]


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
        if asof is not None and not (price_csv and os.path.exists(price_csv)):
            # B09-R10 (P-C, EV-0236): the provider branch honours the as-of like the cache branch does
            try:
                _ix = ser.index.tz_localize(None) if getattr(ser.index, "tz", None) is not None else ser.index
                ser = ser[_ix <= pd.Timestamp(asof)]
            except Exception:                                       # noqa: BLE001
                return None
        if len(ser) < 5:
            return None
        end = ser.index[-1]
        # ⚑ B09-R10 (P-C, EV-0234): the newest close must be CURRENT relative to the as-of, and the window
        #   must actually START near (end - days). A June cache read in October used to return its own
        #   4-day move as a '30-day' alert.
        if asof is not None:
            try:
                _end_d = (end.tz_localize(None) if getattr(end, "tzinfo", None) is not None else end).date()
            except Exception:                                       # noqa: BLE001
                _end_d = None
            if _end_d is None or (pd.Timestamp(asof).date() - _end_d).days > int(_cfg("ALERT_PRICE_MAX_STALE_D")):
                return None
        window = ser[ser.index >= (end - pd.Timedelta(days=days))]
        if len(window) < 2:
            return None
        if (window.index[0] - (end - pd.Timedelta(days=days))).days > 7:
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


def min_hold_from_ledger(ledger_path=None, *, min_hold_days=None, root=None):
    """ISA-0645 — derive min_hold_until from the TRANSACTION LEDGER, which exists.

    ⚑⚑ WHY THIS FUNCTION EXISTS. `load_min_hold()` reads `project_isa_trades_log.md`, and that
    file DOES NOT EXIST anywhere under the ISA folder — verified 12-Sep-2026. Its docstring
    says "absent file is fine" and it returns `{}`, so `position_alerts.json` publishes
    `min_hold_until: None` and `in_min_hold_window: False` for EVERY held name, and the branch
    headed "Raj rule (29-Jul-2026) — TRIMS ALLOWED ON WINNERS, BLOCKED ON LOSERS" has never
    once executed: `gain` is always None and `in_window` always False.
    `position_sizing.min_hold_ok`, which carries the profit/loss asymmetry, has zero
    production call sites. **The 182-day framework min-hold — the C-1 anti-churn fix that
    closed a realised -GBP 1,097 churn pattern — has had no live enforcement path at all.**
    "Absent file is fine" was true of the READER and false of the CONTROL (R2.10).

    ⚑ THE ENTRY DATES WERE ALWAYS THERE. `transaction_ledger.json` carries every dealt row
    with its date, and the first BUY of a ticker is the entry. Measured: ONT 2026-06-01,
    ABCL 2026-07-10, QBTS 2026-08-10 -> min-hold to 2026-11-30, 2027-01-08, 2027-02-08. The
    control was not blocked on data; it was blocked on reading a file nobody wrote (FC-E).

    ⚑ FIRST BUY, NOT LAST (D24, ISA-0545). `position_first_entry_date` is the FIRST entry:
    a top-up must not restart a position's min-hold clock, or the anti-churn control could be
    reset indefinitely by adding to a position.

    Returns {TICKER: {min_hold_until, position_first_entry_date, source}}. A ticker with no
    BUY row is OMITTED rather than given today's date — a manufactured entry date would make
    a position look freshly bought and permanently unexitable (R4.3)."""
    import datetime as _dt
    root = root or os.path.dirname(os.path.abspath(__file__))
    if min_hold_days is None:
        try:
            import scoring_config as _sc
            min_hold_days = int(getattr(_sc, "MIN_HOLD_DAYS", 182))
        except Exception:                                            # noqa: BLE001
            min_hold_days = 182
    ledger_path = ledger_path or os.path.join(root, "transaction_ledger.json")
    out = {}
    if not os.path.exists(ledger_path):
        return out
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:                                                # noqa: BLE001
        return out
    # ⚑ THE MMF IS NOT A STOCK POSITION, AND THIS MATTERS. The ledger classifies CSH2
    #   (Amundi Smart Overnight, the B2 cash sweep destination) as asset_class "stock"
    #   because it is dealt like one. If it inherited a 182-day min-hold, the B2 recall leg —
    #   'when the next opportunity arises, trim funds and move capital into stocks' — would be
    #   BLOCKED by an anti-churn rule that exists to stop stock churn, and the waiting room
    #   would have no exit. `waiting_room.py` already states the min-hold 'binds the STOCK leg
    #   only'; this is that sentence made mechanical rather than documented (R14.2).
    #   Excluded by DECLARED ticker, not by guessing at ETF-shaped names (R4.8).
    try:
        import scoring_config as _sc2
        _mmf = {str(t).upper() for t in getattr(_sc2, "MMF_TICKERS", ("CSH2", "CSH2.L"))}
    except Exception:                                                # noqa: BLE001
        _mmf = {"CSH2", "CSH2.L"}
    first = {}
    for e in (doc.get("entries") or []):
        if str(e.get("type") or "").lower() != "buy":
            continue
        if str(e.get("asset_class") or "").lower() not in ("stock", "equity", ""):
            continue
        if str(e.get("ticker") or "").upper() in _mmf:
            continue
        tk = str(e.get("ticker") or "").upper()
        d = str(e.get("date") or "")[:10]
        if not tk or not d:
            continue
        if tk not in first or d < first[tk]:
            first[tk] = d
    for tk, d in first.items():
        try:
            until = (_dt.date.fromisoformat(d)
                     + _dt.timedelta(days=min_hold_days)).isoformat()
        except Exception:                                            # noqa: BLE001
            continue
        out[tk] = {"min_hold_until": until, "position_first_entry_date": d,
                   "min_hold_days": min_hold_days,
                   "source": "transaction_ledger.json first BUY row (D24: FIRST entry, not "
                             "last — a top-up must not restart the anti-churn clock)"}
    return out


def load_min_hold(trades_log_path, *, ledger_fallback=True, root=None):
    """-> {TICKER: 'YYYY-MM-DD'} from the trades log `min_hold_until` fields.

    ⚑ ISA-0645: an absent trades log is NO LONGER 'fine'. It falls back to the transaction
    ledger, which is broker-dealt truth and actually exists. If BOTH are unavailable the
    caller must treat the min-hold as UNENFORCED and say so — see `min_hold_state()`."""
    out = {}
    if not trades_log_path or not os.path.exists(trades_log_path):
        if ledger_fallback:
            return {k: v["min_hold_until"]
                    for k, v in min_hold_from_ledger(root=root).items()}
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


_LEDGER_ENTRY_CACHE = {}


def _entry_from_ledger(ticker, root=None):
    """First-BUY date for one ticker, from the ledger. None if absent — never today's date."""
    if not _LEDGER_ENTRY_CACHE:
        _LEDGER_ENTRY_CACHE.update(min_hold_from_ledger(root=root) or {"__empty__": {}})
    row = _LEDGER_ENTRY_CACHE.get(str(ticker or "").upper())
    return (row or {}).get("position_first_entry_date")


def min_hold_state(trades_log_path=None, ledger_path=None, root=None) -> dict:
    """ISA-0645 — is the 182-day min-hold ENFORCED right now, and from what source?

    R2.10: 'the control could not run' and 'no position is inside its window' must not
    produce the same output, and for seven weeks they did — `position_alerts.json` published
    `in_min_hold_window: False` for every name because the source file did not exist."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    have_log = bool(trades_log_path and os.path.exists(trades_log_path))
    led = min_hold_from_ledger(ledger_path, root=root)
    if have_log:
        src, rows = "trades_log", load_min_hold(trades_log_path, ledger_fallback=False, root=root)
        rows = {k: {"min_hold_until": v} for k, v in rows.items()}
    elif led:
        src, rows = "transaction_ledger", led
    else:
        return {"enforced": False, "source": None, "n_positions": 0, "positions": {},
                "why": ("NEITHER project_isa_trades_log.md NOR transaction_ledger.json yielded "
                        "an entry date, so the 182-day min-hold is UNENFORCED this run. This "
                        "is a REFUSAL, not a finding that no position is inside its window "
                        "(R2.10/R4.3).")}
    return {"enforced": True, "source": src, "n_positions": len(rows), "positions": rows,
            "why": "min-hold derived from %s for %d position(s)" % (src, len(rows))}


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


def _actionability(in_window, is_held, gain, *, position_first_entry_date=None, today=None):
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

    # ⚑⚑ ISA-0645 / R4.5 — THE VERDICT IS NOT DECIDED HERE. It is READ from
    #    `position_sizing.min_hold_ok`, which is where the profit/loss asymmetry is
    #    implemented. Until 12-Sep-2026 this function reimplemented that rule in prose while
    #    `min_hold_ok` sat with ZERO production call sites: two paths for one rule, which R4.5
    #    calls a defect on the day it is created. Worse, neither path ran — `min_hold` was
    #    read from a trades log that does not exist, so `gain` was always None and `in_window`
    #    always False, and the branch below has never once executed since 29-Jul-2026.
    #    Now: one home decides, this function renders.
    try:
        if gain is not None and not _finite(gain):
            gain = None          # B09-R10 (P-C, EV-0240): a NaN gain is UNRESOLVED, never a winner
    except Exception:                                                # noqa: BLE001
        gain = None
    if position_first_entry_date and gain is None:
        # ⚑ ISA-0729 (23-Sep-2026, found by the census bringing test_wpbc under a gate): a NULL gain
        #    was passed to min_hold_ok as at_a_loss=False, so an UNRESOLVED cost basis read as a
        #    winner and returned PROFIT_TAKING_REVIEW_PERMITTED - a null fed to a control returning
        #    the permissive verdict (FC-F, R4.3). Whether this is profit-taking (permitted) or C-1
        #    loss capitulation (blocked) is UNKNOWN until the gain resolves.
        return ("MIN_HOLD_UNEVALUATED — the position's gain/loss is unresolved, so whether a "
                "reduction would be profit-taking (permitted) or C-1 loss capitulation (blocked) "
                "cannot be decided. UNKNOWN, never a permission (R4.3/R2.10, ISA-0729).")
    if position_first_entry_date:
        try:
            import position_sizing as _ps
            v = _ps.min_hold_ok(position_first_entry_date=position_first_entry_date,
                                today=today, at_a_loss=bool(gain is not None and gain <= 0))
            if v.get("ok") and v.get("trim_only"):
                return ("PROFIT_TAKING_REVIEW_PERMITTED — %s (gain %s). Raj rule 29-Jul-26: "
                        "min-hold blocks loss capitulation (C-1), not profit taking. Any "
                        "reduction is a REVIEW OUTCOME, never this module's instruction; a "
                        "full exit still requires thesis-break or a declared MIN_HOLD_EXEMPT "
                        "ground. [verdict: position_sizing.min_hold_ok]"
                        % (v["basis"], ("%+.1f%%" % gain) if gain is not None else "unresolved"))
            if v.get("ok"):
                return ("REVIEW_AT_NEXT_SCHEDULED_RUN — %s [verdict: "
                        "position_sizing.min_hold_ok]" % v["basis"])
            return ("NOT_ACTIONABLE — %s. This is exactly the C-1 failure mode; exit requires "
                    "a genuine thesis-break or a declared MIN_HOLD_EXEMPT ground. [verdict: "
                    "position_sizing.min_hold_ok]" % v["basis"])
        except Exception as _e:                                      # noqa: BLE001
            # R4.3 — the control could not run, and that must not read as "not actionable
            # because it is at a loss". Name the failure.
            return ("MIN_HOLD_UNEVALUATED — position_sizing.min_hold_ok could not be evaluated "
                    "(%s: %s), so the 182-day verdict for this name is UNKNOWN, not a refusal. "
                    "Treat as unenforced and escalate (ISA-0645)."
                    % (type(_e).__name__, _e))

    # No entry date at all — the pre-ISA-0645 state, kept as an explicit REFUSAL rather than
    # a verdict, because "no entry date" and "at a loss" must not produce the same output.
    return ("MIN_HOLD_UNEVALUATED — no `position_first_entry_date` for this name, so the "
            "182-day window cannot be evaluated. Published as UNEVALUATED, never as "
            "NOT_ACTIONABLE (R2.10/R4.3, ISA-0645).")


def evaluate(store, tickers, held_set, min_hold, today=None, price_csv=None, asof=None, gain_pct=None):
    today = today or datetime.date.today().isoformat()
    weeks = int(_cfg("ALERT_TRAJECTORY_WEEKS"))
    wow_lim = float(_cfg("ALERT_EPS_WOW_DROP_PCT"))
    traj_lim = float(_cfg("ALERT_EPS_TRAJ_DROP_PCT"))
    tgt_lim = float(_cfg("ALERT_TARGET_CUT_PCT"))
    consec_lim = int(_cfg("ALERT_CONSEC_DOWN_WEEKS"))
    alerts, skipped = [], []
    asof_eff = asof or today
    max_age = int(_cfg("ALERT_SNAPSHOT_MAX_AGE_D"))
    for tk in tickers:
        eps = _series(store, tk, "eps_fwd1y", weeks, asof=asof_eff)
        tgt = _series(store, tk, "target_mean", weeks, asof=asof_eff)
        fired = []
        # ⚑ B09-R10 (P-C): a NaN as the NEWEST estimate is MISSING COVERAGE, typed - it used to vanish
        _raw_pts = sorted((p for p in store.get("series", {}).get((tk or "").upper(), [])
                           if _d(p.get("date")) is not None and _d(p.get("date")) <= _d(asof_eff)),
                          key=lambda p: p["date"])
        if _raw_pts and "eps_fwd1y" in _raw_pts[-1] and not _finite(_raw_pts[-1].get("eps_fwd1y")):
            skipped.append({"ticker": tk, "reason": "missing_latest_eps", "date": _raw_pts[-1]["date"]})
        # ⚑ B09-R10 (EV-0225): estimates older than ALERT_SNAPSHOT_MAX_AGE_D vs the as-of are STALE
        _stale = []
        for _nm, _ser in (("eps", eps), ("target", tgt)):
            if _ser and (_d(asof_eff) - _d(_ser[-1]["date"])).days > max_age:
                _stale.append(_nm)
        if "eps" in _stale:
            skipped.append({"ticker": tk, "reason": "stale_snapshot", "series": "eps_fwd1y",
                            "latest": eps[-1]["date"], "max_age_days": max_age})
            eps = []
        if "target" in _stale:
            skipped.append({"ticker": tk, "reason": "stale_snapshot", "series": "target_mean",
                            "latest": tgt[-1]["date"], "max_age_days": max_age})
            tgt = []
        # target change over the SAME window as the price move (EV-0233): points inside the window only
        win_tgt = [p for p in tgt if (_d(asof_eff) - _d(p["date"])).days <= int(_cfg("ALERT_PRICE_DROP_WINDOW_D")) + 7]
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
            if len(win_tgt) >= 2:
                tc = _pct(win_tgt[-1]["target_mean"], win_tgt[0]["target_mean"])
                if tc is not None and (tc - pmove) >= float(_cfg("ALERT_PRICE_VS_TARGET_DIVERGENCE_PP")):
                    fired.append({"rule": "price_target_divergence", "price_pct": pmove,
                                  "target_pct": tc, "divergence_pp": round(tc - pmove, 2),
                                  "target_window_days": (_d(win_tgt[-1]["date"]) - _d(win_tgt[0]["date"])).days,
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
            if len(win_tgt) >= 2:
                tc = _pct(win_tgt[-1]["target_mean"], win_tgt[0]["target_mean"])
                if tc is not None and (rmove - tc) >= float(_cfg("ALERT_PRICE_VS_TARGET_DIVERGENCE_PP")):
                    fired.append({"rule": "price_target_convergence", "price_pct": rmove,
                                  "target_pct": tc, "divergence_pp": round(rmove - tc, 2),
                                  "note": "price rose while the consensus target held or fell — "
                                          "the implied upside has been CONSUMED rather than "
                                          "earned; re-underwrite before treating the position "
                                          "as still carrying its entry case"})
        # ⚑ B09-R10 (EV-0226): the TARGET rule is evaluated whether or not EPS history exists - a 20% target
        #   cut on a name with no EPS snapshots used to be skipped as 'insufficient_history'.
        if len(tgt) >= 2:
            tc = _pct(tgt[-1]["target_mean"], tgt[0]["target_mean"])
            if tc is not None and tc <= tgt_lim:
                fired.append({"rule": "target_mean_cut", "value_pct": tc, "threshold_pct": tgt_lim,
                              "weeks": len(tgt),
                              "elapsed_days": (_d(tgt[-1]["date"]) - _d(tgt[0]["date"])).days})
        if len(eps) < 2:
            if not fired:
                skipped.append({"ticker": tk, "reason": "insufficient_history", "points": len(eps)})
                continue
            eps = []
        # ⚑ B09-R10 (EV-0222/0223/0230): elapsed time is MEASURED. Week-on-week needs two observations no
        #   more than ALERT_WEEKLY_GAP_MAX_D apart (a 92-day gap is not WoW); consecutive-down counts only
        #   weekly-spaced pairs; the trajectory publishes its real span.
        _pairs = _weekly_pairs(eps)
        wow = (_pct(eps[-1]["eps_fwd1y"], eps[-2]["eps_fwd1y"])
               if len(eps) >= 2 and _pairs and _pairs[-1][1] is eps[-1] else None)
        if wow is not None and wow <= wow_lim:
            fired.append({"rule": "eps_wow_drop", "value_pct": wow, "threshold_pct": wow_lim,
                          "elapsed_days": (_d(eps[-1]["date"]) - _d(eps[-2]["date"])).days})
        traj = _pct(eps[-1]["eps_fwd1y"], eps[0]["eps_fwd1y"]) if len(eps) >= 2 else None
        _span = (_d(eps[-1]["date"]) - _d(eps[0]["date"])).days if len(eps) >= 2 else 0
        if traj is not None and traj <= traj_lim:
            fired.append({"rule": "eps_trajectory_drop", "value_pct": traj, "threshold_pct": traj_lim,
                          "weeks": len(eps), "elapsed_days": _span,
                          "weekly_spacing": len(_pairs) == len(eps) - 1})
        cd = 0
        for _a, _b in reversed(_pairs):
            if _pairs and float(_b["eps_fwd1y"]) < float(_a["eps_fwd1y"]):
                cd += 1
            else:
                break
        if cd and _pairs[-1][1] is not eps[-1]:
            cd = 0                     # the newest observation is not weekly-linked: no current streak
        if cd >= consec_lim:
            fired.append({"rule": "eps_consecutive_down", "weeks": cd, "threshold_weeks": consec_lim})
        if not fired:
            continue
        is_held = tk in held_set
        mh_until = min_hold.get(tk.upper())
        if isinstance(mh_until, dict):
            # B09-R10 (P-C, EV-0241): the dict entry shape (min_hold_until + position_first_entry_date) used
            # to raise TypeError comparing a dict with a date string before anything rendered
            mh_until = mh_until.get("min_hold_until")
        in_window = bool(mh_until and str(mh_until) > str(today))
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
            "actionability": _actionability(
                in_window, is_held, (gain_pct or {}).get(tk.upper()),
                position_first_entry_date=((min_hold or {}).get(tk.upper()) or {}).get(
                    "position_first_entry_date")
                if isinstance((min_hold or {}).get(tk.upper()), dict) else
                _entry_from_ledger(tk),
                today=today),
        })
    return alerts, skipped


def newest_snapshot_date(store):
    """The newest dated observation in an eps_trend snapshot store ('' when none)."""
    best = ""
    for pts in ((store or {}).get("series") or {}).values():
        for p in pts or []:
            d = str((p or {}).get("date") or "")[:10]
            if d > best:
                best = d
    return best


def store_needs_refresh(snapshot_newest, store_as_of):
    """True when the snapshot store holds an observation NEWER than the alert store's as-of (or the alert
    store has none). Equal dates -> False (the store already reflects that snapshot)."""
    s = str(snapshot_newest or "")[:10]
    return bool(s) and s > str(store_as_of or "")[:10]


def prefetch_closes(tickers, *, workers=8, budget_s=45.0, period="3mo"):
    """Fetch daily closes for `tickers` in parallel into one close matrix (DataFrame) within a time
    budget. Returns (frame_or_None, {ticker: reason}) - a ticker that could not be fetched in time is
    NAMED, and its price rules then read None (never a fabricated move)."""
    import concurrent.futures as _cf
    import time as _t
    import pandas as pd
    missing, cols = {}, {}
    try:
        import yfinance as yf
    except Exception as e:                                          # noqa: BLE001
        return None, {t: "YFINANCE_UNAVAILABLE:%s" % type(e).__name__ for t in tickers}

    def _one(t):
        h = yf.Ticker(t).history(period=period, auto_adjust=False)
        s = h["Close"].dropna()
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_localize(None)
        return s

    t0 = _t.time()
    ex = _cf.ThreadPoolExecutor(max_workers=max(1, int(workers)))
    futs = {ex.submit(_one, t): t for t in tickers}
    try:
        for f in _cf.as_completed(futs, timeout=max(1.0, budget_s)):
            t = futs[f]
            try:
                s = f.result()
                if len(s):
                    cols[t] = s
                else:
                    missing[t] = "NO_PRICE_HISTORY"
            except Exception as e:                                  # noqa: BLE001
                missing[t] = "FETCH_FAILED:%s" % type(e).__name__
    except _cf.TimeoutError:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for t in tickers:
        if t not in cols and t not in missing:
            missing[t] = "FETCH_BUDGET_EXCEEDED(%.0fs)" % (_t.time() - t0)
    if not cols:
        return None, missing
    return pd.DataFrame(cols).sort_index(), missing


def build_payload(store, held, watch, *, today, price_csv=None, asof=None, producer="position_alerts CLI",
                  price_basis=None):
    """ONE payload builder for the CLI and the pre-run refresh (the store contract is identical)."""
    min_hold = load_min_hold(None)          # -> transaction_ledger.json first BUY (ISA-0645)
    alerts, skipped = evaluate(store, held + watch, set(held), min_hold, today=today,
                               price_csv=price_csv, asof=asof, gain_pct={})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "as_of_date": today or datetime.date.today().isoformat(),
        "producer": producer,
        "snapshot_newest_date": newest_snapshot_date(store),
        "price_basis": price_basis or ("cache:%s" % os.path.basename(price_csv) if price_csv else "yfinance live"),
        "scope": {"held": held, "watchlist": watch,
                  "n_evaluated": len(held) + len(watch), "n_insufficient_history": len(skipped)},
        "thresholds": {k: _cfg(k) for k in DEFAULTS},
        "alerts": alerts,
        "insufficient_history": skipped,
        "min_hold_source": "transaction_ledger.json (first BUY; ISA-0645/0761)",
        "gain_basis": ("UNAVAILABLE - no cost-basis store is read by this CLI; in-window "
                       "winners/losers degrade to NOT_ACTIONABLE (ISA-0761)"),
        "consumed_by": ["monthly_isa_prerun (Step 5 context)", "isa-mid-month-intelligence-brief"],
        "doctrine": ("Detection only. Never an execution trigger. Alerts are context for the next "
                     "SCHEDULED review; the framework 182-day min-hold (C-1) is unaffected."),
    }


def refresh_store(out_path, snapshots_path, watchlist_path, *, today, workers=8, budget_s=45.0):
    """⚑ P-C (24-Oct-2026, found by the P-C shadow): the weekly task kept writing EPS snapshots (16/23-Oct)
    but position_alerts.json stopped at 25-Sep, so the pre-run surfaced 4-week-old alerts as Step 5
    context. The pre-run now re-evaluates the store whenever the snapshots are NEWER than the alerts,
    at the run date, with prices prefetched in parallel inside a time budget (unfetched names are named;
    their price rules read None). Returns (payload_or_None, info)."""
    import tempfile as _tf
    store = json.load(open(snapshots_path, encoding="utf-8"))
    held, watch = scope_tickers(watchlist_path)
    frame, missing = prefetch_closes(held + watch, workers=workers, budget_s=budget_s)
    tmp = None
    try:
        if frame is not None:
            fd, tmp = _tf.mkstemp(suffix="_alert_closes.csv")
            os.close(fd)
            frame.to_csv(tmp)
        payload = build_payload(store, held, watch, today=today, price_csv=tmp, asof=today,
                                producer="monthly_isa_prerun Step 9b.5 refresh (P-C)",
                                price_basis=("yfinance prefetch (%d of %d names)" % (
                                    0 if frame is None else frame.shape[1], len(held) + len(watch))))
        payload["price_unresolved"] = missing
        tmp_out = out_path + ".tmp"
        json.dump(payload, open(tmp_out, "w", encoding="utf-8"), indent=2)
        os.replace(tmp_out, out_path)
        return payload, {"refreshed": True, "price_unresolved": len(missing)}
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def _parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default="eps_trend_snapshots.json")
    ap.add_argument("--watchlist", default="watchlist_tickers.json")
    ap.add_argument("--out", default="position_alerts.json")
    ap.add_argument("--price-cache", dest="price_cache", default=None,
                    help="cached close matrix (CSV) — offline/reproducible price rules; omit for live yfinance")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--quiet", action="store_true")
    return ap


def main():
    # ⚑ ISA-0761: the trades-log flag is RETIRED. An installed task that still passes it must not
    #   crash (installed-task text is unreadable from a session, ISA-0537), so unknown arguments
    #   are tolerated, WARNED, and never read.
    a, _extra = _parser().parse_known_args()
    if _extra:
        print("WARNING (ISA-0761): ignoring retired/unknown argument(s) %s - min-hold comes from "
              "transaction_ledger.json" % _extra, file=sys.stderr)

    if not os.path.exists(a.snapshots):
        print("NO_SNAPSHOTS %s — weekly EPS task has not written yet; nothing to evaluate." % a.snapshots)
        return 0
    store = json.load(open(a.snapshots, encoding="utf-8"))
    held, watch = scope_tickers(a.watchlist)
    # No cost-basis store is read by this CLI (the trades log never existed): gain is UNAVAILABLE
    # and the winners/losers rule degrades to NOT_ACTIONABLE — never to a permissive verdict.
    payload = build_payload(store, held, watch, today=a.today, price_csv=a.price_cache, asof=a.asof)
    alerts, skipped = payload["alerts"], payload["insufficient_history"]
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


def _selftest(verbose: bool = True) -> int:
    """R5.5 — ISA-0645's min-hold derivation, with the controls that must fail.

    liveness_ref: position_alerts._selftest"""
    import json as _json
    import os as _os
    import tempfile as _tf
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    here = _os.path.dirname(_os.path.abspath(__file__))
    st = min_hold_state(root=here)
    ok(st["enforced"] is True and st["source"] == "transaction_ledger",
       "⚑ MUST-FIRE (R5.10): the 182-day min-hold must be ENFORCED from the transaction "
       "ledger. It had NO live enforcement path from 29-Jul-2026 because load_min_hold read "
       "project_isa_trades_log.md, which has never existed: %r" % st)
    ok(st["positions"].get("ABCL", {}).get("min_hold_until") == "2027-01-08",
       "ABCL's entry 2026-07-10 + 182d must give 2027-01-08: %r"
       % st["positions"].get("ABCL"))

    # ⚑ NEGATIVE CONTROL — the MMF must be EXCLUDED, or B2's recall leg self-blocks.
    ok("CSH2" not in st["positions"],
       "⚑ NEGATIVE CONTROL: CSH2 is the B2 cash-sweep destination and the ledger classifies it "
       "as asset_class 'stock'. If it inherited a 182-day STOCK hold, the recall leg — 'when "
       "the next opportunity arises, trim funds and move capital into stocks' — would be "
       "blocked by an anti-churn rule that exists to stop stock churn: %r"
       % sorted(st["positions"]))

    # ⚑ NEGATIVE CONTROL — no entry date must yield UNEVALUATED, never NOT_ACTIONABLE.
    a = _actionability(True, True, 5.0, position_first_entry_date=None)
    ok(a.startswith("MIN_HOLD_UNEVALUATED"),
       "⚑ NEGATIVE CONTROL: 'the control could not run' and 'the position is at a loss' must "
       "not produce the same output. For seven weeks they did (R2.10): %r" % a[:90])

    # positive controls: the asymmetry that had never once executed since 29-Jul-2026
    win = _actionability(True, True, 62.0, position_first_entry_date="2026-07-10",
                         today="2026-09-12")
    los = _actionability(True, True, -17.7, position_first_entry_date="2026-08-10",
                         today="2026-09-12")
    ok(win.startswith("PROFIT_TAKING_REVIEW_PERMITTED"),
       "inside min-hold and IN PROFIT -> a trim is permitted: %r" % win[:70])
    ok(los.startswith("NOT_ACTIONABLE"),
       "inside min-hold and AT A LOSS -> not actionable, the C-1 failure mode: %r" % los[:70])
    ok("min_hold_ok" in win and "min_hold_ok" in los,
       "and BOTH verdicts must cite position_sizing.min_hold_ok — the verdict is READ from the "
       "one home, not reimplemented here (R4.5)")

    # ⚑ NEGATIVE CONTROL — an absent ledger must report UNENFORCED, never 'nothing in window'.
    d = _tf.mkdtemp()
    st2 = min_hold_state(root=d)
    ok(st2["enforced"] is False and st2["positions"] == {} and st2["why"],
       "⚑ NEGATIVE CONTROL: with no ledger the control must report UNENFORCED with a reason, "
       "not an empty-and-therefore-clean result: %r" % st2)
    # ISA-0761 — the trades-log flag is retired from the CLI and never read
    _retired = "--" + "trades-log"      # built, so the no-trades-log code scan does not match its own test
    _opts = {o for act in _parser()._actions for o in act.option_strings}
    ok(_retired not in _opts,
       "ISA-0761: the CLI must no longer accept the retired trades-log flag: %r" % sorted(_opts))
    _a, _x = _parser().parse_known_args([_retired, "x.md", "--quiet"])
    ok(_x == [_retired, "x.md"] and _a.quiet is True,
       "NEGATIVE CONTROL (ISA-0761): an installed task still passing the flag is tolerated as an "
       "ignored extra, never parsed into an input: %r" % (_x,))
    if verbose:
        print("position_alerts._selftest: %d assertions, 0 failed" % n)
    return n
