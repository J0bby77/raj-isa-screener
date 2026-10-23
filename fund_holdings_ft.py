#!/usr/bin/env python3
"""
fund_holdings_ft.py — ISA-0869 CA(7) (18-Oct-2026): per-fund TOP-10 HOLDINGS from the FT fund tearsheet into
fund_holdings_declared.json (register H9's name-level store), stamped with the page's portfolio date and source.

The store's discipline is unchanged (declared-not-inferred, PARTIAL coverage stated so the name-level test returns
a BOUND): this module only replaces the hand-typed entries with the FT page's own table, one writer, provenance on
every entry. It is QUARTERLY - an entry younger than REFRESH_AFTER_DAYS is not re-fetched - and every outcome is
typed (DECLARED_FT / KEPT_EXISTING / UNAVAILABLE / SOURCE_FAILED). A fund the FT page does not cover (investment
trusts, Polar's class) keeps whatever entry it had, or stays ABSENT; it is never written as empty.
Consumer: lookthrough.marginal_allocation / declaration_status (Step 6.06). It does not move the H9 verdict,
which turns on factor_map's declared fund_ai_share; it fills the name-level bound beside it.
"""
from __future__ import annotations
import datetime as dt, json, os, re, sys, html as _html

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "fund_holdings_declared.json")
FT_URL = "https://markets.ft.com/data/funds/tearsheet/holdings?s={sym}"
REFRESH_AFTER_DAYS = 100          # quarterly: FT restates top-10s with the quarter's portfolio date
US_VENUES = {"NSQ", "NYQ", "NMQ", "ASE", "NAS", "NYS", "PCQ", "BTQ"}
STATES = ("DECLARED_FT", "KEPT_EXISTING", "CURRENT", "UNAVAILABLE", "SOURCE_FAILED")


class FtHoldingsLayoutError(RuntimeError):
    pass


def ft_symbols(u):
    """Candidate FT symbols for a fund, most specific first (ISIN:GBP, ISIN:GBX, then LSE ticker for ETFs)."""
    out = []
    if (u or {}).get("ft_holdings_symbol"):
        out.append(u["ft_holdings_symbol"])
    if (u or {}).get("isin"):
        out += [u["isin"] + ":GBP", u["isin"] + ":GBX"]
    ys = (u or {}).get("yf_symbol") or ""
    if ys.endswith(".L"):
        out += [ys[:-2] + ":LSE:GBP", ys[:-2] + ":LSE:GBX"]
    return out


def parse_ft_holdings(page):
    """{as_of, coverage_pct, holdings:[{name, ft_symbol, ticker, weight_pct}]} from an FT holdings tearsheet, or
    None when the page carries no top-10 table (the fund is not covered). RAISES when the table is present but
    its shape changed (never a guessed list)."""
    i = page.find("Top 10 holdings")
    if i < 0:
        return None
    k = page.find(">Company<", i)
    if k < 0:
        raise FtHoldingsLayoutError("top-10 section without the Company table")
    end = page.find("</table>", k)
    table = page[k:end]
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    hold = []
    for r in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(tds) < 3:
            continue
        name_cell = tds[0]
        sym = re.search(r'cell__disclaimer">([^<]+)<', name_cell)
        name = _html.unescape(re.sub(r"<[^>]+>", " ", name_cell.split("<br")[0])).strip()
        w = re.sub(r"<[^>]+>", "", tds[2]).strip().rstrip("%")
        try:
            wt = float(w)
        except ValueError:
            raise FtHoldingsLayoutError("weight cell %r is not a percentage" % w)
        fs = sym.group(1).strip() if sym else None
        base, venue = (fs.split(":", 1) + [None])[:2] if fs else (None, None)
        hold.append({"name": name, "ft_symbol": fs,
                     "ticker": (base if venue in US_VENUES else fs), "weight_pct": wt})
    if not hold:
        raise FtHoldingsLayoutError("top-10 table has no parseable rows")
    tot = re.search(r"Per cent of portfolio in top 10 holdings:.*?([0-9.]+)%", page[i:], re.S)
    dates = [dt.datetime.strptime(m.group(1), "%b %d %Y").date()
             for m in re.finditer(r"As of ([A-Z][a-z]{2} \d{1,2} \d{4})", page[:i])]
    return {"as_of": max(dates).isoformat() if dates else None,
            "as_of_basis": ("the holdings page's portfolio date (asset/sector/region sections); FT does not stamp "
                            "the top-10 table separately") if dates else "NO DATE ON PAGE",
            "coverage_pct": round(float(tot.group(1)), 2) if tot else round(sum(h["weight_pct"] for h in hold), 2),
            "holdings": hold}


def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ISA research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def refresh(as_of, universe=None, store_path=None, *, get=None, budget_s=15.0, force=False):
    """Bring the declared store up to the quarter. Returns a typed per-fund report; never raises for a source
    failure (the fund keeps its existing entry, or stays ABSENT and declaration_status names it)."""
    import time as _t
    t0 = _t.time()
    get = get or _http_get
    if universe is None:
        import fund_performance as fp
        universe = fp.load_universe()
    funds = universe.get("funds", universe)
    path = store_path or STORE
    try:
        store = json.load(open(path, encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        store = {"schema_version": 1, "funds": {}}
    decl = store.setdefault("funds", {})
    rep, changed = {}, False
    for sd, u in funds.items():
        if str(sd).startswith("_") or not isinstance(u, dict):
            continue
        cur = decl.get(sd) or {}
        age = None          # cadence runs on the RETRIEVAL date: FT's portfolio dates lag the quarter by ~3 months
        try:
            age = (as_of - dt.date.fromisoformat(str(cur.get("retrieved_on"))[:10])).days if cur.get("retrieved_on") else None
        except ValueError:
            age = None
        if not force and cur and age is not None and age <= REFRESH_AFTER_DAYS and cur.get("source_kind") == "FT":
            rep[sd] = {"state": "CURRENT", "as_of": cur.get("as_of")}
            continue
        chk = (store.get("_ft_unavailable_checked") or {}).get(sd)
        if not force and chk and (as_of - dt.date.fromisoformat(chk)).days <= REFRESH_AFTER_DAYS:
            rep[sd] = {"state": "KEPT_EXISTING" if cur else "UNAVAILABLE",
                       "why": "FT had no top-10 for this fund when checked %s (re-checked quarterly)" % chk}
            continue
        if _t.time() - t0 > budget_s:
            rep[sd] = {"state": "KEPT_EXISTING" if cur else "UNAVAILABLE", "why": "budget %.0f s spent" % budget_s}
            continue
        got, why, used = None, "FT tearsheet carries no top-10 table for this fund", None
        for sym in ft_symbols(u):
            try:
                g = parse_ft_holdings(get(FT_URL.format(sym=sym)))
            except FtHoldingsLayoutError as e:
                got, why = None, "FT layout changed: %s" % e
                break
            except Exception as e:                                      # noqa: BLE001
                why = "%s: %s" % (type(e).__name__, str(e)[:120])
                continue
            if g:
                got, used = g, sym
                break
        if not got:
            failed = "layout" in why or "Error" in why
            if not failed:
                store.setdefault("_ft_unavailable_checked", {})[sd] = as_of.isoformat()
                changed = True
            rep[sd] = {"state": ("SOURCE_FAILED" if failed else ("KEPT_EXISTING" if cur else "UNAVAILABLE")), "why": why}
            continue
        # an FT table supersedes a hand-typed entry (whose as_of was a RECORDING date, not a portfolio date); an
        # older FT portfolio date never replaces a newer FT one
        if cur.get("source_kind") == "FT" and cur.get("as_of") and got["as_of"] and got["as_of"] < str(cur["as_of"])[:10]:
            rep[sd] = {"state": "KEPT_EXISTING", "why": "existing entry %s is newer than FT %s" % (cur["as_of"], got["as_of"])}
            continue
        entry = {"name": u.get("name"), "as_of": got["as_of"], "as_of_basis": got["as_of_basis"],
                 "source": "FT fund tearsheet holdings %s (retrieved %s)" % (FT_URL.format(sym=used), as_of.isoformat()),
                 "source_kind": "FT", "retrieved_on": as_of.isoformat(),
                 "coverage_pct": got["coverage_pct"], "coverage_basis": "top_10_weights",
                 "holdings": got["holdings"]}
        if cur:
            entry["superseded"] = [{k: cur.get(k) for k in ("as_of", "source", "coverage_pct", "coverage_basis")}] + \
                                  list(cur.get("superseded") or [])[:3]
        decl[sd] = entry
        changed = True
        rep[sd] = {"state": "DECLARED_FT", "as_of": got["as_of"], "coverage_pct": got["coverage_pct"], "n": len(got["holdings"])}
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    return {"as_of": as_of.isoformat(), "funds": rep, "secs": round(_t.time() - t0, 1),
            "counts": {s: sum(1 for r in rep.values() if r["state"] == s) for s in STATES}}


def _selftest():
    import tempfile, shutil
    fails = []

    def ok(name, cond, detail=None):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
        if not cond:
            fails.append(name)
    row = ('<tr><td class="mod-ui-table__cell--text"><a href="/x" class="mod-ui-link">%s</a><br/>'
           '<span class="mod-ui-table__cell__disclaimer">%s</span></td><td><span>+1%%</span></td><td>%s%%</td><td></td></tr>')
    page = ("<div>As of Jun 30 2026</div><div>As of Mar 31 2026</div><h2>Top 10 holdings</h2><table>x</table>"
            "<table><thead><th>Company</th><th>1 year change</th><th>Portfolio weight</th></thead>"
            + row % ("NVIDIA Corporation", "NVDA:NSQ", "8.10") + row % ("HSBC Holdings PLC", "HSBA:LSE", "5.44")
            + "<tr><td class=\"mod-ui-table__cell--text\">Unlisted Co</td><td>--</td><td>1.00%</td><td></td></tr>"
            + "</table><p>Per cent of portfolio in top 10 holdings: <span>14.54%</span></p>")
    g = parse_ft_holdings(page)
    ok("parser reads name, FT symbol, ticker (US venue -> base ticker; others keep the FT symbol) and weight",
       g["holdings"][0] == {"name": "NVIDIA Corporation", "ft_symbol": "NVDA:NSQ", "ticker": "NVDA", "weight_pct": 8.1}
       and g["holdings"][1]["ticker"] == "HSBA:LSE", g["holdings"][:2])
    ok("an unlisted holding keeps its name and weight with no ticker", g["holdings"][2] == {"name": "Unlisted Co", "ft_symbol": None, "ticker": None, "weight_pct": 1.0})
    ok("coverage is the page's stated top-10 total; as_of is the latest portfolio date before the table",
       g["coverage_pct"] == 14.54 and g["as_of"] == "2026-06-30", g)
    ok("NEGATIVE CONTROL: a page without a top-10 section is None (not covered), never an empty declaration",
       parse_ft_holdings("<html>no holdings</html>") is None)
    try:
        parse_ft_holdings(page.replace("8.10%", "n/a"))
        ok("MUST-FIRE: a changed weight cell RAISES", False)
    except FtHoldingsLayoutError:
        ok("MUST-FIRE: a changed weight cell RAISES", True)
    tmp = tempfile.mkdtemp(prefix="fh_st_")
    try:
        sp = os.path.join(tmp, "decl.json")
        json.dump({"schema_version": 1, "funds": {"A": {"name": "A", "as_of": "2026-08-02", "source": "manual", "coverage_pct": 28.8,
                                                        "coverage_basis": "top_5_total_only", "holdings": []}}}, open(sp, "w"))
        U = {"funds": {"A": {"name": "A", "isin": "GB00A"}, "T": {"name": "Trust", "isin": "GB00T"}}}
        pages = {FT_URL.format(sym="GB00A:GBP"): page}
        r = refresh(dt.date(2026, 10, 18), U, sp, get=lambda url: pages.get(url, "<html>none</html>"))
        d = json.load(open(sp))["funds"]
        ok("MUST-FIRE: an FT top-10 supersedes a hand-typed entry, keeping the superseded record",
           r["funds"]["A"]["state"] == "DECLARED_FT" and d["A"]["source_kind"] == "FT" and d["A"]["superseded"][0]["source"] == "manual", r)
        ok("NEGATIVE CONTROL: a fund FT does not cover stays ABSENT (UNAVAILABLE), never written empty",
           r["funds"]["T"]["state"] == "UNAVAILABLE" and "T" not in d, r["funds"]["T"])
        r2 = refresh(dt.date(2026, 11, 28), U, sp, get=lambda url: (_ for _ in ()).throw(AssertionError("fetched")))
        ok("R5.14 repeat/next occurrence inside the quarter: an FT entry retrieved under 100 days ago is CURRENT and not re-fetched",
           r2["funds"]["A"]["state"] == "CURRENT", r2)
        r3 = refresh(dt.date(2027, 1, 30), U, sp, get=lambda url: "<html>blocked</html>")
        ok("T+1 quarter: a failed re-fetch KEEPS the existing entry (typed), never deletes it",
           r3["funds"]["A"]["state"] == "KEPT_EXISTING" and json.load(open(sp))["funds"]["A"]["source_kind"] == "FT", r3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("fund_holdings_ft selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    a = dt.date.fromisoformat(sys.argv[sys.argv.index("--as-of") + 1]) if "--as-of" in sys.argv else dt.date.today()
    print(json.dumps(refresh(a, force="--force" in sys.argv, budget_s=150.0), indent=1, default=str))
