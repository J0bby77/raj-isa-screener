#!/usr/bin/env python3
"""
enrich_volatility.py  --  Volatility / ATR enrichment for the entry-level builder.

Computes a true technical anchor input (realised volatility + ATR) from daily
price history. Two uses:

  1. STANDALONE (current-month enrichment, no full re-fetch):
        python3 enrich_volatility.py --metrics watchlist_metrics_mmm_yyyy.json
     Pulls 1y daily history for every ticker in the metrics file and writes
     these fields back into each ticker record:
        _realised_vol   annualised realised volatility (log returns x sqrt(252))
        _atr_pct        14-day ATR as a fraction of current price
        _vol_profile    "low" | "normal" | "high" | "unknown"
        _vol_source     "history_1y" | "history_2y" | "unavailable"

  2. LIBRARY (live integration): fetch_watchlist_metrics.py imports
     compute_volatility_metrics(history_df, current_price) and calls it inside
     the per-ticker loop where data["history"] is already pulled (no extra
     network round-trip in the live pipeline).

The realised-vol / ATR ratios are currency-agnostic (they are ratios), so no
GBp/pence correction is required here.
"""
from __future__ import annotations
import argparse, json, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Volatility profile cut points (annualised realised vol)
VOL_LOW_MAX    = 0.30   # < 0.30  -> low-vol compounder
VOL_NORMAL_MAX = 0.50   # 0.30-0.50 -> normal growth ; > 0.50 -> high beta / cyclical


def classify_vol_profile(realised_vol: float | None) -> str:
    if realised_vol is None:
        return "unknown"
    if realised_vol < VOL_LOW_MAX:
        return "low"
    if realised_vol <= VOL_NORMAL_MAX:
        return "normal"
    return "high"


def compute_volatility_metrics(history, current_price: float | None) -> dict:
    """
    Compute realised volatility + 14d ATR% from a yfinance history DataFrame.
    Returns dict: realised_vol, atr_pct, vol_profile, vol_source.
    Safe on None/empty/short history -> returns unknowns.
    """
    out = {"realised_vol": None, "atr_pct": None,
           "vol_profile": "unknown", "vol_source": "unavailable"}
    try:
        import numpy as np
    except Exception:
        return out
    if history is None or getattr(history, "empty", True):
        return out
    if "Close" not in history.columns:
        return out
    close = history["Close"].dropna()
    if len(close) < 30:
        return out

    # Annualised realised volatility from daily log returns
    rets = np.log(close / close.shift(1)).dropna()
    if len(rets) >= 20:
        rv = float(rets.std() * np.sqrt(252))
        out["realised_vol"] = round(rv, 4)

    # 14-day ATR as a fraction of current price
    if {"High", "Low"}.issubset(history.columns):
        tr = (history["High"] - history["Low"]).dropna()
        if len(tr) >= 14:
            atr = float(tr.rolling(14).mean().iloc[-1])
            ref = current_price or float(close.iloc[-1])
            if ref:
                out["atr_pct"] = round(atr / ref, 4)

    out["vol_profile"] = classify_vol_profile(out["realised_vol"])
    out["vol_source"]  = "history"
    return out


def _fetch_one(sym: str, current_price: float | None) -> tuple[str, dict]:
    try:
        import yfinance as yf
        tk = yf.Ticker(sym)
        h = None
        for per in ("1y", "2y"):
            try:
                h = tk.history(period=per)
                if h is not None and not h.empty:
                    break
            except Exception:
                h = None
        m = compute_volatility_metrics(h, current_price)
        if m["vol_source"] == "history":
            m["vol_source"] = "history_1y"
        return sym, m
    except Exception:
        return sym, {"realised_vol": None, "atr_pct": None,
                     "vol_profile": "unknown", "vol_source": "unavailable"}


def enrich_metrics_file(metrics_path: str, workers: int = 8) -> dict:
    with open(metrics_path, encoding="utf-8") as f:
        data = json.load(f)
    tickers = data.get("tickers", {})
    syms = list(tickers.keys())
    print(f"[enrich] {len(syms)} tickers -> pulling history ({workers} workers)")

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, s, tickers[s].get("current_price")): s
                for s in syms}
        for fut in as_completed(futs):
            sym, m = fut.result()
            results[sym] = m

    ok = 0
    profiles = {"low": 0, "normal": 0, "high": 0, "unknown": 0}
    for sym, m in results.items():
        t = tickers[sym]
        t["_realised_vol"] = m["realised_vol"]
        t["_atr_pct"]      = m["atr_pct"]
        t["_vol_profile"]  = m["vol_profile"]
        t["_vol_source"]   = m["vol_source"]
        profiles[m["vol_profile"]] = profiles.get(m["vol_profile"], 0) + 1
        if m["realised_vol"] is not None:
            ok += 1

    data.setdefault("_meta", {})["volatility_enriched"] = True
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[enrich] vol computed for {ok}/{len(syms)} | profiles={profiles}")
    return profiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", required=True, help="watchlist_metrics_mmm_yyyy.json to enrich in place")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--preflight", action="store_true",
                    help="Local-primary preflight (yfinance/dev-shm/Yahoo). On failure prints "
                         "FALLBACK_TO_COMPOSIO and exits 3. Default off = review run unchanged.")
    args = ap.parse_args()
    if not os.path.exists(args.metrics):
        print(f"ERROR: {args.metrics} not found", file=sys.stderr); sys.exit(1)
    # Local-primary guardrail parity (opt-in). Fails over to Composio (exit 3) when the local
    # sandbox can't fetch — the same decision screener_local makes for the growth path.
    if getattr(args, "preflight", False):
        try:
            import isa_env_guard as _guard
            _guard.run_preflight_or_fallback(outputs_dir=os.path.dirname(os.path.abspath(args.metrics)))
        except SystemExit:
            raise
        except Exception:
            pass
    enrich_metrics_file(args.metrics, args.workers)


if __name__ == "__main__":
    main()
