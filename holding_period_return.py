#!/usr/bin/env python3
"""
holding_period_return.py — MONEY-WEIGHTED (holding-period) return. Tier-1 item 1, 06-Aug-2026.

⚑ WHY THIS EXISTS, IN ONE SENTENCE
Every trailing window the framework uses is a CHOICE OF START DATE. Raj's holding period is not.

The Scottish Mortgage episode is the whole argument. Its calendar years are 2020 +110.5, 2021
+10.5, 2022 −45.7, 2023 +12.5, 2024 +18.8, 2025 +24.7, so the *same fund* reads 0.2% p.a. over
5 years, 22.2% over 3 and 16.7% over 10 — decided entirely by whether the 2022 collapse sits
inside the window. On today's arithmetic the 5-year figure will leap ~15pp in 2027 when 2022
rolls out, with no new information whatsoever. `fund_action_stack` currently answers this by
scoring the MEDIAN across windows and banding a disagreement `WINDOW_SPLIT`, which is a fair
treatment of an unanswerable question — but it is still an average of arbitrary choices.

There is exactly one window that nobody chose: **when Raj's money actually went in.** That is
what "every pound of capital deployed must earn its place" is asking about. A fund that returned
30% p.a. in the two years before he bought it and 2% since has not earned its place, and no
trailing window says so.

WHAT THIS COMPUTES
    per holding: XIRR over the dated external cashflows + today's market value
    invested / returned / net, the span, and the flows themselves for audit

⚑ WHAT IT REFUSES TO COMPUTE, AND WHY THAT IS THE POINT
A money-weighted return over an INCOMPLETE flow record is not a slightly-wrong number, it is a
confident wrong number — miss one purchase and the IRR silently attributes that capital's gain to
the rest. So the flow record must PROVE itself first: the units implied by the ledger are summed
independently and must equal the units the broker says are held (I-MWR-1). Two derivations of the
same fact, from two different exports, asserted to agree — Standard rule 4. A break returns
Missing(reason); it never returns a number with a caveat attached, because caveats do not survive
being copied into a table.

That invariant earned itself immediately: it caught **Ranmore**, where a Fund Class Conversion on
24-Feb-2026 moved £8,501.50 from the Investor class (SEDOL BR2Q8G6-INV) into the Institutional
class (BR2Q8G6). Read naively the Institutional holding looks 12 months old and 6.5 units in size;
it is really 62.4 units and Raj's money has been in Ranmore Global Equity continuously throughout.
A share-class conversion is not an exit and a re-entry, and treating it as one would restart the
clock on the very measurement whose entire value is that its clock is not a choice.

⚑ Lineage is DECLARED in `position_lineage.json`, never inferred. Amount-and-date matching would
have joined this pair correctly, and that is exactly the trap: it works until the month two
conversions settle on the same day, and a wrong lineage is invisible because the resulting number
is plausible. Candidate pairs are DETECTED and reported so the declaration is easy to write; they
are never acted on. (Same discipline as `preferred_listing.json` N4 and the `xray_name` join.)

BOUNDARY, STATED RATHER THAN BURIED
The dealing record begins 01-May-2025 with Transfer In rows. For those holdings the measured
period starts when the assets ARRIVED AT AJ BELL, at their transfer-in value. Whatever they did
under the previous platform is not retained and cannot be recovered, so `history_boundary` is
stamped `transfer_in` and the span is what it is. An unknown prior period is reported as unknown.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from isa_metric import Metric, Missing, is_present, as_dict           # noqa: E402

SCHEMA_VERSION = 1
LEDGER_PATH = os.path.join(HERE, "transaction_ledger.json")
LINEAGE_PATH = os.path.join(HERE, "position_lineage.json")

# ── calibration constants. One home each; every one stated and reversible. ──────────────
# ⚑ TWO THRESHOLDS, AND THE ASYMMETRY BETWEEN THEM IS THE WHOLE DESIGN (Raj, 06-Aug-2026).
#
# The naive version had one threshold and it was wrong in a way worth recording. At a 1-year
# span on live data the money-weighted return gives **11 of 12 funds FULL return-adequacy marks**
# — including JPM UK at +25.88%, the fund the significance test had just identified as the worst
# in the sleeve at t −3.53. Raj bought it into a rising UK market, so his capital grew 25.88%
# while the fund cost him 10.57pp a year against the one beside it.
#
# **MWR asks whether the capital GREW. It does not ask whether that pound should be here rather
# than somewhere else.** Growth and earning a place are different tests, and only the second is
# the mantra. Used symmetrically over a short favourable window, MWR rewards having been invested
# during a rally and discriminates between nothing.
#
# But the evidence is genuinely ASYMMETRIC, and that is the way out. Over a short good window a
# HIGH MWR is uninformative — the market did it. A LOW one is strong evidence: capital that
# failed to grow in a rising market is hard to explain away. So:
#
#   span >= 1.0y : DOWNWARD ONLY — MWR may pull return adequacy BELOW the trailing figure and
#                  may never lift it. An insurance policy that arms the moment a fund's money
#                  stops growing, and is silent otherwise.
#   span >= 3.0y : SYMMETRIC — long enough to contain a drawdown, so a high MWR now means
#                  something. This is the anchor the register argued for.
MWR_MIN_SPAN_DOWNWARD = 1.0
MWR_MIN_SPAN_SYMMETRIC = 3.0
MWR_MIN_ANCHOR_SPAN_YEARS = MWR_MIN_SPAN_DOWNWARD   # retained: the span at which MWR first binds
QTY_TOL_FRAC = 1e-4        # ledger-vs-broker unit reconciliation tolerance (relative)
QTY_TOL_ABS = 1e-3
NPV_TOL = 1e-6             # I-MWR-2
IRR_LO, IRR_HI = -0.999999, 100.0

# Ledger `type` -> how it moves capital IN or OUT of the holding.
#   capital_in   : investor capital entering the position (negative CF)
#   capital_out  : capital leaving (positive CF)
#   internal     : no external flow — must contribute EXACTLY zero
#   lineage      : share-class conversion; no flow, but links two SEDOLs
FLOW_SEMANTICS = {
    "buy": "capital_in",
    "transfer_in": "capital_in",
    "sell": "capital_out",
    "distribution": "income",       # zero if accumulated, positive if paid out as cash
    "equalisation": "income",
    "conversion": "lineage",
}


# ────────────────────────────────────────────────────────────── loading
def load_ledger(path=LEDGER_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_lineage(path=LINEAGE_PATH):
    if not os.path.exists(path):
        return {"links": [], "_meta": {"note": "no declarations yet"}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def lineage_aliases(sedol, lineage):
    """Every SEDOL that is the SAME economic position as `sedol`, per DECLARED links only."""
    out = {sedol}
    changed = True
    while changed:
        changed = False
        for lk in lineage.get("links", []):
            a, b = lk.get("from_sedol"), lk.get("to_sedol")
            if not (a and b):
                continue
            if a in out and b not in out:
                out.add(b); changed = True
            if b in out and a not in out:
                out.add(a); changed = True
    return out


def detect_conversion_candidates(entries):
    """REPORT unlinked conversion pairs so a declaration can be written. Never acts on them.

    A conversion pair is two rows on the same date, type `conversion`, with the same value and
    opposite roles. Matching on that is reliable *today* and would silently mis-join the month two
    conversions settle on one day for the same amount. Detection is cheap; acting on detection is
    the defect."""
    by_key = {}
    for r in entries:
        if r.get("type") != "conversion":
            continue
        by_key.setdefault((r.get("date"), round(float(r.get("amount_gbp") or 0), 2)), []).append(r)
    out = []
    for (d, amt), rows in sorted(by_key.items()):
        if len(rows) == 2:
            out.append({"date": d, "amount_gbp": amt,
                        "sedols": sorted(r.get("ticker") for r in rows),
                        "descriptions": [r.get("description") for r in rows],
                        "status": "candidate — DECLARE in position_lineage.json to chain"})
        else:
            out.append({"date": d, "amount_gbp": amt,
                        "sedols": sorted(r.get("ticker") for r in rows),
                        "status": f"AMBIGUOUS — {len(rows)} rows share this date+amount; "
                                  f"a declaration is mandatory, detection cannot resolve it"})
    return out


# ────────────────────────────────────────────────────────────── flows & episodes
FLAT_UNITS_TOL = 1e-6


def nav_price_at(sedol, on_date, universe=None):
    """Market price per unit on/just before `on_date`, from the golden source's own NAV cache.

    ⚑ WHY THIS IS NEEDED AT ALL — the Transfer In amount is BOOK COST, not market value.
    Proven, not assumed. Scottish Mortgage transferred in as 417 units for £4,827.78, an implied
    £11.5774/unit, against a market close of £9.2194 on the same day: the amount is 25.6% ABOVE
    market. Vanguard S&P 500 transferred as 77 units for £5,211.01 = £67.68/unit against a market
    £81.14 — 16.6% BELOW. One transferred at a loss and one at a gain, which is exactly what book
    cost looks like and exactly what a market value cannot look like.

    It is confirmed independently by the broker's own `cost_gbp`, which reconciles to the ledger
    to the PENNY once accumulation distributions are added and equalisations subtracted (I-MWR-4).

    Using book cost as the capital-in at the transfer DATE imports the previous platform's
    unrealised P&L into the AJ Bell window and then annualises it over the wrong number of years —
    wrong amount at the wrong date. So the transferred units are re-valued at the market price on
    the transfer date, and where no price exists the position is reported on the cost basis and
    made INELIGIBLE as an anchor rather than quietly measured on the wrong one."""
    try:
        import fund_performance as fp
    except Exception:
        return None, "fund_performance unavailable"
    u = (universe or {}).get(sedol) or {}
    sym = u.get("yf_symbol")
    if not sym:
        return None, f"no yf_symbol for {sedol} in fund_universe"
    series = fp.fetch_nav_history(sym, use_cache=True, scale=fp._scale_for(u))
    if not series:
        return None, f"no NAV history cached for {sym}"
    hit = fp._nav_on_or_before(series, on_date, max_back_days=7)
    if not hit:
        return None, f"no NAV within 7d on/before {on_date} for {sym}"
    return hit[1], f"{sym} close {hit[1]:.4f} @{hit[0]}"


def build_flows(entries, sedols, lineage=None, price_at=None, universe=None):
    """Walk the dealing record for ONE economic position (all declared classes) and return
    (rows, units_by_sedol). Units are tracked PER SHARE CLASS: 17.0365 Investor units became
    55.8321 Institutional units in the Ranmore conversion, so a single running unit total across
    classes is not a quantity of anything."""
    rows, units, notes = [], {}, []
    ordered = sorted((r for r in entries if r.get("ticker") in sedols),
                     key=lambda x: (x.get("date") or "", x.get("reference") or ""))
    for r in ordered:
        typ, sd = r.get("type"), r.get("ticker")
        sem = FLOW_SEMANTICS.get(typ)
        d = r.get("date")
        amt = float(r.get("amount_gbp") or 0.0)
        cash = float(r.get("cash_impact_gbp") or 0.0)
        qty = float(r.get("quantity") or 0.0)
        if sem is None:
            notes.append(f"UNKNOWN ledger type {typ!r} on {d} ({sd}) — reported, never counted")
            rows.append({"date": d, "type": typ, "sedol": sd, "cf_gbp": None, "units": 0.0,
                         "why": "unknown ledger type — REPORTED, never silently dropped"})
            continue
        cf, du, why, tbasis = 0.0, 0.0, "", None
        if sem == "capital_in":
            # Transfer In carries NO cash impact (assets moved, cash did not) yet is unambiguously
            # capital entering the position. Reading `cash_impact_gbp` alone would value the whole
            # transferred book at zero and hand its entire subsequent gain to a £0 cost base.
            cf, du = -amt, qty
            why = "purchase"
            if typ == "transfer_in":
                mv, detail_px = (price_at or nav_price_at)(sd, dt.date.fromisoformat(d), universe)
                if mv:
                    cf, tbasis = -(qty * mv), "market_value_at_transfer_date"
                    why = (f"transfer in, RE-VALUED at market: {qty:g} units x {mv:.4f} = "
                           f"£{qty * mv:,.2f} ({detail_px}). The ledger amount £{amt:,.2f} is the "
                           f"BOOK COST carried from the ceding scheme, not a market value — "
                           f"using it would import that platform's P&L and date it to the "
                           f"transfer day")
                else:
                    tbasis = "book_cost"
                    why = (f"transfer in at BOOK COST £{amt:,.2f} — market value at the transfer "
                           f"date unavailable ({detail_px}). Measured, but NOT anchor-eligible: "
                           f"a cost-basis IRR mis-dates the prior platform's P&L")
                    notes.append(f"transfer_in {sd} on {d}: no market price — cost basis used, "
                                 f"anchor use blocked ({detail_px})")
        elif sem == "capital_out":
            cf, du = +amt, -qty
            why = "sale"
        elif sem == "income":
            # ⚑ THE NEGATIVE TEST LIVES HERE. `Accumulation Distribution` and `Equalisation Acc
            # Units` rows carry a real-looking `amount_gbp` (£66.53, £39.14, £151.49) and a cash
            # impact of ZERO: the income is reinvested inside the fund and is already inside the
            # NAV. Counting `amount_gbp` books the same money twice — once as a cash return and
            # again as the unit-price rise it paid for. Only cash that reached the account counts.
            cf, du = (cash if abs(cash) > 0.005 else 0.0), 0.0
            why = ("income paid out to cash" if cf else
                   "ACCUMULATED inside the fund (cash impact 0) — already in the NAV; counting "
                   "amount_gbp here would double-count it")
        elif sem == "lineage":
            # ⚑ DIRECTION IS READ FROM THE DECLARATION, NEVER INFERRED.
            # The first cut guessed it from "this class had prior buys and nothing after", which
            # got Ranmore exactly backwards: BR2Q8G6 had two earlier purchases AND was the class
            # the conversion moved INTO, so the heuristic booked +55.83 units as −55.83 and the
            # unit reconciliation failed by 111.66 units. A guess that is wrong in the first case
            # it meets is not a fallback, it is a defect with a default.
            direction = _conversion_direction(sd, lineage)
            if direction is None:
                du = 0.0
                why = ("share-class conversion with NO DECLARED direction — units not applied. "
                       "I-MWR-5 blocks this position rather than guessing which leg this is")
            else:
                du = qty if direction == "in" else -qty
                why = (f"share-class conversion, {direction.upper()} leg per position_lineage.json "
                       f"— units move between classes, capital neither enters nor leaves")
        units[sd] = units.get(sd, 0.0) + du
        rows.append({"date": d, "type": typ, "raw_type": r.get("raw_type"), "sedol": sd,
                     "cf_gbp": round(cf, 2), "units": round(du, 6), "amount_gbp": amt,
                     "cash_impact_gbp": cash, "transfer_basis": tbasis, "why": why})
    return rows, units, notes


def _conversion_direction(sedol, lineage):
    """`in` | `out` | None, from the DECLARED link only."""
    for lk in (lineage or {}).get("links", []):
        if lk.get("to_sedol") == sedol:
            return "in"
        if lk.get("from_sedol") == sedol:
            return "out"
    return None


def split_episodes(rows):
    """⚑ AN IRR ACROSS A PERIOD WHEN NOTHING WAS HELD IS NOT A HOLDING-PERIOD RETURN.

    The first cut of this module chained every Scottish Mortgage flow into one XIRR and produced
    **−18.17% p.a.**, which reads as "Raj's money lost value in SMT" and is FALSE. The record is:

        2025-05-01  transfer in  417 units @ £4,827.78
        2025-05-12  SELL         417 units  → £4,116.66      ← position FLAT for eleven months
        2026-04-07  buy          498 units @ £6,488.59

    Two unconnected episodes with an eleven-month gap out of the market. A single IRR discounts
    the 2026 purchase across a window in which no capital was at risk, so the arithmetic silently
    charges SMT for a period Raj did not own it. The honest reading is +2.1% cumulative on the
    CURRENT position, plus a separate, real fact about the 2025 round trip — which is a decision-
    quality observation (MOA's territory), not a fund-quality one.

    A position is FLAT when every chained share class holds ~zero units simultaneously. Each
    episode is measured on its own; nothing is ever annualised across a gap.
    """
    # ⚑ FLATNESS IS EVALUATED AT DATE BOUNDARIES, NOT AFTER EVERY ROW.
    # A share-class conversion books its OUT leg and its IN leg on the same day. Testing row by
    # row sees units hit zero between the two and declares the position CLOSED, restarting the
    # clock on the exact event this module exists to see through. Live Ranmore only escaped it
    # because two earlier purchases happened to keep the old class non-zero at that instant — the
    # kind of near miss that ships. Intra-day sequencing is not an exit.
    eps, cur, run = [], [], {}
    for i, r in enumerate(rows):
        cur.append(r)
        if r.get("units"):
            run[r["sedol"]] = run.get(r["sedol"], 0.0) + r["units"]
        same_day_next = (i + 1 < len(rows) and rows[i + 1].get("date") == r.get("date"))
        if same_day_next:
            continue
        if run and all(abs(v) <= FLAT_UNITS_TOL for v in run.values()):
            eps.append({"rows": cur, "closed": True})
            cur, run = [], {}
    if cur:
        eps.append({"rows": cur, "closed": False, "units_at_end": dict(run)})
    return eps


# ────────────────────────────────────────────────────────────── XIRR
def _npv(rate, flows, t0):
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.25) for d, cf in flows)


def xirr(flows, source, as_of):
    """Annualised money-weighted return. Bisection inside a proven bracket, Newton nowhere: a
    bare Newton on irregular flows diverges and then returns a plausible number from a failed
    solve, which is precisely the defect class this project keeps paying for."""
    if len(flows) < 2:
        return Missing("fewer than two cashflows", as_of, source)
    if not any(cf < 0 for _, cf in flows):
        return Missing("no capital was ever paid in — IRR undefined", as_of, source)
    if not any(cf > 0 for _, cf in flows):
        return Missing("nothing came back and there is no terminal value — IRR undefined",
                       as_of, source)
    t0 = min(d for d, _ in flows)
    span = (max(d for d, _ in flows) - t0).days / 365.25
    if span <= 0:
        return Missing("all cashflows fall on one date — no elapsed time to annualise over",
                       as_of, source)
    lo, hi = IRR_LO, IRR_HI
    f_lo, f_hi = _npv(lo, flows, t0), _npv(hi, flows, t0)
    if f_lo * f_hi > 0:
        return Missing(f"no sign change in NPV over [{lo:.0%}, {hi:.0%}] — this flow pattern "
                       f"admits no single IRR", as_of, source)
    for _ in range(400):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, flows, t0)
        if abs(f_mid) < NPV_TOL or (hi - lo) < 1e-13:
            break
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    rate = (lo + hi) / 2.0
    resid = _npv(rate, flows, t0)
    if abs(resid) > max(NPV_TOL, 1e-9 * sum(abs(cf) for _, cf in flows)):
        return Missing(f"IRR solve did not converge (residual NPV {resid:.6g})", as_of, source)
    return Metric(rate * 100.0, as_of, source, unit="%",
                  note=f"XIRR over {len(flows)} flows, {t0}->{max(d for d, _ in flows)} "
                       f"({span:.2f}y); residual NPV {resid:.2e}")


# ────────────────────────────────────────────────────────────── one episode
def measure_episode(ep, source, terminal_value=None, terminal_date=None):
    flows = [(dt.date.fromisoformat(r["date"]), r["cf_gbp"])
             for r in ep["rows"] if r.get("cf_gbp")]
    detail = list(ep["rows"])
    if terminal_value is not None and terminal_date is not None:
        flows.append((terminal_date, float(terminal_value)))
        detail = detail + [{"date": terminal_date.isoformat(), "type": "terminal_valuation",
                            "cf_gbp": round(float(terminal_value), 2), "units": 0.0,
                            "why": "position marked at the broker valuation — the notional exit "
                                   "that closes the IRR"}]
    if not flows:
        return {"open": not ep.get("closed"), "flows": detail,
                "mwr_annualised": as_dict(Missing("no cashflows in this episode", None, source))}
    first, last = min(d for d, _ in flows), max(d for d, _ in flows)
    span = (last - first).days / 365.25
    cap_in = sum(-cf for _, cf in flows if cf < 0)
    cap_out = sum(cf for _, cf in flows if cf > 0)
    as_of = last.isoformat()

    # ⚑ SUB-YEAR IRRs ARE NOT PUBLISHED AS ANNUAL RATES.
    # Micron read **+954.56% p.a.** on the first run: £2,024 into £4,278 over 0.32 years, which is
    # arithmetically exact and informationally worthless. Once a figure like that is in a table
    # somebody quotes it. Below one year only the CUMULATIVE money-weighted return is published,
    # and the annualised slot carries a Missing that says why — the two facts must not converge.
    if span >= 1.0:
        ann = xirr(flows, source, as_of)
    else:
        ann = Missing(f"span {span:.2f}y — annualising a sub-year IRR turns a {(cap_out - cap_in) / cap_in * 100:+.1f}% "
                      f"move into a headline annual rate; refused as misleading, cumulative "
                      f"published instead" if cap_in else f"span {span:.2f}y — sub-year",
                      as_of, source)
    cum = (Metric((cap_out - cap_in) / cap_in * 100.0, as_of, source, unit="%",
                  note=f"money-weighted cumulative over {span:.2f}y on £{cap_in:,.0f} deployed")
           if cap_in > 0 else Missing("no capital deployed in this episode", as_of, source))
    return {
        "open": not ep.get("closed"),
        "first_flow_date": first.isoformat(), "last_flow_date": last.isoformat(),
        "span_years": round(span, 3),
        "capital_in_gbp": round(cap_in, 2),
        "capital_returned_gbp": round(cap_out - (terminal_value or 0.0), 2),
        "terminal_value_gbp": terminal_value,
        "net_gain_gbp": round(cap_out - cap_in, 2),
        "mwr_annualised": as_dict(ann),
        "mwr_cumulative": as_dict(cum),
        "flows": detail,
    }


# ────────────────────────────────────────────────────────────── the main API
def holding_period_return(sedol, entries, broker_qty, terminal_value, terminal_date,
                          lineage=None, name=None, broker_cost=None, price_at=None,
                          universe=None):
    """Money-weighted return for ONE economic position, split into episodes."""
    lineage = lineage if lineage is not None else load_lineage()
    sedols = lineage_aliases(sedol, lineage)
    src = "transaction_ledger + broker valuation"
    out = {"sedol": sedol, "name": name, "sedols_chained": sorted(sedols),
           "terminal_value_gbp": terminal_value,
           "terminal_date": terminal_date.isoformat() if terminal_date else None,
           "invariants": {}, "notes": []}

    rows, units, notes = build_flows(entries, sedols, lineage, price_at, universe)
    out["notes"].extend(notes)
    out["units_by_class"] = {k: round(v, 6) for k, v in units.items()}

    # ── I-MWR-1 — the dealing record must prove itself against an independent derivation ────
    # Per share class, because units of two classes are not the same thing. The class the broker
    # holds must match its valuation; every predecessor class must have gone to zero.
    tol = max(QTY_TOL_ABS, abs(broker_qty or 0.0) * QTY_TOL_FRAC)
    held_ok = broker_qty is not None and abs(units.get(sedol, 0.0) - broker_qty) <= tol
    stale = {k: round(v, 6) for k, v in units.items()
             if k != sedol and abs(v) > max(QTY_TOL_ABS, abs(v) * QTY_TOL_FRAC)}
    out["invariants"]["I_MWR_1_units_reconcile"] = {
        "pass": bool(held_ok and not stale),
        "ledger_units": round(units.get(sedol, 0.0), 6), "broker_units": broker_qty,
        "delta": (round(units.get(sedol, 0.0) - broker_qty, 6)
                  if broker_qty is not None else None),
        "predecessor_classes_not_zero": stale,
        "note": "units implied by the dealing record must equal the units the valuation reports, "
                "per share class. A break means a flow is missing, and an IRR over a missing "
                "flow is a confident wrong number, not an approximate right one."}

    # ── I-MWR-4 — the MONEY dimension, proven against a second export ──────────────────────
    # I-MWR-1 proves the UNIT dimension. This proves the money, and it is an EXACT identity, not
    # a tolerance band: the broker's book cost equals purchases and transfers at cost, plus
    # accumulation distributions, minus equalisations. (An accumulation distribution raises the
    # base cost — it is taxable income notionally reinvested — and the equalisation part of it is
    # a return of capital that lowers it again.) On live August data all sixteen holdings
    # reconcile to £0.00 once the two adjustments are applied; before they were applied, VUAG was
    # out by exactly its £70.25 distribution and Artemis UK by exactly its £305.54 − £158.55.
    # An identity that lands on zero across sixteen independent positions is worth far more than
    # a tolerance that would have hidden both.
    cost_in = sum(-r["cf_gbp"] for r in rows
                  if r.get("cf_gbp") and r["cf_gbp"] < 0 and r["type"] in ("buy", "transfer_in"))
    # re-valuation changes the transfer figure, so the identity is checked on the LEDGER amount
    ledger_cost_in = sum(float(r.get("amount_gbp") or 0.0) for r in rows
                         if r["type"] in ("buy", "transfer_in"))
    acc_dist = sum(float(r.get("amount_gbp") or 0.0) for r in rows
                   if r["type"] == "distribution" and not r.get("cash_impact_gbp"))
    equal = sum(float(r.get("amount_gbp") or 0.0) for r in rows
                if r["type"] == "equalisation" and not r.get("cash_impact_gbp"))
    sold_cost = None
    implied = round(ledger_cost_in + acc_dist - equal, 2)
    cost_ok = (broker_cost is None) or (abs(implied - float(broker_cost)) <= 0.01)
    out["invariants"]["I_MWR_4_cost_reconciles"] = {
        "pass": bool(cost_ok),
        "ledger_implied_cost_gbp": implied, "broker_cost_gbp": broker_cost,
        "delta": (round(implied - float(broker_cost), 2) if broker_cost is not None else None),
        "components": {"purchases_and_transfers_at_cost": round(ledger_cost_in, 2),
                       "accumulation_distributions": round(acc_dist, 2),
                       "equalisations": round(equal, 2)},
        "note": "purchases + accumulation distributions - equalisations must EQUAL the broker's "
                "book cost, to the penny. Checked on the ledger amounts, so it is independent of "
                "the market re-valuation applied to transfers."}
    # ⚑ The identity is EXACT only where nothing re-bases the book cost. A SALE reduces cost by
    # the disposed proportion; a share-class CONVERSION re-bases it to the conversion value
    # (Ranmore: broker cost is exactly 690 + 460 + 8,501.50, with the Investor class's £151.49
    # accumulation uplift dropped at the conversion). Both are structural, both are named, and
    # neither is allowed to weaken the check where it SHOULD be exact — on the thirteen holdings
    # with no sale and no conversion it must land on £0.00 or it blocks.
    rebasers = sorted({r["type"] for r in rows if r["type"] in ("sell", "conversion")})
    if not cost_ok and rebasers:
        out["invariants"]["I_MWR_4_cost_reconciles"].update({
            "pass": True, "blocking": False, "exact_identity_applies": False,
            "rebasing_events": rebasers,
            "note": out["invariants"]["I_MWR_4_cost_reconciles"]["note"] +
                    f" NOT BLOCKING HERE: this position has {'/'.join(rebasers)} activity, which "
                    f"re-bases the broker's book cost. The identity is exact only for positions "
                    f"never sold and never converted; the residual is reported, not suppressed."})
    else:
        out["invariants"]["I_MWR_4_cost_reconciles"]["exact_identity_applies"] = not rebasers

    # ── I-MWR-5 — an undeclared conversion touching this position is a lineage break ────────
    unlinked = [r for r in entries
                if r.get("type") == "conversion" and r.get("ticker") in sedols
                and not any(r.get("ticker") in (lk.get("from_sedol"), lk.get("to_sedol"))
                            for lk in lineage.get("links", []))]
    out["invariants"]["I_MWR_5_lineage_declared"] = {
        "pass": not unlinked,
        "unlinked_conversions": [{"date": r["date"], "sedol": r["ticker"],
                                  "amount_gbp": r["amount_gbp"]} for r in unlinked],
        "note": "a share-class conversion restarts the measured clock unless the two classes are "
                "DECLARED one position — and the clock not being a choice is the entire value of "
                "this measurement"}

    if any(not v["pass"] for v in out["invariants"].values()):
        blockers = [k for k, v in out["invariants"].items() if not v["pass"]]
        out["episodes"], out["current"], out["n_episodes"] = [], None, 0
        _m = Missing("flow record failed " + ", ".join(blockers),
                     terminal_date.isoformat() if terminal_date else None, src)
        out["mwr"] = out["mwr_cumulative"] = as_dict(_m)
        out["span_years"] = out["capital_in_gbp"] = out["net_gain_gbp"] = None
        out["usable_as_anchor"] = False
        out["anchor_block_reason"] = out["mwr"]["reason"]
        out["flows"] = rows
        return out

    eps = split_episodes(rows)
    measured = []
    for i, ep in enumerate(eps):
        last = (i == len(eps) - 1) and not ep.get("closed")
        measured.append(measure_episode(ep, src,
                                        terminal_value if last else None,
                                        terminal_date if last else None))
    out["episodes"] = measured
    out["n_episodes"] = len(measured)
    cur = next((m for m in measured if m.get("open")), None)
    out["current"] = cur

    # ── I-MWR-6 — no figure may span a period when nothing was held ─────────────────────────
    gaps = []
    for a, b in zip(measured, measured[1:]):
        if a.get("last_flow_date") and b.get("first_flow_date"):
            g = (dt.date.fromisoformat(b["first_flow_date"])
                 - dt.date.fromisoformat(a["last_flow_date"])).days
            if g > 0:
                gaps.append({"from": a["last_flow_date"], "to": b["first_flow_date"],
                             "days_flat": g})
    out["invariants"]["I_MWR_6_no_figure_spans_a_flat_gap"] = {
        "pass": True, "gaps": gaps, "episodes": len(measured),
        "note": "each episode is measured on its own. Chaining across a gap charges the holding "
                "for a period in which no capital was at risk — it produced a FALSE −18.17% p.a. "
                "on Scottish Mortgage before this split existed."}
    if gaps:
        out["notes"].append(
            f"{len(gaps)} period(s) out of this position entirely ("
            + "; ".join(f"{g['days_flat']}d from {g['from']}" for g in gaps)
            + "). The round trip is a real fact about the DECISION and belongs to the missed-"
              "opportunity ledger; it is not evidence about the holding's current merit.")

    if cur is None:
        out["mwr"] = as_dict(Missing("position is not currently open", None, src))
        out["usable_as_anchor"] = False
        out["flows"] = rows
        return out

    ann = cur["mwr_annualised"]
    out["mwr"] = ann
    out["mwr_cumulative"] = cur["mwr_cumulative"]
    out["span_years"] = cur["span_years"]
    out["capital_in_gbp"] = cur["capital_in_gbp"]
    out["net_gain_gbp"] = cur["net_gain_gbp"]
    tr = [r for r in cur["flows"] if r.get("type") == "transfer_in"]
    out["history_boundary"] = "transfer_in" if tr else "first_purchase"
    cost_basis_transfer = any(r.get("transfer_basis") == "book_cost" for r in tr)
    out["transfer_valuation_basis"] = (
        "n/a (no transfer in this episode)" if not tr else
        "book_cost (market value at transfer unavailable)" if cost_basis_transfer else
        "market_value_at_transfer_date")
    if tr:
        out["notes"].append(
            "measured from the TRANSFER-IN date. Performance under the previous platform is not "
            "retained by AJ Bell and cannot be recovered, so the span understates how long Raj "
            "has held this exposure. Stated, not silently ignored.")

    if ann.get("present"):
        t0 = min(dt.date.fromisoformat(f["date"]) for f in cur["flows"] if f.get("cf_gbp"))
        fl = [(dt.date.fromisoformat(f["date"]), f["cf_gbp"])
              for f in cur["flows"] if f.get("cf_gbp")]
        resid = _npv(ann["value"] / 100.0, fl, t0)
        out["invariants"]["I_MWR_2_npv_zero"] = {
            "pass": abs(resid) <= max(NPV_TOL, 1e-9 * sum(abs(c) for _, c in fl)),
            "residual": resid,
            "note": "the returned rate is re-substituted into the NPV independently of the "
                    "solver, so a non-converged solve cannot be published as a rate"}

    sp = cur["span_years"]
    mode = ("symmetric" if sp >= MWR_MIN_SPAN_SYMMETRIC else
            "downward_only" if sp >= MWR_MIN_SPAN_DOWNWARD else "none")
    if not ann.get("present"):
        mode = "none"
    if cost_basis_transfer:
        mode = "none"
    out["anchor_mode"] = mode
    long_enough = mode != "none"
    out["invariants"]["I_MWR_7_transfer_valued_at_market"] = {
        "pass": not cost_basis_transfer,
        "basis": out["transfer_valuation_basis"],
        "note": "a transfer priced at BOOK COST dates the ceding platform's P&L to the transfer "
                "day and then annualises it over the wrong period. Reported, never anchored."}
    out["invariants"]["I_MWR_3_span_sufficient_for_anchor"] = {
        "pass": long_enough, "span_years": sp, "anchor_mode": mode,
        "downward_only_from_years": MWR_MIN_SPAN_DOWNWARD,
        "symmetric_from_years": MWR_MIN_SPAN_SYMMETRIC,
        "note": ("REPORTING is never withheld on span; only the MODE changes. Downward-only "
                 "means the figure can lower return adequacy and never raise it — a high MWR "
                 "over a short rising window is the market's result, a low one is the fund's.")}
    out["usable_as_anchor"] = bool(mode != "none")
    if not out["usable_as_anchor"]:
        out["anchor_block_reason"] = (
            ann.get("reason") if not ann.get("present") else
            "transfer valued at book cost, not market — see I-MWR-7" if cost_basis_transfer else
            f"span {sp}y < {MWR_MIN_SPAN_DOWNWARD}y minimum — reported, not used as the anchor")
    out["flows"] = rows
    return out


def all_holding_period_returns(portfolio, ledger=None, lineage=None, as_of=None,
                               universe=None, price_at=None):
    ledger = ledger if ledger is not None else load_ledger()
    lineage = lineage if lineage is not None else load_lineage()
    if universe is None:
        try:
            import fund_performance as fp
            universe = fp.load_universe()
        except Exception:
            universe = {}
    entries = ledger.get("entries", [])
    meta = portfolio.get("_meta", {}) or {}
    td = as_of or _parse_broker_date(meta.get("data_date"))
    rows = {}
    for h in (portfolio.get("funds", []) or []) + (portfolio.get("stocks", []) or []):
        sd = h.get("ticker")
        rows[sd] = holding_period_return(
            sd, entries, h.get("quantity"), h.get("value_gbp"), td, lineage, h.get("name"),
            broker_cost=h.get("cost_gbp"), price_at=price_at, universe=universe)
        rows[sd]["kind"] = h.get("kind")
    reported = [r for r in rows.values()
                if (r.get("mwr_cumulative") or {}).get("present")]
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": td.isoformat() if td else None,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ledger_span": {"earliest": (ledger.get("_meta") or {}).get("earliest"),
                        "latest": (ledger.get("_meta") or {}).get("latest")},
        "min_anchor_span_years": MWR_MIN_SPAN_DOWNWARD,
        "anchor_modes": {"downward_only_from_years": MWR_MIN_SPAN_DOWNWARD,
                         "symmetric_from_years": MWR_MIN_SPAN_SYMMETRIC},
        "conversion_candidates": detect_conversion_candidates(entries),
        "lineage_declared": lineage.get("links", []),
        "holdings": rows,
        "summary": {
            "n_holdings": len(rows),
            "cumulative_computed": len(reported),
            "annualised_computed": sum(1 for r in rows.values()
                                       if (r.get("mwr") or {}).get("present")),
            "usable_as_anchor": sum(1 for r in rows.values() if r.get("usable_as_anchor")),
            "anchor_mode_counts": {m: sum(1 for r in rows.values()
                                          if r.get("anchor_mode") == m)
                                   for m in ("symmetric", "downward_only", "none")},
            "multi_episode": sorted(r["sedol"] for r in rows.values()
                                    if (r.get("n_episodes") or 0) > 1),
            "blocked": sorted(r["sedol"] for r in rows.values()
                              if not (r.get("mwr_cumulative") or {}).get("present")),
        },
    }


def _parse_broker_date(v):
    for f in ("%d-%b-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(str(v).strip(), f).date()
        except (ValueError, TypeError):
            continue
    return None


# ────────────────────────────────────────────────────────────── CLI
def _pc(m):
    return f"{m['value']:+.2f}" if (m or {}).get("present") else "n/a"
def build(portfolio_path=None, out_path=None, as_of=None):
    portfolio_path = portfolio_path or os.path.join(HERE, "portfolio_data_aug_2026.json")
    with open(portfolio_path, encoding="utf-8") as fh:
        pf = json.load(fh)
    res = all_holding_period_returns(pf, as_of=as_of)
    res["portfolio_source"] = os.path.basename(portfolio_path)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, default=str)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="money-weighted holding-period returns")
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = build(a.portfolio, a.out,
              dt.date.fromisoformat(a.as_of) if a.as_of else None)
    if a.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        print(f"Money-weighted holding-period returns as at {r['as_of']}")
        print(f"  anchor eligibility needs >= {r['min_anchor_span_years']}y of holding\n")
        hdr = f"{'sedol':13}{'name':30}{'ep':>3}{'span':>7}{'basis':>8}{'in GBP':>11}{'cum %':>9}{'ann %':>9}  anchor"
        print(hdr); print("-" * len(hdr))
        for sd, h in sorted(r["holdings"].items(),
                            key=lambda kv: -(kv[1].get("terminal_value_gbp") or 0)):
            c, an = h.get("mwr_cumulative") or {}, h.get("mwr") or {}
            b = {"market_value_at_transfer_date": "market",
                 "book_cost (market value at transfer unavailable)": "COST"}.get(
                     h.get("transfer_valuation_basis"), "-")
            print(f"{sd:13}{str(h.get('name'))[:28]:30}{h.get('n_episodes', '-'):>3}"
                  f"{str(h.get('span_years')):>7}{b:>8}{(h.get('capital_in_gbp') or 0):>11,.0f}"
                  f"{_pc(c):>9}{_pc(an):>9}"
                  f"  {h.get('usable_as_anchor')}")
        print(f"\n{json.dumps(r['summary'], indent=1)}")
