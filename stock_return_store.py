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
Until 28-Aug-2026 this docstring said: *"Yahoo is network-blocked from both the container and
the device shell."* **TESTED 28-Aug-2026: HTTP 200.** AVGO 371.54 USD - ONT.L 179.4 GBp -
GBPUSD=X 1.3585, all from the v8 chart endpoint with stdlib `urllib` alone.

It was **contradicted by the framework's own register FIVE DAYS BEFORE it was written**:
ISA-0411 (21-Aug-2026) logs a 400-ticker MIDCAP400 Yahoo screen run from local bash.

**The cost was not the docstring. The cost was that nobody fetched.** With no series every name
read UNMEASURED, A2.3's adverse default of 0.70 applied, and every position was capped at
STARTER - against a MEASURED rho_bar of 0.163, which is 4.3x LOWER than the default. The top
three ladder rungs were unreachable for a reason that was never true.

⚑ **A NEGATIVE CLAIM NEEDS MORE EVIDENCE THAN A POSITIVE ONE, NOT LESS, because it is
load-bearing precisely where it STOPS INVESTIGATION.** A positive claim gets checked because
somebody uses it; a negative claim gets checked by nobody, because it means *don't bother*.
The claim is now registered in `negative_claims.json` with a falsifier
(`stock_price_fetch._network_probe`) and a 3-run expiry (P0.4 / ISA-0455).

`stock_price_fetch.py` populates this store. Until 52 weeks accrue for a name,
`correlation_engine` reads UNMEASURED and applies A2.3's adverse default - a MEASURED REFUSAL,
not a silent zero (R2.10, R4.3).

⚑ AND IT MUST NEVER SILENTLY IMPROVE. `coverage()` reports weeks-held per name every run, so
the day a name crosses 52 weeks is visible rather than inferred from a size that changed.
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
from typing import Dict, List, Optional, Tuple

# ── P0.1 LIVE-PATH EXECUTION LEDGER (framework_integrity) ──────────────────────────────
# ⚑ ONE LINE at the head of each capital-path function. `_mark` is a NO-OP when
# isa_policy.V2_FLAGS["execution_ledger"] is False, and it never raises into the caller — a
# monitoring hook that can break a capital run is a worse risk than the risk it monitors.
# The CALLS STAY IN THE CODE when the flag is off; removing them is what makes it droppable.
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None


HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(os.environ.get("ISA_OUT", HERE), "stock_weekly_returns.json")

TARGET_WEEKS = 104
MIN_WEEKS = 52
# P1.6 - a name unobserved for longer than this reads STALE and is EXCLUDED from the matrix.
STALE_WEEKS = 2
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
                 total_return: bool = False,
                 recorded_on: Optional[datetime.date] = None) -> dict:
    """Record ONE weekly close. Every observation carries as_of, source and currency (R4.2).

    ⚑ `total_return` is declared, never assumed. A price series and a total-return series are
    different quantities, and quietly treating the first as the second understates the return of
    whatever pays the most income — which in this sleeve is systematically the least volatile
    name. Stored either way; `weekly_returns()` reports which basis each name is on."""
    _fi_mark("stock_return_store", "record_level")
    if level is None or float(level) <= 0:
        raise ValueError(f"{ticker}: refusing a non-positive level {level!r} at {on} — "
                         f"'missing' must not be representable as a number (R4.1)")
    # ⚑⚑ ISA-0586, 03-Sep-2026. `GBp` IS PENCE AND CASE IS THE ONLY THING THAT SAYS SO.
    # This line read `cur = (currency or "").upper()` and nothing else, so 'GBp' became 'GBP',
    # the `cur != "GBP"` branch below was not taken, and the 0.01 factor that
    # `stock_price_fetch.run()` passes — exactly as ISA-0499's corrective action required —
    # was DISCARDED. 1,353 observations across nine London lines were stored at 100x.
    # ⚑ IT SURVIVED BECAUSE WEEKLY RETURNS ARE RATIOS: a constant scale error cancels, so
    # correlation, sigma and the risk decomposition all looked right. Only a consumer reading
    # the LEVEL — `stock_price_fetch.e4_relative_momentum` does — would ever have shown it.
    # ⚑ THE CLASS IS FC-D, NOT FC-B: ISA-0499's fix was made in the PRODUCER and undone in the
    # STORE. Two homes normalising one quantity. The recurrence check is
    # `stock_price_fetch._selftest` assertion E, which multiplies 1250.0 by 0.01 and compares.
    _raw_cur = (currency or "").strip()
    _is_pence = _raw_cur in ("GBp", "GBX", "GBx")
    cur = _raw_cur.upper()
    if (cur != "GBP" or _is_pence) and fx_to_gbp is None:
        raise ValueError(
            f"{ticker}: level is in {_raw_cur} and no fx_to_gbp was supplied. A2.2 requires GBP "
            f"total return because FX correlation is part of realised risk; converting later "
            f"from a rate we did not capture at the time is not point-in-time (R6.4).")
    wk = week_key(datetime.date.fromisoformat(on))
    _rec_on = recorded_on or datetime.date.today()
    rec = doc["names"].setdefault(ticker, {"currency": cur, "total_return_basis": bool(total_return),
                                           "observations": {}})
    if rec.get("currency") != cur:
        raise ValueError(f"{ticker}: currency changed {rec.get('currency')} -> {cur}. Declare a "
                         f"new ticker rather than mixing two currencies in one series.")
    rec["observations"][wk] = {
        "level": float(level),
        "gbp": (round(float(level) * float(fx_to_gbp), 8)
                if (cur != "GBP" or _is_pence) else float(level)),
        "fx_to_gbp": float(fx_to_gbp) if fx_to_gbp is not None else 1.0,
        # ⚑ THE DENOMINATION THE LEVEL ARRIVED IN, stamped so the transform is visible on the
        # row rather than inferred from a rate (R4.2). The SERIES currency stays GBP because
        # `gbp` is pounds after conversion; `source_currency` says where it came from.
        "source_currency": _raw_cur or cur,
        "as_of": on, "source": source,
        # ⚑⚑ F10, FIXED 28-Aug-2026. The old expression was
        #        "point_in_time" if on == wk else "backfilled_not_pit"
        # and `wk` IS `friday_of(on)` - so for ANY Friday close `on == wk`, which means
        # EVERY BACKFILLED HISTORICAL FRIDAY CERTIFIED ITSELF POINT-IN-TIME. A series fetched
        # in August 2026 would claim it had been recorded in September 2024.
        # ⚑ THE CLASS: a provenance field derived from the DATA'S OWN TIMESTAMP rather than
        # the CAPTURE timestamp can only ever say "this Friday is a Friday". Provenance must
        # compare the observation against WHEN WE WROTE IT DOWN - the one fact the data
        # itself cannot supply.
        "stamp_basis": ("point_in_time"
                        if (_rec_on - friday_of(datetime.date.fromisoformat(on))).days <= 7
                        else "backfilled_not_pit"),
        "recorded_on": _rec_on.isoformat(),
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


def coverage(doc: dict, tickers: Optional[List[str]] = None,
             refusals: Optional[dict] = None) -> dict:
    """Per-name measurement status. Published EVERY run so a name crossing 52 weeks is an
    observed event, never inferred from a position size that changed."""
    _fi_mark("stock_return_store", "coverage")
    # ⚑ ISA-0580. PASS THE UNIVERSE. Defaulting to the names already IN the store makes a name
    # that was never fetched INVISIBLE — on 02-Sep-2026 this reported "59 names, 59 measured, 0
    # unmeasured" while 119 universe names had no series at all. A metric whose denominator is
    # its own numerator's source can only ever report 100%.
    tickers = tickers if tickers is not None else sorted(doc.get("names") or {})
    # ⚑ ISA-0578. `refusals` (from stock_price_fetch.load_declared_refusals) is what lets this
    # report tell "NEVER FETCHED" from "TOO SHORT". Both read UNMEASURED and both take A2.3's
    # adverse 0.70, but they are different facts with different fixes — one needs a declared
    # symbol, the other needs time — and R2.10 forbids them producing the same output.
    refusals = refusals or {}
    today = datetime.date.today()
    out, measured = {}, 0
    for t in tickers:
        rets, meta = weekly_returns(doc, t)
        n = len(rets)
        status = ("MEASURED" if n >= TARGET_WEEKS else
                  "MEASURED_SHORT" if n >= MIN_WEEKS else "UNMEASURED")
        if status != "UNMEASURED":
            measured += 1
        obs = ((doc.get("names") or {}).get(t) or {}).get("observations") or {}
        n_pit = sum(1 for k in obs if obs[k].get("stamp_basis") == "point_in_time")
        n_bf = len(obs) - n_pit
        # ⚑ P1.6 THE STALENESS CONTRACT. A frozen series and a live one are IDENTICAL on every
        # other measure - which is the exact shape of a delisting or a ticker rename. A name
        # unobserved for more than STALE_WEEKS is NAMED and EXCLUDED rather than carried,
        # because carrying it lends a dead series the authority of a live one.
        last = meta.get("last")
        weeks_since = None
        if last:
            weeks_since = (today - datetime.date.fromisoformat(last)).days / 7.0
            if weeks_since > STALE_WEEKS and status != "UNMEASURED":
                status = "STALE"
        if status != "UNMEASURED":
            why = None
        elif t in refusals:
            why = "NOT_FETCHED_NO_SYMBOL"
        elif n_pit + n_bf == 0:
            why = "NOT_FETCHED_NO_SERIES"
        else:
            why = "INSUFFICIENT_HISTORY"
        out[t] = {**meta, "usable_returns": n, "status": status,
                  "unmeasured_reason": why,
                  "unmeasured_detail": (
                      None if why is None else
                      ("no verified Yahoo symbol is declared, so it was NEVER FETCHED: %s"
                       % ((refusals.get(t) or {}).get("reason") or "refused")[:180])
                      if why == "NOT_FETCHED_NO_SYMBOL" else
                      "mapped, but no observation was ever written — a FETCH FAILURE, not an "
                      "absence of data"
                      if why == "NOT_FETCHED_NO_SERIES" else
                      "fetched and measured: %d of the %d weekly returns required. Nothing is "
                      "wrong with it; it needs %d more weeks."
                      % (n, MIN_WEEKS, max(0, MIN_WEEKS - n))),
                  "weeks_to_minimum": max(0, MIN_WEEKS - n),
                  "weeks_since_last_observation": (round(weeks_since, 2)
                                                   if weeks_since is not None else None),
                  "n_point_in_time": n_pit, "n_backfilled": n_bf,
                  "sigma_weekly": (round(statistics.pstdev(list(rets.values())), 6)
                                   if n >= 8 else None)}
    measured = sum(1 for v in out.values() if v["status"] in ("MEASURED", "MEASURED_SHORT"))
    stale = sorted(t for t, v in out.items() if v["status"] == "STALE")
    tot = sum(v["n_point_in_time"] + v["n_backfilled"] for v in out.values())
    pit = sum(v["n_point_in_time"] for v in out.values())
    reasons = {}
    for t, v in out.items():
        if v.get("unmeasured_reason"):
            reasons.setdefault(v["unmeasured_reason"], []).append(t)
    return {"as_of": today.isoformat(),
            "unmeasured_by_reason": {k: sorted(v) for k, v in sorted(reasons.items())},
            "unmeasured_basis": (
                "NOT_FETCHED_NO_SYMBOL — no verified Yahoo symbol is declared, so nothing was "
                "tried; NOT_FETCHED_NO_SERIES — mapped but no observation written, a fetch "
                "FAILURE; INSUFFICIENT_HISTORY — fetched correctly, simply younger than the "
                "%d-week minimum. All three read UNMEASURED and take A2.3's adverse 0.70, and "
                "they are NOT the same fact (R2.10, ISA-0578)." % MIN_WEEKS),
            "names": out, "n_names": len(tickers), "n_measured": measured,
            "n_unmeasured": len(tickers) - measured,
            "stale_excluded": stale, "n_stale": len(stale), "stale_after_weeks": STALE_WEEKS,
            "n_point_in_time": pit, "n_backfilled": tot - pit,
            "pit_share": round(pit / tot, 4) if tot else None,
            # ⚑ THE CONSEQUENCE THAT TRAVELS WITH EVERY PUBLISHED MATRIX (P1.5).
            "basis_note": ("On a first fetch EVERY observation is `backfilled_not_pit`. The "
                           "matrix is admissible for RISK and is NOT point-in-time evidence. "
                           "§7 of the email says so in those words until the PIT share is "
                           "material."),
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
