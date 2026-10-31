#!/usr/bin/env python3
"""held_action_permissions.py - S3 / B03-R02 + B03-R03 (24-Oct-2026, ISA-0885): THE ONE WRITTEN TABLE of what
is allowed for every held-position action under every kind of missing or late data.

Every held-position action (EXIT, TRIM, HOLD, ADD, RE_ENTRY, SUCCESSOR, SWITCH_SALE) is evaluated through ONE
declared precedence (B03-R02), highest first:
  1 IDENTITY_LEGAL_AUTHORITY  - identity resolved; wrapper eligibility / dealability; capital authority + control
  2 HARD_RISK_EMERGENCY       - an applicable hard-risk breach or emergency obligation (offer period, delisting)
  3 THESIS_EVENT_VALIDITY     - thesis broken / deteriorating / binary resolved / unpriceable by nature
  4 HOLDING_PERMISSIONS       - the canonical position-first-entry 182-day clock (B03-R01), minimum residual,
                                standing permissions (C-1 loss block lives ONLY inside the clock)
  5 ECONOMIC_COMPARISON       - a feasible comparison exists (complete case, resolved ER12) - the engine decides
  6 APPROVAL_EXECUTION        - current approval and execution checks
A higher layer that decides ends the walk; two facts at the SAME layer that disagree emit POLICY_CONFLICT
with its owner - never a silent pick.

Missingness is TYPED (B03-R03): MISSING_DATA, THESIS_DETERIORATION, INHERENTLY_UNPRICEABLE, TEMPORARY_DEFECT,
EXPIRED_REVIEW_AUTHORITY. REVIEW and DEFER are neither an endorsed HOLD nor an automatic SELL: each carries
the exposure, a deadline and an escalation.

Verdicts: PERMITTED (may be proposed; never commanded) | BLOCKED | REVIEW | DEFER | POLICY_CONFLICT.
This module DECIDES NOTHING ON ITS OWN AUTHORITY: it states what the framework's existing rules permit, in
one place, so the review, the lifecycle and the engine cannot disagree about it (R4.4).
"""
from __future__ import annotations
import datetime
import json
import math
import sys

ACTIONS = ("EXIT", "TRIM", "HOLD", "ADD", "RE_ENTRY", "SUCCESSOR", "SWITCH_SALE")
REDUCING = ("EXIT", "TRIM", "SWITCH_SALE")
ADDING = ("ADD", "RE_ENTRY", "SUCCESSOR")
PRECEDENCE = ("IDENTITY_LEGAL_AUTHORITY", "HARD_RISK_EMERGENCY", "THESIS_EVENT_VALIDITY",
              "HOLDING_PERMISSIONS", "ECONOMIC_COMPARISON", "APPROVAL_EXECUTION")
MISSINGNESS = ("MISSING_DATA", "THESIS_DETERIORATION", "INHERENTLY_UNPRICEABLE", "TEMPORARY_DEFECT",
               "EXPIRED_REVIEW_AUTHORITY")
PERMITTED, BLOCKED, REVIEW, DEFER, CONFLICT = "PERMITTED", "BLOCKED", "REVIEW", "DEFER", "POLICY_CONFLICT"
REVIEW_DEADLINE_DAYS = {"MISSING_DATA": 7, "TEMPORARY_DEFECT": 7, "THESIS_DETERIORATION": 14,
                        "INHERENTLY_UNPRICEABLE": 14, "EXPIRED_REVIEW_AUTHORITY": 0}
ESCALATION = {"MISSING_DATA": "next pre-run must supply the field or the name is escalated to the review's Step 5",
              "TEMPORARY_DEFECT": "owner fixes the defect before the next pre-run; else Step 5 decides on the evidence held",
              "THESIS_DETERIORATION": "Step 5 thesis review (intramonth review if an alert fired)",
              "INHERENTLY_UNPRICEABLE": "VCI lifecycle route (successor / Path-A / EXIT) - never a silent HOLD",
              "EXPIRED_REVIEW_AUTHORITY": "renew the case before any capital action; reductions remain permitted"}
OWNERS = {"IDENTITY_LEGAL_AUTHORITY": "ISA-0860 / ISA-0807", "HARD_RISK_EMERGENCY": "ISA-0817",
          "THESIS_EVENT_VALIDITY": "ISA-0716", "HOLDING_PERMISSIONS": "C-1 / ISA-0645",
          "ECONOMIC_COMPARISON": "ISA-0804 (Capital Decision Receipt)", "APPROVAL_EXECUTION": "ISA-0727"}


def _deadline(today, kind):
    try:
        d = datetime.date.fromisoformat(str(today)[:10])
    except ValueError:
        d = datetime.date.today()
    return (d + datetime.timedelta(days=REVIEW_DEADLINE_DAYS.get(kind, 7))).isoformat()


def _v(verdict, layer, why, **kw):
    out = {"verdict": verdict, "layer": layer, "owner": OWNERS.get(layer), "why": why}
    out.update(kw)
    return out


def evaluate(action, facts, today=None):
    """-> one verdict for `action` on a held position described by `facts` (missing keys are UNKNOWN).

    facts: identity_resolved, wrapper_eligible, dealable (bool|None); capital_authority ('AUTHORISED'|'REFUSED'|
    None); new_capital_blocked (bool|None); hard_risk_breach, emergency (bool|None, may be lists of signals);
    thesis_state ('INTACT'|'WATCH'|'BROKEN'|'RESOLVED'|None); unpriceable_by_nature (bool); data_state
    ('COMPLETE'|'MISSING'|'TEMPORARY_DEFECT'|None); review_authority_until (ISO date|None); position_clock
    ('INSIDE'|'EXPIRED'|'UNKNOWN'); in_profit (bool|None); min_residual_ok (bool|None); case_complete,
    er12_resolved (bool|None); approved (bool|None); exposure_gbp (float)."""
    if action not in ACTIONS:
        raise ValueError("undeclared action %r (declared: %s)" % (action, ", ".join(ACTIONS)))
    f = dict(facts or {})
    today = str(today or datetime.date.today().isoformat())[:10]
    exp = f.get("exposure_gbp")
    exp = round(float(exp), 2) if isinstance(exp, (int, float)) and math.isfinite(float(exp)) else None

    def review_(kind, layer, why, verdict=REVIEW):
        return _v(verdict, layer, why, missingness=kind, exposure_gbp=exp, deadline=_deadline(today, kind),
                  escalation=ESCALATION[kind], endorsed_hold=False, automatic_sale=False)

    # ── 1 IDENTITY / LEGAL / AUTHORITY
    L = PRECEDENCE[0]
    if f.get("identity_resolved") is False:
        return _v(BLOCKED, L, "identity unresolved - no action may be proposed on an unidentified instrument")
    if f.get("identity_resolved") is None:
        return review_("MISSING_DATA", L, "identity not established")
    if action in ADDING:
        if f.get("wrapper_eligible") is False or f.get("dealable") is False:
            return _v(BLOCKED, L, "not ISA-eligible / not dealable - positive exposure is refused")
        if f.get("new_capital_blocked") is True:
            return _v(BLOCKED, L, "the canonical new-capital control blocks positive new stock exposure (ISA-0807)")
        if f.get("capital_authority") == "REFUSED":
            return _v(BLOCKED, L, "capital authority REFUSED (untrusted LIVE or engine not LIVE)")
        if f.get("wrapper_eligible") is None or f.get("dealable") is None or f.get("capital_authority") is None:
            return review_("MISSING_DATA", L, "eligibility / dealability / capital authority not established", verdict=DEFER)
    # ── 2 HARD RISK / EMERGENCY - outranks the holding clock for reductions, blocks additions
    L = PRECEDENCE[1]
    hr, em = f.get("hard_risk_breach"), f.get("emergency")
    if hr is True or em is True:
        if action in REDUCING:
            return _v(PERMITTED, L, "an applicable hard-risk / emergency obligation outranks the holding clock "
                                    "(a permission to reduce, decided by the review)")
        if action in ADDING:
            return _v(BLOCKED, L, "hard-risk breach / emergency: no positive exposure")
        return review_("THESIS_DETERIORATION", L, "hard-risk / emergency signal: HOLD is not endorsed without review")
    # ── 3 THESIS / EVENT VALIDITY
    L = PRECEDENCE[2]
    ts = f.get("thesis_state")
    signals = [s for s in (ts if isinstance(ts, list) else [ts]) if s is not None]
    if len(set(signals) & {"INTACT"}) and len(set(signals) & {"BROKEN"}):
        return _v(CONFLICT, L, "two thesis readings at the same layer disagree (INTACT and BROKEN) - the owner resolves",
                  conflicting=sorted(set(signals)))
    ts = signals[0] if signals else None
    if f.get("unpriceable_by_nature"):
        if action in ADDING and action != "SUCCESSOR":
            return _v(BLOCKED, L, "inherently unpriceable: no addition (the successor route is the VCI lifecycle's)")
        return review_("INHERENTLY_UNPRICEABLE", L, "inherently unpriceable by nature - routed to the lifecycle")
    if ts == "BROKEN":
        if action in ADDING:
            return _v(BLOCKED, L, "thesis BROKEN: no addition")
        if action == "HOLD":
            return review_("THESIS_DETERIORATION", L, "thesis BROKEN: HOLD is not endorsed")
        # a thesis exit is a THESIS event: C-1 (anti-churn) does not bind it, the clock check below is skipped
        return _v(PERMITTED, L, "thesis BROKEN: a thesis exit is permitted (thesis event, not noise - D31)")
    if ts == "WATCH" and action in ADDING:
        return review_("THESIS_DETERIORATION", L, "thesis on WATCH: an addition waits for the review", verdict=DEFER)
    if ts is None:
        if action in ADDING:
            return review_("MISSING_DATA", L, "no current thesis state", verdict=DEFER)
        if action == "HOLD":
            return review_("MISSING_DATA", L, "no current thesis state: HOLD is not endorsed")
    # data / authority freshness (typed, B03-R03)
    ds = f.get("data_state")
    ra = f.get("review_authority_until")
    if ra and today > str(ra)[:10]:
        if action in ADDING or action == "HOLD":
            return review_("EXPIRED_REVIEW_AUTHORITY", L, "the case's review authority expired on %s" % ra,
                           verdict=DEFER if action in ADDING else REVIEW)
    if ds in ("MISSING", "TEMPORARY_DEFECT") and action in ADDING:
        return review_("MISSING_DATA" if ds == "MISSING" else "TEMPORARY_DEFECT", L,
                       "decision data %s" % ds, verdict=DEFER)
    # ── 4 HOLDING PERMISSIONS (B03-R01: clock BEFORE the ordinary loss restriction)
    L = PRECEDENCE[3]
    clk = f.get("position_clock")
    if action in REDUCING:
        if clk == "INSIDE":
            if f.get("in_profit") is False and action == "EXIT":
                return _v(BLOCKED, L, "C-1: a full exit at a loss inside the 182-day window is blocked")
            if action == "SWITCH_SALE":
                return _v(BLOCKED, L, "switch sales are opportunity sales: not inside the 182-day min-hold (ISA-0621)")
            if f.get("in_profit") is None:
                return review_("MISSING_DATA", L, "inside the window and the gain/loss is unknown - C-1 cannot be evaluated",
                               verdict=DEFER)
        elif clk == "EXPIRED":
            pass        # expiry PERMITS an otherwise authorised action; it commands nothing
        else:
            return review_("MISSING_DATA", L, "the position clock is UNKNOWN - a loss block cannot be lifted on it",
                           verdict=DEFER)
        if action == "TRIM" and f.get("min_residual_ok") is False:
            return _v(BLOCKED, L, "a trim would leave less than the minimum meaningful residual - EXIT or HOLD")
    # ── 5 ECONOMIC COMPARISON - the engine/review decides; this layer states whether it CAN
    L = PRECEDENCE[4]
    if action in ADDING or action == "SWITCH_SALE":
        if f.get("case_complete") is not True or f.get("er12_resolved") is not True:
            return review_("MISSING_DATA", L, "no feasible economic comparison yet (case incomplete / ER12 unresolved)",
                           verdict=DEFER)
    # ── 6 APPROVAL / EXECUTION
    L = PRECEDENCE[5]
    if action != "HOLD" and f.get("approved") is not True:
        return _v(PERMITTED, L, "permitted to be PROPOSED; executes only on the review's approval and execution checks",
                  requires_approval=True)
    return _v(PERMITTED, L, "permitted")


def evaluate_all(facts, today=None, actions=ACTIONS):
    return {a: evaluate(a, facts, today=today) for a in actions}


def table():
    """The declared table as data (rendered into Run_Context / the email appendix)."""
    return {"precedence": list(PRECEDENCE), "actions": list(ACTIONS), "missingness": list(MISSINGNESS),
            "verdicts": [PERMITTED, BLOCKED, REVIEW, DEFER, CONFLICT],
            "review_deadline_days": REVIEW_DEADLINE_DAYS, "escalation": ESCALATION, "owners": OWNERS,
            "doctrine": "REVIEW/DEFER is neither an endorsed HOLD nor an automatic SELL; PERMITTED is a "
                        "permission to propose, never an instruction; clock expiry permits, never commands."}


def _selftest():
    fails = []

    def ok(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)[:200]) if (detail and not cond) else ""))
        if not cond:
            fails.append(name)
    T = "2026-10-24"
    base = {"identity_resolved": True, "wrapper_eligible": True, "dealable": True, "capital_authority": "AUTHORISED",
            "new_capital_blocked": False, "hard_risk_breach": False, "emergency": False, "thesis_state": "INTACT",
            "data_state": "COMPLETE", "position_clock": "EXPIRED", "in_profit": True, "min_residual_ok": True,
            "case_complete": True, "er12_resolved": True, "exposure_gbp": 5000.0}
    ok("positive control: a healthy held name may propose an ADD (approval still required)",
       evaluate("ADD", base, T)["verdict"] == PERMITTED and evaluate("ADD", base, T)["requires_approval"])
    ok("B03-R01: after clock expiry a loss EXIT is PERMITTED (not commanded)",
       evaluate("EXIT", dict(base, in_profit=False), T)["verdict"] == PERMITTED)
    ok("negative control: inside the window a loss EXIT must stay BLOCKED (C-1)",
       evaluate("EXIT", dict(base, in_profit=False, position_clock="INSIDE"), T)["verdict"] == BLOCKED)
    ok("negative control: an UNKNOWN clock must not lift the loss block",
       evaluate("EXIT", dict(base, in_profit=False, position_clock="UNKNOWN"), T)["verdict"] == DEFER)
    ok("B03-R02: a hard-risk / emergency obligation outranks the clock for a reduction",
       evaluate("EXIT", dict(base, in_profit=False, position_clock="INSIDE", emergency=True), T)["verdict"] == PERMITTED)
    ok("B03-R02: a broken thesis exit is a thesis event (not blocked by C-1)",
       evaluate("EXIT", dict(base, in_profit=False, position_clock="INSIDE", thesis_state="BROKEN"), T)["verdict"] == PERMITTED)
    r = evaluate("HOLD", dict(base, thesis_state=None), T)
    ok("B03-R03: missing thesis -> REVIEW that is NOT an endorsed hold, with exposure, deadline and escalation",
       r["verdict"] == REVIEW and r["endorsed_hold"] is False and r["automatic_sale"] is False and r["deadline"] == "2026-10-31"
       and r["exposure_gbp"] == 5000.0 and r["escalation"], r)
    ok("negative control: REVIEW must never read as a SELL", evaluate("HOLD", dict(base, thesis_state=None), T)["verdict"] != PERMITTED)
    c = evaluate("EXIT", dict(base, thesis_state=["INTACT", "BROKEN"]), T)
    ok("B03-R02: same-layer disagreement emits POLICY_CONFLICT with an owner", c["verdict"] == CONFLICT and c["owner"], c)
    ok("negative control: the new-capital control must block ADD and RE_ENTRY",
       all(evaluate(a, dict(base, new_capital_blocked=True), T)["verdict"] == BLOCKED for a in ("ADD", "RE_ENTRY")))
    ok("negative control: a switch sale inside the min-hold must be BLOCKED (ISA-0621)",
       evaluate("SWITCH_SALE", dict(base, position_clock="INSIDE"), T)["verdict"] == BLOCKED)
    ok("an expired review authority DEFERS an addition but leaves reductions permitted",
       evaluate("ADD", dict(base, review_authority_until="2026-10-01"), T)["verdict"] == DEFER
       and evaluate("TRIM", dict(base, review_authority_until="2026-10-01"), T)["verdict"] == PERMITTED)
    ok("unpriceable by nature routes to the lifecycle (typed), never a silent HOLD",
       evaluate("HOLD", dict(base, unpriceable_by_nature=True), T)["missingness"] == "INHERENTLY_UNPRICEABLE")
    ok("negative control: a trim below the minimum residual must be BLOCKED",
       evaluate("TRIM", dict(base, min_residual_ok=False), T)["verdict"] == BLOCKED)
    ok("missing eligibility DEFERS an addition (typed MISSING_DATA)",
       evaluate("ADD", dict(base, dealable=None), T)["missingness"] == "MISSING_DATA")
    try:
        evaluate("BUY_MORE", base, T)
        ok("negative control: an undeclared action must be refused", False)
    except ValueError:
        ok("negative control: an undeclared action must be refused", True)
    ok("every action evaluates on empty facts without raising (unknowns typed)",
       all(v["verdict"] in (PERMITTED, BLOCKED, REVIEW, DEFER, CONFLICT) for v in evaluate_all({}, T).values()))
    print("held_action_permissions selftest: %d FAIL(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(table(), indent=1))
