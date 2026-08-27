#!/usr/bin/env python3
"""
stock_return_store.py — the weekly GBP total-return store for the direct-stock sleeve.
V2.1-B (ISA-0355). Authority: ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md s7; amendment A2.2.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY WEEKLY, WHY FRIDAY, WHY GBP — all three are load-bearing (A2.2)
═══════════════════════════════════════════════════════════════════════════════════════════
FREQUENCY  Weekly, Friday-to-Friday. The book spans SEK, PLN, EUR, GBP and USD. Daily
           correlations across non-synchronous market closes are biased DOWNWARD, which would
           systematically OVERSTATE diversification and therefore OVERSIZE positions. The bias
           runs in the dangerous direction, so this is not a refinement.
CURRENCY   GBP total return. FX correlation is part of realised risk whether or not it is
           intended, and the ISA is denominated in GBP.
WINDOW     104 weeks target, 52 minimum. Below 52 the name is UNMEASURED and A2.3 applies.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ THE STORE IS EMPTY TODAY AND THAT IS THE CORRECT STATE, NOT A FAILURE
═══════════════════════════════════════════════════════════════════════════════════════════
No 104-week price series for AVGO / MU / ONT.L / ABCL / QBTS / COCO exists on disk, and Yahoo
is network-blocked from both the container and the device shell. So this module ships as a
CAPTURE instrument first and a computation second (R6.5 — retain first, analyse later: if we
do not write it down this week, can we ever get it back?).

Until 52 weeks accrue, `correlation_engine` reads UNMEASURED and applies A2.3's adverse
default. That is a MEASURED REFUSAL, not a silent zero, and it is the difference between this
and the class of defect the framework keeps finding (R2.10, R4.3).

⚑ AND IT MUST NEVER SILENTLY IMPROVE. `coverage()` reports weeks-held per name every run, so
the day a name crosses 52 weeks is visible rather than inferred from a size that changed.
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(os.environ.get("ISA_OUT", HERE), "stock_weekly_returns.json")

TARGET_WEEKS = 104
MIN_WEEKS = 52
SCHEMA_VERSION = 2


# ────────────────────────────────────────────────────────────────── calendar
def friday_of(d: datetime.date) -> datetime.date:
    """The Friday of the ISO week containing d. Anchoring every observation to one weekday is
    what makes two names comparable; 'the last price we happened to have' is not a series."""
    return d + datetime.timedelta(days=(4 - d.weekday()))


def week_key(d: datetime.date) -> str:
    return friday_of(d).isoformat()


# ────────────────────────────────────────────────────────────────── store I/O
def _empty() -> dict:
    return {"_what": "Weekly Friday-to-Friday GBP total-return store for the direct-stock "
                     "sleeve (V2.1-B / ISA-0355). Levels are stored, returns derived.",
            "schema_version": SCHEMA_VERSION, "names": {}, "fx": {}, "history": []}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    try:
        with open(STORE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        raise RuntimeError(
            f"stock_return_store: {STORE} is unreadable ({exc}). Refusing to start a fresh "
            f"store over it — that would silently discard accrued history that cannot be "
            f"re-fetched (R6.5). Move the file aside deliberately if that is the intent.")
    if doc.get("schema_version") != SCHEMA_VERSION:
        doc.setdefault("_migrations", []).append(
            {"from": doc.get("schema_version"), "to": SCHEMA_VERSION,
             "on": datetime.date.today().isoformat()})
        doc["schema_version"] = SCHEMA_VERSION
    doc.setdefault("names", {}); doc.setdefault("fx", {}); doc.setdefault("history", [])
    return doc


def save(doc: dict) -> str:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, STORE)
    return STORE


# ────────────────────────────────────────────────────────────────── ingest
def record_level(doc: dict, ticker: str, on: str, level: float, currency: str,
                 source: str, fx_to_gbp: Optional[float] = None,
                 total_return: bool = False) -> dict:
    """Record ONE weekly close. Every observation carries as_of, source and currency (R4.2).

    ⚑ `total_return` is declared, never assumed. A price series and a total-return series are
    different quantities, and quietly treating the first as the second understates the return of
    whatever pays the most income — which in this sleeve is systematically the least volatile
    name. Stored either way; `weekly_returns()` reports which basis each name is on."""
    if level is None or float(level) <= 0:
        raise ValueError(f"{ticker}: refusing a non-positive level {level!r} at {on} — "
                         f"'missing' must not be representable as a number (R4.1)")
    cur = (currency or "").upper()
    if cur != "GBP" and fx_to_gbp is None:
        raise ValueError(
            f"{ticker}: level is in {cur} and no fx_to_gbp was supplied. A2.2 requires GBP "
            f"total return because FX correlation is part of realised risk; converting later "
            f"from a rate we did not capture at the time is not point-in-time (R6.4).")
    wk = week_key(datetime.date.fromisoformat(on))
    rec = doc["names"].setdefault(ticker, {"currency": cur, "total_return_basis": bool(total_return),
                                           "observations": {}})
    if rec.get("currency") != cur:
        raise ValueError(f"{ticker}: currency changed {rec.get('currency')} -> {cur}. Declare a "
                         f"new ticker rather than mixing two currencies in one series.")
    rec["observations"][wk] = {
        "level": float(level),
        "gbp": round(float(level) * float(fx_to_gbp), 8) if cur != "GBP" else float(level),
        "fx_to_gbp": float(fx_to_gbp) if fx_to_gbp is not None else 1.0,
        "as_of": on, "source": source,
        "stamp_basis": "point_in_time" if on == wk else "backfilled_not_pit",
    }
    return doc


def weekly_returns(doc: dict, ticker: str) -> Tuple[Dict[str, float], dict]:
    """-> ({friday: return}, meta). CONSECUTIVE Fridays only.

    ⚑ A gap is NOT bridged. Two observations 6 weeks apart give a 6-week return, and treating
    that as a 1-week return would deflate measured volatility and inflate apparent
    diversification — the same directional error as daily sampling. Gaps are counted and
    reported (R4.9: a reader that cannot match a row COUNTS it)."""
    rec = (doc.get("names") or {}).get(ticker) or {}
    obs = rec.get("observations") or {}
    keys = sorted(obs)
    rets, gaps = {}, 0
    for prev, cur in zip(keys, keys[1:]):
        d0 = datetime.date.fromisoformat(prev); d1 = datetime.date.fromisoformat(cur)
        if (d1 - d0).days != 7:
            gaps += 1
            continue
        p0, p1 = obs[prev]["gbp"], obs[cur]["gbp"]
        if p0 > 0:
            rets[cur] = (p1 / p0) - 1.0
    meta = {"ticker": ticker, "weeks_observed": len(keys), "returns": len(rets),
            "gaps_skipped": gaps, "currency": rec.get("currency"),
            "total_return_basis": rec.get("total_return_basis", False),
            "first": keys[0] if keys else None, "last": keys[-1] if keys else None,
            "backfilled": sum(1 for k in keys
                              if obs[k].get("stamp_basis") == "backfilled_not_pit")}
    return rets, meta


def coverage(doc: dict, tickers: Optional[List[str]] = None) -> dict:
    """Per-name measurement status. Published EVERY run so a name crossing 52 weeks is an
    observed event, never inferred from a position size that changed."""
    tickers = tickers if tickers is not None else sorted(doc.get("names") or {})
    out, measured = {}, 0
    for t in tickers:
        rets, meta = weekly_returns(doc, t)
        n = len(rets)
        status = ("MEASURED" if n >= TARGET_WEEKS else
                  "MEASURED_SHORT" if n >= MIN_WEEKS else "UNMEASURED")
        if status != "UNMEASURED":
            measured += 1
        out[t] = {**meta, "usable_returns": n, "status": status,
                  "weeks_to_minimum": max(0, MIN_WEEKS - n),
                  "sigma_weekly": (round(statistics.pstdev(list(rets.values())), 6)
                                   if n >= 8 else None)}
    return {"as_of": datetime.date.today().isoformat(),
            "names": out, "n_names": len(tickers), "n_measured": measured,
            "n_unmeasured": len(tickers) - measured,
            "min_weeks": MIN_WEEKS, "target_weeks": TARGET_WEEKS,
            "store_path": STORE, "store_exists": os.path.exists(STORE)}


def _selftest():
    import tempfile
    global STORE
    STORE = os.path.join(tempfile.mkdtemp(), "s.json")
    d = _empty()
    base = datetime.date(2026, 1, 2)          # a Friday
    assert friday_of(base) == base
    assert friday_of(datetime.date(2026, 1, 1)) == base, "Thursday maps to that week's Friday"
    for i in range(60):
        day = base + datetime.timedelta(weeks=i)
        record_level(d, "AAA", day.isoformat(), 100.0 * (1.01 ** i), "USD",
                     "selftest", fx_to_gbp=0.78)
    r, m = weekly_returns(d, "AAA")
    assert len(r) == 59 and m["gaps_skipped"] == 0, (len(r), m)
    assert all(abs(v - 0.01) < 1e-9 for v in r.values()), "constant 1% weekly"
    cov = coverage(d, ["AAA"])
    assert cov["names"]["AAA"]["status"] == "MEASURED_SHORT", cov
    # a gap must NOT be bridged into a fake one-week return
    d2 = _empty()
    record_level(d2, "BBB", "2026-01-02", 100.0, "GBP", "t")
    record_level(d2, "BBB", "2026-02-13", 200.0, "GBP", "t")
    r2, m2 = weekly_returns(d2, "BBB")
    assert r2 == {} and m2["gaps_skipped"] == 1, (r2, m2)
    # missing FX on a non-GBP level must REFUSE
    try:
        record_level(_empty(), "CCC", "2026-01-02", 10.0, "USD", "t")
        raise AssertionError("should have refused a non-GBP level with no FX")
    except ValueError as e:
        assert "fx_to_gbp" in str(e)
    # a non-positive level must REFUSE
    try:
        record_level(_empty(), "DDD", "2026-01-02", 0.0, "GBP", "t")
        raise AssertionError("should have refused a zero level")
    except ValueError as e:
        assert "non-positive" in str(e)
    cov0 = coverage(_empty(), ["ZZZ"])
    assert cov0["names"]["ZZZ"]["status"] == "UNMEASURED" and cov0["n_measured"] == 0
    print("stock_return_store selftest OK (9 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps(coverage(load()), indent=1))
