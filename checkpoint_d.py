#!/usr/bin/env python3
"""
checkpoint_d.py — Step-10 BLOCKING Checkpoint-D gate (redesign Part3 §13.7 / Part2 §C3 / retro #1, E5).

The post-mortem's #1 failure was deciding "buy #1" with no real comparative contest. This makes Step 10
BLOCKING: a deployment decision cannot be finalised until ALL of:
  (1) full comparative cases exist for the TOP-5 T1 names,
  (2) the CHOSEN action is justified PAIRWISE against each of the other top-5 (why it beats each), and
  (3) all top-N names (default 10) — INCLUDING the passes — are logged to the decision ledger.

validate_checkpoint_d() returns {passed, blocks[]}; the review must clear it (no blocks) before acting.
Pure logic + a log_top10 helper over decision_ledger. NOT auto-run in any scheduled task — the review
(Run Context Step 10) calls it as the gate. "The road not taken is the signal" — passes are logged too.
"""
from __future__ import annotations
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import decision_ledger as _dl
except Exception:
    _dl = None
try:
    import action_language as _alang
except Exception:
    _alang = None


# Tick 6 (WP-D): minimum length for a falsification statement. Deliberately low — this is a
# completeness gate, not a quality gate. A one-word "none" must fail; a real sentence must pass.
FALSIFICATION_MIN_CHARS = 40


def _to_ledger_decision(decision):
    """P2-3: map a canonical/stack action (BUY/STARTER/ADD/WATCH/...) to the decision_ledger vocabulary
    {buy,trim,sell,top_up,PASS,hold} so log_decision never raises. Valid ledger decisions pass through;
    anything unrecognised defaults to PASS."""
    if _dl is not None and decision in getattr(_dl, "DECISIONS", set()):
        return decision
    if _alang is not None:
        mapped = _alang.to_ledger_decision(decision)
        if mapped:
            return mapped
    return "PASS"


REQUIRED_TOP = 5      # full comparative cases + pairwise required on the top-5 T1
LOG_ALL_N = 10        # log all 10 names (incl passes) to the ledger


def _norm_set(xs):
    return {str(x).strip().upper() for x in (xs or []) if str(x).strip()}


def validate_checkpoint_d(top5, decision, comparative_cases, logged_tickers, log_all_n=LOG_ALL_N,
                          step9_records=None, factor_state=None, opportunity_set=None):
    """Return {passed, blocks, chosen, top5}.
      top5              : ordered list of the top T1 tickers (Fix Pack A4: ALL T1 qualify;
                          cases capped at 5 by the deploy-Source tiebreak — this is that list)
      decision          : {chosen_ticker, chosen_action, pairwise:{other_ticker: justification}}
      comparative_cases : {ticker: case_text} — must cover all top-5
      logged_tickers    : tickers logged to the decision ledger this run (incl passes)
      step9_records     : optional {ticker: step9_pre record} — enables tick (4): a chosen BUY
                          whose stage_gate is BLOCKED_PENDING_CASE or late_cycle_flag is True
                          (A3/A15) must carry a documented cause (t1_gate_overrides recorded via
                          decision["documented_causes"][ticker] or the step9 record itself).
      falsification    : optional {ticker: statement} — tick (6). See _tick6 below.
    Any non-empty `blocks` list means the decision is BLOCKED."""
    blocks = []
    top5 = [str(t).strip().upper() for t in (top5 or []) if str(t).strip()][:REQUIRED_TOP]
    cases = {str(k).strip().upper(): v for k, v in (comparative_cases or {}).items()}
    logged = _norm_set(logged_tickers)
    decision = decision or {}
    # Step 10 is NOT capped at one action — accept a single chosen_ticker OR a list chosen_tickers;
    # deploy as many as clear the contest AND fit available funds.
    if decision.get("chosen_tickers"):
        chosen_set = _norm_set(decision["chosen_tickers"])
    elif decision.get("chosen_ticker"):
        chosen_set = {str(decision["chosen_ticker"]).strip().upper()}
    else:
        chosen_set = set()
    pairwise = {str(k).strip().upper(): v for k, v in (decision.get("pairwise") or {}).items()}

    # (8) ISA-0608 (23-Sep-2026): when the run's feasible population is supplied (the router's
    #     `opportunity_set` view), the contest IS that population's first five in capital order -
    #     not a hand-built list. Fewer than five feasible names is a complete contest, so the
    #     only deployable name can never be blocked for want of four infeasible companions.
    _req = REQUIRED_TOP
    if opportunity_set is not None:
        _pub = [str(t).strip().upper() for t in (opportunity_set.get("checkpoint_d_top5") or [])]
        if opportunity_set.get("checkpoint_d_top5") is None:
            blocks.append("ISA-0608: the supplied opportunity set carries no Checkpoint-D population "
                          f"(state={opportunity_set.get('state')}) - re-run the pre-run router")
        else:
            _req = min(REQUIRED_TOP, len(_pub))
            if set(top5) != set(_pub):
                blocks.append(f"ISA-0608: top-5 {top5} is not the run's feasible top-5 {_pub} "
                              f"(opportunity_set_id={opportunity_set.get('opportunity_set_id')})")
        if opportunity_set.get("state") == "PARITY_BREACH":
            blocks.append("ISA-0608: the router would fund main-route names outside the feasible "
                          f"population {opportunity_set.get('router_qualifying_not_feasible')}")
    if len(top5) < _req:
        blocks.append(f"need {_req} top-T1 names for Checkpoint-D, got {len(top5)}")

    # (1) full comparative case for every top-5 name
    for t in top5:
        if not str(cases.get(t) or "").strip():
            blocks.append(f"missing comparative case for top-5 name {t}")

    # (2) chosen action(s) in the top-5 + justify each NON-deployed top-5 name vs what WAS deployed.
    #     MULTIPLE deployments are allowed whenever they fit available funds — no single-action cap.
    if not chosen_set:
        blocks.append("no chosen action(s) in decision")
    for c in chosen_set:
        if c not in top5:
            blocks.append(f"chosen {c} is not among the top-5 {top5}")
    for t in top5:
        if t in chosen_set:
            continue
        if not str(pairwise.get(t) or "").strip():
            blocks.append(f"missing justification: why {sorted(chosen_set) or ['?']} chosen over {t}")

    # (2b) Optional funds check — many actions are FINE if they fit; this blocks ONLY when the total
    #      cost of the chosen actions exceeds available funds, never merely because there is >1 action.
    _funds = decision.get("available_funds")
    _costs = {str(k).strip().upper(): v for k, v in (decision.get("costs") or {}).items()}
    if _funds is not None and _costs:
        _total = sum((_costs.get(c) or 0) for c in chosen_set)
        if _total > _funds:
            blocks.append(f"chosen actions cost {_total} > available funds {_funds}")

    # (4) Fix Pack A3/A15 (P2): stage/late-cycle gate — a chosen name flagged
    #     BLOCKED_PENDING_CASE (stage in SUMMARY_STAGE_EXCLUDE) or late_cycle cannot be
    #     finalised without a DOCUMENTED cause (mirror of the reversal-gate mechanism).
    #     The machine verifies PRESENCE of the cause; its content is the review's judgment.
    _s9 = {str(k).strip().upper(): v for k, v in (step9_records or {}).items()}
    _causes = {str(k).strip().upper(): v for k, v in (decision.get("documented_causes") or {}).items()}
    for c in chosen_set:
        rec = _s9.get(c) or {}
        _ov = rec.get("t1_gate_overrides") or {}
        if rec.get("stage_gate") == "BLOCKED_PENDING_CASE":
            if not str(_causes.get(c) or _ov.get("stage") or "").strip():
                blocks.append(f"{c} stage_gate=BLOCKED_PENDING_CASE (A3/D2): document remaining "
                              f"runway in the Step-10 case (documented_causes[{c}]) before BUY")
        if rec.get("late_cycle_flag"):
            if not str(_causes.get(c) or _ov.get("late_cycle") or "").strip():
                blocks.append(f"{c} late_cycle_flag (A15): extended multiple + late stage — "
                              f"document the cause (documented_causes[{c}]) before BUY")

    # (5) Doc B B3: while the AI-complex factor cap is in BREACH, a chosen BUY that raises the
    #     factor weight is BLOCKED (factor_state = {"breach": bool, "classes": {ticker: 0/0.5/1}}).
    if factor_state and factor_state.get("breach"):
        _fc = {str(k).upper(): v for k, v in (factor_state.get("classes") or {}).items()}
        for c in chosen_set:
            if (_fc.get(c) or 0) > 0:
                blocks.append(f"{c} raises AI-complex weight while cap is in BREACH (B3/D15) — "
                              f"de-concentrate first or choose a non-factor name")

    # (6) FALSIFICATION TEST (WP-D, 29-Jul-2026 — assessment §5.3).
    #     Ticks 1-2 prove the chosen name beat the other four. They do NOT test whether all five
    #     were wrong: a comparative contest is a RELATIVE test and is blind to a shared error.
    #     This tick forces the disconfirming question BEFORE capital is committed, which is where
    #     the framework previously had no adversarial step at all (the full bull/bear case sits at
    #     Step 10.3, "Decided Action Only" — i.e. after the name has already won).
    #
    #     The machine verifies (a) a non-trivial statement EXISTS for each chosen name, and
    #     (b) that it ENGAGES with any disconfirming evidence the pre-run already surfaced —
    #     mirroring the tick-4 documented-cause mechanism. Its CONTENT is the review's judgment;
    #     this is a completeness gate, not a quality gate.
    _fals = {str(k).strip().upper(): v for k, v in (decision.get("falsification") or {}).items()}
    for c in sorted(chosen_set):
        stmt = str(_fals.get(c) or "").strip()
        if len(stmt) < FALSIFICATION_MIN_CHARS:
            blocks.append(f"{c} missing falsification test (tick 6): state what observable evidence "
                          f"would make this a mistake, whether any is already visible in the pre-run "
                          f"data, and what would trigger a thesis-break "
                          f"(decision['falsification']['{c}'], >= {FALSIFICATION_MIN_CHARS} chars)")
            continue
        # (b) if the pre-run already flagged disconfirming evidence, the statement must address it
        rec = _s9.get(c) or {}
        _rf = rec.get("risk_flags") or {}
        visible = []
        if rec.get("disqualifier_flags"):
            visible.append("disqualifier_flags")
        if _rf.get("delta_score_direction") in ("down", "falling", "deteriorating"):
            visible.append("delta_score_direction")
        if _rf.get("analyst_disparity"):
            visible.append("analyst_disparity")
        if _rf.get("binary_event_within_90d"):
            visible.append("binary_event_within_90d")
        if rec.get("recent_reversal_vs_12_1m") or _rf.get("recent_reversal_vs_12_1m"):
            visible.append("recent_reversal_vs_12_1m")
        if rec.get("review_flags"):
            visible.append("review_flags")
        if visible and not decision.get("falsification_addresses_flags", {}).get(c):
            low = stmt.lower()
            if not any(tok.split("_")[0] in low or tok.replace("_", " ") in low for tok in visible):
                blocks.append(f"{c} falsification test does not engage the disconfirming evidence the "
                              f"pre-run already surfaced ({', '.join(visible)}) — address it, or set "
                              f"decision['falsification_addresses_flags']['{c}']=True with the reason "
                              f"stated in the case (tick 6)")

    # (7) ISA-0607 (23-Sep-2026): the contest is between names capital can actually reach. A
    #     top-5 member the broker cannot deal ONLINE (its step9_pre record carries the ONE broker
    #     verdict) occupies a place that belongs to a feasible name - in Sep-2026 ZAB.WA took the
    #     fourth place. Only enforced where the record is supplied; an absent verdict on a
    #     supplied record is refused, never read as dealable.
    if _s9:
        for t in top5:
            rec = _s9.get(t)
            if rec is None:
                continue
            st = rec.get("broker_dealability")
            if st != "DEALABLE_ONLINE":
                blocks.append(f"{t} is not an executable destination (broker_dealability={st}, "
                              f"venue={rec.get('broker_venue')}) - draw the top-5 from "
                              f"step9_pre.deployable_stack (ISA-0607)")

    # (9) ISA-0616 (24-Sep-2026): C-1 CURRENT ADMISSIBILITY. A chosen name that adds capital (new
    #     entry OR top-up) must carry the ONE canonical verdict step9_pre stamped on its record
    #     (`c1_admissibility`, built by t1_gates.current_admissibility on its current snapshot).
    #     This replaces the manual Step 10.1(d) authority: the review STATES the verdict, it never
    #     computes one. Only enforced where records are supplied; a supplied record with no verdict
    #     is refused. A reduction (SELL/TRIM/REDUCE/EXIT) is never blocked by C-1.
    _REDUCE = {"SELL", "TRIM", "REDUCE", "EXIT"}
    _acts = {str(k).strip().upper(): str(v).strip().upper()
             for k, v in (decision.get("chosen_actions") or {}).items()}
    _gact = str(decision.get("chosen_action") or "").strip().upper()
    if _s9:
        for c in chosen_set:
            if (_acts.get(c) or _gact) in _REDUCE:
                continue
            _c1 = (_s9.get(c) or {}).get("c1_admissibility")
            if not isinstance(_c1, dict) or _c1.get("admissible") is not True:
                _vv = _c1.get("verdict") if isinstance(_c1, dict) else "ABSENT"
                blocks.append(f"{c} has no ADMISSIBLE current C-1 verdict (c1_admissibility={_vv}) - "
                              f"new capital refused (ISA-0616); the holding itself is unaffected")

    # (3) log all N (incl passes) — at minimum every top-5 name must be logged
    not_logged = [t for t in top5 if t not in logged]
    if not_logged:
        blocks.append(f"top-5 names not logged to decision ledger: {not_logged}")
    if len(logged) < min(log_all_n, len(top5)):
        blocks.append(f"only {len(logged)} names logged; log all {log_all_n} (incl PASSES)")

    return {"passed": not blocks, "blocks": blocks, "chosen": sorted(chosen_set) or None, "top5": top5}


def log_top10(ledger_path, ranked, decisions=None, route="growth", log_all_n=LOG_ALL_N, **ledger_kw):
    """Log the top-N deployment names (incl passes) to the decision ledger so 'the road not taken'
    is captured. ranked: ordered ticker list (e.g. deployment_priority_rank). decisions: {ticker:
    'buy'|'PASS'|'top_up'|...}; anything unlisted defaults to PASS. Returns the logged ticker set."""
    if _dl is None:
        raise RuntimeError("decision_ledger not importable — cannot log top-10")
    decisions = {str(k).upper(): v for k, v in (decisions or {}).items()}
    # Fix Pack A9 (P2): Checkpoint-D tick 3 is MACHINE-VERIFIED — capture the pre-write count,
    # then assert (a) file exists, (b) entry count increased or dedupe matched, (c) this month's
    # stamp is present. Failure raises -> Checkpoint-D BLOCKED (hard, per its charter). July's
    # silent-miss (prose-mandated write, never wired) cannot recur.
    from datetime import date as _date
    _pre = 0
    if os.path.exists(ledger_path):
        try:
            _pre = len(_dl.load_ledger(ledger_path).get("entries", []))
        except Exception:
            _pre = 0
    logged = []
    for t in (ranked or [])[:log_all_n]:
        d = decisions.get(str(t).upper(), "PASS")
        d = _to_ledger_decision(d)   # P2-3: map canonical/stack action -> ledger vocabulary (never raise)
        _dl.log_decision(ledger_path, t, route, d, **ledger_kw)
        logged.append(str(t).upper())
    if logged:
        if not os.path.exists(ledger_path):
            raise RuntimeError(f"A9 VERIFY FAILED: {ledger_path} does not exist after log_top10 — "
                               f"Checkpoint-D tick 3 BLOCKED")
        led = _dl.load_ledger(ledger_path)
        _post = len(led.get("entries", []))
        _stamp = (str(ledger_kw.get("date"))[:7] if ledger_kw.get("date")
                  else _date.today().strftime("%Y-%m"))   # honour an explicit run date
        _has_month = any(str(e.get("date", "")).startswith(_stamp) for e in led.get("entries", []))
        if _post < _pre or not _has_month:
            raise RuntimeError(f"A9 VERIFY FAILED: ledger count {_pre}->{_post}, month stamp "
                               f"{_stamp} present={_has_month} — Checkpoint-D tick 3 BLOCKED")
        print(f"LEDGER_VERIFIED entries={_post} (+{_post - _pre}) month={_stamp} path={ledger_path}")
    return logged


def _step9_records(spec):
    """ISA-0607: the CLI previously called the validator WITHOUT step9 records, so ticks 4, 6 and
    7 could never fire from the command line. A spec may carry `step9_records` directly or name
    the run's `step9_pre` file; records are then keyed by ticker from deployment_priority_rank."""
    if spec.get("step9_records"):
        return spec["step9_records"]
    p = spec.get("step9_pre")
    if not p:
        return None
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    return {r.get("ticker"): r for r in (doc.get("deployment_priority_rank") or [])
            if isinstance(r, dict) and r.get("ticker")}


def _opportunity_set(spec):
    """ISA-0608: the router's feasible-population view, from the spec or the run's run_context."""
    if spec.get("opportunity_set") is not None:
        return spec["opportunity_set"]
    p = spec.get("run_context")
    if not p:
        return None
    with open(p, encoding="utf-8") as fh:
        rc = json.load(fh)
    return ((((rc.get("summary") or {}).get("capital_destination") or {}).get("pipeline") or {})
            .get("opportunity_set"))


def _selftest() -> int:
    """ISA-0607 tick 7 through the REAL validator."""
    cases = {t: "case text" for t in ("HRMY", "HALO", "ABNB", "ZAB.WA", "NTAP", "ENX.PA")}
    dec = {"chosen_tickers": ["NTAP"], "pairwise": {t: "why" for t in cases},
           "falsification": {"NTAP": "x" * 200}}
    recs = {t: {"broker_dealability": "DEALABLE_ONLINE", "broker_venue": "NMS"} for t in cases}
    recs["ZAB.WA"] = {"broker_dealability": "NOT_DEALABLE_ONLINE", "broker_venue": "WSE"}
    sep = ["HRMY", "HALO", "ABNB", "ZAB.WA", "NTAP"]
    r = validate_checkpoint_d(sep, dec, cases, sep, step9_records=recs)
    assert any("ZAB.WA is not an executable destination" in b for b in r["blocks"]), \
        "MUST-FIRE (ISA-0607): the Sep-2026 top-5 containing ZAB.WA (WSE) must be blocked"
    feas = ["HRMY", "HALO", "ABNB", "ENX.PA", "NTAP"]
    r2 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=recs)
    assert not any("not an executable destination" in b for b in r2["blocks"]), \
        "NEGATIVE CONTROL (ISA-0607): a fully dealable top-5 must not be blocked by tick 7 %r" % r2
    recs2 = dict(recs, HALO={"broker_venue": None})
    r3 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=recs2)
    assert any("HALO is not an executable destination" in b for b in r3["blocks"]), \
        "MUST-FIRE (ISA-0607): a supplied record with NO verdict must refuse, never read as dealable"
    assert _step9_records({"step9_records": {"A": {}}}) == {"A": {}} and _step9_records({}) is None
    # ISA-0608 tick 8 — the Sep-2026 hand-built top-5 against the run's feasible population
    ops = {"opportunity_set_id": "OPS-fx", "state": "OK",
           "checkpoint_d_top5": ["HALO", "HRMY", "ABNB", "ENX.PA", "NTAP"]}
    r4 = validate_checkpoint_d(sep, dec, cases, sep, step9_records=recs, opportunity_set=ops)
    assert any("not the run's feasible top-5" in b for b in r4["blocks"]), \
        "MUST-FIRE (ISA-0608): a hand-built top-5 that differs from the feasible top-5 must be blocked"
    r5 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=recs,
                               opportunity_set=dict(ops, checkpoint_d_top5=feas))
    assert not any("ISA-0608" in b for b in r5["blocks"]), \
        "NEGATIVE CONTROL (ISA-0608): the published feasible top-5 must pass tick 8 %r" % r5["blocks"]
    one = {"opportunity_set_id": "OPS-1", "state": "OK", "checkpoint_d_top5": ["NTAP"]}
    r6 = validate_checkpoint_d(["NTAP"], dec, cases, ["NTAP"], step9_records=recs, opportunity_set=one)
    assert not any("need" in b or "ISA-0608" in b for b in r6["blocks"]), \
        "MUST-FIRE (ISA-0608 historical shape): the ONLY feasible name must not be blocked for want of 5 %r" % r6["blocks"]
    # ISA-0616 tick 9 — canonical C-1 on the chosen name(s)
    _A = {"verdict": "ADMISSIBLE", "admissible": True}
    rc = {t: dict(v, c1_admissibility=_A) for t, v in recs.items()}
    r7 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=rc)
    assert not any("ISA-0616" in b for b in r7["blocks"]), \
        "POSITIVE CONTROL (ISA-0616): an ADMISSIBLE chosen name passes tick 9 %r" % r7["blocks"]
    rs = dict(rc, NTAP=dict(rc["NTAP"], c1_admissibility={"verdict": "STALE", "admissible": False}))
    r8 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=rs)
    assert any("NTAP has no ADMISSIBLE current C-1" in b for b in r8["blocks"]), \
        "MUST-FIRE (ISA-0616): a STALE chosen BUY is blocked"
    r9 = validate_checkpoint_d(feas, dec, cases, feas, step9_records=recs)
    assert any("c1_admissibility=ABSENT" in b for b in r9["blocks"]), \
        "MUST-FIRE (ISA-0616): a supplied record with no C-1 verdict is refused, never admitted"
    r10 = validate_checkpoint_d(feas, dict(dec, chosen_actions={"NTAP": "TRIM"}), cases, feas, step9_records=rs)
    assert not any("ISA-0616" in b for b in r10["blocks"]), \
        "NEGATIVE CONTROL (ISA-0616): C-1 never blocks a risk/thesis REDUCTION"
    r11 = validate_checkpoint_d(feas, dict(dec, chosen_actions={"NTAP": "TOP_UP"}), cases, feas, step9_records=rs)
    assert any("NTAP has no ADMISSIBLE" in b for b in r11["blocks"]), \
        "MUST-FIRE (ISA-0616): the SAME verdict blocks a TOP-UP"
    print("checkpoint_d selftest OK (ISA-0607 tick 7 + ISA-0608 tick 8 + ISA-0616 tick 9: must-fire, negative control, absent verdict)")
    return 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    ap = argparse.ArgumentParser(description="Step-10 BLOCKING Checkpoint-D gate (E5).")
    ap.add_argument("--spec", required=True,
                    help="JSON: {top5:[...], decision:{chosen_ticker, chosen_action, pairwise:{}}, "
                         "comparative_cases:{}, logged_tickers:[...]}")
    a = ap.parse_args()
    with open(a.spec, encoding="utf-8") as fh:
        s = json.load(fh)
    res = validate_checkpoint_d(s.get("top5"), s.get("decision"),
                                s.get("comparative_cases"), s.get("logged_tickers"),
                                step9_records=_step9_records(s),
                                opportunity_set=_opportunity_set(s))
    print(json.dumps(res, indent=2))
    if not res["passed"]:
        print("\nBLOCKED — resolve the above before finalising the Step-10 decision.", file=sys.stderr)
        sys.exit(2)
    print("\nCHECKPOINT-D PASSED — decision may proceed.")


if __name__ == "__main__":
    main()
