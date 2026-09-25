#!/usr/bin/env python3
"""
sleeve_membership.py — ISA-0685: a holding is not an admission.

Authority: ISA_Engineering_Rules.md (R2.10, R4.3, R4.8, R5.1, R20.1);
ChatGPT Astra 6 Audit/ISA_Wave2_Consolidated_BuildSpec_19Sep2026.md §5.6, §10 (Membership),
§13 challenge 16, §14, §15.1; ISA_BuildSpec_ISA0701_ExecutionOwned_Fill_Obligations_16Sep2026.md
§7 mandatory negative 6.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
The sleeve had **no membership contract binding a holding to an admission decision**. "Held and
in the registry" WAS the membership test, so a name could acquire new-capital rights — including
a D17 first claim on the next tranche — purely by being owned. A legacy position, a position
inherited from a retired route, or a position bought outside the framework was indistinguishable
from one the framework decided to admit.

⚑ ISA-0685 WAS BLOCKED ON ISA-0686 AND IS NOT ANY MORE. The item said so in its own words:
*"until VCI deploy decisions are captured into the ledger there is no reliable place to ASK
whether a name was admitted, so the contract has nothing to read."* ISA-0686 closed 20-Sep-2026;
`decision_ledger.current_decision(ticker, route)` is that place, and it returns None for a name
the framework never decided — which is exactly the ADMITTED_UNDECIDED test.

⚑ ADMITTED_UNDECIDED IS NOT A PENALTY AND IT IS NOT AN EXIT SIGNAL. The position stays
**visible**, **risk-counted** and **reportable**; it is sizeable and it is reviewed like any
other holding. What it may NOT do is create a fill obligation or hold new-capital priority,
because a top-up is a NEW capital decision and an unmade decision cannot confer one.

⚑ THE STATE IS NEVER INFERRED FROM `held`. That inference IS the defect.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["sleeve_membership"] = False` — `classify()` returns
`UNKNOWN_DISABLED`, which is treated as NOT admitted for obligation purposes and never as
ADMITTED_DECIDED (R4.3: the safe direction is the refusing one).
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Optional

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"

# ── §10 canonical membership states ─────────────────────────────────────────────────────
ADMITTED_DECIDED = "ADMITTED_DECIDED"
ADMITTED_UNDECIDED = "ADMITTED_UNDECIDED"
NOT_HELD = "NOT_HELD"
UNKNOWN_DISABLED = "UNKNOWN_DISABLED"
# ⚑ ISA-0716 (23-Sep-2026): a HELD name whose MOST RECENT current decision (any route) is an
#   EXIT. It is decided, so it must not read "undecided — NOT a sell signal"; it is visible,
#   risk-counted and reportable, and it may neither create a fill obligation nor hold
#   new-capital priority. Executing the exit is the sale-permission route's job, not this one.
EXIT_DECIDED = "EXIT_DECIDED"
EXIT_DECISIONS = ("sell",)
MEMBERSHIP_STATES = (ADMITTED_DECIDED, ADMITTED_UNDECIDED, EXIT_DECIDED, NOT_HELD,
                     UNKNOWN_DISABLED)

# An admission is a decision to OWN the name. A PASS is the road not taken and admits nothing;
# a sell decides the opposite. `hold` is deliberately included: a recorded decision to keep a
# position is a decision about owning it, which is what admission means.
ADMITTING_DECISIONS = ("buy", "top_up", "hold")

# Routes searched for an admission, in the order the framework issues them. A name may be
# admitted by any route it was actually decided on — restricting the search to one route would
# re-create the defect for every name that changed route.
ADMISSION_ROUTES = ("vci", "growth", "growth_stock", "held_sleeve", "portfolio")


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.V2_FLAGS.get("sleeve_membership", True))
    except Exception:                                                   # noqa: BLE001
        return True


def classify(ticker, *, held, ledger_path=None, routes=ADMISSION_ROUTES,
             as_of=None) -> dict:
    """The membership verdict for ONE name. The ONE home (R4.4).

    `held` is BROKER TRUTH — whether the position exists. It is an input, never the answer.
    """
    _fi_mark("sleeve_membership", "classify")
    t = str(ticker or "").upper()
    if not _flag():
        return {"ticker": t, "state": UNKNOWN_DISABLED, "held": bool(held),
                "admission": None,
                "visible": True, "risk_counted": True, "reportable": True,
                "may_generate_fill_obligation": False, "may_hold_new_capital_priority": False,
                "why": ("isa_policy.V2_FLAGS['sleeve_membership'] is False. DISABLED is treated "
                        "as NOT admitted for obligation purposes and never as ADMITTED_DECIDED "
                        "(R4.3 — the safe direction is the refusing one).")}
    if not held:
        return {"ticker": t, "state": NOT_HELD, "held": False, "admission": None,
                "visible": False, "risk_counted": False, "reportable": True,
                "may_generate_fill_obligation": False, "may_hold_new_capital_priority": False,
                "why": ("not held. A claim needs a position (ISA-0701): an obligation for a "
                        "name that is not owned is a first claim on nothing.")}
    try:
        import decision_ledger as _dl
        path = ledger_path or _dl.default_path(HERE)
    except Exception as exc:                                            # noqa: BLE001
        return {"ticker": t, "state": UNKNOWN_DISABLED, "held": True, "admission": None,
                "visible": True, "risk_counted": True, "reportable": True,
                "may_generate_fill_obligation": False, "may_hold_new_capital_priority": False,
                "why": ("decision_ledger unavailable (%s) — the admission question could not be "
                        "ASKED, which is UNKNOWN and blocks, never a pass (R2.9/R4.3)." % exc)}
    found = []
    for r in routes:
        cur = _dl.current_decision(path, t, r)
        if not cur:
            continue
        if str(cur.get("decision") or "").lower() not in ADMITTING_DECISIONS:
            found.append({"route": r, "decision": cur.get("decision"),
                          "decision_id": cur.get("decision_id") or cur.get("_id"),
                          "date": cur.get("date"), "admits": False})
            continue
        found.append({"route": r, "decision": cur.get("decision"),
                      "decision_id": cur.get("decision_id") or cur.get("_id"),
                      "date": cur.get("date"), "admits": True})
    admitting = [f for f in found if f["admits"]]
    # ISA-0716 — the LATEST current decision across routes wins when it is an exit. An older
    #   admitting row on another route cannot keep a name admitted after the framework decided
    #   to leave it (the lifecycle engine supersedes those rows; this is the reader's guard).
    if found:
        latest = sorted(found, key=lambda f: str(f.get("date") or ""))[-1]
        if str(latest.get("decision") or "").lower() in EXIT_DECISIONS:
            return {"ticker": t, "state": EXIT_DECIDED, "held": True, "admission": None,
                    "exit_decision": latest, "all_decisions": found,
                    "visible": True, "risk_counted": True, "reportable": True,
                    "may_generate_fill_obligation": False,
                    "may_hold_new_capital_priority": False,
                    "why": ("HELD with a current EXIT decision (%s on the %s route, %s, %s). It "
                            "stays visible, risk-counted and reportable until sold; it may not "
                            "claim new capital or a fill obligation. When the sale may execute "
                            "is decided by the min-hold / sale-permission route, not here."
                            % (latest["decision"], latest["route"], latest["decision_id"],
                               latest["date"]))}
    if admitting:
        best = sorted(admitting, key=lambda f: str(f.get("date") or ""))[-1]
        return {"ticker": t, "state": ADMITTED_DECIDED, "held": True, "admission": best,
                "all_decisions": found,
                "visible": True, "risk_counted": True, "reportable": True,
                "may_generate_fill_obligation": True, "may_hold_new_capital_priority": True,
                "why": ("admitted by a current, non-superseded %s decision on the %s route "
                        "(%s, %s)" % (best["decision"], best["route"], best["decision_id"],
                                      best["date"]))}
    return {"ticker": t, "state": ADMITTED_UNDECIDED, "held": True, "admission": None,
            "all_decisions": found,
            # ⚑ Visible, risk-counted and reportable. This is not a penalty and not an exit.
            "visible": True, "risk_counted": True, "reportable": True,
            "may_generate_fill_obligation": False, "may_hold_new_capital_priority": False,
            "why": ("HELD with no current admitting decision on any route (%s). The position is "
                    "visible, risk-counted and reportable and is reviewed like any other "
                    "holding — but a top-up is a NEW capital decision, and an unmade decision "
                    "cannot confer a first claim or new-capital priority (ISA-0685). It is NOT "
                    "a sell signal."
                    % (", ".join("%s=%s" % (f["route"], f["decision"]) for f in found)
                       or "no decision on any searched route"))}


def population(portfolio_doc, *, ledger_path=None, routes=ADMISSION_ROUTES) -> dict:
    """Every held direct stock, classified. N expected -> N classified, denominated by the
    BROKER BOOK (the same rule ISA-0580 taught and ISA-0713 applies)."""
    rows = (portfolio_doc or {}).get("stocks") or []
    out, by_state = [], {}
    for s in rows:
        t = str(s.get("ticker") or "").upper()
        if not t:
            continue
        m = classify(t, held=True, ledger_path=ledger_path, routes=routes)
        m["value_gbp"] = s.get("value_gbp")
        out.append(m)
        by_state.setdefault(m["state"], []).append(t)
    undec = by_state.get(ADMITTED_UNDECIDED) or []
    # ⚑ Named `undecided_gbp`, not `gbp` (ISA-0685, 20-Sep-2026). framework_integrity Q1
    #   flagged a local called `gbp` as a possible SECOND HOME for the declared
    #   stock_return_store quantity of that name. It is NOT that quantity - it is the market
    #   value of the ADMITTED_UNDECIDED holdings - so the collision is removed AT SOURCE
    #   rather than annotated away with a register disposition. R4.4/R4.5 precedent: one
    #   home, and a name that cannot be mistaken for another home. The PUBLISHED key
    #   (`admitted_undecided_gbp`) is unchanged, so no consumer moves.
    undecided_gbp = round(sum(float(m.get("value_gbp") or 0.0) for m in out
                              if m["state"] == ADMITTED_UNDECIDED), 2)
    exiting = sorted(by_state.get(EXIT_DECIDED) or [])
    exiting_value = round(sum(float(m.get("value_gbp") or 0.0) for m in out
                              if m["state"] == EXIT_DECIDED), 2)
    return {"schema_version": SCHEMA_VERSION,
            "n_expected": len(out), "n_classified": len(out),
            "rows": out, "by_state": by_state,
            "admitted_undecided": sorted(undec),
            "admitted_undecided_gbp": undecided_gbp,
            "exit_decided": exiting,
            "exit_decided_value_gbp": exiting_value,
            "complete": True,
            "basis": ("ISA-0685: membership is read from the canonical decision ledger, never "
                      "inferred from `held`. The denominator is the broker book."),
            "why": ("%d held direct stock(s): %s"
                    % (len(out), ", ".join("%s %s" % (k, len(v)) for k, v in sorted(by_state.items()))))}


def may_activate_obligation(membership: dict) -> dict:
    """R5.1 at the obligation boundary — ISA-0701 mandatory negative 6."""
    st = (membership or {}).get("state")
    ok = bool((membership or {}).get("may_generate_fill_obligation"))
    return {"allowed": ok, "state": st,
            "why": ("admitted by a recorded decision" if ok else
                    "ISA-0685: %s may not create a fill obligation — %s"
                    % (st, (membership or {}).get("why", "no membership verdict")))}


# ═════════════════════════════════════════════════════════════════════════════════════════
# SELFTEST
# ═════════════════════════════════════════════════════════════════════════════════════════

def _selftest(verbose: bool = True) -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:220]) if not cond else ""))
        if not cond:
            fails.append(name)

    import decision_ledger as dl
    td = tempfile.mkdtemp()
    lp = os.path.join(td, "ledger.json")
    dl.log_decision(lp, "ADM", "growth", "buy", date="2026-09-01")
    dl.log_decision(lp, "PASSED", "vci", "PASS", date="2026-09-13")
    dl.log_decision(lp, "SOLD", "growth", "sell", date="2026-09-01")
    _old = dl.log_decision(lp, "SUP", "vci", "buy", date="2026-09-01",
                           input_snapshot_id="a@1")
    dl.log_decision(lp, "SUP", "vci", "PASS", date="2026-09-13", input_snapshot_id="b@2",
                    supersedes_decision_id=_old["decision_id"])

    m = classify("ADM", held=True, ledger_path=lp)
    ok("a held name with a current BUY decision is ADMITTED_DECIDED and names the decision",
       m["state"] == ADMITTED_DECIDED and m["admission"]["decision"] == "buy"
       and m["may_generate_fill_obligation"] and m["may_hold_new_capital_priority"], m)

    m2 = classify("PASSED", held=True, ledger_path=lp)
    ok("⚑ MUST-FIRE: a held name whose only current decision is a PASS is ADMITTED_UNDECIDED. "
       "A PASS is the road not taken — it admits nothing, and this is the real ABCL shape",
       m2["state"] == ADMITTED_UNDECIDED
       and m2["may_generate_fill_obligation"] is False
       and m2["may_hold_new_capital_priority"] is False, m2)
    ok("⚑ NEGATIVE CONTROL: ADMITTED_UNDECIDED is NOT an exit and NOT a penalty — the position "
       "stays visible, risk-counted and reportable, and nothing in the verdict says sell",
       m2["visible"] and m2["risk_counted"] and m2["reportable"]
       and "SELL" not in json.dumps({k: v for k, v in m2.items() if k != "why"}).upper(), m2)

    ok("⚑ MUST-FIRE: a held name with NO decision at all is ADMITTED_UNDECIDED, never admitted "
       "by default — inferring admission from `held` IS the defect (ISA-0685)",
       classify("LEGACY", held=True, ledger_path=lp)["state"] == ADMITTED_UNDECIDED)
    _sold = classify("SOLD", held=True, ledger_path=lp)
    ok("⚑ MUST-FIRE ISA-0716: a SELL decision does not admit — it decides the opposite, and the "
       "verdict SAYS so (EXIT_DECIDED), rather than reading 'undecided — NOT a sell signal'",
       _sold["state"] == EXIT_DECIDED and not _sold["may_generate_fill_obligation"]
       and not _sold["may_hold_new_capital_priority"] and _sold["risk_counted"]
       and _sold["visible"], _sold)
    import tempfile as _tf2
    _lp2 = os.path.join(_tf2.mkdtemp(), "decision_ledger.json")
    dl.log_decision(_lp2, "MIX", "growth", "buy", date="2026-08-01")
    dl.log_decision(_lp2, "MIX", "vci", "sell", date="2026-09-01")
    dl.log_decision(_lp2, "BACK", "vci", "sell", date="2026-08-01")
    dl.log_decision(_lp2, "BACK", "growth", "buy", date="2026-09-01")
    ok("⚑ MUST-FIRE ISA-0716: an OLDER admitting row on another route cannot keep a name "
       "admitted after a NEWER exit decision",
       classify("MIX", held=True, ledger_path=_lp2)["state"] == EXIT_DECIDED)
    ok("NEGATIVE CONTROL ISA-0716: a NEWER admitting decision after an old exit re-admits — "
       "the rule is 'latest decision wins', not 'any sell ever'",
       classify("BACK", held=True, ledger_path=_lp2)["state"] == ADMITTED_DECIDED)
    ok("⚑ MUST-FIRE: a SUPERSEDED buy does not admit. The successor decision is a PASS, and a "
       "stale decision cannot confer a claim (ISA-0686 supersession, consumed here)",
       classify("SUP", held=True, ledger_path=lp)["state"] == ADMITTED_UNDECIDED,
       classify("SUP", held=True, ledger_path=lp))
    ok("NEGATIVE CONTROL: a name that is NOT held is NOT_HELD and can hold no claim — an "
       "obligation for a position nobody owns is a first claim on nothing",
       classify("ADM", held=False, ledger_path=lp)["state"] == NOT_HELD
       and not classify("ADM", held=False, ledger_path=lp)["may_generate_fill_obligation"])

    p = population({"stocks": [{"ticker": "ADM", "value_gbp": 100.0},
                               {"ticker": "PASSED", "value_gbp": 250.0}]}, ledger_path=lp)
    ok("population reconciles N expected -> N classified on the BROKER BOOK, and names the "
       "undecided holdings with their GBP",
       p["n_expected"] == 2 == p["n_classified"] and p["admitted_undecided"] == ["PASSED"]
       and p["admitted_undecided_gbp"] == 250.0, p)

    ok("⚑ MUST-FIRE at the obligation boundary: ADMITTED_UNDECIDED may not activate a claim",
       may_activate_obligation(m2)["allowed"] is False
       and "ISA-0685" in may_activate_obligation(m2)["why"])
    ok("POSITIVE CONTROL: ADMITTED_DECIDED may — a contract that can only refuse is not a "
       "contract", may_activate_obligation(m)["allowed"] is True)
    ok("⚑ NEGATIVE CONTROL: an UNKNOWN verdict is treated as NOT admitted and never as "
       "admitted — not being able to ask is not the same as a yes (R4.3)",
       may_activate_obligation({"state": UNKNOWN_DISABLED,
                                "may_generate_fill_obligation": False})["allowed"] is False)
    ok("an unreadable ledger yields UNKNOWN_DISABLED and blocks, rather than passing",
       classify("X", held=True, ledger_path=os.path.join(td, "nope.json"))["state"]
       in (ADMITTED_UNDECIDED, UNKNOWN_DISABLED))

    if verbose:
        print("\nsleeve_membership selftest: %d FAIL(s)%s"
              % (len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    try:
        with open(os.path.join(HERE, argv[0]), encoding="utf-8") as fh:
            pf = json.load(fh)
    except Exception:                                                   # noqa: BLE001
        print(json.dumps({"states": MEMBERSHIP_STATES, "routes": ADMISSION_ROUTES,
                          "admitting": ADMITTING_DECISIONS}, indent=2))
        return 0
    print(json.dumps(population(pf), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
