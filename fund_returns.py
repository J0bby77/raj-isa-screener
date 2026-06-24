#!/usr/bin/env python3
"""
fund_returns.py — fund-sleeve return sourcing + the real 12% gate (redesign retro #5, G1/G2).

The monthly review left the fund-sleeve return gate "pending" — est_return_pct=None on every fund,
filled manually from Morningstar each month. This module automates it as far as the data allows:

  SOURCING (hybrid, in priority order, per fund):
    1. fresh CACHE entry (fund_returns_cache.json) — quarterly; the reliable path for OEIC funds that
       have no exchange ticker (Fundsmith etc.), populated once from Morningstar and re-sourced quarterly.
    2. yfinance — for ticker-able funds/ETFs (e.g. *.L): info['threeYearAverageReturn'] if present,
       else a 3yr price-history CAGR proxy (price-only, so dividends understate — flagged in `source`).
    3. PENDING — neither available -> flagged for manual lookup (the old behaviour, for that fund only).

  GATE: value-weighted fund-sleeve return across COVERED funds; PASS/FAIL vs FUND_GATE_PCT only when
        coverage >= FUND_MIN_COVERAGE of fund-sleeve value (else PENDING — never a false PASS/FAIL).

Pure-computation parts (gate, classification, cache) are deterministic and unit-tested; the live
yfinance fetch is best-effort and injectable (fetch_fn) so callers/tests stay deterministic.
Additive: nothing here runs unless a caller invokes it (portfolio_analytics gates it behind a flag).
"""
from __future__ import annotations
import argparse, json, os, datetime

try:
    import scoring_config as _cfg
except Exception:
    _cfg = None
try:
    import action_language as _alang
except Exception:
    _alang = None

SCHEMA_VERSION = "1.0"


def _g(key, default):
    return getattr(_cfg, key, default)


def _today() -> str:
    return datetime.date.today().isoformat()


def _key(fund: dict) -> str:
    return str(fund.get("ticker") or fund.get("name") or "").upper()


# --- cache -----------------------------------------------------------------
def load_cache(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "returns": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("returns"), dict):
            d.setdefault("schema_version", SCHEMA_VERSION)
            return d
    except Exception:
        pass
    return {"schema_version": SCHEMA_VERSION, "returns": {}}


def save_cache(cache: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, default=str)
    os.replace(tmp, path)


def _is_stale(date_str, max_days=None) -> bool:
    max_days = _g("FUND_RETURN_STALE_DAYS", 92) if max_days is None else max_days
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(str(date_str)[:10])).days
        return age > max_days
    except Exception:
        return True


# --- yfinance best-effort fetch -------------------------------------------
def fetch_yf_return(ticker: str):
    """Best-effort 3yr annualised total return (%) for a ticker-able fund/ETF. Returns
    (pct|None, source_str). Never raises."""
    try:
        import yfinance as yf
    except Exception:
        return None, "yfinance_unavailable"
    try:
        tk = yf.Ticker(ticker)
        info = getattr(tk, "info", {}) or {}
        tar = info.get("threeYearAverageReturn")
        if tar is not None:
            return round(float(tar) * 100, 2), "yfinance_3yr_avg_return"
        hist = tk.history(period="3y")
        closes = [float(c) for c in hist["Close"].tolist()] if hist is not None and not hist.empty else []
        closes = [c for c in closes if c and c > 0]
        if len(closes) >= 2:
            yrs = max(1.0, len(closes) / 252.0)
            cagr = (closes[-1] / closes[0]) ** (1.0 / yrs) - 1
            return round(cagr * 100, 2), "yfinance_price_cagr(px-only,div-understated)"
    except Exception:
        pass
    return None, "fetch_failed"


# --- sourcing --------------------------------------------------------------
def source_fund_returns(funds: list, cache_path: str = None, fetch: bool = True,
                        fetch_fn=None) -> dict:
    """Return {KEY: {est_return_pct, source, stale, pending}} for each fund, using
    cache -> yfinance -> pending. fetch_fn(ticker)->(pct,source) overrides the live fetch (tests)."""
    fetch_fn = fetch_fn or fetch_yf_return
    cache = load_cache(cache_path) if cache_path else {"returns": {}}
    out = {}
    for f in funds:
        k = _key(f)
        c = cache.get("returns", {}).get(k)
        if c and c.get("return_pct") is not None and not _is_stale(c.get("date")):
            out[k] = {"est_return_pct": c["return_pct"], "source": c.get("source", "cache"),
                      "stale": False, "pending": False}
            continue
        pct, src = (fetch_fn(f.get("ticker")) if (fetch and f.get("ticker")) else (None, "no_ticker"))
        if pct is not None:
            out[k] = {"est_return_pct": pct, "source": src, "stale": False, "pending": False,
                      "low_confidence": ("price_cagr" in (src or ""))}   # M5: price-only CAGR understates total return
        elif c and c.get("return_pct") is not None:
            # only a STALE cache value available — use it but flag stale for re-sourcing
            out[k] = {"est_return_pct": c["return_pct"], "source": c.get("source", "cache") + "(stale)",
                      "stale": True, "pending": False}
        else:
            out[k] = {"est_return_pct": None, "source": src, "stale": False, "pending": True}
    return out


# --- the 12% gate ----------------------------------------------------------
def compute_fund_gate(funds: list, returns: dict, gate_pct: float = None,
                      min_coverage: float = None) -> dict:
    """Value-weighted fund-sleeve return across COVERED funds + PASS/FAIL vs gate.
    PENDING (no PASS/FAIL) until coverage >= min_coverage of fund-sleeve value."""
    gate_pct = _g("FUND_GATE_PCT", 12.0) if gate_pct is None else gate_pct
    min_coverage = _g("FUND_MIN_COVERAGE", 0.80) if min_coverage is None else min_coverage
    total_val = sum((f.get("value_gbp") or 0) for f in funds)
    covered_val = w_ret = 0.0
    pending = []
    for f in funds:
        r = returns.get(_key(f), {})
        v = f.get("value_gbp") or 0
        # M5: a price-only CAGR understates total return (no dividends) -> exclude it from the PASS/FAIL
        # set (treat as pending) so an understated number can never produce a false FAIL.
        if r.get("est_return_pct") is not None and not r.get("low_confidence"):
            covered_val += v
            w_ret += v * r["est_return_pct"]
        else:
            pending.append(f.get("ticker") or f.get("name"))
    coverage = (covered_val / total_val) if total_val > 0 else 0.0
    weighted_avg = round(w_ret / covered_val, 2) if covered_val > 0 else None
    if coverage < min_coverage or weighted_avg is None:
        result = "PENDING"
    else:
        result = "PASS" if weighted_avg >= gate_pct else "FAIL"
    return {
        "weighted_avg_return": weighted_avg,
        "threshold_pct": gate_pct,
        "result": result,
        "coverage_pct": round(coverage * 100, 1),
        "covered_value_gbp": round(covered_val, 2),
        "pending_funds": pending,
        "status": "computed" if result != "PENDING" else "pending_estimated_returns",
    }


# --- fund actions (G2) -----------------------------------------------------
def classify_fund_action(fund_row: dict, ret_info: dict) -> dict | None:
    """Map a fund's drift + return into an action for the Global Action Stack agenda.
    Returns None for a clean Hold (no action). Uses canonical action language."""
    band_breach = str(fund_row.get("band_breach")) == "Yes"
    actual = fund_row.get("actual_pct")
    target = fund_row.get("target_pct")
    min_ret = fund_row.get("min_return_pct")
    est = (ret_info or {}).get("est_return_pct")
    overweight = (actual is not None and target is not None and actual > target)

    action = reason = None
    if band_breach and overweight:
        action, reason = "TRIM", f"overweight {actual:.1f}% vs target {target:.1f}% — rebalance down"
    elif band_breach and not overweight:
        action, reason = "ADD", f"underweight {actual:.1f}% vs target {target:.1f}% — rebalance up"
    if est is not None and min_ret is not None and est < min_ret:
        # return below the fund's minimum-expected hurdle -> sell/replace review (overrides rebalance-add)
        action, reason = "SELL", f"est return {est:.1f}% < min hurdle {min_ret:.1f}% — review for replacement"
    if action is None:
        return None
    canon = _alang.normalize_action(action) if _alang else action
    label = _alang.label_for(action) if _alang else action
    return {
        "ticker": fund_row.get("ticker"), "name": fund_row.get("name"), "route": "fund",
        "action": action, "canonical_action": canon, "action_label": label,
        "reason": reason, "est_return_pct": est, "min_return_pct": min_ret,
        "actual_pct": actual, "target_pct": target,
        "source_required": bool((ret_info or {}).get("pending")),
    }


def default_cache_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "fund_returns_cache.json")


def set_cached_return(path, ticker, return_pct, source="manual", date=None) -> dict:
    """Write a fund's 3yr annualised return into the cache. Source-agnostic — `source` records
    provenance (ajbell / morningstar / hl / manual). This is how the quarterly OEIC refresh lands."""
    cache = load_cache(path)
    entry = {"return_pct": round(float(return_pct), 2), "source": source, "date": date or _today()}
    cache.setdefault("returns", {})[str(ticker).upper()] = entry
    save_cache(cache, path)
    return entry


def main():
    ap = argparse.ArgumentParser(
        description="Fund-return cache (G1) — set / get / list cached 3yr annualised fund returns. "
                    "Source-agnostic: populate from AJ Bell, Morningstar or HL at the quarterly refresh.")
    ap.add_argument("--path", required=True, help="fund_returns_cache.json path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="write a fund's 3yr annualised return to the cache")
    s.add_argument("--ticker", required=True)
    s.add_argument("--return", dest="ret", type=float, required=True, help="3yr annualised return %%")
    s.add_argument("--source", default="manual", help="provenance: ajbell / morningstar / hl / manual")
    s.add_argument("--date", default=None, help="assessment date YYYY-MM-DD (default today)")
    g = sub.add_parser("get", help="show one fund's cached return + staleness")
    g.add_argument("--ticker", required=True)
    sub.add_parser("list", help="list all cached returns + staleness")
    a = ap.parse_args()
    if a.cmd == "set":
        e = set_cached_return(a.path, a.ticker, a.ret, a.source, a.date)
        print(f"SET {a.ticker.upper()} = {e['return_pct']}% (source={e['source']}, date={e['date']})")
    elif a.cmd == "get":
        e = load_cache(a.path).get("returns", {}).get(a.ticker.upper())
        print(json.dumps({**e, "stale": _is_stale(e.get("date"))}, indent=2) if e
              else f"NO_CACHE for {a.ticker.upper()}")
    elif a.cmd == "list":
        rows = sorted(load_cache(a.path).get("returns", {}).items())
        if not rows:
            print("(empty cache)")
        for t, e in rows:
            flag = "STALE" if _is_stale(e.get("date")) else "ok"
            print(f"{t:14} {str(e.get('return_pct')):>7}%  {flag:5}  {e.get('source','')}  {e.get('date','')}")


if __name__ == "__main__":
    main()
