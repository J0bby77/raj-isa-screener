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
WINDOW     the fetch keeps ~3y (range=3y, ~156 weeks); the RISK window is isa_policy.RISK_WINDOW_WEEKS /
           RISK_MIN_WEEKS (ISA-0680). Below the minimum the name is UNMEASURED and A2.3 applies.

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
SCHEMA_VERSION = 3

# ══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0934 (20-Nov-2026) — FX DIRECTION IS A DECLARED BASIS, NEVER INFERRED FROM A NUMBER
# ══════════════════════════════════════════════════════════════════════════════════════════
# `fx_to_gbp` has always MEANT "pounds per one unit of the level's currency" (the multiplier).
# The producer passed the Yahoo quote GBP<ccy>=X, which is <ccy> PER POUND, so every foreign
# level from 28-Aug-2026 was MULTIPLIED by the inverse of the right number: AVGO 30-Oct
# 326.08 USD x 1.3379 was stored as GBP 436.25 (true ~243.73). Ratios hid the scale, but NOT the
# direction: each weekly return carried the FX move with its SIGN FLIPPED, so sigma, beta,
# correlation and the 52-week worst were measured on a currency that does not exist.
# ⚑ THE CONTRACT NOW: a non-GBP level is accepted only with fx_basis == FX_BASIS_GBP_PER_CCY,
#   and, when the quote it came from is supplied, fx_to_gbp x fx_quote must equal 1.
#   Every row carries its basis; `load()` REFUSES a store holding a foreign row without one,
#   so restoring the pre-ISA-0934 file (or old code writing into a new file) cannot be read
#   silently as qualified GBP (instruction §7: rollback must not restore a bad FX dependency).
FX_BASIS_GBP_PER_CCY = "GBP_PER_CCY"      # multiplier: GBP for one unit of the source currency
FX_BASIS_PENCE = "GBP_PER_GBX"            # fixed 0.01 (ISA-0499/0586)
FX_BASIS_IDENTITY = "IDENTITY"            # GBP series, 1.0
FX_VINTAGE = "fx-direction-v2/ISA-0934"
FX_RECIPROCAL_TOL = 1e-6


class StoreFXUnqualified(ValueError):
    """The store holds a foreign-currency observation whose FX basis is not declared (ISA-0934)."""


def fx_unqualified(doc: dict) -> dict:
    """{ticker: n_rows} of observations whose GBP value cannot be trusted for direction:
    a non-GBP series row (or a GBP-series row with a non-unit, non-pence factor) carrying no
    declared fx_basis. Pure read; used by load() and by the migration."""
    out = {}
    for t, rec in ((doc or {}).get("names") or {}).items():
        cur = (rec or {}).get("currency")
        for o in ((rec or {}).get("observations") or {}).values():
            if not isinstance(o, dict) or o.get("fx_basis"):
                continue
            f = o.get("fx_to_gbp", 1.0)
            if cur not in (None, "GBP") or (f is not None and abs(float(f) - 1.0) > 1e-12
                                              and abs(float(f) - 0.01) > 1e-12):
                out[t] = out.get(t, 0) + 1
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0549 (16-Sep-2026) — ONE ECONOMIC SECURITY, ONE STORE IDENTITY
# ══════════════════════════════════════════════════════════════════════════════════════════
# The store held BOTH `ONT` (broker label) and `ONT.L` (Yahoo symbol) as first-class names with
# 159/159 identical observations, so any consumer that unioned the store counted one holding twice
# (a spurious rho = 1.000 pair, double weight, deflated N_eff). The alias was applied on the way IN
# (fetch) and never on the way OUT (store). The canonical identity of a store record is the
# DECLARED Yahoo symbol from stock_price_fetch.load_symbol_map (one alias truth — no second
# registry, no similarity guessing). Every reader resolves a requested label through
# `record_of()`; every writer keys by `canonical_name()`; `canonicalise()` migrates a legacy store
# and REFUSES (never merges) two keys for one security whose series differ. The invariant is
# identity UNIQUENESS: rho == 1 between two DISTINCT canonical ids is only a review diagnostic.
# ROLLBACK (R4.13): restore this module, its readers AND the pre-migration store together — the
# old readers look records up by raw label and would silently lose the alias key.
IDENTITY_TOL = 1e-9


class CanonicalIdentityConflict(RuntimeError):
    """Two labels resolve to one security but cannot be one record (or the map is ambiguous)."""


_SMAP_CACHE: Dict[str, object] = {"key": None, "map": None}


def symbol_map() -> Dict[str, str]:
    """The declared alias truth, cached per symbol-map file state (refresh_symbol_map may add
    entries mid-run; a stale map would key a new name under its old label)."""
    import stock_price_fetch as _spf                       # lazy: stock_price_fetch imports this module
    p = getattr(_spf, "SYMBOL_MAP_STORE", None)
    try:
        key = (p, os.path.getmtime(p)) if p and os.path.exists(p) else (p, None)
    except OSError:
        key = (p, None)
    if _SMAP_CACHE["key"] != key or _SMAP_CACHE["map"] is None:
        _SMAP_CACHE["map"] = dict(_spf.load_symbol_map())
        _SMAP_CACHE["key"] = key
    return _SMAP_CACHE["map"]                                         # type: ignore[return-value]


def canonical_name(ticker: str, smap: Optional[Dict[str, str]] = None) -> str:
    """Declared symbol for `ticker`, or the label itself when undeclared. An alias CHAIN
    (a -> b -> c) is ambiguous and REFUSES (R4.8): one hop is the whole contract."""
    t = str(ticker or "").strip()
    m = symbol_map() if smap is None else smap
    c = m.get(t, t)
    if m.get(c, c) != c:
        raise CanonicalIdentityConflict("ISA-0549: alias chain %s -> %s -> %s is ambiguous"
                                        % (t, c, m.get(c)))
    return c


def record_of(doc: dict, ticker: str, smap: Optional[Dict[str, str]] = None) -> dict:
    """THE reader for one record: resolves the requested label to its canonical key. A legacy
    (pre-migration) store keyed only by the alias still resolves, so no reader loses a series."""
    names = (doc or {}).get("names") or {}
    t = str(ticker or "").strip()
    c = canonical_name(t, smap)
    return names.get(c) or names.get(t) or {}


def _series_identical(a: dict, b: dict) -> bool:
    oa, ob = a.get("observations") or {}, b.get("observations") or {}
    if set(oa) != set(ob):
        return False
    for k in oa:
        ga, gb = (oa[k] or {}).get("gbp"), (ob[k] or {}).get("gbp")
        if ga is None or gb is None or abs(float(ga) - float(gb)) > IDENTITY_TOL * max(1.0, abs(float(ga))):
            return False
    return a.get("currency") == b.get("currency") and \
        bool(a.get("total_return_basis")) == bool(b.get("total_return_basis"))


def identity_report(doc: dict, smap: Optional[Dict[str, str]] = None) -> dict:
    """-> raw vs canonical key counts, collisions {canonical: [keys]}, conflicts, chains."""
    names = (doc or {}).get("names") or {}
    m = symbol_map() if smap is None else smap
    groups, chains = {}, []
    for k in sorted(names):
        try:
            groups.setdefault(canonical_name(k, m), []).append(k)
        except CanonicalIdentityConflict as exc:
            chains.append({"key": k, "why": str(exc)})
    coll = {c: ks for c, ks in groups.items() if len(ks) > 1 or ks[0] != c}
    conflicts = []
    for c, ks in coll.items():
        recs = [names[k] for k in ks]
        if any(not _series_identical(recs[0], r) for r in recs[1:]):
            conflicts.append({"canonical": c, "keys": ks,
                              "observations": {k: len(names[k].get("observations") or {}) for k in ks}})
    return {"raw_keys": len(names), "canonical_keys": len(groups), "collisions": coll,
            "conflicts": conflicts, "chains": chains,
            "state": ("CONFLICT" if conflicts or chains else ("LEGACY_ALIASES" if coll else "CANONICAL"))}


def canonicalise(doc: dict, smap: Optional[Dict[str, str]] = None, *, today: Optional[str] = None) -> dict:
    """Migrate alias keys onto the canonical key. ATOMIC: every group is checked BEFORE anything
    is mutated; a conflict or chain raises and leaves `doc` untouched. Identical aliases merge once
    and are retained as `aliases` + an `identity_migrations` history row (R2.13/R6.5)."""
    rep = identity_report(doc, smap)
    if rep["conflicts"] or rep["chains"]:
        raise CanonicalIdentityConflict(
            "ISA-0549 CANONICAL_IDENTITY_CONFLICT: migration REFUSED, store unchanged: %s"
            % json.dumps({"conflicts": rep["conflicts"], "chains": rep["chains"]})[:600])
    names = doc.setdefault("names", {})
    moved = []
    for c, ks in sorted(rep["collisions"].items()):
        keep = c if c in ks else ks[0]
        base = names[keep]
        aliases = sorted(set(base.get("aliases") or []) | {k for k in ks if k != c})
        fetched = [names[k].get("last_fetched_on") for k in ks if names[k].get("last_fetched_on")]
        for k in ks:
            if k != keep:
                names.pop(k)
        if keep != c:
            names[c] = names.pop(keep)
        rec = names[c]
        if aliases:
            rec["aliases"] = aliases
        if fetched:
            rec["last_fetched_on"] = max(fetched)
        moved.append({"canonical": c, "collapsed_keys": ks,
                      "observations": len(rec.get("observations") or {})})
    if moved:
        doc.setdefault("identity_migrations", []).append(
            {"on": today or datetime.date.today().isoformat(), "item": "ISA-0549",
             "raw_keys_before": rep["raw_keys"], "canonical_keys_after": len(names), "merged": moved,
             "basis": "identical series under one declared symbol merged once; aliases retained"})
    return {"changed": bool(moved), "merged": moved, "raw_keys_before": rep["raw_keys"],
            "canonical_keys_after": len(names)}


# ────────────────────────────────────────────────────────────────── calendar
def friday_of(d: datetime.date) -> datetime.date:
    """The Friday of the ISO week containing d. Anchoring every observation to one weekday is
    what makes two names comparable; 'the last price we happened to have' is not a series."""
    return d + datetime.timedelta(days=(4 - d.weekday()))


def week_key(d: datetime.date) -> str:
    return friday_of(d).isoformat()


def last_settled_friday(as_of: datetime.date) -> datetime.date:
    """The most recent Friday whose weekly close is FINAL (a Friday is not settled while it is still
    Friday). ONE HOME (ISA-0756, 19-Oct-2026): stock_price_fetch.last_settled_friday delegates here."""
    back = (as_of.weekday() - 4) % 7                   # 4 = Friday
    if back == 0:
        back = 7
    return as_of - datetime.timedelta(days=back)


def _as_date(v) -> datetime.date:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.date.fromisoformat(str(v)[:10])


FRESHNESS_STATES = ("FRESH", "LAGGING", "STALE_INPUT", "UNMEASURED")


def freshness(doc: dict, tickers: Optional[List[str]] = None, *, as_of) -> dict:
    """⚑ ISA-0756 (B05-R04), 19-Oct-2026 — THE ONE freshness contract for the weekly store.

    CLOCK-INJECTED: `as_of` is REQUIRED (no bare date.today()), so the same store and the same clock
    always give the same answer - certification cannot turn red by calendar alone. Per name:
    latest_observation, expected_latest_observation = last_settled_friday(as_of),
    missed_expected_observations (settled Fridays after the latest observation), last_fetched_on,
    state FRESH (0 missed) | LAGGING (1..STALE_WEEKS-1) | STALE_INPUT (>= STALE_WEEKS) | UNMEASURED.

    ⚑ NO NEW NUMBER. STALE_WEEKS (2) is the existing constant, now counted in MISSED SETTLED FRIDAYS
    instead of calendar days/7. For a Friday-dated last observation the two agree day for day (a
    02-Oct close: calendar STALE from Sat 17-Oct when 15 days > 14; missed-Friday STALE from Sat
    17-Oct when the 09-Oct and 16-Oct closes are both settled and absent); they differ only for a
    non-Friday last observation (a holiday-shortened week), where the Friday count is the correct
    measure of what is missing."""
    if as_of is None:
        raise ValueError("freshness() is clock-injected: as_of is required (ISA-0756)")
    a = _as_date(as_of)
    exp = last_settled_friday(a)
    tickers = tickers if tickers is not None else sorted((doc or {}).get("names") or {})
    out, counts = {}, {k: 0 for k in FRESHNESS_STATES}
    for t in tickers:
        rec = record_of(doc, t)
        obs = rec.get("observations") or {}
        last = max(obs) if obs else None
        if not last:
            st, missed = "UNMEASURED", None
        else:
            missed = max(0, (exp - _as_date(last)).days // 7)
            st = "FRESH" if missed == 0 else ("STALE_INPUT" if missed >= STALE_WEEKS else "LAGGING")
        counts[st] += 1
        out[t] = {"latest_observation": last, "missed_expected_observations": missed,
                  "last_fetched_on": rec.get("last_fetched_on"), "state": st}
    return {"as_of": a.isoformat(), "expected_latest_observation": exp.isoformat(),
            "stale_after_missed": STALE_WEEKS, "names": out, "counts": counts,
            "stale_input": sorted(t for t, v in out.items() if v["state"] == "STALE_INPUT"),
            "basis": ("missed settled Fridays since the latest observation, at the injected clock "
                      "(ISA-0756); STALE_INPUT at >= %d" % STALE_WEEKS)}


# ────────────────────────────────────────────────────────────────── store I/O
def _empty() -> dict:
    return {"_what": "Weekly Friday-to-Friday GBP total-return store for the direct-stock "
                     "sleeve (V2.1-B / ISA-0355). Levels are stored, returns derived.",
            "schema_version": SCHEMA_VERSION, "names": {}, "fx": {}, "history": []}


def load(allow_fx_unqualified: bool = False) -> dict:
    """ISA-0934: REFUSES (StoreFXUnqualified) a store with any foreign row lacking a declared
    fx_basis. `allow_fx_unqualified=True` is for the one-time migration and forensics only."""
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
    if not allow_fx_unqualified:
        _bad = fx_unqualified(doc)
        if _bad:
            raise StoreFXUnqualified(
                "ISA-0934: %s holds %d foreign observation(s) across %d name(s) with no declared "
                "fx_basis (e.g. %s). Their `gbp` values may be the INVERTED pre-ISA-0934 "
                "conversion; refusing rather than measuring risk on them. Run "
                "`stock_return_store.py --migrate-fx-direction` (lineage-stamped, original frozen)."
                % (STORE, sum(_bad.values()), len(_bad), sorted(_bad)[:3]))
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
                 recorded_on: Optional[datetime.date] = None,
                 fx_basis: Optional[str] = None, fx_pair: Optional[str] = None,
                 fx_quote: Optional[float] = None) -> dict:
    """Record ONE weekly close. Every observation carries as_of, source and currency (R4.2).

    ⚑ `total_return` is declared, never assumed. A price series and a total-return series are
    different quantities, and quietly treating the first as the second understates the return of
    whatever pays the most income — which in this sleeve is systematically the least volatile
    name. Stored either way; `weekly_returns()` reports which basis each name is on."""
    _fi_mark("stock_return_store", "record_level")
    ticker = canonical_name(ticker)                        # ISA-0549: writers key by canonical identity
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
    # ⚑ ISA-0934: the DIRECTION is declared, then checked against the quote it came from.
    if cur != "GBP" and not _is_pence:
        if fx_basis != FX_BASIS_GBP_PER_CCY:
            raise ValueError(
                f"{ticker}: a {_raw_cur} level needs fx_basis={FX_BASIS_GBP_PER_CCY!r} (GBP per one "
                f"{_raw_cur}); got {fx_basis!r}. ISA-0934: a GBP<ccy>=X quote is <ccy> PER POUND and "
                f"multiplying by it inverts the conversion. Pass fx_to_gbp=1/quote.")
        if not (float(fx_to_gbp) > 0):
            raise ValueError(f"{ticker}: fx_to_gbp must be positive, got {fx_to_gbp!r}")
        if fx_quote is not None and abs(float(fx_to_gbp) * float(fx_quote) - 1.0) > FX_RECIPROCAL_TOL:
            raise ValueError(
                f"{ticker}: fx_to_gbp {fx_to_gbp} x fx_quote {fx_quote} != 1 - the multiplier is not "
                f"the reciprocal of the quote it claims to come from (ISA-0934)")
        _band = _gbp_per_ccy_band(fx_pair) if fx_pair else None
        if _band and not (_band[0] <= float(fx_to_gbp) <= _band[1]):
            raise ValueError(
                f"{ticker}: fx_to_gbp {fx_to_gbp} is outside the GBP-per-{_raw_cur} band "
                f"[{_band[0]:.6f}, {_band[1]:.6f}] implied by {fx_pair}'s declared band - an inverted "
                f"or scaled rate (ISA-0934/ISA-0429)")
        _basis = FX_BASIS_GBP_PER_CCY
    elif _is_pence:
        if abs(float(fx_to_gbp) - 0.01) > 1e-12:
            raise ValueError(f"{ticker}: a GBp level converts at exactly 0.01, got {fx_to_gbp!r} (ISA-0499)")
        _basis = FX_BASIS_PENCE
    else:
        _basis = FX_BASIS_IDENTITY
    wk = week_key(datetime.date.fromisoformat(on))
    _rec_on = recorded_on or datetime.date.today()
    rec = doc["names"].setdefault(ticker, {"currency": cur, "total_return_basis": bool(total_return),
                                           "observations": {}})
    # ⚑ ISA-0809 (03-Oct-2026): a STUB record (no observations, no currency - created when the
    #   fetcher stamped `last_fetched_on` before converting) adopts the currency of its first
    #   observation. It is not a currency CHANGE: there is no series to mix. Before this, every
    #   new universe name raised here on every observation and stayed empty (35 names on 03-Oct).
    if rec.get("currency") is None and not rec.get("observations"):
        rec["currency"] = cur
        rec.setdefault("total_return_basis", bool(total_return))
        rec["observations"] = {}
    if rec.get("currency") != cur:
        raise ValueError(f"{ticker}: currency changed {rec.get('currency')} -> {cur}. Declare a "
                         f"new ticker rather than mixing two currencies in one series.")
    rec["observations"][wk] = {
        "level": float(level),
        "gbp": (round(float(level) * float(fx_to_gbp), 8)
                if (cur != "GBP" or _is_pence) else float(level)),
        "fx_to_gbp": float(fx_to_gbp) if fx_to_gbp is not None else 1.0,
        "fx_basis": _basis,
        **({"fx_pair": fx_pair} if fx_pair else {}),
        **({"fx_quote": float(fx_quote)} if fx_quote is not None else {}),
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


def _gbp_per_ccy_band(pair: Optional[str]):
    """Reciprocal of stock_price_fetch.FX_BANDS[pair] (which bounds the <ccy>-per-GBP quote)."""
    try:
        import stock_price_fetch as _spf                   # lazy: stock_price_fetch imports this module
        lo, hi = _spf.FX_BANDS[pair]
        return (1.0 / hi, 1.0 / lo)
    except Exception:                                      # noqa: BLE001
        return None


def _pair_for(ccy: Optional[str]) -> Optional[str]:
    try:
        import stock_price_fetch as _spf
        return _spf.CURRENCY_TO_PAIR.get(ccy)
    except Exception:                                      # noqa: BLE001
        return None


def migrate_fx_direction(doc: dict, *, today: Optional[str] = None,
                         original_sha256: Optional[str] = None,
                         frozen_path: Optional[str] = None) -> dict:
    """ISA-0934 one-time, lineage-stamped correction of the pre-v2 vintage, IN MEMORY.

    Rule: a foreign row's stored `fx_to_gbp` is the GBP<ccy>=X QUOTE (ccy per GBP). The corrected
    value is gbp = level / quote, multiplier = 1/quote. Each corrected row keeps `level` and its
    capture stamps untouched, records the quote as `fx_quote`, and is stamped `fx_basis`,
    `fx_vintage` and `fx_corrected_on`. GBP rows are stamped IDENTITY, GBp rows PENCE.
    ⚑ ATOMIC + REFUSING: every foreign quote must sit inside its pair's declared <ccy>-per-GBP
    band (so the row is PROVABLY the inverted kind); a row outside it, an undeclared currency or a
    non-positive level makes the whole migration refuse and leaves `doc` untouched.
    Idempotent: rows already carrying fx_basis are left alone."""
    import copy
    today = today or datetime.date.today().isoformat()
    work = copy.deepcopy(doc.get("names") or {})
    per_ccy, refusals, n_ident, n_pence, deltas = {}, [], 0, 0, []
    for t, rec in work.items():
        cur = (rec or {}).get("currency")
        obs = (rec or {}).get("observations") or {}
        pair = _pair_for(cur) if cur not in (None, "GBP") else None
        band = None
        if pair:
            try:
                import stock_price_fetch as _spf
                band = _spf.FX_BANDS[pair]
            except Exception:                              # noqa: BLE001
                band = None
        prev_key = None
        for k in sorted(obs):
            o = obs[k]
            if not isinstance(o, dict) or o.get("fx_basis"):
                prev_key = k
                continue
            f = float(o.get("fx_to_gbp", 1.0))
            if cur in (None, "GBP"):
                if abs(f - 1.0) <= 1e-12:
                    o["fx_basis"] = FX_BASIS_IDENTITY; n_ident += 1
                elif abs(f - 0.01) <= 1e-12:
                    o["fx_basis"] = FX_BASIS_PENCE; n_pence += 1
                else:
                    refusals.append({"ticker": t, "week": k, "why": "GBP series with factor %r" % f})
                prev_key = k
                continue
            lv = o.get("level")
            if not pair or not band:
                refusals.append({"ticker": t, "week": k, "why": "no declared FX pair/band for %s" % cur})
                continue
            if not (band[0] <= f <= band[1]):
                refusals.append({"ticker": t, "week": k,
                                 "why": "stored rate %r outside the %s quote band %s - not provably "
                                        "the inverted kind" % (f, pair, band)})
                continue
            if not lv or float(lv) <= 0:
                refusals.append({"ticker": t, "week": k, "why": "non-positive level %r" % lv})
                continue
            old_gbp = o.get("gbp")
            # ⚑ KR6 / FI-Q1-gbp (found 20-Nov-2026 in the P3 SHADOW preflight): this function used to compute
            #   `gbp` itself (level / quote), a SECOND computer of a registered quantity. The corrected row is now
            #   derived THROUGH `record_level` - the one registered computer - on a scratch doc, so the same
            #   direction, reciprocal and band checks bind the migration, and only the conversion fields are
            #   relayed onto the original row (level, as_of, source and capture stamps stay untouched).
            try:
                _scr = record_level({"names": {}}, t, o.get("as_of") or k, float(lv), cur,
                                    o.get("source") or "ISA-0934 migration", fx_to_gbp=1.0 / f,
                                    fx_basis=FX_BASIS_GBP_PER_CCY, fx_pair=pair, fx_quote=f)
            except ValueError as exc:
                refusals.append({"ticker": t, "week": k, "why": "record_level refused the corrected row: %s" % exc})
                continue
            _row = next(iter(next(iter(_scr["names"].values()))["observations"].values()))
            o["gbp"] = _row["gbp"]
            o["fx_to_gbp"] = _row["fx_to_gbp"]
            o["fx_quote"] = _row["fx_quote"]
            o["fx_pair"] = _row["fx_pair"]
            o["fx_basis"] = _row["fx_basis"]
            o["fx_vintage"] = FX_VINTAGE
            o["fx_corrected_on"] = today
            o.setdefault("source_currency", cur)
            if old_gbp is not None and abs(float(old_gbp) - round(float(lv) * f, 8)) > 1e-6 * max(1.0, abs(float(old_gbp))):
                refusals.append({"ticker": t, "week": k,
                                 "why": "stored gbp %r != level x rate %r - not the documented "
                                        "pre-v2 arithmetic" % (old_gbp, round(float(lv) * f, 8))})
            c = per_ccy.setdefault(cur, {"names": set(), "observations": 0})
            c["names"].add(t); c["observations"] += 1
            # two-derivation check on the weekly RETURN: corrected = local return x (q0 / q1)
            if prev_key is not None and k in obs:
                p = obs[prev_key]
                d0 = datetime.date.fromisoformat(prev_key); d1 = datetime.date.fromisoformat(k)
                if (d1 - d0).days == 7 and p.get("fx_quote") and p.get("level"):
                    r_new = o["gbp"] / p["gbp"] - 1.0
                    r_ind = (float(lv) / float(p["level"])) * (float(p["fx_quote"]) / f) - 1.0
                    deltas.append(abs(r_new - r_ind))
            prev_key = k
    summary = {"item": "ISA-0934", "on": today, "vintage": FX_VINTAGE,
               "rule": "gbp = level / quote(GBP<ccy>=X); fx_to_gbp = 1/quote; quote kept as fx_quote",
               "per_currency": {c: {"names": len(v["names"]), "observations": v["observations"]}
                                for c, v in sorted(per_ccy.items())},
               "n_corrected": sum(v["observations"] for v in per_ccy.values()),
               "n_names_corrected": len({n for v in per_ccy.values() for n in v["names"]}),
               "n_stamped_identity": n_ident, "n_stamped_pence": n_pence,
               "two_derivation_max_return_delta": max(deltas) if deltas else None,
               "two_derivation_pairs": len(deltas),
               "original_sha256": original_sha256, "frozen_original": frozen_path,
               "refusals": refusals[:50], "n_refusals": len(refusals)}
    if refusals:
        summary["state"] = "REFUSED"
        return summary
    doc["names"] = work
    doc.setdefault("repairs", []).append({k: v for k, v in summary.items() if k != "refusals"})
    doc["schema_version"] = SCHEMA_VERSION
    summary["state"] = "MIGRATED" if summary["n_corrected"] or n_ident or n_pence else "NO_OP"
    return summary


def weekly_returns(doc: dict, ticker: str) -> Tuple[Dict[str, float], dict]:
    """-> ({friday: return}, meta). CONSECUTIVE Fridays only.

    ⚑ A gap is NOT bridged. Two observations 6 weeks apart give a 6-week return, and treating
    that as a 1-week return would deflate measured volatility and inflate apparent
    diversification — the same directional error as daily sampling. Gaps are counted and
    reported (R4.9: a reader that cannot match a row COUNTS it)."""
    rec = record_of(doc, ticker)                          # ISA-0549: resolve the requested label
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
             refusals: Optional[dict] = None, as_of=None) -> dict:
    """Per-name measurement status. Published EVERY run so a name crossing 52 weeks is an
    observed event, never inferred from a position size that changed."""
    _fi_mark("stock_return_store", "coverage")
    # ⚑ ISA-0580. PASS THE UNIVERSE. Defaulting to the names already IN the store makes a name
    # that was never fetched INVISIBLE — on 02-Sep-2026 this reported "59 names, 59 measured, 0
    # unmeasured" while 119 universe names had no series at all. A metric whose denominator is
    # its own numerator's source can only ever report 100%.
    tickers = tickers if tickers is not None else sorted(doc.get("names") or {})
    # ⚑ ISA-0549: ONE row per economic security. Two requested labels for one canonical id (ONT and
    #   ONT.L) are counted ONCE; the first label is kept (callers key weights by it) and the
    #   collapse is published, never silent (R4.9).
    _seen, _dedup, _collapsed = {}, [], []
    for _t in tickers:
        _c = canonical_name(_t)
        if _c in _seen:
            _collapsed.append({"label": _t, "canonical": _c, "kept_label": _seen[_c]})
            continue
        _seen[_c] = _t
        _dedup.append(_t)
    tickers = _dedup
    # ⚑ ISA-0578. `refusals` (from stock_price_fetch.load_declared_refusals) is what lets this
    # report tell "NEVER FETCHED" from "TOO SHORT". Both read UNMEASURED and both take A2.3's
    # adverse 0.70, but they are different facts with different fixes — one needs a declared
    # symbol, the other needs time — and R2.10 forbids them producing the same output.
    refusals = refusals or {}
    # ⚑ ISA-0756: the clock is INJECTED where a caller has one (the pre-run's run date, a pinned
    #   consistency check); the wall clock is only the default for display callers.
    today = _as_date(as_of) if as_of is not None else datetime.date.today()
    _fresh = freshness(doc, tickers, as_of=today)["names"]
    out, measured = {}, 0
    for t in tickers:
        rets, meta = weekly_returns(doc, t)
        n = len(rets)
        status = ("MEASURED" if n >= TARGET_WEEKS else
                  "MEASURED_SHORT" if n >= MIN_WEEKS else "UNMEASURED")
        if status != "UNMEASURED":
            measured += 1
        obs = record_of(doc, t).get("observations") or {}   # ISA-0549
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
        # ⚑ ISA-0756: STALE is decided by the ONE freshness contract (missed settled Fridays at the
        #   injected clock), never by a second calendar-age rule here.
        _fs = (_fresh.get(t) or {})
        if _fs.get("state") == "STALE_INPUT" and status != "UNMEASURED":
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
                  "freshness_state": _fs.get("state"),
                  "missed_expected_observations": _fs.get("missed_expected_observations"),
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
            "aliases_collapsed": _collapsed,
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


def _selftest_0756():
    """ISA-0756: the freshness contract is clock-injected and deterministic (T-1/T/T+1)."""
    D = datetime.date
    doc = {"names": {"X": {"observations": {"2026-09-25": {}, "2026-10-02": {}}, "last_fetched_on": "2026-10-03"},
                     "Y": {"observations": {}}}}
    try:
        freshness(doc, ["X"], as_of=None)
        raise AssertionError("MUST-FIRE ISA-0756: freshness without a clock must refuse")
    except ValueError:
        pass
    st = lambda d: freshness(doc, ["X"], as_of=d)["names"]["X"]["state"]   # noqa: E731
    assert st(D(2026, 10, 3)) == "FRESH" and st(D(2026, 10, 9)) == "FRESH", "a Friday's own close is not yet due"
    assert st(D(2026, 10, 10)) == "LAGGING" and st(D(2026, 10, 16)) == "LAGGING", "T-1: one settled Friday missing"
    assert st(D(2026, 10, 17)) == "STALE_INPUT", "T: the second settled Friday missing -> STALE_INPUT"
    assert st(D(2026, 10, 18)) == "STALE_INPUT" and st(D(2026, 10, 18)) == st(D(2026, 10, 18)), "T+1 / repeat: deterministic"
    assert freshness(doc, ["Y"], as_of=D(2026, 10, 3))["names"]["Y"]["state"] == "UNMEASURED", \
        "NEGATIVE CONTROL: no observation is UNMEASURED, never FRESH"
    assert freshness(doc, ["X"], as_of=D(2026, 10, 1))["names"]["X"]["state"] == "FRESH", \
        "NEGATIVE CONTROL: data newer than the clock is never 'missed'"
    # coverage reads the SAME contract at the SAME clock (no second age rule)
    assert last_settled_friday(D(2026, 10, 16)) == D(2026, 10, 9) and last_settled_friday(D(2026, 10, 17)) == D(2026, 10, 16)
    return True


def _selftest():
    _selftest_0756()                          # ISA-0756 freshness contract (clock-injected, T-1/T/T+1)
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
                     "selftest", fx_to_gbp=0.78, fx_basis=FX_BASIS_GBP_PER_CCY)
    r, m = weekly_returns(d, "AAA")
    assert len(r) == 59 and m["gaps_skipped"] == 0, (len(r), m)
    # ISA-0809 — a stub (attempt-stamped, no currency, no observations) adopts its first currency
    d["names"]["STUB"] = {"last_fetched_on": "2026-10-03"}
    record_level(d, "STUB", base.isoformat(), 100.0, "USD", "selftest", fx_to_gbp=0.78, fx_basis=FX_BASIS_GBP_PER_CCY)
    assert d["names"]["STUB"]["currency"] == "USD" and len(d["names"]["STUB"]["observations"]) == 1, \
        "MUST-FIRE ISA-0809: a stub record must accept its first observation"
    try:
        record_level(d, "AAA", (base + datetime.timedelta(weeks=70)).isoformat(), 100.0, "EUR",
                     "selftest", fx_to_gbp=0.85, fx_basis=FX_BASIS_GBP_PER_CCY)
        raise AssertionError("NEGATIVE CONTROL ISA-0809: a REAL currency change must still raise")
    except ValueError:
        pass
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
    # ── ISA-0549 canonical identity (BuildSpec §15 fixtures 1-4, 8) ─────────────────────────
    smap = {"ONT": "ONT.L", "ONT.L": "ONT.L", "TWIN_A": "TWIN_A", "TWIN_B": "TWIN_B"}

    def _series(doc, key, vals):
        doc["names"][key] = {"currency": "GBP", "total_return_basis": False,
                             "observations": {(base + datetime.timedelta(weeks=i)).isoformat():
                                              {"gbp": v} for i, v in enumerate(vals)}}
    vals = [100.0 + i for i in range(159)]
    leg = _empty(); _series(leg, "ONT", vals); _series(leg, "ONT.L", vals)
    rep = identity_report(leg, smap)
    assert rep["state"] == "LEGACY_ALIASES" and rep["canonical_keys"] == 1, rep
    assert weekly_returns(leg, "ONT")[0] == weekly_returns(leg, "ONT.L")[0], \
        "ISA-0549: both labels resolve to ONE series before migration"
    mig = canonicalise(leg, smap, today="2026-09-16")
    assert mig["changed"] and list(leg["names"]) == ["ONT.L"] \
        and len(leg["names"]["ONT.L"]["observations"]) == 159 and leg["names"]["ONT.L"]["aliases"] == ["ONT"], \
        "ISA-0549 MUST-FIRE fixture 1: identical ONT + ONT.L -> one canonical record, no data loss"
    assert len(record_of(leg, "ONT", smap)["observations"]) == 159, \
        "ISA-0549: the broker label still resolves after migration"
    assert canonicalise(leg, smap)["changed"] is False, "ISA-0549: migration is idempotent"
    bad = _empty(); _series(bad, "ONT", vals); _series(bad, "ONT.L", vals[:-1] + [999.0])
    before = json.dumps(bad, sort_keys=True)
    try:
        canonicalise(bad, smap)
        raise AssertionError("ISA-0549 NEGATIVE CONTROL fixture 2: conflicting series must REFUSE migration")
    except CanonicalIdentityConflict:
        pass
    assert json.dumps(bad, sort_keys=True) == before, \
        "ISA-0549 NEGATIVE CONTROL: a refused migration leaves the store byte-identical (atomic)"
    tw = _empty(); _series(tw, "TWIN_A", vals); _series(tw, "TWIN_B", vals)
    assert identity_report(tw, smap)["state"] == "CANONICAL" and canonicalise(tw, smap)["changed"] is False \
        and sorted(tw["names"]) == ["TWIN_A", "TWIN_B"], \
        "ISA-0549 NEGATIVE CONTROL fixture 3: distinct securities with identical series stay DISTINCT"
    try:
        canonical_name("X", {"X": "Y", "Y": "Z"})
        raise AssertionError("ISA-0549 NEGATIVE CONTROL fixture 4: an alias chain must REFUSE")
    except CanonicalIdentityConflict:
        pass
    ch = _empty(); _series(ch, "X", vals)
    try:
        canonicalise(ch, {"X": "Y", "Y": "Z"})
        raise AssertionError("ISA-0549 NEGATIVE CONTROL: migration over an ambiguous map must REFUSE")
    except CanonicalIdentityConflict:
        pass
    n934 = _selftest_0934()
    print("stock_return_store selftest OK (9 + 11 ISA-0549 + %d ISA-0934 assertions)" % n934)


def _selftest_0934() -> int:
    """ISA-0934: direction through the store, rising/falling/constant sterling, inverse refusal,
    migration (exact, atomic, idempotent, refusing) and the strict load()."""
    import tempfile
    global STORE
    n = 0
    D = datetime.date
    wk = [D(2026, 1, 2) + datetime.timedelta(weeks=i) for i in range(3)]

    def series(quotes, locals_=(100.0, 100.0, 100.0)):
        d = _empty()
        for day, q, lv in zip(wk, quotes, locals_):
            record_level(d, "USX", day.isoformat(), lv, "USD", "t", fx_to_gbp=1.0 / q,
                         fx_basis=FX_BASIS_GBP_PER_CCY, fx_pair="GBPUSD=X", fx_quote=q,
                         recorded_on=day)
        return weekly_returns(d, "USX")[0], d
    # RISING STERLING (1.25 -> 1.30): a flat USD price LOSES in GBP
    r, _ = series((1.25, 1.30, 1.30))
    assert abs(r[wk[1].isoformat()] - (1.25 / 1.30 - 1.0)) < 1e-9 and r[wk[1].isoformat()] < 0, r; n += 1
    # FALLING STERLING (1.30 -> 1.25): a flat USD price GAINS in GBP
    r, _ = series((1.30, 1.25, 1.25))
    assert r[wk[1].isoformat()] > 0 and abs(r[wk[1].isoformat()] - (1.30 / 1.25 - 1.0)) < 1e-9, r; n += 1
    # CONSTANT FX: the GBP return IS the local return
    r, _ = series((1.30, 1.30, 1.30), (100.0, 110.0, 99.0))
    assert abs(r[wk[1].isoformat()] - 0.10) < 1e-9 and abs(r[wk[2].isoformat()] - (99.0 / 110.0 - 1)) < 1e-9; n += 1
    # NEGATIVE CONTROL - the defect's sign: under the old multiply-by-quote, rising sterling GAINED
    assert (100.0 * 1.30) / (100.0 * 1.25) - 1.0 > 0; n += 1
    # INVERSE refused: the quote in a GBP-per-ccy slot fails the reciprocal band
    try:
        record_level(_empty(), "USX", wk[0].isoformat(), 100.0, "USD", "t", fx_to_gbp=1.30,
                     fx_basis=FX_BASIS_GBP_PER_CCY, fx_pair="GBPUSD=X")
        raise AssertionError("ISA-0934 MUST-FIRE: an inverted multiplier must be refused by the band")
    except ValueError:
        pass
    n += 1
    # MIGRATION: an old-vintage doc (multiplied by the quote) becomes byte-equal to a fresh v2 write
    old = _empty()
    old["names"]["USX"] = {"currency": "USD", "total_return_basis": False, "observations": {}}
    old["names"]["GBX.L"] = {"currency": "GBP", "total_return_basis": False, "observations": {}}
    for day, q, lv in zip(wk, (1.25, 1.30, 1.28), (100.0, 104.0, 101.0)):
        old["names"]["USX"]["observations"][day.isoformat()] = {
            "level": lv, "gbp": round(lv * q, 8), "fx_to_gbp": q, "source_currency": "USD",
            "as_of": day.isoformat(), "source": "t", "stamp_basis": "point_in_time",
            "recorded_on": day.isoformat()}
        old["names"]["GBX.L"]["observations"][day.isoformat()] = {
            "level": 1250.0, "gbp": 12.5, "fx_to_gbp": 0.01, "source_currency": "GBp",
            "as_of": day.isoformat(), "source": "t"}
    assert fx_unqualified(old) == {"USX": 3}, fx_unqualified(old); n += 1
    before = json.dumps(old, sort_keys=True)
    res = migrate_fx_direction(old, today="2026-11-20", original_sha256="x")
    assert res["state"] == "MIGRATED" and res["n_corrected"] == 3 and res["n_stamped_pence"] == 3, res; n += 1
    _, fresh = series((1.25, 1.30, 1.28), (100.0, 104.0, 101.0))
    for k in fresh["names"]["USX"]["observations"]:
        a, b = old["names"]["USX"]["observations"][k], fresh["names"]["USX"]["observations"][k]
        assert abs(a["gbp"] - b["gbp"]) < 1e-8 and abs(a["fx_to_gbp"] - b["fx_to_gbp"]) < 1e-15, (a, b)
    n += 1
    assert res["two_derivation_max_return_delta"] < 1e-9, res; n += 1
    assert fx_unqualified(old) == {} and old["repairs"][-1]["item"] == "ISA-0934"; n += 1
    assert migrate_fx_direction(old, today="2026-11-21")["state"] == "NO_OP", "idempotent"; n += 1
    assert json.dumps(old, sort_keys=True) != before; n += 1
    # MIGRATION REFUSES atomically on a row that is NOT provably the inverted kind
    bad = json.loads(before)
    bad["names"]["USX"]["observations"][wk[1].isoformat()]["fx_to_gbp"] = 0.77   # already GBP/USD?
    bad_b = json.dumps(bad, sort_keys=True)
    rr = migrate_fx_direction(bad, today="2026-11-20")
    assert rr["state"] == "REFUSED" and json.dumps(bad, sort_keys=True) == bad_b, rr; n += 1
    # STRICT LOAD: an unmigrated store on disk is REFUSED; the migrated one loads
    STORE = os.path.join(tempfile.mkdtemp(), "s.json")
    with open(STORE, "w", encoding="utf-8") as fh:
        fh.write(before)
    try:
        load()
        raise AssertionError("ISA-0934 MUST-FIRE: load() must refuse an unqualified store")
    except StoreFXUnqualified:
        pass
    n += 1
    assert load(allow_fx_unqualified=True)["names"]["USX"]; n += 1
    save(old)
    assert load()["names"]["USX"]["observations"][wk[0].isoformat()]["fx_basis"] == FX_BASIS_GBP_PER_CCY; n += 1
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    if "--identity-report" in sys.argv:
        print(json.dumps(identity_report(load()), indent=1)); sys.exit(0)
    if "--canonicalise" in sys.argv:
        # ISA-0549 one-time migration of a legacy store. Writes a byte-exact pre-migration copy beside it.
        import shutil
        _doc = load()
        _bk = STORE + ".pre_ISA-0549_canonicalise"
        if not os.path.exists(_bk):
            shutil.copy2(STORE, _bk)
        _r = canonicalise(_doc)
        if _r["changed"]:
            save(_doc)
        print(json.dumps(dict(_r, backup=_bk, after=identity_report(load())), indent=1)); sys.exit(0)
    if "--migrate-fx-direction" in sys.argv:
        # ISA-0934 one-time migration. Freezes the byte-exact original, then CAS-replaces the store.
        import durable_store as _ds
        _orig = _ds.read_bytes(STORE)
        if _orig is None:
            print(json.dumps({"state": "NO_STORE", "store": STORE})); sys.exit(2)
        _sha = _ds.sha256_bytes(_orig)
        _fdir = os.path.join(os.path.dirname(STORE), "Dashboard", "state", "frozen_stores")
        os.makedirs(_fdir, exist_ok=True)
        _frozen = os.path.join(_fdir, "stock_weekly_returns.pre_ISA-0934.%s.json" % _sha[:12])
        _doc = json.loads(_orig.decode("utf-8"))
        _today = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--on=")), None)
        _rel = os.path.relpath(_frozen, os.path.dirname(STORE)).replace(os.sep, "/")
        _r = migrate_fx_direction(_doc, today=_today, original_sha256=_sha, frozen_path=_rel)
        if _r["state"] != "MIGRATED":
            print(json.dumps(_r, indent=1)); sys.exit(0 if _r["state"] == "NO_OP" else 3)
        if os.path.exists(_frozen):
            if _ds.sha256_file(_frozen) != _sha:
                print(json.dumps({"state": "REFUSED", "why": "frozen path exists with different bytes"})); sys.exit(3)
        else:
            _ds.atomic_write_bytes(_frozen, _orig, expected_sha=None)
        assert _ds.sha256_file(_frozen) == _sha
        _new = json.dumps(_doc, indent=1, ensure_ascii=False).encode("utf-8")
        _w = _ds.atomic_write_bytes(STORE, _new, expected_sha=_sha)
        _after = load()                                   # strict: must now be qualified
        _r.update(new_sha256=_ds.sha256_file(STORE), write=_w, strict_reload_ok=True,
                  unqualified_after=fx_unqualified(_after))
        print(json.dumps(_r, indent=1, default=str)); sys.exit(0)
    print(json.dumps(coverage(load()), indent=1))
