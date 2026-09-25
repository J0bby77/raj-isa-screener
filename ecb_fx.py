#!/usr/bin/env python3
"""
ecb_fx.py — ISA-0197 / ISA-0744 (24-Sep-2026). THE canonical valuation-FX producer.

Authority: European Central Bank — EXR euro foreign exchange reference rates, acquired through the
official ECB Data Portal SDMX 2.1 REST API (no scraping, no Yahoo/yfinance FX, no second provider).

    GET https://data-api.ecb.europa.eu/service/data/EXR/D.<CCY+CCY...>.EUR.SP00.A
        ?startPeriod=YYYY-MM-DD&endPeriod=YYYY-MM-DD&format=csvdata      (Accept: text/csv)

Rate convention (ECB): R_CCY(t) = units of CCY per 1 EUR.
    GBP_per_CCY(t) = R_GBP(t) / R_CCY(t);  value_GBP = value_CCY x GBP_per_CCY;  CCY_per_GBP = 1/GBP_per_CCY
    GBP -> GBP is exactly 1.0 because NO conversion is required (never a substitute for a missing rate).
    EUR's own rate is 1 by the ECB's definition of the denominator (R_EUR = 1 EUR per EUR).

PIT: for an as_of date only observations with observation_date <= as_of are eligible; the latest one
is used; requested_as_of, fx_observation_date and the calendar lag are persisted; nothing from the
future fills a weekend or TARGET holiday. A lag beyond MAX_ASOF_LAG_DAYS is STALE.

Realised volatility (INFORMATIONAL sensitivity only - never a forecast, probability or gate):
    X_t = GBP_per_CCY(t); last observation of each ISO week (<= as_of); weekly log returns;
    1y = last 53 weekly obs -> 52 returns; 3y = last 157 -> 156; sample stdev (ddof=1) x sqrt(52).
    Published separately; never blended; insufficient coverage -> typed UNAVAILABLE (no silent shortening).
    One-sigma 12m FX sensitivities: favourable exp(+sigma) - 1, adverse exp(-sigma) - 1.

ECB reference rates are VALUATION rates, not execution rates: broker transaction records remain the
authority for dealing FX and costs.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import math
import os
import statistics
import time
import zipfile
from typing import Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDER = "ECB"
DATAFLOW = "EXR"
API_BASE = "https://data-api.ecb.europa.eu/service/data/EXR/"
SERIES_TEMPLATE = "D.{ccys}.EUR.SP00.A"
FORMAT_PARAM = "csvdata"
ACCEPT = "text/csv"
STATIC_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
METHOD_VERSION = "ECBFX-1.0"
REQUIRED_COLUMNS = ("KEY", "FREQ", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX",
                    "TIME_PERIOD", "OBS_VALUE")
TIMEOUT_S = 20.0
MAX_ATTEMPTS = 3                       # bounded - never infinite
# ECB euro foreign exchange reference rates - the currencies the EXR daily SP00 series publishes.
# A currency outside this set is typed UNSUPPORTED_CURRENCY (never requested, never 1.0, never another provider).
ECB_SUPPORTED = frozenset("USD JPY BGN CZK DKK GBP HUF PLN RON SEK CHF ISK NOK TRY AUD BRL CAD CNY HKD IDR "
                          "ILS INR KRW MXN MYR NZD PHP SGD THB ZAR EUR".split())
S_UNSUPPORTED = "UNSUPPORTED_CURRENCY"
BACKOFF_S = (1.0, 3.0)
MAX_ASOF_LAG_DAYS = 7                  # longest TARGET closure run (Easter/Christmas + weekend) is 4 days
WINDOWS = {"1y": 52, "3y": 156}        # weekly RETURNS; observations needed = returns + 1
HISTORY_DAYS = 1200                    # > 157 ISO weeks of history requested
BASE = "GBP"

OK = "OK"
UNAVAILABLE = "FX_UNAVAILABLE"
STALE = "FX_STALE"
S_TIMEOUT, S_NETWORK, S_HTTP4XX, S_HTTP5XX = "TIMEOUT", "NETWORK_ERROR", "HTTP_4XX", "HTTP_5XX"
S_EMPTY, S_MALFORMED, S_SCHEMA = "EMPTY_RESPONSE", "MALFORMED_CSV", "SCHEMA_MISMATCH"
S_MISSING_CCY, S_DUP, S_NONNUM, S_NONPOS, S_LEAK = ("MISSING_CURRENCY", "DUPLICATE_OBSERVATION",
                                                    "NON_NUMERIC_RATE", "NON_POSITIVE_RATE",
                                                    "OUT_OF_RANGE_OBSERVATION")
TRANSIENT = {S_TIMEOUT, S_NETWORK, S_HTTP5XX, "HTTP_429"}

try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                                            # noqa: D103
        return None


def _d(x) -> Optional[datetime.date]:
    if isinstance(x, datetime.date) and not isinstance(x, datetime.datetime):
        return x
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def api_currencies(ccys) -> List[str]:
    """ECB-quoted currencies needed to price CCY->GBP (EUR is the denominator, never requested)."""
    out = sorted({c for c in ccys if c and c != "EUR"} | {BASE})
    return out


def build_url(ccys, start, end) -> str:
    key = SERIES_TEMPLATE.format(ccys="+".join(api_currencies(ccys)))
    return "%s%s?startPeriod=%s&endPeriod=%s&format=%s" % (API_BASE, key, _d(start), _d(end), FORMAT_PARAM)


# ── transport ────────────────────────────────────────────────────────────────────────────────
def _urllib_get(url, headers, timeout):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Content-Type", "") if e.headers else ""), b""


def _classify(exc) -> str:
    import socket
    name = type(exc).__name__.lower()
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timeout" in name or "timed out" in str(exc).lower():
        return S_TIMEOUT
    return S_NETWORK


def fetch(url, *, http: Optional[Callable] = None, sleep: Callable = time.sleep,
          max_attempts: int = MAX_ATTEMPTS, accept: str = ACCEPT, timeout_s: float = TIMEOUT_S) -> dict:
    """Bounded retry on transient classes only. -> {state, body, attempts[], http_status, url}."""
    http = http or _urllib_get
    attempts = []
    for n in range(1, max_attempts + 1):
        try:
            status, ctype, body = http(url, {"Accept": accept, "User-Agent": "ISA-framework ecb_fx/" + METHOD_VERSION},
                                       timeout_s)
            if status == 200:
                attempts.append({"n": n, "state": OK, "http": 200})
                return {"state": OK, "body": body, "content_type": ctype, "attempts": attempts,
                        "http_status": 200, "url": url}
            st = "HTTP_429" if status == 429 else (S_HTTP5XX if status >= 500 else S_HTTP4XX)
        except Exception as exc:                                        # noqa: BLE001
            st, status = _classify(exc), None
        attempts.append({"n": n, "state": st, "http": status})
        if st not in TRANSIENT or n == max_attempts:
            return {"state": st, "body": None, "attempts": attempts, "http_status": status, "url": url,
                    "why": "%s after %d attempt(s) - deterministic failure, no fallback provider" % (st, n)}
        sleep(BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)])
    return {"state": S_NETWORK, "body": None, "attempts": attempts, "url": url}   # unreachable


# ── parsing / validation ─────────────────────────────────────────────────────────────────────
def parse_api_csv(body, ccys, start, end) -> dict:
    """-> {state, obs: {CCY: {date_iso: rate}}, why}. Strict: schema, dimensions, requested
    currencies EXACTLY, dates inside [start, end], no duplicates, numeric and positive rates."""
    want = set(api_currencies(ccys))
    if not body:
        return {"state": S_EMPTY, "obs": {}, "why": "empty response body"}
    try:
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as exc:                                            # noqa: BLE001
        return {"state": S_MALFORMED, "obs": {}, "why": "CSV unreadable: %s" % exc}
    if not rows:
        return {"state": S_EMPTY, "obs": {}, "why": "CSV has a header but no observations"}
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing_cols:
        return {"state": S_SCHEMA, "obs": {}, "why": "missing columns %s" % missing_cols}
    s, e = _d(start), _d(end)
    obs: Dict[str, Dict[str, float]] = {}
    for r in rows:
        if (r["FREQ"], r["CURRENCY_DENOM"], r["EXR_TYPE"], r["EXR_SUFFIX"]) != ("D", "EUR", "SP00", "A"):
            return {"state": S_SCHEMA, "obs": {}, "why": "unexpected series dimensions %s" % r["KEY"]}
        c, dt = r["CURRENCY"], _d(r["TIME_PERIOD"])
        if c not in want:
            return {"state": S_SCHEMA, "obs": {}, "why": "unrequested currency %s returned" % c}
        if dt is None:
            return {"state": S_MALFORMED, "obs": {}, "why": "unparseable TIME_PERIOD %r" % r["TIME_PERIOD"]}
        if (s and dt < s) or (e and dt > e):
            return {"state": S_LEAK, "obs": {}, "why": "%s %s outside [%s, %s]" % (c, dt, s, e)}
        try:
            v = float(r["OBS_VALUE"])
        except (TypeError, ValueError):
            return {"state": S_NONNUM, "obs": {}, "why": "%s %s value %r" % (c, dt, r["OBS_VALUE"])}
        if not math.isfinite(v) or v <= 0:
            return {"state": S_NONPOS if math.isfinite(v) else S_NONNUM, "obs": {},
                    "why": "%s %s value %r" % (c, dt, v)}
        day = obs.setdefault(c, {})
        if dt.isoformat() in day:
            return {"state": S_DUP, "obs": {}, "why": "duplicate %s %s" % (c, dt)}
        day[dt.isoformat()] = v
    absent = sorted(want - set(obs))
    if absent:
        return {"state": S_MISSING_CCY, "obs": obs, "why": "requested but not returned: %s" % absent}
    return {"state": OK, "obs": obs}


def parse_static_hist(zbytes, ccys, start, end) -> dict:
    """The ECB's own static eurofxref-hist.zip (same authority, different transport): columns are
    'Date' plus one per currency, units of CCY per EUR. Parsed into the SAME obs structure."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zbytes))
        text = z.read(z.namelist()[0]).decode("utf-8")
    except Exception as exc:                                            # noqa: BLE001
        return {"state": S_MALFORMED, "obs": {}, "why": "static zip unreadable: %s" % exc}
    want = api_currencies(ccys)
    rd = csv.DictReader(io.StringIO(text))
    cols = [c.strip() for c in (rd.fieldnames or [])]
    absent = [c for c in want if c not in cols]
    if "Date" not in cols or absent:
        return {"state": S_MISSING_CCY if absent else S_SCHEMA, "obs": {}, "why": "static columns lack %s" % (absent or "Date")}
    s, e = _d(start), _d(end)
    obs: Dict[str, Dict[str, float]] = {c: {} for c in want}
    for r in rd:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        dt = _d(r.get("Date"))
        if dt is None or (s and dt < s) or (e and dt > e):
            continue
        for c in want:
            v = r.get(c)
            if v in ("", "N/A", None):
                continue
            try:
                f = float(v)
            except ValueError:
                return {"state": S_NONNUM, "obs": {}, "why": "static %s %s %r" % (c, dt, v)}
            if f <= 0:
                return {"state": S_NONPOS, "obs": {}, "why": "static %s %s %r" % (c, dt, f)}
            obs[c][dt.isoformat()] = f
    return {"state": OK, "obs": obs}


# ── arithmetic ───────────────────────────────────────────────────────────────────────────────
def rate_per_eur(obs, ccy, date_iso) -> Optional[float]:
    if ccy == "EUR":
        return 1.0                      # the ECB denominator: 1 EUR per EUR by definition
    return (obs.get(ccy) or {}).get(date_iso)


def gbp_per(obs, ccy, date_iso) -> Optional[float]:
    """GBP per 1 CCY on one ECB observation date. GBP->GBP is exactly 1.0 (no conversion)."""
    if ccy == BASE:
        return 1.0
    rg, rc = rate_per_eur(obs, BASE, date_iso), rate_per_eur(obs, ccy, date_iso)
    if rg is None or rc is None:
        return None
    return rg / rc


def cross(obs, from_ccy, to_ccy, date_iso) -> Optional[float]:
    """Units of TO per 1 FROM on one date: R_TO / R_FROM (both per EUR)."""
    if from_ccy == to_ccy:
        return 1.0
    a, b = rate_per_eur(obs, from_ccy, date_iso), rate_per_eur(obs, to_ccy, date_iso)
    return None if a is None or b is None else b / a


def asof_date(obs, ccys, as_of) -> dict:
    """Latest ECB date <= as_of on which EVERY currency in `ccys` (and GBP) is published."""
    a = _d(as_of)
    need = [c for c in api_currencies(ccys)]
    dates = None
    for c in need:
        ds = {d for d in (obs.get(c) or {}) if _d(d) <= a}
        dates = ds if dates is None else dates & ds
    if not dates:
        return {"state": UNAVAILABLE, "why": "no ECB observation on or before %s for %s" % (a, need)}
    best = max(dates)
    lag = (a - _d(best)).days
    if lag > MAX_ASOF_LAG_DAYS:
        return {"state": STALE, "fx_observation_date": best, "lag_days": lag,
                "why": "latest ECB observation %s is %d days before %s (> %d)" % (best, lag, a, MAX_ASOF_LAG_DAYS)}
    return {"state": OK, "requested_as_of": a.isoformat(), "fx_observation_date": best, "lag_days": lag}


def weekly_crosses(obs, ccy, as_of) -> List[tuple]:
    """(iso_year, iso_week, date, GBP_per_CCY) - the LAST observation of each ISO week, <= as_of."""
    a = _d(as_of)
    last = {}
    base_dates = set(obs.get(BASE) or {})
    # EUR is the ECB numeraire (R_EUR = 1 on every published date), so its dates are the GBP dates.
    ccy_dates = base_dates if ccy == "EUR" else set(obs.get(ccy) or {})
    for d in sorted(ccy_dates & base_dates):
        dd = _d(d)
        if dd > a:
            continue
        x = gbp_per(obs, ccy, d)
        if x is None or not (x > 0):
            continue
        y, w, _ = dd.isocalendar()
        last[(y, w)] = (y, w, d, x)          # sorted ascending -> the last write is the week's last obs
    return [last[k] for k in sorted(last)]


def realised_vol(weekly, n_returns) -> dict:
    need = n_returns + 1
    if len(weekly) < need:
        return {"state": UNAVAILABLE, "n_obs": len(weekly), "n_needed": need,
                "why": "only %d weekly observations (< %d) - window NOT shortened" % (len(weekly), need)}
    w = weekly[-need:]
    rets = [math.log(w[i][3] / w[i - 1][3]) for i in range(1, len(w))]
    sd = statistics.stdev(rets)                                           # ddof = 1
    return {"state": OK, "sigma_ann": sd * math.sqrt(52.0), "n_obs": len(w), "n_returns": len(rets),
            "first_week": w[0][2], "last_week": w[-1][2]}


def one_sigma(sigma_ann) -> dict:
    return {"favourable_fx_return": math.exp(sigma_ann) - 1.0, "adverse_fx_return": math.exp(-sigma_ann) - 1.0}


def gbp_return(r_local, r_fx) -> float:
    """1 + R_GBP = (1 + R_local)(1 + R_FX) - fractions, multiplicative, never added."""
    return (1.0 + r_local) * (1.0 + r_fx) - 1.0


# ── the producer ─────────────────────────────────────────────────────────────────────────────
def build_artefact(obs, ccys, as_of, *, route, request, retrieved_at, body_sha) -> dict:
    foreign = sorted({c for c in ccys if c and c != BASE})
    ad = asof_date(obs, foreign, as_of)
    art = {"schema": "fx_pit/1.0", "provider": PROVIDER, "dataflow": DATAFLOW, "method_version": METHOD_VERSION,
           "acquisition_route": route, "request": request, "retrieved_at": retrieved_at,
           "response_sha256": body_sha, "requested_as_of": str(_d(as_of)), "base_currency": BASE,
           "rate_convention": "ECB: units of CCY per 1 EUR; GBP_per_CCY = R_GBP / R_CCY",
           "valuation_not_execution": "ECB reference rates; broker records remain the execution-FX authority",
           "state": ad["state"], "asof": ad, "currencies": {},
           "volatility_rule": ("ISO-week last observation <= as_of; weekly log returns; 1y = 53 obs/52 returns, "
                               "3y = 157 obs/156 returns; sample stdev ddof=1 x sqrt(52); not blended; "
                               "INFORMATIONAL only"),
           "raw_eur_rates": {c: dict(sorted((obs.get(c) or {}).items())) for c in api_currencies(foreign)}}
    for c in foreign:
        rec = {"ccy": c}
        if ad["state"] == OK:
            d = ad["fx_observation_date"]
            g = gbp_per(obs, c, d)
            rec.update(state=OK if g else UNAVAILABLE, fx_observation_date=d, eur_rate=rate_per_eur(obs, c, d),
                       gbp_eur_rate=rate_per_eur(obs, BASE, d), gbp_per_ccy=g, ccy_per_gbp=(1.0 / g if g else None))
        else:
            rec.update(state=ad["state"], why=ad.get("why"))
        wk = weekly_crosses(obs, c, as_of)
        for k, n in WINDOWS.items():
            v = realised_vol(wk, n)
            if v["state"] == OK:
                v.update(one_sigma(v["sigma_ann"]))
            rec["vol_" + k] = v
        art["currencies"][c] = rec
    art["fingerprint"] = hashlib.sha256(json.dumps({k: v for k, v in art.items() if k != "retrieved_at"},
                                                   sort_keys=True, default=str).encode()).hexdigest()
    return art


def produce(ccys, as_of, *, month_label=None, root=None, http=None, sleep=time.sleep, write=True,
            allow_static_contingency=True, refresh=False, timeout_s=TIMEOUT_S, max_attempts=MAX_ATTEMPTS,
            static_max_attempts=MAX_ATTEMPTS) -> dict:
    """ONE canonical, idempotent acquisition for the run. Never raises. A SHADOW consumer only.
    `timeout_s`/`max_attempts` let the pre-run bound its worst case inside one orchestrator call;
    they are recorded in the request contract. Unsupported currencies are typed, not requested."""
    _fi_mark("ecb_fx", "produce")
    root = root or HERE
    a = _d(as_of)
    out_path = os.path.join(root, "fx_pit_%s.json" % (month_label or a.strftime("%b_%Y").lower()))
    unsupported = sorted({c for c in ccys if c and c not in ECB_SUPPORTED})
    ccys = sorted({c for c in ccys if c and c in ECB_SUPPORTED})
    try:
        if not refresh and os.path.exists(out_path):
            prev = json.load(open(out_path, encoding="utf-8"))
            if (prev.get("requested_as_of") == a.isoformat() and prev.get("state") == OK
                    and set(prev.get("currencies", {})) >= ({c for c in ccys if c and c != BASE} | set(unsupported))):
                return dict(prev, reused=True)
        start = a - datetime.timedelta(days=HISTORY_DAYS)
        url = build_url(ccys, start, a)
        f = fetch(url, http=http, sleep=sleep, max_attempts=max_attempts, timeout_s=timeout_s)
        request = {"url": url, "series_key": SERIES_TEMPLATE.format(ccys="+".join(api_currencies(ccys))),
                   "startPeriod": start.isoformat(), "endPeriod": a.isoformat(), "format": FORMAT_PARAM,
                   "accept": ACCEPT, "timeout_s": timeout_s, "max_attempts": max_attempts, "attempts": f["attempts"],
                   "unsupported_not_requested": unsupported}
        route = "ECB_SDMX_REST_API"
        parsed = parse_api_csv(f["body"], ccys, start, a) if f["state"] == OK else {"state": f["state"], "obs": {},
                                                                                    "why": f.get("why")}
        body = f.get("body") or b""
        if parsed["state"] != OK and allow_static_contingency:
            api_failure = {"state": parsed["state"], "why": parsed.get("why")}
            s = fetch(STATIC_URL, http=http, sleep=sleep, accept="application/zip",
                      max_attempts=static_max_attempts, timeout_s=timeout_s)
            if s["state"] == OK:
                p2 = parse_static_hist(s["body"], ccys, start, a)
                if p2["state"] == OK:
                    parsed, body, route = p2, s["body"], "ECB_STATIC_EUROFXREF_HIST_CONTINGENCY"
                    request["contingency"] = {"url": STATIC_URL, "api_failure": api_failure,
                                              "attempts": s["attempts"]}
        if parsed["state"] != OK:
            art = {"schema": "fx_pit/1.0", "provider": PROVIDER, "dataflow": DATAFLOW, "method_version": METHOD_VERSION,
                   "state": parsed["state"], "why": parsed.get("why"), "request": request,
                   "requested_as_of": a.isoformat(), "currencies": {}, "acquisition_route": None}
        else:
            art = build_artefact(parsed["obs"], ccys, a, route=route, request=request,
                                 retrieved_at=datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                 body_sha=hashlib.sha256(body).hexdigest())
        for c in unsupported:
            art["currencies"][c] = {"ccy": c, "state": S_UNSUPPORTED,
                                    "why": "%s is not an ECB reference-rate currency - FX_UNAVAILABLE, "
                                           "no other provider, never 1.0" % c}
        if unsupported and art.get("fingerprint"):
            art["fingerprint"] = hashlib.sha256(json.dumps({k: v for k, v in art.items()
                                                            if k not in ("retrieved_at", "fingerprint")},
                                                           sort_keys=True, default=str).encode()).hexdigest()
        if write:
            tmp = out_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(art, fh, indent=1, sort_keys=True, default=str)
            os.replace(tmp, out_path)
        return art
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc), "provider": PROVIDER}


def pair_rates(art, pairs) -> dict:
    """{'<FROM><TO>': units of TO per 1 FROM} at the artefact's as-of observation date, for the
    horizon-value quote->reporting conversion. Missing -> absent (the consumer types FX_UNAVAILABLE)."""
    if not art or art.get("state") != OK:
        return {}
    d = art["asof"]["fx_observation_date"]
    raw = art.get("raw_eur_rates") or {}
    out = {}
    for p in pairs:
        f, t = p[:3], p[3:]
        a = 1.0 if f == "EUR" else (raw.get(f) or {}).get(d)
        b = 1.0 if t == "EUR" else (raw.get(t) or {}).get(d)
        if a and b:
            out[p] = b / a
    return out


def vol_for(art, ccy) -> dict:
    """{'1y': sigma or None, '3y': sigma or None} for CCY->GBP; GBP -> zero FX exposure."""
    if ccy == BASE:
        return {"1y": 0.0, "3y": 0.0}
    rec = ((art or {}).get("currencies") or {}).get(ccy) or {}
    return {k: ((rec.get("vol_" + k) or {}).get("sigma_ann") if (rec.get("vol_" + k) or {}).get("state") == OK else None)
            for k in WINDOWS}


# ─────────────────────────────────────────────────────────────── selftest (offline) ──────
def _csv(rows):
    head = ",".join(REQUIRED_COLUMNS + ("TITLE",))
    lines = [head] + [",".join(["EXR.D.%s.EUR.SP00.A" % c, "D", c, "EUR", "SP00", "A", d, str(v), "t"]) for c, d, v in rows]
    return ("\n".join(lines) + "\n").encode()


def _selftest(verbose=True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -- " + str(detail)[:200]))
        if not cond:
            fails.append(name)
    S, E = "2026-09-01", "2026-09-30"
    ok("URL contract: explicit series key, bounds and pinned csvdata format",
       build_url(["USD", "GBP", "EUR"], S, E) ==
       API_BASE + "D.GBP+USD.EUR.SP00.A?startPeriod=2026-09-01&endPeriod=2026-09-30&format=csvdata")
    good = _csv([("GBP", "2026-09-24", 0.86), ("USD", "2026-09-24", 1.10)])
    p = parse_api_csv(good, ["USD"], S, E)
    ok("parse: a valid response is OK and typed", p["state"] == OK and p["obs"]["USD"]["2026-09-24"] == 1.10)
    neg = {
        "empty body": (b"", S_EMPTY),
        "header only": (_csv([]), S_EMPTY),
        "missing column": (b"KEY,FREQ\nx,D\n", S_SCHEMA),
        "wrong frequency": (good.replace(b",D,GBP", b",M,GBP"), S_SCHEMA),
        "wrong exr type": (good.replace(b"SP00,A,2026-09-24,0.86", b"SP01,A,2026-09-24,0.86"), S_SCHEMA),
        "missing requested currency (partial)": (_csv([("GBP", "2026-09-24", 0.86)]), S_MISSING_CCY),
        "unrequested currency": (_csv([("GBP", "2026-09-24", 0.86), ("USD", "2026-09-24", 1.1), ("JPY", "2026-09-24", 160.0)]), S_SCHEMA),
        "duplicate observation": (_csv([("GBP", "2026-09-24", 0.86), ("GBP", "2026-09-24", 0.87), ("USD", "2026-09-24", 1.1)]), S_DUP),
        "non-numeric rate": (_csv([("GBP", "2026-09-24", "abc"), ("USD", "2026-09-24", 1.1)]), S_NONNUM),
        "zero rate": (_csv([("GBP", "2026-09-24", 0.0), ("USD", "2026-09-24", 1.1)]), S_NONPOS),
        "negative rate": (_csv([("GBP", "2026-09-24", -0.86), ("USD", "2026-09-24", 1.1)]), S_NONPOS),
        "observation outside the requested range": (_csv([("GBP", "2026-10-01", 0.86), ("USD", "2026-09-24", 1.1)]), S_LEAK),
        "unparseable date": (_csv([("GBP", "24/09/2026", 0.86), ("USD", "2026-09-24", 1.1)]), S_MALFORMED),
    }
    for name, (body, want) in neg.items():
        ok("NEGATIVE CONTROL parse: %s -> %s" % (name, want), parse_api_csv(body, ["USD"], S, E)["state"] == want,
           parse_api_csv(body, ["USD"], S, E))
    # transport / retry
    calls = []

    def seq(*outcomes):
        it = iter(outcomes)

        def h(url, headers, timeout):
            calls.append(url)
            o = next(it)
            if isinstance(o, Exception):
                raise o
            return o
        return h
    sl = []
    r = fetch("u", http=seq(TimeoutError("timed out"), TimeoutError("t"), TimeoutError("t")), sleep=sl.append)
    ok("MUST-FIRE retry: timeout x3 -> TIMEOUT after exactly MAX_ATTEMPTS (bounded, 2 sleeps)",
       r["state"] == S_TIMEOUT and len(r["attempts"]) == 3 and len(sl) == 2)
    r = fetch("u", http=seq((500, "", b""), (503, "", b""), (200, "text/csv", b"x")), sleep=lambda s: None)
    ok("retry: transient 5xx then 200 -> OK on attempt 3", r["state"] == OK and len(r["attempts"]) == 3)
    r = fetch("u", http=seq((404, "", b"")), sleep=lambda s: None)
    ok("NEGATIVE CONTROL: HTTP 4xx is NOT retried -> HTTP_4XX after 1 attempt", r["state"] == S_HTTP4XX and len(r["attempts"]) == 1)
    r = fetch("u", http=seq(OSError("Name or service not known"), OSError("x"), OSError("x")), sleep=lambda s: None)
    ok("MUST-FIRE: DNS/connection failure -> NETWORK_ERROR (bounded)", r["state"] == S_NETWORK and len(r["attempts"]) == 3)
    r = fetch("u", http=seq((500, "", b""), (500, "", b""), (500, "", b"")), sleep=lambda s: None)
    ok("MUST-FIRE: persistent 5xx -> deterministic HTTP_5XX, no fallback provider", r["state"] == S_HTTP5XX and r["body"] is None)
    # arithmetic
    obs = {"GBP": {"2026-09-24": 0.86}, "USD": {"2026-09-24": 1.10}, "CHF": {"2026-09-24": 0.94}}
    g = gbp_per(obs, "USD", "2026-09-24")
    ok("identity: GBP_per_USD = R_GBP / R_USD", abs(g - 0.86 / 1.10) < 1e-15)
    ok("identity: GBP_per_CCY x CCY_per_GBP = 1", abs(g * (1.0 / g) - 1.0) < 1e-15)
    ok("identity: GBP -> GBP is exactly 1.0 (no conversion), EUR -> GBP = R_GBP",
       gbp_per(obs, "GBP", "2026-09-24") == 1.0 and gbp_per(obs, "EUR", "2026-09-24") == 0.86)
    ok("NEGATIVE CONTROL: a currency with no rate is None - never 0 or 1", gbp_per(obs, "ZAR", "2026-09-24") is None)
    ok("cross: CHF per USD = R_CHF / R_USD", abs(cross(obs, "USD", "CHF", "2026-09-24") - 0.94 / 1.10) < 1e-15)
    ok("GBP return is multiplicative: 10% local, +5% FX -> 15.5%", abs(gbp_return(0.10, 0.05) - 0.155) < 1e-12)
    # PIT
    days = {"2026-04-01": 0.86, "2026-04-02": 0.861, "2026-04-07": 0.862,     # Good Friday 3rd / Easter Monday 6th closed
            "2026-09-18": 0.859, "2026-09-21": 0.858, "2026-09-24": 0.857, "2026-09-25": 0.856}
    pit = {"GBP": dict(days), "USD": {k: 1.1 for k in days}}
    ok("PIT working day uses that day", asof_date(pit, ["USD"], "2026-09-24")["fx_observation_date"] == "2026-09-24")
    sat = asof_date(pit, ["USD"], "2026-09-19")
    ok("PIT Saturday uses Friday (lag 1) - never the following Monday",
       sat["fx_observation_date"] == "2026-09-18" and sat["lag_days"] == 1)
    ok("PIT TARGET holiday (Easter Monday 06-Apr) uses Thursday 02-Apr, never 07-Apr",
       asof_date(pit, ["USD"], "2026-04-06")["fx_observation_date"] == "2026-04-02")
    ok("PIT a date before the latest observation never selects a future one",
       asof_date(pit, ["USD"], "2026-09-23")["fx_observation_date"] == "2026-09-21")
    ok("MUST-FIRE PIT: > MAX_ASOF_LAG_DAYS -> FX_STALE", asof_date(pit, ["USD"], "2026-09-12")["state"] == STALE)
    ok("MUST-FIRE PIT: nothing on/before as_of -> FX_UNAVAILABLE", asof_date(pit, ["USD"], "2026-03-01")["state"] == UNAVAILABLE)
    # volatility - independent derivation
    import random
    rnd = random.Random(7)
    x, t0, gbp, usd = 1.0, datetime.date(2023, 1, 2), {}, {}
    wk_truth = []
    for w in range(170):
        for dd in range(5):                    # Mon..Fri; the Friday value closes the ISO week
            day = t0 + datetime.timedelta(days=7 * w + dd)
            v = x * math.exp(rnd.gauss(0, 0.004))
            gbp[day.isoformat()], usd[day.isoformat()] = 0.86, 0.86 / v
        wk_truth.append(v)
        x = v
    vobs = {"GBP": gbp, "USD": usd}
    asof_v = (t0 + datetime.timedelta(days=7 * 169 + 4)).isoformat()
    wk = weekly_crosses(vobs, "USD", asof_v)
    ok("ISO-week sampling: one observation per week, the LAST (Friday) one", len(wk) == 170 and
       all(abs(wk[i][3] - wk_truth[i]) < 1e-12 for i in range(170)))
    for k, n in WINDOWS.items():
        v = realised_vol(wk, n)
        ind = [math.log(wk_truth[i] / wk_truth[i - 1]) for i in range(170 - n, 170)]
        m = sum(ind) / len(ind)
        sd = math.sqrt(sum((q - m) ** 2 for q in ind) / (len(ind) - 1)) * math.sqrt(52)
        ok("vol %s: %d returns, ddof=1 x sqrt(52), equals an independent derivation" % (k, n),
           v["n_returns"] == n and abs(v["sigma_ann"] - sd) < 1e-12, (v, sd))
    # EUR is the numeraire: ECB publishes no EUR series, its dates are the GBP dates (found by the
    # 24-Sep live run - 8 EUR-quoted names were silently vol-UNAVAILABLE before this fix)
    eobs = {"GBP": {d: 0.86 / u for d, u in usd.items()}}     # GBP_per_EUR = R_GBP -> equals wk_truth
    wke = weekly_crosses(eobs, "EUR", asof_v)
    ok("MUST-FIRE EUR vol: numeraire dates = GBP dates; weekly GBP_per_EUR = R_GBP",
       len(wke) == 170 and all(abs(wke[i][3] - wk_truth[i]) < 1e-12 for i in range(170)))
    ok("MUST-FIRE EUR vol: 1y and 3y computed (not UNAVAILABLE)",
       all(realised_vol(wke, n)["state"] == OK for n in WINDOWS.values()))
    ok("MUST-FIRE vol: insufficient history -> UNAVAILABLE, the window is NOT shortened",
       realised_vol(wk[:100], 156)["state"] == UNAVAILABLE)
    s1 = one_sigma(0.08)
    ok("one-sigma: favourable exp(+s)-1, adverse exp(-s)-1",
       abs(s1["favourable_fx_return"] - (math.exp(0.08) - 1)) < 1e-15 and abs(s1["adverse_fx_return"] - (math.exp(-0.08) - 1)) < 1e-15)
    # the producer (mocked transport) - idempotence, contingency, typed failure
    import tempfile
    rows = []
    for d, v in usd.items():
        rows += [("GBP", d, 0.86), ("USD", d, v)]
    body = _csv(rows)

    def api_ok(url, headers, timeout):
        return 200, "text/csv", body
    with tempfile.TemporaryDirectory() as td:
        a1 = produce(["USD", "GBP"], asof_v, month_label="t", root=td, http=api_ok)
        ok("produce: OK artefact with route, request contract, PIT date, crosses and both vol windows",
           a1["state"] == OK and a1["acquisition_route"] == "ECB_SDMX_REST_API"
           and a1["currencies"]["USD"]["vol_3y"]["state"] == OK and a1["request"]["format"] == "csvdata", a1.get("why"))
        a2 = produce(["USD", "GBP"], asof_v, month_label="t", root=td, http=lambda *a: (_ for _ in ()).throw(AssertionError("refetched")))
        ok("produce is idempotent: a valid artefact for the same as_of is re-used, not re-fetched", a2.get("reused") is True)
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w") as z:
            z.writestr("eurofxref-hist.csv", "Date,USD,GBP,\n" + "".join("%s,%s,0.86,\n" % (d, v) for d, v in sorted(usd.items(), reverse=True)))

        def api_down_static_up(url, headers, timeout):
            return (503, "", b"") if "data-api" in url else (200, "application/zip", zb.getvalue())
        a3 = produce(["USD"], asof_v, month_label="c", root=td, http=api_down_static_up, sleep=lambda s: None)
        ok("contingency: API 5xx -> the ECB's OWN static file, route TYPED and the API failure recorded",
           a3["state"] == OK and a3["acquisition_route"] == "ECB_STATIC_EUROFXREF_HIST_CONTINGENCY"
           and a3["request"]["contingency"]["api_failure"]["state"] == S_HTTP5XX)
        ok("contingency values are identical to the API values for the same dates",
           a3["currencies"]["USD"]["gbp_per_ccy"] == a1["currencies"]["USD"]["gbp_per_ccy"])
        a4 = produce(["USD"], asof_v, month_label="f", root=td, http=lambda *a: (500, "", b""), sleep=lambda s: None)
        ok("MUST-FIRE: API and static both down -> typed failure, no rates, no other provider",
           a4["state"] == S_HTTP5XX and a4["currencies"] == {} and a4["acquisition_route"] is None)
        a5 = produce(["USD"], asof_v, month_label="n", root=td, http=lambda *a: (503, "", b""), sleep=lambda s: None,
                     allow_static_contingency=False)
        ok("contingency is explicit: disabled -> the API failure stands", a5["state"] == S_HTTP5XX)
    ok("pair_rates: USD->ZAR style cross = R_TO/R_FROM; missing -> absent (never 1.0)",
       pair_rates({"state": OK, "asof": {"fx_observation_date": "d"}, "raw_eur_rates": {"USD": {"d": 1.1}, "ZAR": {"d": 19.8}}},
                  ["USDZAR", "USDNOK"]) == {"USDZAR": 19.8 / 1.1})
    # unsupported currency: typed, not requested, does not fail the supported ones
    seen = []
    def _h_uns(url, headers, timeout):
        seen.append((url, timeout))
        return 200, "text/csv", _csv([("GBP", "2026-09-18", "0.86"), ("USD", "2026-09-18", "1.17")])
    with tempfile.TemporaryDirectory() as td:
        u = produce(["USD", "ARS", "GBP"], "2026-09-18", root=td, http=_h_uns, sleep=lambda s: None,
                    month_label="t_uns", timeout_s=7.0, max_attempts=2)
    ok("MUST-FIRE unsupported currency: typed UNSUPPORTED_CURRENCY, never requested, USD still OK",
       u.get("state") == OK and u["currencies"]["ARS"]["state"] == S_UNSUPPORTED
       and u["currencies"]["USD"]["state"] == OK and "ARS" not in seen[0][0], (u.get("state"), seen[:1]))
    ok("run budget: timeout_s / max_attempts are honoured and recorded in the request contract",
       seen[0][1] == 7.0 and u["request"]["timeout_s"] == 7.0 and u["request"]["max_attempts"] == 2
       and u["request"]["unsupported_not_requested"] == ["ARS"])
    ok("pair_rates: an unsupported currency is ABSENT (the consumer types FX_UNAVAILABLE)",
       "ARSGBP" not in pair_rates(u, ["ARSGBP"]) and vol_for(u, "ARS") == {"1y": None, "3y": None})
    print("ecb_fx selftest: %d FAIL(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
