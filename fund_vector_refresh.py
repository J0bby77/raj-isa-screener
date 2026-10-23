#!/usr/bin/env python3
"""
fund_vector_refresh.py - ISA-0569 (October build P-B8, 19-Oct-2026): check every fund's exposure-vector SOURCE
each run and take a newer publication automatically where the capture is mechanical.

⚑ THE PREMISE THIS RETIRES. fund_exposure_sources.json said "THIS CAPTURE CANNOT BE REFRESHED BY THE MONTHLY
PRE-RUN ... Raj's device has no network access", so a vector's age was whatever the last assisted session left.
MEASURED 19-Oct-2026: the device reaches the Hargreaves Lansdown fund pages (9 of 12 funds) in ~4.5 s each; the
pre-run already fetches yfinance at Steps 5y/8t. The premise was stale.

⚑ WHAT THE CHECK FOUND (19-Oct-2026). HL's own data is old: Artemis European is still "Data accurate as at
31 March 2026" on HL; only Royal London and Thornbridge publish anything newer than the stored capture (31-Jul).
So a vector can be old because nobody re-captured it (NEGLECT - fixable, an error) or because its source has
published nothing newer (SOURCE_LAG - not fixable by re-capture, a stated limitation). Before this module the
two were indistinguishable; XR1 graded both as a freshness breach.

Per fund (sources whose host has a parser - HL today):
  CURRENT              source date == stored as_of AND the parsed table reproduces the stored raw (golden check)
  CURRENT_MISMATCH     same date, different table - parser or source changed; NOT applied, named
  NEWER_APPLIED        newer source date, table validated (labels mappable, published 90-101.5%) -> applied,
                       old capture kept as previous_capture (drift is then MEASURED on the pair)
  NEWER_AVAILABLE_ASSISTED  newer, but the fund carries a located residual that needs the source's second table
                       (assisted capture) - named, not applied
  PARSE_FAILED / FETCH_FAILED   named; stored vector unchanged
  UNCHECKABLE          no parser for the source host (justETF, Trustnet PDF, AIC) - stated, never assumed current
The result is written into each fund block as `source_check`; fund_exposure_vectors.build() then rebuilds the
vectors (ages current at this run). Never raises into the run (R4.12).
"""
from __future__ import annotations
import concurrent.futures as cf, datetime as dt, html as _html, json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SOURCES_FILE = "fund_exposure_sources.json"
UA = "Mozilla/5.0 (ISA framework fund_vector_refresh)"
FETCH_TIMEOUT_S = 15
TOTAL_BUDGET_S = 40
PUBLISHED_MIN_PCT, PUBLISHED_MAX_PCT = 90.0, 101.5


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
        return r.read().decode("utf-8", "replace")


def _iso(d: str):
    try:
        return dt.datetime.strptime(d.strip(), "%d %B %Y").date().isoformat()
    except ValueError:
        return None


def parse_hl(page: str) -> dict:
    """HL 'fund-analysis/geographical-analysis' page -> {state, as_of, raw}. raw keeps rows > 0 (as captured)."""
    m = re.search(r">\s*Countries\s*<", page or "")
    if not m:
        return {"state": "PARSE_FAILED", "why": "no Countries table"}
    seg = page[m.start(): m.start() + 40000]
    end = seg.find("</table>")
    if end < 0:
        return {"state": "PARSE_FAILED", "why": "Countries table not closed"}
    toks = [t.strip() for t in _html.unescape(re.sub(r"<[^>]+>", "\n", seg[:end])).split("\n") if t.strip()]
    raw, i = {}, 0
    toks = [t for t in toks if t not in (">Countries", "Countries", "% Weight")]
    while i + 1 < len(toks):
        lab, pct = toks[i], toks[i + 1]
        mm = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", pct)
        if not mm:
            return {"state": "PARSE_FAILED", "why": "unexpected token pair %r %r" % (lab, pct)}
        v = float(mm.group(1))
        if v > 0:
            raw[lab] = v
        i += 2
    da = re.search(r"Data accurate as at (\d{1,2} \w+ \d{4})", page[m.start():])
    as_of = _iso(da.group(1)) if da else None
    if not raw or not as_of:
        return {"state": "PARSE_FAILED", "why": "empty table" if not raw else "no 'Data accurate as at' date"}
    return {"state": "OK", "as_of": as_of, "raw": raw}


PARSERS = {"hl.co.uk": parse_hl}


def _parser_for(url: str):
    for host, fn in PARSERS.items():
        if host in (url or ""):
            return fn
    return None


def check_one(sd: str, blk: dict, page: str = None, err: str = None) -> dict:
    import fund_exposure_vectors as F
    out = {"source_host": (re.search(r"https?://([^/]+)/", blk.get("url") or "") or [None, None])[1],
           "stored_as_of": blk.get("as_of")}
    fn = _parser_for(blk.get("url"))
    if fn is None:
        out.update(state="UNCHECKABLE", why="no parser for this source host - currency NOT established")
        return out
    if err:
        out.update(state="FETCH_FAILED", why=err[:160])
        return out
    p = fn(page)
    if p["state"] != "OK":
        out.update(state="PARSE_FAILED", why=p.get("why"))
        return out
    out["source_as_of"] = p["as_of"]
    stored = {k: float(v) for k, v in (blk.get("raw") or {}).items() if float(v) > 0}
    if p["as_of"] == blk.get("as_of"):
        same = set(stored) == set(p["raw"]) and all(abs(stored[k] - p["raw"][k]) < 0.005 for k in stored)
        out.update(state="CURRENT" if same else "CURRENT_MISMATCH",
                   why=None if same else "same source date, different table - parser or source changed")
        return out
    if p["as_of"] < (blk.get("as_of") or ""):
        out.update(state="SOURCE_OLDER", why="the source now shows an OLDER date than the stored capture")
        return out
    _vec, diag = F.normalise(p["raw"])
    pub = sum(p["raw"].values())
    if diag["unmapped_labels"] or not (PUBLISHED_MIN_PCT <= pub <= PUBLISHED_MAX_PCT):
        out.update(state="PARSE_FAILED", why="newer table failed validation (unmapped %s; published %.2f%%)"
                   % (diag["unmapped_labels"], pub))
        return out
    if blk.get("located_residual"):
        out.update(state="NEWER_AVAILABLE_ASSISTED", why="the fund carries a located residual from the source's "
                   "second table - assisted capture needed")
        return out
    out.update(state="NEWER_APPLIED", new_raw=p["raw"])
    return out


def run(here: str = HERE, *, today: dt.date = None, fetch=None, write: bool = True, rebuild: bool = True) -> dict:
    t0 = time.time()
    today = today or dt.date.today()
    res = {"state": "OK", "checked_on": today.isoformat(), "funds": {}, "applied": [], "lag": [], "named": []}
    try:
        path = os.path.join(here, SOURCES_FILE)
        doc = json.load(open(path, encoding="utf-8"))
        funds = doc.get("funds") or {}
        fetch = fetch or _fetch
        todo = {sd: b for sd, b in funds.items() if _parser_for(b.get("url"))}
        pages, errs = {}, {}
        with cf.ThreadPoolExecutor(max_workers=max(1, min(9, len(todo)))) as ex:
            futs = {ex.submit(fetch, b["url"]): sd for sd, b in todo.items()}
            try:
                for f in cf.as_completed(futs, timeout=TOTAL_BUDGET_S):
                    sd = futs[f]
                    try:
                        pages[sd] = f.result()
                    except Exception as e:                                    # noqa: BLE001
                        errs[sd] = "%s: %s" % (type(e).__name__, e)
            except cf.TimeoutError:
                for f, sd in futs.items():
                    if sd not in pages and sd not in errs:
                        errs[sd] = "total fetch budget %ss exceeded" % TOTAL_BUDGET_S
        for sd, blk in sorted(funds.items()):
            c = check_one(sd, blk, pages.get(sd), errs.get(sd) if sd in todo and sd not in pages else None)
            new_raw = c.pop("new_raw", None)
            if c["state"] == "NEWER_APPLIED":
                blk["previous_capture"] = {"as_of": blk.get("as_of"), "raw": blk.get("raw")}
                blk["raw"], blk["as_of"] = new_raw, c["source_as_of"]
                blk["capture"] = {"by": "fund_vector_refresh (monthly pre-run, ISA-0569)", "on": today.isoformat()}
                res["applied"].append(sd)
            c["checked_on"] = today.isoformat()
            blk["source_check"] = c
            res["funds"][sd] = c
            if c["state"] == "CURRENT":
                res["lag"].append(sd)
            elif c["state"] not in ("NEWER_APPLIED",):
                res["named"].append("%s %s" % (sd, c["state"]))
        m = doc.setdefault("_meta", {})
        m["network_note"] = ("ISA-0569 (19-Oct-2026): the monthly pre-run CHECKS every source with a parser (Hargreaves "
                             "Lansdown today) and applies a newer publication automatically (fund_vector_refresh.py); "
                             "funds whose source has no parser, or whose newer table needs a second table (located "
                             "residual), are named for an assisted capture. Superseded premise: 'the pre-run has no "
                             "network' (measured false 19-Oct-2026).")
        if write:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1, ensure_ascii=False)
            os.replace(tmp, path)
            if rebuild:
                import fund_exposure_vectors as F
                F.build(sources_path=path, out_path=os.path.join(here, F.OUT_FILE), as_of=today.isoformat())
        if res["named"]:
            res["state"] = "PARTIAL"
    except Exception as e:                                                        # noqa: BLE001
        res.update(state="ERROR", why="%s: %s" % (type(e).__name__, e))
    res["secs"] = round(time.time() - t0, 1)
    res["line"] = ("Fund exposure sources (ISA-0569): %d applied newer%s; %d current at source (source lag, nothing "
                   "newer published)%s" % (len(res["applied"]), (" (%s)" % ", ".join(res["applied"])) if res["applied"] else "",
                                           len(res["lag"]), ("; named: " + "; ".join(res["named"])) if res["named"] else ""))
    return res


def _selftest() -> int:
    import tempfile, shutil
    fails = []

    def ok(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    def page(rows, date):
        body = "".join("<tr><td>%s</td><td> %s%% </td></tr>" % (k, ("%.2f" % v)) for k, v in rows)
        return ("<h2>Geographical</h2><table><tr><th>Countries</th><th>%% Weight</th></tr>%s</table>"
                "<p>Data accurate as at %s</p>" % (body, date))
    p = parse_hl(page([("France", 60.0), ("Germany", 40.0), ("Russian Federation", 0.0)], "31 March 2026"))
    ok("parse_hl reads the Countries table and the 'Data accurate as at' date; zero rows dropped (as captured)",
       p["state"] == "OK" and p["as_of"] == "2026-03-31" and p["raw"] == {"France": 60.0, "Germany": 40.0})
    ok("NEGATIVE CONTROL: a page without the table is PARSE_FAILED, never an empty vector",
       parse_hl("<html>no table</html>")["state"] == "PARSE_FAILED")
    blk = {"url": "https://www.hl.co.uk/x", "as_of": "2026-03-31", "raw": {"France": 60.0, "Germany": 40.0}}
    ok("golden: same date + same table -> CURRENT (source lag, nothing newer)",
       check_one("A", blk, page([("France", 60.0), ("Germany", 40.0)], "31 March 2026"))["state"] == "CURRENT")
    ok("NEGATIVE CONTROL: same date, different table -> CURRENT_MISMATCH (named, not applied)",
       check_one("A", blk, page([("France", 61.0), ("Germany", 39.0)], "31 March 2026"))["state"] == "CURRENT_MISMATCH")
    c = check_one("A", blk, page([("France", 55.0), ("Germany", 45.0)], "31 July 2026"))
    ok("MUST-FIRE: a newer, valid table -> NEWER_APPLIED with its date", c["state"] == "NEWER_APPLIED" and c["source_as_of"] == "2026-07-31")
    ok("NEGATIVE CONTROL: a newer table with an unmappable label is PARSE_FAILED (not applied)",
       check_one("A", blk, page([("Atlantis", 50.0), ("Germany", 50.0)], "31 July 2026"))["state"] == "PARSE_FAILED")
    ok("NEGATIVE CONTROL: newer table but the fund needs its located residual -> NEWER_AVAILABLE_ASSISTED",
       check_one("A", dict(blk, located_residual={"weights": {"Japan": 1.0}}),
                 page([("France", 55.0), ("Germany", 45.0)], "31 July 2026"))["state"] == "NEWER_AVAILABLE_ASSISTED")
    ok("a source host with no parser is UNCHECKABLE - currency never assumed",
       check_one("V", {"url": "https://www.justetf.com/x", "as_of": "2026-06-30", "raw": {}})["state"] == "UNCHECKABLE")
    td = tempfile.mkdtemp()
    try:
        doc = {"_meta": {}, "funds": {"A": dict(blk, source="HL", dimension="country"),
                                       "B": {"url": "https://www.hl.co.uk/b", "as_of": "2026-06-30", "source": "HL",
                                             "dimension": "country", "raw": {"Japan": 100.0}},
                                       "V": {"url": "https://www.justetf.com/v", "as_of": "2026-06-30", "source": "j",
                                             "dimension": "country", "raw": {"United States": 100.0}}}}
        json.dump(doc, open(os.path.join(td, SOURCES_FILE), "w"))

        def fake(url):
            if url.endswith("/b"):
                raise OSError("timed out")
            return page([("France", 55.0), ("Germany", 45.0)], "31 July 2026")
        r = run(td, today=dt.date(2026, 10, 31), fetch=fake, rebuild=False)
        d2 = json.load(open(os.path.join(td, SOURCES_FILE)))
        ok("MUST-FIRE run: A applied, old capture kept as previous_capture (drift becomes measurable)",
           r["applied"] == ["A"] and d2["funds"]["A"]["as_of"] == "2026-07-31"
           and d2["funds"]["A"]["previous_capture"]["as_of"] == "2026-03-31")
        ok("NEGATIVE CONTROL run: a failed fetch leaves B unchanged and NAMED; V named UNCHECKABLE (state PARTIAL)",
           d2["funds"]["B"]["as_of"] == "2026-06-30" and d2["funds"]["B"]["source_check"]["state"] == "FETCH_FAILED"
           and "V UNCHECKABLE" in r["named"] and r["state"] == "PARTIAL")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("fund_vector_refresh selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(json.dumps(run(write="--dry-run" not in sys.argv), indent=1)[:4000])
