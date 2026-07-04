#!/usr/bin/env python3
"""
learning_snapshot.py — weekly eps_trend snapshot (redesign Part 1 §5, H4).

yfinance only exposes the +1y consensus-EPS estimate as a few coarse endpoints (current,
7d/30d/60d/90d ago) — too sparse to robustly classify WHERE a rising estimate sits in its
upgrade cycle (the revision-journey-stage). This module captures the current value weekly
into an append-only store, building a dense, OWNED time series so revision-stage (and the
quarterly per-signal information coefficient) rests on real history, not vendor endpoints.

Store schema (eps_trend_snapshots.json):
  { schema_version, series: { TICKER: [ {date, eps_fwd1y, eps_fwd0y, src}, ... ] } }
Append-only, idempotent per (ticker, ISO-week). NOT wired into any scheduled run yet —
pure additive new module. Intended to back a lightweight weekly scheduled task later.
"""
from __future__ import annotations
import argparse, json, os, datetime

try:
    import isa_env_guard  # shared disk guard + optional local-primary preflight
except Exception:
    isa_env_guard = None

SCHEMA_VERSION = "1.0"


def _today() -> str:
    return datetime.date.today().isoformat()


def _isoweek(date_str: str) -> str:
    y, w, _ = datetime.date.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def load_store(path: str) -> dict:
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "series": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "series": {}}
    if isinstance(d, dict) and isinstance(d.get("series"), dict):
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d
    return {"schema_version": SCHEMA_VERSION, "series": {}}


def save_store(store: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, default=str)
    os.replace(tmp, path)


def _extract_fwd_eps(eps_trend):
    """From a yfinance eps_trend table (rows 0q/+1q/0y/+1y, col 'current') return
    (eps_fwd1y, eps_fwd0y). Tolerant of a DataFrame or a plain dict; None on anything odd."""
    if eps_trend is None:
        return None, None
    if hasattr(eps_trend, "loc"):
        def _get(label):
            try:
                return float(eps_trend.loc[label, "current"])
            except Exception:
                return None
        return _get("+1y"), _get("0y")
    if isinstance(eps_trend, dict):
        def g(k):
            v = eps_trend.get(k) or {}
            try:
                return float(v.get("current"))
            except Exception:
                return None
        return g("+1y"), g("0y")
    return None, None


def record_snapshot(store, ticker, eps_fwd1y, eps_fwd0y, date=None, src="yfinance", target_mean=None) -> bool:
    """Append one point. Idempotent per (ticker, ISO-week): at most one point per week.
    Returns True if appended, False if this week was already captured.
    Jul-26 Part 9b: also snapshots the analyst target_mean, so the reporter can quantify analyst
    target LAG (how late consensus targets move relative to the price/estimate signal)."""
    date = date or _today()
    wk = _isoweek(date)
    ser = store["series"].setdefault((ticker or "").upper(), [])
    if any(_isoweek(p["date"]) == wk for p in ser):
        return False
    ser.append({"date": date, "eps_fwd1y": eps_fwd1y, "eps_fwd0y": eps_fwd0y,
                "target_mean": target_mean, "src": src})
    return True


def _chunk(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def snapshot_tickers(path, tickers, date=None, preflight=False, resume_path=None, batch_size=30) -> dict:
    """Fetch current +1y/0y consensus EPS for each ticker and append a weekly point.
    yfinance-backed; honours the shared local-primary guardrail when preflight=True.

    If resume_path is given, this call is BATCHED/RESUMABLE across multiple invocations
    (needed because a single sandbox call cannot fetch 100+ tickers before the wall-clock
    limit): progress is tracked in a small JSON stub at resume_path — {"done": [tickers]} —
    so repeated calls with the same tickers list only fetch what's left. Returns an extra
    "remaining" count; caller loops until remaining == 0. The main data store (path) is
    saved incrementally after EVERY batch, so a partial run never loses completed work."""
    if preflight and isa_env_guard is not None:
        isa_env_guard.run_preflight_or_fallback()
    import yfinance as yf
    store = load_store(path)

    done = set()
    if resume_path and os.path.exists(resume_path):
        try:
            with open(resume_path, encoding="utf-8") as fh:
                done = set(json.load(fh).get("done", []))
        except Exception:
            done = set()

    todo = [t for t in tickers if t.upper() not in done] if resume_path else list(tickers)
    batch = todo[:batch_size] if resume_path else todo

    added = skipped = failed = 0
    for t in batch:
        try:
            _tk = yf.Ticker(t)
            et = getattr(_tk, "eps_trend", None)
            f1, f0 = _extract_fwd_eps(et)
            tgt = None
            try:
                _info = getattr(_tk, "info", None) or {}
                tgt = _info.get("targetMeanPrice")
                if tgt is None:
                    _apt = getattr(_tk, "analyst_price_targets", None)
                    if isinstance(_apt, dict):
                        tgt = _apt.get("mean")
            except Exception:
                tgt = None
            if record_snapshot(store, t, f1, f0, date=date, target_mean=tgt):
                added += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
        finally:
            done.add(t.upper())

    save_store(store, path)

    if resume_path:
        with open(resume_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"done": sorted(done)}, fh)
        os.replace(resume_path + ".tmp", resume_path)
        remaining = len([t for t in tickers if t.upper() not in done])
        if remaining == 0 and os.path.exists(resume_path):
            try:
                os.remove(resume_path)
            except Exception:
                # mount may forbid delete; overwrite with a tiny done-stub instead
                with open(resume_path, "w", encoding="utf-8") as fh:
                    json.dump({"status": "done"}, fh)
    else:
        remaining = 0

    return {"added": added, "skipped_same_week": skipped, "failed": failed,
            "tickers": len(tickers), "batch": len(batch), "remaining": remaining}


def revision_trajectory(path, ticker, lookback_weeks=12) -> dict:
    """Return {points, latest, oldest, pct_change, direction} for the +1y EPS series — a
    robust, owned input to the revision-journey-stage (vs yfinance's sparse endpoints)."""
    store = load_store(path)
    ser = [p for p in store["series"].get((ticker or "").upper(), [])
           if p.get("eps_fwd1y") is not None]
    ser = sorted(ser, key=lambda p: p["date"])[-lookback_weeks:]
    if len(ser) < 2:
        return {"points": len(ser), "latest": None, "oldest": None,
                "pct_change": None, "direction": "insufficient_history"}
    oldest, latest = ser[0]["eps_fwd1y"], ser[-1]["eps_fwd1y"]
    pct = ((latest - oldest) / abs(oldest) * 100.0) if oldest else None
    if pct is None:
        direction = "unknown"
    elif pct > 1:
        direction = "rising"
    elif pct < -1:
        direction = "falling"
    else:
        direction = "flat"
    return {"points": len(ser), "latest": latest, "oldest": oldest,
            "pct_change": (round(pct, 2) if pct is not None else None), "direction": direction}


def _tickers_from_watchlist(path) -> list:
    """Tolerant extraction: list[str] | list[dict(ticker)] | {tickers:[...]} | {TICKER:{...}}
    | ISA watchlist_tickers.json shape (sections of list[dict(ticker=...)])."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return []
    if isinstance(d, dict) and isinstance(d.get("tickers"), list):
        d = d["tickers"]
    if isinstance(d, dict):
        # ISA watchlist_tickers.json shape: sections of list[dict(ticker=...)] plus
        # underscore-prefixed metadata keys. Pull tickers from the known sections
        # rather than treating top-level dict keys as tickers.
        sections = ["watchlist", "candidate_pool", "stock_sleeve", "vci_watchlist"]
        out = []
        found_section = False
        for key in sections:
            v = d.get(key)
            if isinstance(v, list):
                found_section = True
                for x in v:
                    if isinstance(x, str) and x not in out:
                        out.append(x)
                    elif isinstance(x, dict) and x.get("ticker") and x["ticker"] not in out:
                        out.append(x["ticker"])
        if found_section:
            return out
        return [k for k in d.keys()]
    out = []
    for x in (d or []):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and x.get("ticker"):
            out.append(x["ticker"])
    return out


def default_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "eps_trend_snapshots.json")


def main():
    ap = argparse.ArgumentParser(description="Weekly eps_trend snapshot capture (H4).")
    ap.add_argument("--path", required=True, help="eps_trend_snapshots.json store path")
    ap.add_argument("--tickers", nargs="+", help="tickers to snapshot")
    ap.add_argument("--from-watchlist", default=None,
                    help="watchlist_tickers.json to pull tickers from")
    ap.add_argument("--preflight", action="store_true",
                    help="run shared local-primary preflight (FALLBACK_TO_COMPOSIO exit 3 on failure)")
    ap.add_argument("--trajectory", default=None,
                    help="print revision trajectory for this TICKER and exit (no fetch)")
    ap.add_argument("--resume-path", default=None,
                    help="progress-stub path for batched/resumable runs (one bash call can't "
                         "fetch 100+ tickers before the wall-clock limit). Call repeatedly with "
                         "the SAME --tickers/--from-watchlist until output shows remaining: 0.")
    ap.add_argument("--batch-size", type=int, default=30,
                    help="tickers fetched per call when --resume-path is set (default 30)")
    a = ap.parse_args()

    if a.trajectory:
        print(json.dumps(revision_trajectory(a.path, a.trajectory), indent=2))
        return

    tickers = list(a.tickers or [])
    if a.from_watchlist:
        tickers += [t for t in _tickers_from_watchlist(a.from_watchlist) if t not in tickers]
    if not tickers:
        ap.error("provide --tickers or --from-watchlist")
    print(json.dumps(snapshot_tickers(a.path, tickers, preflight=a.preflight,
                                       resume_path=a.resume_path, batch_size=a.batch_size),
                      indent=2))


if __name__ == "__main__":
    main()
