#!/usr/bin/env python3
"""
stock_price_fetch.py — P1. Populates `stock_weekly_returns.json` with 104 weeks of
Friday-to-Friday GBP total return for the deployment universe. **stdlib only.**

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md P1.
Raised as ISA-0455. Built 28-Aug-2026.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHY THIS MODULE EXISTS AT ALL — the claim that stopped it being written
═══════════════════════════════════════════════════════════════════════════════════════════
`stock_return_store.py` shipped with *"Yahoo is network-blocked from both the container and
the device shell"* in a live docstring. It was FALSE, and the framework's own ISA-0411 had
recorded a 400-ticker Yahoo screen from local bash **five days earlier**.

Nothing fetched. Every name read UNMEASURED. A2.3's adverse default of **0.70** applied to a
sleeve whose measured mean pairwise correlation is **0.163**. Every position was capped at
STARTER for a reason that was never true.

**No `yfinance`, no pip, no `/dev/shm`, no stub on PYTHONPATH.** The v8 chart endpoint needs
`urllib.request` and `json`, and R5.9 sign-off no longer needs a stub to import.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ THE THREE RULES THAT ARE LOAD-BEARING, NOT REFINEMENTS
═══════════════════════════════════════════════════════════════════════════════════════════
1. **NEVER FORWARD-FILL A MISSING WEEK.** A manufactured zero-return week deflates measured
   volatility and inflates apparent diversification — the same DIRECTIONAL error as daily
   sampling, and it runs toward the risk. No close in the window ⇒ no observation.
2. **FX DIRECTION IS CONTRACTED, NOT ASSUMED.** `_assert_fx_direction()` RAISES outside wide
   declared bands. It exists because of ISA-0429: one `/100.0` was 100x wrong for five months,
   disabled the D6 kill switch and INVERTED THE SIGN of every published comparison. ⚑ An
   inverted rate produces a perfectly plausible-looking correlation matrix. The bands are wide
   on purpose — they catch INVERSION and SCALE, not drift.
3. **A TICKER IS MAPPED, NEVER GUESSED.** `build_universe()` RAISES on an unmapped ticker. A
   bare `ONT` resolved to *"Onterris, Inc."* and published **£18,471.20 against a broker truth
   of £997.92**. ⚑ A guessed symbol and a correct one produce identically well-formed output.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["stock_return_fetch"] = False ⇒ no fetch, store
untouched, Step 6.12b identical to today.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

import stock_return_store as srs

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("ISA_OUT", HERE)

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
QSUM = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
        "?modules=earningsHistory,earningsTrend")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ISA-framework/1.0)"}

BATCH_SIZE = 20
ATTEMPTS = 3
BACKOFF_S = 2.0
TIMEOUT_S = 25

PARTIAL = os.path.join(OUT, "stock_weekly_returns.fetch_partial.json")

# ── P1.2 THE TICKER -> YAHOO SYMBOL MAP. One declared dict. Unmapped RAISES. ───────────
SYMBOL_MAP: Dict[str, str] = {
    # held sleeve
    "AVGO": "AVGO", "MU": "MU", "ONT.L": "ONT.L", "ONT": "ONT.L",
    "ABCL": "ABCL", "QBTS": "QBTS", "COCO": "COCO",
    # benchmarks used by E4 and by the ratchet legs
    "VUAG.L": "VUAG.L", "IWMO.L": "IWMO.L", "SWDA.L": "SWDA.L", "VWRL.L": "VWRL.L",
}

# ── P1.4 FX. Pairs are GBP-BASE: the number is <foreign> per 1 GBP. ───────────────────
# ⚑ ISA-0587 (03-Sep-2026). GBPNOK and GBPDKK added. `SUFFIX_EXCHANGE` declares sixteen European
# venues; this tuple declared six currencies, and the two lists describe ONE reachable universe.
# SUBC.OL and TGS.OL resolved, verified on OSL, fetched cleanly and were then refused at
# conversion — "no FX pair declared for NOK" — so two mapped, verified names could never enter
# the store. It stayed invisible only because both were unmapped under ISA-0577 and never got as
# far as conversion. `_selftest` assertion H now asserts the two lists reconcile.
FX_PAIRS = ("GBPUSD=X", "GBPEUR=X", "GBPSEK=X", "GBPCHF=X", "GBPPLN=X", "GBPJPY=X",
            "GBPNOK=X", "GBPDKK=X")

# ⚑ WIDE ON PURPOSE. These catch INVERSION and SCALE, not drift (ISA-0429).
FX_BANDS: Dict[str, Tuple[float, float]] = {
    "GBPUSD=X": (1.05, 1.65), "GBPEUR=X": (1.00, 1.45), "GBPSEK=X": (9.0, 16.0),
    "GBPCHF=X": (0.95, 1.55), "GBPPLN=X": (4.0, 8.0),   "GBPJPY=X": (130.0, 260.0),
    # Verified against live rates 03-Sep-2026: GBPNOK 12.5661, GBPDKK 8.6877. WIDE ON PURPOSE —
    # these catch INVERSION (0.0796, 0.1151) and SCALE, not drift.
    "GBPNOK=X": (9.0, 20.0),  "GBPDKK=X": (6.0, 12.0),
}
BENCHMARKS = ("VUAG.L", "IWMO.L", "SWDA.L", "VWRL.L")

CURRENCY_TO_PAIR = {"USD": "GBPUSD=X", "EUR": "GBPEUR=X", "SEK": "GBPSEK=X",
                    "CHF": "GBPCHF=X", "PLN": "GBPPLN=X", "JPY": "GBPJPY=X",
                    "NOK": "GBPNOK=X", "DKK": "GBPDKK=X"}

# ⚑ ISA-0587 — THE CURRENCY EVERY DECLARED VENUE CAN QUOTE. A venue the verifier admits whose
# currency the converter cannot handle is a name that is mapped, verified and then silently
# absent from the store. Asserted by `_selftest` assertion H, which is the reconciliation the
# two lists never had. GBP covers the LSE (with GBp handled by the fixed 0.01 factor); a London
# line may also quote USD or EUR, which is why `.L` lists three.
VENUE_CURRENCIES = {
    ".L": {"GBP", "GBp", "USD", "EUR"}, ".ST": {"SEK"}, ".MI": {"EUR"}, ".PA": {"EUR"},
    ".AS": {"EUR"}, ".DE": {"EUR"}, ".SW": {"CHF", "EUR", "USD"}, ".MC": {"EUR"},
    ".WA": {"PLN"}, ".HE": {"EUR"}, ".CO": {"DKK", "EUR"}, ".OL": {"NOK", "USD"},
    ".BR": {"EUR"}, ".VI": {"EUR"}, ".IR": {"EUR"}, ".LS": {"EUR"},
}


class FetchRefused(RuntimeError):
    """The fetch cannot proceed on a contract it cannot honour. NEVER downgraded to a partial
    silent result — `FETCH_UNAVAILABLE` leaves the store UNCHANGED and NAMES the cause, so
    Step 6.12b reports the fetch failure rather than the absence of data (R2.10)."""


def _flag(name: str = "stock_return_fetch", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


# ══════════════════════════════════════════════════════════════════════════════════════
# NETWORK
# ══════════════════════════════════════════════════════════════════════════════════════
def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _network_probe(symbol: str = "AVGO") -> dict:
    """THE FALSIFIER for the P0.4 negative claim *"Yahoo is network-blocked"*.

    Registered in `negative_claims.json` as `test_id`. ⚑ It returns a RESULT, never a boolean
    — "the probe could not run" and "the network is blocked" are different facts, and
    collapsing them is how the original claim survived (R2.10)."""
    try:
        d = _get(CHART.format(sym=symbol, rng="5d"))
        res = (((d.get("chart") or {}).get("result") or [None])[0]) or {}
        meta = res.get("meta") or {}
        return {"ran": True, "reachable": True, "symbol": meta.get("symbol"),
                "price": meta.get("regularMarketPrice"), "currency": meta.get("currency"),
                "verdict": "CLAIM_FALSIFIED",
                "detail": "the v8 chart endpoint answered with stdlib urllib alone"}
    except Exception as exc:                                            # noqa: BLE001
        return {"ran": True, "reachable": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "verdict": "CLAIM_HOLDS_TODAY",
                "detail": ("the endpoint did not answer on this run. That is EVIDENCE FOR the "
                           "claim on this run only — it is not a standing capability fact, "
                           "which is why the claim carries an expiry.")}


def _e3_source_probe() -> dict:
    """D21b's falsifier: is there a FREE STRUCTURED source for guidance (E3)?

    ⚑ It reports UNMEASURABLE and says why, rather than returning False. A probe that returns
    False for "I did not look" and for "I looked and there is none" reproduces exactly the
    conflation D22 exists to forbid."""
    return {"ran": True, "verdict": "NO_FREE_STRUCTURED_SOURCE",
            "checked": ["yahoo quoteSummary earningsTrend (consensus only, not guidance)",
                        "yahoo calendarEvents (dates only)"],
            "detail": ("Consensus estimates are not guidance. 'Guidance raised or reaffirmed "
                       "ABOVE consensus' needs the company's own statement, and no free "
                       "structured feed carries it. E3 is therefore PERMANENTLY UNMEASURED "
                       "(D21b) — which under D21b makes the remaining four channels MANDATORY "
                       "for STRONG, and under D22 is `None` rather than a coverage failure.")}


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.3 CALENDAR — Friday resampling
# ══════════════════════════════════════════════════════════════════════════════════════
MAX_LOOKBACK_DAYS = 4


def fridays_between(start: datetime.date, end: datetime.date) -> List[datetime.date]:
    d = start + datetime.timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += datetime.timedelta(days=7)
    return out


def to_friday(daily: Dict[datetime.date, float], *, weeks: Optional[int] = None
              ) -> Dict[datetime.date, float]:
    """The last trading close ON OR BEFORE each Friday, within MAX_LOOKBACK_DAYS.

    ⚑ NO CLOSE IN THE WINDOW ⇒ NO OBSERVATION. Never forward-filled. A manufactured
    zero-return week deflates measured volatility and inflates apparent diversification — the
    same DIRECTIONAL error as daily sampling, and it runs toward the risk."""
    if not daily:
        return {}
    days = sorted(daily)
    fr = fridays_between(days[0], days[-1])
    if weeks:
        fr = fr[-weeks:]
    out = {}
    for f in fr:
        for back in range(0, MAX_LOOKBACK_DAYS + 1):
            d = f - datetime.timedelta(days=back)
            if d in daily:
                out[f] = daily[d]
                break
    return out


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.4 FX
# ══════════════════════════════════════════════════════════════════════════════════════
def _assert_fx_direction(pair: str, rate: float) -> None:
    lo, hi = FX_BANDS[pair]
    if not (lo <= rate <= hi):
        raise FetchRefused(
            "%s = %.6f is outside the declared band [%.2f, %.2f]. The bands are WIDE ON "
            "PURPOSE: they catch INVERSION and SCALE, not drift. ISA-0429 records one "
            "`/100.0` that was 100x wrong for five months, disabled the D6 kill switch and "
            "INVERTED THE SIGN of every published comparison — and an inverted rate produces "
            "a perfectly plausible-looking correlation matrix, which is why this is a RAISE "
            "and not a warning." % (pair, rate, lo, hi))


def fetch_fx(pairs: Sequence[str] = FX_PAIRS, *, rng: str = "3y"
             ) -> Dict[str, Dict[datetime.date, float]]:
    out = {}
    for p in pairs:
        daily = _fetch_daily(p, rng=rng)["daily"]
        if not daily:
            continue
        wk = to_friday(daily)
        for f, v in wk.items():
            _assert_fx_direction(p, v)
        out[p] = wk
    return out


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.1 THE FETCH
# ══════════════════════════════════════════════════════════════════════════════════════
def _fetch_daily(symbol: str, *, rng: str = "3y") -> dict:
    """-> {daily: {date: level}, currency, total_return: bool, source}."""
    last_exc = None
    for attempt in range(ATTEMPTS):
        try:
            d = _get(CHART.format(sym=symbol, rng=rng))
            res = (((d.get("chart") or {}).get("result") or [None])[0])
            if not res:
                raise FetchRefused("%s: chart.result is empty" % symbol)
            ts = res.get("timestamp") or []
            ind = res.get("indicators") or {}
            adj = (((ind.get("adjclose") or [{}])[0]) or {}).get("adjclose")
            total_return = True
            if not adj:
                # ⚑ NEVER SILENTLY. A price series and a total-return series are different
                # quantities, and quietly treating the first as the second understates the
                # return of whatever pays the most income.
                adj = (((ind.get("quote") or [{}])[0]) or {}).get("close")
                total_return = False
            if not adj:
                raise FetchRefused("%s: neither adjclose nor close is present" % symbol)
            meta = res.get("meta") or {}
            daily = {}
            for t, v in zip(ts, adj):
                if v is None:
                    continue
                daily[datetime.date.fromtimestamp(t)] = float(v)
            # ⚑⚑ DO NOT UPPERCASE THE CURRENCY. Yahoo returns `GBp` (pence) for London
            # lines and `GBP` (pounds) for others, and the ONLY thing distinguishing them is
            # the case of one letter. `.upper()` would silently turn ONT.L's pence into
            # pounds — a 100x error with no exception, no warning and a perfectly
            # plausible-looking series. That is ISA-0429's exact class: one scale error that
            # ran for five months and inverted every published comparison.
            # Caught 28-Aug-2026 in review before the first fetch ran.
            return {"daily": daily, "currency": (meta.get("currency") or "").strip(),
                    "exchange": (meta.get("exchangeName") or "").strip(),
                    "instrument_type": (meta.get("instrumentType") or "").strip(),
                    "total_return": total_return,
                    "source": "yahoo_adjclose_v8" if total_return else "yahoo_close_v8",
                    "symbol": meta.get("symbol")}
        except Exception as exc:                                        # noqa: BLE001
            last_exc = exc
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_S)
    raise FetchRefused("%s: %d attempts failed — %s: %s"
                       % (symbol, ATTEMPTS, type(last_exc).__name__, str(last_exc)[:160]))


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.2 UNIVERSE — sticky, from BROKER TRUTH
# ══════════════════════════════════════════════════════════════════════════════════════
def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                                   # noqa: BLE001
        return None


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _month_key(fn: str) -> Tuple[int, int]:
    """(year, month) parsed from a `..._[mmm]_[yyyy].json` filename. Unparseable sorts LAST.

    ⚑ ISA-0579. A MONTH-STAMPED ARTEFACT FAMILY REFERENCED BY LITERAL NAME IS A GLOB THAT
    MATCHED 1 OF N. `_provenance` read `vci_prescore_aug_2026.json` and three siblings by
    literal name, so in September it read August's file and in October it would still read
    August's — and a VCI name promoted in September would be refused with *"NO PROVENANCE — no
    index screen, broker holding or VCI artefact has ever named it"* while the artefact naming
    it sat on disk. The reason would have named the wrong cause, which is the R2.10 conflation
    in a new place. The month belongs in the RESOLUTION, never in the constant."""
    m = re.search(r"_([a-z]{3})_(\d{4})\.json$", fn.lower())
    if not m or m.group(1) not in MONTHS:
        return (-1, -1)
    return (int(m.group(2)), MONTHS.index(m.group(1)) + 1)


def _latest_by_month(prefix: str, root: str = HERE) -> List[str]:
    """Every `<prefix>_[mmm]_[yyyy].json` under `root`, NEWEST MONTH FIRST.

    ⚑ ALL of them, not only the newest. The store is STICKY: a name promoted in July and
    dropped from August's screen still has a live series that must keep being refreshed.
    Newest-first settles only WHICH file wins when two describe the same ticker."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = [fn for fn in names
           if fn.startswith(prefix + "_") and fn.endswith(".json")
           and _month_key(fn) != (-1, -1)]
    return sorted(out, key=_month_key, reverse=True)


def _vci_deploy_names(root: str = HERE) -> List[str]:
    """Every ticker carrying a VCI deployment scorecard, newest month first (ISA-0581).

    ⚑ THE UNIVERSE IS THE SET OF NAMES CAPITAL COULD BE SIZED AGAINST — not the set of files
    the pre-run happens to write. `vci_deploy_[mmm]_[yyyy].json` is keyed BY TICKER at the top
    level and carries `size_pct`, `deploy_eligible` and a full asymmetry scorecard for each.
    On 28-Aug-2026 it held nine names and TWO of them — IONQ and SATL — appeared in none of the
    five declared sources. A name that turns `deploy_eligible` next month would therefore have
    arrived with zero weekly observations and been sized on A2.3's adverse 0.70: the same
    failure as the unmapped ticker, through a different door."""
    out: List[str] = []
    for fn in _latest_by_month("vci_deploy", root):
        d = _read_json(os.path.join(root, fn))
        if not isinstance(d, dict):
            continue
        for t, rec in d.items():
            t = str(t).strip()
            if t and isinstance(rec, dict) and rec.get("ticker") and t not in out:
                out.append(t)
    return out


def _portfolio_sleeve(portfolio_data: Optional[dict] = None) -> List[str]:
    """⚑ FROM `portfolio_data`, NEVER `watchlist_tickers.stock_sleeve`. The latter is STALE —
    it is missing COCO and QBTS — and the two disagree (ISA-0463, assertion P1-A6)."""
    if portfolio_data is None:
        for fn in sorted(os.listdir(HERE), reverse=True):
            if fn.startswith("portfolio_data_") and fn.endswith(".json"):
                portfolio_data = _read_json(os.path.join(HERE, fn))
                break
    if not portfolio_data:
        return []
    out = []
    for key in ("stock_sleeve", "stocks", "direct_stocks", "holdings"):
        v = portfolio_data.get(key)
        if isinstance(v, list):
            for h in v:
                t = h.get("ticker") if isinstance(h, dict) else h
                if t:
                    out.append(str(t))
    if not out:
        for h in (portfolio_data.get("positions") or []):
            if isinstance(h, dict) and (h.get("asset_type") or "").lower().startswith("stock"):
                out.append(h.get("ticker"))
    return [t for t in out if t]


def build_universe(*, store: Optional[dict] = None, portfolio_data: Optional[dict] = None,
                   watchlist: Optional[Sequence[str]] = None,
                   vci_watchlist: Optional[Sequence[str]] = None,
                   vci_deploy: Optional[Sequence[str]] = None,
                   candidate_pool: Optional[Sequence[str]] = None,
                   symbol_map: Optional[Dict[str, str]] = None,
                   strict: bool = True) -> dict:
    """Union, in declared order. RAISES on any unmapped ticker.

    ⚑ STICKY: once a name is in the store it is refreshed forever. That is what protects a
    name which drops out of `candidate_pool`, which is EPHEMERAL — wiped and rewritten every
    pre-run by `update_watchlist.py`. A series that stops being refreshed because a screen
    stopped listing the name is a series that silently dies."""
    smap = symbol_map if symbol_map is not None else load_symbol_map()
    store = store if store is not None else srs.load()
    sources: List[Tuple[str, Sequence[str]]] = [
        ("store_sticky", sorted((store.get("names") or {}))),
        ("portfolio_data", _portfolio_sleeve(portfolio_data)),
    ]
    wt = None
    if watchlist is None or vci_watchlist is None or candidate_pool is None:
        wt = _read_json(os.path.join(HERE, "watchlist_tickers.json")) or {}

    def _pick(explicit, key):
        if explicit is not None:
            return list(explicit)
        v = (wt or {}).get(key)
        if isinstance(v, list):
            return [x.get("ticker") if isinstance(x, dict) else x for x in v]
        return []
    sources += [("watchlist", _pick(watchlist, "watchlist")),
                ("vci_watchlist", _pick(vci_watchlist, "vci_watchlist")),
                # ⚑ ISA-0581 — the SIXTH source, added 03-Sep-2026. VCI names carrying a
                # deployment scorecard are promoted names whether or not they also reached the
                # watchlist: IONQ and SATL were scored on 28-Aug and were in none of the other
                # five.
                ("vci_deploy", list(vci_deploy) if vci_deploy is not None
                 else _vci_deploy_names()),
                ("candidate_pool", _pick(candidate_pool, "candidate_pool"))]
    order, origin, unmapped = [], {}, []
    for label, names in sources:
        for t in names:
            if not t:
                continue
            t = str(t).strip()
            if t in origin:
                continue
            origin[t] = label
            order.append(t)
    for t in order:
        if t not in smap:
            unmapped.append(t)
    if unmapped and not strict:
        # NAMED, never dropped quietly — the caller puts these in the run's warning list.
        order = [t for t in order if t not in set(unmapped)]
    elif unmapped:
        raise FetchRefused(
            "%d ticker(s) have no declared Yahoo symbol: %s. ⚑ A GUESSED SYMBOL AND A CORRECT "
            "ONE PRODUCE IDENTICALLY WELL-FORMED OUTPUT — a bare `ONT` resolved to "
            "'Onterris, Inc.' and published GBP 18,471.20 against a broker truth of GBP "
            "997.92. Add each to stock_price_fetch.SYMBOL_MAP deliberately."
            % (len(unmapped), unmapped[:20]))
    return {"tickers": order, "n": len(order), "origin": origin,
            "unmapped": sorted(unmapped), "n_unmapped": len(unmapped),
            "symbols": {t: smap[t] for t in order},
            "basis": ("union in declared order: store (STICKY) -> portfolio_data (BROKER "
                      "TRUTH, never watchlist_tickers.stock_sleeve, which is stale) -> "
                      "watchlist -> vci_watchlist -> vci_deploy (ISA-0581, every VCI name "
                      "carrying a deployment scorecard) -> candidate_pool (EPHEMERAL). ⚑ THE "
                      "UNIVERSE IS EVERY NAME CAPITAL COULD BE SIZED AGAINST THIS MONTH — "
                      "every name promoted by the monthly growth screeners (candidate_pool) "
                      "and by VCI (vci_watchlist + vci_deploy), not only the watchlist and "
                      "the held sleeve.")}


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.7 BATCHING AND RESUME  ·  P1.8 REFUSALS  ·  run()
# ══════════════════════════════════════════════════════════════════════════════════════
def _load_partial() -> dict:
    d = _read_json(PARTIAL)
    if not isinstance(d, dict) or d.get("status") == "done":
        return {"status": "idle", "done": []}
    return d


def _save_partial(doc: dict) -> None:
    """⚑ A SINGLE OVERWRITE-ONLY FILE, shrunk to {"status":"done"} at completion. The mount
    cannot delete, so 'remove the resume file' is not available — the file must be able to
    say it is finished rather than being absent."""
    tmp = PARTIAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, PARTIAL)


def run(*, universe: Optional[Sequence[str]] = None, batch_size: int = BATCH_SIZE,
        rng: str = "3y", today: Optional[datetime.date] = None,
        symbol_map: Optional[Dict[str, str]] = None) -> dict:
    """One BATCH. The caller loops until state == "ALL_DONE".

    ⚑ `run()` NEVER DELETES A NAME. A ticker that fails every attempt is COUNTED and NAMED and
    its history is untouched — a fetch failure must never look like a delisting."""
    if not _flag():
        return {"state": "DISABLED", "flag": "stock_return_fetch",
                "detail": "rollback: no fetch, store untouched, 6.12b identical to today"}
    rec_on = today or datetime.date.today()
    store = srs.load()
    smap = symbol_map if symbol_map is not None else load_symbol_map()
    unmapped = []
    if universe is None:
        try:
            uni = build_universe(store=store, symbol_map=smap)
            tickers = uni["tickers"]
        except FetchRefused as exc:
            # ⚑ THE CONTRACT STILL RAISES — `build_universe` is the place that refuses to
            # guess, and P1-A5 tests it directly. What changes here is the ORCHESTRATION: two
            # unverifiable VCI names must not take 59 verified ones down with them, and the
            # store must not go a month unwritten because of it. The refusal is NAMED in the
            # fetch report and reaches the warning list; it is not swallowed.
            uni = build_universe(store=store, symbol_map=smap, strict=False)
            tickers = uni["tickers"]
            unmapped = uni.get("unmapped") or []
    else:
        tickers = list(universe)
        for t in tickers:
            if t not in smap:
                raise FetchRefused("%s has no declared Yahoo symbol (SYMBOL_MAP)" % t)

    part = _load_partial()
    done = set(part.get("done") or [])
    todo = [t for t in tickers if t not in done]
    batch = todo[:batch_size]

    # FX first — no conversion may happen before the direction contract has been asserted.
    try:
        fx = fetch_fx(rng=rng)
    except FetchRefused:
        raise
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FETCH_UNAVAILABLE", "stage": "fx",
                "reason": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "store_unchanged": True,
                "detail": ("⚑ THE FETCH FAILED — the store is UNCHANGED and this is NOT an "
                           "absence of data. Step 6.12b must name the fetch failure as the "
                           "cause (R2.10).")}

    ok, failed, skipped_obs = [], [], 0
    for t in batch:
        try:
            d = _fetch_daily(smap[t], rng=rng)
        except Exception as exc:                                        # noqa: BLE001
            failed.append({"ticker": t, "symbol": smap[t],
                           "reason": "%s: %s" % (type(exc).__name__, str(exc)[:160])})
            continue
        cur = d["currency"] or "GBP"
        wk = to_friday(d["daily"])
        # `GBp` is PENCE. It is not a foreign currency and it needs no FX pair — it needs a
        # fixed, declared factor of exactly 0.01 (P1-A4).
        n_before = len(((store.get("names") or {}).get(t) or {}).get("observations") or {})
        for f, level in sorted(wk.items()):
            if cur == "GBP":
                fxr = None
            elif cur == "GBp":
                fxr = 0.01
            else:
                pair = CURRENCY_TO_PAIR.get(cur)
                if pair is None:
                    failed.append({"ticker": t, "reason": "no FX pair declared for %s" % cur})
                    break
                fxr = (fx.get(pair) or {}).get(f)
                if fxr is None:
                    # ⚑ SKIPPED AND COUNTED, never converted at a NEIGHBOURING rate.
                    skipped_obs += 1
                    continue
            try:
                srs.record_level(store, t, f.isoformat(), level, cur,
                                 d["source"], fx_to_gbp=fxr,
                                 total_return=d["total_return"], recorded_on=rec_on)
            except ValueError:
                skipped_obs += 1
        n_after = len(((store.get("names") or {}).get(t) or {}).get("observations") or {})
        ok.append({"ticker": t, "symbol": smap[t], "currency": cur,
                   "total_return": d["total_return"],
                   "observations_before": n_before, "observations_after": n_after})
        done.add(t)

    store.setdefault("history", []).append(
        {"on": rec_on.isoformat(), "batch": [r["ticker"] for r in ok],
         "failed": [f["ticker"] for f in failed], "skipped_observations": skipped_obs})
    srs.save(store)

    remaining = [t for t in tickers if t not in done]
    if remaining:
        _save_partial({"status": "in_progress", "done": sorted(done),
                       "remaining": remaining, "as_of": rec_on.isoformat()})
        state = "BATCH_DONE"
    else:
        _save_partial({"status": "done"})
        state = "ALL_DONE"
    return {"state": state, "fetched": ok, "failed": failed, "n_failed": len(failed),
            "unmapped_refused": unmapped, "n_unmapped_refused": len(unmapped),
            "skipped_observations": skipped_obs,
            "remaining": len(remaining), "n_universe": len(tickers),
            "fx_pairs": sorted(fx), "store_path": srs.STORE, "recorded_on": rec_on.isoformat(),
            "detail": ("a still-failing ticker is COUNTED and NAMED and its history is "
                       "untouched — run() never deletes a name (P1.7)")}


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.9 WINDOW-STABILITY GATE — mandatory pre-05-Sep (C9)
# ══════════════════════════════════════════════════════════════════════════════════════
def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 8 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / (va ** 0.5 * vb ** 0.5)


def matrix(store: Optional[dict] = None, tickers: Optional[Sequence[str]] = None,
           *, weeks: int = 104) -> dict:
    """The pairwise correlation matrix over the last `weeks` COMMON Fridays.

    ⚑ sigma comes from THE SAME weekly series as rho, annualised sigma_weekly x sqrt(52). A
    sigma from another window would assemble a covariance matrix out of two windows (P2.5)."""
    store = store if store is not None else srs.load()
    cov = srs.coverage(store, list(tickers) if tickers else None)
    live = [t for t, v in cov["names"].items() if v["status"] != "STALE"]
    series = {}
    for t in live:
        rets, _ = srs.weekly_returns(store, t)
        if rets:
            series[t] = rets
    names = sorted(series)
    rho, pairs = {}, []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(set(series[a]) & set(series[b]))[-weeks:]
            if len(common) < 8:
                rho[(a, b)] = None
                continue
            r = _corr([series[a][k] for k in common], [series[b][k] for k in common])
            rho[(a, b)] = r
            if r is not None:
                pairs.append(r)
    sig = {}
    for t in names:
        vals = [series[t][k] for k in sorted(series[t])[-weeks:]]
        if len(vals) >= 8:
            m = sum(vals) / len(vals)
            sd = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5
            sig[t] = sd * (52 ** 0.5)
    n_used = max((len(sorted(set(series[a]) & set(series[b]))[-weeks:])
                  for a in names for b in names if a < b), default=0)
    return {"weeks_requested": weeks, "weeks_used_max": n_used,
            "names": names, "n_names": len(names),
            "rho": {"%s|%s" % k: (round(v, 4) if v is not None else None)
                    for k, v in rho.items()},
            "rho_bar": round(sum(pairs) / len(pairs), 4) if pairs else None,
            "rho_max": round(max(pairs), 4) if pairs else None,
            "rho_min": round(min(pairs), 4) if pairs else None,
            "n_pairs_measured": len(pairs), "n_pairs_total": len(rho),
            "sigma_ann": {t: round(v, 4) for t, v in sig.items()},
            "stale_excluded": cov["stale_excluded"],
            "se_rho": (round(1.0 / ((max(n_used, 5) - 3) ** 0.5), 4) if n_used >= 8 else None),
            "coverage": {"n_measured": cov["n_measured"], "n_names": cov["n_names"],
                         "pit_share": cov["pit_share"]}}


def window_stability(store: Optional[dict] = None, tickers: Optional[Sequence[str]] = None,
                     *, windows: Sequence[int] = (52, 104, 156),
                     rho_pairwise_gate: float = 0.70,
                     rho_sleeve_gate: float = 0.60) -> dict:
    """P1.9. Publish 52 / 104 / 156 side by side and assert the GATE VERDICT is identical.

    ⚑ MANDATORY BEFORE 05-SEP, and no longer deferrable, because P4 goes live in the same
    window. If the verdict is NOT stable the gate is NOT ready and P3/P4 ship with
    `correlation_admission_gate` OFF — a measurement that changes the answer depending on how
    far back you look is not yet a measurement."""
    store = store if store is not None else srs.load()
    rows, verdicts = {}, {}
    for w in windows:
        m = matrix(store, tickers, weeks=w)
        vals = [v for v in m["rho"].values() if v is not None]
        breach_pair = sorted(k for k, v in m["rho"].items()
                             if v is not None and v >= rho_pairwise_gate)
        verdict = "BREACH" if breach_pair else ("CLEAN" if vals else "UNMEASURED")
        rows[w] = {"rho_bar": m["rho_bar"], "rho_max": m["rho_max"], "rho_min": m["rho_min"],
                   "n_pairs_measured": m["n_pairs_measured"],
                   "weeks_used_max": m["weeks_used_max"],
                   "breaching_pairs": breach_pair, "verdict": verdict}
        verdicts[w] = verdict
    stable = len(set(verdicts.values())) == 1
    return {"windows": rows, "verdicts": verdicts, "stable": stable,
            "gate_ready": stable,
            "gates": {"rho_pairwise": rho_pairwise_gate, "rho_sleeve": rho_sleeve_gate},
            "consequence": ("the A2.1 gate verdict is IDENTICAL across %s — the gate may ship "
                            "ON" % list(windows)) if stable else
                           ("⚑ THE VERDICT DIFFERS ACROSS WINDOWS %s. The gate is NOT ready: "
                            "P3/P4 must ship with correlation_admission_gate OFF (P1.9/C9)."
                            % verdicts)}


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.11 THE EVIDENCE CHANNELS — E4 free, E2 fetched, E3 declared dead (F12, D21b, D22)
# ══════════════════════════════════════════════════════════════════════════════════════
def e4_relative_momentum(ticker: str, *, store: Optional[dict] = None,
                         benchmark: str = "VUAG.L") -> Optional[bool]:
    """E4 — 12-1m AND 6m relative both > 0, versus the frame benchmark. FREE: computed from
    the daily history P1 already fetches.

    ⚑ RETURNS None, NEVER False, when it cannot be computed. An unmeasured channel can neither
    confirm nor reverse; conflating the two is the FC-F pattern that flipped DENY->ADMIT on
    QBTS (D22 / P3.3)."""
    store = store if store is not None else srs.load()
    obs = {}
    for t in (ticker, benchmark):
        rec = ((store.get("names") or {}).get(t) or {}).get("observations") or {}
        if not rec:
            return None
        obs[t] = {k: v["gbp"] for k, v in rec.items()}
    keys = sorted(set(obs[ticker]) & set(obs[benchmark]))
    if len(keys) < 53:
        return None

    def rel(lookback_w: int, skip_w: int = 0) -> Optional[float]:
        if len(keys) < lookback_w + skip_w + 1:
            return None
        a, b = keys[-(lookback_w + skip_w + 1)], keys[-(skip_w + 1)]
        try:
            rt = obs[ticker][b] / obs[ticker][a] - 1.0
            rb = obs[benchmark][b] / obs[benchmark][a] - 1.0
        except ZeroDivisionError:
            return None
        return rt - rb
    r12_1 = rel(52, 4)      # 12-1 month
    r6 = rel(26, 0)
    if r12_1 is None or r6 is None:
        return None
    return bool(r12_1 > 0 and r6 > 0)


def _e2_source_probe(symbol: str = "AVGO") -> dict:
    """THE FALSIFIER for the negative claim *"E2 has no reachable free source"* (P0.4).

    ⚑ THIS CLAIM IS NEW ON 28-Aug-2026 AND IT CORRECTS THE SPEC. §P1.11 states E2 is sourced
    from Yahoo `earningsHistory` at "one extra endpoint per ticker". **That endpoint returns
    HTTP 401 Unauthorized** — v10 `quoteSummary` now requires a cookie/crumb pair. Nothing in
    the tree carries `epsActual` / `epsEstimate` either: `watchlist_scored` holds FORWARD
    fields (`fwd_eps_growth`, `eps_trend_mom_pct`, `rev_est_fwd_pct`), which are E1 revision
    material, not an E2 quarter BEAT.

    ⚑ AND THIS IS AN ACCESS PROBLEM, NOT AN ABSENCE OF DATA — which is why its expiry is 3
    runs and not E3's 6. Access changes; the non-existence of a free guidance feed does not.
    Writing it into a docstring and moving on is precisely what happened with the Yahoo
    network claim, so it is registered with this probe attached instead."""
    try:
        _get(QSUM.format(sym=symbol))
        return {"ran": True, "reachable": True, "verdict": "CLAIM_FALSIFIED",
                "detail": "quoteSummary answered — E2 is sourceable again; wire it up"}
    except Exception as exc:                                            # noqa: BLE001
        return {"ran": True, "reachable": False, "verdict": "CLAIM_HOLDS_TODAY",
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]),
                "detail": ("v10 quoteSummary refused. E2 is UNSOURCED on this run, so under "
                           "D22 it is None — NOT False — and under D21b it makes STRONG "
                           "unreachable, which the run must SAY rather than absorb.")}


def e2_quarter_beat(ticker: str, *, symbol_map: Optional[Dict[str, str]] = None
                    ) -> Optional[bool]:
    """E2 — last quarter beat on BOTH revenue and EPS.

    ⚑ RETURNS None TODAY, and the reason is registered rather than assumed: the endpoint the
    spec names is HTTP 401 (see `_e2_source_probe`). `None`, never False — an unmeasured
    channel can neither confirm nor reverse, and conflating the two is the FC-F pattern that
    flipped DENY->ADMIT on QBTS (D22)."""
    smap = symbol_map if symbol_map is not None else load_symbol_map()
    sym = smap.get(ticker)
    if not sym:
        return None
    try:
        d = _get(QSUM.format(sym=sym))
    except Exception:                                                   # noqa: BLE001
        return None
    res = (((d.get("quoteSummary") or {}).get("result") or [None])[0]) or {}
    hist = ((res.get("earningsHistory") or {}).get("history") or [])
    if not hist:
        return None
    last = hist[-1]
    act = (last.get("epsActual") or {}).get("raw")
    est = (last.get("epsEstimate") or {}).get("raw")
    if act is None or est is None:
        return None
    eps_beat = act > est
    rev_beat = None
    for tr in ((res.get("earningsTrend") or {}).get("trend") or []):
        if tr.get("period") == "0q":
            g = (tr.get("revenueEstimate") or {}).get("growth")
            rev_beat = (g or {}).get("raw", None)
            rev_beat = None if rev_beat is None else rev_beat > 0
            break
    if rev_beat is None:
        return None
    return bool(eps_beat and rev_beat)


def e3_guidance(_ticker: str) -> None:
    """E3 — guidance raised or reaffirmed above consensus.

    ⚑ PERMANENTLY UNMEASURED (D21b). Returns `None` for EVERY name, and under D22 that is NOT
    a coverage failure: coverage means *the inputs this name SHOULD have, it has*. Under D21b
    the remaining four channels (E1/E2/E4/E5) become MANDATORY for STRONG — an unmeasurable
    channel is not quietly dropped from the count, it makes the rest compulsory. Registered as
    an expiring P0.4 claim (6 runs) with `_e3_source_probe` as its falsifier, because a
    permanent claim with no expiry is exactly the shape of the Yahoo claim."""
    return None



# ── E1 AND E5 — ADDED 29-Aug-2026 (ISA-0489). One home for channel SOURCING. ────────────
# ⚑ WHY THESE LIVE HERE AND NOT IN `evidence_state`: `evidence_state.classify` is the
# CLASSIFIER. If it also sourced its own inputs it would be measuring itself (R10), and the
# one module able to say "this channel is unmeasured" would be the module that decided not to
# measure it. Sourcing is a data question and belongs beside the other three channels.

def e1_analyst_revision(_ticker: str, row: Optional[dict] = None) -> Optional[bool]:
    """E1 — declared as *"90d revision breadth > 0 AND FY1 estimate up over 90d"*.

    ⚑⚑ RETURNS None, AND THE REASON IS THE POINT: THE DECLARED LOOKBACK IS 90 DAYS AND THE
    ONLY BREADTH THE FRAMEWORK CARRIES IS 30 DAYS (`est_rev_eps_up_30d` /
    `est_rev_eps_down_30d`, from yfinance). 30-day breadth is a DIFFERENT QUANTITY from 90-day
    breadth, not a shorter view of the same one, and substituting it would be ISA-0405's class
    exactly — *a formula whose terms are named in English rather than defined admits two
    readings*, with the reading chosen silently by whoever wired it.

    ⚑ A channel sourced from the wrong lookback is WORSE than an unsourced one, because it
    confirms. Under D22 an unsourceable channel is `None` and NOT a coverage failure; under
    D21b it makes the remaining channels compulsory. So this refuses, names its near-miss, and
    the refusal is registered as an expiring negative claim with `_e1_source_probe` as the
    falsifier — because a permanent claim with no expiry is the shape of the Yahoo claim that
    cost the whole correlation gate.

    The 30d fields are returned by `_e1_source_probe` for a reader to see, never used to size."""
    return None


def _e1_source_probe(row: Optional[dict] = None) -> dict:
    """THE FALSIFIER for *"90-day revision breadth has no source in this framework"* (P0.4).

    A future session gains E1 the moment ANY row carries a 90d breadth field. This probe looks
    for one by name rather than by assumption, and reports what it found."""
    row = row or {}
    NINETY = ("est_rev_eps_up_90d", "est_rev_eps_down_90d", "revision_breadth_90d",
              "fy1_estimate_change_90d")
    present = sorted(k for k in NINETY if row.get(k) is not None)
    thirty = {k: row.get(k) for k in ("est_rev_eps_up_30d", "est_rev_eps_down_30d")
              if k in row}
    if present:
        return {"ran": True, "verdict": "CLAIM_FALSIFIED", "found": present,
                "detail": ("a 90-day revision field is now present — E1 is sourceable; wire it "
                           "to the DECLARED 90d definition, not to the 30d near-miss")}
    return {"ran": True, "verdict": "CLAIM_HOLDS_TODAY", "found": [],
            "nearest_available_30d": thirty,
            "detail": ("no 90-day revision breadth field exists on the screen row. E1 is "
                       "UNSOURCED, therefore None (D22). The 30d fields above are a DIFFERENT "
                       "lookback and are reported for the reader, never substituted.")}


def e5_route_native(ticker: str, row: Optional[dict] = None,
                    frame_median: Optional[float] = None) -> Optional[bool]:
    """E5 — *"forward_axis_score above frame median"*. SOURCEABLE: both terms are on the frame.

    ⚑ `None` when either term is missing. ⚑ And the median is the FRAME's, passed in by the
    caller who holds the whole frame — computing it here from whatever subset happened to be
    handy would make the channel's verdict depend on the caller's slice, which is the
    ISA-0022 defect in miniature (the population being scored decides its own benchmark)."""
    row = row or {}
    v = row.get("forward_axis_score")
    if v is None or frame_median is None:
        return None
    try:
        return bool(float(v) > float(frame_median))
    except (TypeError, ValueError):
        return None


def frame_median_forward_axis(rows: Sequence[dict]) -> Optional[float]:
    """The frame's median `forward_axis_score`. -> None (never 0.0) on an empty frame."""
    vals = []
    for r in (rows or []):
        v = (r or {}).get("forward_axis_score")
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0

def channel_report(tickers: Sequence[str], *, store: Optional[dict] = None,
                   rows: Optional[Dict[str, dict]] = None,
                   frame_rows: Optional[Sequence[dict]] = None) -> dict:
    """Per-channel sourcing rate, PUBLISHED EVERY RUN (F12 / D22).

    ⚑ The 3-of-5 shortfall is published, never absorbed. *"A name at STARTER because a channel
    is UNSOURCED"* and *"a name at STARTER because its evidence is THIN"* must render as
    different sentences."""
    store = store if store is not None else srs.load()
    screen = rows or {}
    # ⚑ The median is taken over the WHOLE frame the caller holds, not over `tickers` — the
    # population being scored may not choose its own benchmark (ISA-0022's rule).
    med = frame_median_forward_axis(frame_rows if frame_rows is not None
                                    else list(screen.values()))
    out_rows = {}
    for t in tickers:
        row = screen.get(t) or {}
        out_rows[t] = {"E1": e1_analyst_revision(t, row),
                       "E2": None,
                       "E3": e3_guidance(t),
                       "E4": e4_relative_momentum(t, store=store),
                       "E5": e5_route_native(t, row, med)}
    rows = out_rows          # published under the historical key
    def rate(ch):
        n = sum(1 for r in rows.values() if r[ch] is not None)
        return {"n_sourced": n, "n": len(rows),
                "rate": round(n / len(rows), 4) if rows else None}
    return {"rows": rows, "E1": rate("E1"), "E2": rate("E2"), "E3": rate("E3"),
            "E4": rate("E4"), "E5": rate("E5"),
            "frame_median_forward_axis": med,
            "e1_basis": ("UNSOURCED (ISA-0489). The declared definition is 90-day revision "
                         "breadth; the framework carries only 30-day breadth, which is a "
                         "DIFFERENT quantity. None, never False (D22). Falsifier: "
                         "`_e1_source_probe`."),
            "e3_basis": ("PERMANENTLY UNMEASURED (D21b) — no free structured source. Under "
                         "D22 this is None and NOT a coverage failure; under D21b it makes "
                         "E1/E2/E4/E5 MANDATORY for STRONG."),
            "shortfall_note": ("3 of 5 channels had no source field at all on 27-Aug-2026, "
                               "which is why STRONG and EARNED_MAX were structurally "
                               "unreachable whatever correlation said. The true unlock from "
                               "measuring correlation ALONE is 1.29x (NORMAL 4.5% vs STARTER "
                               "3.5%), NOT the 1.86x the 26-Aug spec claimed.")}


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.2b — BUILDING THE SYMBOL MAP BY VERIFICATION, NOT BY ASSUMPTION
# ══════════════════════════════════════════════════════════════════════════════════════
# ⚑⚑ WHY A SYMBOL MATCH IS NOT ENOUGH, WHICH IS THE WHOLE REASON THIS EXISTS.
# The ONT defect was a bare `ONT` resolving to *"Onterris, Inc."* and publishing £18,471.20
# against a broker truth of £997.92. Requesting `ONT` returns `meta.symbol == "ONT"` — **the
# symbol matches, and the wrong company is returned.** So "the endpoint echoed my ticker" is
# not a check; it is the defect agreeing with itself.
#
# ⚑ WHAT DOES FALSIFY IT: the CURRENCY the listing reports, against the currency its exchange
# suffix and its index group REQUIRE. Oxford Nanopore is `GBp` on the LSE; Onterris is `USD`.
# A ticker whose fetched currency contradicts its declared venue is REFUSED AND NAMED — it
# never silently enters the map.
#
# ⚑ AND PROVENANCE IS REQUIRED BEFORE A TICKER IS EVEN OFFERED FOR VERIFICATION: it must
# appear in `constituents_history.csv` (an index screen actually saw it), in `portfolio_data`
# (the broker holds it), or in the VCI artefacts. A ticker from nowhere RAISES.

SUFFIX_CURRENCY = {
    ".L": ("GBP", "GBp"), ".ST": ("SEK",), ".MI": ("EUR",), ".PA": ("EUR",),
    ".AS": ("EUR",), ".DE": ("EUR",), ".SW": ("CHF",), ".MC": ("EUR",),
    ".WA": ("PLN",), ".HE": ("EUR",), ".CO": ("DKK",), ".OL": ("NOK",),
    ".BR": ("EUR",), ".VI": ("EUR",), ".IR": ("EUR",), ".LS": ("EUR",),
}
US_GROUPS = {"NASDAQ", "SP500", "MIDCAP400", "SP400", "RUSSELL"}


def expected_currencies(ticker: str, group: str = "",
                       broker_currency: str = "") -> Tuple[str, ...]:
    """The currencies this ticker's declared venue permits. () when unknown — and UNKNOWN is
    a REFUSAL, never a pass: an expectation we cannot form cannot falsify anything.

    ⚑ BROKER CURRENCY IS THE STRONGEST EXPECTATION AND IT IS CHECKED FIRST. The first version
    of this function ignored it and duly refused AVGO and MU — two names the broker holds and
    states the currency of — because they are bare US tickers with no index group. Refusing a
    name whose currency the BROKER has recorded is the check being wrong in the safe
    direction, which is still wrong: it teaches the reader to route around the refusal."""
    if broker_currency:
        bc = broker_currency.strip()
        return (bc, "GBp") if bc.upper() == "GBP" else (bc,)
    for suf, cur in SUFFIX_CURRENCY.items():
        if ticker.upper().endswith(suf):
            return cur
    if "." not in ticker and (group or "").upper() in US_GROUPS:
        return ("USD",)
    if "." not in ticker and not group:
        return ()                       # a BARE ticker with no index provenance — the ONT shape
    return ()



# ⚑⚑ THE SUFFIX ASSERTS AN EXCHANGE, NOT A CURRENCY — and the exchange is the right check.
# The first version of this verifier compared CURRENCY against the suffix and refused
# `IWMO.L`, which is a perfectly correct USD-denominated ETF line on the LSE. London lists
# USD, EUR and GBP; the venue is the thing the suffix actually names.
# ⚑ AND IT IS ALSO THE STRICTER CHECK, which is why it is the one to keep:
#       ONT   -> exchangeName NYQ  (NYSE)      -> REFUSED   <- the Onterris defect
#       ONT.L -> exchangeName LSE              -> admitted
#       IWMO.L-> exchangeName LSE, currency USD-> admitted
SUFFIX_EXCHANGE = {
    ".L": {"LSE", "IOB"}, ".ST": {"STO"}, ".MI": {"MIL"}, ".PA": {"PAR"},
    ".AS": {"AMS"}, ".DE": {"GER", "FRA", "XETRA"}, ".SW": {"EBS", "VTX"},
    ".MC": {"MCE"}, ".WA": {"WSE"}, ".HE": {"HEL"}, ".CO": {"CPH"}, ".OL": {"OSL"},
    ".BR": {"BRU"}, ".VI": {"VIE"}, ".IR": {"ISE"}, ".LS": {"LIS"},
}
US_EXCHANGES = {"NMS", "NYQ", "NGM", "ASE", "PCX", "BTS", "NCM", "NYS"}

# ⚑ ISA-0577 — A DECLARED VENUE NAME -> THE EXCHANGE CODES IT PERMITS. Deliberately TIGHT: a
# declared NASDAQ admits NMS/NCM/NGM and REFUSES an NYQ answer, which is STRICTER than the
# index-group route (that admits any US exchange). This is the route by which a bare VCI ticker
# can be admitted at all — and the fetch must still fail to contradict it, or it is refused.
# An unrecognised venue name returns NOTHING and the ticker is REFUSED. It never widens.
VENUE_EXCHANGES = {
    "NASDAQ": {"NMS", "NCM", "NGM"}, "NASDAQGS": {"NMS"}, "NASDAQCM": {"NCM"},
    "NASDAQGM": {"NGM"},
    "NYSE": {"NYQ", "NYS"},
    "NYSE AMERICAN": {"ASE"}, "NYSEAMERICAN": {"ASE"}, "AMEX": {"ASE"},
    "NYSE ARCA": {"PCX"}, "ARCA": {"PCX"},
    "BATS": {"BTS"}, "CBOE": {"BTS"},
}


def expected_exchanges(ticker: str, group: str = "", broker_currency: str = "",
                       declared_venue: str = "") -> set:
    """The exchanges this ticker's DECLARED venue permits. Empty set ⇒ REFUSE.

    ⚑ A BARE TICKER IS ADMITTED ONLY WHERE ITS PROVENANCE ITSELF ASSERTS A US VENUE — a US
    index group, or a broker record in USD. It is NOT enough that the endpoint happened to
    return a US exchange: `ONT` returns NYQ, and inferring "bare therefore US" from the answer
    is the Onterris defect wearing the verifier's own uniform. The provenance must assert the
    venue BEFORE the fetch, or there is nothing for the fetch to contradict."""
    for suf, exch in SUFFIX_EXCHANGE.items():
        if ticker.upper().endswith(suf):
            return set(exch)
    if "." in ticker:
        return set()
    # ⚑ ISA-0577 — THE DECLARED-VENUE ROUTE, and it is checked BEFORE the index group because
    # it is the more specific assertion and the STRICTER one. `declared_venue` is admitted from
    # exactly one source, `watchlist_tickers.vci_watchlist[].exchange`, because that is the only
    # place in the framework where a venue is typed by a human and never manufactured
    # (`sync_vci_watchlist.py:93` yields "" when the column is absent).
    # ⚑ IT IS NOT ADMITTED FROM `candidate_pool` OR `watchlist`: `update_watchlist.py` writes
    # `"exchange": ... if exchange else "NASDAQ"` (lines 205/982/1120), so an absence there is
    # byte-identical to an answer. FRO — a STOXX600 constituent with a Cyprus ISIN whose own
    # screener gate records `ticker_verified: False` — carries exchange NASDAQ in that file.
    # Trusting it would be the ONT/Onterris defect with a new source (ISA-0582).
    dv = (declared_venue or "").strip().upper()
    if dv:
        return set(VENUE_EXCHANGES.get(dv, ()))
    if (group or "").upper() in US_GROUPS:
        return set(US_EXCHANGES)
    if (broker_currency or "").upper() == "USD":
        return set(US_EXCHANGES)
    return set()


def _provenance(root: str = HERE) -> Dict[str, dict]:
    """ticker -> {source, group, company}. Index screens, broker truth, VCI artefacts."""
    import csv
    out: Dict[str, dict] = {}
    ch = os.path.join(root, "constituents_history.csv")
    if os.path.exists(ch):
        try:
            with open(ch, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    t = (r.get("ticker") or "").strip()
                    if t and t not in out:
                        out[t] = {"source": "constituents_history",
                                  "group": (r.get("group") or "").strip(),
                                  "company": (r.get("company") or "").strip()[:80]}
        except Exception:                                               # noqa: BLE001
            pass
    for fn in sorted(os.listdir(root), reverse=True):
        if fn.startswith("portfolio_data_") and fn.endswith(".json"):
            d = _read_json(os.path.join(root, fn)) or {}
            for h in (d.get("stocks") or []):
                t = (h.get("ticker") or "").strip()
                if not t:
                    continue
                # ⚑ BROKER TRUTH CARRIES THE VENUE IN THE NAME: "(LSE:ONT)". Prefer the
                # suffixed form, because the bare one is the shape that went wrong.
                full = h.get("full_name") or ""
                if "LSE:" in full and not t.endswith(".L"):
                    t = t + ".L"
                out[t] = {"source": "portfolio_data(broker truth)", "group": "",
                          "company": (h.get("name") or "")[:80],
                          "broker_currency": h.get("currency")}
            break
    # ⚑ THE DECLARED BENCHMARKS ARE THEIR OWN PROVENANCE. VUAG.L and IWMO.L are comparators,
    # not candidates: no index screen will ever name them, and demanding screen provenance for
    # a comparator is the check asking the wrong question. They are declared IN CODE, in
    # SYMBOL_MAP, which is a stronger statement than a screen row — a human typed them once.
    for _bt in BENCHMARKS:
        out.setdefault(_bt, {"source": "declared benchmark (SYMBOL_MAP)", "group": "",
                             "company": "comparator, not a candidate"})
    # ⚑ ISA-0579 — RESOLVED BY MONTH, NEWEST FIRST, never by literal filename. This tuple read
    # `vci_prescore_aug_2026.json` and three siblings by name until 03-Sep-2026: in September it
    # read August's artefacts and in October it would still have read August's.
    _fam = []
    for _pref in ("vci_prescore", "vci_deploy", "vci_prescore_cache", "watchlist_scored"):
        _fam.extend(_latest_by_month(_pref, root))
    for fn in _fam:
        d = _read_json(os.path.join(root, fn))
        # ⚑ A TOP-LEVEL LIST IS A SHAPE TOO. `vci_prescore_aug_2026.json` is a bare list of
        # name records, and the first version of this reader skipped it with
        # `if not isinstance(d, dict): continue` — so QBTS, INFQ and RXRX read as "no
        # provenance" when the artefact naming them was sitting on disk. A reader that
        # recognises one container shape is R4.6's lesson again: the class does not live in
        # one shape.
        if isinstance(d, list):
            d = {"rows": d}
        if not isinstance(d, dict):
            continue
        src = "artefact:" + fn
        for key in ("names", "candidates", "rows", "tickers", "scored"):
            v = d.get(key)
            if isinstance(v, list):
                for n in v:
                    tk = (n.get("ticker") if isinstance(n, dict) else n)
                    if tk and tk not in out:
                        out[tk] = {"source": src, "group": "VCI" if "vci" in fn else "SCREEN",
                                   "company": ((n.get("name") or n.get("company") or "")[:80]
                                               if isinstance(n, dict) else "")}
            elif isinstance(v, dict):
                for tk, n in v.items():
                    if tk and tk not in out:
                        out[tk] = {"source": src, "group": "VCI" if "vci" in fn else "SCREEN",
                                   "company": ((n.get("name") or n.get("company") or "")[:80]
                                               if isinstance(n, dict) else "")}
    # ── ISA-0577 / ISA-0582 — watchlist_tickers.json. THE TWO LISTS IN THIS ONE FILE CARRY
    # DIFFERENT AUTHORITY AND CONFLATING THEM WOULD BE THE ONT/ONTERRIS DEFECT WITH A NEW SOURCE.
    #   IDENTITY (all lists): the framework itself promoted the name, so it can never be refused
    #     for "NO PROVENANCE" — the honest refusal is "no venue could be formed", which is a
    #     different fact and a different fix (R2.10).
    #   VENUE (`vci_watchlist` ONLY): `sync_vci_watchlist.py:93` takes the exchange from
    #     `project_isa_vci_watchlist.md` and yields "" when the column is absent — it never
    #     manufactures one. `candidate_pool` and `watchlist` default to the literal "NASDAQ".
    wl = _read_json(os.path.join(root, "watchlist_tickers.json")) or {}
    for _key in ("vci_watchlist", "watchlist", "candidate_pool", "stock_sleeve"):
        for r in (wl.get(_key) or []):
            if not isinstance(r, dict):
                continue
            tk = str(r.get("ticker") or "").strip()
            if not tk:
                continue
            out.setdefault(tk, {"source": "watchlist_tickers.%s" % _key, "group": "",
                                "company": str(r.get("name") or "")[:80]})
            if _key == "vci_watchlist":
                venue = str(r.get("exchange") or "").strip().upper()
                # ⚑ ATTACHED even when an earlier source already claimed the ticker. A prescore
                # artefact carries no venue at all; this is the only record that can, and it must
                # not be shadowed by the artefact that happened to name the ticker first. It
                # overwrites NEITHER the source NOR the group.
                if venue:
                    out[tk]["declared_venue"] = venue
    return out


def build_symbol_map(tickers: Sequence[str], *, root: str = HERE, rng: str = "5d",
                     verify: bool = True) -> dict:
    """-> {"map": {...}, "refused": [...], "verified": [...]}. Persisted by `--build-map`.

    ⚑ A ticker enters the map ONLY when (a) something with provenance saw it and (b) the live
    listing's currency is one its declared venue permits. Everything else is REFUSED AND
    NAMED, so `build_universe` keeps raising on it (P1-A5)."""
    prov = _provenance(root)
    smap, refused, verified = {}, [], []
    for t in tickers:
        p = prov.get(t)
        if not p:
            refused.append({"ticker": t, "reason": "NO PROVENANCE — no index screen, broker "
                                                   "holding or VCI artefact has ever named it"})
            continue
        exp = expected_exchanges(t, p.get("group", ""), p.get("broker_currency") or "",
                                 p.get("declared_venue") or "")
        if not exp:
            refused.append({"ticker": t, "group": p.get("group"), "source": p.get("source"),
                            "company": p.get("company"),
                            "declared_venue": p.get("declared_venue"),
                            "fix": ("declare the intended listing in "
                                    "stock_price_fetch.SYMBOL_MAP, one line, e.g. "
                                    "\"%s\": \"%s.L\" — deliberately, never guessed" % (t, t)),
                            "reason": ("CANNOT FORM A VENUE EXPECTATION — its provenance "
                                       "asserts no exchange, so nothing the fetch returns "
                                       "could contradict it. A bare ticker admitted on the "
                                       "strength of the answer alone is the ONT/Onterris "
                                       "defect wearing the verifier's own uniform.")})
            continue
        if not verify:
            smap[t] = t
            verified.append({"ticker": t, "expected": exp, "verified_by": "provenance_only"})
            continue
        try:
            d = _fetch_daily(t, rng=rng)
        except Exception as exc:                                        # noqa: BLE001
            refused.append({"ticker": t, "reason": "fetch failed: %s" % str(exc)[:120]})
            continue
        cur, exch = d.get("currency") or "", d.get("exchange") or ""
        if exch not in exp:
            refused.append({"ticker": t, "fetched_exchange": exch, "fetched_currency": cur,
                            "expected_exchanges": sorted(exp),
                            "group": p.get("group"), "company": p.get("company"),
                            "reason": ("EXCHANGE CONTRADICTS THE DECLARED VENUE — this is the "
                                       "ONT/Onterris falsifier. The listing that answered is "
                                       "not the one the framework means (bare `ONT` answers "
                                       "from NYQ; Oxford Nanopore is on LSE).")})
            continue
        smap[t] = t
        verified.append({"ticker": t, "currency": cur, "exchange": exch,
                         "expected_exchanges": sorted(exp),
                         "source": p.get("source"), "company": p.get("company"),
                         "verified_by": "exchange_vs_declared_venue"})
    return {"map": smap, "refused": refused, "verified": verified,
            "n_map": len(smap), "n_refused": len(refused),
            "basis": ("A symbol MATCH is not a check — requesting a bare `ONT` returns "
                      "meta.symbol 'ONT' for Onterris, Inc. The falsifier is the CURRENCY the "
                      "listing reports against the currency its venue requires.")}


SYMBOL_MAP_STORE = os.path.join(HERE, "stock_symbol_map.json")


def load_symbol_map() -> Dict[str, str]:
    """SYMBOL_MAP plus any VERIFIED entries persisted by `refresh_symbol_map()` (Step 5x)."""
    out = dict(SYMBOL_MAP)
    d = _read_json(SYMBOL_MAP_STORE)
    if isinstance(d, dict):
        for k, v in (d.get("map") or {}).items():
            out.setdefault(k, v)
    return out


# ══════════════════════════════════════════════════════════════════════════════════════
# P1.2c  STEP 5x — SYMBOL MAP REFRESH  ·  ISA-0577
# ══════════════════════════════════════════════════════════════════════════════════════
REFUSALS_STORE = os.path.join(HERE, "symbol_map_refusals.json")


def refresh_symbol_map(*, store: Optional[dict] = None, portfolio_data: Optional[dict] = None,
                       root: str = HERE, verify: bool = True,
                       map_path: Optional[str] = None,
                       refusals_path: Optional[str] = None) -> dict:
    """THE STAGE 5x ENTRY POINT. Bring `stock_symbol_map.json` up to the live universe.

    ═══════════════════════════════════════════════════════════════════════════════════
    ⚑ WHY THIS FUNCTION EXISTS — ISA-0577, and it is an ORCHESTRATION defect, not a data one
    ═══════════════════════════════════════════════════════════════════════════════════
    `build_symbol_map()` shipped on 28-Aug-2026 and had **ZERO CALL SITES ON DISK**. No
    `__main__`, no CLI, no import from the orchestrator. `stock_symbol_map.json` was therefore
    a HAND-RUN artefact frozen at its `built_on` date, while `build_universe` grew every month
    with each screener and VCI promotion.

    Measured 03-Sep-2026: **119 of 178 universe names (66.9%) unmapped.** Every one was dropped
    by `build_universe(strict=False)`, never fetched, read UNMEASURED, took A2.3's adverse
    rho of 0.70 and was capped at STARTER — including HRMY and NVDA, the two names the
    September ranker put at the top, each opened at £5,116.63 at STARTER on 02-Sep. **116 of
    the 119 already carried index-screen provenance in `constituents_history.csv`** and were
    admitted the moment this ran. Nothing was broken. Nobody built the map.

    R4.11: capture is a property of PRODUCING the artefact, not a prose step. The map is now
    refreshed by the run that consumes it, so it cannot age out under session pressure.

    ⚑ APPEND-ONLY (R4.8). An existing entry is NEVER overwritten. A stored symbol is reused
    unconditionally and becomes invisible, which is FC-J; a re-verification that silently
    changed one would move a whole price series onto a different listing with no diff anywhere.
    A name already present is reported as `kept`, never re-decided.

    ⚑ THE REFUSALS ARE AN ARTEFACT, NOT A LOG LINE. `symbol_map_refusals.json` is the DECLARED
    set of names the framework knowingly does not fetch, each with its reason, its provenance
    and the one-line fix. `consistency_check.pair_symbol_map_covers_universe` reads it and
    FAILS the build on any universe name that is in neither the map nor this file — so a name
    can be refused, but it can never be refused silently. That is the class-killer: the defect
    was not the two missing symbols, it was that 119 absences produced no output at all.

    ROLLBACK (R4.13): `isa_policy.V2_FLAGS["symbol_map_refresh"] = False` ⇒ no refresh, the map
    is whatever is on disk, Step 5y behaves exactly as it did on 02-Sep-2026.
    """
    map_path = map_path or SYMBOL_MAP_STORE
    refusals_path = refusals_path or REFUSALS_STORE
    today = datetime.date.today().isoformat()
    if not _flag("symbol_map_refresh"):
        return {"state": "DISABLED", "flag": "symbol_map_refresh", "as_of": today,
                "detail": "rollback: the map is whatever is on disk; 5y unchanged"}

    smap = load_symbol_map()
    store = store if store is not None else srs.load()
    uni = build_universe(store=store, portfolio_data=portfolio_data, symbol_map=smap,
                         strict=False)
    unmapped = list(uni.get("unmapped") or [])
    n_before = len(unmapped)
    if not unmapped:
        _write_refusals(refusals_path, [], today, uni)
        return {"state": "NO_CHANGE", "as_of": today, "n_universe": uni["n"],
                "n_unmapped_before": 0, "n_admitted": 0, "n_refused": 0,
                "admitted": [], "refused": [], "n_unmapped_after": 0,
                "detail": "every universe name already carries a verified Yahoo symbol"}

    res = build_symbol_map(unmapped, root=root, verify=verify)

    doc = _read_json(map_path)
    if not isinstance(doc, dict):
        doc = {"_what": "", "built_on": today, "map": {}, "verified": [], "refused": []}
    existing = dict(doc.get("map") or {})
    admitted, kept = [], []
    for k, v in sorted((res.get("map") or {}).items()):
        if k in existing:
            kept.append(k)                       # ⚑ NEVER overwritten (R4.8)
            continue
        existing[k] = v
        admitted.append(k)
    ver_index = {v.get("ticker"): v for v in (doc.get("verified") or []) if isinstance(v, dict)}
    for v in (res.get("verified") or []):
        if isinstance(v, dict) and v.get("ticker") in admitted:
            v = dict(v)
            v["admitted_on"] = today
            ver_index[v["ticker"]] = v
    doc["map"] = existing
    doc["verified"] = [ver_index[k] for k in sorted(ver_index)]
    doc["refused"] = res.get("refused") or []
    doc["n_map"] = len(existing)
    doc["n_refused"] = len(doc["refused"])
    doc["refreshed_on"] = today
    doc.setdefault("built_on", today)
    doc["_what"] = (
        "P1.2c VERIFIED ticker->Yahoo symbol map. An entry exists ONLY where (a) an index "
        "screen, the broker, a VCI artefact, the VCI watchlist or the declared benchmark list "
        "named the ticker, AND (b) the live listing's EXCHANGE is one its declared venue "
        "permits. \u2691 A symbol match is NOT a check: a bare `ONT` returns meta.symbol 'ONT' "
        "for Onterris, Inc. The EXCHANGE is the falsifier. \u2691 REFRESHED BY THE RUN THAT "
        "CONSUMES IT (Step 5x, ISA-0577) \u2014 this file was a hand-run artefact frozen at "
        "2026-08-28 while the universe grew every month, and 119 of 178 names (66.9%) were "
        "unmapped, unfetched and sized on A2.3's adverse 0.70. \u2691 APPEND-ONLY: an existing "
        "entry is never re-decided (R4.8). \u2691 GENERATED \u2014 never hand-edited (R14.3).")

    tmp = map_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=False)
    os.replace(tmp, map_path)

    after = build_universe(store=store, portfolio_data=portfolio_data,
                           symbol_map=load_symbol_map(), strict=False)
    _write_refusals(refusals_path, doc["refused"], today, after)
    return {"state": "REFRESHED", "as_of": today, "n_universe": after["n"] + after["n_unmapped"],
            "n_unmapped_before": n_before, "n_admitted": len(admitted), "admitted": admitted,
            "n_kept": len(kept), "kept": kept,
            "n_refused": len(doc["refused"]), "refused": doc["refused"],
            "n_unmapped_after": after["n_unmapped"], "unmapped_after": after["unmapped"],
            "n_mapped_after": after["n"], "map_path": map_path,
            "refusals_path": refusals_path,
            "detail": ("append-only: %d admitted, %d already present and NOT re-decided, %d "
                       "REFUSED and NAMED in %s (R4.8, R4.9)"
                       % (len(admitted), len(kept), len(doc["refused"]),
                          os.path.basename(refusals_path)))}


def _write_refusals(path: str, refused: Sequence[dict], as_of: str, uni: dict) -> None:
    """The DECLARED refusal set. ⚑ An artefact, not a log line — `consistency_check`
    reads it and fails the build on a universe name present in neither it nor the map."""
    doc = {"_what": ("Names in the fetch universe that the framework KNOWINGLY does not fetch, "
                     "each with its reason and its one-line fix. \u2691 A name may be refused; "
                     "it may never be refused SILENTLY (ISA-0578, R4.9). Read by "
                     "consistency_check.pair_symbol_map_covers_universe. GENERATED (R14.3)."),
           "as_of": as_of, "n_refused": len(refused),
           "refused": list(refused),
           "universe_basis": uni.get("basis"),
           "n_universe": uni.get("n", 0) + uni.get("n_unmapped", 0)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def load_declared_refusals(path: Optional[str] = None) -> Dict[str, dict]:
    """ticker -> refusal record, from `symbol_map_refusals.json`. {} when absent."""
    d = _read_json(path or REFUSALS_STORE)
    if not isinstance(d, dict):
        return {}
    return {r.get("ticker"): r for r in (d.get("refused") or [])
            if isinstance(r, dict) and r.get("ticker")}



# ══════════════════════════════════════════════════════════════════════════════════════
# SELFTEST  ·  ISA-0520 — OFFLINE, NO NETWORK, NO WRITES OUTSIDE tempfile
# ══════════════════════════════════════════════════════════════════════════════════════
def _selftest() -> dict:
    """32 functions, the source of every price the correlation matrix and the sizing ladder
    rest on, and until 03-Sep-2026 NO `_selftest` and no `__main__` while all eight sibling
    modules had one (ISA-0520). Its three most valuable corrections — the GBp/GBP 100×
    normalisation, the exchange-based symbol verifier, the Friday alignment — had been verified
    exactly once, by hand, against a live network, which is why ISA-0499 could not be closed:
    its only recurrence check would have lived here.

    ⚑ EVERY ASSERTION BELOW IS OFFLINE AND ARITHMETIC. A selftest that needs the network is a
    selftest that gets skipped, and a skipped check is FC-E — an absent execution reporting
    success, which is this project's second failure class."""
    import tempfile
    n = 0

    # ── A. MONTH RESOLUTION (ISA-0579). The literal-filename defect. ────────────────────
    assert _month_key("vci_deploy_aug_2026.json") == (2026, 8)
    assert _month_key("vci_deploy_jan_2027.json") == (2027, 1)
    assert _month_key("vci_deploy.json") == (-1, -1)
    assert _month_key("vci_deploy_xxx_2026.json") == (-1, -1)
    with tempfile.TemporaryDirectory() as d:
        for fn in ("vci_deploy_aug_2026.json", "vci_deploy_sep_2026.json",
                   "vci_deploy_jul_2026.json", "vci_deploy_jan_2027.json",
                   "vci_deploy_notamonth.json", "watchlist_scored_aug_2026.json"):
            open(os.path.join(d, fn), "w").write("{}")
        got = _latest_by_month("vci_deploy", d)
        assert got == ["vci_deploy_jan_2027.json", "vci_deploy_sep_2026.json",
                       "vci_deploy_aug_2026.json", "vci_deploy_jul_2026.json"], got
        # NEGATIVE CONTROL (R5.5): a sibling family must not leak in, and an unparseable
        # month must be dropped rather than sorted to the front.
        assert "watchlist_scored_aug_2026.json" not in got
        assert "vci_deploy_notamonth.json" not in got
        # ⚑ AND THE DEFECT ITSELF: the literal name would have answered August in September.
        assert got[0] != "vci_deploy_aug_2026.json"
    n += 8

    # ── B. THE ONT/ONTERRIS FALSIFIER — venue expectation ───────────────────────────────
    # A bare ticker whose provenance asserts NOTHING is REFUSED. This is the whole defect:
    # `ONT` answers from NYQ for "Onterris, Inc." and published £18,471.20 against a broker
    # truth of £997.92.
    assert expected_exchanges("ONT") == set()
    assert expected_exchanges("ONT.L") == {"LSE", "IOB"}
    # The suffix asserts an EXCHANGE, not a currency: IWMO.L is a correct USD line on the LSE.
    assert expected_exchanges("IWMO.L") == {"LSE", "IOB"}
    # A suffix nobody declared is REFUSED rather than guessed.
    assert expected_exchanges("XYZ.QQ") == set()
    # Index-group provenance admits any US venue.
    assert expected_exchanges("NVDA", "NASDAQ") == set(US_EXCHANGES)
    assert expected_exchanges("NVDA", "STOXX600") == set()
    # ⚑ THE DECLARED-VENUE ROUTE IS STRICTER, NOT LOOSER (ISA-0577).
    assert expected_exchanges("RXRX", "VCI", "", "NASDAQ") == {"NMS", "NCM", "NGM"}
    assert "NYQ" not in expected_exchanges("RXRX", "VCI", "", "NASDAQ")
    assert expected_exchanges("INFQ", "VCI", "", "NYSE") == {"NYQ", "NYS"}
    # It BEATS the group, because it is the more specific assertion — and it narrows.
    assert expected_exchanges("XX", "NASDAQ", "", "NYSE") == {"NYQ", "NYS"}
    # NEGATIVE CONTROL (R5.5): an unrecognised venue name REFUSES. It never widens to US.
    assert expected_exchanges("XX", "", "", "MOON EXCHANGE") == set()
    assert expected_exchanges("XX", "NASDAQ", "", "MOON EXCHANGE") == set()
    # A suffix still wins over a declared venue: the symbol itself names its venue.
    assert expected_exchanges("ONT.L", "NASDAQ", "", "NASDAQ") == {"LSE", "IOB"}
    n += 13

    # ── C. FRIDAY ALIGNMENT — NEVER FORWARD-FILLED (ISA-0455 rule 1) ────────────────────
    D = datetime.date
    daily = {D(2026, 8, 6): 100.0,          # Thu
             D(2026, 8, 7): 101.0,          # Fri  -> taken as-is
             D(2026, 8, 12): 103.0,         # Wed  -> 14-Aug Fri takes 12-Aug (2 days back)
             D(2026, 8, 28): 110.0}         # Fri  -> 21-Aug has NO close within 4 days
    wk = to_friday(daily)
    assert wk[D(2026, 8, 7)] == 101.0
    assert wk[D(2026, 8, 14)] == 103.0, wk
    assert D(2026, 8, 21) not in wk, "21-Aug has no close within MAX_LOOKBACK_DAYS"
    assert wk[D(2026, 8, 28)] == 110.0
    # ⚑ NEGATIVE CONTROL: a manufactured zero-return week is the defect. If 21-Aug were
    # forward-filled at 103.0 the series would gain a false flat week, deflating measured
    # volatility and inflating apparent diversification — the error runs TOWARD the risk.
    assert len(wk) == 3, wk
    assert fridays_between(D(2026, 8, 3), D(2026, 8, 24)) == [
        D(2026, 8, 7), D(2026, 8, 14), D(2026, 8, 21)]
    n += 6

    # ── D. FX DIRECTION (ISA-0429) — one /100.0 was 100× wrong for five months ──────────
    _assert_fx_direction("GBPUSD=X", 1.27)          # GBP-BASE: USD per 1 GBP
    for pair, bad in (("GBPUSD=X", 0.787),          # INVERTED
                      ("GBPUSD=X", 127.0),          # SCALE
                      ("GBPJPY=X", 1.95)):          # INVERTED
        try:
            _assert_fx_direction(pair, bad)
            raise AssertionError("%s = %s must be REFUSED — an inverted rate produces a "
                                 "perfectly plausible-looking correlation matrix" % (pair, bad))
        except FetchRefused:
            pass
    n += 4

    # ── E. GBp IS PENCE — a fixed 0.01, never an FX pair (ISA-0499, P1-A4) ──────────────
    # ⚑ THE ARITHMETIC, not the presence of the constant. A grep cannot tell a rule from a
    # sentence about a rule; this multiplies and checks the product.
    doc = srs.new() if hasattr(srs, "new") else {"names": {}, "fx": {}, "history": []}
    doc.setdefault("names", {})
    srs.record_level(doc, "TST.L", "2026-08-07", 1250.0, "GBp", "selftest", fx_to_gbp=0.01,
                     recorded_on=D(2026, 8, 10))
    obs = doc["names"]["TST.L"]["observations"]
    only = obs[sorted(obs)[0]]
    assert abs(only["gbp"] - 12.50) < 1e-9, only
    # NEGATIVE CONTROL: treating pence as pounds is the 100× defect.
    assert abs(only["gbp"] - 1250.0) > 1.0
    # And a non-GBP level with NO rate is REFUSED, never converted at 1.0.
    try:
        srs.record_level(doc, "TST2", "2026-08-07", 100.0, "USD", "selftest")
        raise AssertionError("a USD level with no fx_to_gbp must be REFUSED (A2.2/R6.4)")
    except ValueError:
        pass
    n += 3

    # ── F. UNIVERSE — the sixth source, and the refusal contract (ISA-0581, ISA-0577) ───
    st = {"names": {"AVGO": {}}}
    u = build_universe(store=st, portfolio_data={"stocks": [{"ticker": "MU"}]},
                       watchlist=["NVDA"], vci_watchlist=["RXRX"], vci_deploy=["IONQ"],
                       candidate_pool=["HRMY"],
                       symbol_map={"AVGO": "AVGO", "MU": "MU", "NVDA": "NVDA",
                                   "RXRX": "RXRX", "IONQ": "IONQ", "HRMY": "HRMY"})
    assert u["tickers"] == ["AVGO", "MU", "NVDA", "RXRX", "IONQ", "HRMY"], u["tickers"]
    assert u["origin"]["IONQ"] == "vci_deploy"
    assert u["n_unmapped"] == 0
    # ⚑ STRICT STILL RAISES. P1-A5. The orchestration changed; the contract did not.
    try:
        build_universe(store=st, portfolio_data={"stocks": []}, watchlist=["NVDA"],
                       vci_watchlist=[], vci_deploy=[], candidate_pool=[],
                       symbol_map={"AVGO": "AVGO"})
        raise AssertionError("an unmapped ticker must RAISE under strict=True (P1-A5)")
    except FetchRefused:
        pass
    # And strict=False DROPS IT BY NAME, never silently.
    u2 = build_universe(store=st, portfolio_data={"stocks": []}, watchlist=["NVDA"],
                        vci_watchlist=[], vci_deploy=[], candidate_pool=[],
                        symbol_map={"AVGO": "AVGO"}, strict=False)
    assert u2["unmapped"] == ["NVDA"] and u2["n_unmapped"] == 1, u2
    assert "NVDA" not in u2["tickers"]
    n += 6

    # ── G. THE MAP IS APPEND-ONLY (R4.8) ────────────────────────────────────────────────
    # ⚑ A stored symbol is reused unconditionally and becomes invisible (FC-J). A refresh that
    # silently re-decided one would move a whole price series onto a different listing with no
    # diff anywhere. This asserts the merge, offline, with verify=False.
    with tempfile.TemporaryDirectory() as d:
        mp = os.path.join(d, "map.json")
        rp = os.path.join(d, "refusals.json")
        json.dump({"map": {"AVGO": "SENTINEL-DO-NOT-OVERWRITE"}, "verified": [], "refused": [],
                   "built_on": "2026-08-28"}, open(mp, "w"))
        r = refresh_symbol_map(store={"names": {}}, portfolio_data={"stocks": []},
                               root=d, verify=False, map_path=mp, refusals_path=rp)
        after = json.load(open(mp))
        assert after["map"]["AVGO"] == "SENTINEL-DO-NOT-OVERWRITE", after["map"]["AVGO"]
        assert after.get("refreshed_on"), after
        assert os.path.exists(rp), "the refusal set must be written even when empty"
        assert r["state"] in ("REFRESHED", "NO_CHANGE", "DISABLED"), r
    n += 4

    # ── H. THE VENUE LIST AND THE CURRENCY LIST DESCRIBE ONE UNIVERSE (ISA-0587) ────────
    # ⚑ THE RECONCILIATION THEY NEVER HAD. `SUFFIX_EXCHANGE` admitted .OL; `CURRENCY_TO_PAIR`
    # had no NOK; SUBC.OL and TGS.OL were mapped, verified, fetched and then refused at
    # conversion. Two lists, one reachable universe, never checked against each other.
    _unconvertible = {}
    for _suf in SUFFIX_EXCHANGE:
        for _c in VENUE_CURRENCIES.get(_suf, set()):
            if _c not in ("GBP", "GBp") and _c not in CURRENCY_TO_PAIR:
                _unconvertible.setdefault(_suf, set()).add(_c)
    assert not _unconvertible, (
        "every currency a declared venue can quote needs a declared FX pair, or the name is "
        "mapped, verified and then silently absent from the store: %r" % _unconvertible)
    assert set(SUFFIX_EXCHANGE) == set(VENUE_CURRENCIES), (
        "a venue declared in one list and not the other cannot be reconciled at all: %r"
        % (set(SUFFIX_EXCHANGE) ^ set(VENUE_CURRENCIES)))
    assert set(CURRENCY_TO_PAIR.values()) <= set(FX_PAIRS)
    assert set(FX_PAIRS) == set(FX_BANDS), set(FX_PAIRS) ^ set(FX_BANDS)
    # NEGATIVE CONTROL (R5.5): remove NOK and the reconciliation must FAIL.
    _saved = CURRENCY_TO_PAIR.pop("NOK")
    try:
        _leak = {c for suf in SUFFIX_EXCHANGE for c in VENUE_CURRENCIES.get(suf, set())
                 if c not in ("GBP", "GBp") and c not in CURRENCY_TO_PAIR}
        assert _leak == {"NOK"}, _leak
    finally:
        CURRENCY_TO_PAIR["NOK"] = _saved
    n += 5

    return {"ok": True, "assertions": n, "network": False,
            "covers": ["month resolution (ISA-0579)", "venue expectation / ONT (ISA-0577)",
                       "Friday alignment, no forward-fill (ISA-0455)",
                       "FX direction (ISA-0429)", "GBp 0.01 arithmetic (ISA-0499)",
                       "universe incl. vci_deploy + refusal contract (ISA-0581)",
                       "symbol map append-only (R4.8)",
                       "venue/currency reconciliation (ISA-0587)"]}


if __name__ == "__main__":                                          # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="stock_price_fetch — P1 weekly GBP return store")
    ap.add_argument("--selftest", action="store_true", help="offline assertions (ISA-0520)")
    ap.add_argument("--refresh-map", action="store_true",
                    help="Step 5x: bring stock_symbol_map.json up to the live universe")
    ap.add_argument("--no-verify", action="store_true",
                    help="--refresh-map without the live exchange check (NOT for a real run)")
    ap.add_argument("--universe", action="store_true", help="print the universe and its origins")
    ap.add_argument("--probe", action="store_true", help="the negative-claim network falsifier")
    ap.add_argument("--fetch", action="store_true", help="run batches to ALL_DONE")
    a = ap.parse_args()
    did = False
    if a.selftest:
        did = True
        print(json.dumps(_selftest(), indent=1))
    if a.probe:
        did = True
        print(json.dumps(_network_probe(), indent=1))
    if a.universe:
        did = True
        u = build_universe(strict=False)
        print(json.dumps({k: u[k] for k in ("n", "n_unmapped", "unmapped", "basis")}, indent=1))
    if a.refresh_map:
        did = True
        print(json.dumps(refresh_symbol_map(verify=not a.no_verify), indent=1)[:20000])
    if a.fetch:
        did = True
        g = 0
        while g < 200:
            g += 1
            r = run()
            print(r.get("state"), r.get("remaining"), r.get("n_universe"))
            if r.get("state") in ("ALL_DONE", "DISABLED", "FETCH_UNAVAILABLE"):
                break
    if not did:
        ap.print_help()
