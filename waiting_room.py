#!/usr/bin/env python3
"""
waiting_room.py — ISA-0390.  THE RECALL LEG.  Built 20-Aug-2026.

⚑ RAJ'S INSTRUCTION, 19-Aug-2026, answering ISA-0361 Q3:
    "we definitely cannot let the capital just sit there or all just sit in MMF. having said that
     i realise that for stock trading patience is really important... if in the next run there are
     no stock opportunities, it should go into funds and then when the next opportunity arises,
     the framework should know to trim funds and move capital into stocks."

⚑ THE GAP. `capital_destination` routes capital ONE WAY. Nothing identified a fund DONOR when a
stock candidate later qualified, so "park it in funds" and "allocate it to funds permanently" were
the same act with different intentions — and intentions are not on disk. Without a recall leg,
patience in the stock sleeve is paid for by a permanent increase in the fund sleeve, silently:
the opposite of what Raj asked for. A waiting room with no exit is a destination.

⚑ WHY THE FUND SLEEVE AND NOT THE MMF. Every pound must count. The MMF pays a MEASURED 1.757%
(return_architecture.derive_cash_rate, one observation) against an operative required return of
13.8%. Parking in funds is right on those grounds — but ONLY if the capital stays RECALLABLE.

⚑ FIVE PARTS, exactly as ISA-0390's corrective action specifies.
  1  PARKED IS A TAG, NOT A MOOD. Capital placed in funds for want of a qualifying stock candidate
     is written to the ledger with its amount, its date and its destinations, so it is visible as a
     TIMING decision. Nothing else distinguishes it from an allocation.
  2  THE DONOR ORDERING IS READ, NOT DECIDED HERE. ⚑ SUPERSEDED 26-Aug-2026 by A7 (ISA-0440).
     It used to be "the buy ranking run in reverse", and that is wrong for an arithmetic reason
     rather than a stylistic one: `_rank_key` puts band deviation LAST on purpose, so reversing it
     puts "how far above its own declared band this money sits" LAST on the sell side. A7 puts it
     FIRST and demotes FRS to a vote, because L-1/ISA-0351 measured alpha rank persistence at
     -0.482 in this sleeve. The sell rule now has exactly one home —
     `capital_destination.donor_order` — and this function READS it (R4.4/R4.5).
  3  THE ROUND TRIP IS PRICED BEFORE IT IS TAKEN, from the DEALING RECORD rather than a rate card:
     observed sell/buy costs by asset class out of transaction_ledger.json, plus AJ Bell's own FX
     charge for a USD leg. Below the framework's declared minimum economic trade the recall is
     REFUSED, and the refusal is the output.
  4  THERE IS NO CGT INSIDE AN ISA. That is what makes a round trip affordable here and it is
     STATED, so its absence is a recorded fact and not an oversight.
  5  THE 182-DAY MIN-HOLD BINDS THE STOCK LEG, NOT THE FUND LEG. It is an anti-churn rule on
     positions the framework bought; a fund used as a waiting room was never held on that basis.

⚑⚑ THE INTERACTION NOBODY HAD STATED, AND IT BITES TODAY. Raj declared the scaling freeze's basis
as `reallocation_only` on 20-Aug-2026 (ISA-0387): the freeze binds capital whose SOURCE is a
disposal FROM THE FUND SLEEVE. A recall is exactly that. So while the freeze is active the recall
leg is BARRED — until 2026-11-01 (override) or 2026-12-01 (mechanical). Building the leg without
that check would have made ISA-0390 quietly overrule ISA-0387 the day after it was declared. The
freeze is read from its one home, never copied.

ROLLBACK (R4.13): `ENABLED = False` -> every entry point returns DISABLED and writes nothing.

CLI:  python3 waiting_room.py [--report] [--selftest]
"""
from __future__ import annotations
import datetime as dt, json, os, statistics as st, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENABLED = True
SCHEMA_VERSION = "1.0.0"
LEDGER_FILE = "waiting_room_ledger.json"

# ⚑ NO NEW CAPITAL-GATING CONSTANT IS DECLARED HERE, ON PURPOSE. The economic floor is
# `portfolio_analytics.MIN_ECONOMIC_TRADE` — already declared, already the framework's answer to
# "when is a trade too small to be worth its costs" — and the "declared fraction" ISA-0390 asks for
# is DERIVED from it and the observed dealing record on every run rather than typed. A second
# threshold for the same question would be two homes for one rule (R4.4).


class RecallRefused(RuntimeError):
    """A recall that must not happen. Raised, never downgraded to a warning."""


def _today() -> str:
    return dt.date.today().isoformat()


def _fig(value, *, as_of, source, unit=None, note=None):
    return {"value": value, "as_of": as_of, "source": source, "unit": unit, "note": note,
            "present": value is not None}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §1  THE PRICE OF A ROUND TRIP — measured from the dealing record, never from a rate card
# ══════════════════════════════════════════════════════════════════════════════════════════════
def round_trip_cost(position_gbp: float, *, stock_is_usd: bool = True,
                    ledger_path=None) -> dict:
    """-> what one recall actually costs, from what AJ Bell has actually charged.

    R6.1 one golden source: `transaction_ledger.json` IS the dealing record. R4.1: if it cannot be
    read the cost is UNMEASURED and the recall REFUSES — it is never assumed to be zero, and a
    zero cost would make every recall look economic.
    """
    p = Path(ledger_path or HERE / "transaction_ledger.json")
    if not p.exists():
        return {"state": "UNMEASURED",
                "reason": ("transaction_ledger.json is absent. The round-trip cost is the dealing "
                           "record's to state; with no record the cost is UNMEASURED, which is a "
                           "different fact from zero (R4.1), and a recall priced at zero would "
                           "always look economic.")}
    try:
        entries = json.load(open(p))["entries"]
    except Exception as e:                                            # noqa: BLE001
        return {"state": "UNREADABLE", "reason": "%s: %s" % (type(e).__name__, e)}

    def med(asset_class, kind):
        xs = [float(r["cost_gbp"]) for r in entries
              if r.get("asset_class") == asset_class and r.get("type") == kind
              and r.get("cost_gbp") is not None]
        # ⚑ the MEDIAN, because a handful of stock buys carry the FX charge inside `cost_gbp` and
        # a mean would smear that across the commission. FX is added back explicitly below, once.
        return (st.median(xs), len(xs)) if xs else (None, 0)

    sell_fund, n_sf = med("fund", "sell")
    buy_stock, n_bs = med("stock", "buy")
    if sell_fund is None or buy_stock is None:
        return {"state": "UNMEASURED",
                "reason": ("the dealing record contains no %s observation to price this leg"
                           % ("fund sell" if sell_fund is None else "stock buy"))}
    # the stock-buy median includes FX on the observations that had it; take the FLOOR of the
    # observed stock-buy costs as the commission and add FX explicitly, so nothing is double-counted
    stock_costs = [float(r["cost_gbp"]) for r in entries
                   if r.get("asset_class") == "stock" and r.get("type") == "buy"
                   and r.get("cost_gbp") is not None]
    commission_stock = min(stock_costs)
    try:
        from extract_cash_statement import FX_RATE_FRACTION as FX
        fx_src = "extract_cash_statement.FX_RATE_FRACTION"
    except Exception as e:                                            # noqa: BLE001
        return {"state": "UNMEASURED",
                "reason": ("the FX charge has one home (extract_cash_statement.FX_RATE_FRACTION) "
                           "and it could not be read: %s: %s" % (type(e).__name__, e))}
    fx_gbp = (position_gbp * FX) if stock_is_usd else 0.0
    total = sell_fund + commission_stock + fx_gbp

    try:
        from portfolio_analytics import MIN_ECONOMIC_TRADE as FLOOR
        floor_src = "portfolio_analytics.MIN_ECONOMIC_TRADE"
    except Exception as e:                                            # noqa: BLE001
        return {"state": "UNMEASURED",
                "reason": ("the economic floor has one home (portfolio_analytics."
                           "MIN_ECONOMIC_TRADE) and it could not be read: %s: %s"
                           % (type(e).__name__, e))}
    max_fraction = (sell_fund + commission_stock) / float(FLOOR)
    fraction = (total / position_gbp) if position_gbp else None
    return {
        "state": "MEASURED", "as_of": _today(),
        "position_gbp": round(position_gbp, 2),
        "fund_sell_gbp": _fig(round(sell_fund, 2), as_of=_today(),
                              source="transaction_ledger.json median of %d fund SELL rows" % n_sf,
                              unit="GBP"),
        "stock_buy_commission_gbp": _fig(round(commission_stock, 2), as_of=_today(),
                                         source=("transaction_ledger.json minimum of %d stock BUY "
                                                 "rows — the commission with no FX inside it"
                                                 % n_bs), unit="GBP"),
        "fx_charge_gbp": _fig(round(fx_gbp, 2), as_of=_today(), source=fx_src, unit="GBP",
                              note=("AJ Bell charges %.2f%% on the USD leg; not applied to a "
                                    "GBP-denominated purchase" % (FX * 100))),
        "round_trip_gbp": round(total, 2),
        "round_trip_fraction_of_position": (None if fraction is None else round(fraction, 5)),
        "declared_max_fraction": round(max_fraction, 5),
        "declared_max_fraction_basis": (
            "DERIVED, not chosen: the framework already declares %s = GBP %.0f as the size below "
            "which a trade is uneconomical. The implied maximum acceptable round-trip fraction is "
            "the fixed cost of the round trip over that floor. One home, one rule."
            % (floor_src, float(FLOOR))),
        "min_economic_trade_gbp": float(FLOOR),
        "economic": bool(position_gbp >= float(FLOOR)),
        "cgt": ("NOT APPLICABLE — this is a stocks-and-shares ISA. No capital gains tax arises on "
                "the disposal leg, which is exactly what makes a round trip affordable here. "
                "Stated so the absence is a recorded fact rather than an omission (ISA-0390 (4))."),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §2  THE LEDGER — parked capital is a TAG with a date, or it is an allocation
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _load_ledger(path=None) -> dict:
    p = Path(path or HERE / LEDGER_FILE)
    if not p.exists():
        return {"schema_version": SCHEMA_VERSION, "_meta": {"created": _today(),
                "module": "waiting_room.py", "item": "ISA-0390"}, "lots": []}
    return json.load(open(p))


def _save_ledger(doc, path=None) -> str:
    p = Path(path or HERE / LEDGER_FILE)
    doc["_meta"] = dict(doc.get("_meta") or {}, last_written=_today(),
                        module="waiting_room.py", item="ISA-0390")
    p.write_text(json.dumps(doc, indent=2))
    return str(p)


def park(amount_gbp: float, destinations: dict, *, reason: str, as_of=None, run_id=None,
         ledger_path=None) -> dict:
    """Record capital placed in funds FOR WANT OF A QUALIFYING STOCK CANDIDATE.

    ⚑ THE TAG IS THE WHOLE POINT. Untagged, this is indistinguishable from an allocation, and the
    difference is not recoverable later from the portfolio file — R6.5, retain first.
    """
    if not ENABLED:
        return {"state": "DISABLED", "reason": "waiting_room.ENABLED is False (R4.13)"}
    if amount_gbp <= 0:
        raise RecallRefused("a parked lot must carry a positive amount; got %r" % amount_gbp)
    placed = round(sum(float(v or 0.0) for v in destinations.values()), 2)
    if abs(placed - round(amount_gbp, 2)) > 0.01:                     # R5.2 stated tolerance
        raise RecallRefused(
            "the parked amount GBP %.2f and the destinations GBP %.2f do not agree. A lot whose "
            "parts do not sum to its whole cannot be recalled against."
            % (round(amount_gbp, 2), placed))
    led = _load_ledger(ledger_path)
    # ⚑ IDEMPOTENT BY run_id. `park()` is called from a path that runs every month and may be
    # re-run inside a month; without this a second run silently doubles the parked balance and the
    # recall leg would then be sized against capital that does not exist. R4.11 — the guard lives
    # in the function that writes the artefact, not in a prose instruction to the caller.
    if run_id is not None:
        dup = [l for l in led["lots"] if l.get("run_id") == run_id]
        if dup:
            return {"state": "ALREADY_PARKED", "lot": dup[0],
                    "reason": "run_id %r already has a lot (%s); park() is idempotent by run_id"
                              % (run_id, dup[0]["lot_id"])}
    lot = {"lot_id": "WR-%s-%03d" % ((as_of or _today()).replace("-", ""), len(led["lots"]) + 1),
           "parked_on": as_of or _today(), "run_id": run_id,
           "amount_gbp": round(amount_gbp, 2),
           "destinations": {k: round(float(v), 2) for k, v in destinations.items() if v},
           "reason": reason, "status": "PARKED", "recalled_gbp": 0.0, "recalls": []}
    led["lots"].append(lot)
    written = _save_ledger(led, ledger_path)
    return {"state": "PARKED", "lot": lot, "_written": written}


def outstanding(ledger_path=None) -> dict:
    led = _load_ledger(ledger_path)
    live = [l for l in led["lots"] if l.get("status") != "FULLY_RECALLED"]
    total = round(sum(l["amount_gbp"] - l.get("recalled_gbp", 0.0) for l in live), 2)
    by_fund = {}
    for l in live:
        share = (l["amount_gbp"] - l.get("recalled_gbp", 0.0)) / l["amount_gbp"] if l["amount_gbp"] else 0
        for sd, v in (l.get("destinations") or {}).items():
            by_fund[sd] = round(by_fund.get(sd, 0.0) + v * share, 2)
    return {"state": "MEASURED", "as_of": _today(), "parked_gbp": total,
            "lots_live": len(live), "by_fund_gbp": by_fund,
            "oldest_parked_on": min([l["parked_on"] for l in live], default=None)}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §3  THE RECALL — the ranking run in REVERSE, and the freeze consulted first
# ══════════════════════════════════════════════════════════════════════════════════════════════
def freeze_state(policy=None) -> dict:
    """-> whether a recall is permitted TODAY, read from the freeze's one home (ISA-0387)."""
    tw = policy or json.load(open(HERE / "target_weights.json"))
    fz = tw.get("scaling_freeze") or {}
    if not fz.get("active"):
        return {"state": "PERMITTED", "reason": "no scaling freeze is active"}
    basis = fz.get("basis")
    if not basis:
        return {"state": "REFUSED", "basis": None,
                "reason": ("the scaling freeze is ACTIVE and declares no `basis` (ISA-0387). A "
                           "recall cannot be judged against a constraint whose unit nobody "
                           "declared.")}
    if basis in ("reallocation_only", "pounds"):
        return {"state": "BARRED", "basis": basis,
                "earliest_override": fz.get("earliest_unfreeze_override"),
                "earliest_mechanical": fz.get("earliest_unfreeze_mechanical"),
                "reason": ("the freeze basis is `%s`. A recall trims the FUND SLEEVE to fund the "
                           "stock sleeve — a disposal from funds into stocks — which is precisely "
                           "the act this basis bars. Earliest %s (override) / %s (mechanical)."
                           % (basis, fz.get("earliest_unfreeze_override"),
                              fz.get("earliest_unfreeze_mechanical")))}
    return {"state": "PERMITTED", "basis": basis,
            "reason": ("the freeze basis is `weight`, which constrains the SIZE of the sleeve and "
                       "not the source of the capital, so a recall inside the derived cap is not "
                       "barred by it")}


def donor_order(portfolio=None, universe=None, ranking=None, policy=None, frs_by_sedol=None):
    """-> the donor ranking, READ from `capital_destination.donor_order` (A7 / ISA-0440).

    ⚑ THIS USED TO REVERSE THE BUY KEY, AND THAT WAS WRONG IN A WAY R4.4 DOES NOT CATCH. The old
    basis line — "one ordering rule, read not copied (ISA-0386)" — was sound about copies and
    unsound about this decision: `_rank_key` puts band deviation LAST on purpose, so reversing it
    puts "how far above its own declared band this money sits" last on the SELL side, where C1 and
    C2 have already decided the order. A7 makes it first. R4.4 forbids two homes for ONE rule; the
    buy rule and the sell rule are two rules, and the sell one now has exactly one home, which
    this function READS rather than reimplements (R4.5).

    Everything this function adds on top is about the WAITING ROOM specifically and belongs
    nowhere else: what is PARKED, and how far a fund can be sold before it breaches its declared
    `band_low`.
    """
    import capital_destination as CD
    portfolio = portfolio or json.load(open(HERE / "portfolio_data_aug_2026.json"))
    universe = universe or json.load(open(HERE / "fund_universe.json"))
    tw = policy or json.load(open(HERE / "target_weights.json"))
    total = float(portfolio["summary"]["total_value_gbp"])
    parked = (outstanding().get("by_fund_gbp") or {})
    a7 = CD.donor_order(portfolio=portfolio, universe=universe, ranking=ranking, policy=tw,
                        frs_by_sedol=frs_by_sedol, nav_gbp=total)

    cands = []
    for d in a7["donors"]:
        sd = d["sedol"]
        b = (tw["funds"].get(sd) or {})
        lo = b.get("band_low")
        floor_gbp = (lo * total) if lo is not None else 0.0
        v = d["value_gbp"]
        cands.append({
            "sedol": sd, "value_gbp": round(v, 2),
            "weight_pct": round(v / total * 100, 2),
            "parked_gbp": parked.get(sd, 0.0),
            # ⚑ the sell head-room is to the DECLARED band_low, never below it: a recall may not
            # create the band breach the framework would then have to repair.
            "sellable_to_band_low_gbp": round(max(v - floor_gbp, 0.0), 2),
            "band_low_pct": (None if lo is None else lo * 100),
            "donor_rank": d["donor_rank"],
            "why": d["why"],
            "rank": d["rank"],
        })
    return {"state": a7["state"], "as_of": _today(),
            "basis": ("capital_destination.donor_order — the A7 sell-side ordering, READ not "
                      "copied (ISA-0440). %s" % a7["basis"]),
            "a7_order": a7["order"], "a7_enabled": a7["enabled"],
            "frs_supplied": a7["frs_supplied"], "donors": cands,
            "parked_first": ("a lot that was PARKED is recalled from the funds it was parked in "
                             "before any other donor is touched: it is the capital whose stay was "
                             "always conditional")}


def recall(amount_gbp: float, *, portfolio=None, universe=None, ranking=None, policy=None,
           stock_is_usd=True, honour_freeze=True, ledger_path=None) -> dict:
    """-> the donor plan for GBP `amount_gbp` of stock capital, or the REFUSAL, with its reason."""
    if not ENABLED:
        return {"state": "DISABLED", "reason": "waiting_room.ENABLED is False (R4.13)"}
    tw = policy or json.load(open(HERE / "target_weights.json"))
    fz = freeze_state(tw)
    cost = round_trip_cost(amount_gbp, stock_is_usd=stock_is_usd)
    order = donor_order(portfolio, universe, ranking, tw)
    out = {"_meta": {"module": "waiting_room.py", "schema_version": SCHEMA_VERSION,
                     "as_of": _today(), "item": "ISA-0390", "enabled": ENABLED},
           "requested_gbp": round(amount_gbp, 2),
           "freeze": fz, "round_trip": cost, "parked": outstanding(ledger_path),
           "min_hold": {
               "binds": "the STOCK leg only",
               "days": _min_hold_days(),
               "basis": ("scoring_config.MIN_HOLD_DAYS is an ANTI-CHURN rule on positions the "
                         "framework bought on a thesis. A fund used as the stock sleeve's waiting "
                         "room was never held on that basis, so it does not bind the donor leg "
                         "(ISA-0390 (5)). It binds the stock that the recall BUYS, from the day "
                         "that stock is bought."),
           }}

    if honour_freeze and fz["state"] in ("BARRED", "REFUSED"):
        out["state"] = "REFUSED_BY_FREEZE"
        out["plan"] = []
        out["reason"] = fz["reason"]
        return out
    if cost.get("state") != "MEASURED":
        out["state"] = "REFUSED_COST_UNMEASURED"
        out["plan"] = []
        out["reason"] = cost.get("reason")
        return out
    if not cost["economic"]:
        out["state"] = "REFUSED_UNECONOMIC"
        out["plan"] = []
        out["reason"] = (
            "a recall of GBP %.2f costs GBP %.2f round trip (%.2f%% of the position), against a "
            "declared minimum economic trade of GBP %.0f. %s"
            % (amount_gbp, cost["round_trip_gbp"],
               (cost["round_trip_fraction_of_position"] or 0) * 100,
               cost["min_economic_trade_gbp"], cost["declared_max_fraction_basis"]))
        return out

    # ── build the plan: PARKED lots first, then the reversed ranking ──────────────────────────
    remaining, plan = round(amount_gbp, 2), []
    for pool in ("parked_gbp", "sellable_to_band_low_gbp"):
        for d in order["donors"]:
            if remaining <= 0.005:
                break
            already = sum(p["gbp"] for p in plan if p["sedol"] == d["sedol"])
            head = max(min(d[pool], d["sellable_to_band_low_gbp"]) - already, 0.0)
            if head <= 0.005:
                continue
            take = round(min(head, remaining), 2)
            plan.append({"sedol": d["sedol"], "gbp": take, "donor_rank": d["donor_rank"],
                         "pool": ("PARKED" if pool == "parked_gbp" else "band headroom"),
                         "weight_pct": d["weight_pct"], "band_low_pct": d["band_low_pct"]})
            remaining = round(remaining - take, 2)
        if remaining <= 0.005:
            break

    out["plan"] = plan
    out["shortfall_gbp"] = round(remaining, 2)
    out["state"] = "RECALL_PLANNED" if remaining <= 0.005 else "RECALL_PARTIAL"
    out["reason"] = (
        "donors are the A7 sell-side ordering (ISA-0440): pounds above the declared band_high "
        "first, then look-through and concentration relief, then cost to keep, with FRS as a "
        "VOTE and never as authority — taking PARKED capital before any other headroom, and "
        "never below a declared band_low.")
    return out


def _min_hold_days():
    try:
        import scoring_config as SC
        return int(SC.MIN_HOLD_DAYS)
    except Exception:                                                 # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════════════════════════════════════════
def report(doc=None) -> str:
    d = doc or recall(2000.0)
    L = ["WAITING ROOM / RECALL LEG (ISA-0390) — %s" % d.get("state"),
         "requested GBP %.2f" % d.get("requested_gbp", 0.0),
         "freeze: %s — %s" % (d["freeze"]["state"], d["freeze"]["reason"]), ""]
    p = d.get("parked") or {}
    L.append("parked capital outstanding: GBP %.2f across %d lot(s)"
             % (p.get("parked_gbp", 0.0), p.get("lots_live", 0)))
    rt = d.get("round_trip") or {}
    if rt.get("state") == "MEASURED":
        L.append("round trip: GBP %.2f (%.3f%% of the position) vs a declared max of %.3f%%"
                 % (rt["round_trip_gbp"], (rt["round_trip_fraction_of_position"] or 0) * 100,
                    rt["declared_max_fraction"] * 100))
        L.append("CGT: " + rt["cgt"].split(" —")[0])
    for row in d.get("plan") or []:
        L.append("  sell GBP %8.2f  %-9s [donor rank %d, %s]"
                 % (row["gbp"], row["sedol"], row["donor_rank"], row["pool"]))
    if not d.get("plan"):
        L.append("  NO PLAN — " + str(d.get("reason"))[:200])
    return "\n".join(L)


def _cd_above_band_high(order_out):
    """-> [(sedol, pounds above declared band_high)] for the A7 assertions, derived INDEPENDENTLY.

    ⚑ R5.2 — this recomputes P1 from `target_weights.json` and the portfolio rather than reading
    the `p1` block the module under test produced, so the assertion is a second derivation and not
    a restatement of the first.
    """
    tw = json.load(open(HERE / "target_weights.json"))
    portfolio = json.load(open(HERE / "portfolio_data_aug_2026.json"))
    total = float(portfolio["summary"]["total_value_gbp"])
    out = []
    for d in order_out["donors"]:
        hi = (tw["funds"].get(d["sedol"]) or {}).get("band_high")
        out.append((d["sedol"],
                    0.0 if hi is None else max(float(d["value_gbp"]) - float(hi) * total, 0.0)))
    return out


def _a7_rollback_moves_the_order(tw2) -> bool:
    """R4.13 + reachability: flipping A7 OFF must produce a DIFFERENT head of the donor order.

    ⚑ THIS ASSERTS THE ORDER MOVES, NOT THAT THE HEAD MOVES, AND THE DIFFERENCE IS A MEASUREMENT.
    On the August book A7 and the rule it replaces AGREE on donor #1 — B2PLJD7 is both the fund
    furthest above its band_high and the fund the reversed buy key ranked first — and they differ
    at positions 5/6 and 8/10. Requiring the head to move would make this assertion fail for a
    reason that is about the book rather than about the build. That P1 can DECIDE the head is a
    separate, stronger control: `_a7_p1_is_decisive`. ⚑ And if this one ever goes green on
    equality — on != off becoming false — A7 has gone inert and ISA-0440's `falsified_by` clause
    ("A7's reordering changes no donor across three consecutive runs") is being met.
    """
    import capital_destination as CD
    was = CD.A7_DONOR_ORDER_ENABLED
    try:
        on = [d["sedol"] for d in donor_order(policy=tw2)["donors"]]
        CD.A7_DONOR_ORDER_ENABLED = False
        off = [d["sedol"] for d in donor_order(policy=tw2)["donors"]]
    finally:
        CD.A7_DONOR_ORDER_ENABLED = was
    return on != off


def _a7_p1_is_decisive(tw2) -> bool:
    """NEGATIVE CONTROL for A7's P1, and it had to be built as a FIXTURE rather than read off the
    live book.

    ⚑ WHY, AND THIS IS THE POINT OF THE ASSERTION. On the August book the fund that is over its
    band_high (B2PLJD7, GBP 197 above a 9.00% ceiling) is ALSO the fund the old reversed-buy rule
    put first, so "did the head of the order change?" answers NO and proves nothing either way.
    An assertion that cannot distinguish "P1 decided this" from "P1 agreed with what already
    happened" is not a control. So the fixture drops the band_high of the fund the OLD rule ranks
    LAST until it is over its ceiling, and asserts A7 promotes it to #1 while the rollback leaves
    it where it was.
    """
    import capital_destination as CD
    portfolio = json.load(open(HERE / "portfolio_data_aug_2026.json"))
    total = float(portfolio["summary"]["total_value_gbp"])
    was = CD.A7_DONOR_ORDER_ENABLED
    try:
        CD.A7_DONOR_ORDER_ENABLED = False
        old_order = [d["sedol"] for d in donor_order(policy=tw2)["donors"]]
        target = old_order[-1]
        val = next(float(f["value_gbp"]) for f in portfolio["funds"]
                   if f["ticker"] == target)
        tw3 = json.loads(json.dumps(tw2))
        # put it just over its ceiling, and leave every other declared band untouched
        tw3["funds"][target]["band_high"] = (val / total) * 0.90
        still_last = [d["sedol"] for d in donor_order(policy=tw3)["donors"]][-1] == target
        CD.A7_DONOR_ORDER_ENABLED = True
        promoted = [d["sedol"] for d in donor_order(policy=tw3)["donors"]][0] == target
    finally:
        CD.A7_DONOR_ORDER_ENABLED = was
    return bool(promoted and still_last)


def selftest(verbose=True) -> int:
    import tempfile
    fails, ran = [], []

    def ck(name, cond):
        ran.append(name)
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    tmp = Path(tempfile.mkdtemp()) / "wr_ledger.json"      # ⚑ NEVER beside the scripts: the mount
    #                                                        denies delete, so a fixture written
    #                                                        here would become live policy.
    # ── §1 cost ───────────────────────────────────────────────────────────────────────────────
    c = round_trip_cost(4000.0)
    ck("round-trip cost is MEASURED from the dealing record", c["state"] == "MEASURED")
    ck("the cost carries as_of and source on every component (R4.2)",
       all(c[k]["as_of"] and c[k]["source"]
           for k in ("fund_sell_gbp", "stock_buy_commission_gbp", "fx_charge_gbp")))
    ck("CGT is STATED, not omitted", "NOT APPLICABLE" in c["cgt"])
    ck("the max round-trip fraction is DERIVED from MIN_ECONOMIC_TRADE, not typed",
       abs(c["declared_max_fraction"]
           - (c["fund_sell_gbp"]["value"] + c["stock_buy_commission_gbp"]["value"])
           / c["min_economic_trade_gbp"]) < 1e-9)
    ck("a GBP leg carries NO FX charge and a USD leg does (negative control)",
       round_trip_cost(4000.0, stock_is_usd=False)["fx_charge_gbp"]["value"] == 0.0
       and c["fx_charge_gbp"]["value"] > 0.0)
    ck("an ABSENT dealing record REFUSES, it never prices the trip at zero (R4.3)",
       round_trip_cost(4000.0, ledger_path=tmp.parent / "nope.json")["state"] == "UNMEASURED")

    # ── §2 ledger ─────────────────────────────────────────────────────────────────────────────
    pk = park(1000.0, {"VUAG": 600.0, "SMT": 400.0}, reason="no qualifying stock candidate",
              ledger_path=tmp)
    ck("a parked lot is written with amount, date and destinations",
       pk["state"] == "PARKED" and pk["lot"]["amount_gbp"] == 1000.0
       and pk["lot"]["parked_on"] and pk["lot"]["destinations"])
    ck("parked capital is readable back as an outstanding balance",
       outstanding(tmp)["parked_gbp"] == 1000.0)
    again = park(1000.0, {"VUAG": 600.0, "SMT": 400.0}, reason="same run", run_id="RUN-X",
                 ledger_path=tmp)
    twice = park(1000.0, {"VUAG": 600.0, "SMT": 400.0}, reason="same run", run_id="RUN-X",
                 ledger_path=tmp)
    ck("park() is IDEMPOTENT by run_id — a re-run does not double the parked balance",
       again["state"] == "PARKED" and twice["state"] == "ALREADY_PARKED"
       and outstanding(tmp)["parked_gbp"] == 2000.0)
    ck("a lot with NO run_id is still written (the guard does not swallow untagged capital)",
       park(50.0, {"VUAG": 50.0}, reason="untagged", ledger_path=tmp)["state"] == "PARKED")
    bad = False
    try:
        park(1000.0, {"VUAG": 600.0}, reason="x", ledger_path=tmp)
    except RecallRefused:
        bad = True
    ck("a lot whose parts do not sum to its whole is REFUSED (R5.2 negative control)", bad)

    # ── §3 recall ─────────────────────────────────────────────────────────────────────────────
    r = recall(4000.0, ledger_path=tmp)
    ck("the ACTIVE reallocation_only freeze BARS the recall (ISA-0387 interaction)",
       r["state"] == "REFUSED_BY_FREEZE" and r["freeze"]["state"] == "BARRED")
    ck("the refusal names the earliest date the freeze could lift",
       bool(r["freeze"].get("earliest_override")) and bool(r["freeze"].get("earliest_mechanical")))

    tw = json.load(open(HERE / "target_weights.json"))
    tw2 = json.loads(json.dumps(tw))
    tw2["scaling_freeze"]["active"] = False
    r2 = recall(4000.0, policy=tw2, ledger_path=tmp)
    ck("with the freeze lifted the recall PLANS, so the bar is the freeze and not the build",
       r2["state"] in ("RECALL_PLANNED", "RECALL_PARTIAL") and len(r2["plan"]) > 0)
    ck("the donor plan never sells a fund below its DECLARED band_low",
       all(row["band_low_pct"] is None
           or row["gbp"] <= next(d["sellable_to_band_low_gbp"]
                                 for d in donor_order(policy=tw2)["donors"]
                                 if d["sedol"] == row["sedol"]) + 0.01
           for row in r2["plan"]))
    # ⚑ R5.8 — the assertion this replaces ("donors are the BUY ranking reversed") checked only
    # that a rank 1 existed and differed from the last row. It could not have failed on any real
    # reordering defect, and it did NOT fail when A7 changed the rule underneath it. These do.
    _a7 = donor_order(policy=tw2)
    ck("A7: the donor ordering is READ from capital_destination, not decided here (R4.5)",
       "capital_destination.donor_order" in _a7["basis"] and _a7["a7_enabled"] is True)
    _over = [x for x in _cd_above_band_high(_a7) if x[1] > 0]
    ck("A7: donor #1 is the fund furthest above its OWN declared band_high, whenever one is over",
       (not _over) or _a7["donors"][0]["sedol"] == max(_over, key=lambda x: x[1])[0])
    ck("A7: on the LIVE book the reorder changes the donor ORDER — measured, not assumed",
       _a7_rollback_moves_the_order(tw2))
    ck("A7 negative control: P1 is DECISIVE — a fund the OLD rule ranked low is promoted to "
       "donor #1 as soon as it sits above its declared band_high, and the rollback does not "
       "promote it (ISA-0388: the criterion must MOVE capital, not decorate the order)",
       _a7_p1_is_decisive(tw2))
    small = recall(100.0, policy=tw2, ledger_path=tmp)
    ck("a recall below the declared minimum economic trade is REFUSED, with the price stated",
       small["state"] == "REFUSED_UNECONOMIC" and "round trip" in small["reason"])
    ck("the 182-day min-hold is declared to bind the STOCK leg only",
       r2["min_hold"]["binds"] == "the STOCK leg only" and r2["min_hold"]["days"] == 182)

    # ── R4.3 negative control: an ACTIVE freeze with no basis REFUSES, never permits ──────────
    tw3 = json.loads(json.dumps(tw))
    tw3["scaling_freeze"].pop("basis", None)
    ck("an ACTIVE freeze with NO declared basis REFUSES the recall (R4.3 — never PASS)",
       freeze_state(tw3)["state"] == "REFUSED")

    # ── R4.13 rollback ───────────────────────────────────────────────────────────────────────
    global ENABLED
    ENABLED = False
    ck("rollback constant disables every entry point",
       recall(4000.0)["state"] == "DISABLED"
       and park(1.0, {"VUAG": 1.0}, reason="x")["state"] == "DISABLED")
    ENABLED = True

    # ⚑ the count is COUNTED, not typed. It read "20 assertions green" as a literal while the
    # suite ran 23 — the project's own smallest instance of a figure that says one thing and IS
    # another, in the one place a reader trusts without checking (R4.2 / FC-B).
    print("\nwaiting_room selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails
             else " — %d assertions green" % len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--report" in sys.argv:
        print(report())
    else:
        print(json.dumps(recall(4000.0), indent=2))
