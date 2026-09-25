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


def _split(uses, amount=20799.54, order=None, store=None, sequence=None, membership=None,
           underwriting=None):
    """`sequence=` passes the REAL sequencer output through the real router (ISA-0705).

    A hand-made {order, basis} dict is fine for fixtures that are not about admission, but it
    cannot exercise a funding verdict it does not carry — which is precisely how the
    REPLACEMENT_ONLY gap stayed invisible to every test that used it."""
    import capital_destination as _cd
    portfolio, policy = _book()
    seq = sequence if sequence is not None else {
        "order": [u["ticker"] for u in uses] if order is None else order,
        "basis": "fixture_order"}
    with _TempFillStore(store):
        out = _cd.sleeve_split(amount, portfolio, policy, 11250.0, candidates=uses,
                               sequence=seq, membership=membership, underwriting=underwriting)
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
    # ⚑ ISA-0705 (20-Sep-2026) — THE MUST-FIRE THE WITNESS DEMANDED. The breach verdict is now
    #   decision-effective on capital: `allocate()` consumes it, so the declaration asserts the
    #   GBP outcome, not merely that the gate produced a string.
    _w705 = witness_isa0705_replacement_only_funded()
    actual = {"measured_flag": corr["measured"], "short_flag": corr_short["measured"],
              "unmeasured_capped_below_measured": unmeasured["stock_max_gbp"] < measured["stock_max_gbp"],
              "breach_verdict": verdict, "breach_in_order": "C" in (seq.get("order") or []),
              "breach_funded_gbp": _w705["C_allocated_gbp"],
              "breach_net_incremental_gbp": _w705["net_incremental_gbp"]}
    expected = {"measured_flag": True, "short_flag": False, "unmeasured_capped_below_measured": True,
                "breach_verdict": "REPLACEMENT_ONLY", "breach_in_order": False,
                "breach_funded_gbp": 0.0, "breach_net_incremental_gbp": 0.0}
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
    # ⚑ BOTH names go through the sequencer, which is what the real router does. Sequencing
    #   only the breaching name and hand-writing the order for the other is how this fixture
    #   managed to exercise a funding path without exercising the funding VERDICT.
    d_row = dict(_use("D", "THIN"), band="A", source_score=80.0)
    seq = _ds.sequence({"qualifying": [c_row, d_row], "ranking_basis": "source_score"},
                       held=["H"], matrix=mtx, ranking_basis="source_score")
    # ⚑ THE REAL SEQUENCER OUTPUT goes through the real router — not a hand-made {order}
    #   dict. Feeding the router an order without the verdict is how this gap survived every
    #   test that touched it.
    _, al, rows = _split([d_row, c_row], sequence=seq)
    funded = float((rows.get("C") or {}).get("allocated_gbp") or 0.0)
    repl = (al.get("replacement_only") or {})
    _c_rec = next((r for r in (seq.get("records") or []) if r.get("ticker") == "C"), {})
    return {"item": "ISA-0705", "sequencer_verdict": _c_rec.get("verdict"),
            "sequencer_order": seq.get("order"), "C_allocated_gbp": funded,
            "C_state": (rows.get("C") or {}).get("state"),
            "net_incremental_gbp": repl.get("net_incremental_gbp"),
            "refused": repl.get("refused"),
            # ⚑ NOT named fund_max_gbp: this is the run's RESIDUAL after the refusal, and
            #   naming it after a registered quantity it is not would make this witness a
            #   second computer of that quantity (framework_integrity Q1 flagged it).
            "residual_gbp_after_refusal": al.get("residual_gbp"),
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
    # ⚑ ISA-0685 (20-Sep-2026): this fixture asserts a VALID executed obligation, which
    #   presupposes the name was admitted. Declare that instead of inheriting whatever the live
    #   ledger says about a synthetic ticker — and prove the refusing case in the same check.
    _ADM = {"state": "ADMITTED_DECIDED", "may_generate_fill_obligation": True,
            "may_hold_new_capital_priority": True,
            "why": "capability_checks fixture: DECLARED admitted (ISA-0685)"}
    _UND = {"state": "ADMITTED_UNDECIDED", "may_generate_fill_obligation": False,
            "may_hold_new_capital_priority": False,
            "why": "capability_checks fixture: held, no admitting decision (ISA-0685)"}
    act = _ps.activate_from_executions([entry], plans.get, doc=store, today="2026-10-06",
                                       membership=lambda _t: _ADM)
    # ⚑ ISA-0685 FALSE-POSITIVE COMPARATOR, on the identical confirmed execution: a name the
    #   framework never admitted creates NO claim. Same trade, same plan, same execution — the
    #   only difference is the admission, which is the whole contract.
    _na_store = {"obligations": []}
    _na = _ps.activate_from_executions([entry], plans.get, doc=_na_store, today="2026-10-06",
                                       membership=lambda _t: _UND)
    nxt = [_use("NEWB", score=99.0), _use("FB", score=1.0, held_gbp=executed)]
    # ISA-0721 (Raj D17, 23-Sep-2026): the fill is fresh positive capital - declare the admissible
    #   CURRENT case it consumes, and prove the refusing case on the identical claim.
    _UWC = lambda _t: {"case_id": "UWC-FIXTURE-%s" % _t, "er": {"state": "VALID_MECHANICAL"},
                       "admissible_for_positive_size": True}
    _, al_with, rows_with = _split(nxt, amount=9700.0, order=["NEWB", "FB"], store=store,
                                   membership=lambda _t: _ADM, underwriting=_UWC)
    _, _, rows_nocase = _split(nxt, amount=9700.0, order=["NEWB", "FB"], store=json.loads(json.dumps(store)),
                               membership=lambda _t: _ADM, underwriting=lambda _t: None)
    unexec = {"obligations": []}
    _ps.activate_from_executions([dict(entry, execution_status="recommended")], plans.get, doc=unexec,
                                 today="2026-10-06", membership=lambda _t: _ADM)
    _, _, rows_without = _split(nxt, amount=9700.0, order=["NEWB", "FB"], store=unexec,
                                membership=lambda _t: _ADM, underwriting=_UWC)
    actual = {"proposed": bool(prop) and prop[0].get("state") == "PROPOSED", "activated": act.get("activated"),
              "filled_first": (al_with.get("order") or [None])[0] == "FB"
              and rows_with.get("FB", {}).get("state") == "OBLIGATION_FILLED",
              "comparator_active": len(unexec["obligations"]),
              "comparator_state": rows_without.get("FB", {}).get("state") != "OBLIGATION_FILLED",
              # ISA-0685: the same confirmed execution on an UNADMITTED name creates nothing
              "unadmitted_active": len(_na_store["obligations"]),
              "unadmitted_outcome": (_na.get("outcomes") or [{}])[0].get("outcome"),
              # ISA-0721: the same ACTIVE claim with no admissible current case does not fill
              "no_case_state": rows_nocase.get("FB", {}).get("state")}
    expected = {"proposed": True, "activated": ["FB"], "filled_first": True, "comparator_active": 0,
                "comparator_state": True, "no_case_state": "OBLIGATION_FILL_BLOCKED_NO_ADMISSIBLE_ER",
                "unadmitted_active": 0, "unadmitted_outcome": "BLOCKED_NOT_ADMITTED"}
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


def check_vci_lifecycle():
    """ISA-0716 — vci_lifecycle through the REAL held review on the frozen Sep-2026 book: ABCL's
    resolved binary triggers graduation, the successor is UNPRICEABLE_BY_NATURE and Path A is not
    declared, so the route is EXIT; recorded under AUTHORISED into an isolated ledger copy, the
    membership contract (a CONSUMER) then reads ABCL as EXIT_DECIDED from that decision."""
    import shutil
    import tempfile
    import held_position_review as _h
    import sleeve_membership as _sm
    lp = os.path.join(tempfile.mkdtemp(), "decision_ledger.json")
    shutil.copy(os.path.join(HERE, "decision_ledger.json"), lp)
    pf = os.path.join(HERE, "portfolio_data_sep_2026.json")
    r = _h.review(pf, dry_run=True, record_lifecycle=True, capital_authority="AUTHORISED",
                  ledger_path=lp, lifecycle_observer="selftest", today="2026-10-03")
    lc = (r["summary"].get("lifecycle") or {}).get("ABCL") or {}
    with open(pf, encoding="utf-8") as fh:
        mem = _sm.classify("ABCL", held=True, ledger_path=lp)
    actual = {"ABCL": {"state": lc.get("state"), "decision": lc.get("decision"),
                       "membership": mem.get("state"), "recorded": bool(lc.get("decision_id"))}}
    expected = {"ABCL": {"state": "EXIT", "decision": "sell", "membership": "EXIT_DECIDED",
                         "recorded": True}}
    return record("CAP-vci_lifecycle", "frozen Sep-2026 book, isolated ledger copy",
                  {"portfolio": "portfolio_data_sep_2026.json", "as_of": "2026-10-03"},
                  {"lifecycle": lc}, {"membership": mem.get("state"),
                                      "decision_id": lc.get("decision_id")},
                  lc.get("state"), expected, actual)


def check_held_underwriting():
    """ISA-0722 — underwriting.capture_month through the real function on the frozen Sep-2026
    book (dry run, isolated ledger copy): N held -> N named cases; the email renderer (a CONSUMER)
    renders every row with its state; a BUY decision written to the ledger BINDS the case."""
    import shutil
    import tempfile
    import underwriting as _uw
    import email_prefill as _ep
    import decision_ledger as _dl
    td = tempfile.mkdtemp(prefix="cc_uw_")
    lp = os.path.join(td, "decision_ledger.json")
    shutil.copy(os.path.join(HERE, "decision_ledger.json"), lp)
    cap = _uw.capture_month(portfolio_path=os.path.join(HERE, "portfolio_data_sep_2026.json"),
                            scored_path=os.path.join(HERE, "watchlist_scored_sep_2026.json"),
                            as_of="2026-10-03", root=HERE, ledger_path=lp, dry_run=True)
    blk = _ep.build_held_underwriting_block(cap)
    # binding: a case in the scratch store + a decision written beside it
    c = _uw.case_from_row("CCUW", {"expected_return_12_24m": 18.0, "er_state": "VALID_MECHANICAL",
                                   "er_horizon_months": 12, "er_method_id": "expected_return@cc"},
                          as_of="2026-10-03", purpose="SELFTEST", route="growth")
    _uw.append([c], root=td)
    e = _dl.log_decision(lp, "CCUW", "growth", "buy", date="2026-10-04")
    actual = {"complete": cap.get("complete"), "n_rows_rendered": len(blk.get("rows") or []),
              "bound": e.get("underwriting_case_id") == c["case_id"]}
    expected = {"complete": True, "n_rows_rendered": cap.get("n_expected"), "bound": True}
    shutil.rmtree(td, ignore_errors=True)
    return record("CAP-held_underwriting", "frozen Sep-2026 book, dry run, scratch ledger",
                  {"portfolio": "portfolio_data_sep_2026.json", "as_of": "2026-10-03"},
                  {"by_state": cap.get("by_state")}, {"rendered": len(blk.get("rows") or []),
                                                      "decision_bound": actual["bound"]},
                  "COMPLETE" if cap.get("complete") else "INCOMPLETE", expected, actual)


def check_broker_dealability():
    """ISA-0607 — the Sep-2026 Warsaw shape through the REAL candidate list and the REAL router:
    a top-scored WSE name gets no capital and a verified NASDAQ comparator does; the SAME rows with
    an admit-all resolver fund the WSE name, so the refusal is the broker gate's and nothing else."""
    import stock_candidates as _sc
    import broker_dealability as _bd
    rows = [{"ticker": "ZAB.WA", "t1_qualified": True, "source_score": 69.2, "exchange": "NASDAQ",
             "expected_return_12_24m": 20.0, "decision_bucket": "DEPLOY"},
            {"ticker": "HALO", "t1_qualified": True, "source_score": 68.0,
             "expected_return_12_24m": 20.0, "decision_bucket": "DEPLOY"}]
    portfolio, _ = _book()

    def _run(resolver):
        c = _sc.build(portfolio_data=portfolio, step9_pre={"deployable_stack": [dict(r) for r in rows],
                                                  # ISA-0616: C-1 admissible for both, so the
                                                  # refusal measured here is the broker gate's alone
                                                  "current_admissibility": {"verdicts": {
                                                      r["ticker"]: {"verdict": "ADMISSIBLE", "admissible": True}
                                                      for r in rows}}},
                      correlation_assessment={"candidates": {}, "holdings": {}},
                      deploy_floor_pct=15.8, dealability=resolver)
        uses = []
        for x in c["candidates"]:
            u = _use(x["ticker"], ev="THIN", score=x["source_score"], qualifies=x["qualifies"])
            u["disqualified_reason"] = x.get("disqualified_reason")
            u["broker_dealability"] = x.get("broker_dealability")
            uses.append(u)
        _, al, rws = _split(uses, amount=9000.0, order=["ZAB.WA", "HALO"])
        return c, rws
    live_c, live_rows = _run(_bd.Resolver())
    cmp_c, cmp_rows = _run(lambda t: {"state": "DEALABLE_ONLINE", "venue": "FIXTURE",
                                      "admissible_for_new_capital": True, "why": "comparator"})
    _g = lambda rws, t: float((rws.get(t) or {}).get("allocated_gbp") or 0.0)
    actual = {"wse_funded_live": _g(live_rows, "ZAB.WA") > 0, "comparator_funded_live": _g(live_rows, "HALO") > 0,
              "wse_state": ({x["ticker"]: x for x in live_c["candidates"]}["ZAB.WA"]["broker_dealability"] or {}).get("state"),
              "wse_funded_when_gate_admits": _g(cmp_rows, "ZAB.WA") > 0}
    expected = {"wse_funded_live": False, "comparator_funded_live": True,
                "wse_state": "NOT_DEALABLE_ONLINE", "wse_funded_when_gate_admits": True}
    return record("CAP-broker_dealability", "Sep-2026 Warsaw shape (ZAB.WA ranked first) + NMS comparator",
                  {"rows": ["ZAB.WA", "HALO"], "amount": 9000.0},
                  {"verdicts": {x["ticker"]: (x["broker_dealability"] or {}).get("state") for x in live_c["candidates"]}},
                  {"allocated": {t: _g(live_rows, t) for t in ("ZAB.WA", "HALO")}},
                  "REFUSED_NON_DEALABLE" if not actual["wse_funded_live"] else "FUNDED_NON_DEALABLE",
                  expected, actual)


def check_current_admissibility():
    """ISA-0616 - the canonical C-1 verdict through the REAL producer (step9_pre_builder.
    build_current_admissibility -> t1_gates.current_admissibility) and the REAL consumers
    (stock_candidates.build -> the router split). Two T1 rows, same scores: one with THIS run's
    current-definition snapshot, one with a snapshot under a different (historical) definition.
    The current one is funded; the other receives nothing - and a held name whose C-1 is
    unavailable stays a held_topup candidate with no SELL field emitted."""
    import step9_pre_builder as _s9
    import stock_candidates as _sc
    import score_definition as _sd
    from datetime import date as _date
    ref = _date(2026, 9, 5)
    cur = _sd.current_identity()
    snap = {"forward_axis_score": 80.0, "revisions_score": 70.0, "part_a_score": 28,
            "score_definition_basis": "STAMPED_AT_SCORING", "snapshot_as_of": ref.isoformat()}
    scored = {"NTAP": dict(snap, score_definition_hash=cur["hash"]),
              "HALO": dict(snap, score_definition_hash="680b67139d457657")}
    c1 = _s9.build_current_admissibility(scored, ref_date=ref,
                                         pipelines={"NTAP": "growth_stock", "HALO": "growth_stock"})
    rows = [{"ticker": t, "t1_qualified": True, "source_score": 70.0,
             "expected_return_12_24m": 20.0, "decision_bucket": "DEPLOY"} for t in ("HALO", "NTAP")]
    portfolio, _ = _book()
    c = _sc.build(portfolio_data=portfolio,
                  step9_pre={"deployable_stack": rows, "current_admissibility": c1},
                  correlation_assessment={"candidates": {}, "holdings": {}}, deploy_floor_pct=15.8,
                  dealability=lambda t: {"state": "DEALABLE_ONLINE", "venue": "FIXTURE",
                                         "admissible_for_new_capital": True, "why": "fixture"})
    uses = []
    for x in c["candidates"]:
        u = _use(x["ticker"], ev="THIN", score=x["source_score"], qualifies=x["qualifies"])
        u["disqualified_reason"] = x.get("disqualified_reason")
        uses.append(u)
    _, al, rws = _split(uses, amount=9000.0, order=["HALO", "NTAP"])
    _g = lambda t: float((rws.get(t) or {}).get("allocated_gbp") or 0.0)
    v = {t: c1["verdicts"][t]["verdict"] for t in scored}
    actual = {"current_funded": _g("NTAP") > 0, "legacy_funded": _g("HALO") > 0,
              "legacy_verdict": v["HALO"], "current_verdict": v["NTAP"],
              "no_sell_field": not any(k in x for x in c["candidates"] for k in ("sell", "exit", "action_sell"))}
    expected = {"current_funded": True, "legacy_funded": False,
                "legacy_verdict": "INCOMPARABLE_LEGACY_DEFINITION", "current_verdict": "ADMISSIBLE",
                "no_sell_field": True}
    return record("CAP-current_admissibility", "same scores; current-definition vs historical-definition snapshot",
                  {"scored": scored, "amount": 9000.0}, {"verdicts": v},
                  {"allocated": {t: _g(t) for t in ("HALO", "NTAP")}},
                  v["HALO"], expected, actual)


def check_feasible_opportunity_set():
    """ISA-0608 - the historical top-N failure through the REAL producers/consumers: step9's
    build_opportunity_set -> capital_destination.opportunity_set_view -> checkpoint_d tick 8. A
    higher-scored infeasible name cannot take a Checkpoint-D place; the lower-scored feasible name
    can, and the hand-built population that includes the infeasible name is BLOCKED."""
    import step9_pre_builder as _s9b
    import capital_destination as _cd
    import checkpoint_d as _ckd
    dpr = [{"ticker": "ZAB.WA", "rank_basis": "source_score", "source_score": 69.2, "deployment_rank": 1,
            "broker_dealability": "NOT_DEALABLE_ONLINE"},
           {"ticker": "HRMY", "rank_basis": "source_score", "source_score": 75.3, "deployment_rank": 2,
            "t1_qualified": True, "broker_dealability": "DEALABLE_ONLINE"},
           {"ticker": "NTAP", "rank_basis": "source_score", "source_score": 66.0, "deployment_rank": 3,
            "t1_qualified": True, "broker_dealability": "DEALABLE_ONLINE"}]
    stack = [dict(dpr[1], deployable_rank=1), dict(dpr[2], deployable_rank=2)]
    ops = _s9b.build_opportunity_set("cc", dpr, stack)
    view = _cd.opportunity_set_view(ops, [{"ticker": t, "route": "main", "qualifies": True}
                                          for t in ("HRMY", "NTAP")], {"order": ["NTAP", "HRMY"]})
    dec = {"chosen_tickers": ["NTAP"], "pairwise": {"HRMY": "x", "ZAB.WA": "x"},
           "falsification": {"NTAP": "y" * 60}}
    cases = {t: "case" for t in ("HRMY", "NTAP", "ZAB.WA")}
    ok_run = _ckd.validate_checkpoint_d(view["checkpoint_d_top5"], dec, cases, view["checkpoint_d_top5"],
                                        opportunity_set=view)
    bad_run = _ckd.validate_checkpoint_d(["HRMY", "ZAB.WA", "NTAP"], dec, cases, ["HRMY", "ZAB.WA", "NTAP"],
                                         opportunity_set=view)
    actual = {"top5": view["checkpoint_d_top5"], "tick8_clean": not any("ISA-0608" in b for b in ok_run["blocks"]),
              "hand_built_blocked": any("ISA-0608" in b for b in bad_run["blocks"]),
              "infeasible_stage": {x["ticker"]: x["stage"] for x in ops["infeasible"]}.get("ZAB.WA")}
    expected = {"top5": ["HRMY", "NTAP"], "tick8_clean": True, "hand_built_blocked": True,
                "infeasible_stage": "NOT_DEALABLE"}
    return record("CAP-feasible_opportunity_set", "Sep-2026 top-N shape: ZAB.WA ranked above NTAP",
                  {"deployment_priority_rank": [r["ticker"] for r in dpr]},
                  {"opportunity_set_id": ops["opportunity_set_id"], "n_feasible": ops["n_feasible"]},
                  {"checkpoint_d_top5": view["checkpoint_d_top5"]},
                  "FEASIBLE_TOP5" if actual == expected else "MISMATCH", expected, actual)


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
    "CAP-vci_lifecycle": check_vci_lifecycle,
    "CAP-held_underwriting": check_held_underwriting,
    "CAP-broker_dealability": check_broker_dealability,
    "CAP-current_admissibility": check_current_admissibility,
    "CAP-feasible_opportunity_set": check_feasible_opportunity_set,
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
    # check_mctr check_ratchet_route check_vci_lifecycle check_held_underwriting
    # check_broker_dealability check_feasible_opportunity_set
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
    # ⚑ ISA-0705 MUST-FIRE (was a defect witness until 20-Sep-2026). The witness was built to
    #   fail ON PURPOSE the day the defect was fixed, so the CAP-rho_sleeve declaration could
    #   not go quietly stale. It has now flipped, and this is the assertion it demanded: a
    #   REPLACEMENT_ONLY name reaches the real router and is funded GBP 0 as an addition.
    w = witness_isa0705_replacement_only_funded()
    assert w["sequencer_verdict"] == "REPLACEMENT_ONLY" and not w["reproduces"], (
        "ISA-0705 REGRESSED (%s): a REPLACEMENT_ONLY name is being funded as an addition "
        "again. A rollback must not silently re-enable this." % w)
    n += 1
    assert w["C_allocated_gbp"] == 0.0 and w["C_state"] == "REFUSED_REPLACEMENT_ONLY" \
        and w["net_incremental_gbp"] == 0.0 and w["refused"] == ["C"], (
        "ISA-0705 MUST-FIRE: the refusal must be NAMED and the net incremental exposure zero, "
        "not merely a smaller number (%s)" % w)
    n += 1
    # POSITIVE COMPARATOR (ISA-0705 acceptance test 2): the same name with an admissible paired
    # donor IS funded, from the donor, at zero net incremental exposure. A control that can only
    # refuse is not a control.
    import deployment_sequencer as _ds705
    _, _rets705, _ = _returns_fixture()
    _a705, _corr705 = _assess(_rets705)
    _mtx705 = {k: (v.get("rho") if isinstance(v, dict) else v)
               for k, v in _a705["matrix"]["pairs"].items()}
    _c705 = dict(_use("C", "CONFIRMED", corr=_corr705), band="A")
    _d705 = dict(_use("D", "THIN"), band="A", source_score=80.0)
    _seq705 = _ds705.sequence({"qualifying": [_c705, _d705], "ranking_basis": "source_score"},
                              held=["H"], matrix=_mtx705, ranking_basis="source_score")
    _pf705, _pol705 = _book()
    _pol705 = json.loads(json.dumps(_pol705))
    _pol705.setdefault("stock_sleeve", {})["donor_releases"] = {
        "C": {"donor": "H", "released_gbp": 3000.0, "state": "REALISED",
              "provenance": "capability_checks fixture: realised donor reduction"}}
    import capital_destination as _cd705
    with _TempFillStore(None):
        _o705 = _cd705.sleeve_split(20799.54, _pf705, _pol705, 11250.0,
                                    candidates=[_d705, _c705], sequence=_seq705)
    _r705 = {r["ticker"]: r for r in ((_o705.get("allocation") or {}).get("rows") or [])}
    assert (_r705.get("C") or {}).get("state") == "REPLACEMENT_FILL" \
        and (_r705["C"]["allocated_gbp"] == 3000.0) \
        and _r705["C"]["net_incremental_gbp"] == 0.0 \
        and _r705["C"]["funding_source"] == "DONOR_RELEASE", (
        "ISA-0705 POSITIVE COMPARATOR: a paired REALISED donor release must fund the "
        "replacement, capped at the release, at zero net incremental exposure (%s)" % _r705.get("C"))
    n += 1
    if verbose:
        print("capability_checks selftest: %d assertions over %d checks, 0 failed" % (n, len(recs)))
    return 0


if __name__ == "__main__":
    if "--run" in sys.argv:
        print(json.dumps(run_all(), indent=1, default=str))
    else:
        sys.exit(_selftest())
