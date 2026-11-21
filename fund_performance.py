"""
fund_performance.py -- THE GOLDEN SOURCE for fund trailing performance.

BuildSpec: ISA_BuildSpec_FundPerformance_GoldenSource_Aug2026.md
Standard : feedback_isa_engineering_standard.md (all five rules)

Replaces the X-Ray as the SOURCE of per-fund returns. The X-Ray is demoted to a
CORROBORATOR (invariant I6) because its per-fund rows are dated a month before
the report header that carries them -- the D1 defect.

Every figure returned is a Metric (value + as_of + source) or a Missing(reason).
Nothing here ever returns a bare float for a decision-grade number.
"""
from __future__ import annotations
import csv, datetime as dt, json, os, sys

from isa_metric import Metric, Missing, is_present, value_or, as_dict

# ── ISA-0940 (20-Nov-2026): LIVE-PATH EXECUTION LEDGER (framework_integrity) ─────────────
# ONE LINE at the head of the declared capability producer. `_mark` is in-memory and a NO-OP when
# isa_policy.V2_FLAGS["execution_ledger"] is False; it never raises into the caller.
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

SCHEMA_VERSION = 1
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE = os.path.join(SCRIPT_DIR, "fund_universe.json")
CACHE_DIR = os.path.join(SCRIPT_DIR, "nav_cache")
HISTORY_CSV = os.path.join(SCRIPT_DIR, "fund_performance_history.csv")

WINDOWS = {"1m": 1/12, "3m": 0.25, "6m": 0.5, "1y": 1.0, "3y": 3.0, "5y": 5.0,
           # 10y added 05-Aug-2026: a single window is a bet on a start date. Scottish
           # Mortgage reads ~0.2% over 5y, ~22% over 3y and ~16.7% over 10y — the same
           # fund, three answers, decided entirely by whether the 2022 -45.7% year is
           # inside the window. The anchor rule now needs every window it can get.
           "10y": 10.0}
PRICE_MATCH_TOL_PCT = 1.0      # I1
MAX_PRICING_LAG_DAYS = 3       # I1: OEICs are struck T-1 vs the broker valuation
                               # date; ETFs/ITs T-0. Empirically every holding
                               # reconciles to 0.00% at the right lag, so a
                               # >1% diff at the BEST lag is a genuine fault.
XRAY_AGREE_TOL_PP  = 2.0       # I6
MIN_COVERAGE = 0.98            # need 98% of the window actually covered by NAV history


# ---------------------------------------------------------------- universe
def load_universe(path=UNIVERSE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["funds"]


def isin_check_digit(body: str) -> str:
    """Luhn over the alphanumeric-expanded body. Used to VALIDATE a KID-sourced
    ISIN -- never to invent one (domicile is not derivable from a SEDOL)."""
    conv = "".join(str(int(c, 36)) if c.isalpha() else c for c in body.upper())
    total = 0
    for i, ch in enumerate(reversed(conv)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def isin_is_valid(isin: str) -> bool:
    return (isinstance(isin, str) and len(isin) == 12
            and isin_check_digit(isin[:11]) == isin[11])


# ---------------------------------------------------------------- NAV history
def _cache_path(symbol): return os.path.join(CACHE_DIR, f"{symbol.replace('/','_')}.csv")


def _read_cache(symbol):
    p = _cache_path(symbol)
    if not os.path.exists(p):
        return None
    out = []
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out.append((dt.date.fromisoformat(row["date"]), float(row["close"])))
            except (ValueError, KeyError):
                continue
    return out or None


def _write_cache(symbol, series, *, basis=None, depth_held_from=None, provider="yfinance"):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(symbol), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["date", "close"])
        for d, c in series:
            w.writerow([d.isoformat(), f"{c:.6f}"])
    if basis is not None:
        _write_provenance(symbol, basis, series, depth_held_from=depth_held_from, provider=provider)


# ---------------------------------------------------------------- price basis + provenance (ISA-0901 / ISA-0874)
# ⚑ ISA-0901 (20-Nov-2026): THE PRICE BASIS IS DECLARED PER SHARE CLASS, never one setting for every fund.
#   Every series used to be fetched with auto_adjust=True. An ACCUMULATION class already carries its income in
#   the price, and Yahoo ALSO reports the notional accumulation distribution as a dividend (Artemis UK/European
#   I Acc: 5 events since 2021), so auto-adjusting counted the income twice - ~3-3.5pp/yr of return that does
#   not exist (raw close = the X-Ray to 2dp on 1Y/3Y/5Y for all 11 Yahoo-fed funds; the double-counted series
#   moved two OTHER funds' bands through the sleeve-relative P75 stretch and fired T4 on Artemis European).
#   acc -> the raw close; inc -> the dividend-reinvested (adjusted) close. A class that declares neither is
#   REFUSED (PriceBasisUndeclared) - an uninformed choice is refused, not guessed (R4.8).
PRICE_BASES = {"acc": "raw_close", "inc": "total_return_adjusted"}
PRICE_BASIS_STATES = ("BASIS_RECORDED", "PROVENANCE_UNRECORDED", "BASIS_MISMATCH", "PRICE_BASIS_UNDECLARED",
                      "DECLARED_LOCAL_SERIES")
REQUIREMENTS_TXT = os.path.join(SCRIPT_DIR, "requirements.txt")


class PriceBasisUndeclared(RuntimeError):
    """A share class with no declared acc_or_inc: its price basis cannot be chosen, so it is refused (R4.8)."""


def price_basis_for(u):
    a = str((u or {}).get("acc_or_inc") or "").strip().lower()
    if a not in PRICE_BASES:
        raise PriceBasisUndeclared("share class %r declares no acc_or_inc (acc|inc): price basis refused, not guessed"
                                   % ((u or {}).get("share_class") or (u or {}).get("name") or (u or {}).get("isin")))
    return PRICE_BASES[a]


def _prov_path(symbol): return os.path.join(CACHE_DIR, f"{symbol.replace('/','_')}.prov.json")


def _provider_version():
    try:
        from importlib import metadata as _md
        return _md.version("yfinance")
    except Exception:                                                   # noqa: BLE001
        return None


def _pinned_version(path=None):
    try:
        for line in open(path or REQUIREMENTS_TXT, encoding="utf-8"):
            s = line.strip()
            if s.lower().startswith("yfinance=="):
                return s.split("==", 1)[1].strip()
    except OSError:
        return None
    return None


def _write_provenance(symbol, basis, series, depth_held_from=None, provider="yfinance"):
    """ISA-0874 leg 3: every series a writer produces records WHAT made it - provider, provider version (and
    the pin it should have been), price basis, when, and the depth it was held to. A sidecar, so every existing
    reader of the CSV is untouched."""
    pv, pin = _provider_version(), _pinned_version()
    doc = {"schema_version": 1, "symbol": symbol, "provider": provider, "provider_version": pv,
           "pinned_version": pin,
           "version_state": ("UNKNOWN" if not pv or not pin else ("MATCHES_PIN" if pv == pin else "DIFFERS_FROM_PIN")),
           "basis": basis, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
           "first": series[0][0].isoformat() if series else None, "last": series[-1][0].isoformat() if series else None,
           "points": len(series or []),
           "depth_held_from": depth_held_from.isoformat() if hasattr(depth_held_from, "isoformat") else depth_held_from}
    with open(_prov_path(symbol), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return doc


def read_provenance(symbol):
    try:
        return json.load(open(_prov_path(symbol), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def provenance_state(symbol, declared_basis):
    """-> {state, declared_basis, recorded_basis, provider_version, pinned_version, version_state, fetched_at,
    depth_held_from}. A cache with no sidecar is PROVENANCE_UNRECORDED - never assumed to be either basis."""
    pr = read_provenance(symbol) if symbol else None
    if not pr:
        return {"state": "PROVENANCE_UNRECORDED", "declared_basis": declared_basis, "recorded_basis": None,
                "why": "no provenance sidecar - the cache predates ISA-0874 (fetched with auto_adjust=True under an "
                       "unrecorded yfinance version)"}
    st = "BASIS_RECORDED" if pr.get("basis") == declared_basis else "BASIS_MISMATCH"
    return {"state": st, "declared_basis": declared_basis, "recorded_basis": pr.get("basis"),
            "provider": pr.get("provider"), "provider_version": pr.get("provider_version"),
            "pinned_version": pr.get("pinned_version"), "version_state": pr.get("version_state"),
            "fetched_at": pr.get("fetched_at"), "depth_held_from": pr.get("depth_held_from")}


def nav_series_provenance(sedol, u):
    """The door's provenance view of one fund (typed, never blank)."""
    ls = (u or {}).get("local_series")
    if ls:
        return {"state": "DECLARED_LOCAL_SERIES", "declared_basis": ls.get("basis"), "recorded_basis": ls.get("basis"),
                "why": "declared local_series with its own factsheet reconciliation (load_local_series)"}
    try:
        basis = price_basis_for(u)
    except PriceBasisUndeclared as e:
        return {"state": "PRICE_BASIS_UNDECLARED", "declared_basis": None, "recorded_basis": None, "why": str(e)}
    return provenance_state((u or {}).get("yf_symbol") or (u or {}).get("isin"), basis)


def _supersede_copy(symbol, recorded_basis):
    """Keep the bytes a basis migration replaces (rollback / audit). nav_cache/_superseded/ is a SUBDIRECTORY so
    no `nav_cache/*.csv` reader can pick it up."""
    import shutil
    src_p = _cache_path(symbol)
    if not os.path.exists(src_p):
        return None
    d = os.path.join(CACHE_DIR, "_superseded")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "%s.%s.%s.csv" % (symbol.replace("/", "_"), recorded_basis or "unrecorded",
                                            dt.date.today().strftime("%Y%m%d")))
    if not os.path.exists(dst):
        shutil.copy2(src_p, dst)
    return dst


def _scale_for(u):
    """GBp -> GBP. Unit confusion is its own defect class: SMT priced 1330.50
    (GBp) against a broker 13.3050 (GBP) read as a 9900% mismatch."""
    return 100.0 if (u or {}).get("price_unit") == "GBp" else 1.0


def fetch_nav_history(symbol, period="max", use_cache=True, refresh=False, scale=1.0, *, basis):
    """[(date, close)] ascending, dividend-reinvested. Cached so the 45s sandbox
    ceiling cannot half-complete a 12-fund run.

    ⚑ period="max", NOT "10y" (fixed 06-Aug-2026). A fetch exactly as long as the longest window
    can never cover that window: yfinance measures 10y back from TODAY, the returns are measured
    back from a MONTH-END as_of, and the few days between them are enough. Every cache begins
    2016-08-05 while the 10-year window from 31-Jul-2026 needs 01-Aug-2016 — short by four days.
    The consequence was that the 10y window, declared in WINDOWS and load-bearing in the
    return-adequacy median, produced NOTHING for any of the twelve funds on any run since it was
    added, and read as "not applicable" rather than "never fetched". `window_coverage()` below
    now makes that class of silence impossible to repeat."""
    if basis not in PRICE_BASES.values():
        raise PriceBasisUndeclared("fetch_nav_history(%r): basis %r is not one of %s (ISA-0901: declared per class)"
                                   % (symbol, basis, sorted(PRICE_BASES.values())))
    if not symbol:
        return []
    if use_cache and not refresh:
        c = _read_cache(symbol)
        if c:
            return [(d, v / scale) for d, v in c] if scale != 1.0 else c
    # ⚑ ISA-0869 (18-Oct-2026): a refresh that fails, returns nothing, or returns a series that does not
    #   ADVANCE and COVER the cache leaves the cache in place and returns it (typed STALE by
    #   refresh_nav_cache / nav_freshness) - it used to return [] (a fund silently without NAV).
    cached = _read_cache(symbol) if use_cache else None

    def _cached_or_empty():
        if not cached:
            return []
        return [(d, v / scale) for d, v in cached] if scale != 1.0 else cached
    series = _yf_history(symbol, period, basis=basis)
    if series is None:
        return _cached_or_empty()
    series = _hold_depth(series, cached)
    if not _series_supersedes(series, cached):
        return _cached_or_empty()
    if series and use_cache:
        _write_cache(symbol, series, basis=basis, depth_held_from=(cached[0][0] if cached else None))
    if scale != 1.0:
        series = [(d, c / scale) for d, c in series]
    return series


def _yf_history(symbol, period="max", *, basis):
    """[(date, close)] from yfinance at the DECLARED price basis, or None on any failure.
    basis 'total_return_adjusted' = auto_adjust=True (dividends reinvested; an INCOME class);
    basis 'raw_close' = auto_adjust=False Close (an ACCUMULATION class: income is already in the price -
    auto-adjusting it counts the income twice, ISA-0901).
    A FULL history every time, never an increment: auto_adjust rescales the past at each
    distribution, so an increment spliced onto an older adjusted cache would drop the dividend."""
    if basis not in PRICE_BASES.values():
        raise PriceBasisUndeclared("_yf_history(%r): undeclared basis %r" % (symbol, basis))
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        h = yf.Ticker(symbol).history(period=period, auto_adjust=(basis == "total_return_adjusted"))["Close"]
    except Exception:                                                   # noqa: BLE001
        return None
    series = []
    for idx, val in h.items():
        try:
            d = idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])
            if val == val and val > 0:          # NaN-safe
                series.append((d, float(val)))
        except Exception:                                               # noqa: BLE001
            continue
    series.sort()
    return series or None


def _hold_depth(new, cached):
    """A refresh EXTENDS a cached series; it never changes where it starts. Measured 18-Oct-2026: yfinance
    1.7.0 `period=max` returns history back to 2013 (SMT to the 1960s) where the caches (1.5.1) begin
    2016-08-05 - and the mandate-drift noise floors (benchmark_registry, first-half vs second-half of a
    fund's OWN history) were measured on the 2016+ depth: a deeper history moved the Vanguard Japan control
    from |dbeta| 0.057 to 0.090, above its 0.06 floor. Changing history depth is a methodology decision
    (re-measure the floors), not a freshness fix, so the depth is held (ISA-0869; follow-on registered)."""
    if not new or not cached:
        return new
    start = cached[0][0]
    return [p for p in new if p[0] >= start]


def _series_supersedes(new, cached, start_slack_days=7):
    """A fetched series may replace the cache only if it ADVANCES (newest date later) and still
    COVERS the cache's start - a truncated provider response must never shorten history."""
    if not new:
        return False
    if not cached:
        return True
    return new[-1][0] > cached[-1][0] and new[0][0] <= cached[0][0] + dt.timedelta(days=start_slack_days)


# ---------------------------------------------------------------- NAV freshness (ISA-0869)
NAV_STATE = os.path.join(SCRIPT_DIR, "nav_cache_state.json")
NAV_STALE_BDAYS = 5            # refresh when the newest NAV is older than run date - 5 business days
NAV_FRESH_MAX_DAYS = 10        # the consumer bar: a held fund's NAV must be within 10 days (pair)
NAV_REFRESH_HOURS = 18         # an attempt inside 18 h is not repeated (re-run protocol)
FT_HIST_URL = ("https://markets.ft.com/data/equities/ajax/get-historical-prices"
               "?startDate={start}&endDate={end}&symbol={xid}")
FT_JUNCTION_TOL_PCT = 0.05     # FT vs the cached series on overlapping dates
NAV_STATES = ("FRESH", "STALE_CACHE", "SOURCE_FAILED", "SOURCE_DISAGREES", "MANUAL_SERIES", "NO_SERIES",
              "PRICE_BASIS_UNDECLARED")


def _bdays_after(d0, d1):
    """Weekdays in (d0, d1]."""
    if d0 is None or d1 is None or d1 <= d0:
        return 0
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def nav_needs_refresh(newest, as_of):
    return newest is None or _bdays_after(newest, as_of) > NAV_STALE_BDAYS


class FtLayoutError(RuntimeError):
    """The FT historical-prices payload no longer has the shape this parser was written against."""


def parse_ft_history(raw):
    """[(date, close)] ascending from the FT `get-historical-prices` JSON (rows of an HTML table:
    long date, open, high, low, close, volume). RAISES on layout change, never returns a guess."""
    import re
    try:
        html = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)["html"]
    except Exception as e:                                              # noqa: BLE001
        raise FtLayoutError("FT payload is not the {html: ...} JSON: %s" % e)
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(cells) < 5:
            raise FtLayoutError("FT row has %d cells, expected >= 5" % len(cells))
        m = re.search(r'mod-ui-hide-small-below">([^<]+)<', cells[0])
        if not m:
            raise FtLayoutError("FT row date cell has no long-form date")
        try:
            d = dt.datetime.strptime(m.group(1).strip(), "%A, %B %d, %Y").date()
            c = float(re.sub(r"<[^>]+>", "", cells[4]).replace(",", "").strip())
        except ValueError as e:
            raise FtLayoutError("FT row unparseable: %s" % e)
        if c > 0:
            out.append((d, c))
    return sorted(set(out))


def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ISA research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def merge_ft_series(cached, ft):
    """Cached history (before FT's first date) + FT daily closes. Returns (series, check) where
    check names the junction evidence; a disagreement on overlapping dates returns (None, check)."""
    cached = cached or []
    ftd = dict(ft)
    overlap = [(d, v, ftd[d]) for d, v in cached if d in ftd]
    bad = [(d.isoformat(), v, f) for d, v, f in overlap if abs(f / v - 1.0) * 100 > FT_JUNCTION_TOL_PCT]
    check = {"overlap_points": len(overlap), "disagreements": bad[:5], "tolerance_pct": FT_JUNCTION_TOL_PCT}
    if bad:
        return None, check
    first = ft[0][0]
    merged = [(d, v) for d, v in cached if d < first] + list(ft)
    return merged, check


def _load_nav_state(path=None):
    try:
        with open(path or NAV_STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                                   # noqa: BLE001
        return {}


def refresh_nav_cache(as_of, budget_s=25.0, universe=None, *, get=None, yf_history=None,
                      now=None, state_path=None, cache_dir=None):
    """ISA-0869 producer (pre-run Step 6.04). Brings every fund's NAV cache up to the run date:
      - yfinance funds: FULL re-fetch when the newest NAV is > NAV_STALE_BDAYS business days old;
        replaces the cache only if it advances and covers it (_series_supersedes);
      - funds declaring `ft_xid` (no yfinance feed, e.g. Polar IE00B42W4J83): FT daily closes
        merged onto the declared local_series cache, junction-checked (merge_ft_series);
      - a declared manual local_series without a feed: MANUAL_SERIES (never refreshed here).
    Every fund leaves with a typed state (NAV_STATES) + newest NAV date + why; nothing is blank.
    Writes nav_cache_state.json. Never raises for a source failure."""
    import time as _t
    global CACHE_DIR
    t0 = _t.time()
    now = now or dt.datetime.utcnow()
    get = get or _http_get
    yf_history = yf_history or _yf_history
    universe = universe or load_universe()
    funds = universe.get("funds", universe)
    prev = _load_nav_state(state_path).get("funds", {})
    _cd0 = CACHE_DIR
    if cache_dir:
        CACHE_DIR = cache_dir
    out = {}
    try:
        for sedol, u in funds.items():
            if str(sedol).startswith("_") or not isinstance(u, dict):
                continue
            ls = u.get("local_series") or {}
            sym = u.get("yf_symbol") or u.get("isin")
            route = "FT" if u.get("ft_xid") else ("MANUAL" if ls else "YF")
            key = (ls.get("cache") if route != "YF" else None)
            if route == "YF":
                cached = _read_cache(sym) if sym else None
            else:
                cp = os.path.join(os.path.dirname(CACHE_DIR), key) if key else None
                cached = _read_csv_series(cp) if cp else None
            newest = cached[-1][0] if cached else None
            rec = {"sedol": sedol, "name": u.get("name"), "route": route, "symbol": sym,
                   "ft_xid": u.get("ft_xid"), "newest_before": newest.isoformat() if newest else None}
            p = prev.get(sedol) or {}
            recent = False
            try:
                recent = (now - dt.datetime.fromisoformat(p.get("attempted_at", "1970-01-01T00:00:00"))
                          ).total_seconds() < NAV_REFRESH_HOURS * 3600 and p.get("as_of_run") == as_of.isoformat()
            except ValueError:
                recent = False
            fetched = False
            basis, migrate = None, False
            if route == "YF":
                try:
                    basis = price_basis_for(u)
                except PriceBasisUndeclared as _pbe:
                    basis = None
                    rec["why_basis"] = str(_pbe)
                rec["price_basis"] = basis
                if basis is not None and cached:
                    _pst = provenance_state(sym, basis)
                    rec["provenance_before"] = _pst.get("state")
                    # ⚑ ISA-0901: a cache whose recorded basis is absent or different is MIGRATED here, by the
                    #   cache's declared writer - a fresh-but-wrong-basis cache is not FRESH.
                    migrate = _pst.get("state") != "BASIS_RECORDED"
            if route == "MANUAL":
                rec["state"], rec["why"] = "MANUAL_SERIES", "declared manual local_series; no feed"
            elif route == "YF" and basis is None:
                rec["state"], rec["why"] = "PRICE_BASIS_UNDECLARED", rec.get("why_basis")
            elif migrate and not (recent and p.get("state") in ("SOURCE_FAILED", "STALE_CACHE")
                                  and p.get("basis_migration_failed")):
                if _t.time() - t0 > budget_s:
                    rec["state"], rec["why"] = "STALE_CACHE", ("refresh budget %.0f s spent before this fund's basis "
                                                               "migration to %s - cache kept at its recorded basis"
                                                               % (budget_s, basis))
                else:
                    fetched = True
                    rec["attempted_at"] = now.isoformat(timespec="seconds")
                    new = yf_history(sym, basis=basis) if sym else None
                    new = _hold_depth(new, cached) if new else new
                    covers = (bool(new) and new[0][0] <= cached[0][0] + dt.timedelta(days=7)
                              and new[-1][0] >= cached[-1][0])
                    if not covers:
                        rec["state"] = "STALE_CACHE" if new else "SOURCE_FAILED"
                        rec["why"] = ("basis migration to %s REFUSED: %s - the existing cache is kept, typed by its "
                                      "provenance" % (basis, "no series returned" if not new else
                                                      "the new series (%s..%s) does not cover the cache (%s..%s)"
                                                      % (new[0][0], new[-1][0], cached[0][0], cached[-1][0])))
                        rec["basis_migration_failed"] = True
                    else:
                        _prev = read_provenance(sym)
                        rec["basis_migration"] = {"from": (_prev or {}).get("basis"), "to": basis,
                                                  "superseded_copy": _supersede_copy(sym, (_prev or {}).get("basis")),
                                                  "depth_held_from": cached[0][0].isoformat(), "points": len(new)}
                        _write_cache(sym, new, basis=basis, depth_held_from=cached[0][0])
                        newest = new[-1][0]
                        rec["state"] = "FRESH" if not nav_needs_refresh(newest, as_of) else "STALE_CACHE"
                        rec["why"] = "re-fetched %d points at the declared basis %s (migration)" % (len(new), basis)
            elif not nav_needs_refresh(newest, as_of):
                rec["state"], rec["why"] = "FRESH", "cache within %d business days of the run date" % NAV_STALE_BDAYS
            elif recent and p.get("state") in ("SOURCE_FAILED", "SOURCE_DISAGREES", "STALE_CACHE"):
                rec["state"], rec["why"] = p["state"], "attempted at %s (< %d h ago): %s" % (
                    p.get("attempted_at"), NAV_REFRESH_HOURS, p.get("why"))
                rec["attempted_at"] = p.get("attempted_at")
            elif _t.time() - t0 > budget_s:
                rec["state"], rec["why"] = "STALE_CACHE", "refresh budget %.0f s spent before this fund" % budget_s
            else:
                fetched = True
                rec["attempted_at"] = now.isoformat(timespec="seconds")
                if route == "YF":
                    new = yf_history(sym, basis=basis) if sym else None
                    new = _hold_depth(new, cached) if new else new
                    if new is None:
                        rec["state"], rec["why"] = "SOURCE_FAILED", "yfinance returned nothing for %s" % sym
                    elif not _series_supersedes(new, cached):
                        rec["state"], rec["why"] = "STALE_CACHE", (
                            "yfinance series (newest %s) does not advance/cover the cache" % new[-1][0])
                    else:
                        _write_cache(sym, new, basis=basis, depth_held_from=(cached[0][0] if cached else None))
                        newest = new[-1][0]
                        rec["state"] = "FRESH" if not nav_needs_refresh(newest, as_of) else "STALE_CACHE"
                        rec["why"] = "re-fetched %d points" % len(new)
                else:   # FT
                    start = (newest - dt.timedelta(days=45)) if newest else as_of - dt.timedelta(days=365 * 5)
                    try:
                        ft = parse_ft_history(get(FT_HIST_URL.format(
                            start=start.strftime("%Y/%m/%d"), end=as_of.strftime("%Y/%m/%d"), xid=u["ft_xid"])))
                    except Exception as e:                              # noqa: BLE001
                        ft, rec["why"] = None, "FT fetch/parse failed: %s: %s" % (type(e).__name__, str(e)[:160])
                        rec["state"] = "SOURCE_FAILED"
                    if ft:
                        merged, chk = merge_ft_series(cached, ft)
                        rec["junction"] = chk
                        if merged is None:
                            rec["state"], rec["why"] = "SOURCE_DISAGREES", "FT disagrees with the cached series on overlapping dates"
                        elif not _series_supersedes(merged, cached):
                            rec["state"], rec["why"] = "STALE_CACHE", "FT series does not advance the cache"
                        else:
                            _write_csv_series(cp, merged)
                            newest = merged[-1][0]
                            rec["state"] = "FRESH" if not nav_needs_refresh(newest, as_of) else "STALE_CACHE"
                            rec["why"] = "FT merged %d daily closes" % len(ft)
                    elif ft is not None and rec.get("state") != "SOURCE_FAILED":
                        rec["state"], rec["why"] = "SOURCE_FAILED", "FT returned no rows"
            if not newest and rec["state"] in ("FRESH", "STALE_CACHE"):
                rec["state"] = "NO_SERIES"
            rec["newest"] = newest.isoformat() if newest else None
            rec["age_days"] = (as_of - newest).days if newest else None
            if route == "YF" and basis is not None and sym:
                rec["provenance"] = provenance_state(sym, basis)
            rec["fetched_this_call"] = fetched
            rec["as_of_run"] = as_of.isoformat()
            if not fetched and rec["state"] != "STALE_CACHE" and p.get("attempted_at") and "attempted_at" not in rec:
                rec["attempted_at"] = p.get("attempted_at")
            out[sedol] = rec
    finally:
        CACHE_DIR = _cd0
    doc = {"schema_version": 1, "as_of_run": as_of.isoformat(), "written_at": now.isoformat(timespec="seconds"),
           "basis": "ISA-0869: typed NAV freshness per fund; FRESH = newest NAV within %d business days of the run date"
                    % NAV_STALE_BDAYS,
           "counts": {k: sum(1 for r in out.values() if r["state"] == k) for k in NAV_STATES},
           "funds": out, "secs": round(_t.time() - t0, 1)}
    try:
        with open(state_path or NAV_STATE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    except OSError:
        pass
    return doc


def _read_csv_series(path):
    if not path or not os.path.exists(path):
        return None
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out.append((dt.date.fromisoformat(r["date"]), float(r["close"])))
            except (ValueError, KeyError):
                continue
    return sorted(out) or None


def _write_csv_series(path, series):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["date", "close"])
        for d, c in series:
            w.writerow([d.isoformat(), ("%.6f" % c).rstrip("0").rstrip(".")])
    os.replace(tmp, path)


def nav_freshness(sedol, u, as_of, series=None, state=None):
    """{state, newest, age_days, why} for one fund - the consumer view (never blank)."""
    st = ((state if state is not None else _load_nav_state()).get("funds") or {}).get(sedol) or {}
    if series is None:
        try:
            series = nav_series_for(sedol, u)
        except Exception:                                               # noqa: BLE001
            series = []
    newest = series[-1][0] if series else None
    age = (as_of - newest).days if newest else None
    if not newest:
        s = "NO_SERIES"
    elif age is not None and age <= NAV_FRESH_MAX_DAYS:
        s = "FRESH"
    else:
        s = st.get("state") if st.get("state") in ("SOURCE_FAILED", "SOURCE_DISAGREES", "MANUAL_SERIES") else "STALE_CACHE"
    return {"state": s, "newest": newest.isoformat() if newest else None, "age_days": age,
            "producer_state": st.get("state"), "why": st.get("why") or ("no refresh record" if not st else None)}


# ---------------------------------------------------------------- local series (ISA-0307)
class LocalSeriesError(RuntimeError):
    """A declared local series that cannot be read. RAISED, never silently empty: the whole
    defect this route exists to fix was a NAV series that was on disk, correct, reconciled, and
    read by nothing — while the study reported `warnings: []`."""


def load_local_series(u, base=None):
    """[(date, close)] for a fund whose NAV has no feed and is supplied manually.

    ⚑ WHY THIS EXISTS. `yfinance` rejects the valid ISIN IE00B42W4J83, so Polar Capital Global
    Tech had no NAV series and every consumer recorded 'no NAV series — factsheet-only fund'.
    Raj supplied a reconciling monthly series on 06-Aug-2026, it was cached, and for six days
    nothing read it: 7.6% of the portfolio carried no measured beta and M* coverage read 92.4%
    for want of a file that was already on disk. Built, believed live, never reached.

    ⚑ THE ROUTE IS DECLARED, NOT DISCOVERED. It fires only where `fund_universe` carries a
    `local_series` block naming the file, sheet and columns, and it re-asserts that block's own
    factsheet reconciliation before returning anything (R4.10 — an artefact asserts its own
    fitness for the use it will be put to)."""
    ls = (u or {}).get("local_series")
    if not ls:
        return []
    base = base or os.path.dirname(os.path.abspath(__file__))
    cache = os.path.join(base, ls.get("cache", ""))
    series = []
    if ls.get("cache") and os.path.exists(cache):
        with open(cache) as fh:
            for r in csv.DictReader(fh):
                series.append((dt.date.fromisoformat(r["date"]), float(r["close"])))
    else:
        path = os.path.normpath(os.path.join(base, ls["path"]))
        if not os.path.exists(path):
            raise LocalSeriesError(f"declared local_series not found: {path}")
        import openpyxl
        ws = openpyxl.load_workbook(path, data_only=True)[ls["sheet"]]
        hdr = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        di, vi = hdr.index(ls["date_col"]), hdr.index(ls["value_col"])
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[di] is None or row[vi] is None:
                continue
            d = row[di].date() if hasattr(row[di], "date") else row[di]
            series.append((d, float(row[vi])))
        if ls.get("cache"):
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["date", "close"])
                w.writerows([(d.isoformat(), v) for d, v in sorted(series)])
    series.sort()
    if not series:
        raise LocalSeriesError("declared local_series produced no rows")
    _assert_local_reconciliation(series, ls)
    return series


def _assert_local_reconciliation(series, ls):
    """The block declares deltas against a named factsheet. Re-derive them and RAISE on
    disagreement — two independent derivations must agree, with a stated tolerance (R5.2)."""
    rec = ls.get("reconciliation") or {}
    tol = rec.get("tolerance_pp")
    deltas = rec.get("deltas_pp") or {}
    if tol is None or not deltas:
        raise LocalSeriesError("local_series carries no reconciliation — a manually supplied NAV "
                               "series may not be used unmeasured (R6.3)")
    for k, v in deltas.items():
        if abs(float(v)) > float(tol):
            raise LocalSeriesError(
                f"local_series reconciliation {k} is {v}pp, outside the declared {tol}pp tolerance")
    L = {}
    for d, v in series:
        L[(d.year, d.month)] = v
    if len(L) < 30:
        raise LocalSeriesError(f"local_series has {len(L)} months, below the 30-month minimum")


def _held_series(u, refresh=False):
    if (u or {}).get("local_series"):
        s = load_local_series(u)
        if s:
            return s
    sym = (u or {}).get("yf_symbol") or (u or {}).get("isin")
    return fetch_nav_history(sym, use_cache=True, refresh=refresh, scale=_scale_for(u), basis=price_basis_for(u))


# ⚑ ISA-0919 (Raj 29-Oct-2026) - DECLARED SHARE-CLASS HISTORY SPLICE. A share class younger than the fund
#   reads 'insufficient history' on every window and 'never tested by a drawdown' at the router's regime gate,
#   although the KID itself simulates the class's history with an older class of the SAME portfolio. A fund
#   may declare `history_splice` = {path, ...}: that predecessor series is prepended BEFORE the held class's
#   first NAV, rescaled to it at the join, and ONLY while the two classes still validate on their overlap
#   (fund_learning.validate_proxy: correlation >= PROXY_CORR_MIN AND |annualised difference| <= PROXY_ANN_DIFF_MAX_PP,
#   >= SPLICE_MIN_OVERLAP_MONTHS). A refused validation returns the held class alone - never a silent splice.
SPLICE_MIN_OVERLAP_MONTHS = 24
_SPLICE_MEMO = {}


def _month_end_returns(series):
    by = {}
    for d, v in series or []:
        by[(d.year, d.month)] = v
    ks = sorted(by)
    return {ks[i]: by[ks[i]] / by[ks[i - 1]] - 1.0 for i in range(1, len(ks)) if by[ks[i - 1]] > 0}


def history_splice(sedol, u, held, base=None):
    """-> (series, state). state = {state: NOT_DECLARED | APPLIED | REFUSED | UNREADABLE, ...}. Pure on its inputs
    (the predecessor file is read from `base`, default this folder); memoised on (file stat, held span)."""
    hs = (u or {}).get("history_splice")
    if not hs:
        return held, {"state": "NOT_DECLARED"}
    path = os.path.join(base or SCRIPT_DIR, hs.get("path") or "")
    try:
        st_ = os.stat(path)
        key = (path, st_.st_mtime_ns, st_.st_size, len(held or []), (held or [(None,)])[0][0], (held or [(None,)])[-1][0])
    except OSError as exc:
        return held, {"state": "UNREADABLE", "why": "predecessor file %s: %s" % (hs.get("path"), exc)}
    if key in _SPLICE_MEMO:
        return _SPLICE_MEMO[key]
    pred = _read_csv_series(path)
    if not pred or not held:
        res = (held, {"state": "UNREADABLE", "why": "empty predecessor or held series"})
        _SPLICE_MEMO[key] = res
        return res
    import fund_learning as _fl
    v = _fl.validate_proxy(_month_end_returns(held), _month_end_returns(pred), label="%s<-%s" % (sedol, hs.get("isin")))
    if not v.get("admissible") or (v.get("n_overlap") or 0) < SPLICE_MIN_OVERLAP_MONTHS:
        res = (held, {"state": "REFUSED", "validation": v,
                      "why": "the classes no longer validate on their overlap (or < %d months) - held class only"
                             % SPLICE_MIN_OVERLAP_MONTHS})
        _SPLICE_MEMO[key] = res
        return res
    join_d, join_v = held[0]
    anchor = _nav_on_or_before(pred, join_d, max_back_days=10)
    if not anchor or anchor[1] <= 0:
        res = (held, {"state": "REFUSED", "why": "no predecessor NAV within 10d of the join %s" % join_d})
        _SPLICE_MEMO[key] = res
        return res
    k = join_v / anchor[1]
    pre = [(d, x * k) for d, x in pred if d < join_d]
    res = (pre + list(held), {"state": "APPLIED", "join": join_d.isoformat(), "predecessor_isin": hs.get("isin"),
                              "predecessor_share_class": hs.get("share_class"), "n_prepended": len(pre),
                              "from": pre[0][0].isoformat() if pre else None, "scale": round(k, 8),
                              "validation": {kk: v.get(kk) for kk in ("n_overlap", "overlap", "monthly_corr", "ann_diff_pp",
                                                                      "admissible")},
                              "source": hs.get("source"), "authority": hs.get("authority")})
    _SPLICE_MEMO[key] = res
    return res


def nav_series_for(sedol, u, refresh=False, with_state=False):
    """THE one way to get a NAV series for a fund. Local series first where declared, then the
    feed. One home (R4.4) — every caller that had its own `yf_symbol or isin` line was a place a
    fund with no feed could go quietly missing. ISA-0919: a declared history_splice is applied HERE, so
    every consumer of the door sees the same joined history (or the same refusal)."""
    _fi_mark("fund_performance", "nav_series_for")                      # ISA-0940: declared capability producer
    held = _held_series(u, refresh=refresh)
    series, state = history_splice(sedol, u, held)
    return (series, state) if with_state else series


# ---------------------------------------------------------------- the door's declared consumers (ISA-0942 / ISA-0874 leg 1)
# Every production module that reads fund NAV through nav_series_for, with the history WINDOW it consumes. One
# declaration, asserted by door_callsite_audit(): a new caller, or a caller that leaves, fails the selftest until
# this table says what it reads (R4.6.3). Depth of every series is held at the cache start (2016-08-05 on the
# yfinance caches, _hold_depth) - a consumer that wants "full history" reads exactly that depth.
NAV_DOOR_CONSUMERS = {
    "fund_performance": "WINDOWS 1m/3m/6m/1y/3y/5y/10y trailing to the calendar as_of (MIN_COVERAGE 98%); nav_freshness = newest point",
    "fund_action_stack": "monthly month-end returns: last 60 (5y) and 36 (3y) months for mean/stddev, correlation >= 24 months; FRS returns via fund_performance()",
    "holding_period_return": "point lookups: NAV on/before each transfer date (<= 7 days back)",
    "benchmark_registry": "monthly returns over the full held depth, split in two halves (DRIFT_MIN_MONTHS 48); noise floors were measured on this depth",
    "fund_learning": "monthly panel over the full held depth; rolling 36-month windows (ROLLING_WINDOW_M) for T4 calibration",
    "whole_book_risk": "weekly last-NAV-of-week levels over the common whole-book window",
    "whole_book_stress": "weekly levels over the common whole-book window (same series_fn as whole_book_risk)",
    "concentration_clusters": "monthly returns over the common window (MIN_COMMON_MONTHS 24)",
    "report_measures": "1M/3M/6M/YTD/1Y/3Y pa/5Y pa to the run date; 1Y/3Y/5Y at the X-Ray's corroborated date",
    "fund_pair_test": "monthly returns, common window >= 24 months (H7)",
    "missed_opportunity_diag": "VUAG weekly levels (benchmark baseline)",
    "beta_alpha_study": "monthly returns over the full held depth (study, not a decision input)",
    "extract_xray": "1Y/3Y/5Y trailing at each candidate month-end strike (printed date - 100 days .. report date) - X-Ray date corroboration (ISA-0202)",
}
# Calls that bypass the door. Only fund_performance's own internals may make them; anything else is a defect,
# except these DECLARED exemptions (each names why; a stale exemption fails the audit too).
NAV_DOOR_PRIVATE = ("fetch_nav_history", "load_local_series", "_yf_history", "_held_series", "_read_cache")
NAV_DOOR_EXEMPT = {
    "test_fund_performance.py": "the door's own regression suite reads raw caches by symbol on purpose",
    "test_benchmark_registry.py": "negative controls on load_local_series reconciliation (it must RAISE)",
    "fund_pair_test.py": "ISA-0919 selftest asserts door == _held_series for an undeclared fund (equivalence check)",
}
# Readers that open nav_cache/*.csv FILES directly (not a call above). Their bytes are migrated in place by the
# cache's declared writer, but they bypass provenance - owned by ISA-0945, listed so they are visible, not silent.
NAV_DOOR_DIRECT_READERS = {
    "capital_destination.py": "ISA-0945: _load_sleeve (regime coverage) / _fund_levels read nav_cache/<symbol>.csv",
    "fund_expected_return.py": "ISA-0945: sigma_fund reads the nav_cache glob by stem",
    "whole_book_risk.py": "ISA-0945: _nav_stamp (mtime cache-invalidation stamp only, no values)",
    "isa_tree_scope.py": "not a reader: lists nav_cache as a non-source directory",
}


def door_callsite_audit(root=None):
    """-> {state PASS|FAIL, violations, stale_exemptions, undeclared_consumers, stale_consumers, direct_readers}.
    AST over every top-level module (and tests_jul2026/): no attribute call to a NAV_DOOR_PRIVATE name outside
    fund_performance.py except a declared exemption; the set of modules calling `.nav_series_for` equals
    NAV_DOOR_CONSUMERS; every module using the 'nav_cache' path literal is a declared direct reader."""
    import ast as _ast, glob as _glob
    root = root or SCRIPT_DIR
    files = sorted(_glob.glob(os.path.join(root, "*.py")) + _glob.glob(os.path.join(root, "tests_jul2026", "*.py")))
    viol, callers, used_ex, direct = [], set(), set(), {}
    for f in files:
        base = os.path.basename(f)
        mod = base[:-3]
        if base == "fund_performance.py":
            continue
        try:
            tree = _ast.parse(open(f, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute):
                if n.func.attr in NAV_DOOR_PRIVATE:
                    if base in NAV_DOOR_EXEMPT:
                        used_ex.add(base)
                    else:
                        viol.append("%s:%d calls .%s outside the door" % (base, n.lineno, n.func.attr))
                elif n.func.attr == "nav_series_for" and not base.startswith("test_"):
                    callers.add(mod)
            if isinstance(n, _ast.Constant) and n.value == "nav_cache":
                direct.setdefault(base, n.lineno)
    callers.add("fund_performance")
    undeclared = sorted(callers - set(NAV_DOOR_CONSUMERS))
    stale = sorted(set(NAV_DOOR_CONSUMERS) - callers)
    stale_ex = sorted(set(NAV_DOOR_EXEMPT) - used_ex)
    undeclared_direct = sorted(set(direct) - set(NAV_DOOR_DIRECT_READERS))
    stale_direct = sorted(set(NAV_DOOR_DIRECT_READERS) - set(direct))
    ok_ = not (viol or undeclared or stale or stale_ex or undeclared_direct or stale_direct)
    return {"state": "PASS" if ok_ else "FAIL", "violations": viol, "stale_exemptions": stale_ex,
            "undeclared_consumers": undeclared, "stale_consumers": stale,
            "undeclared_direct_readers": undeclared_direct, "stale_direct_readers": stale_direct,
            "consumers": sorted(callers), "direct_readers": direct}


# ---------------------------------------------------------------- maths
def _nav_on_or_before(series, target, max_back_days=10):
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= target:
            best = series[mid]; lo = mid + 1
        else:
            hi = mid - 1
    if best and (target - best[0]).days <= max_back_days:
        return best
    return None


def _years_before(as_of: dt.date, years: float) -> dt.date:
    return as_of - dt.timedelta(days=round(years * 365.25))


def trailing_return(series, as_of, years, source, label):
    """-> (cumulative Metric|Missing, annualised Metric|Missing). Never
    extrapolates: a window not genuinely covered returns Missing (rule 1)."""
    miss = lambda r: (Missing(r, as_of, source), Missing(r, as_of, source))
    if not series:
        return miss("no NAV history")
    end = _nav_on_or_before(series, as_of)
    if not end:
        return miss(f"no NAV within 10d of {as_of}")
    start_target = _years_before(as_of, years)
    # ⚑ ONE THRESHOLD, NOT TWO (fixed 06-Aug-2026). This gate demanded the history reach the FULL
    # window while MIN_COVERAGE below declared 98% sufficient — two rules for the same question,
    # with the stricter one winning silently and MIN_COVERAGE never consulted at the boundary.
    # Every cache begins 2016-08-05 and the 10-year window from 31-Jul-2026 needs 01-Aug-2016, so
    # the 10y figure was refused on FOUR DAYS while 99.9% of the window was sitting in the file.
    # The two now agree, and MIN_COVERAGE is the single home for "how much of the window is
    # enough". (Annualisation stays on the NOMINAL window — that is the convention Morningstar
    # uses and the reason RLGES 15.82 and JPM UK 12.06 reproduce to 2dp.)
    if series[0][0] > _years_before(as_of, years * MIN_COVERAGE):
        have = (as_of - series[0][0]).days / 365.25
        return miss(f"insufficient_history: {have:.2f}y available, "
                    f"{years * MIN_COVERAGE:.2f}y required ({MIN_COVERAGE:.0%} of {years:g}y)")
    start = _nav_on_or_before(series, start_target, max_back_days=15)
    if not start:
        # The history begins just AFTER the window's start date. Refusing here was the second
        # half of the same two-thresholds defect: the gate above now admits 98% coverage, and
        # then this lookup demanded a NAV on or before the full-window date and refused anyway.
        # Fall back to the earliest observation and let MIN_COVERAGE below be the single arbiter.
        cand = series[0]
        if (end[0] - cand[0]).days / 365.25 >= years * MIN_COVERAGE:
            start = cand
        else:
            return miss(f"no NAV within 15d of {start_target} and the earliest observation "
                        f"({cand[0]}) covers only "
                        f"{(end[0] - cand[0]).days / 365.25:.2f}y of {years:g}y")
    covered = (end[0] - start[0]).days / 365.25
    # ⚑ ISA-0920 (29-Oct-2026): coverage is about where the HISTORY BEGINS. The END is already bounded by the
    #   10-day rule above, so measuring the span to the latest NAV double-counted ordinary publication lag as
    #   missing history: a 1y window whose NAV was 8 days old read 0.97y and was blanked, and 1m/3m/6m windows
    #   were empty for every fund. The test is now: the window's start is covered to within MIN_COVERAGE.
    if (as_of - start[0]).days / 365.25 < years * MIN_COVERAGE:
        return miss(f"window_undercovered: {covered:.2f}y of {years:g}y")
    if start[1] <= 0:
        return miss("non-positive start NAV")
    cum = (end[1] / start[1]) - 1.0
    ann = (1.0 + cum) ** (1.0 / years) - 1.0 if years >= 1 else cum
    note = f"{start[0]}->{end[0]} ({covered:.2f}y covered of {years:g}y nominal)"
    return (Metric(cum * 100, end[0], source, unit="%", note=note),
            Metric(ann * 100, end[0], source, unit="%", note=note))


# ---------------------------------------------------------------- invariants
def verify_price_match(series, broker_price, broker_date,
                       tol_pct=PRICE_MATCH_TOL_PCT, max_lag=MAX_PRICING_LAG_DAYS):
    """I1 -- LAG-AWARE, DATE-ALIGNED price identity check.

    A naive latest-vs-broker comparison produced false mismatches that were pure
    market drift (M&G 5.34%, Invesco 3.31%). A naive same-day comparison produced
    false mismatches because UK OEICs are struck T-1 against the broker's
    valuation date. Searching a short lag window and taking the BEST match gives
    an exact identity (every holding reconciles to 0.00%), so any residual
    difference is a real fault -- which is how the Ranmore wrong-share-class
    (315%) was caught.

    Returns (ok, detail, nav, lag_days).
    """
    if not series:
        return False, "no NAV history", None, None
    if not broker_price:
        return False, "no broker price", None, None
    cands = [(d, c) for d, c in series
             if 0 <= (broker_date - d).days <= max_lag]
    if not cands:
        return False, f"no NAV within {max_lag}d on/before {broker_date}", None, None
    d, nav = min(cands, key=lambda x: abs(x[1] - broker_price))
    diff = abs(nav - broker_price) / broker_price * 100.0
    lag = (broker_date - d).days
    ok = diff <= tol_pct
    return ok, (f"{'OK' if ok else 'MISMATCH'}: NAV {nav:.4f} @{d} (lag {lag}d) vs "
                f"broker {broker_price:.4f} @{broker_date} = {diff:.2f}% "
                f"(tol {tol_pct}%)"), nav, lag


def verify_cum_ann(cum, ann, years, tol=1e-6):
    """I3 -- the two derivations of the same return must agree."""
    if not (is_present(cum) and is_present(ann)):
        return True, "skipped (absent)"
    if years < 1:
        return True, "n/a (<1y not annualised)"
    lhs = (1 + cum.value / 100) ** (1 / years) - 1
    d = abs(lhs - ann.value / 100)
    return d <= tol, f"cum->ann delta {d:.2e} (tol {tol:.0e})"


def verify_vs_xray(golden_ann, xray_pct, tol_pp=XRAY_AGREE_TOL_PP):
    """I6 -- X-Ray is a CORROBORATOR, not a source. Disagreement is a WARN that
    must name both values and both dates."""
    if not is_present(golden_ann) or xray_pct is None:
        return True, "skipped"
    d = abs(golden_ann.value - xray_pct)
    return d <= tol_pp, (f"golden {golden_ann.value:.2f}% @{golden_ann.as_of} vs "
                         f"xray {xray_pct:.2f}% = {d:.2f}pp (tol {tol_pp}pp)")


# ---------------------------------------------------------------- main API
def fund_performance(sedol, as_of, universe=None, broker_price=None,
                     broker_date=None, refresh=False):
    """Golden-source trailing performance for one fund."""
    universe = universe or load_universe()
    u = universe.get(sedol)
    if not u:
        return {"sedol": sedol, "status": "not_in_universe",
                "returns": {}, "invariants": {}}

    out = {"sedol": sedol, "name": u["name"], "isin": u.get("isin"),
           "share_class": u.get("share_class"), "bucket": u.get("bucket"),
           "ocf": u.get("ocf"), "as_of_requested": as_of.isoformat(),
           "status": u.get("resolution_status"), "returns": {}, "invariants": {},
           "notes": []}

    if u.get("isin") and not isin_is_valid(u["isin"]):
        out["notes"].append(f"ISIN {u['isin']} fails its own check digit")

    man = u.get("manual_returns")
    if man and u.get("resolution_status") == "manual_factsheet":
        out["status"] = "manual_factsheet"
        src = man["source"]
        for w in WINDOWS:
            v = man["annualised_pct"].get(w)
            if v is None:
                m = Missing(man.get("absent_reason", {}).get(w, "not published by source"),
                            man["as_of"], src)
                out["returns"][w] = {"cumulative": as_dict(m), "annualised": as_dict(m),
                                     "proxy_used": False}
            else:
                mm = Metric(v, man["as_of"], src, confidence=man.get("confidence", 1.0),
                            unit="%", note="manager/platform factsheet")
                out["returns"][w] = {"cumulative": as_dict(
                                         Missing("factsheet publishes annualised only",
                                                 man["as_of"], src)),
                                     "annualised": as_dict(mm), "proxy_used": False}
        out["notes"].append(f"NAV feed unavailable; trailing returns taken from {src} "
                            f"as at {man['as_of']} (stamped, not inferred).")
        return out

    if u.get("resolution_status") != "resolved" or not (u.get("yf_symbol") or u.get("local_series")):
        r = u.get("unresolved_reason", "no yf_symbol")
        out["status"] = "unresolved"
        out["fallback"] = u.get("fallback")
        for w in WINDOWS:
            out["returns"][w] = {"cumulative": as_dict(Missing(r, as_of, "fund_universe")),
                                 "annualised": as_dict(Missing(r, as_of, "fund_universe"))}
        return out

    sym = u.get("yf_symbol")
    src = ((f"local_series:{u['local_series']['cache']}+ft:{u['ft_xid']}" if u.get("ft_xid")
            else f"local_series:{u['local_series']['path']}") if u.get("local_series")
           else f"yfinance:{sym}")
    scale = _scale_for(u)
    # ⚑ ISA-0942 (20-Nov-2026): through THE door, so the class's declared price basis, provenance and splice reach
    #   the FRS return adequacy, the anchor rule and return_architecture's realised leg (this function fed all three
    #   while reading the cache itself).
    try:
        series, _spl = nav_series_for(sedol, u, refresh=refresh, with_state=True)
    except PriceBasisUndeclared as _pbe:
        out["status"] = "price_basis_undeclared"
        for w in WINDOWS:
            out["returns"][w] = {"cumulative": as_dict(Missing(str(_pbe), as_of, "fund_universe")),
                                 "annualised": as_dict(Missing(str(_pbe), as_of, "fund_universe"))}
        return out
    out["nav_provenance"] = nav_series_provenance(sedol, u)
    if _spl.get("state") != "NOT_DECLARED":
        out["history_splice"] = _spl
        if _spl.get("state") == "APPLIED":
            src = src + "+splice:%s[%s before %s]" % (_spl.get("predecessor_isin"), _spl.get("predecessor_share_class"),
                                                     _spl.get("join"))
            out["notes"].append("History before %s is the %s class (%s), joined at its first NAV and validated on the "
                                "overlap (corr %s, diff %spp/yr) - KID-simulated history, Raj 29-Oct-2026 (ISA-0919)."
                                % (_spl.get("join"), _spl.get("predecessor_share_class"), _spl.get("predecessor_isin"),
                                   (_spl.get("validation") or {}).get("monthly_corr"),
                                   (_spl.get("validation") or {}).get("ann_diff_pp")))
        else:
            out["notes"].append("Declared history splice NOT applied (%s): %s" % (_spl.get("state"), _spl.get("why")))
    out["nav_points"] = len(series)
    out["nav_freshness"] = nav_freshness(sedol, u, as_of, series=series)     # ISA-0869: typed, never blank
    if series:
        out["nav_range"] = [series[0][0].isoformat(), series[-1][0].isoformat()]
        # DECLARED inception (KID/factsheet), carried so that an absent window can be
        # classified rather than merely reported. Absent = the absence stays `unclassified`,
        # which is an honest open question, not a pass.
        out["declared_inception"] = (u or {}).get("inception")
        out["inception_source"] = (u or {}).get("inception_source")
        if out["declared_inception"] and out["nav_range"][0] < str(out["declared_inception"])[:10]:
            out.setdefault("notes", []).append(
                f"⚑ NAV history begins {out['nav_range'][0]}, BEFORE the declared inception "
                f"{out['declared_inception']} — one of the two is wrong. Declared inception is "
                f"not used for this fund until reconciled.")
            out["declared_inception"] = None

    # Pin the measurement date to the NAV the BROKER actually priced against.
    # "Latest available" makes a return depend on WHEN the pipeline runs, because
    # OEIC NAVs publish with a lag (Ranmore's 31-Jul NAV appeared between two
    # fetches 30 minutes apart and moved its 1y from 17.70% to 16.82%). Pinning
    # makes every figure reproducible and matches the basis the broker valued on.
    effective_as_of = as_of
    if broker_price and broker_date:
        ok, msg, nav, lag = verify_price_match(series, broker_price, broker_date)
        out["invariants"]["I1_price_match"] = {"pass": ok, "detail": msg,
                                               "lag_days": lag}
        out["pricing_lag_days"] = lag
        # NOTE: lag is recorded for the I1 identity check ONLY. Returns are
        # measured to the CALENDAR as_of -- confirmed against two managers'
        # published figures on the exact held share class:
        #   MI Thornbridge C Acc  1y 13.32 / 3y 15.57 / 5y 16.93  (HL)
        #   Ranmore Institutional 1y 16.82 / 3y 19.94 / 5y n/a    (AJ Bell)
        # Both reproduce EXACTLY on the calendar basis. Lag-pinning reproduced
        # neither and was reverted.
        if not ok:
            out["status"] = "price_mismatch"
            out["notes"].append("I1 FAILED -- likely wrong share class. "
                                "Figures suppressed rather than published.")
    out["as_of_effective"] = effective_as_of.isoformat()
    out["as_of_basis"] = "calendar as_of (validated vs manager factsheets)"

    proxy = u.get("proxy") or {}
    proxy_series = None
    for label, yrs in WINDOWS.items():
        cum, ann = trailing_return(series, effective_as_of, yrs, src, label)
        used_proxy = False
        if isinstance(cum, Missing) and label in proxy.get("applies_to", []):
            if proxy_series is None:
                try:
                    proxy_series = fetch_nav_history(proxy["yf_symbol"], refresh=refresh, scale=scale,
                                                     basis=price_basis_for(proxy))
                except PriceBasisUndeclared as _ppe:
                    proxy_series = []
                    out["notes"].append("%s: proxy %s NOT used - %s" % (label, proxy.get("isin"), _ppe))
            psrc = f"yfinance:{proxy['yf_symbol']}[{proxy['flag']}]"
            pc, pa = trailing_return(proxy_series, effective_as_of, yrs, psrc, label)
            if is_present(pc):
                cum, ann, used_proxy = pc, pa, True
        okc, dmsg = verify_cum_ann(cum, ann, yrs)
        out["invariants"][f"I3_cum_ann_{label}"] = {"pass": okc, "detail": dmsg}
        out["returns"][label] = {"cumulative": as_dict(cum), "annualised": as_dict(ann),
                                 "proxy_used": used_proxy}
        if used_proxy:
            out["notes"].append(
                f"{label}: {proxy['flag']} via {proxy['share_class']} "
                f"({proxy['isin']}) -- {proxy['authority'][:80]}...")

    if out["status"] == "resolved":
        out["status"] = "ok"
    return out


def all_fund_performance(as_of, portfolio_funds=None, refresh=False):
    """Every fund in the universe. I5: every portfolio fund must appear."""
    universe = load_universe()
    px = {f["ticker"]: (f.get("price"), f.get("_as_of")) for f in (portfolio_funds or [])}
    res = {}
    for sedol in universe:
        p, d = px.get(sedol, (None, None))
        res[sedol] = fund_performance(sedol, as_of, universe, p, d, refresh)
    if portfolio_funds is not None:
        missing = [f["ticker"] for f in portfolio_funds if f["ticker"] not in res]
        res["_i5_coverage"] = {"pass": not missing, "missing": missing}
    return res


def window_coverage(perf_by_sedol):
    """I8 — a DECLARED window that yields nothing for the ENTIRE universe is a build fault, not a
    data fact. It was silence exactly like this that hid the 10-year window for a whole month:
    every fund returned Missing('insufficient_history'), which is individually plausible and
    collectively impossible. One fund missing a window is data; twelve of twelve is code."""
    cov = {w: {"have": [], "missing": {}} for w in WINDOWS}
    for sedol, p in sorted(perf_by_sedol.items()):
        if str(sedol).startswith("_"):
            continue
        for w in WINDOWS:
            a = ((p.get("returns", {}).get(w) or {}).get("annualised") or {})
            if a.get("present"):
                cov[w]["have"].append(sedol)
            else:
                cov[w]["missing"][sedol] = a.get("reason", "absent")
    n = sum(1 for k in perf_by_sedol if not str(k).startswith("_"))
    out = {}
    for w, c in cov.items():
        # ⚑ FIXED 06-Aug-2026. This line was
        #     {k: v for k, v in list(c["missing"].items())[:3]}
        # — the reasons were TRUNCATED TO THREE. On the August run four funds had no 10-year
        # figure and three reasons were published, so **VUAG's absence was silent**: the report
        # showed 8 of 12 covered and explained 3 of the 4 gaps, with nothing indicating that
        # anything had been left out.
        #
        # This is the same failure as N5 (`consistency_check` verifying a fixed byte window
        # instead of the table): a report whose explanatory coverage shrinks as the thing it
        # explains grows. It is harmless-looking at 4 gaps and a false all-clear at 40.
        # An explanation list must never be truncated — the whole point of it is completeness.
        out[w] = {"covered": len(c["have"]), "of": n,
                  "alarm": bool(n and not c["have"]),
                  "n_missing": len(c["missing"]),
                  "reasons": dict(c["missing"])}
        assert len(out[w]["reasons"]) == out[w]["n_missing"], (
            f"{w}: every absence must carry a reason ({out[w]['n_missing']} missing, "
            f"{len(out[w]['reasons'])} explained)")
        assert out[w]["covered"] + out[w]["n_missing"] == n, (
            f"{w}: covered + missing must equal the universe")
        # ── structural history limit vs an unexpected short series ─────────────────────
        # "insufficient_history" is two different facts wearing one label: a fund that did not
        # exist yet (permanent, expected, not a defect) and a fetch that came back short
        # (transient, a defect, and indistinguishable from the first without a second source).
        # `inception` in fund_universe.json is that second source — DECLARED from the KID, never
        # inferred — and the two must agree.
        struct = {}
        for sd, reason in out[w]["reasons"].items():
            p = perf_by_sedol.get(sd) or {}
            inc = p.get("declared_inception")
            first = (p.get("nav_range") or [None])[0]
            cls = "unclassified"
            note = ("no declared inception in fund_universe.json — this absence cannot be "
                    "distinguished from a short fetch. One declared date per fund closes it.")
            if "not published" in str(reason):
                cls, note = "source_does_not_publish", str(reason)
            elif inc:
                need = _years_before(dt.date.today(), WINDOWS[w])
                inc_d = str(inc)[:10]
                if inc_d > need.isoformat():
                    cls = "structural_history_limit"
                    note = (f"declared inception {inc_d} postdates the {w} window start "
                            f"{need.isoformat()} — the history does not exist and never will. "
                            f"Expected, permanent, NOT a defect.")
                else:
                    cls = "UNEXPECTED_SHORT_SERIES"
                    note = (f"⚑ declared inception {inc_d} PREDATES the {w} window start "
                            f"{need.isoformat()}, so this history should exist. A short series "
                            f"here is a FETCH problem, not a fund fact.")
            struct[sd] = {"reason": str(reason), "classification": cls,
                          "declared_inception": inc, "observed_series_start": first,
                          "note": note}
        out[w]["absence_classification"] = struct
        if out[w]["alarm"]:
            out[w]["note"] = (f"⚑ the {w} window produced NOTHING across all {n} funds. A window "
                              f"declared in WINDOWS and used by return adequacy must either "
                              f"produce values or be removed — an empty one reads as 'not "
                              f"applicable' and quietly shrinks the evidence base.")
    return out


def append_history(perf_by_sedol, est_by_sedol, run_date):
    """I7 -- persist est BESIDE realised every month so D2 (est has no
    discriminating power) becomes measurable instead of merely noticed."""
    new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_date", "sedol", "name", "bucket", "est_return_pct",
                        "realised_1y_ann", "realised_3y_ann", "realised_5y_ann",
                        "as_of", "source", "status"])
        for sedol, p in sorted(perf_by_sedol.items()):
            if sedol.startswith("_"):
                continue
            g = lambda w_: (p.get("returns", {}).get(w_, {})
                            .get("annualised", {}) or {}).get("value")
            a = (p.get("returns", {}).get("1y", {}).get("annualised", {}) or {})
            w.writerow([run_date, sedol, p.get("name"), p.get("bucket"),
                        est_by_sedol.get(sedol), g("1y"), g("3y"), g("5y"),
                        a.get("as_of"), a.get("source"), p.get("status")])
    return HISTORY_CSV


def _selftest():
    """ISA-0869 offline selftest (no network): FT parser, junction merge, supersede guard, the refresh
    producer's typed states, cache fallback on a failed refresh, freshness boundary (R5.14 matrix)."""
    import tempfile, shutil
    fails = []

    def ok(name, cond, detail=None):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
        if not cond:
            fails.append(name)
    D = dt.date
    row = ('<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">%s</span>'
           '<span class="mod-ui-hide-medium-above">x</span></td><td>1</td><td>1</td><td>1</td><td>%s</td>'
           '<td><span>0</span></td></tr>')
    fx = json.dumps({"data": {}, "html": row % ("Thursday, October 15, 2026", "202.38")
                     + row % ("Wednesday, October 14, 2026", "1,209.21")})
    got = parse_ft_history(fx)
    ok("FT parser reads date + close (thousands separator) ascending",
       got == [(D(2026, 10, 14), 1209.21), (D(2026, 10, 15), 202.38)], got)
    try:
        parse_ft_history(json.dumps({"data": {}, "html": "<tr><td>Oct 15</td><td>1</td></tr>"}))
        ok("MUST-FIRE: an FT layout change RAISES (never a guessed series)", False)
    except FtLayoutError:
        ok("MUST-FIRE: an FT layout change RAISES (never a guessed series)", True)
    try:
        parse_ft_history("<html>blocked</html>")
        ok("MUST-FIRE: a non-JSON FT payload RAISES", False)
    except FtLayoutError:
        ok("MUST-FIRE: a non-JSON FT payload RAISES", True)
    cached = [(D(2026, 7, 31), 193.97), (D(2026, 8, 4), 205.35)]
    m, chk = merge_ft_series(cached, [(D(2026, 8, 4), 205.35), (D(2026, 8, 5), 204.61)])
    ok("NEGATIVE CONTROL: FT agreeing on the overlap extends the cache",
       m == [(D(2026, 7, 31), 193.97), (D(2026, 8, 4), 205.35), (D(2026, 8, 5), 204.61)] and chk["overlap_points"] == 1, (m, chk))
    m2, chk2 = merge_ft_series(cached, [(D(2026, 8, 4), 210.0), (D(2026, 8, 5), 204.61)])
    ok("MUST-FIRE: FT disagreeing with the cache on an overlapping date is REFUSED", m2 is None and chk2["disagreements"], chk2)
    full = [(D(2016, 8, 5) + dt.timedelta(days=i), 1.0 + i / 1000) for i in range(0, 3700, 7)]
    ok("MUST-FIRE: a truncated provider series never replaces the cache",
       not _series_supersedes(full[100:] + [(full[-1][0] + dt.timedelta(days=30), 9.0)], full))
    ok("MUST-FIRE: a series that does not advance never replaces the cache", not _series_supersedes(full[:-1], full))
    ok("NEGATIVE CONTROL: an advancing covering series replaces it",
       _series_supersedes(full + [(full[-1][0] + dt.timedelta(days=7), 9.0)], full))
    ok("business-day arithmetic: Fri->Mon is 1", _bdays_after(D(2026, 10, 16), D(2026, 10, 19)) == 1)
    deep = [(D(2013, 1, 2) + dt.timedelta(days=i), 1.0) for i in range(0, 5000, 7)]
    held = _hold_depth(deep, full)
    ok("MUST-FIRE: a deeper provider history is trimmed to the cache's start (depth held, ISA-0869)",
       held and held[0][0] >= full[0][0] and held[-1] == deep[-1], held[:1])
    ok("NEGATIVE CONTROL: with no cache the provider's full depth is kept", _hold_depth(deep, None) == deep)

    tmp = tempfile.mkdtemp(prefix="fp_st_")
    try:
        cdir = os.path.join(tmp, "nav_cache"); os.makedirs(cdir)
        stp = os.path.join(tmp, "nav_cache_state.json")
        old_hist = [(D(2026, 5, 1) + dt.timedelta(days=i), 10.0 + i / 100) for i in range(0, 96)]   # newest 04-Aug
        global CACHE_DIR
        _cd = CACHE_DIR
        CACHE_DIR = cdir
        _write_cache("GOOD", old_hist); _write_cache("DOWN", old_hist)
        CACHE_DIR = _cd
        _write_csv_series(os.path.join(cdir, "POLAR.csv"), [(D(2026, 7, 31), 193.97), (D(2026, 8, 4), 205.35)])
        _write_csv_series(os.path.join(cdir, "MAN.csv"), [(D(2026, 7, 31), 5.0)])
        U = {"funds": {"G": {"name": "Good", "yf_symbol": "GOOD", "acc_or_inc": "acc"},
                       "X": {"name": "Down", "yf_symbol": "DOWN", "acc_or_inc": "acc"},
                       "P": {"name": "Polar", "ft_xid": "1", "local_series": {"cache": "nav_cache/POLAR.csv"}},
                       "M": {"name": "Manual", "local_series": {"cache": "nav_cache/MAN.csv"}}}}
        new_hist = old_hist + [(D(2026, 8, 5) + dt.timedelta(days=i), 11.0) for i in range(0, 86)]  # to 29-Oct
        yfh = lambda sym, basis=None: new_hist if sym == "GOOD" else None                   # noqa: E731
        ftp = json.dumps({"html": row % ("Tuesday, August 04, 2026", "205.35") + row % ("Thursday, October 29, 2026", "220.00")})
        T = D(2026, 10, 30)
        now = dt.datetime(2026, 10, 31, 9, 30)
        r = refresh_nav_cache(T, universe=U, get=lambda url: ftp, yf_history=yfh, now=now, state_path=stp, cache_dir=cdir)
        f = r["funds"]
        ok("MUST-FIRE: a stale yfinance cache is re-fetched -> FRESH", f["G"]["state"] == "FRESH" and f["G"]["newest"] == "2026-10-29", f["G"])
        CACHE_DIR = cdir
        down = _read_cache("DOWN")
        CACHE_DIR = _cd
        ok("MUST-FIRE: a failed refresh is SOURCE_FAILED, typed with its NAV date, and the cache is KEPT",
           f["X"]["state"] == "SOURCE_FAILED" and f["X"]["newest"] == "2026-08-04" and down == old_hist, f["X"])
        ok("FT route merges daily closes onto the declared series (junction checked) -> FRESH",
           f["P"]["state"] == "FRESH" and f["P"]["newest"] == "2026-10-29" and f["P"]["junction"]["overlap_points"] == 1, f["P"])
        ok("a declared manual series is MANUAL_SERIES (never silently fresh)", f["M"]["state"] == "MANUAL_SERIES", f["M"])
        ok("state file written with counts", json.load(open(stp))["counts"]["FRESH"] == 2)
        r2 = refresh_nav_cache(T, universe=U, get=lambda url: (_ for _ in ()).throw(AssertionError("no fetch")),
                               yf_history=lambda s_, basis=None: (_ for _ in ()).throw(AssertionError("no fetch")),
                               now=now + dt.timedelta(minutes=5), state_path=stp, cache_dir=cdir)
        ok("R5.14 repeat_same_occurrence: a re-run within 18 h fetches nothing and keeps the typed states",
           r2["funds"]["X"]["state"] == "SOURCE_FAILED" and r2["funds"]["G"]["state"] == "FRESH"
           and not any(v["fetched_this_call"] for v in r2["funds"].values()), r2["funds"])
        r3 = refresh_nav_cache(D(2026, 11, 27), universe=U, get=lambda url: "<html>x</html>", yf_history=lambda s_, basis=None: None,
                               now=dt.datetime(2026, 11, 28, 9, 30), state_path=stp, cache_dir=cdir)
        ok("R5.14 next_occurrence: a month later every fund is re-attempted; FT layout failure -> SOURCE_FAILED",
           r3["funds"]["G"]["state"] == "SOURCE_FAILED" and r3["funds"]["P"]["state"] == "SOURCE_FAILED"
           and r3["funds"]["G"]["fetched_this_call"], r3["funds"])
        r4 = refresh_nav_cache(D(2026, 11, 27), universe=U, budget_s=-1, yf_history=lambda s_, basis=None: None,
                               now=dt.datetime(2026, 11, 29, 9, 30), state_path=os.path.join(tmp, "s4.json"), cache_dir=cdir)
        ok("budget spent -> STALE_CACHE (typed), never a silent pass", r4["funds"]["G"]["state"] == "STALE_CACHE", r4["funds"]["G"])
        # fetch_nav_history: failed refresh falls back to the cache, never []
        global _yf_history
        _yh = _yf_history
        CACHE_DIR = cdir
        try:
            _yf_history = lambda sym, period="max", basis=None: None                         # noqa: E731
            ok("MUST-FIRE (ISA-0869 CA2): fetch_nav_history(refresh=True) on a failed fetch returns the CACHE, not []",
               fetch_nav_history("DOWN", refresh=True, basis="raw_close") == old_hist)
            _yf_history = lambda sym, period="max", basis=None: old_hist[:10]                # noqa: E731
            ok("MUST-FIRE: a truncated refresh never overwrites the cache",
               fetch_nav_history("DOWN", refresh=True, basis="raw_close") == old_hist
               and _read_cache("DOWN") == old_hist)
        finally:
            _yf_history = _yh
            CACHE_DIR = _cd
        # freshness boundary (R5.14 T-1 / T / T+1 around the 10-day bar)
        ser = [(D(2026, 10, 20), 1.0)]
        st0 = {"funds": {}}
        ok("R5.14 T-1: 9 days old is FRESH", nav_freshness("Z", {}, D(2026, 10, 29), series=ser, state=st0)["state"] == "FRESH")
        ok("R5.14 T: exactly 10 days old is FRESH", nav_freshness("Z", {}, D(2026, 10, 30), series=ser, state=st0)["state"] == "FRESH")
        ok("R5.14 T+1: 11 days old is STALE_CACHE (typed)",
           nav_freshness("Z", {}, D(2026, 10, 31), series=ser, state=st0)["state"] == "STALE_CACHE")
        ok("NEGATIVE CONTROL: no series is NO_SERIES, never FRESH",
           nav_freshness("Z", {}, D(2026, 10, 31), series=[], state=st0)["state"] == "NO_SERIES")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # ── ISA-0920: publication lag is not missing history ──────────────────────────────────────
    daily = [(D(2024, 1, 1) + dt.timedelta(days=i), 100.0 * (1.0005 ** i)) for i in range(0, 1020)]
    lagged = [p for p in daily if p[0] <= D(2026, 10, 15)]
    c1, a1 = trailing_return(lagged, D(2026, 10, 24), 1.0, "t", "1y")
    ok("ISA-0920 MUST-FIRE: a 1y window whose latest NAV is 9 days old is PRESENT (was 'window_undercovered 0.97y')",
       a1.__class__.__name__ == "Metric", getattr(a1, "reason", None))
    c6, a6 = trailing_return(lagged, D(2026, 10, 24), 0.5, "t", "6m")
    ok("ISA-0920 MUST-FIRE: the 6m window with the same lag is PRESENT", c6.__class__.__name__ == "Metric", getattr(c6, "reason", None))
    short = [p for p in daily if p[0] >= D(2025, 11, 10) and p[0] <= D(2026, 10, 20)]
    cs, as_ = trailing_return(short, D(2026, 10, 24), 1.0, "t", "1y")
    ok("ISA-0920 NEGATIVE CONTROL: a history that STARTS 0.95y back still refuses the 1y window",
       cs.__class__.__name__ != "Metric")
    cx, ax = trailing_return(lagged, D(2026, 10, 31), 1.0, "t", "1y")
    ok("ISA-0920 NEGATIVE CONTROL: an end NAV 16 days old still refuses (10-day rule unchanged)", cx.__class__.__name__ != "Metric")
    # ── ISA-0919: the declared history splice ───────────────────────────────────────────────────
    td = tempfile.mkdtemp(prefix="fp_splice_")
    try:
        pred = [(D(2018, 1, 1) + dt.timedelta(days=i), 50.0 * (1.0004 ** i) * (0.80 if D(2020, 3, 1) <= D(2018, 1, 1) + dt.timedelta(days=i) <= D(2020, 6, 1) else 1.0))
                for i in range(0, 3200)]
        _write_csv_series(os.path.join(td, "pred.csv"), pred)
        held = [(d, x * 2.0) for d, x in pred if d >= D(2023, 6, 1)]
        u = {"history_splice": {"path": "pred.csv", "isin": "XX0000000000", "share_class": "Investor"}}
        _SPLICE_MEMO.clear()
        sp, st = history_splice("T", u, held, base=td)
        ok("ISA-0919 MUST-FIRE: a validated predecessor is prepended before the held class's first NAV, rescaled at the join",
           st["state"] == "APPLIED" and sp[0][0] == D(2018, 1, 1) and abs(sp[0][1] - 100.0) < 1e-6
           and [p for p in sp if p[0] >= D(2023, 6, 1)] == held, st)
        ok("ISA-0919: the joined series makes the 5y window and the 2020 drawdown measurable",
           trailing_return(sp, D(2026, 9, 30), 5.0, "t", "5y")[1].__class__.__name__ == "Metric"
           and trailing_return(held, D(2026, 9, 30), 5.0, "t", "5y")[1].__class__.__name__ != "Metric")
        bad = [(d, x * (1.03 if d.month % 2 else 0.97)) for d, x in held]
        _SPLICE_MEMO.clear()
        sp2, st2 = history_splice("T", u, bad, base=td)
        ok("ISA-0919 NEGATIVE CONTROL: classes that no longer track each other are REFUSED and the held class is returned alone",
           st2["state"] == "REFUSED" and sp2 == bad, st2.get("why"))
        _SPLICE_MEMO.clear()
        ok("ISA-0919 NEGATIVE CONTROL: no declaration -> NOT_DECLARED, series untouched",
           history_splice("T", {}, held, base=td) == (held, {"state": "NOT_DECLARED"}))
        sp3, st3 = history_splice("T", {"history_splice": {"path": "missing.csv"}}, held, base=td)
        ok("ISA-0919 NEGATIVE CONTROL: an unreadable predecessor is UNREADABLE (typed), never a silent splice",
           st3["state"] == "UNREADABLE" and sp3 == held)
    finally:
        shutil.rmtree(td, ignore_errors=True)
        _SPLICE_MEMO.clear()
    # ── ISA-0901 / ISA-0874 / ISA-0942 (20-Nov-2026): price basis per class, provenance, migration, one door ──
    ok("ISA-0901 MUST-FIRE: an ACC class reads the raw close", price_basis_for({"acc_or_inc": "acc"}) == "raw_close")
    ok("ISA-0901 MUST-FIRE: an INC class reads the dividend-reinvested close",
       price_basis_for({"acc_or_inc": "inc"}) == "total_return_adjusted")
    try:
        price_basis_for({"share_class": "X"}); ok("ISA-0901 NEGATIVE CONTROL: an undeclared class is REFUSED", False)
    except PriceBasisUndeclared:
        ok("ISA-0901 NEGATIVE CONTROL: an undeclared class is REFUSED", True)
    try:
        fetch_nav_history("ANY"); ok("R4.7 MUST-FIRE: fetch_nav_history without a basis RAISES (an un-updated caller fails)", False)
    except TypeError:
        ok("R4.7 MUST-FIRE: fetch_nav_history without a basis RAISES (an un-updated caller fails)", True)
    import types as _ty
    _seen = {}

    class _H:
        def __init__(self, adj):
            self.adj = adj

        def __getitem__(self, k):
            v = 1.0 if not self.adj else 0.9
            return _ty.SimpleNamespace(items=lambda: [(dt.datetime(2026, 1, 2), v), (dt.datetime(2026, 1, 5), v)])

    class _T:
        def __init__(self, s):
            pass

        def history(self, period="max", auto_adjust=True):
            _seen["auto_adjust"] = auto_adjust
            return _H(auto_adjust)
    _yf_saved = sys.modules.get("yfinance")
    sys.modules["yfinance"] = _ty.SimpleNamespace(Ticker=_T)
    try:
        r_raw = _yf_history("Z", basis="raw_close"); a_raw = _seen.get("auto_adjust")
        r_adj = _yf_history("Z", basis="total_return_adjusted"); a_adj = _seen.get("auto_adjust")
        ok("ISA-0901 MUST-FIRE: raw_close asks the provider for auto_adjust=False; adjusted asks for True",
           a_raw is False and a_adj is True and r_raw[0][1] == 1.0 and r_adj[0][1] == 0.9, (a_raw, a_adj))
    finally:
        if _yf_saved is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = _yf_saved
    tmp2 = tempfile.mkdtemp(prefix="fp_basis_")
    _cd2 = CACHE_DIR
    try:
        cdir = os.path.join(tmp2, "nav_cache"); os.makedirs(cdir)
        legacy = [(D(2016, 8, 5) + dt.timedelta(days=7 * i), 2.0 * (1.003 ** i)) for i in range(0, 530)]   # to ~Oct-2026
        raw = [(d, v * 0.80) for d, v in legacy] + [(legacy[-1][0] + dt.timedelta(days=7), legacy[-1][1] * 0.8)]
        deep_raw = [(D(2014, 1, 2), 1.0)] + raw
        CACHE_DIR = cdir
        _write_cache("ACCF", legacy)                                    # legacy: no provenance sidecar
        _write_cache("INCF", legacy, basis="total_return_adjusted")     # already at its declared basis
        _write_cache("SHRT", legacy)
        CACHE_DIR = _cd2
        U2 = {"funds": {"A": {"name": "Acc fund", "yf_symbol": "ACCF", "acc_or_inc": "acc"},
                        "I": {"name": "Inc fund", "yf_symbol": "INCF", "acc_or_inc": "inc"},
                        "S": {"name": "Short", "yf_symbol": "SHRT", "acc_or_inc": "acc"},
                        "N": {"name": "Undeclared", "yf_symbol": "INCF"}}}
        calls = []

        def yfh2(sym, basis=None):
            calls.append((sym, basis))
            if sym == "SHRT":
                return raw[200:]
            return deep_raw if sym == "ACCF" else legacy
        T2 = legacy[-1][0] + dt.timedelta(days=3)
        rb = refresh_nav_cache(T2, universe=U2, yf_history=yfh2, now=dt.datetime(2026, 11, 21, 9, 0),
                               state_path=os.path.join(tmp2, "st.json"), cache_dir=cdir)["funds"]
        CACHE_DIR = cdir
        acc_now, prov_a = _read_cache("ACCF"), read_provenance("ACCF")
        shrt_now = _read_cache("SHRT")
        sup = sorted(os.listdir(os.path.join(cdir, "_superseded"))) if os.path.isdir(os.path.join(cdir, "_superseded")) else []
        CACHE_DIR = _cd2
        ok("ISA-0901 MUST-FIRE: a FRESH legacy cache of an ACC class is MIGRATED by its declared writer to the raw close",
           rb["A"].get("basis_migration", {}).get("to") == "raw_close" and acc_now and abs(acc_now[0][1] - legacy[0][1] * 0.8) < 1e-6
           and prov_a and prov_a["basis"] == "raw_close" and rb["A"]["provenance"]["state"] == "BASIS_RECORDED", rb["A"])
        ok("ISA-0901 MUST-FIRE: the migration holds depth at the old cache start (no 2014 history) and keeps the superseded bytes",
           acc_now[0][0] == legacy[0][0] and any(x.startswith("ACCF.unrecorded.") for x in sup), (acc_now[:1], sup))
        ok("ISA-0901 NEGATIVE CONTROL: a cache already at its declared basis is NOT migrated (and is FRESH)",
           "basis_migration" not in rb["I"] and ("INCF", "total_return_adjusted") not in calls and rb["I"]["state"] == "FRESH", rb["I"])
        ok("ISA-0901 NEGATIVE CONTROL: a migration whose series does not cover the cache is REFUSED, cache kept",
           rb["S"].get("basis_migration_failed") and shrt_now == [(d, round(v, 6)) for d, v in legacy], rb["S"].get("why"))
        ok("ISA-0901 MUST-FIRE: an undeclared class is PRICE_BASIS_UNDECLARED in the refresh (typed), never fetched",
           rb["N"]["state"] == "PRICE_BASIS_UNDECLARED", rb["N"])
        ok("ISA-0874 MUST-FIRE: provenance records provider version, pin and the version state",
           prov_a.get("provider") == "yfinance" and prov_a.get("version_state") in ("MATCHES_PIN", "DIFFERS_FROM_PIN", "UNKNOWN")
           and "pinned_version" in prov_a and prov_a.get("depth_held_from") == legacy[0][0].isoformat(), prov_a)
        CACHE_DIR = cdir
        ok("ISA-0874 NEGATIVE CONTROL: a cache with no sidecar is PROVENANCE_UNRECORDED (never assumed)",
           provenance_state("NOPE", "raw_close")["state"] == "PROVENANCE_UNRECORDED")
        ok("ISA-0874 MUST-FIRE: a recorded basis different from the declared one is BASIS_MISMATCH",
           provenance_state("INCF", "raw_close")["state"] == "BASIS_MISMATCH")
        CACHE_DIR = _cd2
    finally:
        CACHE_DIR = _cd2
        shutil.rmtree(tmp2, ignore_errors=True)
    import ast as _ast2
    _fpsrc = open(os.path.abspath(__file__), encoding="utf-8").read()
    _fpt = _ast2.parse(_fpsrc)
    _fpfn = [n for n in _fpt.body if isinstance(n, _ast2.FunctionDef) and n.name == "fund_performance"][0]
    _inner = {(c.func.id if isinstance(c.func, _ast2.Name) else getattr(c.func, "attr", None))
              for c in _ast2.walk(_fpfn) if isinstance(c, _ast2.Call)}
    ok("ISA-0942 MUST-FIRE: fund_performance() reads its series through nav_series_for (not fetch_nav_history / load_local_series)",
       "nav_series_for" in _inner and "load_local_series" not in _inner and "history_splice" not in _inner, sorted(x for x in _inner if x))
    aud = door_callsite_audit()
    ok("ISA-0942 MUST-FIRE: no production module calls a NAV_DOOR_PRIVATE function outside fund_performance; consumers, "
       "exemptions and direct readers are exactly as declared", aud["state"] == "PASS",
       {k: aud[k] for k in ("violations", "stale_exemptions", "undeclared_consumers", "stale_consumers",
                            "undeclared_direct_readers", "stale_direct_readers")})
    print("fund_performance selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    as_of = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    r = all_fund_performance(as_of)
    print(json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                     indent=1, default=str)[:4000])
