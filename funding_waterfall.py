#!/usr/bin/env python3
"""
funding_waterfall.py - ISA-0858 (12-Oct-2026): THE ONE DEPLOYABLE-CASH ROUTING RULE and ONE dated funding /
reservation view, read by the capital decision engine, the marginal-pound router and the review before any
final proposal. Pure functions + one file-reading entry point (`build`). It never trades and never writes.

RAJ'S DECISIONS (ISA-0858 rev3/rev4; in session 11-Oct-2026 and 12-Oct-2026)
  D1  stock sleeve below 25% of NAV: qualifying stocks take FIRST CALL on deployable capital; 25-30%: stocks and
      funds compete (stocks on CASH only - no comparable fund ER until ISA-0859); 30% is a HARD CEILING for new
      stock capital (sell-to-buy replacement only; price drift above 30% forces no sale and permits no addition).
      The sleeve is measured AT DECISION TIME including fills not yet in the broker book.
  D3  OPERATIONAL absence of a stock decision (control block, receipt not authorised, data refusal, a decided
      trade still to execute) -> MMF; ECONOMIC absence (no qualifying candidate) -> funds via the waiting room.
      The D23 GBP 250 reserve and the B1 drawdown reservation stay cash.
  D4  fund trims MAY fund qualifying stock positions ONLY while the POST-TRADE sleeve is <= 25%; sourcing order
      cash/MMF -> stock sale proceeds -> fund trims.
  D5  the scaling-freeze reallocation_only bar is RETIRED; the brake on FUND-FUNDED stock capital is the
      retention.py three-leg step-down ratchet (D18/D19). D5a: while ACTIVE_UNMEASURED, fund-funded buys are
      ALLOWED and labelled 'brake UNMEASURED'. D5b: when it FIRES, fund-funded buys stop, cash-funded buys
      continue up to 30%, and the step-down ceiling is REPORTED, not enforced (no forced sale).
  D8  a fund-funded stock order is released only after the fund-sale proceeds SETTLE - never bridged from the
      D23 reserve or the drawdown reservation.
  B1.6b (adopted): a FIRED drawdown tranche takes its pounds BEFORE any stock deployment (the 11-Oct audit's
      GBP 9,000 scenario double-claimed GBP 2,583.33 because the router sized on the same cash).

ORDER OF CLAIMS (each pound claimed once):  D23 reserve -> fired drawdown tranches -> stock receipt (cash) ->
  stock receipt (fund trims, D4/D5) -> funds (ECONOMIC residual via the waiting room) -> MMF (OPERATIONAL).
Reservations are idempotent: a drawdown tranche is keyed by (episode, threshold) and stays reserved until the
dealing record shows the VUAG purchase; a proposed SALE is never purchasing power until its proceeds settle.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from framework_integrity import mark as _fi_mark            # execution ledger (R4.14)
except Exception:                                                   # noqa: BLE001
    def _fi_mark(*_a, **_k):                                        # noqa: D103
        return None

SCHEMA_VERSION = "1.0.0"
EPS = 0.005
PARK_OPERATIONAL, PARK_ECONOMIC = "OPERATIONAL", "ECONOMIC"
TRANCHE_EXECUTED_FRACTION = 0.9      # a VUAG buy >= 90% of the tranche on/after its fire date executes it


class WaterfallRefused(Exception):
    """A declared input the routing rule needs is missing (R4.1/R4.7) - never defaulted."""


# ── policy ───────────────────────────────────────────────────────────────────────────────────────────
def ceilings(tw: dict) -> dict:
    """-> the declared D1 bands. ONE HOME each: the hard ceiling is target_weights.stock_sleeve.sleeve_ceiling
    (read as the one stock-sleeve policy contract by new_capital_control.stock_sleeve_policy, ISA-0805), the
    priority band is stock_sleeve.sleeve_priority_ceiling. Both fractions of NAV. Missing -> REFUSED (R4.1)."""
    ss = (tw or {}).get("stock_sleeve") or {}
    p, h = ss.get("sleeve_priority_ceiling"), ss.get("sleeve_ceiling")
    if p is None or h is None:
        raise WaterfallRefused("target_weights.stock_sleeve.sleeve_priority_ceiling / sleeve_ceiling NOT DECLARED "
                               "(Raj D1, ISA-0858) - the routing rule cannot be applied without them (R4.1)")
    p, h = float(p) * 100.0, float(h) * 100.0
    if not (0 < p <= h <= 100):
        raise WaterfallRefused("declared sleeve bands are inconsistent: priority %.2f%% / hard %.2f%%" % (p, h))
    res = ss.get("cash_reserve_gbp")
    if res is None:
        raise WaterfallRefused("target_weights.stock_sleeve.cash_reserve_gbp NOT DECLARED (D23, R4.1)")
    return {"priority_ceiling_pct": p, "hard_ceiling_pct": h, "cash_reserve_gbp": float(res),
            "basis": ss.get("sleeve_ceiling_basis")}


# ── the sleeve at decision time ─────────────────────────────────────────────────────────────────────
def _book_date(portfolio: dict) -> Optional[str]:
    bd = str(((portfolio or {}).get("_meta") or {}).get("data_date") or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%b-%y"):
        try:
            return datetime.datetime.strptime(bd, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _mmf_tickers() -> set:
    try:
        import scoring_config as _sc
        return {str(t).upper() for t in getattr(_sc, "MMF_TICKERS", ())}
    except Exception:                                                   # noqa: BLE001
        return {"CSH2", "CSH2.L"}


def sleeve_at_decision(portfolio: dict, ledger_entries: Optional[List[dict]] = None) -> dict:
    """-> the stock sleeve measured at decision time: the broker book PLUS direct-stock trades in the dealing
    record dated AFTER the book date (buys add, sells subtract, at dealt GBP). The MMF is cash, not stock.
    Without a book date the pending leg is UNMEASURED and named - never read as zero (R4.3)."""
    s = (portfolio or {}).get("summary") or {}
    nav = float(s["total_value_gbp"])
    book = float(s.get("stock_sleeve_value_gbp") or 0.0)
    bd = _book_date(portfolio)
    mmf = _mmf_tickers()
    rows, pend = [], 0.0
    if bd is None:
        state = "PENDING_UNMEASURED"
    else:
        state = "MEASURED"
        for e in ledger_entries or []:
            if str(e.get("date") or "") <= bd or e.get("type") not in ("buy", "sell"):
                continue
            tk = str(e.get("ticker") or "").upper()
            if e.get("asset_class") != "stock" or not tk or tk in mmf:
                continue
            g = abs(float(e.get("amount_gbp") or 0.0))
            sgn = 1.0 if e["type"] == "buy" else -1.0
            pend += sgn * g
            rows.append({"date": e.get("date"), "ticker": tk, "type": e["type"], "gbp": round(g, 2),
                         "reference": e.get("reference")})
    sleeve = book + pend
    return {"state": state, "book_date": bd, "nav_gbp": round(nav, 2), "book_stock_gbp": round(book, 2),
            "pending_fills_gbp": round(pend, 2), "pending_fills": rows,
            "sleeve_gbp": round(sleeve, 2), "sleeve_pct": round(sleeve / nav * 100.0, 4) if nav else None,
            "basis": "broker book + dealing-record stock trades after the book date (ISA-0858 D1: incl. pending fills)"}


# ── B1.6b drawdown precedence ───────────────────────────────────────────────────────────────────────
def drawdown_reservation(state: Optional[dict], ledger_entries: Optional[List[dict]] = None) -> dict:
    """-> GBP reserved for FIRED drawdown tranches not yet executed. Keyed by (episode, threshold) so a re-run
    reserves the same pounds once (idempotent). Executed = a VUAG buy in the dealing record on/after the fire date
    for >= TRANCHE_EXECUTED_FRACTION of the tranche. A fired tranche with no size recorded is UNMEASURED and
    REFUSES (a reservation nobody can size must not be read as zero)."""
    st = state or {}
    log = list(st.get("tranche_log") or [])
    fired = {k for k, v in (st.get("tranches_fired") or {}).items() if v}
    ep = st.get("episode_id")
    logged = {(r.get("episode_id"), r.get("key")) for r in log}
    missing = sorted(k for k in fired if (ep, k) not in logged)
    if missing:
        return {"state": "UNMEASURED", "reserved_gbp": None, "rows": [],
                "why": ("tranche(s) %s fired in episode %s with no tranche_log size - the B1.6b reservation cannot be "
                        "sized and REFUSES (R4.3)" % (missing, ep))}
    buys = [e for e in (ledger_entries or []) if e.get("type") == "buy"
            and str(e.get("ticker") or "").upper().split(".")[0] == "VUAG"]
    rows, total = [], 0.0
    for r in log:
        if r.get("episode_id") != ep:
            continue
        g = float(r.get("tranche_gbp") or 0.0)
        ex = next((b for b in buys if str(b.get("date") or "") >= str(r.get("fired_on") or "9999")
                   and abs(float(b.get("amount_gbp") or 0.0)) >= TRANCHE_EXECUTED_FRACTION * g), None)
        rows.append({"episode_id": ep, "key": r.get("key"), "fired_on": r.get("fired_on"), "tranche_gbp": round(g, 2),
                     "executed": ex is not None, "executed_on": (ex or {}).get("date")})
        if ex is None:
            total += g
    return {"state": "MEASURED", "reserved_gbp": round(total, 2), "rows": rows,
            "basis": "B1.6b: fired, unexecuted drawdown tranches take their pounds BEFORE any stock deployment"}


# ── D5 brake ────────────────────────────────────────────────────────────────────────────────────────
def brake(portfolio: dict, counterfactual: Optional[dict]) -> dict:
    """-> the D5 brake on FUND-FUNDED stock capital, computed by its one home (retention.evaluate_ratchet)."""
    try:
        import retention as _rt
        legs = _rt.ratchet_inputs_from_freeze_history((counterfactual or {}).get("freeze_history"))
        s = (portfolio or {}).get("summary") or {}
        sw = float(s["stock_sleeve_value_gbp"]) / float(s["total_value_gbp"]) * 100.0
        rr = _rt.evaluate_ratchet(positions=(portfolio or {}).get("stocks") or [], current_sleeve_weight_pct=sw,
                                  months_measured=legs.get("months_measured"),
                                  months_trailing=legs.get("months_trailing"),
                                  sleeve_vs_vuag_pp=legs.get("sleeve_vs_vuag_pp"),
                                  sleeve_vs_vuag_exlargest_pp=legs.get("sleeve_vs_vuag_exlargest_pp"),
                                  largest_position_ticker=legs.get("largest_position_ticker"),
                                  early_warning_pp=legs.get("early_warning_pp"))
        state = rr.get("state")
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNAVAILABLE", "permits_fund_funded": False, "label": "brake UNAVAILABLE",
                "why": "retention ratchet could not be evaluated (%s: %s) - fund-funded stock capital REFUSES "
                       "(an unevaluated brake is not a released one, R4.3)" % (type(exc).__name__, exc)}
    fires = state == "FIRES"
    return {"state": state, "permits_fund_funded": not fires,
            "label": {"FIRES": "brake FIRED", "ACTIVE_UNMEASURED": "brake UNMEASURED"}.get(state, "brake %s" % state),
            "step_down_reported": rr.get("step_down") if fires else None,
            "step_down_enforced": False,
            "binding": rr.get("binding"), "unmeasured": rr.get("unmeasured"),
            "basis": ("Raj D5a/D5b 12-Oct-2026: FIRES -> fund-funded stock capital stops, cash-funded continues to "
                      "the hard ceiling, the step-down ceiling is reported not enforced; ACTIVE_UNMEASURED -> "
                      "fund-funded allowed and labelled")}


# ── the waterfall ───────────────────────────────────────────────────────────────────────────────────
def waterfall(*, cash_deployable_gbp: float, policy_bands: dict, sleeve: dict, drawdown: dict,
              brake_state: dict, donor_headroom_gbp: Optional[float]) -> dict:
    """-> the ordered claims and the capital the stock decision may use. Pure.

    cash_capital  = cash/MMF - D23 reserve - fired drawdown tranches               (B1.6b, D23)
    hard_room     = hard_ceiling x NAV - sleeve at decision time                    (D1: 30%)
    trim_capacity = min(donor headroom, priority_ceiling x NAV - sleeve - cash_capital), only if the brake
                    permits and the room is positive                               (D4/D5)
    stock_capital = min(cash_capital + trim_capacity, hard_room), floored at 0
    A fund-funded pound is spent only after every cash pound (sourcing order cash/MMF -> stock proceeds -> trims).
    """
    _fi_mark("funding_waterfall", "waterfall")
    nav = float(sleeve["nav_gbp"])
    sl = float(sleeve["sleeve_gbp"])
    res = float(policy_bands["cash_reserve_gbp"])
    dd = drawdown.get("reserved_gbp")
    if dd is None:
        raise WaterfallRefused("the drawdown reservation is UNMEASURED (%s) - B1.6b precedence cannot be honoured "
                               "and no stock capital is sized on cash a fired tranche may own" % drawdown.get("why"))
    if sleeve.get("state") != "MEASURED":
        raise WaterfallRefused("the sleeve at decision time is %s - D1 bands cannot be applied" % sleeve.get("state"))
    cash = float(cash_deployable_gbp)
    cash_capital = max(cash - res - float(dd), 0.0)
    pri = policy_bands["priority_ceiling_pct"] / 100.0 * nav
    hard = policy_bands["hard_ceiling_pct"] / 100.0 * nav
    hard_room = max(hard - sl, 0.0)
    pri_room_after_cash = pri - sl - cash_capital
    why_trim = []
    if not brake_state.get("permits_fund_funded"):
        trim = 0.0
        why_trim.append("D5: %s - fund-funded stock capital stops" % brake_state.get("label"))
    elif pri_room_after_cash <= EPS:
        trim = 0.0
        why_trim.append("D4: cash alone reaches the %.0f%% priority band (room after cash GBP %.2f) - fund trims may "
                        "not fund stocks at or above it" % (policy_bands["priority_ceiling_pct"], pri_room_after_cash))
    elif donor_headroom_gbp is None:
        trim = 0.0
        why_trim.append("donor headroom UNMEASURED - no fund trim is planned on an unmeasured donor (R4.3)")
    else:
        trim = max(min(float(donor_headroom_gbp), pri_room_after_cash), 0.0)
        why_trim.append("D4: min(donor headroom GBP %.2f, room to the %.0f%% band after cash GBP %.2f)"
                        % (float(donor_headroom_gbp), policy_bands["priority_ceiling_pct"], pri_room_after_cash))
    stock_capital = max(min(cash_capital + trim, hard_room), 0.0)
    trim_used_cap = max(stock_capital - cash_capital, 0.0)
    if hard_room <= EPS:
        binding = "HARD_CEILING"
    elif stock_capital < cash_capital + trim - EPS:
        binding = "HARD_CEILING_PARTIAL"
    elif trim_used_cap > EPS:
        binding = "PRIORITY_BAND_OR_DONOR_HEADROOM"
    else:
        binding = "CASH"
    band = ("BELOW_PRIORITY" if sl < pri - EPS else ("COMPETE_25_30" if sl < hard - EPS else "AT_OR_ABOVE_HARD"))
    lines = [
        {"order": 1, "claim": "D23 cash reserve", "gbp": round(res, 2), "destination": "CASH"},
        {"order": 2, "claim": "B1.6b fired drawdown tranches (unexecuted)", "gbp": round(float(dd), 2),
         "destination": "VUAG (drawdown protocol)"},
        {"order": 3, "claim": "stock decision - cash/MMF", "gbp_max": round(min(cash_capital, stock_capital), 2),
         "destination": "STOCK (receipt)"},
        {"order": 4, "claim": "stock decision - fund trims (D4, settle-first D8)", "gbp_max": round(trim_used_cap, 2),
         "destination": "STOCK (receipt)", "brake": brake_state.get("label")},
        {"order": 5, "claim": "ECONOMIC residual (no qualifying stock)", "destination": "FUNDS via the waiting room"},
        {"order": 6, "claim": "OPERATIONAL hold (block / refusal / decided trade to execute)", "destination": "MMF"}]
    return {"state": "OK", "schema_version": SCHEMA_VERSION,
            "nav_gbp": round(nav, 2), "sleeve_gbp": round(sl, 2), "sleeve_pct": sleeve.get("sleeve_pct"),
            "band": band, "cash_deployable_gbp": round(cash, 2), "cash_reserve_gbp": round(res, 2),
            "drawdown_reserved_gbp": round(float(dd), 2), "cash_capital_gbp": round(cash_capital, 2),
            "priority_ceiling_gbp": round(pri, 2), "hard_ceiling_gbp": round(hard, 2),
            "hard_room_gbp": round(hard_room, 2), "trim_capacity_gbp": round(trim, 2),
            "trim_capacity_why": why_trim, "stock_capital_gbp": round(stock_capital, 2),
            "max_fund_funded_gbp": round(trim_used_cap, 2), "binding": binding,
            "brake": brake_state, "settlement": "SETTLE_FIRST (Raj D8): a fund-funded stock order is released only "
                                                "after the fund-sale proceeds settle; never bridged from the D23 "
                                                "reserve or the drawdown reservation",
            "lines": lines,
            "replacement_only": hard_room <= EPS,
            "note": ("at or above the %.0f%% hard ceiling: new stock capital is 0 - sell-to-buy replacement only; price "
                     "drift above it forces no sale (D1)" % policy_bands["hard_ceiling_pct"]) if hard_room <= EPS else None}


def split_funding(spend_gbp: float, wf: dict) -> dict:
    """-> how a selected stock spend is funded: cash first, then fund trims (sourcing order D4)."""
    spend = float(spend_gbp)
    cash = min(spend, float(wf["cash_capital_gbp"]))
    trims = max(spend - cash, 0.0)
    if trims > float(wf["max_fund_funded_gbp"]) + EPS:
        raise WaterfallRefused("spend GBP %.2f needs GBP %.2f of fund trims > the GBP %.2f the waterfall permits"
                               % (spend, trims, wf["max_fund_funded_gbp"]))
    return {"spend_gbp": round(spend, 2), "cash_gbp": round(cash, 2), "fund_trims_gbp": round(trims, 2),
            "brake_label": (wf.get("brake") or {}).get("label") if trims > EPS else None,
            "sequencing": ("SELL FUNDS FIRST, release the stock order(s) after settlement (D8)" if trims > EPS
                           else "cash-funded: no sequencing constraint")}


def park_reason(stock_state: str) -> dict:
    """D3 - why stock-intended capital is not in a stock, and where it waits."""
    operational = {"STOCK_SLEEVE_REFUSED", "REFUSED_NEW_STOCK_CAPITAL_BLOCKED", "REFUSED_RECEIPT_NOT_AUTHORISED",
                   "DATA_REFUSED", "DECIDED_TRADE_PENDING_EXECUTION", "RESEARCH_PENDING", "SEARCH_INCOMPLETE",
                   "DECISION_TIE_UNRESOLVED", "REFUSED"}
    if stock_state in operational:
        return {"park_reason": PARK_OPERATIONAL, "destination": "MMF",
                "basis": "Raj D3: an operational absence of a stock decision waits in the MMF (recallable at once)"}
    return {"park_reason": PARK_ECONOMIC, "destination": "FUNDS_VIA_WAITING_ROOM",
            "basis": "Raj D3: no qualifying candidate - the pound goes to funds, tagged PARKED and recallable"}


def band_low_headroom(portfolio: dict, tw: dict) -> dict:
    """-> GBP each held fund can be sold down to its DECLARED band_low (never below it), summed. The same arithmetic
    waiting_room.donor_order publishes per donor as `sellable_to_band_low_gbp` - pure (no ranking is built, nothing is
    written), so the engine stays side-effect free (ISA-0858: calling the ranking from the engine wrote the month's
    strategic_allocation / process_concentration artefacts). A held fund with no declared band contributes 0 (it is
    not a donor the framework can size) and is named."""
    s = (portfolio or {}).get("summary") or {}
    nav = float(s.get("total_value_gbp") or 0.0)
    funds = (tw or {}).get("funds") or {}
    rows, tot, undeclared = {}, 0.0, []
    for f in (portfolio or {}).get("funds") or []:
        sd = f.get("ticker")
        lo = (funds.get(sd) or {}).get("band_low")
        if lo is None:
            undeclared.append(sd)
            continue
        h = max(float(f.get("value_gbp") or 0.0) - float(lo) * nav, 0.0)
        rows[sd] = round(h, 2)
        tot += h
    return {"state": "MEASURED", "headroom_gbp": round(tot, 2), "by_fund": rows, "undeclared_band_low": undeclared,
            "basis": "sum over held funds of max(value - band_low x NAV, 0) (waiting_room sellable_to_band_low)"}


# ── entry point ─────────────────────────────────────────────────────────────────────────────────────
def _json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                                   # noqa: BLE001
        return default


def build(portfolio: dict, *, root: str = HERE, tw: Optional[dict] = None, ledger_entries=None,
          drawdown_state: Optional[dict] = None, counterfactual: Optional[dict] = None,
          donor_headroom_gbp="auto", incoming: Optional[List[dict]] = None) -> dict:
    """Read the declared inputs from `root` (unless supplied) and return the waterfall + its inputs."""
    _fi_mark("funding_waterfall", "build")
    tw = tw if tw is not None else _json(os.path.join(root, "target_weights.json"))
    if ledger_entries is None:
        ledger_entries = (_json(os.path.join(root, "transaction_ledger.json"), {}) or {}).get("entries") or []
    drawdown_state = drawdown_state if drawdown_state is not None else _json(os.path.join(root, "drawdown_state.json"), {})
    counterfactual = counterfactual if counterfactual is not None else _json(os.path.join(root, "sleeve_counterfactual.json"), {})
    bands = ceilings(tw)
    sl = sleeve_at_decision(portfolio, ledger_entries)
    dd = drawdown_reservation(drawdown_state, ledger_entries)
    br = brake(portfolio, counterfactual)
    donors = None
    if donor_headroom_gbp == "auto":
        donors = band_low_headroom(portfolio, tw)
        donor_headroom_gbp = donors.get("headroom_gbp")
    s = portfolio.get("summary") or {}
    if s.get("cash_deployable_gbp") is None:
        raise WaterfallRefused("portfolio summary.cash_deployable_gbp absent - no current funding figure (ISA-0857)")
    wf = waterfall(cash_deployable_gbp=float(s["cash_deployable_gbp"]), policy_bands=bands, sleeve=sl,
                   drawdown=dd, brake_state=br, donor_headroom_gbp=donor_headroom_gbp)
    wf["inputs"] = {"bands": bands, "sleeve": sl, "drawdown": dd,
                    "donor_headroom_gbp": donor_headroom_gbp,
                    "donor_headroom": donors}
    return wf


# ── selftest ────────────────────────────────────────────────────────────────────────────────────────
def _selftest(verbose: bool = True) -> int:
    n = [0]

    def ok(cond, msg):
        n[0] += 1
        if not cond:
            raise AssertionError("funding_waterfall selftest FAIL: " + msg)

    bands = {"priority_ceiling_pct": 25.0, "hard_ceiling_pct": 30.0, "cash_reserve_gbp": 250.0}
    pf = lambda st, nav=100000.0, cash=0.0: {"_meta": {"data_date": "02-Oct-2026"},  # noqa: E731
                                              "summary": {"total_value_gbp": nav, "stock_sleeve_value_gbp": st,
                                                          "cash_deployable_gbp": cash}, "stocks": []}
    nodd = {"state": "MEASURED", "reserved_gbp": 0.0, "rows": []}
    unm = {"state": "ACTIVE_UNMEASURED", "permits_fund_funded": True, "label": "brake UNMEASURED"}
    fired = {"state": "FIRES", "permits_fund_funded": False, "label": "brake FIRED"}
    # MUST-FIRE: 24% sleeve and no cash -> a donor-funded capacity exists (D4)
    w = waterfall(cash_deployable_gbp=250.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(24000.0)),
                  drawdown=nodd, brake_state=unm, donor_headroom_gbp=50000.0)
    ok(abs(w["trim_capacity_gbp"] - 1000.0) < 0.01 and abs(w["stock_capital_gbp"] - 1000.0) < 0.01
       and w["max_fund_funded_gbp"] == 1000.0,
       "ISA-0858 MUST-FIRE (D4): 24%% sleeve + no cash -> GBP 1,000 fund-funded capacity to the 25%% band: %s" % w)
    ok(split_funding(1000.0, w)["fund_trims_gbp"] == 1000.0 and "SELL FUNDS FIRST" in split_funding(1000.0, w)["sequencing"]
       and split_funding(1000.0, w)["brake_label"] == "brake UNMEASURED",
       "ISA-0858 (D4/D5a/D8): a fund-funded spend is labelled 'brake UNMEASURED' and sequenced settle-first")
    # NEGATIVE CONTROL: 26% sleeve -> no fund trims; cash still usable
    w = waterfall(cash_deployable_gbp=5250.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(26000.0)),
                  drawdown=nodd, brake_state=unm, donor_headroom_gbp=50000.0)
    ok(w["trim_capacity_gbp"] == 0.0 and abs(w["stock_capital_gbp"] - 4000.0) < 0.01 and w["band"] == "COMPETE_25_30",
       "ISA-0858 NEGATIVE CONTROL (D4): 26%% sleeve -> fund trim refused; cash GBP 5,000 capped by the 30%% room GBP 4,000: %s" % w)
    # MUST-FIRE: 31% sleeve -> no new stock capital (D1 hard ceiling)
    w = waterfall(cash_deployable_gbp=20250.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(31000.0)),
                  drawdown=nodd, brake_state=unm, donor_headroom_gbp=50000.0)
    ok(w["stock_capital_gbp"] == 0.0 and w["replacement_only"] and w["binding"] == "HARD_CEILING",
       "ISA-0858 MUST-FIRE (D1): 31%% sleeve refuses any addition (replacement only)")
    # MUST-FIRE: brake FIRED -> fund-funded stops, cash-funded continues (D5b)
    w = waterfall(cash_deployable_gbp=3250.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(10000.0)),
                  drawdown=nodd, brake_state=fired, donor_headroom_gbp=50000.0)
    ok(w["trim_capacity_gbp"] == 0.0 and abs(w["stock_capital_gbp"] - 3000.0) < 0.01,
       "ISA-0858 MUST-FIRE (D5b): brake FIRED -> fund-funded 0, cash-funded GBP 3,000 continues: %s" % w)
    try:
        split_funding(3500.0, w)
        ok(False, "a spend needing trims under a FIRED brake must refuse")
    except WaterfallRefused:
        ok(True, "")
    # MUST-FIRE: B1.6b - the 11-Oct audit scenario (GBP 9,000 cash, tranche GBP 2,833.33 fired) claims once
    dd = drawdown_reservation({"episode_id": 2, "tranches_fired": {"t10": True, "t20": False, "t30": False},
                               "tranche_log": [{"episode_id": 2, "key": "t10", "fired_on": "2026-10-20",
                                                "tranche_gbp": 2833.33}]}, [])
    w = waterfall(cash_deployable_gbp=9000.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(10000.0)),
                  drawdown=dd, brake_state=fired, donor_headroom_gbp=0.0)
    ok(abs(w["cash_capital_gbp"] - 5916.67) < 0.01 and abs(w["stock_capital_gbp"] + 250.0 + 2833.33 - 9000.0) < 0.01,
       "ISA-0858 MUST-FIRE (B1.6b): stock 5,916.67 + tranche 2,833.33 + reserve 250 = 9,000 exactly, no double claim: %s" % w)
    dd2 = drawdown_reservation({"episode_id": 2, "tranches_fired": {"t10": True},
                                "tranche_log": [{"episode_id": 2, "key": "t10", "fired_on": "2026-10-20",
                                                 "tranche_gbp": 2833.33}]},
                               [{"date": "2026-10-21", "type": "buy", "ticker": "VUAG", "amount_gbp": 2830.0}])
    ok(dd2["reserved_gbp"] == 0.0 and dd2["rows"][0]["executed"],
       "ISA-0858 (B1.6b): an executed tranche (VUAG buy on/after the fire date) releases its reservation")
    ok(drawdown_reservation({"episode_id": 2, "tranches_fired": {"t10": True}}, [])["state"] == "UNMEASURED",
       "ISA-0858 NEGATIVE CONTROL (R4.3): a fired tranche with no recorded size is UNMEASURED, never zero")
    try:
        waterfall(cash_deployable_gbp=9000.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(1.0)),
                  drawdown={"reserved_gbp": None, "why": "x"}, brake_state=unm, donor_headroom_gbp=0.0)
        ok(False, "an unmeasured drawdown reservation must refuse")
    except WaterfallRefused:
        ok(True, "")
    # idempotence: same inputs, same output
    a = waterfall(cash_deployable_gbp=9000.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(10000.0)),
                  drawdown=dd, brake_state=unm, donor_headroom_gbp=7000.0)
    b = waterfall(cash_deployable_gbp=9000.0, policy_bands=bands, sleeve=sleeve_at_decision(pf(10000.0)),
                  drawdown=dd, brake_state=unm, donor_headroom_gbp=7000.0)
    ok(a == b, "ISA-0858: the waterfall is idempotent (a re-run reserves the same pounds once)")
    # pending fills count toward the sleeve; the MMF never does
    p = pf(24000.0)
    sl = sleeve_at_decision(p, [{"date": "2026-10-05", "type": "buy", "ticker": "MTDR", "asset_class": "stock",
                                 "amount_gbp": 2000.0},
                                {"date": "2026-10-05", "type": "sell", "ticker": "CSH2", "asset_class": "stock",
                                 "amount_gbp": 2000.0},
                                {"date": "2026-10-01", "type": "buy", "ticker": "OLD", "asset_class": "stock",
                                 "amount_gbp": 9.0}])
    ok(sl["pending_fills_gbp"] == 2000.0 and sl["sleeve_gbp"] == 26000.0,
       "ISA-0858 MUST-FIRE (D1 'incl. pending fills'): a buy after the book date counts; the MMF and pre-book rows do not: %s" % sl)
    w = waterfall(cash_deployable_gbp=250.0, policy_bands=bands, sleeve=sl, drawdown=nodd, brake_state=unm,
                  donor_headroom_gbp=50000.0)
    ok(w["trim_capacity_gbp"] == 0.0, "ISA-0858: the pending MTDR fill lifts the sleeve over 25%% - trims refused")
    ok(park_reason("STOCK_SLEEVE_REFUSED")["destination"] == "MMF"
       and park_reason("STOCK_SLEEVE_ENGINE_ALLOCATED")["destination"] == "FUNDS_VIA_WAITING_ROOM",
       "ISA-0858 (D3): operational absence -> MMF; economic -> funds via the waiting room")
    try:
        ceilings({"stock_sleeve": {"cash_reserve_gbp": 250, "sleeve_ceiling": 0.30}})
        ok(False, "undeclared bands must refuse")
    except WaterfallRefused:
        ok(True, "")
    hb = band_low_headroom({"summary": {"total_value_gbp": 100000.0},
                            "funds": [{"ticker": "A", "value_gbp": 9000.0}, {"ticker": "B", "value_gbp": 3000.0},
                                      {"ticker": "C", "value_gbp": 5000.0}]},
                           {"funds": {"A": {"band_low": 0.05}, "B": {"band_low": 0.05}}})
    ok(hb["headroom_gbp"] == 4000.0 and hb["by_fund"] == {"A": 4000.0, "B": 0.0} and hb["undeclared_band_low"] == ["C"],
       "ISA-0858 (D7): donor headroom stops at each DECLARED band_low; an undeclared band is named, not sized: %s" % hb)
    if verbose:
        print("funding_waterfall selftest: %d assertions, 0 failed" % n[0])
    return n[0]


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        import capital_destination as _cd
        print(json.dumps(build(_cd._load_portfolio()), indent=1, default=str))
