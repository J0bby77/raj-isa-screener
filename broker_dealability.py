#!/usr/bin/env python3
"""
broker_dealability.py — ONE home for "can the broker deal this instrument ONLINE?" (ISA-0607).

Built 23-Sep-2026 (Candidate on TB-2026-09-23-02).

⚑ THE DEFECT THIS CLOSES. On the Sep-2026 run the deployment sequencer ranked ZAB.WA (Zabka,
Warsaw/PLN) FIRST and the router allocated it GBP 6,578.53 — the month's largest destination —
on a listing AJ Bell reaches only through its RSP network with no guaranteed online quote. It was
caught at Step 10.2 by session judgement, not by any check; the same conclusion on BFT.WA in
August had been written as prose and changed nothing. A constraint that exists only as prose is a
constraint that does not exist (ISA-0465, ISA-0307 class).

⚑ WHERE THE VENUE COMES FROM — AND WHERE IT NEVER COMES FROM.
  * The venue is `stock_symbol_map.json` -> `verified[].exchange`: the Yahoo exchange code of
    the listing that ANSWERED, admitted only when it matches the venue its provenance declared
    (stock_price_fetch.build_symbol_map, the ONT/Onterris falsifier).
  * The broker ticker is mapped to that Yahoo symbol ONLY through the DECLARED alias table
    `stock_price_fetch.SYMBOL_MAP` (e.g. ONT -> ONT.L). Nothing is inferred from how a ticker
    looks: a ".WA" suffix is not read as Warsaw, and a bare ticker is not read as NASDAQ.
  * `watchlist_tickers.json` -> `candidate_pool[].exchange` is NEVER read. It is the literal
    default "NASDAQ" on 124 of 124 rows, including London, Frankfurt, Paris and Stockholm names
    (ISA-0582). This module does not accept a row at all — only a ticker — so it cannot.

⚑ WHAT IS ADMISSIBLE. `broker_venues.json` is Raj's declaration (23-Sep-2026) of which verified
exchange codes AJ Bell deals ONLINE, i.e. which may receive AUTOMATED new capital. Only
`DEALABLE_ONLINE` admits. Everything else REFUSES new capital:
  NOT_DEALABLE_ONLINE  the code is declared, and declared non-online (WSE)
  UNDECLARED_VENUE     a verified code the declaration does not list
  UNKNOWN_VENUE        no verified listing, a malformed ticker/entry, or an unreadable input
Refusing is the safe direction (R4.3): the cost of a wrong refusal is an opportunity cost that is
logged by name; the cost of a wrong admission is an unfillable or unmeasurable trade.

⚑ WHAT THIS DOES NOT DO. It never sells, never touches a holding, never re-scores, and never
decides size. It publishes a verdict; step9_pre_builder (ranking population) and
stock_candidates (capital population) consume it, and the capital receipt carries it.

ROLLBACK (R4.13): revert this file and its two consumers; the declaration file is inert alone.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DECLARATION = "broker_venues.json"
SYMBOL_MAP_FILE = "stock_symbol_map.json"

DEALABLE_ONLINE = "DEALABLE_ONLINE"
NOT_DEALABLE_ONLINE = "NOT_DEALABLE_ONLINE"
UNDECLARED_VENUE = "UNDECLARED_VENUE"
UNKNOWN_VENUE = "UNKNOWN_VENUE"
STATES = (DEALABLE_ONLINE, NOT_DEALABLE_ONLINE, UNDECLARED_VENUE, UNKNOWN_VENUE)
ONLINE = "ONLINE"


class DealabilityRefused(Exception):
    """An input the verdict depends on is missing or malformed."""


def _read(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_declaration(root: Optional[str] = None) -> dict:
    """The DECLARED broker venue table. Refuses (never defaults) when absent or malformed."""
    p = os.path.join(root or HERE, DECLARATION)
    if not os.path.exists(p):
        raise DealabilityRefused("%s is absent: no venue is declared dealable, so none is." % DECLARATION)
    doc = _read(p)
    venues = (doc or {}).get("venues")
    if not isinstance(venues, dict) or not venues:
        raise DealabilityRefused("%s carries no `venues` table" % DECLARATION)
    for code, v in venues.items():
        if not isinstance(v, dict) or v.get("routing") not in (ONLINE, "NON_ONLINE"):
            raise DealabilityRefused("%s: venue %r has no valid routing (ONLINE / NON_ONLINE)"
                                     % (DECLARATION, code))
    return doc


def load_verified(root: Optional[str] = None) -> dict:
    """-> {"by_symbol": {yahoo_symbol: entry}, "as_of": str}. Refuses when absent/malformed."""
    p = os.path.join(root or HERE, SYMBOL_MAP_FILE)
    if not os.path.exists(p):
        raise DealabilityRefused("%s is absent: no listing is verified." % SYMBOL_MAP_FILE)
    doc = _read(p)
    ver = (doc or {}).get("verified")
    if not isinstance(ver, list):
        raise DealabilityRefused("%s carries no `verified` list" % SYMBOL_MAP_FILE)
    by = {}
    for e in ver:
        if isinstance(e, dict) and isinstance(e.get("ticker"), str) and e["ticker"].strip():
            by[e["ticker"].strip()] = e
    return {"by_symbol": by, "as_of": doc.get("refreshed_on") or doc.get("built_on")}


def _aliases() -> Dict[str, str]:
    """Declared broker-ticker -> Yahoo-symbol aliases. Never guessed."""
    try:
        import stock_price_fetch as _spf
        return dict(getattr(_spf, "SYMBOL_MAP", {}) or {})
    except Exception:                                                   # noqa: BLE001
        return {}


class Resolver:
    """Loads the two inputs ONCE and answers per ticker. A load failure is not raised to the
    consumer: every verdict then reads UNKNOWN_VENUE with the reason, so a broken input refuses
    capital by name instead of crashing the run or admitting anything."""

    def __init__(self, root: Optional[str] = None, *, declaration: Optional[dict] = None,
                 verified: Optional[dict] = None, aliases: Optional[Dict[str, str]] = None):
        self.load_error = None
        try:
            self.declaration = declaration if declaration is not None else load_declaration(root)
            self.verified = verified if verified is not None else load_verified(root)
        except Exception as exc:                                        # noqa: BLE001
            self.declaration, self.verified = None, None
            self.load_error = "%s: %s" % (type(exc).__name__, exc)
        self.aliases = aliases if aliases is not None else _aliases()

    def __call__(self, ticker) -> dict:
        return verdict(ticker, resolver=self)

    def meta(self) -> dict:
        d = self.declaration or {}
        return {"declaration": DECLARATION, "declared_by": d.get("declared_by"),
                "declared_on": d.get("declared_on"),
                "symbol_map_as_of": (self.verified or {}).get("as_of"),
                "venue_source": "%s.verified[].exchange" % SYMBOL_MAP_FILE,
                "load_error": self.load_error}


def verdict(ticker, *, resolver: Optional[Resolver] = None, root: Optional[str] = None) -> dict:
    """-> {ticker, yahoo_symbol, venue, state, admissible_for_new_capital, why, ...}."""
    rs = resolver or Resolver(root)
    out = {"ticker": ticker, "yahoo_symbol": None, "venue": None, "state": UNKNOWN_VENUE,
           "admissible_for_new_capital": False,
           "venue_source": "%s.verified[].exchange" % SYMBOL_MAP_FILE,
           "symbol_map_as_of": (rs.verified or {}).get("as_of"),
           "declared_on": (rs.declaration or {}).get("declared_on"), "why": None}
    if not isinstance(ticker, str) or not ticker.strip():
        out["why"] = "malformed ticker %r" % (ticker,)
        return out
    if rs.load_error:
        out["why"] = "dealability inputs unavailable (%s) - refused, not assumed" % rs.load_error
        return out
    t = ticker.strip()
    sym = rs.aliases.get(t, t)
    out["yahoo_symbol"] = sym
    e = rs.verified["by_symbol"].get(sym)
    if e is None:
        out["why"] = ("%s has no VERIFIED listing in %s (symbol %s) - its venue is not "
                      "established, so it cannot receive automated new capital" % (t, SYMBOL_MAP_FILE, sym))
        return out
    code = e.get("exchange")
    if not isinstance(code, str) or not code.strip():
        out["why"] = "verified entry for %s carries no exchange code" % sym
        return out
    code = code.strip().upper()
    out["venue"] = code
    out["currency"] = e.get("currency")
    v = (rs.declaration.get("venues") or {}).get(code)
    if v is None:
        out["state"] = UNDECLARED_VENUE
        out["why"] = ("verified venue %s is not in %s; an undeclared venue refuses (Raj "
                      "23-Sep-2026: only a declared ONLINE venue admits)" % (code, DECLARATION))
        return out
    if v.get("routing") != ONLINE:
        out["state"] = NOT_DEALABLE_ONLINE
        out["mechanism"] = v.get("mechanism")
        out["why"] = ("venue %s is declared NON_ONLINE for automated new capital (%s)"
                      % (code, v.get("mechanism") or "no mechanism stated"))
        return out
    out["state"] = DEALABLE_ONLINE
    out["admissible_for_new_capital"] = True
    out["why"] = "verified venue %s is declared ONLINE" % code
    return out


def assess(tickers: Iterable, resolver: Optional[Resolver] = None, root: Optional[str] = None) -> dict:
    rs = resolver or Resolver(root)
    return {t: verdict(t, resolver=rs) for t in tickers}


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)[:200]) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    DEC = {"declared_by": "fixture", "declared_on": "2026-09-23",
           "venues": {"NMS": {"routing": "ONLINE"}, "LSE": {"routing": "ONLINE"},
                      "WSE": {"routing": "NON_ONLINE", "mechanism": "RSP/telephone"}}}
    VER = {"as_of": "2026-09-12", "by_symbol": {
        "ZAB.WA": {"ticker": "ZAB.WA", "exchange": "WSE", "currency": "PLN"},
        "HALO": {"ticker": "HALO", "exchange": "NMS", "currency": "USD"},
        "ONT.L": {"ticker": "ONT.L", "exchange": "LSE", "currency": "GBp"},
        "LOTB.BR": {"ticker": "LOTB.BR", "exchange": "BRU", "currency": "EUR"},
        "BAD": {"ticker": "BAD", "exchange": ""}}}
    rs = Resolver(declaration=DEC, verified=VER, aliases={"ONT": "ONT.L"})
    ok("MUST-FIRE: the Sep-2026 Warsaw shape (ZAB.WA on WSE) must not be admissible",
       rs("ZAB.WA")["state"] == NOT_DEALABLE_ONLINE and not rs("ZAB.WA")["admissible_for_new_capital"],
       rs("ZAB.WA"))
    ok("POSITIVE CONTROL: a verified NASDAQ name (HALO/NMS) is DEALABLE_ONLINE",
       rs("HALO")["state"] == DEALABLE_ONLINE and rs("HALO")["admissible_for_new_capital"], rs("HALO"))
    ok("POSITIVE CONTROL: a declared broker alias resolves (ONT -> ONT.L -> LSE online)",
       rs("ONT")["state"] == DEALABLE_ONLINE and rs("ONT")["venue"] == "LSE", rs("ONT"))
    ok("MUST-FIRE: an unmapped ticker (FRO) must refuse as UNKNOWN_VENUE, never default to NASDAQ",
       rs("FRO")["state"] == UNKNOWN_VENUE and rs("FRO")["venue"] is None, rs("FRO"))
    ok("MUST-FIRE: a verified venue the declaration does not list must refuse (UNDECLARED_VENUE)",
       rs("LOTB.BR")["state"] == UNDECLARED_VENUE, rs("LOTB.BR"))
    ok("MUST-FIRE: a malformed ticker / empty exchange code must refuse",
       rs(None)["state"] == UNKNOWN_VENUE and rs("BAD")["state"] == UNKNOWN_VENUE)
    ok("NEGATIVE CONTROL: the suffix is not read - an unverified '.L' ticker must not be admitted",
       rs("NOPE.L")["state"] == UNKNOWN_VENUE)
    broken = Resolver(declaration=None, verified=None, root="/nonexistent-dir-for-selftest")
    ok("MUST-FIRE: unreadable inputs must refuse every name, not admit it",
       broken("HALO")["state"] == UNKNOWN_VENUE and broken.load_error)
    # the real on-disk declaration: WSE non-online, the 16 declared codes online
    try:
        real = load_declaration()
        ven = real["venues"]
        online = sorted(k for k, v in ven.items() if v["routing"] == ONLINE)
        ok("the declared table carries Raj's 16 ONLINE codes and WSE as NON_ONLINE",
           online == sorted("NMS NYQ NGM NCM LSE STO PAR GER EBS AMS MIL HEL OSL MCE ISE BRU".split())
           and ven.get("WSE", {}).get("routing") == "NON_ONLINE", online)
    except DealabilityRefused as exc:
        ok("the declared table is readable", False, exc)
    print("broker_dealability selftest: %d FAIL(s)" % len(fails))
    assert not fails, "broker_dealability negative controls must not fail: %s" % fails
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    rs = Resolver()
    print(json.dumps({"meta": rs.meta(), "verdicts": assess(sys.argv[1:], rs)}, indent=1))
