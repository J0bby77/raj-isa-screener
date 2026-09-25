#!/usr/bin/env python3
"""
vci_lifecycle.py — ISA-0716. THE ONE post-event (graduation / successor) lifecycle engine.

Authority: ChatGPT Astra 6 Audit/ISA_Wave2_Consolidated_BuildSpec_19Sep2026.md §5.8, §5.9, §10
("VCI lifecycle"), §15.1 "Graduation"; Raj decisions 3–6 carried by that BuildSpec (§18);
ISA_Engineering_Rules.md R4.4, R4.5, R4.14, R5.10, R16.1, R20.1.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
Before 23-Sep-2026 the post-event question was answered three ways for one name:
  * the monthly pre-run's held review computed `retention.graduation_disposition` = SELL for
    ABCL and published it only as a Step 6.5 warning;
  * the VCI task decided "graduation" in Run_Context prose and captured a 13-Sep PASS;
  * an 09-Aug VCI top_up stayed un-superseded in the ledger;
and the membership contract read "ADMITTED_UNDECIDED — NOT a sell signal". Three answers, and
the one the framework computed (SELL) decided nothing (R4.14). The pre-run could not even reach
KEEP: it passed a forward case with no E[r] and no hurdle.

⚑ "GRADUATED" IS A TRIGGER, NOT A DISPOSITION (§5.8). When the investment-defining milestone has
resolved, the old event-specific authority EXPIRES, and in the SAME invocation this engine runs:

    A. SUCCESSOR VCI  — a genuine successor event, underwritten AFRESH (p_thesis, L, asymmetry vs
                        floor, budget-derived size >= MIN_ENTRY). "Another catalyst exists" is
                        not sufficient: the new economics must positively authorise capital.
    B. PATH A         — only where Path A is DECLARED applicable and fresh Path-A inputs exist
                        (E[r] vs hurdle, Source Score, method id). Old VCI values confer nothing.
    C. EXIT           — if neither route positively authorises ownership, target = 0 through the
                        existing sale-permission route.

⚑ NO GRACE STATE. If A or B cannot be ESTABLISHED in the same run (data/research missing), the
old authority is NOT revived: the route is EXIT with reason AUTHORITY_EXPIRED_UNESTABLISHED and
the blocking items named. That is an authority-expiry action, not a claim that the unknown
economics are negative (Raj decision 3). The D5 by-nature/by-defect split survives where it
belongs — in EXECUTABILITY: an exit forced by a defect does not unlock the thesis_realised
min-hold exemption, so inside 182 days it is SELL_PENDING_MIN_HOLD, and the defect escalates.

⚑ ROUTE AUTHORITY AND EXECUTABILITY ARE DIFFERENT FIELDS. The 182-day clock, the entry P&L and
the loss block decide WHEN a decided exit may execute (`retention.exit_executability`); they
never change WHICH route owns the position (§15.1: "inside 182 days / +50% / -20% must not
change route-authority semantics").

⚑ ONE ENGINE, THREE CALLERS (§5.9). The monthly pre-run (held_position_review), the VCI run
(vci_run_capture.write) and the intramonth review (CLI `--observe intramonth`) all reach
`held_position_review.review`, which calls `assess` + `record` here. The first workflow to observe
a resolution writes the canonical decision; a later workflow with UNCHANGED admissible inputs
REVALIDATES the same decision id; changed inputs SUPERSEDE it with an attributable delta.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["vci_lifecycle"] = False` — `assess` returns
UNKNOWN_DISABLED and `record` writes nothing. That is UNKNOWN, never ACTIVE (R4.3); the held
review then publishes the lifecycle as UNEVALUATED for every resolved binary.
"""
from __future__ import annotations

import datetime
import hashlib
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

# ── §10 lifecycle states. GRADUATION_TRIGGERED is internal and never a final state. ────────
ACTIVE_CURRENT_VCI = "ACTIVE_CURRENT_VCI"
GRADUATION_TRIGGERED = "GRADUATION_TRIGGERED"
SUCCESSOR_VCI = "SUCCESSOR_VCI"
PATH_A_RECLASSIFIED = "PATH_A_RECLASSIFIED"
EXIT = "EXIT"
UNKNOWN_DISABLED = "UNKNOWN_DISABLED"
FINAL_STATES = (ACTIVE_CURRENT_VCI, SUCCESSOR_VCI, PATH_A_RECLASSIFIED, EXIT)

# per-route outcomes
AUTHORISED = "AUTHORISED"
FAILS_ECONOMICS = "FAILS_ECONOMICS"
UNPRICEABLE = "UNPRICEABLE_BY_NATURE"
UNESTABLISHED = "UNESTABLISHED"
NONE = "NONE"
NOT_APPLICABLE = "NOT_APPLICABLE"

OBSERVERS = ("monthly_prerun", "vci_run", "intramonth", "selftest")
LIFECYCLE_FLAG = "ISA0716_LIFECYCLE"
LIFECYCLE_ROUTES = ("vci", "growth", "growth_stock", "held_sleeve", "portfolio")


class LifecycleRefused(RuntimeError):
    """Raised when the engine is asked to decide without the fact it needs (R4.3/R4.7)."""


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.flag("vci_lifecycle"))
    except Exception:                                                   # noqa: BLE001
        return True


def _today() -> str:
    return datetime.date.today().isoformat()


def _sha12(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ROUTE A — SUCCESSOR VCI
# ═══════════════════════════════════════════════════════════════════════════════════════════

SUCCESSOR_REQUIRED = ("p_thesis", "L", "fv_asymmetry", "fv_floor", "as_of")


def _route_successor(declared: dict, successor: Optional[dict], sizing: dict, held: dict) -> dict:
    succ = dict(successor if successor is not None else (declared.get("successor") or {}))
    out = {"route": SUCCESSOR_VCI, "inputs": {k: succ.get(k) for k in
                                              ("state", "type", "date", "priceable",
                                               "refusal_kind", "p_thesis", "L",
                                               "fv_asymmetry", "fv_floor", "as_of")}}
    if not succ or str(succ.get("state") or "NONE").upper() in ("NONE", "NO_SUCCESSOR"):
        out.update(outcome=NONE, why="no successor event is declared for this name")
        return out
    if not succ.get("priceable"):
        kind = succ.get("refusal_kind")
        if kind == "UNPRICEABLE_BY_NATURE":
            out.update(outcome=UNPRICEABLE, why=("a successor (%s) exists but is UNPRICEABLE BY "
                                                 "NATURE — %s. Existence of another catalyst is "
                                                 "not an authorisation (§5.8)."
                                                 % (succ.get("type") or "unnamed",
                                                    (succ.get("note") or "no underwriting")[:220])))
        else:
            out.update(outcome=UNESTABLISHED, blocking_items=list(succ.get("blocking_items") or []),
                       why=("the successor (%s) could not be underwritten in this run (%s)%s"
                            % (succ.get("type") or "unnamed", kind or "no refusal kind declared",
                               ("; blocking: " + ", ".join(succ.get("blocking_items")))
                               if succ.get("blocking_items") else "")))
        return out
    missing = [k for k in SUCCESSOR_REQUIRED if succ.get(k) is None]
    if missing:
        out.update(outcome=UNESTABLISHED,
                   why="the successor is declared priceable but is missing %s — a fresh "
                       "underwriting needs every one (R4.3)" % ", ".join(missing))
        return out
    resolved_on = str(declared.get("catalyst_date") or "")
    if resolved_on and str(succ.get("as_of")) < resolved_on:
        out.update(outcome=UNESTABLISHED,
                   why="the successor underwriting (as_of %s) predates the resolution (%s) — it "
                       "is the OLD case, not a fresh one (§5.8)" % (succ.get("as_of"), resolved_on))
        return out
    asym, floor = _num(succ.get("fv_asymmetry")), _num(succ.get("fv_floor"))
    if asym < floor:
        out.update(outcome=FAILS_ECONOMICS,
                   why="successor asymmetry %.2fx is below its floor %.2fx — the new economics "
                       "do not authorise capital" % (asym, floor))
        return out
    if sizing.get("budget_available_pct") is None or not sizing.get("evidence_state"):
        out.update(outcome=UNESTABLISHED,
                   why="the successor is priceable but the VCI budget (%s) or evidence state "
                       "(%s) is unavailable, so no size can be derived"
                       % (sizing.get("budget_available_pct"), sizing.get("evidence_state")))
        return out
    import position_sizing as _ps
    sz = _ps.vci_size_pct(p_thesis=_num(succ["p_thesis"]), L=_num(succ["L"]),
                          budget_available_pct=sizing["budget_available_pct"],
                          evidence_state=sizing["evidence_state"])
    nav = _num(sizing.get("nav_gbp"))
    target = (sz["w_vci_pct"] / 100.0) * nav if nav else None
    ceil = _num(sizing.get("risk_ceiling_gbp"))
    if target is not None and ceil is not None:
        target = min(target, ceil)
    min_entry = _num(sizing.get("min_entry_gbp"))
    out["sizing"] = dict(sz, target_gbp=None if target is None else round(target, 2),
                         budget_calc_id=sizing.get("budget_calc_id"))
    if target is None or min_entry is None:
        out.update(outcome=UNESTABLISHED, why="NAV or MIN_ENTRY unavailable — no target in GBP")
        return out
    if target < min_entry:
        out.update(outcome=FAILS_ECONOMICS,
                   why="the successor sizes to GBP %.0f, below MIN_ENTRY GBP %.0f — no size is "
                       "both material and budget-acceptable (D27)" % (target, min_entry))
        return out
    out.update(outcome=AUTHORISED, target_gbp=round(target, 2),
               why="fresh successor underwriting authorises GBP %.0f (asymmetry %.2fx vs floor "
                   "%.2fx; binding %s)" % (target, asym, floor, sz.get("binding_constraint")))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ROUTE B — PATH A (only where genuinely applicable)
# ═══════════════════════════════════════════════════════════════════════════════════════════

PATH_A_REQUIRED = ("er_pct", "hurdle_pct", "source_score", "method_id", "as_of")


def _route_path_a(declared, path_a, sizing, held, *, catalyst_status, inputs_not_before, today):
    pa = dict(path_a if path_a is not None else (declared.get("path_a") or {}))
    out = {"route": PATH_A_RECLASSIFIED, "inputs": {k: pa.get(k) for k in
                                                    ("applicable",) + PATH_A_REQUIRED}}
    if pa.get("applicable") is not True:
        out.update(outcome=NOT_APPLICABLE,
                   why=("Path A is not DECLARED applicable to this name (Raj decision 5: tested "
                        "only where genuinely appropriate, never as a forced fallback)"))
        return out
    missing = [k for k in PATH_A_REQUIRED if pa.get(k) is None]
    if missing:
        out.update(outcome=UNESTABLISHED, why="Path A is declared applicable but %s is missing"
                   % ", ".join(missing))
        return out
    if inputs_not_before and str(pa["as_of"]) < str(inputs_not_before):
        out.update(outcome=UNESTABLISHED,
                   why="the Path-A inputs (as_of %s) predate this review's book (%s) — stale "
                       "inputs confer nothing" % (pa["as_of"], inputs_not_before))
        return out
    import retention as _rt
    disp = _rt.graduation_disposition(
        ticker=declared.get("_ticker"), catalyst_status=catalyst_status,
        forward_case={"priceable": True, "er_pct": _num(pa["er_pct"]),
                      "hurdle_pct": _num(pa["hurdle_pct"])},
        in_profit=bool(held.get("in_profit")), realisation=held.get("realisation"),
        giveback=held.get("giveback"), size_gbp=_num(held.get("value_gbp")),
        min_entry_gbp=_num(sizing.get("min_entry_gbp")), rung_gbp=_num(sizing.get("rung_gbp")),
        risk_ceiling_gbp=_num(sizing.get("risk_ceiling_gbp")),
        min_hold_until=held.get("min_hold_until"), today=today)
    out["disposition"] = {k: disp.get(k) for k in ("disposition", "state", "why",
                                                   "top_up_to_gbp", "top_up_amount_gbp",
                                                   "target_basis")}
    if disp.get("disposition") == _rt.KEEP_AND_TOP_UP:
        out.update(outcome=AUTHORISED, target_gbp=disp.get("top_up_to_gbp"), why=disp.get("why"))
    elif disp.get("disposition") == _rt.KEEP_AT_SIZE:
        out.update(outcome=AUTHORISED, target_gbp=_num(held.get("value_gbp")), why=disp.get("why"))
    else:
        out.update(outcome=FAILS_ECONOMICS, why=disp.get("why"))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ASSESS — one name, one invocation, route order A -> B -> C
# ═══════════════════════════════════════════════════════════════════════════════════════════

def assess(*, ticker, declared, held, sizing, successor=None, path_a=None,
           inputs_not_before=None, today=None) -> dict:
    """The lifecycle verdict for ONE held name.

    `declared` is the vci_binary_positions row (catalyst_status/date, successor, path_a).
    `held`     {value_gbp, in_profit, min_hold_until, realisation, giveback}.
    `sizing`   {nav_gbp, min_entry_gbp, rung_gbp, risk_ceiling_gbp, budget_available_pct,
                budget_calc_id, evidence_state}.
    Nothing here re-prices a probability, re-derives a budget or recomputes a ceiling; each is
    consumed from the module that owns it (R4.4)."""
    _fi_mark("vci_lifecycle", "assess")
    today = today or _today()
    t = str(ticker or "").upper()
    base = {"schema_version": SCHEMA_VERSION, "ticker": t, "as_of": today,
            "catalyst_status": (declared or {}).get("catalyst_status"),
            "catalyst_date": (declared or {}).get("catalyst_date")}
    if not _flag():
        return dict(base, state=UNKNOWN_DISABLED, trigger=None,
                    why=("isa_policy.V2_FLAGS['vci_lifecycle'] is False — the lifecycle was NOT "
                         "evaluated. UNKNOWN, never ACTIVE (R4.3)."))
    if declared is None:
        raise LifecycleRefused("%s: no declared binary row — the lifecycle cannot be asked "
                               "about a name the registry does not declare (V-1)" % t)
    import retention as _rt
    st = declared.get("catalyst_status")
    if not declared.get("is_binary") or st not in _rt.RESOLVED_STATUSES:
        return dict(base, state=ACTIVE_CURRENT_VCI if declared.get("is_binary") else None,
                    trigger=None, decision=None,
                    why=("the investment-defining binary has not resolved (%s); the current VCI "
                         "authority continues" % st) if declared.get("is_binary") else
                        "not a declared binary — outside the VCI lifecycle")

    d = dict(declared, _ticker=t)
    a = _route_successor(d, successor, sizing, held)
    b = _route_path_a(d, path_a, sizing, held, catalyst_status=st,
                      inputs_not_before=inputs_not_before, today=today)
    routes = [a, b]
    value = _num(held.get("value_gbp")) or 0.0
    out = dict(base, trigger=GRADUATION_TRIGGERED, old_authority_expired=True,
               routes_evaluated=routes)
    blocking = sorted({x for r in routes for x in (r.get("blocking_items") or [])})
    if a["outcome"] == AUTHORISED:
        chosen, route = a, "vci"
    elif b["outcome"] == AUTHORISED:
        chosen, route = b, "growth"
    else:
        chosen, route = None, "vci"

    if chosen is not None:
        target = _num(chosen.get("target_gbp"))
        decision = ("top_up" if target is not None and target > value + 0.005 else
                    "trim" if target is not None and target < value - 0.005 else "hold")
        out.update(state=chosen["route"], route=route, decision=decision,
                   target_gbp=None if target is None else round(target, 2),
                   incremental_gbp=None if target is None else round(max(target - value, 0.0), 2),
                   reason_codes=["%s_AUTHORISED" % chosen["route"]],
                   rejected_alternatives=[{"route": r["route"], "outcome": r["outcome"],
                                           "why": r.get("why")} for r in routes if r is not chosen]
                   + [{"route": EXIT, "outcome": "NOT_TAKEN",
                       "why": "a route positively authorised continued ownership"}],
                   why=chosen.get("why"), executability=None)
    else:
        unest = [r for r in routes if r["outcome"] == UNESTABLISHED]
        codes = ["SUCCESSOR_%s" % a["outcome"], "PATH_A_%s" % b["outcome"]]
        if unest:
            codes.append("AUTHORITY_EXPIRED_UNESTABLISHED")
        ex = {"disposition": "SELL",
              "why": ("post-event route authority: EXIT. Successor VCI — %s. Path A — %s.%s"
                      % (a.get("why"), b.get("why"),
                         (" ⚑ The old authority is NOT revived while a route is unestablished: "
                          "this is authority EXPIRY, not a finding that the economics are "
                          "negative (Raj decision 3)%s."
                          % ((" — blocking %s" % ", ".join(blocking)) if blocking else ""))
                         if unest else ""))}
        # D5: an exit forced by an UNESTABLISHED route does not unlock thesis_realised.
        ex = _rt.exit_executability(ex, in_profit=bool(held.get("in_profit")),
                                    realisation=held.get("realisation"),
                                    giveback=held.get("giveback"),
                                    by_nature=not unest,
                                    min_hold_until=held.get("min_hold_until"), today=today)
        out.update(state=EXIT, route="vci", decision="sell", target_gbp=0.0,
                   incremental_gbp=0.0, reason_codes=codes, blocking_items=blocking,
                   rejected_alternatives=[{"route": r["route"], "outcome": r["outcome"],
                                           "why": r.get("why")} for r in routes],
                   why=ex["why"], executability={k: ex.get(k) for k in
                                                 ("state", "min_hold_exemption",
                                                  "min_hold_until")})
    out["input_id"] = input_id(out)
    return out


def input_id(assessment: dict) -> str:
    """The identity of the ADMISSIBLE inputs and the route outcome they produce. Executability
    (min-hold day count, today's P&L) is re-observed every run and is NOT part of it — a price
    tick must not mint a new decision, but a changed route, target or underwriting must."""
    core = {"ticker": assessment.get("ticker"),
            "catalyst_status": assessment.get("catalyst_status"),
            "catalyst_date": assessment.get("catalyst_date"),
            "state": assessment.get("state"), "decision": assessment.get("decision"),
            "target_gbp": (None if assessment.get("target_gbp") is None
                           else round(float(assessment["target_gbp"]))),
            "routes": [{"route": r.get("route"), "outcome": r.get("outcome"),
                        "inputs": r.get("inputs")}
                       for r in (assessment.get("routes_evaluated") or [])]}
    return "LCI-%s" % _sha12(core)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# RECORD — the canonical decision (first observer writes; unchanged inputs revalidate)
# ═══════════════════════════════════════════════════════════════════════════════════════════

def record(assessment: dict, *, ledger_path=None, observed_by, capital_authority,
           build_id=None, dry_run=False) -> dict:
    """Write, revalidate or supersede the canonical decision for a transitioned name.

    ⚑ Only an AUTHORISED capital run may issue a decision (R18.5): under any other authority the
    assessment is published as evidence and NOTHING is written, exactly as vci_decision_for
    refuses under an unsigned LIVE state."""
    _fi_mark("vci_lifecycle", "record")
    if observed_by not in OBSERVERS:
        raise LifecycleRefused("observer %r is not declared (%s)" % (observed_by, ", ".join(OBSERVERS)))
    st = assessment.get("state")
    if st not in (SUCCESSOR_VCI, PATH_A_RECLASSIFIED, EXIT):
        return {"action": "NO_TRANSITION", "state": st, "decision_id": None}
    if str(capital_authority or "").upper() != "AUTHORISED":
        return {"action": "NOT_RECORDED_CAPITAL_AUTHORITY_%s"
                          % (str(capital_authority).upper() if capital_authority else "UNKNOWN"),
                "state": st, "decision_id": None,
                "why": ("R18.5: capital authority is %s — the lifecycle is published as evidence "
                        "and no decision is issued" % (capital_authority or "UNKNOWN"))}
    import decision_ledger as _dl
    path = ledger_path or _dl.default_path(HERE)
    t = assessment["ticker"]
    iid = assessment["input_id"]
    cur = _dl.current_decisions(path, t, routes=LIFECYCLE_ROUTES)
    prev_lc = [e for e in cur if LIFECYCLE_FLAG in (e.get("flags_at_decision") or [])]
    if prev_lc and prev_lc[-1].get("input_snapshot_id") == iid:
        did = prev_lc[-1].get("decision_id")
        if not dry_run:
            _dl.record_observation(path, did, observed_by=observed_by,
                                   date=assessment.get("as_of"),
                                   note="unchanged admissible inputs %s" % iid)
        return {"action": "REVALIDATED", "state": st, "decision_id": did,
                "supersedes": None, "dry_run": bool(dry_run)}
    prior_ids = [e.get("decision_id") or e.get("_id") for e in cur]
    primary = prior_ids[-1] if prior_ids else None
    delta = None
    if prev_lc:
        old = (prev_lc[-1].get("lifecycle") or {})
        delta = {"previous_decision_id": prev_lc[-1].get("decision_id"),
                 "previous_input_id": prev_lc[-1].get("input_snapshot_id"),
                 "changed": sorted(k for k in ("state", "decision", "target_gbp",
                                               "routes_evaluated", "catalyst_status")
                                   if json.dumps(old.get(k), sort_keys=True, default=str)
                                   != json.dumps(assessment.get(k), sort_keys=True, default=str))}
    payload = {k: assessment.get(k) for k in ("state", "trigger", "routes_evaluated",
                                              "rejected_alternatives", "reason_codes",
                                              "blocking_items", "executability", "target_gbp",
                                              "incremental_gbp", "decision",
                                              "catalyst_status", "catalyst_date", "why")}
    payload.update(observed_by=observed_by, input_id=iid, delta=delta,
                   superseded_authority=prior_ids)
    if dry_run:
        return {"action": "WOULD_WRITE", "state": st, "decision_id": None,
                "supersedes": prior_ids, "dry_run": True}
    e = _dl.log_decision(
        path, t, assessment["route"], assessment["decision"], dedupe=False,
        date=assessment.get("as_of"), build_id=build_id, input_snapshot_id=iid,
        supersedes_decision_id=primary, also_supersedes=prior_ids[:-1],
        target_gbp=assessment.get("target_gbp"), reasons=list(assessment.get("reason_codes") or []),
        flags=[LIFECYCLE_FLAG, "OBSERVED_BY_%s" % observed_by.upper()],
        gates=["lifecycle_state=%s" % st, "input_id=%s" % iid],
        catalyst=assessment.get("catalyst_status"), lifecycle=payload,
        thesis=(assessment.get("why") or "")[:400])
    return {"action": "SUPERSEDED" if prior_ids else "NEW", "state": st,
            "decision_id": e.get("decision_id"), "supersedes": prior_ids,
            "delta": delta, "dry_run": False}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# OBSERVE — the entry point for the VCI run and the intramonth review
# ═══════════════════════════════════════════════════════════════════════════════════════════

def observe(observed_by: str, *, root=None, portfolio_path=None, today=None, dry_run=False,
            capital_authority=None, ledger_path=None) -> dict:
    """Run the SAME held review the monthly pre-run runs, with obligations untouched (dry) and
    the lifecycle recorded when authority permits. Returns the lifecycle summary."""
    _fi_mark("vci_lifecycle", "observe")
    root = root or HERE
    # ⚑ Everything is resolved RELATIVE TO `root`: the book, and the ledger the decision is
    #   written to. A caller running in a scratch tree (a selftest, a Candidate) must never
    #   write a decision into another tree's ledger.
    ledger_path = ledger_path or os.path.join(root, "decision_ledger.json")
    if portfolio_path is None:
        import capital_destination as _cd
        try:
            portfolio_path = _cd.latest_portfolio_path(root=root)
        except Exception as exc:                                        # noqa: BLE001
            return {"observed_by": observed_by, "state": "NO_BOOK", "lifecycle": None,
                    "capital_authority": capital_authority,
                    "why": "no broker book under %s (%s: %s) - nothing observed, nothing "
                           "written" % (root, type(exc).__name__, exc), "warnings": []}
    if capital_authority is None:
        try:
            import release_gate as _rg
            capital_authority = _rg.capital_run_authority(observed_by, root)["authority"]
        except Exception as exc:                                        # noqa: BLE001
            capital_authority = "UNKNOWN (%s)" % type(exc).__name__
    import held_position_review as _hpr
    r = _hpr.review(str(portfolio_path), root=root, today=today, dry_run=True,
                    record_lifecycle=not dry_run, lifecycle_observer=observed_by,
                    capital_authority=capital_authority, ledger_path=ledger_path)
    return {"observed_by": observed_by, "portfolio": os.path.basename(str(portfolio_path)),
            "capital_authority": capital_authority, "state": r.get("state"),
            "lifecycle": (r.get("summary") or {}).get("lifecycle"),
            "warnings": [w for w in (r.get("warnings") or []) if "ISA-0716" in w]}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# SELFTEST — §15.1 "Graduation" is the test list
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _selftest(verbose: bool = True) -> int:
    import tempfile
    import retention as _rt
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:260]) if not cond else ""))
        if not cond:
            fails.append(name)

    SZ = {"nav_gbp": 150000.0, "min_entry_gbp": 4200.0, "rung_gbp": 5250.0,
          "risk_ceiling_gbp": 9000.0, "budget_available_pct": 1.19, "budget_calc_id": "VCIB-test",
          "evidence_state": "CONFIRMED"}
    H = {"value_gbp": 1775.39, "in_profit": True, "min_hold_until": "2027-01-10",
         "realisation": {"fired": True, "role": _rt.REALISATION_ROLE_PERMISSION},
         "giveback": {"fired": False}}
    T = "2026-10-03"
    base = {"is_binary": True, "catalyst_date": "2026-08-10", "asset_structure": "platform"}

    # 1 unresolved -> current VCI continues
    r = assess(ticker="PEND", declared=dict(base, catalyst_status="PENDING"), held=H, sizing=SZ,
               today=T)
    ok("§15.1 unresolved milestone -> current VCI continues (ACTIVE_CURRENT_VCI, no decision)",
       r["state"] == ACTIVE_CURRENT_VCI and r.get("decision") is None, r)

    # 2 resolved + attractive successor -> fresh VCI authority
    good = {"state": "PENDING", "type": "phase3", "priceable": True, "p_thesis": 0.55, "L": 0.35,
            "fv_asymmetry": 3.1, "fv_floor": 2.5, "as_of": "2026-10-02"}
    r = assess(ticker="SUCC", declared=dict(base, catalyst_status="RESOLVED_POSITIVE_SUCCESSOR_PENDING",
                                            successor=good), held=H, sizing=SZ, today=T)
    ok("⚑ MUST-FIRE §15.1: resolved + attractive successor -> SUCCESSOR_VCI with a fresh target "
       "on the vci route", r["state"] == SUCCESSOR_VCI and r["route"] == "vci"
       and r["target_gbp"] and r["target_gbp"] >= SZ["min_entry_gbp"], r)

    # 3 resolved + successor exists but fails economics -> no old authority
    bad = dict(good, fv_asymmetry=1.6)
    r = assess(ticker="SFAIL", declared=dict(base, catalyst_status="RESOLVED_POSITIVE_SUCCESSOR_PENDING",
                                             successor=bad), held=H, sizing=SZ, today=T)
    ok("⚑ MUST-FIRE §15.1: a successor that FAILS its economics confers NO authority -> EXIT",
       r["state"] == EXIT and "SUCCESSOR_FAILS_ECONOMICS" in r["reason_codes"], r)

    # 4 resolved + no successor + genuine Path A qualifies -> Path A decision
    pa = {"applicable": True, "er_pct": 16.0, "hurdle_pct": 11.0, "source_score": 71,
          "method_id": "return_architecture@test", "as_of": "2026-10-02"}
    r = assess(ticker="PATHA", declared=dict(base, catalyst_status="RESOLVED_POSITIVE"),
               held=H, sizing=SZ, path_a=pa, today=T, inputs_not_before="2026-09-30")
    ok("⚑ MUST-FIRE §15.1: resolved + no successor + genuine Path A -> PATH_A_RECLASSIFIED on the "
       "growth route, KEEP meaning TOP UP", r["state"] == PATH_A_RECLASSIFIED
       and r["route"] == "growth" and r["decision"] == "top_up", r)

    # 5 resolved + neither qualifies -> EXIT
    r = assess(ticker="NEITHER", declared=dict(base, catalyst_status="RESOLVED_NEGATIVE"),
               held=H, sizing=SZ, path_a=dict(pa, er_pct=6.0), today=T,
               inputs_not_before="2026-09-30")
    ok("⚑ MUST-FIRE §15.1: resolved + neither route qualifies -> EXIT / target 0",
       r["state"] == EXIT and r["target_gbp"] == 0.0 and r["decision"] == "sell", r)

    # 6 resolved + successor research cannot establish -> EXIT, not HOLD
    unk = {"state": "PENDING", "type": "durability", "priceable": False,
           "refusal_kind": "UNMEASURED_BY_DEFECT", "blocking_items": ["ISA-0660"]}
    r6 = assess(ticker="UNEST", declared=dict(base, catalyst_status="RESOLVED_POSITIVE_SUCCESSOR_PENDING",
                                              successor=unk), held=H, sizing=SZ, today=T)
    ok("⚑ MUST-FIRE §15.1: an UNESTABLISHED successor expires authority -> EXIT, never a HOLD or "
       "a grace state, and names the blocking item",
       r6["state"] == EXIT and "AUTHORITY_EXPIRED_UNESTABLISHED" in r6["reason_codes"]
       and r6["blocking_items"] == ["ISA-0660"]
       and r6["decision"] == "sell" and "GRADUATED_HOLD" not in json.dumps(r6)
       and r6["state"] in FINAL_STATES, r6)
    ok("D5 preserved in EXECUTABILITY: an exit forced by a defect does not unlock thesis_realised, "
       "so inside the window it is SELL_PENDING_MIN_HOLD",
       r6["executability"]["state"] == _rt.SELL_PENDING_MIN_HOLD, r6["executability"])

    # 7 min-hold / gain / loss do not change ROUTE AUTHORITY
    states = set()
    for h in (dict(H, in_profit=True, min_hold_until="2027-01-10"),
              dict(H, in_profit=True, min_hold_until="2026-01-01"),
              dict(H, in_profit=False, min_hold_until="2027-01-10")):
        rr = assess(ticker="NAT", declared=dict(base, catalyst_status="RESOLVED_POSITIVE_SUCCESSOR_PENDING",
                                                successor={"state": "PENDING", "priceable": False,
                                                           "refusal_kind": "UNPRICEABLE_BY_NATURE"}),
                    held=h, sizing=SZ, today=T)
        states.add((rr["state"], rr["decision"], rr["input_id"]))
    ok("⚑ MUST-FIRE §15.1: inside 182 days / in profit / at a loss must NOT change route authority "
       "or the decision identity — only executability differs", len(states) == 1, states)

    # 8/9 lineage: first observer writes, unchanged -> revalidate, changed -> supersede
    lp = os.path.join(tempfile.mkdtemp(), "decision_ledger.json")
    import decision_ledger as _dl
    _dl.log_decision(lp, "LIN", "vci", "top_up", date="2026-08-09")
    _dl.log_decision(lp, "LIN", "vci", "PASS", date="2026-09-13")
    decl = dict(base, catalyst_status="RESOLVED_POSITIVE_SUCCESSOR_PENDING",
                successor={"state": "PENDING", "priceable": False,
                           "refusal_kind": "UNPRICEABLE_BY_NATURE"})
    a1 = assess(ticker="LIN", declared=decl, held=H, sizing=SZ, today=T)
    w1 = record(a1, ledger_path=lp, observed_by="monthly_prerun", capital_authority="AUTHORISED")
    ok("the first observer writes ONE canonical decision and supersedes ALL prior authority "
       "(both the 13-Sep PASS and the orphaned 09-Aug top_up)",
       w1["action"] == "SUPERSEDED" and len(w1["supersedes"]) == 2
       and len(_dl.current_decisions(lp, "LIN")) == 1, w1)
    a2 = assess(ticker="LIN", declared=decl, held=dict(H, value_gbp=1900.0), sizing=SZ,
                today="2026-10-11")
    w2 = record(a2, ledger_path=lp, observed_by="vci_run", capital_authority="AUTHORISED")
    ok("⚑ MUST-FIRE §15.1: pre-run detects, later VCI run with UNCHANGED admissible inputs -> the "
       "SAME decision lineage (REVALIDATED, same id, no second row)",
       w2["action"] == "REVALIDATED" and w2["decision_id"] == w1["decision_id"]
       and len(_dl.current_decisions(lp, "LIN")) == 1, w2)
    decl2 = dict(decl, successor=dict(good, as_of="2026-10-15"))
    a3 = assess(ticker="LIN", declared=decl2, held=H, sizing=SZ, today="2026-10-16")
    w3 = record(a3, ledger_path=lp, observed_by="intramonth", capital_authority="AUTHORISED")
    ok("⚑ MUST-FIRE §15.1: later facts change -> a SUPERSEDING decision with an attributable delta",
       w3["action"] == "SUPERSEDED" and w3["supersedes"] == [w1["decision_id"]]
       and "state" in (w3["delta"] or {}).get("changed", []), w3)

    # negative controls
    w4 = record(a1, ledger_path=lp, observed_by="vci_run", capital_authority="REFUSED")
    ok("NEGATIVE CONTROL (R18.5): under REFUSED authority NOTHING is written",
       w4["action"].startswith("NOT_RECORDED") and w4["decision_id"] is None, w4)
    try:
        assess(ticker="NODECL", declared=None, held=H, sizing=SZ, today=T)
        ok("NEGATIVE CONTROL: an undeclared name must RAISE, not be read as not-a-binary", False)
    except LifecycleRefused:
        ok("NEGATIVE CONTROL: an undeclared name RAISES, it is not read as not-a-binary (V-1)", True)
    stale = assess(ticker="STALE", declared=dict(base, catalyst_status="RESOLVED_POSITIVE",
                                                 successor=dict(good, as_of="2026-07-01")),
                   held=H, sizing=SZ, today=T)
    ok("NEGATIVE CONTROL: an underwriting dated BEFORE the resolution is the OLD case and cannot "
       "authorise a successor", stale["routes_evaluated"][0]["outcome"] == UNESTABLISHED, stale)
    ok("NEGATIVE CONTROL: Path A is never a forced fallback — undeclared -> NOT_APPLICABLE",
       r6["routes_evaluated"][1]["outcome"] == NOT_APPLICABLE, r6["routes_evaluated"][1])

    # ⚑ §5.9 THROUGH THE REAL SHARED ENTRY on the frozen Sep-2026 book: the pre-run observer
    #   decides, the VCI and intramonth observers on UNCHANGED inputs revalidate the same id.
    #   Isolated ledger copy; the tree's own ledger is never touched.
    import shutil
    _pf = os.path.join(HERE, "portfolio_data_sep_2026.json")
    if os.path.exists(_pf) and os.path.exists(os.path.join(HERE, "decision_ledger.json")):
        _lpx = os.path.join(tempfile.mkdtemp(), "decision_ledger.json")
        shutil.copy(os.path.join(HERE, "decision_ledger.json"), _lpx)
        o1 = observe("monthly_prerun", portfolio_path=_pf, today="2026-10-03",
                     capital_authority="AUTHORISED", ledger_path=_lpx)
        o2 = observe("vci_run", portfolio_path=_pf, today="2026-10-11",
                     capital_authority="AUTHORISED", ledger_path=_lpx)
        o3 = observe("intramonth", portfolio_path=_pf, today="2026-10-20",
                     capital_authority="AUTHORISED", ledger_path=_lpx)
        l1, l2, l3 = [((o.get("lifecycle") or {}).get("ABCL") or {}) for o in (o1, o2, o3)]
        ok("⚑ MUST-FIRE §5.9 (real book): the first observer decides ABCL -> EXIT and writes ONE "
           "canonical decision", l1.get("state") == EXIT and l1.get("decision_id")
           and l1.get("record_action") in ("NEW", "SUPERSEDED"), l1)
        ok("⚑ MUST-FIRE §5.9 (real book): the VCI run and the intramonth review on unchanged "
           "inputs REVALIDATE the same decision id - one engine, three callers, one lineage",
           l2.get("record_action") == "REVALIDATED" and l3.get("record_action") == "REVALIDATED"
           and l1.get("decision_id") == l2.get("decision_id") == l3.get("decision_id"), (l2, l3))
    if verbose:
        print("vci_lifecycle selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        raise SystemExit(1 if _selftest() else 0)
    if "--observe" in argv:
        who = argv[argv.index("--observe") + 1]
        res = observe(who, dry_run="--dry-run" in argv)
        print(json.dumps(res, indent=1, default=str))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
