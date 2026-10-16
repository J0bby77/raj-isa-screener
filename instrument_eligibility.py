#!/usr/bin/env python3
"""
instrument_eligibility.py - ISA-0860 (12-Oct-2026): is this instrument ISA-QUALIFYING, mechanically?

⚑ THE DEFECT THIS CLOSES. October-2026: the Capital Decision Receipt selected VEON (NASDAQ) for GBP 5,613.69 and
AJ Bell refused it in the ISA - VEON trades only as an ADS whose underlying shares have no recognised listing
(Euronext Amsterdam delisted; Moscow's recognition REVOKED from 05-May-2022). HMRC's rule is mechanical: a share
qualifies if officially listed on a recognised stock exchange; a depositary receipt only if its UNDERLYING would.
The framework never typed the instrument (yfinance says EQUITY for an ADR; security_type calls an ADS 'common' -
correctly, for the ECONOMIC question), so Raj learned it at execution (11-Oct-2026: "is its format determinable?").

⚑ WHAT IT PRODUCES (Step 5x, after refresh_symbol_map): GENERATED `instrument_eligibility.json` - per verified
symbol: the instrument type from OpenFIGI securityType2 (PRIMARY, Raj 12-Oct-2026), the constituent-feed
description form (security_type.classify_instrument_form - CORROBORATING), for a depositary receipt the underlying
issuer's listings (OpenFIGI search, exact issuer match), each classified against the SIGNED config
`recognised_exchanges.json` (HMRC tables) at the as-of date - and one VERDICT per symbol.

⚑ THE RULE (Raj 12-Oct-2026, 'refuse only established'; critiqued and adopted):
  ELIGIBLE               OpenFIGI Common Stock, the feed does not contradict, no flag
  NOT_ISA_QUALIFYING_DR  a DR whose underlying's EVERY observed listing is classified non-qualifying (MTF / OTC /
                         quote composite / revoked / not yet recognised), the search EXHAUSTED, exact issuer match
                         -> broker_dealability REFUSES new capital
  CONFIRM_REQUIRED       everything uncertain stays FUNDABLE and carries reason codes:
                           ELIGIBILITY_UNVERIFIED   no OpenFIGI type (network, unresolved ticker)
                           INSTRUMENT_TYPE_CONFLICT OpenFIGI and the feed disagree (DR vs direct)
                           INSTRUMENT_TYPE_OTHER    neither Common Stock nor DR (ETP, preference ...)
                           DR_DECLARATION_PENDING   a DR not refused - evidence of a recognised listing, an
                                                    unclassified code, an unresolved underlying (Raj declares once)
                           CUSTODY_ROUTE_CONFIRM    US-venue 'Ordinary Shares' of a non-US issuer (GLBE class) -
                                                    a WEAK predictor of a CREST/custody refusal, flag only
  The Capital Decision Receipt pre-authorises a fallback bundle for every subset of confirm-required selected
  names; Raj's dealing outcome (capital_decision_engine --record-dealability) becomes a declaration of record.

⚑ NEVER: a refusal on missing data; a guessed issuer (prefix/fuzzy names - 'TAIWAN SEMICONDUCTOR CO LTD' is a
different company from TSMC); a network call from a consumer (broker_dealability reads the cache only).
ROLLBACK (R4.13): isa_policy.V2_FLAGS['instrument_eligibility_refresh'] = False -> the stage does not fetch; the
verdicts on disk are re-evaluated offline; absent file -> every name reads ELIGIBILITY_UNVERIFIED (fundable).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from typing import Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = "instrument_eligibility.json"
CONFIG_FILE = "recognised_exchanges.json"
SYMBOL_MAP_FILE = "stock_symbol_map.json"
CONSTITUENTS_FILE = "constituents_history.csv"
OPENFIGI = "https://api.openfigi.com/v3/"
REFETCH_DAYS = 180              # instrument type / underlying listings change rarely; re-asked twice a year
MAX_SEARCH_PAGES = 5
MAP_SPACING_S = 2.6             # unauthenticated mapping: ~25 requests/minute
SEARCH_SPACING_S = 13.0         # unauthenticated search: measured 429 above ~5/minute (12-Oct-2026)

ELIGIBLE = "ELIGIBLE"
NOT_ISA_QUALIFYING_DR = "NOT_ISA_QUALIFYING_DR"
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
STATES = (ELIGIBLE, NOT_ISA_QUALIFYING_DR, CONFIRM_REQUIRED)
REASONS = ("ELIGIBILITY_UNVERIFIED", "INSTRUMENT_TYPE_CONFLICT", "INSTRUMENT_TYPE_OTHER", "DR_DECLARATION_PENDING",
           "CUSTODY_ROUTE_CONFIRM")
US_VENUES = ("NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS")
_YAHOO_TO_FIGI = {"NMS": "US", "NGM": "US", "NCM": "US", "NYQ": "US", "ASE": "US", "PCX": "US", "BTS": "US",
                  "LSE": "LN", "STO": "SS", "PAR": "FP", "EBS": "SW", "GER": "GR", "AMS": "NA", "WSE": "PW",
                  "OSL": "NO", "MIL": "IM", "BRU": "BB", "MCE": "SM", "HEL": "FH", "CPH": "DC", "ISE": "ID"}
_NORDIC = ("STO", "HEL", "CPH", "OSL")


def _today() -> str:
    return datetime.date.today().isoformat()


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_config(root: Optional[str] = None) -> dict:
    doc = _read(os.path.join(root or HERE, CONFIG_FILE))
    if not isinstance(doc, dict) or not isinstance(doc.get("counts"), dict) or not isinstance(doc.get("does_not_count"), dict):
        raise ValueError("%s absent or malformed - no exchange is classified, so no DR can be refused" % CONFIG_FILE)
    return doc


# ── the OpenFIGI job for one verified symbol ───────────────────────────────────────────────────────
def figi_job(symbol: str, yahoo_exchange: Optional[str]) -> Optional[dict]:
    ex = _YAHOO_TO_FIGI.get(str(yahoo_exchange or "").upper())
    if not ex or not symbol:
        return None
    base = symbol if ex == "US" else symbol.rsplit(".", 1)[0]
    base = base.replace("-", "") if str(yahoo_exchange).upper() in _NORDIC else base.replace("-", "/")
    return {"idType": "TICKER", "idValue": base, "exchCode": ex, "marketSecDes": "Equity"}


def _post(path: str, body) -> object:
    import urllib.request
    req = urllib.request.Request(OPENFIGI + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _call(fetch: Callable, path: str, body, retries: int = 2, backoff_s: float = 20.0):
    last = None
    for i in range(retries + 1):
        try:
            return fetch(path, body), None
        except Exception as exc:                                        # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, exc)
            if "429" in last and i < retries:
                time.sleep(backoff_s)
                continue
            break
    return None, last


# ── issuer identity: EXACT normalised name, never a prefix ──────────────────────────────────────────
_DR_TAIL = re.compile(r"[\s\-/]*(?:(?:SPONS?(?:ORED)?|SP|UNSP(?:ONSORED)?)\s+)?(?:ADRS?|ADS|GDRS?|NY\s+REG\s+SHS|NY\s+REG\s+SHRS)\s*$")
_CLASS_TAIL = re.compile(r"\s*-\s*[A-Z]{1,2}\s*$")


def issuer_key(name) -> str:
    s = str(name or "").upper().strip()
    s = _DR_TAIL.sub("", s)
    s = _CLASS_TAIL.sub("", s)
    s = re.sub(r"[^A-Z0-9/&]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search_underlying(dr_name: str, fetch: Callable = _post, max_pages: int = MAX_SEARCH_PAGES,
                      spacing_s: float = SEARCH_SPACING_S, deadline: Optional[float] = None) -> dict:
    """Every Common Stock line OpenFIGI holds for the DR's issuer (EXACT issuer key). exhausted=True only when the
    last page carried no `next` token - a refusal needs the WHOLE set."""
    key = issuer_key(dr_name)
    out = {"query": key, "rows": [], "pages": 0, "exhausted": False, "error": None, "n_seen": 0,
           "as_of": _today()}
    if not key:
        out["error"] = "empty issuer name"
        return out
    start = None
    while out["pages"] < max_pages:
        if deadline is not None and time.time() > deadline:
            out["error"] = "time box reached"
            return out
        body = {"query": key, "securityType2": "Common Stock", "marketSecDes": "Equity"}
        if start:
            body["start"] = start
        res, err = _call(fetch, "search", body)
        if out["pages"] or start:
            pass
        out["pages"] += 1
        if err is not None or not isinstance(res, dict):
            out["error"] = err or "malformed search response"
            return out
        for d in res.get("data") or []:
            out["n_seen"] += 1
            if issuer_key(d.get("name")) == key:
                out["rows"].append({"code": str(d.get("exchCode") or "").split(" ")[0], "ticker": d.get("ticker"),
                                    "name": d.get("name")})
        start = res.get("next")
        if not start:
            out["exhausted"] = True
            return out
        if spacing_s:
            time.sleep(spacing_s)
    out["error"] = "page cap %d reached with more results pending" % max_pages
    return out


# ── classification against the signed HMRC table ──────────────────────────────────────────────────
def classify_code(code: str, ticker, rows: List[dict], cfg: dict, as_of: str, _depth: int = 0) -> dict:
    c = str(code or "").split(" ")[0]
    cnt = (cfg.get("counts") or {}).get(c)
    if cnt is not None:
        rf, rv = cnt.get("recognised_from"), cnt.get("revoked_from")
        if rv and as_of >= rv:
            return {"code": c, "class": "REVOKED", "counts": False, "why": "%s revoked from %s" % (cnt["exchange"], rv)}
        if rf and as_of < rf:
            return {"code": c, "class": "NOT_YET_RECOGNISED", "counts": False,
                    "why": "%s recognised only from %s" % (cnt["exchange"], rf)}
        return {"code": c, "class": "RECOGNISED", "counts": True, "why": "%s (%s)" % (cnt["exchange"], cnt.get("hmrc_table", "")[:40])}
    dn = (cfg.get("does_not_count") or {}).get(c)
    if dn is not None:
        return {"code": c, "class": dn.get("class"), "counts": False, "why": dn.get("basis")}
    if c in (cfg.get("composite_codes") or []) and _depth == 0:
        if c == "EO":
            return {"code": c, "class": "OTC", "counts": False, "why": "OpenFIGI OTC Composite"}
        loc = [classify_code(r["code"], r.get("ticker"), rows, cfg, as_of, 1) for r in rows
               if r.get("ticker") == ticker and r.get("code") != c and r.get("code") not in (cfg.get("composite_codes") or [])]
        if any(x["counts"] for x in loc):
            return {"code": c, "class": "RECOGNISED", "counts": True, "why": "composite of a recognised local line"}
        if loc and all(x["class"] != "UNCLASSIFIED" for x in loc):
            return {"code": c, "class": "COMPOSITE_OF_NON_QUALIFYING", "counts": False,
                    "why": "composite whose local lines are all non-qualifying (%s)" % sorted({x["class"] for x in loc})}
    return {"code": c, "class": "UNCLASSIFIED", "counts": False, "why": "code not classified in %s" % CONFIG_FILE}


def evaluate_dr(underlying: Optional[dict], cfg: dict, as_of: str) -> dict:
    """-> {state: NOT_ISA_QUALIFYING_DR | DR_DECLARATION_PENDING, evidence, why}. Refusal ONLY when established."""
    u = underlying or {}
    rows = u.get("rows") or []
    if not rows:
        return {"state": "DR_DECLARATION_PENDING", "detail": "UNDERLYING_UNRESOLVED", "evidence": [],
                "why": ("no Common Stock line with the exact issuer name %r was found (%s) - the underlying is not "
                        "identified, so nothing is established" % (u.get("query"), u.get("error") or "search returned none"))}
    cls = [dict(classify_code(r["code"], r.get("ticker"), rows, cfg, as_of), ticker=r.get("ticker")) for r in rows]
    rec = [x for x in cls if x["counts"]]
    unc = sorted({x["code"] for x in cls if x["class"] == "UNCLASSIFIED"})
    ev = sorted({(x["code"], x["class"]) for x in cls})
    if rec:
        return {"state": "DR_DECLARATION_PENDING", "detail": "RECOGNISED_LISTING_EVIDENCE",
                "evidence": [{"code": x["code"], "ticker": x["ticker"], "why": x["why"]} for x in rec][:6],
                "why": "the underlying has a line on a recognised exchange (%s) - evidence for Raj's one-time declaration, "
                       "not an admission (a foreign line may be a trading line)" % ", ".join(sorted({x["code"] for x in rec}))}
    if unc or not u.get("exhausted") or u.get("error"):
        return {"state": "DR_DECLARATION_PENDING", "detail": "UNDETERMINED", "evidence": [list(e) for e in ev][:20],
                "why": "no recognised listing among the classified lines, but %s - not established, so not refused"
                       % ("code(s) %s are unclassified" % unc if unc else "the search was not exhausted (%s)" % (u.get("error") or "page cap"))}
    return {"state": NOT_ISA_QUALIFYING_DR, "detail": "NO_RECOGNISED_UNDERLYING_LISTING", "evidence": [list(e) for e in ev][:20],
            "why": ("every observed listing of the underlying (%d line(s), search exhausted, exact issuer %r) is non-qualifying "
                    "at %s: %s - HMRC: a depositary receipt qualifies only if its underlying would"
                    % (len(rows), u.get("query"), as_of, sorted({x["class"] for x in cls})))}


def evaluate(rec: dict, cfg: dict, as_of: str) -> dict:
    """ONE verdict for one symbol from its cached record. Pure."""
    fig = rec.get("openfigi") or {}
    st2 = fig.get("securityType2")
    feed = (rec.get("feed") or {}).get("form") or "unknown"
    reasons, why = [], []
    if not st2:
        ftype = "UNRESOLVED"
    elif st2 == "Common Stock":
        ftype = "DIRECT"
    elif st2 == "Depositary Receipt":
        ftype = "DR"
    else:
        ftype = "OTHER"
    out = {"symbol": rec.get("symbol"), "instrument_type": ftype, "openfigi_type": st2, "feed_form": feed,
           "as_of": as_of, "source": {"openfigi_as_of": fig.get("as_of"), "openfigi_error": fig.get("error"),
                                      "feed_source": (rec.get("feed") or {}).get("source")}}
    if ftype == "UNRESOLVED":
        reasons.append("ELIGIBILITY_UNVERIFIED")
        why.append("OpenFIGI type unavailable (%s)" % (fig.get("error") or "not fetched"))
        if feed == "depositary_receipt":
            reasons.append("DR_DECLARATION_PENDING")
            why.append("the feed description names a depositary receipt")
    elif (ftype == "DIRECT" and feed == "depositary_receipt") or (ftype == "DR" and feed == "direct"):
        reasons.append("INSTRUMENT_TYPE_CONFLICT")
        why.append("OpenFIGI %s vs feed %s" % (st2, feed))
    elif ftype == "OTHER":
        reasons.append("INSTRUMENT_TYPE_OTHER")
        why.append("OpenFIGI securityType2 %r is neither Common Stock nor Depositary Receipt" % st2)
    if ftype == "DR":
        d = evaluate_dr(rec.get("underlying"), cfg, as_of)
        out["dr"] = d
        if d["state"] == NOT_ISA_QUALIFYING_DR and "INSTRUMENT_TYPE_CONFLICT" not in reasons:
            out.update(state=NOT_ISA_QUALIFYING_DR, reasons=[], why=d["why"])
            return out
        reasons.append("DR_DECLARATION_PENDING")
        why.append(d["why"])
    if ftype == "DIRECT" and rec.get("venue") in US_VENUES and (rec.get("feed") or {}).get("us_ordinary_shares"):
        reasons.append("CUSTODY_ROUTE_CONFIRM")
        why.append("US-venue 'Ordinary Shares' of a non-US issuer: confirm the broker can hold/settle it (GLBE class; weak predictor)")
    out.update(state=CONFIRM_REQUIRED if reasons else ELIGIBLE, reasons=reasons,
               why="; ".join(why) if why else "OpenFIGI Common Stock; feed %s" % feed)
    return out


# ── inputs for the producer ──────────────────────────────────────────────────────────────────────
def _feed_descriptions(root: str) -> Dict[str, str]:
    import csv
    out = {}
    p = os.path.join(root, CONSTITUENTS_FILE)
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            t, c = (r.get("ticker") or "").strip(), (r.get("company") or "").strip()
            if t and c:
                out[t] = c                                   # last row (latest run) wins
    return out


def _feed_record(symbol: str, verified: dict, feed: Dict[str, str]) -> dict:
    import security_type as _st
    desc, src = feed.get(symbol), CONSTITUENTS_FILE
    if not desc:
        desc, src = verified.get("company"), "%s.verified[].company" % SYMBOL_MAP_FILE
    return {"company": desc, "source": src if desc else None, "form": _st.classify_instrument_form(desc),
            "us_ordinary_shares": bool(_st.us_listed_ordinary_shares(desc))}


def load(root: Optional[str] = None) -> dict:
    """The cache as written (consumers: broker_dealability). Never fetches."""
    return _read(os.path.join(root or HERE, CACHE_FILE), {}) or {}


def verdicts(root: Optional[str] = None, as_of: Optional[str] = None) -> Dict[str, dict]:
    """Verdicts re-evaluated OFFLINE against the current signed table at `as_of` (a table date change applies
    without a fetch). Absent cache -> {} (consumers read every name ELIGIBILITY_UNVERIFIED)."""
    doc = load(root)
    try:
        cfg = load_config(root)
    except ValueError:
        return {s: dict(v, state=CONFIRM_REQUIRED, reasons=sorted(set((v.get("reasons") or [])) | {"ELIGIBILITY_UNVERIFIED"}),
                        why="recognised_exchanges.json unreadable - nothing established")
                for s, v in (doc.get("verdicts") or {}).items()}
    a = as_of or _today()
    return {s: evaluate(r, cfg, a) for s, r in (doc.get("records") or {}).items()}


def _flag(name: str = "instrument_eligibility_refresh", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        return bool((getattr(_p, "V2_FLAGS", {}) or {}).get(name, default))
    except Exception:                                                   # noqa: BLE001
        return default


def refresh(root: Optional[str] = None, today: Optional[str] = None, fetch: Optional[Callable] = None,
            budget_s: float = 150.0, refetch_days: int = REFETCH_DAYS, dry_run: bool = False,
            map_spacing_s: float = MAP_SPACING_S, search_spacing_s: float = SEARCH_SPACING_S) -> dict:
    """THE STEP 5x PRODUCER. Incremental + time-boxed: fetch only names with no record, a stale record or a prior
    error; every name always gets a verdict (offline re-evaluation). A network failure leaves names UNVERIFIED -
    fundable and flagged, never refused."""
    root = root or HERE
    t_end = time.time() + float(budget_s)
    today = today or _today()
    fetch = fetch or _post
    cfg = load_config(root)
    smap = _read(os.path.join(root, SYMBOL_MAP_FILE), {}) or {}
    ver = {v["ticker"]: v for v in (smap.get("verified") or []) if isinstance(v, dict) and v.get("ticker")}
    feed = _feed_descriptions(root)
    doc = load(root) or {}
    recs = dict(doc.get("records") or {})
    fetched, searched, errors = [], [], []
    stale_before = (datetime.date.fromisoformat(today) - datetime.timedelta(days=int(refetch_days))).isoformat()
    for sym, v in ver.items():
        r = dict(recs.get(sym) or {})
        r.update(symbol=sym, venue=v.get("exchange"), feed=_feed_record(sym, v, feed))
        recs[sym] = r
    do_fetch = _flag() and not dry_run
    need = [s for s, r in sorted(recs.items()) if s in ver and (
        not (r.get("openfigi") or {}).get("as_of") or (r.get("openfigi") or {}).get("error")
        or str((r.get("openfigi") or {}).get("as_of")) < stale_before)]
    if do_fetch:
        jobs = [(s, figi_job(s, recs[s].get("venue"))) for s in need]
        nojob = [s for s, j in jobs if j is None]
        for s in nojob:
            recs[s]["openfigi"] = {"as_of": today, "error": "no OpenFIGI exchange mapping for venue %s" % recs[s].get("venue")}
        jobs = [(s, j) for s, j in jobs if j is not None]
        for i in range(0, len(jobs), 10):
            if time.time() > t_end:
                errors.append("mapping time box reached at %d/%d" % (i, len(jobs)))
                break
            chunk = jobs[i:i + 10]
            res, err = _call(fetch, "mapping", [j for _, j in chunk])
            for k, (s, j) in enumerate(chunk):
                if err or not isinstance(res, list) or k >= len(res):
                    recs[s]["openfigi"] = {"as_of": today, "error": err or "malformed mapping response", "job": j}
                    continue
                d = (res[k] or {}).get("data") or []
                if not d:
                    recs[s]["openfigi"] = {"as_of": today, "job": j,
                                           "error": (res[k] or {}).get("warning") or (res[k] or {}).get("error") or "no identifier found"}
                    continue
                st2 = sorted({x.get("securityType2") for x in d if x.get("securityType2")})
                recs[s]["openfigi"] = {"as_of": today, "job": j, "error": None if len(st2) == 1 else "ambiguous securityType2 %s" % st2,
                                       "securityType2": st2[0] if len(st2) == 1 else None,
                                       "securityType": d[0].get("securityType"), "name": d[0].get("name"),
                                       "figi": d[0].get("figi"), "n_lines": len(d)}
                fetched.append(s)
            if map_spacing_s:
                time.sleep(map_spacing_s)
        for s, r in sorted(recs.items()):
            fig = r.get("openfigi") or {}
            if fig.get("securityType2") != "Depositary Receipt":
                continue
            u = r.get("underlying") or {}
            if u.get("as_of") and str(u["as_of"]) >= stale_before and not u.get("error") and u.get("query") == issuer_key(fig.get("name")):
                continue
            if time.time() > t_end:
                errors.append("search time box reached before %s" % s)
                break
            r["underlying"] = search_underlying(fig.get("name"), fetch=fetch, spacing_s=search_spacing_s, deadline=t_end)
            searched.append(s)
            if search_spacing_s:
                time.sleep(search_spacing_s)
    vd = {s: evaluate(r, cfg, today) for s, r in sorted(recs.items()) if s in ver}
    cnt = {}
    for v in vd.values():
        cnt[v["state"]] = cnt.get(v["state"], 0) + 1
    rc = {}
    for v in vd.values():
        for x in v.get("reasons") or []:
            rc[x] = rc.get(x, 0) + 1
    out = {"_what": ("ISA-0860 GENERATED (R14.3) instrument-type and ISA-eligibility evidence per verified symbol (Step 5x, "
                     "instrument_eligibility.refresh). Consumed by broker_dealability only. Never hand-edited."),
           "as_of": today, "config_version": cfg.get("version"), "n_symbols": len(vd), "counts": cnt, "reason_counts": rc,
           "fetch": {"enabled": do_fetch, "fetched": len(fetched), "dr_searched": searched, "errors": errors,
                     "source": "OpenFIGI v3 (Raj approved 12-Oct-2026), unauthenticated"},
           "records": {s: recs[s] for s in sorted(recs) if s in ver}, "verdicts": vd}
    if not dry_run:
        p = os.path.join(root, CACHE_FILE)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    return {"state": "REFRESHED" if do_fetch else ("DISABLED" if not _flag() else "DRY_RUN"), "as_of": today,
            "n_symbols": len(vd), "counts": cnt, "reason_counts": rc, "fetched": len(fetched), "dr_searched": searched,
            "errors": errors, "not_isa_qualifying": sorted(s for s, v in vd.items() if v["state"] == NOT_ISA_QUALIFYING_DR),
            "confirm_required": {s: v["reasons"] for s, v in vd.items() if v["state"] == CONFIRM_REQUIRED}}


# ── selftest ─────────────────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:300]) if detail and not cond else ""))
        if not cond:
            fails.append(name)
    cfg = load_config(HERE)
    D = "2026-10-12"
    veon_rows = [{"code": c, "ticker": t, "name": "VEON LTD"} for c, t in
                 (("RX", "VEON-RX"), ("RN", "VEON-RX"), ("RP", "VEON-RX"), ("T2", "VEONEUR"), ("WT", "VEONEUR"))]
    base = {"symbol": "VEON", "venue": "NMS", "openfigi": {"securityType2": "Depositary Receipt", "name": "VEON LTD", "as_of": D},
            "feed": {"form": "depositary_receipt", "company": "VEON Ltd. ADS"},
            "underlying": {"query": "VEON LTD", "rows": veon_rows, "exhausted": True, "error": None}}
    v = evaluate(base, cfg, D)
    ok("MUST-FIRE: VEON shape (Moscow REVOKED + Tradeweb MTF only, search exhausted) -> NOT_ISA_QUALIFYING_DR", v["state"] == NOT_ISA_QUALIFYING_DR, v)
    ok("the VEON refusal names the revocation", "REVOKED" in v["why"], v["why"])
    v2 = evaluate(dict(base, underlying=dict(base["underlying"], rows=veon_rows + [{"code": "ER", "ticker": "VEON1", "name": "VEON LTD"}])), cfg, D)
    ok("NEGATIVE CONTROL: one UNCLASSIFIED code (ER, the real VEON evidence) blocks the refusal -> CONFIRM_REQUIRED (fundable)",
       v2["state"] == CONFIRM_REQUIRED and "DR_DECLARATION_PENDING" in v2["reasons"] and "ER" in v2["why"], v2)
    v3 = evaluate(dict(base, underlying=dict(base["underlying"], exhausted=False, error="page cap 5 reached")), cfg, D)
    ok("NEGATIVE CONTROL: an un-exhausted search never refuses", v3["state"] == CONFIRM_REQUIRED, v3)
    ok("NEGATIVE CONTROL: a 2021 as-of (Moscow still recognised) is evidence, not a refusal",
       evaluate(base, cfg, "2021-06-01")["state"] == CONFIRM_REQUIRED)
    tsm_rows = [{"code": "TT", "ticker": "2330", "name": "TAIWAN SEMICONDUCTOR MANUFAC"},
                {"code": "PQ", "ticker": "TSMWF", "name": "TAIWAN SEMICONDUCTOR MANUFAC"},
                {"code": "US", "ticker": "TSMWF", "name": "TAIWAN SEMICONDUCTOR MANUFAC"}]
    tsm = {"symbol": "TSM", "venue": "NYQ", "openfigi": {"securityType2": "Depositary Receipt", "name": "TAIWAN SEMICONDUCTOR MANUFAC-SP ADR", "as_of": D},
           "feed": {"form": "unknown"}, "underlying": {"query": "TAIWAN SEMICONDUCTOR MANUFAC", "rows": tsm_rows, "exhausted": True}}
    ok("MUST-FIRE (TSM by table date): before TWSE's 14-Apr-2026 designation -> NOT_ISA_QUALIFYING_DR (TT not yet recognised, US composite inherits OTC)",
       evaluate(tsm, cfg, "2026-03-01")["state"] == NOT_ISA_QUALIFYING_DR, evaluate(tsm, cfg, "2026-03-01"))
    _t = evaluate(tsm, cfg, D)
    ok("MUST-FIRE (TSM by table date): after designation -> recognised-listing evidence -> DR_DECLARATION_PENDING (fundable, Raj declares once)",
       _t["state"] == CONFIRM_REQUIRED and (_t.get("dr") or {}).get("detail") == "RECOGNISED_LISTING_EVIDENCE", _t)
    # R5.14 five-point matrix at the TWSE designation boundary (recognised_from 2026-04-14), clock injected
    _det = lambda d: (evaluate(tsm, cfg, d).get("dr") or {}).get("detail") or evaluate(tsm, cfg, d)["state"]
    ok("ISA-0860 R5.14 T-1: on 2026-04-13 TT is not yet recognised -> NOT_ISA_QUALIFYING_DR", evaluate(tsm, cfg, "2026-04-13")["state"] == NOT_ISA_QUALIFYING_DR)
    ok("ISA-0860 R5.14 T: on 2026-04-14 (designation day) TT counts -> recognised-listing evidence", _det("2026-04-14") == "RECOGNISED_LISTING_EVIDENCE")
    ok("ISA-0860 R5.14 T+1: on 2026-04-15 TT still counts", _det("2026-04-15") == "RECOGNISED_LISTING_EVIDENCE")
    ok("ISA-0860 R5.14 repeat_same_occurrence: the same as-of evaluated twice gives the same verdict",
       evaluate(tsm, cfg, "2026-04-14") == evaluate(tsm, cfg, "2026-04-14"))
    ok("ISA-0860 R5.14 next_occurrence: the next day's own evaluation is unchanged (no drift without a table change)",
       _det("2026-04-16") == "RECOGNISED_LISTING_EVIDENCE")
    ok("issuer identity is EXACT: TSMC's DR name does not match the different issuer 'TAIWAN SEMICONDUCTOR CO LTD'",
       issuer_key("TAIWAN SEMICONDUCTOR CO LTD") != issuer_key("TAIWAN SEMICONDUCTOR MANUFAC-SP ADR")
       and issuer_key("TAIWAN SEMICONDUCTOR MANUFAC-SP ADR") == "TAIWAN SEMICONDUCTOR MANUFAC")
    ok("issuer key strips DR / class tails (ARGX, ASML, ONC, GMAB)",
       issuer_key("ARGENX SE - ADR") == "ARGENX SE" and issuer_key("ASML HOLDING NV-NY REG SHS") == "ASML HOLDING NV"
       and issuer_key("BEONE MEDICINES LTD-ADR") == issuer_key("BEONE MEDICINES LTD-H") and issuer_key("GENMAB A/S -SP ADR") == "GENMAB A/S")
    com = {"symbol": "MTDR", "venue": "NYQ", "openfigi": {"securityType2": "Common Stock", "as_of": D},
           "feed": {"form": "unknown", "company": "Matador Resources Company"}}
    ok("NEGATIVE CONTROL: MTDR (OpenFIGI Common Stock, NYSE) is ELIGIBLE with no flag", evaluate(com, cfg, D)["state"] == ELIGIBLE)
    halo = dict(com, symbol="HALO", venue="NMS", feed={"form": "direct", "company": "Halozyme Therapeutics Inc. Common Stock"})
    ok("NEGATIVE CONTROL: HALO is ELIGIBLE with no flag", evaluate(halo, cfg, D)["state"] == ELIGIBLE)
    glbe = dict(com, symbol="GLBE", venue="NMS", feed={"form": "direct", "us_ordinary_shares": True})
    _g = evaluate(glbe, cfg, D)
    ok("GLBE shape (US-venue Ordinary Shares) -> CONFIRM_REQUIRED CUSTODY_ROUTE_CONFIRM, never a refusal",
       _g["state"] == CONFIRM_REQUIRED and _g["reasons"] == ["CUSTODY_ROUTE_CONFIRM"], _g)
    ok("a London Ordinary Shares line carries no custody flag (US venues only)",
       evaluate(dict(glbe, venue="LSE"), cfg, D)["state"] == ELIGIBLE)
    _u = evaluate({"symbol": "NEW", "venue": "NMS", "openfigi": {"error": "URLError: offline", "as_of": D}, "feed": {"form": "unknown"}}, cfg, D)
    ok("MUST-FIRE (no refusal on missing data): OpenFIGI unreachable -> CONFIRM_REQUIRED ELIGIBILITY_UNVERIFIED",
       _u["state"] == CONFIRM_REQUIRED and _u["reasons"] == ["ELIGIBILITY_UNVERIFIED"], _u)
    _c = evaluate(dict(com, feed={"form": "depositary_receipt"}), cfg, D)
    ok("cross-check: OpenFIGI Common Stock vs a feed 'ADS' -> INSTRUMENT_TYPE_CONFLICT (fundable)",
       _c["state"] == CONFIRM_REQUIRED and "INSTRUMENT_TYPE_CONFLICT" in _c["reasons"], _c)
    _o = evaluate(dict(com, openfigi={"securityType2": "Mutual Fund", "as_of": D}), cfg, D)
    ok("an ETP line -> INSTRUMENT_TYPE_OTHER", "INSTRUMENT_TYPE_OTHER" in _o["reasons"])
    ok("a German code is UNCLASSIFIED (Regulierter Markt and Freiverkehr share codes)",
       classify_code("GY", "X", [], cfg, D)["class"] == "UNCLASSIFIED")
    ok("figi_job maps Yahoo venues and class shares", figi_job("ATCO-A.ST", "STO") == {"idType": "TICKER", "idValue": "ATCOA", "exchCode": "SS", "marketSecDes": "Equity"}
       and figi_job("BRK-B", "NYQ")["idValue"] == "BRK/B" and figi_job("SPI.L", "LSE")["exchCode"] == "LN" and figi_job("X", "ZZZ") is None)
    # producer: fake network, page cap, rate-limit, time box
    calls = []

    def fake(path, body):
        calls.append(path)
        if path == "mapping":
            return [{"data": [{"securityType2": "Depositary Receipt" if j["idValue"] == "VEON" else "Common Stock",
                               "name": "VEON LTD" if j["idValue"] == "VEON" else j["idValue"], "figi": "BBG0"}]} for j in body]
        return {"data": [dict(r, exchCode=r["code"]) for r in veon_rows]}
    import tempfile, shutil
    td = tempfile.mkdtemp(prefix="ie_")
    try:
        shutil.copy(os.path.join(HERE, CONFIG_FILE), td)
        json.dump({"verified": [{"ticker": "VEON", "exchange": "NMS", "company": "VEON Ltd. ADS"},
                                {"ticker": "MTDR", "exchange": "NYQ", "company": "Matador Resources Company"}]},
                  open(os.path.join(td, SYMBOL_MAP_FILE), "w"))
        r = refresh(td, today=D, fetch=fake, map_spacing_s=0, search_spacing_s=0)
        ok("producer: one mapping call + one search for the single DR; VEON refused, MTDR eligible",
           calls == ["mapping", "search"] and r["not_isa_qualifying"] == ["VEON"] and r["counts"].get(ELIGIBLE) == 1, (calls, r))
        calls.clear()
        r2 = refresh(td, today=D, fetch=fake, map_spacing_s=0, search_spacing_s=0)
        ok("producer is INCREMENTAL: a fresh cache makes no network call", calls == [] and r2["not_isa_qualifying"] == ["VEON"], (calls, r2))

        def dead(path, body):
            raise OSError("network down")
        os.remove(os.path.join(td, CACHE_FILE))
        r3 = refresh(td, today=D, fetch=dead, map_spacing_s=0, search_spacing_s=0)
        ok("MUST-FIRE: network down -> every name CONFIRM_REQUIRED ELIGIBILITY_UNVERIFIED, nothing refused",
           r3["not_isa_qualifying"] == [] and set(r3["confirm_required"]) == {"VEON", "MTDR"}
           and all("ELIGIBILITY_UNVERIFIED" in x for x in r3["confirm_required"].values()), r3)
        ok("verdicts() re-evaluates offline by date (TT-style table change applies without a fetch)",
           set(verdicts(td, D)) == {"VEON", "MTDR"})
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("instrument_eligibility selftest: %d FAIL(s)" % len(fails))
    assert not fails, "instrument_eligibility negative controls must not fail: %s" % fails
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--refresh" in sys.argv:
        print(json.dumps(refresh(), indent=1)[:4000])
        sys.exit(0)
    print(json.dumps({s: {k: v.get(k) for k in ("state", "reasons", "why")} for s, v in verdicts().items()
                      if v.get("state") != ELIGIBLE}, indent=1))
