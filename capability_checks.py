#!/usr/bin/env python3
"""capability_checks.py — ISA-0699 legs (b)/(c): typed, falsifiable capability evidence (BS-0699-BC).

Every check here drives a declared capability through the REAL orchestrator that consumes it in
production (capital_destination.sleeve_split, deployment_sequencer.sequence,
held_position_review.review, position_alerts._actionability, risk_contribution.record_run/evaluate,
retention.evaluate_ratchet) and returns ONE evidence record (§7 data contract):

    capability, fixture, source_roll, build_id, input_sha256, producer_observation,
    consumer_observation, decision_state, expected, actual, ok

A check proves CONSUMPTION and DECISION EFFECT only when the producer output is shown to change (or
refuse) the orchestrator's decision against a comparator arm. A record whose expected != actual is a
FAILED check, never a softer PASS. These fixtures prove the decision CAN fire through real code; they
are NOT real-run observation (ISA-0699 leg (d) stays OPEN until the 03/04/11-Oct runs).

⚑ Nothing here changes a capital algorithm. No check writes live state: every store is a temp copy
   and every module global that is redirected is restored (ISA-0704).
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

RECORD_FIELDS = ("capability", "fixture", "source_roll", "build_id", "input_sha256",
                 "producer_observation", "consumer_observation", "decision_state",
                 "expected", "actual", "ok")


# ─────────────────────────────────────────────────────────────── evidence record (§7)
def _source_roll():
    try:
        import release_gate as _rg
        return _rg.source_fingerprint(HERE).get("roll")
    except Exception:                                                   # noqa: BLE001
        return None


def _build_id():
    try:
        with open(os.path.join(HERE, "Dashboard", "state", "trusted_build.json"), encoding="utf-8") as fh:
            return json.load(fh).get("build_id")
    except Exception:                                                   # noqa: BLE001
        return None


def record(capability, fixture, inputs, producer_obs, consumer_obs, decision_state, expected, actual):
    return {"capability": capability, "fixture": fixture, "source_roll": _source_roll(),
            "build_id": _build_id(),
            "input_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str)
                                           .encode("utf-8")).hexdigest(),
            "producer_observation": producer_obs, "consumer_observation": consumer_obs,
            "decision_state": decision_state, "expected": expected, "actual": actual,
            "ok": expected == actual}


def validate_record(rec) -> list:
    """-> list of contract violations (empty = valid). A valid record may still be ok=False."""
    if not isinstance(rec, dict):
        return ["record is not a dict"]
    bad = ["missing field %s" % f for f in RECORD_FIELDS if f not in rec]
    if not bad:
        if not rec["source_roll"]:
            bad.append("source_roll is empty — evidence not bound to source")
        if rec["ok"] != (rec["expected"] == rec["actual"]):
            bad.append("ok does not equal expected == actual")
        if rec["decision_state"] in (None, ""):
            bad.append("no final decision state")
    return bad


# ─────────────────────────────────────────────────────────────── shared fixtures
class _TempFillStore:
    """Redirect position_sizing.FILL_STORE to a temp file (restored on exit)."""
    def __init__(self, doc=None):
        self.doc = doc if doc is not None else {"obligations": []}

    def __enter__(self):
        import position_sizing as _ps
        self._ps, self._saved = _ps, _ps.FILL_STORE
        self._td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._td.name, "underfilled_positions.json")
        _ps.save_fill_obligations(self.doc, self.path)
        _ps.FILL_STORE = self.path
        return self

    def __exit__(self, *exc):
        self._ps.FILL_STORE = self._saved
        self._td.cleanup()
        return False


def _book():
    import capital_destination as _cd
    portfolio, _u, policy = _cd._fixture()
    p3 = copy.deepcopy(policy)
    p3["scaling_freeze"]["basis"] = "reallocation_only"
    return portfolio, p3


_UNMEASURED = {"measured": False, "rho_sleeve": None, "rho_basis": "UNMEASURED_ADVERSE_DEFAULT"}


def _use(tk, ev="THIN", score=70.0, corr=None, qualifies=True, held_gbp=0.0):
    return {"ticker": tk, "qualifies": qualifies, "evidence_state": ev, "source_score": score,
            "current_value_gbp": held_gbp, "disqualified_reason": None if qualifies else "fixture: rejected",
            "correlation": copy.deepcopy(corr if corr is not None else _UNMEASURED)}


def _split(uses, amount=20799.54, order=None, store=None):
    import capital_destination as _cd
    portfolio, policy = _book()
    with _TempFillStore(store):
        out = _cd.sleeve_split(amount, portfolio, policy, 11250.0, candidates=uses,
                               sequence={"order": [u["ticker"] for u in uses] if order is None else order,
                                         "basis": "fixture_order"})
    al = out.get("allocation") or {}
    rows = {r["ticker"]: r for r in (al.get("rows") or [])}
    return out, al, rows


def _returns_fixture(weeks=70, gap_after=None, seed=1):
    """stock_return_store.record_level -> weekly_returns on an IN-MEMORY doc (never the live store)."""
    import stock_return_store as _srs
    doc = _srs._empty()
    rnd = random.Random(seed)
    end = datetime.date(2026, 9, 11)
    lv = {"H": 100.0, "C": 50.0}
    n_written = 0
    for i in range(weeks):
        fri = (end - datetime.timedelta(weeks=weeks - 1 - i)).isoformat()
        m = rnd.gauss(0, 0.03)
        for t in lv:
            lv[t] *= 1 + m + rnd.gauss(0, 0.01)
            if t == "C" and gap_after is not None and i < weeks - gap_after:
                continue
            _srs.record_level(doc, t, fri, round(lv[t], 4), "GBP", "capability_checks fixture", fx_to_gbp=1.0)
            n_written += 1
    rets = {t: _srs.weekly_returns(doc, t)[0] for t in lv}
    return doc, rets, n_written


def _assess(rets):
    import correlation_engine as _ce
    import stock_candidates as _sc
    a = _ce.assess(rets, {"H": 1.0}, candidates=["C"])
    return a, _sc._correlation_record("C", a)


# ─────────────────────────────────────────────────────────────── capital-router checks
def check_stock_max_gbp():
    """A qualified use pulls capital into the stock sleeve; with no qualified use stock_max is 0 and
    the sleeve is BLOCKED (the whole amount is offered to funds)."""
    on, _, rows = _split([_use("FA")])
    off, _, _ = _split([_use("FA", qualifies=False)])
    actual = {"open_state": on["state"], "open_gt_0": on["stock_max_gbp"] > 0,
              "blocked_state": off["state"], "blocked_stock_max": off["stock_max_gbp"]}
    expected = {"open_state": "STOCK_SLEEVE_DEMAND_PULL", "open_gt_0": True,
                "blocked_state": "STOCK_SLEEVE_BLOCKED", "blocked_stock_max": 0.0}
    return record("CAP-stock_max_gbp", "qualified vs non-qualified single use (aug-2026 book)",
                  {"uses": ["FA"], "amount": 20799.54},
                  {"stock_max_open": on["stock_max_gbp"], "stock_max_blocked": off["stock_max_gbp"]},
                  {"allocation_rows": {k: v.get("state") for k, v in rows.items()}},
                  on["state"], expected, actual)


def check_fund_max_gbp():
    """Funds are offered what the stock sleeve actually SPENT: an allocating stock sleeve lowers
    fund_max; a blocked one leaves the whole amount to funds (capital_destination.build ->
    allocate_funds reads split['fund_max_gbp'])."""
    amt = 20799.54
    on, al, _ = _split([_use("FA")], amount=amt)
    off, _, _ = _split([_use("FA", qualifies=False)], amount=amt)
    spent = round(sum(float(r.get("allocated_gbp") or 0) for r in (al.get("rows") or [])), 2)
    actual = {"fund_max_when_stock_spends": on["fund_max_gbp"] < amt,
              "fund_max_when_blocked": off["fund_max_gbp"], "build_feeds_allocate_funds": _build_reads_fund_max()}
    expected = {"fund_max_when_stock_spends": True, "fund_max_when_blocked": round(amt, 2),
                "build_feeds_allocate_funds": True}
    return record("CAP-fund_max_gbp", "stock spends vs stock blocked", {"amount": amt},
                  {"fund_max_open": on["fund_max_gbp"], "fund_max_blocked": off["fund_max_gbp"], "stock_spent": spent},
                  {"consumer": "capital_destination.build -> allocate_funds(fund_amount)"},
                  "FUNDS_OFFERED_%s" % off["fund_max_gbp"], expected, actual)


def _build_reads_fund_max() -> bool:
    """AST: build() binds split['fund_max_gbp'] and passes that name to allocate_funds()."""
    import ast
    src = open(os.path.join(HERE, "capital_destination.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "build")
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Subscript) and \
                getattr(n.value.slice, "value", None) == "fund_max_gbp":
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "allocate_funds":
            used = {a.id for a in ast.walk(n) if isinstance(a, ast.Name)}
            if names & used:
                return True
    return False


def check_min_entry_gbp():
    """D16 entry floor: a NEW position whose share is below min_entry is refused (BELOW_ENTRY_FLOOR);
    with capital above the floor it opens FULL."""
    small, _, rs = _split([_use("FA")], amount=1500.0)
    big, al, rb = _split([_use("FA")], amount=20799.54)
    actual = {"small": rs.get("FA", {}).get("state"), "small_gbp": rs.get("FA", {}).get("allocated_gbp"),
              "large": rb.get("FA", {}).get("state")}
    expected = {"small": "BELOW_ENTRY_FLOOR", "small_gbp": 0.0, "large": "FULL"}
    return record("CAP-min_entry_gbp", "GBP 1,500 vs GBP 20,799.54 offered to one NEW use",
                  {"amounts": [1500.0, 20799.54]}, {"min_entry_gbp": al.get("min_entry_gbp")},
                  {"row_states": actual}, actual["small"], expected, actual)


def check_target_pct():
    """The ladder rung sizes the demand: CONFIRMED (NORMAL 4.5%) pulls more than THIN (STARTER 3.5%);
    DEGRADED_REVERSED may not receive new capital (stock_max 0)."""
    _, rets, _ = _returns_fixture()
    _, corr = _assess(rets)
    thin, _, _ = _split([_use("C", "THIN", corr=corr)])
    conf, _, _ = _split([_use("C", "CONFIRMED", corr=corr)])
    rev, _, _ = _split([_use("C", "DEGRADED_REVERSED", corr=corr)])
    actual = {"confirmed_gt_thin": conf["stock_max_gbp"] > thin["stock_max_gbp"],
              "reversed_stock_max": rev["stock_max_gbp"], "reversed_state": rev["state"]}
    expected = {"confirmed_gt_thin": True, "reversed_stock_max": 0.0, "reversed_state": "STOCK_SLEEVE_BLOCKED"}
    return record("CAP-target_pct", "THIN vs CONFIRMED vs DEGRADED_REVERSED, measured correlation",
                  {"states": ["THIN", "CONFIRMED", "DEGRADED_REVERSED"]},
                  {"thin": thin["stock_max_gbp"], "confirmed": conf["stock_max_gbp"]},
                  {"sleeve_states": [thin["state"], conf["state"], rev["state"]]},
                  rev["state"], expected, actual)


def check_rho_sleeve():
    """correlation_engine (assess -> candidate_correlation) drives two decisions: (1) an UNMEASURED
    record caps a CONFIRMED use at STARTER in sleeve_split; (2) a breach reclassifies the name
    REPLACEMENT_ONLY in deployment_sequencer.sequence, so it is not in the order and gets no capital."""
    import deployment_sequencer as _ds
    _, rets, _ = _returns_fixture()
    a, corr = _assess(rets)
    _, rets_short, _ = _returns_fixture(gap_after=10)
    _, corr_short = _assess(rets_short)
    measured, _, _ = _split([_use("C", "CONFIRMED", corr=corr)])
    unmeasured, _, _ = _split([_use("C", "CONFIRMED", corr=corr_short)])
    mtx = {k: (v.get("rho") if isinstance(v, dict) else v) for k, v in a["matrix"]["pairs"].items()}
    row = dict(_use("C", "CONFIRMED", corr=corr), band="A")
    seq = _ds.sequence({"qualifying": [row], "ranking_basis": "source_score"}, held=["H"], matrix=mtx,
                       ranking_basis="source_score")
    verdict = (seq.get("records") or [{}])[0].get("verdict")
    actual = {"measured_flag": corr["measured"], "short_flag": corr_short["measured"],
              "unmeasured_capped_below_measured": unmeasured["stock_max_gbp"] < measured["stock_max_gbp"],
              "breach_verdict": verdict, "breach_in_order": "C" in (seq.get("order") or [])}
    expected = {"measured_flag": True, "short_flag": False, "unmeasured_capped_below_measured": True,
                "breach_verdict": "REPLACEMENT_ONLY", "breach_in_order": False}
    # ⚑ The breach is NOT claimed decision-effective on capital: allocate() does not consume the
    #   verdict (ISA-0705). witness_isa0705_replacement_only_funded() proves that gap stays visible.
    return record("CAP-rho_sleeve", "70 vs 10 overlapping weeks; rho ~0.92 against the held name",
                  {"weeks": [70, 10], "seed": 1},
                  {"rho_sleeve": corr.get("rho_sleeve"), "rho_basis": [corr.get("rho_basis"), corr_short.get("rho_basis")]},
                  {"stock_max": [measured["stock_max_gbp"], unmeasured["stock_max_gbp"]],
                   "sequencer_order": seq.get("order")},
                  "UNMEASURED_CORRELATION_STARTER_CAP", expected, actual)


def witness_isa0705_replacement_only_funded() -> dict:
    """DEFECT WITNESS (ISA-0705), not a capability proof. Reproduces: a REPLACEMENT_ONLY name absent
    from the sequencer order is still FUNDED by sleeve_split -> allocate. `reproduces` True while the
    defect stands; when ISA-0705 is fixed this turns False and the selftest fails ON PURPOSE so the
    CAP-rho_sleeve declaration (REPLACEMENT_ONLY must-fire) is updated rather than silently stale."""
    import deployment_sequencer as _ds
    _, rets, _ = _returns_fixture()
    a, corr = _assess(rets)
    mtx = {k: (v.get("rho") if isinstance(v, dict) else v) for k, v in a["matrix"]["pairs"].items()}
    c_row = dict(_use("C", "CONFIRMED", corr=corr), band="A")
    seq = _ds.sequence({"qualifying": [c_row], "ranking_basis": "source_score"}, held=["H"], matrix=mtx,
                       ranking_basis="source_score")
    _, al, rows = _split([_use("D", "THIN"), c_row], order=["D"])
    funded = float((rows.get("C") or {}).get("allocated_gbp") or 0.0)
    return {"item": "ISA-0705", "sequencer_verdict": (seq.get("records") or [{}])[0].get("verdict"),
            "sequencer_order": seq.get("order"), "C_allocated_gbp": funded,
            "C_state": (rows.get("C") or {}).get("state"),
            "reproduces": funded > 0 and "C" not in (seq.get("order") or [])}


def check_gbp():
    """stock_return_store.record_level observations are what makes correlation MEASURED: a full
    Friday history yields a measured record and a HIGHER size than a thin history."""
    _, rets, n_full = _returns_fixture()
    _, rets_thin, n_thin = _returns_fixture(gap_after=10)
    _, corr = _assess(rets)
    _, corr_thin = _assess(rets_thin)
    full, _, _ = _split([_use("C", "CONFIRMED", corr=corr)])
    thin, _, _ = _split([_use("C", "CONFIRMED", corr=corr_thin)])
    actual = {"full_history_measured": corr["measured"], "thin_history_measured": corr_thin["measured"],
              "size_changes": full["stock_max_gbp"] != thin["stock_max_gbp"]}
    expected = {"full_history_measured": True, "thin_history_measured": False, "size_changes": True}
    return record("CAP-gbp", "70 vs 10 recorded Friday closes for the candidate",
                  {"observations": [n_full, n_thin]},
                  {"returns_C": [len(rets["C"]), len(rets_thin["C"])]},
                  {"stock_max": [full["stock_max_gbp"], thin["stock_max_gbp"]]},
                  "MEASURED" if corr["measured"] else "UNMEASURED", expected, actual)


def check_evidence_state():
    """evidence_state.classify output is the rung input: one confirming channel -> THIN; a price and
    a fundamental channel -> CONFIRMED, and the router sizes the CONFIRMED name larger."""
    import evidence_state as _es
    thin = _es.classify({"E1": True}, route="main", coverage_ok=True)["state"]
    conf = _es.classify({"E1": True, "E4": True}, route="main", coverage_ok=True)["state"]
    _, rets, _ = _returns_fixture()
    _, corr = _assess(rets)
    a, _, _ = _split([_use("C", thin, corr=corr)])
    b, _, _ = _split([_use("C", conf, corr=corr)])
    actual = {"thin": thin, "confirmed": conf, "confirmed_sized_larger": b["stock_max_gbp"] > a["stock_max_gbp"]}
    expected = {"thin": "THIN", "confirmed": "CONFIRMED", "confirmed_sized_larger": True}
    return record("CAP-evidence_state", "channels {E1} vs {E1,E4}", {"channels": [["E1"], ["E1", "E4"]]},
                  {"states": [thin, conf]}, {"stock_max": [a["stock_max_gbp"], b["stock_max_gbp"]]},
                  conf, expected, actual)


def check_underfilled_obligation_gbp():
    """ISA-0701 end to end: the reporting router PROPOSES; activate_from_executions creates the ACTIVE
    claim only from a confirmed execution; the NEXT router run fills that claim first. Comparator:
    without the execution no ACTIVE claim exists and no OBLIGATION_FILLED row appears."""
    import position_sizing as _ps
    uses = [_use("FA", score=70.0), _use("FB", score=60.0)]
    plan_run, plan_al, plan_rows = _split(uses, amount=9700.0)
    prop = [p for p in (plan_al.get("proposed_obligations") or []) if p.get("ticker") == "FB"]
    plans = {"oct_2026": {"allocation": plan_al, "artifact": "capital_destination_oct_2026.json",
                          "sha256": "fixture", "as_of": "2026-10-03"}}
    executed = float(plan_rows["FB"]["allocated_gbp"])
    entry = {"_id": "2026-10-04::FB::buy", "date": "2026-10-04", "ticker": "FB", "route": "growth",
             "decision": "buy", "execution_status": "confirmed_executed", "execution_source": "transaction_record",
             "executed_amount_gbp": executed, "executed_date": "2026-10-06", "executed_quantity": 10,
             "executed_reference": "FIXTURE-REF"}
    store = {"obligations": []}
    act = _ps.activate_from_executions([entry], plans.get, doc=store, today="2026-10-06")
    nxt = [_use("NEWB", score=99.0), _use("FB", score=1.0, held_gbp=executed)]
    _, al_with, rows_with = _split(nxt, amount=9700.0, order=["NEWB", "FB"], store=store)
    unexec = {"obligations": []}
    _ps.activate_from_executions([dict(entry, execution_status="recommended")], plans.get, doc=unexec,
                                 today="2026-10-06")
    _, _, rows_without = _split(nxt, amount=9700.0, order=["NEWB", "FB"], store=unexec)
    actual = {"proposed": bool(prop) and prop[0].get("state") == "PROPOSED", "activated": act.get("activated"),
              "filled_first": (al_with.get("order") or [None])[0] == "FB"
              and rows_with.get("FB", {}).get("state") == "OBLIGATION_FILLED",
              "comparator_active": len(unexec["obligations"]),
              "comparator_state": rows_without.get("FB", {}).get("state") != "OBLIGATION_FILLED"}
    expected = {"proposed": True, "activated": ["FB"], "filled_first": True, "comparator_active": 0,
                "comparator_state": True}
    return record("CAP-underfilled_obligation_gbp", "propose -> confirmed execution -> next-run first claim",
                  {"amount": 9700.0, "executed": executed},
                  {"proposed_shortfall_gbp": prop[0].get("shortfall_gbp") if prop else None,
                   "store_after_activation": [o.get("state") for o in store["obligations"]]},
                  {"next_run_order": al_with.get("order"), "FB_state": rows_with.get("FB", {}).get("state")},
                  rows_with.get("FB", {}).get("state"), expected, actual)


def check_stock_sleeve_weight_now_pct():
    """NEGATIVE-SCOPE evidence: the published weight is consumed ONLY by the run-context summary and
    the email renderer. Two books with different sleeve weights and the same qualified demand produce
    the SAME stock_max — the weight does not decide capital (the band-floor derivation was deleted,
    ISA-0454)."""
    import capital_destination as _cd
    portfolio, policy = _book()
    heavy = copy.deepcopy(portfolio)
    heavy["summary"]["stock_sleeve_value_gbp"] = float(heavy["summary"]["stock_sleeve_value_gbp"]) * 1.5
    with _TempFillStore():
        a = _cd.sleeve_split(20799.54, portfolio, policy, 11250.0, candidates=[_use("FA")],
                             sequence={"order": ["FA"], "basis": "fixture_order"})
        b = _cd.sleeve_split(20799.54, heavy, policy, 11250.0, candidates=[_use("FA")],
                             sequence={"order": ["FA"], "basis": "fixture_order"})
    summ = _cd.summary_for_run_context({"state": "OK", "sleeve_split": a})
    actual = {"weight_differs": a["stock_sleeve_weight_now_pct"] != b["stock_sleeve_weight_now_pct"],
              "stock_max_identical": a["stock_max_gbp"] == b["stock_max_gbp"],
              "summary_carries_weight": summ.get("sleeve_weight_now_pct") == a["stock_sleeve_weight_now_pct"]}
    expected = {"weight_differs": True, "stock_max_identical": True, "summary_carries_weight": True}
    return record("CAP-stock_sleeve_weight_now_pct", "sleeve value x1.0 vs x1.5, same demand",
                  {"scale": [1.0, 1.5]}, {"weights": [a["stock_sleeve_weight_now_pct"], b["stock_sleeve_weight_now_pct"]]},
                  {"stock_max": [a["stock_max_gbp"], b["stock_max_gbp"]], "renderer": "summary_for_run_context"},
                  "NOT_DECISION_EFFECTIVE", expected, actual)


# ─────────────────────────────────────────────────────────────── held-book / review checks
def _review(**patch):
    import held_position_review as _h
    import thesis_state as _ts
    saved = _ts.load_states
    try:
        if "states" in patch:
            _ts.load_states = lambda root=None: patch["states"]           # noqa: E731
        return _h.review(os.path.join(HERE, patch.get("portfolio", "portfolio_data_sep_2026.json")), dry_run=True)
    finally:
        _ts.load_states = saved


def check_held_position_review():
    """The review runs on the frozen Sep-2026 book (state OK, exit dispositions, refused ceilings) and
    REFUSES an absent portfolio rather than reporting a clean sleeve."""
    r = _review()
    r2 = _review(portfolio="no_such_portfolio_file.json")
    s = r["summary"]
    actual = {"state": r["state"], "absent_state": r2["state"], "has_dispositions": bool(s.get("dispositions")),
              "min_hold_enforced": s.get("min_hold_enforced")}
    expected = {"state": "OK", "absent_state": "UNAVAILABLE", "has_dispositions": True, "min_hold_enforced": True}
    return record("CAP-held_position_review", "portfolio_data_sep_2026.json vs missing file",
                  {"portfolio": "portfolio_data_sep_2026.json"}, {"n_held": s.get("n_held")},
                  {"dispositions": s.get("dispositions"), "ceiling_refused": s.get("ceiling_refused")},
                  r2["state"], expected, actual)


def check_graduation_disposition():
    """retention.graduation_disposition through review: on the frozen Sep-2026 book ABCL reaches SELL
    (the only state this book reaches — the other six states have no real-path fixture yet)."""
    r = _review()
    disp = r["summary"].get("dispositions") or {}
    actual = {"ABCL": disp.get("ABCL")}
    expected = {"ABCL": "SELL"}
    return record("CAP-graduation_disposition", "frozen Sep-2026 book", {"portfolio": "portfolio_data_sep_2026.json"},
                  {"dispositions": disp}, {"warnings_n": len(r.get("warnings") or [])},
                  disp.get("ABCL"), expected, actual)


def check_thesis_state():
    """thesis_state (apply via cap_rung) through review: the same book with MU declared BROKEN blocks new
    capital for MU; with MU INTACT it does not."""
    import thesis_state as _ts
    base = _ts.load_states()
    rows = copy.deepcopy(base)
    def _with(state):
        d = copy.deepcopy(rows)
        tgt = d.get("states") if isinstance(d.get("states"), dict) else d
        cur = tgt.get("MU") if isinstance(tgt.get("MU"), dict) else {}
        tgt["MU"] = dict(cur, state=state, rationale="capability_checks fixture")
        return d
    ri = _review(states=_with("INTACT"))
    rb = _review(states=_with("BROKEN"))
    th = lambda r: next((x.get("thesis") or {} for x in r["rows"] if x.get("ticker") == "MU"), {})  # noqa: E731
    actual = {"intact_blocks": th(ri).get("blocks_new_capital"), "broken_blocks": th(rb).get("blocks_new_capital")}
    expected = {"intact_blocks": False, "broken_blocks": True}
    return record("CAP-thesis_state", "MU INTACT vs BROKEN on the frozen Sep-2026 book", {"ticker": "MU"},
                  {"rung_out": [th(ri).get("rung_out"), th(rb).get("rung_out")]},
                  {"blocks_new_capital": [th(ri).get("blocks_new_capital"), th(rb).get("blocks_new_capital")]},
                  "BLOCKS_NEW_CAPITAL" if th(rb).get("blocks_new_capital") else "NOT_BLOCKED", expected, actual)


def check_min_hold_verdict():
    """position_sizing.min_hold_ok consumed by position_alerts._actionability: inside the window a
    winner is PROFIT_TAKING_REVIEW_PERMITTED, a loser NOT_ACTIONABLE, no entry date UNEVALUATED."""
    import position_alerts as _pa
    kw = dict(position_first_entry_date="2026-08-01", today="2026-09-16")
    win = _pa._actionability(True, True, 12.0, **kw).split(" ")[0]
    loss = _pa._actionability(True, True, -8.0, **kw).split(" ")[0]
    und = _pa._actionability(True, True, 5.0, today="2026-09-16").split(" ")[0]
    actual = {"profit": win, "loss": loss, "no_date": und}
    expected = {"profit": "PROFIT_TAKING_REVIEW_PERMITTED", "loss": "NOT_ACTIONABLE", "no_date": "MIN_HOLD_UNEVALUATED"}
    return record("CAP-min_hold_verdict", "entry 01-Aug-2026, today 16-Sep-2026: +12% / -8% / no date",
                  kw, {"producer": "position_sizing.min_hold_ok"}, actual, loss, expected, actual)


def check_mctr():
    """risk_contribution.contributions -> record_run -> evaluate (Step 6.12e): a below-tolerance name is
    FLAGGED only on the second consecutive run. The flag is a REVIEW finding; no capital consumer."""
    import risk_contribution as _rc
    c = _rc.contributions({"A": 3.5, "E": 3.5}, {"A": 0.40, "E": 0.90}, starter_pct=3.5)
    doc = {"runs": [], "flags": {}, "m3_pairs": []}
    r1 = _rc.record_run(c, run_date="2026-09-01", doc=doc, persist=False)
    r2 = _rc.record_run(c, run_date="2026-10-01", doc=doc, persist=False)
    ev = _rc.evaluate(doc)
    actual = {"first_run_flagged": r1["flagged"], "second_run_flagged": r2["flagged"],
              "m1_verdict_present": bool((ev.get("M1") or {}).get("verdict"))}
    expected = {"first_run_flagged": [], "second_run_flagged": ["E"], "m1_verdict_present": True}
    return record("CAP-mctr", "two consecutive runs, E at sigma 0.90", {"w": {"A": 3.5, "E": 3.5}},
                  {"rc_share_E": c["rows"]["E"]["rc_share"]}, {"M1": (ev.get("M1") or {}).get("verdict")},
                  "FLAGGED" if r2["flagged"] else "NOT_FLAGGED", expected, actual)


def check_ratchet_route():
    """NEGATIVE-SCOPE evidence: route_attribution is published by evaluate_ratchet but is NON-GATING —
    changing every route leaves the gate's state and fire decision identical."""
    import retention as _rt
    pos = [{"ticker": "AVGO", "route": "pre_framework"}, {"ticker": "MU", "route": "pre_framework"}]
    kw = dict(months_measured=12, current_sleeve_weight_pct=16.65, largest_position_ticker="MU",
              sleeve_vs_vuag_pp=-1.0, months_trailing=3, sleeve_vs_vuag_exlargest_pp=-1.0)
    a = _rt.evaluate_ratchet(positions=pos, **kw)
    b = _rt.evaluate_ratchet(positions=[dict(p, route="vci") for p in pos], **kw)
    actual = {"attribution_differs": a.get("route_attribution") != b.get("route_attribution"),
              "state_identical": (a["state"], a["fires"]) == (b["state"], b["fires"]),
              "gating": (a.get("route_attribution") or {}).get("gating")}
    expected = {"attribution_differs": True, "state_identical": True, "gating": False}
    return record("CAP-ratchet_route", "all pre_framework vs all vci routes", {"positions": pos},
                  {"buckets": (a.get("route_attribution") or {}).get("counts")}, {"state": [a["state"], b["state"]]},
                  "NOT_DECISION_EFFECTIVE", expected, actual)


def check_vci_binary_risk_committed():
    """NEGATIVE-SCOPE + producer-state evidence: position_sizing.binary_budget_report (the one home that
    monthly_isa_prerun step 6.5 calls unchanged) MEASURES the Sep-2026 book and reports UNCOMPUTED (None,
    never 0) for an absent portfolio. Its consumers are a human precondition (Run_Context Step 8B) and the
    email renderer; no framework decision engine reads it (ISA-0706 records the second, divergent copy)."""
    import position_sizing as _ps
    m = _ps.binary_budget_report(os.path.join(HERE, "portfolio_data_sep_2026.json"))
    u = _ps.binary_budget_report(os.path.join(HERE, "no_such_portfolio_file.json"))
    ms, us = m.get("summary") or {}, u.get("summary") or {}
    actual = {"sep_measured": ms.get("vci_binary_risk_committed") is not None and m.get("ok") is True,
              "absent_uncomputed": us.get("vci_binary_risk_committed") is None and u.get("ok") is False}
    expected = {"sep_measured": True, "absent_uncomputed": True}
    return record("CAP-vci_binary_risk_committed", "portfolio_data_sep_2026.json vs missing file",
                  {"portfolio": "portfolio_data_sep_2026.json"},
                  {"committed_pct": ms.get("vci_binary_risk_committed")},
                  {"consumers": "Run_Context Step 8B (human) + email_prefill (renderer)"},
                  "NOT_DECISION_EFFECTIVE", expected, actual)


CHECKS = {
    "CAP-stock_max_gbp": check_stock_max_gbp,
    "CAP-fund_max_gbp": check_fund_max_gbp,
    "CAP-min_entry_gbp": check_min_entry_gbp,
    "CAP-target_pct": check_target_pct,
    "CAP-rho_sleeve": check_rho_sleeve,
    "CAP-gbp": check_gbp,
    "CAP-evidence_state": check_evidence_state,
    "CAP-underfilled_obligation_gbp": check_underfilled_obligation_gbp,
    "CAP-stock_sleeve_weight_now_pct": check_stock_sleeve_weight_now_pct,
    "CAP-held_position_review": check_held_position_review,
    "CAP-graduation_disposition": check_graduation_disposition,
    "CAP-thesis_state": check_thesis_state,
    "CAP-min_hold_verdict": check_min_hold_verdict,
    "CAP-mctr": check_mctr,
    "CAP-ratchet_route": check_ratchet_route,
    "CAP-vci_binary_risk_committed": check_vci_binary_risk_committed,
}


def run_all() -> dict:
    out = {}
    for cap, fn in CHECKS.items():
        try:
            out[cap] = fn()
        except Exception as exc:                                        # noqa: BLE001
            out[cap] = {"capability": cap, "error": "%s: %s" % (type(exc).__name__, exc), "ok": False}
    return out


def _selftest(verbose: bool = True) -> int:
    n = 0
    # every check, by name (the registry's AST resolution requires the name to appear here):
    # check_stock_max_gbp check_fund_max_gbp check_min_entry_gbp check_target_pct check_rho_sleeve
    # check_gbp check_evidence_state check_underfilled_obligation_gbp check_stock_sleeve_weight_now_pct
    # check_held_position_review check_graduation_disposition check_thesis_state check_min_hold_verdict
    # check_mctr check_ratchet_route
    recs = run_all()
    for cap, rec in recs.items():
        assert "error" not in rec, "%s: check raised %s" % (cap, rec.get("error"))
        bad = validate_record(rec)
        assert not bad, "%s: evidence record violates §7: %s" % (cap, bad)
        assert rec["capability"] == cap, (cap, rec["capability"])
        assert rec["ok"], "%s MUST-FIRE FAILED: expected %s actual %s" % (cap, rec["expected"], rec["actual"])
        n += 3
    # NEGATIVE: a record whose expected != actual is ok=False and still structurally valid
    neg = record("CAP-x", "neg", {}, {}, {}, "S", {"a": 1}, {"a": 2})
    assert neg["ok"] is False and not validate_record(neg)
    n += 1
    # NEGATIVE: missing fields / unbound source roll / lying ok are refused
    assert validate_record({"capability": "CAP-x"})
    lie = dict(neg, ok=True)
    assert "ok does not equal expected == actual" in validate_record(lie)
    assert validate_record(dict(neg, source_roll=None))
    n += 3
    # NEGATIVE CONTROL (remove the producer->consumer field): strip the correlation record and the
    # router must REFUSE rather than size — the chain the rho/gbp checks rely on is load-bearing.
    import capital_destination as _cd
    raised = False
    try:
        u = _use("FA")
        u["correlation"] = None
        _split([u])
    except _cd.DestinationRefused:
        raised = True
    assert raised, "NEGATIVE CONTROL: a use with no correlation record must be refused by the router"
    n += 1
    # NEGATIVE CONTROL (corrupt the producer output): an evidence_state with no rung refuses sizing
    raised = False
    try:
        _split([_use("FA", "ESTABLISHED")])
    except _cd.DestinationRefused:
        raised = True
    assert raised, "NEGATIVE CONTROL: an unknown evidence_state must refuse, never size"
    n += 1
    # DEFECT WITNESS: ISA-0705 must still reproduce, or the CAP-rho_sleeve declaration is stale
    w = witness_isa0705_replacement_only_funded()
    assert w["sequencer_verdict"] == "REPLACEMENT_ONLY" and w["reproduces"], (
        "ISA-0705 no longer reproduces (%s). If it was FIXED, add the REPLACEMENT_ONLY must-fire to "
        "CAP-rho_sleeve and retire this witness; otherwise the fixture drifted." % w)
    n += 1
    if verbose:
        print("capability_checks selftest: %d assertions over %d checks, 0 failed" % (n, len(recs)))
    return 0


if __name__ == "__main__":
    if "--run" in sys.argv:
        print(json.dumps(run_all(), indent=1, default=str))
    else:
        sys.exit(_selftest())
