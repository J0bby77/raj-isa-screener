#!/usr/bin/env python3
"""
mmf_funding.py - ISA-0904 (Raj 26-Oct-2026): the CASH-FUND SALE that funds the month's share purchases.

"One thing that was potentially missing from last month is the instruction to sell X units of the MMF position to
fund the share purchase(s), so I know exactly how many units to sell before I can actually fund the purchase(s)."

The framework counts the money-market fund (CSH2) as cash everywhere (B2), so a decided purchase is 'cash-funded'
even when the broker's cash balance cannot pay for it until cash-fund units are sold. This module turns the FINAL
decided actions into the dealing instruction: how many whole units to sell, at what reference price, what that
raises, and the order of dealing (sell, let it settle, then buy - funding_waterfall D8 applies the same rule to
fund trims).

    available cash = broker cash (statement cash less the cash fund)
                     - the D23 reserve - any fired drawdown reservation      (funding_waterfall: each pound once)
    needed         = sum of the decided BUY amounts (all-in) - sum of decided share SALE proceeds
    shortfall      = needed - available cash
    units to sell  = ceil((shortfall + dealing charge) / unit price), never more than are held

Pure calculation (no file writes, no trades). `from_files` reads the run's own inputs. A missing input is a typed
state, never a default (R4.1): AMOUNT_NOT_STATED, PRICE_UNKNOWN, INSUFFICIENT_CASH_FUND (fund trims then decide,
ISA-0858 D4 - not this module).
"""
from __future__ import annotations

import json
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SALE_CHARGE_GBP = 5.00          # AJ Bell online dealing charge for an exchange-traded fund (Run_Context costs)
DEALING = ("BUY", "ADD", "TOP-UP")
SELLING = ("SELL", "TRIM")


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[£,\s]|&pound;", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def _verb(a):
    return str((a or {}).get("action", "")).strip().upper().split(" ")[0]


def plan(actions, *, cash_stated_gbp, mmf, reserve_gbp, drawdown_reserved_gbp=0.0, as_of=None,
         sale_charge_gbp=SALE_CHARGE_GBP):
    """actions: the decided s1 actions (each BUY/SELL with a numeric `amount_gbp`, all-in).
    mmf: {"ticker", "name", "quantity", "price", "value_gbp"} - the cash-fund holding at the valuation date."""
    buys = [a for a in actions or [] if _verb(a) in DEALING]
    sells = [a for a in actions or [] if _verb(a) in SELLING]
    if not buys:
        return {"state": "NO_PURCHASE", "line": "No share purchase is decided this month - nothing to fund."}
    amts = [(_num(a.get("amount_gbp")), a) for a in buys + sells]
    missing = [str(a.get("instrument") or a.get("name") or a.get("ticker") or "?") for v, a in amts if v is None]
    if missing:
        return {"state": "AMOUNT_NOT_STATED", "missing": missing,
                "line": "Funding cannot be stated: no numeric amount_gbp on " + ", ".join(missing) + "."}
    need_buy = sum(_num(a["amount_gbp"]) for a in buys)
    from_sales = sum(_num(a["amount_gbp"]) for a in sells)
    mmf_value = float((mmf or {}).get("value_gbp") or 0.0)
    broker_cash = round(float(cash_stated_gbp) - mmf_value, 2)
    available = round(broker_cash - float(reserve_gbp) - float(drawdown_reserved_gbp or 0.0), 2)
    shortfall = round(need_buy - from_sales - available, 2)
    out = {"as_of": as_of, "buys_gbp": round(need_buy, 2), "share_sale_proceeds_gbp": round(from_sales, 2),
           "broker_cash_gbp": broker_cash, "reserve_gbp": float(reserve_gbp),
           "drawdown_reserved_gbp": float(drawdown_reserved_gbp or 0.0), "available_cash_gbp": available,
           "shortfall_gbp": max(0.0, shortfall), "cash_fund": {k: (mmf or {}).get(k) for k in
                                                              ("ticker", "name", "quantity", "price", "value_gbp")}}
    if shortfall <= 0:
        out.update(state="CASH_COVERS", units_to_sell=0,
                   line="No cash-fund sale is needed: the broker cash of GBP %s (after the GBP %s reserve) covers the "
                        "GBP %s of purchases." % (format(broker_cash, ",.2f"), format(float(reserve_gbp), ",.0f"),
                                                  format(need_buy, ",.2f")))
        return out
    price = _num((mmf or {}).get("price"))
    if price is None and _num((mmf or {}).get("quantity")):
        price = mmf_value / float(_num(mmf["quantity"]))
    if not price or price <= 0:
        out.update(state="PRICE_UNKNOWN", line="Funding needs a cash-fund sale of about GBP %s, but the unit price is "
                                               "not on file - check the live price before dealing." % format(shortfall, ",.2f"))
        return out
    units = int(math.ceil((shortfall + sale_charge_gbp) / price - 1e-9))
    held = _num((mmf or {}).get("quantity"))
    out.update(price=round(price, 4), units_to_sell=units, proceeds_gbp=round(units * price - sale_charge_gbp, 2),
               units_held=held)
    if held is not None and units > held + 1e-9:
        out.update(state="INSUFFICIENT_CASH_FUND",
                   line="The purchases need GBP %s more than the broker cash; selling all %g cash-fund units raises "
                        "about GBP %s - the rest would need a fund sale (decided by the funding rules, not here)."
                        % (format(shortfall, ",.2f"), held, format(held * price - sale_charge_gbp, ",.2f")))
        return out
    _nm = re.sub(r"\s*\([A-Z]+:[A-Z0-9.]+\)\s*$", "", str((mmf or {}).get("name") or "the cash fund"))
    out.update(state="SELL_CASH_FUND",
               line="Sell %d units of %s (%s) first - about GBP %s at GBP %s a unit on %s, after the GBP %.0f charge - "
                    "then place the purchases once the sale has settled (two working days). Check the live unit price: "
                    "the count is rounded up." % (units, _nm,
                                                   (mmf or {}).get("ticker") or "", format(units * price - sale_charge_gbp, ",.2f"),
                                                   format(price, ",.2f"), as_of or "the valuation date", sale_charge_gbp))
    return out


def from_files(actions, portfolio_path, root=HERE):
    """The plan from the run's own files: portfolio_data (cash, the cash-fund holding), target_weights (D23 reserve),
    the drawdown reservation (funding_waterfall, one home)."""
    with open(portfolio_path, encoding="utf-8") as f:
        pf = json.load(f)
    s = pf.get("summary") or {}
    mm = (s.get("mmf_holdings") or [{}])[0] if s.get("mmf_holdings") else {}
    import sys
    sys.path.insert(0, root)
    import funding_waterfall as _fw                                         # noqa: E402
    tw = _fw._json(os.path.join(root, "target_weights.json"), {}) or {}
    _r = (tw.get("stock_sleeve") or {}).get("cash_reserve_gbp")
    if _r is None:
        return {"state": "RESERVE_UNDECLARED", "line": "Funding cannot be stated: the D23 cash reserve is not declared "
                                                       "(target_weights.stock_sleeve.cash_reserve_gbp)."}
    reserve = float(_r)
    dd = _fw.drawdown_reservation(_fw._json(os.path.join(root, "drawdown_state.json"), {}),
                                  (_fw._json(os.path.join(root, "transaction_ledger.json"), {}) or {}).get("entries") or [])
    if dd.get("reserved_gbp") is None:
        return {"state": "RESERVATION_UNKNOWN", "line": "Funding cannot be stated: the drawdown reservation cannot be "
                                                        "measured (%s)." % dd.get("why")}
    return plan(actions, cash_stated_gbp=s.get("cash_stated_gbp") or 0.0, mmf=mm, reserve_gbp=reserve,
                drawdown_reserved_gbp=dd["reserved_gbp"], as_of=(pf.get("_meta") or {}).get("data_date"))


def _selftest():
    mm = {"ticker": "CSH2", "name": "Amundi Smart Overnight Return", "quantity": 9.0, "price": 1254.50, "value_gbp": 11290.5}
    p = plan([{"action": "BUY", "amount_gbp": 5613.69}, {"action": "BUY", "amount_gbp": 5613.69}, {"action": "HOLD"}],
             cash_stated_gbp=17970.94, mmf=mm, reserve_gbp=250.0, as_of="02-Oct-2026")
    # broker cash 6,680.44 - 250 = 6,430.44; need 11,227.38 -> shortfall 4,796.94 -> ceil((4,796.94+5)/1,254.50) = 4
    assert p["state"] == "SELL_CASH_FUND" and p["units_to_sell"] == 4 and p["proceeds_gbp"] >= p["shortfall_gbp"], \
        ("MUST-FIRE: the sale needed to fund the purchases is stated in whole units", p)
    assert "Sell 4 units" in p["line"], p["line"]
    p2 = plan([{"action": "BUY", "amount_gbp": 1000}], cash_stated_gbp=17970.94, mmf=mm, reserve_gbp=250.0)
    assert p2["state"] == "CASH_COVERS" and p2["units_to_sell"] == 0, ("NEGATIVE CONTROL: cash covers -> no sale", p2)
    p3 = plan([{"action": "BUY", "amount_gbp": "[Claude: amount_gbp]"}], cash_stated_gbp=1, mmf=mm, reserve_gbp=250)
    assert p3["state"] == "AMOUNT_NOT_STATED", ("MUST-FIRE: a missing amount is named, never guessed", p3)
    p4 = plan([{"action": "BUY", "amount_gbp": 30000}], cash_stated_gbp=17970.94, mmf=mm, reserve_gbp=250.0)
    assert p4["state"] == "INSUFFICIENT_CASH_FUND", ("MUST-FIRE: more than the cash fund holds is said so", p4)
    p5 = plan([{"action": "BUY", "amount_gbp": 9000}, {"action": "SELL", "amount_gbp": 3000}],
              cash_stated_gbp=17970.94, mmf=mm, reserve_gbp=250.0)
    assert p5["shortfall_gbp"] == round(9000 - 3000 - 6430.44, 2) or p5["state"] == "CASH_COVERS", p5
    assert plan([{"action": "HOLD"}], cash_stated_gbp=0, mmf=mm, reserve_gbp=250)["state"] == "NO_PURCHASE"
    p6 = plan([{"action": "BUY", "amount_gbp": 9000}], cash_stated_gbp=17970.94, mmf=dict(mm, price=None),
              reserve_gbp=250.0)
    assert p6["units_to_sell"] == 3, ("price derived from value / quantity when the price cell is absent", p6)
    print("mmf_funding selftest OK (units to sell, cash covers, missing amount, insufficient fund, sale proceeds, "
          "no purchase, derived price)")
    return 0


if __name__ == "__main__":
    import sys as _s
    if "--selftest" in _s.argv:
        _s.exit(_selftest())
