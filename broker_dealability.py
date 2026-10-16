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
NOT_DEALABLE_IN_WRAPPER = "NOT_DEALABLE_IN_WRAPPER"     # ISA-0854: instrument excluded from THIS wrapper (ISA)
NOT_DEALABLE_AT_BROKER = "NOT_DEALABLE_AT_BROKER"       # ISA-0854 amend: broker will not deal it in ANY wrapper (e.g. not CREST-settleable)
NOT_ISA_QUALIFYING_DR = "NOT_ISA_QUALIFYING_DR"         # ISA-0860: a DR whose underlying has NO recognised listing (ESTABLISHED)
STATES = (DEALABLE_ONLINE, NOT_DEALABLE_ONLINE, UNDECLARED_VENUE, UNKNOWN_VENUE, NOT_DEALABLE_IN_WRAPPER,
          NOT_DEALABLE_AT_BROKER, NOT_ISA_QUALIFYING_DR)
OUTCOMES_FILE = "dealability_outcomes.jsonl"            # ISA-0860: Raj's recorded dealing outcomes (APPEND-ONLY)
OUTCOMES = ("DEALT", "CONFIRMED_DEALABLE", "REFUSED")
WRAPPER = "ISA"                                          # the account this framework allocates
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


def load_outcomes(root: Optional[str] = None) -> Dict[str, dict]:
    """ISA-0860 - Raj's RECORDED dealing outcomes, latest row per ticker wins. A REFUSED outcome is a declaration of
    record (refuses new capital); DEALT / CONFIRMED_DEALABLE clears every confirm flag. Unreadable rows are skipped
    and counted, never guessed."""
    p = os.path.join(root or HERE, OUTCOMES_FILE)
    out: Dict[str, dict] = {}
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("ticker") and r.get("outcome") in OUTCOMES:
                out[str(r["ticker"]).strip()] = r
    return out


def record_outcome(ticker: str, outcome: str, recorded_by: str, root: Optional[str] = None, *, wrapper: str = WRAPPER,
                   reason_code: Optional[str] = None, evidence: Optional[str] = None, receipt_id: Optional[str] = None,
                   month: Optional[str] = None, flags_at_decision: Optional[list] = None,
                   recorded_at: Optional[str] = None) -> dict:
    """ISA-0860 - the ONE writer of dealability_outcomes.jsonl (APPEND-ONLY). Raj's result of the dealing check
    becomes a declaration of record (asked once per name) AND the learning record that measures each confirm
    flag's precision. Never edits broker_venues.json (signed config): a build folds outcomes into it."""
    import datetime as _dt
    t = str(ticker or "").strip()
    if not t:
        raise DealabilityRefused("record_outcome: ticker is empty")
    if outcome not in OUTCOMES:
        raise DealabilityRefused("record_outcome: outcome %r not in %s" % (outcome, OUTCOMES))
    if not str(recorded_by or "").strip():
        raise DealabilityRefused("record_outcome: recorded_by is required - an outcome is somebody's statement")
    w = str(wrapper or WRAPPER).upper()
    if outcome == "REFUSED" and w not in (WRAPPER, "ALL"):
        raise DealabilityRefused("record_outcome: a refusal names its wrapper: ISA or ALL (got %r)" % wrapper)
    row = {"schema": "dealability_outcome_v1", "ticker": t, "outcome": outcome, "wrapper": w,
           "reason_code": reason_code, "evidence": evidence, "recorded_by": recorded_by,
           "recorded_at": recorded_at or _dt.datetime.now().isoformat(timespec="seconds"),
           "receipt_id": receipt_id, "month": month, "flags_at_decision": list(flags_at_decision or [])}
    p = os.path.join(root or HERE, OUTCOMES_FILE)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"state": "RECORDED", "row": row, "path": OUTCOMES_FILE}


def flag_precision(root: Optional[str] = None) -> dict:
    """ISA-0860 learning loop: for each confirm-flag class, how often Raj's recorded outcome was a REFUSAL. The
    evidence that would promote a weak flag (CUSTODY_ROUTE_CONFIRM) to a refusal, or retire it."""
    by: Dict[str, dict] = {}
    for r in load_outcomes(root).values():
        for f in r.get("flags_at_decision") or ["NO_FLAG"]:
            b = by.setdefault(f, {"n": 0, "refused": 0})
            b["n"] += 1
            b["refused"] += 1 if r.get("outcome") == "REFUSED" else 0
    for b in by.values():
        b["refusal_rate"] = round(b["refused"] / b["n"], 3) if b["n"] else None
        b["sufficient"] = b["n"] >= 10
    return {"by_flag": by, "rule": "a flag class is re-decided (promote to refusal / retire) only with >= 10 recorded outcomes"}


def _eligibility(root: Optional[str] = None) -> Dict[str, dict]:
    try:
        import instrument_eligibility as _ie
        return _ie.verdicts(root)
    except Exception:                                                   # noqa: BLE001
        return {}


def _alias_states(root: Optional[str] = None) -> Dict[str, str]:
    try:
        import stock_price_fetch as _spf
        return dict(_spf.alias_states(os.path.join(root or HERE, SYMBOL_MAP_FILE)))
    except Exception:                                                   # noqa: BLE001
        return {}


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
                 verified: Optional[dict] = None, aliases: Optional[Dict[str, str]] = None,
                 alias_states: Optional[Dict[str, str]] = None, eligibility: Optional[Dict[str, dict]] = None,
                 outcomes: Optional[Dict[str, dict]] = None):
        self.load_error = None
        # ⚑ ISA-0860 - the eligibility verdicts (cache, never a network call) and Raj's recorded outcomes. A
        #   fixture that injects declaration/verified without these gets {} -> every name ELIGIBILITY_UNVERIFIED
        #   (fundable, flagged) - absence is never read as eligible-without-a-flag.
        self.eligibility = eligibility if eligibility is not None else (
            _eligibility(root) if declaration is None and verified is None else {})
        self.outcomes = outcomes if outcomes is not None else (
            load_outcomes(root) if declaration is None and verified is None else {})
        try:
            self.declaration = declaration if declaration is not None else load_declaration(root)
            self.verified = verified if verified is not None else load_verified(root)
        except Exception as exc:                                        # noqa: BLE001
            self.declaration, self.verified = None, None
            self.load_error = "%s: %s" % (type(exc).__name__, exc)
        self.aliases = aliases if aliases is not None else _aliases()
        # ⚑ ISA-0725 — a DECLARED alias (ticker != symbol) is admitted only when the last
        # Step-5x refresh exchange-VERIFIED it. Declaration is provenance, not verification.
        self.alias_states = (alias_states if alias_states is not None
                             else _alias_states(root))

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
    # ⚑ ISA-0854 (Raj 11-Oct-2026): a venue the broker deals online can still carry an INSTRUMENT the broker
    #   will not hold in THIS wrapper (VEON ADS: dealable in a SIPP, not in an ISA). The declared instrument
    #   exclusion is tested FIRST, by ticker or by its Yahoo symbol, and refuses new capital by name.
    _ex = ((rs.declaration or {}).get("instrument_exclusions") or {})
    _hit = _ex.get(t) or _ex.get(sym)
    # ⚑ ISA-0854 amendment (Raj 11-Oct-2026 18:11 BST): wrapper "ALL" = the broker will not deal the instrument in
    #   ANY account (GLBE: cannot be cleared through CREST). Kept a DISTINCT state from the wrapper exclusion -
    #   the cause differs (settlement route vs wrapper eligibility) and a SIPP would not rescue it.
    if isinstance(_hit, dict) and str(_hit.get("wrapper") or "").upper() == "ALL":
        out["state"] = NOT_DEALABLE_AT_BROKER
        out["wrapper"] = "ALL"
        out["reason_code"] = _hit.get("reason_code")
        out["why"] = ("%s is declared NOT dealable at the broker in any wrapper (%s; %s, declared by %s on %s; %s)"
                      % (t, _hit.get("reason_code") or "no reason code", _hit.get("evidence") or "no evidence stated",
                         _hit.get("declared_by"), _hit.get("declared_on"), _hit.get("register") or "no register item"))
        return out
    if isinstance(_hit, dict) and str(_hit.get("wrapper") or "").upper() == WRAPPER:
        out["state"] = NOT_DEALABLE_IN_WRAPPER
        out["wrapper"] = WRAPPER
        out["why"] = ("%s is declared NOT dealable inside the %s wrapper (%s, declared by %s on %s; %s)"
                      % (t, WRAPPER, _hit.get("evidence") or "no evidence stated", _hit.get("declared_by"),
                         _hit.get("declared_on"), _hit.get("register") or "no register item"))
        return out
    if sym != t:
        _ast = (rs.alias_states or {}).get(t)
        out["alias_state"] = _ast or "NEVER_VERIFIED"
        if _ast != "VERIFIED":
            out["why"] = ("%s is a DECLARED alias of %s that the last symbol-map refresh did not "
                          "exchange-verify (state %s) - a declaration is provenance, not "
                          "verification (ISA-0725), so it cannot receive automated new capital"
                          % (t, sym, out["alias_state"]))
            return out
    # ⚑ ISA-0860 (Raj 12-Oct-2026, 'refuse only established') - THE ISA-ELIGIBILITY STAGE, before the venue test.
    #   (1) Raj's recorded dealing outcome is a declaration of record: REFUSED refuses; DEALT clears every flag.
    #   (2) Raj's signed instrument_eligibility declaration (broker_venues.json) clears the flags.
    #   (3) the mechanical verdict (instrument_eligibility.json, OpenFIGI + HMRC table): NOT_ISA_QUALIFYING_DR refuses;
    #       every uncertain outcome stays FUNDABLE and is carried as confirm_required[] for the receipt's fallbacks.
    _oc = (rs.outcomes or {}).get(t) or (rs.outcomes or {}).get(sym)
    if isinstance(_oc, dict) and _oc.get("outcome") == "REFUSED":
        _w = str(_oc.get("wrapper") or WRAPPER).upper()
        out["state"] = NOT_DEALABLE_AT_BROKER if _w == "ALL" else NOT_DEALABLE_IN_WRAPPER
        out["wrapper"] = _w
        out["reason_code"] = _oc.get("reason_code")
        out["why"] = ("%s was REFUSED at dealing (%s wrapper %s; %s), recorded by %s on %s in %s - a declaration of "
                      "record (ISA-0860)" % (t, _oc.get("reason_code") or "no reason code", _w, _oc.get("evidence") or "no evidence",
                                             _oc.get("recorded_by"), _oc.get("recorded_at"), OUTCOMES_FILE))
        return out
    _decl = (((rs.declaration or {}).get("instrument_eligibility") or {}).get(t)
             or ((rs.declaration or {}).get("instrument_eligibility") or {}).get(sym))
    _cleared = None
    if isinstance(_decl, dict) and _decl.get("state") == "ISA_ELIGIBLE":
        _cleared = "declared ISA_ELIGIBLE by %s on %s (broker_venues.json)" % (_decl.get("declared_by"), _decl.get("declared_on"))
    elif isinstance(_oc, dict) and _oc.get("outcome") in ("DEALT", "CONFIRMED_DEALABLE"):
        _cleared = "recorded %s by %s on %s (%s)" % (_oc.get("outcome"), _oc.get("recorded_by"), _oc.get("recorded_at"), OUTCOMES_FILE)
    _ev = (rs.eligibility or {}).get(sym)
    if not isinstance(_ev, dict):
        _ev = {"state": "CONFIRM_REQUIRED", "reasons": ["ELIGIBILITY_UNVERIFIED"],
               "why": "no instrument_eligibility record for %s (cache absent or the name not yet refreshed)" % sym}
    out["eligibility"] = {"state": _ev.get("state"), "reasons": list(_ev.get("reasons") or []), "why": _ev.get("why"),
                          "instrument_type": _ev.get("instrument_type"), "cleared_by": _cleared}
    if _ev.get("state") == NOT_ISA_QUALIFYING_DR and not _cleared:
        out["state"] = NOT_ISA_QUALIFYING_DR
        out["why"] = "%s is not ISA-qualifying (ESTABLISHED, ISA-0860): %s" % (t, _ev.get("why"))
        return out
    _confirm = [] if _cleared else [{"code": c, "why": _ev.get("why")} for c in (_ev.get("reasons") or [])]
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
    out["confirm_required"] = _confirm
    out["why"] = "verified venue %s is declared ONLINE" % code + (
        "; CONFIRM AT DEALING: %s (ISA-0860 - fundable; the receipt pre-authorises a fallback)"
        % ", ".join(c["code"] for c in _confirm) if _confirm else "")
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
    rs = Resolver(declaration=DEC, verified=VER, aliases={"ONT": "ONT.L"},
                  alias_states={"ONT": "VERIFIED"})
    _DECX = dict(DEC, instrument_exclusions={"VEON": {"wrapper": "ISA", "declared_by": "Raj", "declared_on": "2026-10-11",
                                                      "evidence": "VEON ADS dealable in a SIPP, not an ISA", "register": "ISA-0854"}})
    _VERX = {"as_of": "2026-10-11", "by_symbol": dict(VER["by_symbol"], VEON={"ticker": "VEON", "exchange": "NMS", "currency": "USD"})}
    _rsx = Resolver(declaration=_DECX, verified=_VERX, aliases={}, alias_states={})
    ok("ISA-0854 MUST-FIRE: VEON on an ONLINE NASDAQ venue is NOT_DEALABLE_IN_WRAPPER (ISA) and refuses new capital",
       _rsx("VEON")["state"] == NOT_DEALABLE_IN_WRAPPER and not _rsx("VEON")["admissible_for_new_capital"]
       and "SIPP" in _rsx("VEON")["why"], _rsx("VEON"))
    ok("ISA-0854 NEGATIVE CONTROL: a NASDAQ common stock on the same venue is still DEALABLE_ONLINE",
       _rsx("HALO")["state"] == DEALABLE_ONLINE and _rsx("HALO")["admissible_for_new_capital"])
    _rsy = Resolver(declaration=dict(DEC, instrument_exclusions={"VEON": {"wrapper": "SIPP"}}), verified=_VERX, aliases={}, alias_states={})
    ok("ISA-0854 NEGATIVE CONTROL: an exclusion declared for ANOTHER wrapper does not refuse an ISA verdict",
       _rsy("VEON")["state"] == DEALABLE_ONLINE)
    _DECG = dict(DEC, instrument_exclusions={"GLBE": {"wrapper": "ALL", "reason_code": "NOT_CREST_SETTLEABLE",
                                                      "declared_by": "Raj", "declared_on": "2026-10-11", "register": "ISA-0854"}})
    _VERG = {"as_of": "2026-10-11", "by_symbol": dict(VER["by_symbol"], GLBE={"ticker": "GLBE", "exchange": "NMS", "currency": "USD"})}
    _rsg = Resolver(declaration=_DECG, verified=_VERG, aliases={}, alias_states={})
    ok("ISA-0854 amend MUST-FIRE: GLBE (NASDAQ, ONLINE venue) declared wrapper ALL is NOT_DEALABLE_AT_BROKER and refuses new capital",
       _rsg("GLBE")["state"] == NOT_DEALABLE_AT_BROKER and not _rsg("GLBE")["admissible_for_new_capital"]
       and "NOT_CREST_SETTLEABLE" in _rsg("GLBE")["why"], _rsg("GLBE"))
    ok("ISA-0854 amend NEGATIVE CONTROL: a broker-wide exclusion of GLBE leaves HALO on the same venue DEALABLE_ONLINE",
       _rsg("HALO")["state"] == DEALABLE_ONLINE)
    # ⚑ ISA-0860 - the eligibility stage (fixture verdicts; no network)
    _E = {"VEONX": {"state": "NOT_ISA_QUALIFYING_DR", "reasons": [], "why": "every underlying listing revoked/MTF"},
          "HALO": {"state": "ELIGIBLE", "reasons": [], "why": "Common Stock"},
          "ARGX": {"state": "CONFIRM_REQUIRED", "reasons": ["DR_DECLARATION_PENDING"], "why": "evidence BB"},
          "GLBE": {"state": "CONFIRM_REQUIRED", "reasons": ["CUSTODY_ROUTE_CONFIRM"], "why": "US ordinary shares"}}
    _VE = {"as_of": "2026-10-12", "by_symbol": dict(VER["by_symbol"], **{k: {"ticker": k, "exchange": "NMS", "currency": "USD"}
                                                                         for k in ("VEONX", "ARGX", "GLBE", "NEWCO")})}
    _rse = Resolver(declaration=DEC, verified=_VE, aliases={}, alias_states={}, eligibility=_E, outcomes={})
    ok("ISA-0860 MUST-FIRE: an ESTABLISHED non-qualifying DR refuses new capital (NOT_ISA_QUALIFYING_DR)",
       _rse("VEONX")["state"] == NOT_ISA_QUALIFYING_DR and not _rse("VEONX")["admissible_for_new_capital"], _rse("VEONX"))
    ok("ISA-0860 NEGATIVE CONTROL: an ELIGIBLE common stock is DEALABLE_ONLINE with NO confirm flag",
       _rse("HALO")["state"] == DEALABLE_ONLINE and _rse("HALO")["confirm_required"] == [])
    ok("ISA-0860 (Raj 12-Oct): an uncertain DR stays FUNDABLE and carries DR_DECLARATION_PENDING",
       _rse("ARGX")["admissible_for_new_capital"] and [c["code"] for c in _rse("ARGX")["confirm_required"]] == ["DR_DECLARATION_PENDING"])
    ok("ISA-0860 MUST-FIRE (no refusal on missing data): a name with no eligibility record is fundable + ELIGIBILITY_UNVERIFIED",
       _rse("NEWCO")["admissible_for_new_capital"] and [c["code"] for c in _rse("NEWCO")["confirm_required"]] == ["ELIGIBILITY_UNVERIFIED"])
    _rsd = Resolver(declaration=dict(DEC, instrument_eligibility={"ARGX": {"state": "ISA_ELIGIBLE", "declared_by": "Raj", "declared_on": "2026-10-12"}}),
                    verified=_VE, aliases={}, alias_states={}, eligibility=_E, outcomes={})
    ok("ISA-0860: Raj's ISA_ELIGIBLE declaration clears the DR flag", _rsd("ARGX")["confirm_required"] == []
       and "Raj" in (_rsd("ARGX")["eligibility"]["cleared_by"] or ""))
    _rso = Resolver(declaration=DEC, verified=_VE, aliases={}, alias_states={}, eligibility=_E,
                    outcomes={"GLBE": {"ticker": "GLBE", "outcome": "REFUSED", "wrapper": "ALL", "reason_code": "NOT_CREST_SETTLEABLE",
                                       "recorded_by": "Raj", "recorded_at": "2026-11-02"},
                              "ARGX": {"ticker": "ARGX", "outcome": "DEALT", "recorded_by": "Raj", "recorded_at": "2026-11-02"}})
    ok("ISA-0860 MUST-FIRE: a RECORDED broker refusal is a declaration of record (wrapper ALL -> NOT_DEALABLE_AT_BROKER)",
       _rso("GLBE")["state"] == NOT_DEALABLE_AT_BROKER and not _rso("GLBE")["admissible_for_new_capital"], _rso("GLBE"))
    ok("ISA-0860: a recorded DEALT outcome clears the confirm flags", _rso("ARGX")["confirm_required"] == [])
    import tempfile as _tf860
    _d860 = _tf860.mkdtemp(prefix="bd860_")
    record_outcome("ZZZ", "REFUSED", "fixture", root=_d860, wrapper="ISA", reason_code="NOT_ISA_QUALIFYING",
                   flags_at_decision=["DR_DECLARATION_PENDING"])
    record_outcome("YYY", "DEALT", "fixture", root=_d860, flags_at_decision=["CUSTODY_ROUTE_CONFIRM"])
    _lo = load_outcomes(_d860)
    ok("ISA-0860: record_outcome appends; load_outcomes reads the latest per ticker; flag_precision counts by flag",
       _lo["ZZZ"]["outcome"] == "REFUSED" and flag_precision(_d860)["by_flag"]["DR_DECLARATION_PENDING"]["refused"] == 1
       and flag_precision(_d860)["by_flag"]["CUSTODY_ROUTE_CONFIRM"]["refusal_rate"] == 0.0)
    try:
        record_outcome("ZZZ", "REFUSED", "", root=_d860)
        ok("record_outcome refuses an anonymous outcome", False)
    except DealabilityRefused:
        ok("record_outcome refuses an anonymous outcome", True)
    _real = Resolver()
    ok("ISA-0854 LIVE DECLARATION: the delivered broker_venues.json refuses VEON in the ISA",
       (((_real.declaration or {}).get("instrument_exclusions") or {}).get("VEON") or {}).get("wrapper") == "ISA"
       and _real("VEON")["state"] == NOT_DEALABLE_IN_WRAPPER)
    ok("ISA-0854 amend LIVE DECLARATION: the delivered broker_venues.json refuses GLBE at the broker (wrapper ALL)",
       (((_real.declaration or {}).get("instrument_exclusions") or {}).get("GLBE") or {}).get("wrapper") == "ALL"
       and _real("GLBE")["state"] == NOT_DEALABLE_AT_BROKER)
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
    # ⚑ ISA-0725 MUST-FIRE: the same alias, NOT verified (contradicted / never refreshed)
    for _st in ("CONTRADICTED", None, "NO_VENUE_EXPECTATION", "FETCH_FAILED"):
        _rs = Resolver(declaration=DEC, verified=VER, aliases={"ONT": "ONT.L"},
                       alias_states=({"ONT": _st} if _st else {}))
        ok("MUST-FIRE: a declared alias in state %s refuses as UNKNOWN_VENUE" % _st,
           _rs("ONT")["state"] == UNKNOWN_VENUE and not _rs("ONT")["admissible_for_new_capital"]
           and _rs("ONT")["alias_state"] == (_st or "NEVER_VERIFIED"), _rs("ONT"))
    ok("NEGATIVE CONTROL: an identity mapping (HALO->HALO) needs no alias verification",
       Resolver(declaration=DEC, verified=VER, aliases={"HALO": "HALO"}, alias_states={})("HALO")
       ["state"] == DEALABLE_ONLINE)
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
    ok("ISA-0860 LIVE DECLARATION: Raj's 12-Oct ISA_ELIGIBLE declarations clear ARGX/ASML/GMAB/ONC",
       all(((_real.declaration or {}).get("instrument_eligibility") or {}).get(k, {}).get("state") == "ISA_ELIGIBLE"
           for k in ("ARGX", "ASML", "GMAB", "ONC")))
    print("broker_dealability selftest: %d FAIL(s)" % len(fails))
    assert not fails, "broker_dealability negative controls must not fail: %s" % fails
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    rs = Resolver()
    print(json.dumps({"meta": rs.meta(), "verdicts": assess(sys.argv[1:], rs)}, indent=1))
