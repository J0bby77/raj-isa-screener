#!/usr/bin/env python3
"""
report_measures.py - ISA-0898 Group C: REPORTING-ONLY measures for the monthly report (25-Oct-2026).

Raj, 25-Oct-2026: the October Decision Brief wireframe is the MINIMUM standard; include the account bridge,
the largest contributions, the per-holding gain statement and the security return history.

    account_bridge      opening value -> money added -> movement by sleeve (shares / funds / cash fund) ->
                        income and charges -> closing value, with an explicit reconciliation residual
    contributions       per holding, in pounds, over the same interval (price movement net of dealing,
                        before dividends - dividends are shown once, under income)
    holdings_statement  every holding: value, weight, remaining cost, unrealised gain GBP and %
    return_history      1M / 3M / 6M / calendar YTD / 1Y / 3Y per year / 5Y per year for every holding, from
                        the golden NAV series (funds) and the GBP-matched adjusted price history (shares);
                        the X-Ray is the independent second derivation for 1Y/3Y/5Y and a disagreement is
                        PUBLISHED as SOURCE_CONFLICT, never blended (R6.2)

NOTHING HERE IS A DECISION INPUT. No capital, sizing, ranking or gate reads it; the report renders it
(R20.2). Every figure carries as_of and source (R4.2); a figure that cannot be measured is a typed state with
a reason, never a number (R4.1, R2.10). Stdlib only.

Reuse, not re-derivation (R4.4): fund series come from fund_performance.nav_series_for and every trailing
return is fund_performance.trailing_return - the one home for that arithmetic.
"""
import contextlib, io  # noqa: E401
import datetime as dt
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

WINDOWS = [("1M", 1 / 12, "cumulative"), ("3M", 0.25, "cumulative"), ("6M", 0.5, "cumulative"),
           ("YTD", None, "cumulative"), ("1Y", 1.0, "cumulative"), ("3Y", 3.0, "per year"),
           ("5Y", 5.0, "per year")]
XRAY_WINDOWS = {"1Y": "return_1yr", "3Y": "return_3yr", "5Y": "return_5yr"}
BRIDGE_TOLERANCE_GBP = 5.00          # reporting tolerance for the bridge residual (dealing rounding, FX pennies)
CASH_FUND_TICKERS = ("CSH2",)        # the money-market ETF the framework treats as cash (Run_Context Step 1b-2)


def _date(s):
    if not s:
        return None
    s = str(s)[:11]
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                                   # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------------------------------------
def _holdings(pf):
    """[(code, name, kind, value, cost, gain_gbp, gain_pct, weight)] from a portfolio_data document."""
    out = []
    s = (pf or {}).get("summary") or {}
    mmf = {m.get("ticker"): m for m in (s.get("mmf_holdings") or [])}
    for kind, key in (("Share", "stocks"), ("Fund", "funds")):
        for h in (pf or {}).get(key) or []:
            code = h.get("ticker")
            k = "Cash fund" if code in CASH_FUND_TICKERS or code in mmf else kind
            out.append({"code": code, "name": h.get("name") or code, "kind": k, "value_gbp": h.get("value_gbp"),
                        "cost_gbp": h.get("cost_gbp"), "gain_gbp": h.get("gain_gbp"), "gain_pct": h.get("gain_pct"),
                        "weight_pct": h.get("weight_pct")})
    for code, m in mmf.items():
        if not any(r["code"] == code for r in out):
            out.append({"code": code, "name": m.get("name") or code, "kind": "Cash fund", "value_gbp": m.get("value_gbp"),
                        "cost_gbp": None, "gain_gbp": None, "gain_pct": None, "weight_pct": None})
    return out


def holdings_statement(pf):
    """Every holding with value, weight, remaining cost and unrealised gain. Broker cash shown separately."""
    s = (pf or {}).get("summary") or {}
    total = s.get("total_value_gbp")
    rows = _holdings(pf)
    for r in rows:
        if r["weight_pct"] is None and total and r["value_gbp"] is not None:
            r["weight_pct"] = round(100.0 * r["value_gbp"] / total, 2)
    order = {"Share": 0, "Fund": 1, "Cash fund": 2}
    rows.sort(key=lambda r: (order.get(r["kind"], 3), -(r["value_gbp"] or 0)))
    cash_broker = None
    if s.get("cash_stated_gbp") is not None:
        cash_broker = round((s.get("cash_stated_gbp") or 0) - (s.get("mmf_value_gbp") or 0), 2)
    return {"state": "OK" if rows else "NO_HOLDINGS", "as_of": ((pf or {}).get("_meta") or {}).get("data_date"),
            "source": ((pf or {}).get("_meta") or {}).get("source_file"), "total_value_gbp": total,
            "rows": rows, "broker_cash_gbp": cash_broker,
            "note": "Cost and gain are for the units still held; they exclude gains already realised and income received."}


# ---------------------------------------------------------------------------------------------------------
# Prior snapshot
# ---------------------------------------------------------------------------------------------------------
def prior_portfolio(cur, base=HERE):
    """The newest portfolio_data snapshot strictly before the current one (root or archive)."""
    d1 = _date(((cur or {}).get("_meta") or {}).get("data_date"))
    best, best_d, seen = None, None, set()
    sys.path.insert(0, base)               # ONE home for live + archived month artefacts (ISA-0746 class kill)
    import month_artefacts as _ma                                           # noqa: E402
    for p in _ma.month_glob(base, "portfolio_data_*.json"):
        doc = _load(p)
        d = _date(((doc or {}).get("_meta") or {}).get("data_date"))
        if not d or d1 is None or d >= d1 or d in seen:
            continue
        seen.add(d)
        if best_d is None or d > best_d:
            best, best_d = doc, d
    return best


# ---------------------------------------------------------------------------------------------------------
# Month-end snapshots (Raj, 25-Oct-2026: save the portfolio xlsx at month-end AND at COB the day before the
# pre-run; the latest file drives decisions, the month-end files give a clean calendar-month bridge that is
# on the same date as the month-end X-Ray). The extractor already takes the newest-dated file for decisions.
# ---------------------------------------------------------------------------------------------------------
XLSX_NAME_RE = re.compile(r"^AJ Bell ISA Portfolio (\d{2}-[A-Za-z]{3}-\d{2})\.xlsx$")
MONTH_END_SLACK_DAYS = 2              # a file within 2 days of the last weekday counts (bank holidays)


def _last_weekday(y, m):
    d = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def month_end_files(isa_folder):
    """{(year, month): (date, path)} - the newest portfolio xlsx in each month that sits at the month end."""
    by_month = {}
    for fp in glob.glob(os.path.join(isa_folder or "", "AJ Bell ISA Portfolio *.xlsx")):
        m = XLSX_NAME_RE.match(os.path.basename(fp))
        if not m:
            continue
        try:
            d = dt.datetime.strptime(m.group(1), "%d-%b-%y").date()
        except ValueError:
            continue
        k = (d.year, d.month)
        if k not in by_month or d > by_month[k][0]:
            by_month[k] = (d, fp)
    return {k: v for k, v in by_month.items()
            if v[0] >= _last_weekday(*k) - dt.timedelta(days=MONTH_END_SLACK_DAYS)}


# Quarterly valuation PDF as a one-off month-end source (Raj, 25-Oct-2026: AJ Bell cannot re-issue a past
# portfolio xlsx; the quarterly 'Portfolio Valuation Qn YYYY.pdf' is what exists for 30-Sep-2026). It is in
# WHOLE POUNDS, so a bridge using it carries a wider, stated tolerance; holdings carry names only, so each line
# is matched to a framework code by quantity (exact to 3dp) and then by name - an unmatched line is reported.
QPDF_GLOB = "Portfolio Valuation Q* 20*.pdf"
_QPDF_PERIOD_RE = re.compile(r"Portfolio Valuation for the period\s+\d{1,2} \w+ \d{4}\s+to\s+(\d{1,2} \w+ \d{4})")
_QPDF_LINE_RE = re.compile(r"^(?P<name>.+?) (?P<qty>[\d,]+(?:\.\d+)?) (?P<ccy>[£$€]) ?(?P<px>[\d,]+(?:\.\d+)?) "
                           r"(?P<val>[\d,]+) (?P<cost>[\d,]+) \(?(?P<chg>[\d,]+)\)?$")
_QPDF_TOTAL_RE = re.compile(r"^TOTAL £\s?([\d,]+)", re.M)
_QPDF_CASH_RE = re.compile(r"^TOTAL CASH BALANCE £\s?([\d,]+)", re.M)
_STOP = {"INC", "PLC", "LTD", "LIMITED", "FUND", "FUNDS", "MANAGERS", "MANAGEMENT", "ACC", "INC.", "GBP", "USD", "COM",
         "THE", "NAV", "NET", "CO", "ORD", "ICVC", "SRS", "UK", "I", "C", "M", "Z", "E", "H", "ETF", "UCITS", "ASSET",
         "INVESTMENT", "INVESTMENTS", "SERIES", "LSE", "OF", "AND"}


def _tok(s):
    return {t for t in re.findall(r"[A-Z0-9&]+", str(s or "").upper().replace("SMARTGARP", "SMART GARP"))
            if t not in _STOP and not re.fullmatch(r"USD[\d.]+|\d+(\.\d+)?", t)}


def parse_quarterly_pdf(fp, refs, extract_text=None, ledger=None):
    """A portfolio_data-shaped snapshot from a quarterly valuation PDF; codes matched against `refs`."""
    if extract_text is None:
        import pdfplumber                                                   # noqa: E402
        with pdfplumber.open(fp) as pdf:
            txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    else:
        txt = extract_text(fp)
    m = _QPDF_PERIOD_RE.search(txt)
    t = _QPDF_TOTAL_RE.search(txt)
    if not (m and t):
        raise ValueError("period or TOTAL line not found in %s" % os.path.basename(fp))
    d = dt.datetime.strptime(m.group(1), "%d %B %Y").date()
    lines = txt.splitlines()
    pool = {}
    for doc in refs or []:
        mmf = {x.get("ticker") for x in ((doc or {}).get("summary") or {}).get("mmf_holdings") or []}
        for key in ("stocks", "funds"):
            for h in (doc or {}).get(key) or []:
                c = h.get("ticker")
                if c and c not in pool:
                    pool[c] = {"key": key, "name": h.get("name") or c, "qty": h.get("units") or h.get("shares")
                               or h.get("quantity"), "mmf": c in mmf or c in CASH_FUND_TICKERS}
        for x in ((doc or {}).get("summary") or {}).get("mmf_holdings") or []:
            pool.setdefault(x.get("ticker"), {"key": "funds", "name": x.get("name"), "qty": None, "mmf": True})
    held = {}                                          # quantity held per code AT the statement date, from the ledger
    for e in sorted((ledger or {}).get("entries") or [], key=lambda x: str(x.get("date"))):
        ed = _date(e.get("date"))
        if not ed or ed > d or e.get("type") not in ("buy", "sell", "transfer_in", "transfer_out", "conversion", "split"):
            continue
        c = _trade_code(e)
        q = float(e.get("quantity") or 0)
        # ISA-0923: the sign of every non-conversion row comes from the ledger's ONE home (a split adds units)
        from extract_transactions import UNIT_SIGN as _US
        sign = _US.get(e.get("type"), 1)
        if e.get("type") == "conversion" and str(c).endswith("-INV") and held.get(c):
            sign = -1
        held[c] = held.get(c, 0.0) + sign * q
        pool.setdefault(c, {"key": "funds" if _trade_kind(e) == "Fund" else "stocks",
                            "name": e.get("description") or c, "qty": None, "mmf": False})
    for c, q in held.items():              # the ledger's position AT the statement date beats today's quantity
        if c in pool and q > 1e-6:
            pool[c]["qty"] = round(q, 4)
    stocks, funds, unmatched, mmf_rows, used = [], [], [], [], set()
    for i, ln in enumerate(lines):
        r = _QPDF_LINE_RE.match(ln.strip())
        if not r:
            continue
        nm = r.group("name") + " " + (lines[i + 1].strip() if i + 1 < len(lines) else "")
        qty, val = float(r.group("qty").replace(",", "")), float(r.group("val").replace(",", ""))
        best, score = None, 0.0
        for c, p in pool.items():
            if c in used:
                continue
            sc = 0.0
            if p["qty"] is not None and abs(float(p["qty"]) - qty) < 5e-4:
                sc += 1.0
            a, b = _tok(nm), _tok(p["name"]) | _tok(c)
            sc += (len(a & b) / len(a | b)) if (a | b) else 0.0
            if "SMART OVERNIGHT" in nm.upper() and p["mmf"]:
                sc += 1.0
            if sc > score:
                best, score = c, sc
        if best is None or score < 0.25:
            # not matched to a framework code (renamed share class, a holding sold before any saved export): it is
            # CARRIED under its statement name so the totals still reconcile; its contribution shows as its own line
            unmatched.append(nm.strip())
            row = {"ticker": "NAME:" + nm.strip(), "name": nm.strip().title(), "units": qty, "value_gbp": val,
                   "cost_gbp": float(r.group("cost").replace(",", "")), "match_score": 0.0, "unmatched": True}
            (stocks if r.group("ccy") == "$" else funds).append(row)
            continue
        used.add(best)
        p = pool[best]
        row = {"ticker": best, "name": p["name"], "units": qty, "value_gbp": val,
               "cost_gbp": float(r.group("cost").replace(",", "")), "match_score": round(score, 2)}
        if p["mmf"]:
            mmf_rows.append({"ticker": best, "name": p["name"], "value_gbp": val})
        else:
            (stocks if p["key"] == "stocks" else funds).append(row)
    cash = _QPDF_CASH_RE.search(txt)
    cash = float(cash.group(1).replace(",", "")) if cash else None
    mv = sum(x["value_gbp"] for x in mmf_rows)
    n_lines = len(stocks) + len(funds) + len(mmf_rows)
    return {"_meta": {"data_date": d.strftime("%d-%b-%Y"), "source_file": os.path.basename(fp),
                      "precision_gbp": 1.0, "rounded_lines": n_lines + 2, "unmatched_lines": unmatched,
                      "kind": "quarterly_valuation_pdf"},
            "summary": {"total_value_gbp": float(t.group(1).replace(",", "")),
                        "cash_stated_gbp": (None if cash is None else cash + mv), "mmf_value_gbp": mv,
                        "mmf_holdings": mmf_rows},
            "stocks": stocks, "funds": funds}


def quarterly_pdfs(isa_folder):
    """{(year, month): path} for quarterly valuation PDFs, keyed by the period-end month (from the filename)."""
    out = {}
    for fp in glob.glob(os.path.join(isa_folder or "", QPDF_GLOB)):
        m = re.search(r"Q([1-4]) (20\d\d)\.pdf$", os.path.basename(fp))
        if m:
            out[(int(m.group(2)), 3 * int(m.group(1)))] = fp
    return out


def _month_end_docs(want, cur, ledger, isa_folder, parse=None, qpdf_text=None):
    """{(y, m): snapshot} for the month ends in `want` (portfolio xlsx first, quarterly valuation PDF as the
    one-off fallback) -> (docs, missing labels)."""
    files = month_end_files(isa_folder)
    qpdf = quarterly_pdfs(isa_folder)
    missing = [_last_weekday(*k).strftime("%d-%b-%y") for k in want if k not in files and k not in qpdf]
    if missing:
        return None, missing
    if parse is None:
        sys.path.insert(0, HERE)
        from extract_portfolio import parse_portfolio as parse              # noqa: E402
    docs = {}
    for k in want:
        if k in files:
            with contextlib.redirect_stdout(io.StringIO()):
                docs[k] = parse(files[k][1])
    for k in want:
        if k not in docs:
            docs[k] = parse_quarterly_pdf(qpdf[k], [x for x in docs.values() if x] + [cur], extract_text=qpdf_text,
                                          ledger=ledger)
    return docs, []


def _bridge_docs(a, b, cs, ledger):
    rounded = sum(int(((x or {}).get("_meta") or {}).get("rounded_lines") or 0) for x in (a, b))
    tol = round(BRIDGE_TOLERANCE_GBP + 0.5 * rounded, 2)
    out = account_bridge(a, b, cs, ledger, tolerance_gbp=tol)
    out["files"] = [((x or {}).get("_meta") or {}).get("source_file") for x in (a, b)]
    unm = [u for x in (a, b) for u in (((x or {}).get("_meta") or {}).get("unmatched_lines") or [])]
    if rounded:
        out["precision_note"] = ("one valuation is AJ Bell's quarterly statement, which is in whole pounds; "
                                 "the bridge tolerance is widened to GBP %.2f accordingly" % tol)
    if unm:
        out["unmatched_note"] = ("carried under their statement names (renamed or since-sold holdings), so the totals "
                                 "reconcile but their contributions appear as separate lines: " + "; ".join(unm))
    return out


def year_bridge(cur, cs, ledger, isa_folder, parse=None, qpdf_text=None):
    """Calendar year to date: 31 December (last working day) of the prior year -> the current snapshot.
    Gives the YTD investment gain split into shares / funds / cash fund / income and charges (Raj 26-Oct-2026)."""
    d1 = _date(((cur or {}).get("_meta") or {}).get("data_date"))
    if d1 is None:
        return {"state": "INCOMPLETE", "why": "the current snapshot has no date"}
    d1 = d1 if isinstance(d1, dt.date) else d1.date()
    k = (d1.year - 1, 12)
    docs, missing = _month_end_docs([k], cur, ledger, isa_folder, parse=parse, qpdf_text=qpdf_text)
    period = "%s to %s" % (_last_weekday(*k).strftime("%d %b %Y"), d1.strftime("%d %b %Y"))
    if missing:
        return {"state": "MISSING_MONTH_END", "period": period, "missing": missing,
                "why": "no year-end valuation for " + " and ".join(missing)}
    out = _bridge_docs(docs[k], cur, cs, ledger)
    out["period"] = period
    return out


def month_end_bridge(cur, cs, ledger, isa_folder, parse=None, qpdf_text=None):
    """The bridge between the two most recent COMPLETE calendar month-ends on or before the current snapshot.
    A missing month-end file is named, never substituted (R4.2)."""
    d1 = _date(((cur or {}).get("_meta") or {}).get("data_date"))
    if d1 is None:
        return {"state": "INCOMPLETE", "why": "the current snapshot has no date"}
    d1 = d1 if isinstance(d1, dt.date) else d1.date()
    y, m = d1.year, d1.month
    if d1 < _last_weekday(y, m) - dt.timedelta(days=MONTH_END_SLACK_DAYS):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    want = [(py, pm), (y, m)]
    period = "%s to %s" % (_last_weekday(py, pm).strftime("%d %b %Y"), _last_weekday(y, m).strftime("%d %b %Y"))
    docs, missing = _month_end_docs(want, cur, ledger, isa_folder, parse=parse, qpdf_text=qpdf_text)
    if missing:
        return {"state": "MISSING_MONTH_END", "period": period, "missing": missing,
                "why": "no month-end portfolio file for " + " and ".join(missing)
                       + " (save it as 'AJ Bell ISA Portfolio dd-mmm-yy.xlsx')"}
    out = _bridge_docs(docs[want[0]], docs[want[1]], cs, ledger)
    out["period"] = period
    return out


# ---------------------------------------------------------------------------------------------------------
# Account bridge and contributions
# ---------------------------------------------------------------------------------------------------------
def _cash_rows(cs):
    rows = list((cs or {}).get("rows") or [])
    for py in (cs or {}).get("prior_tax_years") or []:
        rows += [dict(r, _prior=True) for r in (py.get("rows") or [])]
    return rows


_RESOLVE = {}


def _trade_kind(e):
    """Sleeve of a ledger row when no snapshot holds the code: the ledger's own asset class, corrected for the two
    mislabels seen in the record (an ETF tagged 'stock' is a fund; a 'COM USD...' share tagged 'fund' is a share)."""
    d = str(e.get("description") or "").upper()
    if " ETF" in d or d.startswith("ISHARES") or "UCITS" in d:
        return "Fund"
    if re.search(r"\bCOM\b|USD0\.", d):
        return "Share"
    return "Fund" if e.get("asset_class") == "fund" else "Share"


def _trade_code(e, base=HERE):
    """A ledger trade's framework code. A row the importer has not yet resolved (ticker None, ISA-0866 re-resolves
    at the next import) is resolved here with the importer's own resolver; failing that it keys on its name, so
    its cash still lands on ONE contribution line rather than vanishing from the bridge."""
    if e.get("ticker"):
        return e["ticker"]
    d = str(e.get("description") or "").strip()
    if d not in _RESOLVE:
        code = None
        try:
            sys.path.insert(0, base)
            import extract_transactions as _et                                  # noqa: E402
            code, _how = _et.resolve_ticker(d, {}, _et.symbol_map_names(base))
        except Exception:                                                       # noqa: BLE001
            code = None
        _RESOLVE[d] = code or ("NAME:" + d)
    return _RESOLVE[d]


def account_bridge(prev, cur, cs, ledger, tolerance_gbp=BRIDGE_TOLERANCE_GBP):
    """Opening -> money added -> sleeve movements -> income and charges -> closing, over (d0, d1]."""
    if not prev:
        return {"state": "NO_PRIOR_SNAPSHOT",
                "why": "no earlier portfolio snapshot on disk to bridge from"}
    d0 = _date(prev["_meta"].get("data_date"))
    d1 = _date(cur["_meta"].get("data_date"))
    v0 = (prev.get("summary") or {}).get("total_value_gbp")
    v1 = (cur.get("summary") or {}).get("total_value_gbp")
    if None in (d0, d1, v0, v1):
        return {"state": "INCOMPLETE", "why": "a snapshot lacks its date or total value"}

    def inwin(s):
        d = _date(s)
        return d is not None and d0 < d <= d1

    # money added = external flows (contributions / transfers in) in the window
    ext = [r for r in (((cs or {}).get("external_flows") or {}).get("rows") or []) if inwin(r.get("date"))]
    for py in (cs or {}).get("prior_tax_years") or []:
        ext += [r for r in (py.get("external_flows") or []) if inwin(r.get("date"))]
    money_added = round(sum(float(r.get("amount_gbp") or 0) for r in ext), 2)
    rows = [r for r in _cash_rows(cs) if inwin(r.get("date"))]
    inc = {}
    for r in rows:
        if r.get("category") in ("DIVIDEND", "INTEREST", "REBATE", "PLATFORM_FEE", "FX_FEE"):
            inc[r["category"]] = round(inc.get(r["category"], 0.0)
                                       + float(r.get("receipt_gbp") or 0) + float(r.get("payment_gbp") or 0), 2)
    cs_last = max((_date(r.get("date")) for r in _cash_rows(cs) if _date(r.get("date"))), default=None)
    # ISA-0900 (Raj 26-Oct-2026): the statement covers postings up to its EXPORT, not only to its last dated row
    _ex = [_date(str(v)[:10]) for f, v in ((cs or {}).get("file_exports") or {}).items()
           if v and f in ((cs or {}).get("source_files") or [])]
    _ex = [x for x in _ex if x]
    if _ex and cs_last is not None:
        cs_last = max([cs_last] + _ex)
    flows_covered = cs_last is not None and cs_last >= d1

    # contributions per holding: value change, less purchases, plus sale proceeds (dealing cost inside both)
    h0 = {h["code"]: h for h in _holdings(prev)}
    h1 = {h["code"]: h for h in _holdings(cur)}
    trades = [dict(e, ticker=_trade_code(e)) for e in ((ledger or {}).get("entries") or [])
              if inwin(e.get("date")) and e.get("type") in ("buy", "sell")]
    # a share-class CONVERSION is one holding continuing: its retired leg leaves like a sale and the surviving leg
    # arrives like a purchase at the same value, so neither leg's value reads as a gain
    for e in ((ledger or {}).get("entries") or []):
        if inwin(e.get("date")) and e.get("type") == "conversion":
            c = _trade_code(e)
            leg = "sell" if (str(c).endswith("-INV") or (c in h0 and c not in h1)) else "buy"
            trades.append(dict(e, ticker=c, type=leg))
    contrib = []
    for code in sorted(set(h0) | set(h1) | {t.get("ticker") for t in trades if t.get("ticker")}):
        a, b = h0.get(code) or {}, h1.get(code) or {}
        buys = sum(float(t.get("amount_gbp") or 0) for t in trades if t.get("ticker") == code and t["type"] == "buy")
        sells = sum(float(t.get("amount_gbp") or 0) for t in trades if t.get("ticker") == code and t["type"] == "sell")
        c = (b.get("value_gbp") or 0) - (a.get("value_gbp") or 0) - buys + sells
        kind = b.get("kind") or a.get("kind") or next((_trade_kind(t) for t in trades if t.get("ticker") == code), "Fund")
        contrib.append({"code": code, "name": b.get("name") or a.get("name") or next(
            (t.get("description") for t in trades if t.get("ticker") == code), code), "kind": kind,
            "contribution_gbp": round(c, 2), "sold_in_period": bool(a) and not b, "bought_in_period": bool(b) and not a})
    by = lambda k: round(sum(x["contribution_gbp"] for x in contrib if x["kind"] == k), 2)
    parts = {"shares_gbp": by("Share"), "funds_gbp": by("Fund"), "cash_fund_gbp": by("Cash fund"),
             "income_and_charges_gbp": round(sum(inc.values()), 2)}
    # Cash posted on the statement but not yet in a snapshot (e.g. month-end interest credited after the
    # snapshot was taken): derived independently from the two balances, so the residual check keeps its teeth.
    def _bal_on(d):
        best = None
        for r in sorted((r for r in _cash_rows(cs) if not r.get("_prior") and _date(r.get("date"))),
                        key=lambda r: (_date(r["date"]), r.get("file_order", 0))):
            if _date(r["date"]) <= d:
                best = r.get("balance_gbp")
        return best
    def _snap_cash(pf):
        s_ = (pf or {}).get("summary") or {}
        if s_.get("cash_stated_gbp") is None:
            return None
        return float(s_.get("cash_stated_gbp") or 0) - float(s_.get("mmf_value_gbp") or 0)
    b0, b1, c0, c1 = _bal_on(d0), _bal_on(d1), _snap_cash(prev), _snap_cash(cur)
    timing = (round((b0 - c0) - (b1 - c1), 2)
              if None not in (b0, b1, c0, c1) else None)
    if timing:
        parts["cash_posted_after_snapshot_gbp"] = timing
    gain = round(v1 - v0 - money_added, 2)
    # sleeve returns (Raj 26-Oct-2026: "split out how much is attributed to funds and stocks, nominal and %"):
    # Modified Dietz - each sleeve's contribution over its opening value plus its net purchases weighted by the
    # fraction of the period they were held (and the same for the whole account with the money paid in).
    _span = max(1, (d1 - d0).days) if hasattr(d1 - d0, "days") else 1

    def _w(ds):
        d = _date(ds)
        return max(0.0, min(1.0, (d1 - d).days / _span)) if d else 0.5

    def _sleeve(kind, part):
        o = sum(float(h.get("value_gbp") or 0) for h in h0.values() if h.get("kind") == kind)
        net = sum((1 if t["type"] == "buy" else -1) * float(t.get("amount_gbp") or 0) * _w(t.get("date"))
                  for t in trades if ((h1.get(t.get("ticker")) or h0.get(t.get("ticker")) or {}).get("kind")
                                      or _trade_kind(t)) == kind)
        den = o + net
        return {"gain_gbp": part, "opening_gbp": round(o, 2),
                "pct": (round(100.0 * part / den, 2) if den > 0.05 * max(o, 1.0) and den > 0 else None)}
    sleeves = {"shares": _sleeve("Share", parts["shares_gbp"]), "funds": _sleeve("Fund", parts["funds_gbp"])}
    _den = v0 + sum(float(r.get("amount_gbp") or 0) * _w(r.get("date")) for r in ext)
    total_pct = round(100.0 * gain / _den, 2) if _den > 0 else None
    residual = round(gain - sum(parts.values()), 2)
    state = "OK" if abs(residual) <= tolerance_gbp else "UNRECONCILED"
    if state == "OK" and not flows_covered:
        state = "PROVISIONAL"
    return {"state": state, "from": d0.isoformat(), "to": d1.isoformat(),
            "opening_gbp": v0, "money_added_gbp": money_added, "investment_gain_gbp": gain,
            "investment_gain_pct": total_pct, "sleeves": sleeves,
            "pct_basis": "Modified Dietz (gain over opening value plus time-weighted money added or invested)",
            "parts": parts, "income_detail_gbp": inc, "closing_gbp": v1, "residual_gbp": residual,
            "tolerance_gbp": tolerance_gbp, "flows_covered_to": cs_last.isoformat() if cs_last else None,
            "contributions": sorted(contrib, key=lambda x: -x["contribution_gbp"]),
            "sources": {"opening": (prev.get("_meta") or {}).get("source_file"),
                        "closing": (cur.get("_meta") or {}).get("source_file"),
                        "flows": (cs or {}).get("source_files"), "trades": "transaction_ledger.json"},
            "why": (None if state == "OK" else
                    ("the cash statement ends %s, before the closing date %s - later deposits or charges are not "
                     "yet visible" % (cs_last, d1) if state == "PROVISIONAL" else
                     "the parts do not add up to the investment gain within GBP %.2f (residual GBP %.2f)"
                     % (tolerance_gbp, residual)))}


# ---------------------------------------------------------------------------------------------------------
# Security return history
# ---------------------------------------------------------------------------------------------------------
def _stock_series(hist, code):
    rec = ((hist or {}).get("names") or {}).get(code) or ((hist or {}).get("names") or {}).get(f"{code}.L")
    if not rec:
        return None, None
    s = []
    for d, v in sorted((rec.get("daily") or {}).items()):
        dd = _date(d)
        adj, fx = v.get("adj"), v.get("fx_to_gbp")
        if dd and adj and fx:
            s.append((dd, float(adj) * float(fx)))
    return s, "adjusted close x FX to GBP (%s)" % (rec.get("source") or "price store")


def _norm(n):
    return re.sub(r"[^a-z0-9]", "", str(n or "").lower().replace("&pound;", "").replace("£", ""))


def return_history(cur, as_of=None, universe=None, hist=None, xray=None, fp=None):
    """Seven horizons for every holding. Golden series first; X-Ray as the independent check (R5.2/R6.2)."""
    if fp is None:
        import fund_performance as fp                                   # noqa: E402 - optional heavy import
    universe = universe if universe is not None else fp.load_universe()
    as_of = as_of or _date(((cur or {}).get("_meta") or {}).get("data_date")) or dt.date.today()
    xr = {_norm(r.get("name")): r for r in ((xray or {}).get("fund_holdings") or [])}
    tol = getattr(fp, "XRAY_AGREE_TOL_PP", 1.0)
    rows = []
    for h in _holdings(cur):
        code, name = h["code"], h["name"]
        u = (universe or {}).get(code)
        series, source = None, None
        if u:
            try:
                series = fp.nav_series_for(code, u)
                source = "golden NAV series (%s)" % (u.get("yf_symbol") or u.get("isin") or code)
            except Exception as e:                                      # noqa: BLE001
                series, source = None, "NAV series unreadable: %s" % e
        if not series:
            s2, src2 = _stock_series(hist, code)
            if s2:
                series, source = s2, src2
        x = xr.get(_norm(name)) or next((v for k, v in xr.items() if _norm(name)[:18] and k.startswith(_norm(name)[:18])), None)
        cells = {}
        # each window is measured to the series' own newest point on or before the report date, and dated so
        end_d = max((p[0] for p in (series or []) if p[0] <= as_of), default=None)
        for lab, years, basis in WINDOWS:
            ref = end_d or as_of
            yrs = years if years is not None else max((ref - dt.date(ref.year - 1, 12, 31)).days / 365.25, 1 / 365.25)
            cell = {"basis": basis, "state": "NOT_AVAILABLE", "value": None, "as_of": None, "source": None, "why": None}
            if series:
                cum, ann = fp.trailing_return(series, ref, yrs, source, lab)
                m = ann if basis == "per year" else cum
                md = m if isinstance(m, dict) else getattr(m, "__dict__", {})
                val = getattr(m, "value", None) if not isinstance(m, dict) else m.get("value")
                if val is not None:
                    cell.update(state="OK", value=round(float(val), 2),
                                as_of=str(getattr(m, "as_of", None) or md.get("as_of")), source=source)
                else:
                    cell["why"] = str(getattr(m, "reason", None) or md.get("reason") or "not covered by the series")
            else:
                cell["why"] = "no price or NAV series on file for this holding"
            xk = XRAY_WINDOWS.get(lab)
            if x and xk and x.get(xk) is not None:
                xv, xd = float(x[xk]), x.get("as_of")
                cell["xray"] = {"value": xv, "as_of": xd}
                if cell["state"] != "OK":
                    cell.update(state="OK", value=round(xv, 2), as_of=xd, source="Morningstar X-Ray (second source)")
                elif series and xd and _date(xd):
                    gc, ga = fp.trailing_return(series, _date(xd), years, source, lab)
                    gm = ga if basis == "per year" else gc
                    gv = getattr(gm, "value", None) if not isinstance(gm, dict) else gm.get("value")
                    if gv is not None and abs(float(gv) - xv) > tol:
                        cell.update(state="SOURCE_CONFLICT",
                                    why="golden series %.2f%% vs X-Ray %.2f%% at %s (tolerance %.1fpp) - both "
                                        "published, neither chosen" % (float(gv), xv, xd, tol))
            cells[lab] = cell
        rows.append({"code": code, "name": name, "kind": h["kind"], "cells": cells})
    n = sum(1 for r in rows for c in r["cells"].values() if c["state"] == "OK")
    na = sum(1 for r in rows for c in r["cells"].values() if c["state"] == "NOT_AVAILABLE")
    sc = sum(1 for r in rows for c in r["cells"].values() if c["state"] == "SOURCE_CONFLICT")
    return {"state": "OK" if rows else "NO_HOLDINGS", "as_of": as_of.isoformat(), "rows": rows,
            "counts": {"sourced": n, "not_available": na, "source_conflict": sc},
            "note": "Security returns, not the return you earned since buying. 3Y and 5Y are per year; the rest are "
                    "cumulative. Calendar YTD runs from 31 December."}


# ---------------------------------------------------------------------------------------------------------
# Orchestration entry (pre-run Step 9r)
# ---------------------------------------------------------------------------------------------------------
def target_progress(cur, cs=None, target_state=None, base=HERE):
    """Progress against the canonical target (target_state.json, A19): (a) the growth now needed from TODAY's value
    to reach the floor and stretch targets by the target date on the declared contribution schedule; (b) how far
    the value is ahead of / behind the path from the target's last checkpoint at the operative rate, counting the
    money actually paid in since. The solver and path are derive_required_return's own (one home)."""
    ts = target_state if target_state is not None else _load(os.path.join(base, "target_state.json"))
    if not ts:
        return {"state": "ABSENT", "why": "target_state.json not readable"}
    d1 = _date(((cur or {}).get("_meta") or {}).get("data_date"))
    v1 = ((cur or {}).get("summary") or {}).get("total_value_gbp")
    if d1 is None or v1 is None:
        return {"state": "INCOMPLETE", "why": "the current snapshot lacks its date or value"}
    d1 = d1 if isinstance(d1, dt.date) else d1.date()
    sys.path.insert(0, base)
    import derive_required_return as _drr                                   # noqa: E402
    sched = ts.get("contribution_schedule") or []
    end = dt.date.fromisoformat(str(ts.get("target_date")))
    out = {"state": "OK", "as_of": d1.isoformat(), "value_gbp": v1, "target_date": end.isoformat(),
           "target_floor_gbp": ts.get("target_floor_gbp"), "target_stretch_gbp": ts.get("target_stretch_gbp"),
           "operative_pct": ts.get("required_return_operative_pct"),
           "basis": "a goal calculation on the declared contribution schedule, not a forecast"}
    for k in ("floor", "stretch"):
        try:
            out["required_now_%s_pct" % k] = round(_drr.solve_required_annual_pct(
                float(ts["target_%s_gbp" % k]), float(v1), d1, end, sched, rounded=False), 2)
        except Exception as e:                                              # noqa: BLE001
            out["required_now_%s_pct" % k] = None
            out["why_%s" % k] = "%s: %s" % (type(e).__name__, e)
    c0, cd = ts.get("portfolio_value_gbp"), _date(ts.get("portfolio_value_date"))
    if c0 is not None and cd is not None and ts.get("required_return_operative_pct") is not None:
        cd = cd if isinstance(cd, dt.date) else cd.date()
        flows = []
        for r in (((cs or {}).get("external_flows") or {}).get("rows") or []):
            d = _date(r.get("date"))
            if d and cd < (d if isinstance(d, dt.date) else d.date()) <= d1:
                flows.append({"date": (d if isinstance(d, dt.date) else d.date()).isoformat(),
                              "amount_gbp": float(r.get("amount_gbp") or 0)})
        m = (1 + float(ts["required_return_operative_pct"]) / 100.0) ** (1 / 12.0) - 1
        path = _drr.fv_at_rate(m, float(c0), cd, d1, sched, one_offs=flows)
        out.update({"checkpoint_gbp": c0, "checkpoint_date": cd.isoformat(), "flows_since_checkpoint_gbp":
                    round(sum(f["amount_gbp"] for f in flows), 2), "path_value_gbp": round(path, 2),
                    "ahead_gbp": round(float(v1) - path, 2)})
    return out


def compute(cur, cs=None, ledger=None, xray=None, base=HERE, hist=None, universe=None, fp=None,
            isa_folder=None, parse=None):
    prev = prior_portfolio(cur, base)
    if hist is None:
        hist = _load(os.path.join(base, "held_stock_history.json"))
    out = {"state": "OK", "basis": "ISA-0898 reporting-only measures; never a decision input (R20.2)",
           "holdings": holdings_statement(cur),
           "bridge": account_bridge(prev, cur, cs, ledger)}
    if isa_folder is None:
        isa_folder = os.path.dirname(base)
    try:
        out["month_end_bridge"] = month_end_bridge(cur, cs, ledger, isa_folder, parse=parse)
    except Exception as e:                                              # noqa: BLE001
        out["month_end_bridge"] = {"state": "ERROR", "why": "%s: %s" % (type(e).__name__, e)}
    try:
        out["year_bridge"] = year_bridge(cur, cs, ledger, isa_folder, parse=parse)
    except Exception as e:                                              # noqa: BLE001
        out["year_bridge"] = {"state": "ERROR", "why": "%s: %s" % (type(e).__name__, e)}
    try:
        out["target_progress"] = target_progress(cur, cs=cs, base=base)
    except Exception as e:                                              # noqa: BLE001
        out["target_progress"] = {"state": "ERROR", "why": "%s: %s" % (type(e).__name__, e)}
    try:
        out["returns"] = return_history(cur, xray=xray, hist=hist, universe=universe, fp=fp)
    except Exception as e:                                              # noqa: BLE001
        out["returns"] = {"state": "ERROR", "why": "%s: %s" % (type(e).__name__, e)}
    if any((out[k] or {}).get("state") in ("ERROR", "UNRECONCILED") for k in ("bridge", "returns", "month_end_bridge",
                                                                                   "year_bridge")):
        out["state"] = "DEGRADED"
    return out


# ---------------------------------------------------------------------------------------------------------
def _selftest():
    class _M:
        def __init__(self, v, a):
            self.value, self.as_of = v, a

    class FP:                                                           # minimal stand-in for fund_performance
        XRAY_AGREE_TOL_PP = 1.0

        @staticmethod
        def load_universe():
            return {"F1": {"name": "Fund One"}}

        @staticmethod
        def nav_series_for(code, u):
            base = dt.date(2020, 1, 1)
            return [(base + dt.timedelta(days=i), 100.0 * (1.0001 ** i)) for i in range(0, 2400)]

        @staticmethod
        def trailing_return(series, as_of, years, source, label):
            end = [p for p in series if p[0] <= as_of][-1]
            st = [p for p in series if p[0] <= as_of - dt.timedelta(days=round(years * 365.25))]
            if not st:
                return {"reason": "insufficient"}, {"reason": "insufficient"}
            c = end[1] / st[-1][1] - 1
            return _M(c * 100, end[0]), _M(((1 + c) ** (1 / years) - 1) * 100, end[0])

    prev = {"_meta": {"data_date": "02-Oct-2026", "source_file": "p"}, "summary": {"total_value_gbp": 1000.0,
            "cash_stated_gbp": 100.0, "mmf_value_gbp": 0}, "stocks": [{"ticker": "AAA", "name": "Alpha", "value_gbp": 300.0}],
            "funds": [{"ticker": "F1", "name": "Fund One", "value_gbp": 600.0}]}
    cur = {"_meta": {"data_date": "22-Oct-2026", "source_file": "c"}, "summary": {"total_value_gbp": 1260.0,
           "cash_stated_gbp": 50.0, "mmf_value_gbp": 0},
           "stocks": [{"ticker": "BBB", "name": "Beta", "value_gbp": 210.0, "cost_gbp": 200.0, "gain_gbp": 10.0, "gain_pct": 5.0}],
           "funds": [{"ticker": "F1", "name": "Fund One", "value_gbp": 1000.0}]}
    cs = {"external_flows": {"rows": [{"date": "2026-10-10", "amount_gbp": 250.0}]},
          "rows": [{"date": "2026-10-14", "category": "PLATFORM_FEE", "receipt_gbp": 0, "payment_gbp": -10.0},
                   {"date": "2026-10-22", "category": "OPENING_BALANCE", "receipt_gbp": 0, "payment_gbp": 0}]}
    led = {"entries": [{"date": "2026-10-05", "type": "sell", "ticker": "AAA", "amount_gbp": 330.0},
                       {"date": "2026-10-06", "type": "buy", "ticker": "BBB", "amount_gbp": 200.0},
                       {"date": "2026-10-07", "type": "buy", "ticker": "F1", "amount_gbp": 400.0}]}
    b = account_bridge(prev, cur, cs, led)
    # gain = 1260 - 1000 - 250 = 10 ; AAA +30, BBB +10, F1 0, fees -10 -> parts 30 ; residual -20 -> UNRECONCILED
    assert b["investment_gain_gbp"] == 10.0 and b["money_added_gbp"] == 250.0, b
    assert {c["code"]: c["contribution_gbp"] for c in b["contributions"]} == {"AAA": 30.0, "BBB": 10.0, "F1": 0.0}, b
    assert b["state"] == "UNRECONCILED" and b["residual_gbp"] == -20.0, "MUST-FIRE: a bridge that does not add up is said so"
    cur2 = dict(cur, summary=dict(cur["summary"], total_value_gbp=1280.0))
    b2 = account_bridge(prev, cur2, cs, led)
    # MUST-FIRE timing: interest dated before the opening snapshot but absent from its cash is a separate,
    # independently derived bridge line (the 02-Oct-2026 case: GBP 22.14 credited after the snapshot)
    cs4 = {"external_flows": {"rows": []}, "rows": [
        {"date": "2026-09-30", "category": "INTEREST", "receipt_gbp": 20.0, "payment_gbp": 0, "balance_gbp": 120.0},
        {"date": "2026-10-22", "category": "OPENING_BALANCE", "receipt_gbp": 0, "payment_gbp": 0, "balance_gbp": 120.0}]}
    p4 = dict(prev, stocks=[], funds=[], summary=dict(prev["summary"], total_value_gbp=100.0, cash_stated_gbp=100.0))
    c4 = dict(cur, stocks=[], funds=[], summary=dict(cur["summary"], total_value_gbp=120.0, cash_stated_gbp=120.0))
    b4 = account_bridge(p4, c4, cs4, {"entries": []})
    assert b4["parts"].get("cash_posted_after_snapshot_gbp") == 20.0 and b4["state"] == "OK", ("MUST-FIRE timing", b4)
    assert b2["state"] == "OK" and b2["residual_gbp"] == 0.0, ("NEGATIVE CONTROL: a reconciling bridge is OK", b2)
    cs3 = dict(cs, rows=[r for r in cs["rows"] if r["category"] != "OPENING_BALANCE"])
    assert account_bridge(prev, cur2, cs3, led)["state"] == "PROVISIONAL", "MUST-FIRE: flows not covered to the end date"
    cs3x = dict(cs3, source_files=["S.xlsx"], file_exports={"S.xlsx": "2026-10-22T12:14:35"})
    assert account_bridge(prev, cur2, cs3x, led)["state"] == "OK", \
        "NEGATIVE CONTROL: a statement exported on the closing date covers it, even after a quiet week"
    assert account_bridge(None, cur, cs, led)["state"] == "NO_PRIOR_SNAPSHOT", "MUST-FIRE: no prior snapshot is typed"
    hs = holdings_statement(cur)
    assert [r["code"] for r in hs["rows"]] == ["BBB", "F1"] and hs["broker_cash_gbp"] == 50.0, hs
    rh = return_history(cur, as_of=dt.date(2026, 10, 22), universe=FP.load_universe(), hist={},
                        xray={"fund_holdings": [{"name": "FUND ONE", "as_of": "2026-09-30", "return_1yr": 99.0}]}, fp=FP)
    f1 = next(r for r in rh["rows"] if r["code"] == "F1")["cells"]
    assert f1["1M"]["state"] == "OK" and f1["5Y"]["state"] == "OK", f1
    assert f1["1Y"]["state"] == "SOURCE_CONFLICT", ("MUST-FIRE: golden vs X-Ray disagreement is published", f1["1Y"])
    bbb = next(r for r in rh["rows"] if r["code"] == "BBB")["cells"]
    assert bbb["1M"]["state"] == "NOT_AVAILABLE" and bbb["1M"]["value"] is None, "NEGATIVE CONTROL: no series -> no number"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for n in ("AJ Bell ISA Portfolio 31-Aug-26.xlsx", "AJ Bell ISA Portfolio 22-Oct-26.xlsx",
                  "AJ Bell ISA Portfolio 30-Oct-26.xlsx", "AJ Bell ISA Portfolio Example 30-Sep-26.xlsx"):
            open(os.path.join(td, n), "w").close()
        me = month_end_files(td)
        assert set(me) == {(2026, 8), (2026, 10)}, ("month-end files: 22-Oct is not a month end; Example ignored", me)
        mb = month_end_bridge(cur, cs, led, td, parse=lambda fp: None)
        assert mb["state"] == "MISSING_MONTH_END" and mb["missing"] == ["30-Sep-26"], \
            ("MUST-FIRE: a missing month-end is named, never substituted", mb)
        open(os.path.join(td, "AJ Bell ISA Portfolio 30-Sep-26.xlsx"), "w").close()
        snaps = {"30-Sep-26": dict(prev, _meta={"data_date": "30-Sep-2026"}),
                 "30-Oct-26": dict(cur2, _meta={"data_date": "30-Oct-2026"})}
        cs5 = dict(cs, rows=cs["rows"] + [{"date": "2026-10-30", "category": "OPENING_BALANCE",
                                            "receipt_gbp": 0, "payment_gbp": 0}])
        cur_nov = dict(cur, _meta={"data_date": "30-Oct-2026"})
        mb2 = month_end_bridge(cur_nov, cs5, led, td, parse=lambda fp: snaps[os.path.basename(fp)[-14:-5]])
        assert mb2["state"] == "OK" and mb2["period"] == "30 Sep 2026 to 30 Oct 2026", \
            ("NEGATIVE CONTROL: two month-end files bridge cleanly", mb2)
    _qtxt = ("Portfolio Valuation for the period 1 July 2026 to 30 September 2026\n"
             "FUND ONE MANAGERS LIMITED 12.5 £ 80.00 1,000 900 100\nFUND ONE GBP ACC\n"
             "MYSTERY CORP 3 $ 10.00 30 20 10\nCOM USD0.01\n"
             "TOTAL CASH BALANCE £20 £20\nTOTAL £1,050 £940 £110\n")
    _ref = {"funds": [{"ticker": "F1", "name": "Fund One", "units": 12.5}], "stocks": [], "summary": {}}
    q = parse_quarterly_pdf("Portfolio Valuation Q3 2026.pdf", [_ref], extract_text=lambda fp: _qtxt)
    assert [f["ticker"] for f in q["funds"]] == ["F1"] and q["summary"]["total_value_gbp"] == 1050.0, q
    assert q["_meta"]["unmatched_lines"] == ["MYSTERY CORP COM USD0.01"], ("MUST-FIRE: an unmatched valuation line is named", q)
    assert any(x["ticker"] == "NAME:MYSTERY CORP COM USD0.01" for x in q["stocks"]), \
        ("MUST-FIRE: an unmatched line is CARRIED under its own name, so the totals still reconcile", q["stocks"])
    _led = {"entries": [{"date": "2026-09-08", "type": "buy", "ticker": None, "description": "Mystery Corp",
                         "quantity": 3.0, "amount_gbp": 20.0, "asset_class": "unknown"}]}
    q2 = parse_quarterly_pdf("Portfolio Valuation Q3 2026.pdf", [_ref], extract_text=lambda fp: _qtxt, ledger=_led)
    assert not q2["_meta"]["unmatched_lines"] and q2["stocks"][0]["ticker"].endswith("Mystery Corp"), \
        ("NEGATIVE CONTROL: a position opened between snapshots is matched through the ledger", q2)
    assert b2["sleeves"]["funds"]["gain_gbp"] == b2["parts"]["funds_gbp"] and b2["investment_gain_pct"] is not None, \
        ("sleeve split carried with its percentage", b2.get("sleeves"))
    with tempfile.TemporaryDirectory() as td2:
        yb = year_bridge(cur, cs, led, td2, parse=lambda fp: None)
        assert yb["state"] == "MISSING_MONTH_END" and yb["missing"] == ["31-Dec-25"], ("MUST-FIRE: no year-end valuation is named", yb)
    import types
    _fake = types.ModuleType("derive_required_return")
    _fake.solve_required_annual_pct = lambda t, p, s, e, sch, rounded=True: 12.34
    _fake.fv_at_rate = lambda m, p, s, e, sch, one_offs=None: p + sum(o["amount_gbp"] for o in (one_offs or []))
    sys.modules["derive_required_return"] = _fake
    tp = target_progress(cur, cs={"external_flows": {"rows": [{"date": "2026-10-10", "amount_gbp": 100.0}]}},
                         target_state={"target_floor_gbp": 1e6, "target_stretch_gbp": 1.5e6, "target_date": "2037-12-31",
                                       "portfolio_value_gbp": 1000.0, "portfolio_value_date": "2026-09-30",
                                       # the anchor comes from the delivered target_state, never a literal (ISA-0432 Form 3)
                                       "required_return_operative_pct": (_load(os.path.join(HERE, "target_state.json")) or {})
                                       .get("required_return_operative_pct"), "contribution_schedule": []})
    del sys.modules["derive_required_return"]
    assert tp["required_now_floor_pct"] == 12.34 and tp["ahead_gbp"] == 160.0 and tp["flows_since_checkpoint_gbp"] == 100.0, \
        ("target progress: growth needed now and distance from the checkpoint path, flows counted", tp)
    assert target_progress(cur, target_state={})["state"] == "ABSENT", "NEGATIVE CONTROL: no target state -> ABSENT"
    print("report_measures selftest OK (year bridge / sleeve split / target progress; quarterly-PDF month end: quantity match, unmatched line named; month-end bridge names a missing month end / bridges two month ends; bridge reconciles / refuses / provisional / no prior; contributions; "
          "holdings; return history golden-vs-X-Ray conflict; no-series is NOT_AVAILABLE)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
