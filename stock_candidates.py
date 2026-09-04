#!/usr/bin/env python3
"""
stock_candidates.py — P3. THE candidate list that reaches `position_sizing.stock_max`.

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md P3 (ISA-0459).
Built 28-Aug-2026.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHY THIS MODULE EXISTS — nothing built a candidate list, so a fixture did
═══════════════════════════════════════════════════════════════════════════════════════════
`position_sizing.stock_max` is the demand-pull rule and the only thing that decides how much
capital a qualified use pulls. Measured on the delivered tree 27-Aug-2026, **its only caller
was `capital_destination._stock_side_sensitivity`, which passes ONE synthetic `_PROBE`
candidate that self-labels *"not a size anyone should act on"*.** The real router took its
number from `derive_stock_max` instead — a band-floor computation ten times smaller.

**A pipeline was specified, never written, and a fixture stood in for it.**

═══════════════════════════════════════════════════════════════════════════════════════════
⚑⚑ THE FIELD THAT MUST NEVER BE DEFAULTED, AND WHY IT IS THE WHOLE MODULE
═══════════════════════════════════════════════════════════════════════════════════════════
`stock_max` treats `qualifies: False` as *did not qualify* and routes the pound to funds.
**A DEFAULT False AND A MEASURED REJECTION ARE INDISTINGUISHABLE IN THE OUTPUT AND OPPOSITE
IN MEANING** (R2.10). So `qualifies` is never defaulted: a name whose gate verdict cannot be
obtained REFUSES, and `qualifies: False` without a NAMED reason REFUSES.

The same rule runs through the record: `er_ca_margin_pp` is `None`, never `0.0` (R4.1);
`correlation` is never `None` — the record states its own basis, including
`UNMEASURED_ADVERSE_DEFAULT`; an unsourced evidence channel is `None`, never `False` (D22).

⚑ **`current_value_gbp` COMES FROM `portfolio_data` — BROKER TRUTH — AND FROM NOTHING ELSE.**
A `watchlist_scored` row that disagrees LOSES. That is the ONT defect in its original form:
£18,471.20 published against a broker truth of £997.92.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["stock_candidate_pipeline"] = False` ⇒ `build()`
returns `state: DISABLED` with an empty list and a stated reason, and `capital_destination`
falls back to its pre-P4 path.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None


class CandidatesRefused(RuntimeError):
    """The pipeline cannot state a candidate's qualification honestly.

    ⚑ NEVER downgraded to `qualifies: False`. That value means *the gate ran and rejected it*,
    and the pound then routes to funds on the strength of a verdict nobody reached."""


# ⚑ DECLARED, not incidental (P3.4). Under P7/D21 the ranking key is `source_score` — NOT the
# retired /100. A7's lesson: the buy rule and the sell rule are two rules, so the buy-side
# order lives here and is NOT `capital_destination.donor_order` reversed.
RANKING_BASIS = "source_score"

ROUTES = ("main", "vci")


def _flag(name: str = "stock_candidate_pipeline", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


def _broker_values(portfolio_data: Optional[dict]) -> Dict[str, float]:
    """ticker -> current value in GBP, from BROKER TRUTH ONLY.

    ⚑ The LSE suffix is restored from the broker's own `full_name` ("(LSE:ONT)"), because the
    bare form is the shape that once resolved to a different company entirely."""
    out: Dict[str, float] = {}
    for h in ((portfolio_data or {}).get("stocks") or []):
        t = (h.get("ticker") or "").strip()
        if not t:
            continue
        if "LSE:" in (h.get("full_name") or "") and not t.endswith(".L"):
            t += ".L"
        out[t] = float(h.get("value_gbp") or 0.0)
    return out


def _correlation_record(ticker: str, assessment: Optional[dict]) -> dict:
    """NEVER None (P3-A1). The record always states its own basis."""
    a = assessment or {}
    for bucket in ("candidates", "holdings"):
        rec = (a.get(bucket) or {}).get(ticker)
        if rec:
            return {"measured": bool(rec.get("measured")),
                    "rho_sleeve": rec.get("rho_sleeve"),
                    "rho_max_pairwise": rec.get("rho_max_pairwise"),
                    "rho_basis": rec.get("rho_basis") or "UNSTATED",
                    "admission": rec.get("admission")}
    # ⚑ A2.3's ADVERSE default — declared, not silent, and it CAPS rather than sizing.
    return {"measured": False, "rho_sleeve": None, "rho_max_pairwise": None,
            "rho_basis": "UNMEASURED_ADVERSE_DEFAULT", "admission": None,
            "note": ("no correlation record for this name in the run's assessment. The "
                     "adverse default applies and the position is capped at STARTER — a "
                     "MEASURED REFUSAL, not an estimate.")}


def _er_margin_pp(row: dict, deploy_floor_pct: Optional[float]) -> Optional[float]:
    """E[r] less the deploy floor, in pp. ⚑ `None`, NEVER 0.0 (R4.1) — a missing margin and a
    margin of exactly zero are different facts and only one of them is a decision."""
    er = row.get("expected_return_12_24m")
    if er is None or deploy_floor_pct is None:
        return None
    try:
        return round(float(er) - float(deploy_floor_pct), 3)
    except (TypeError, ValueError):
        return None


def _qualifies(row: dict, route: str) -> tuple:
    """-> (qualifies, reason). REFUSES rather than defaulting (P3-A2).

    Reads the verdict the GATES already computed — `t1_qualified` for the main route,
    `vci_deploy_eligible` for VCI. It does not re-derive either: a second computation of a
    qualification verdict would be a second home for the rule (R4.4)."""
    if route == "vci":
        q = row.get("vci_deploy_eligible")
        src = "vci_deploy_eval.vci_deploy_eligible"
    else:
        q = row.get("t1_qualified")
        src = "t1_gates.t1_qualified"
    if q is None:
        raise CandidatesRefused(
            "%s: %s is absent, so this name's qualification verdict is UNKNOWN. Defaulting it "
            "to False would route its pound to funds on a verdict nobody reached — and a "
            "default False is indistinguishable in the output from a measured rejection "
            "(R2.10). Run the gate, or exclude the name explicitly."
            % (row.get("ticker"), src))
    if q:
        return True, None
    reason = (row.get("forward_ineligible_reason") or row.get("t1_gate_detail_summary")
              or row.get("disqualified_reason") or row.get("er_floor_status")
              or row.get("decision_bucket"))
    if not reason:
        raise CandidatesRefused(
            "%s: qualifies is False and NO gate named the reason. 'It failed' without 'which "
            "gate' is an adjective, not a measurement — and it is unreviewable next month."
            % row.get("ticker"))
    return False, str(reason)


def build(*, portfolio_data: Optional[dict] = None, step9_pre: Optional[dict] = None,
          watchlist_scored=None, vci_deploy=None,
          correlation_assessment: Optional[dict] = None,
          weekly_returns: Optional[dict] = None, policy: Optional[dict] = None,
          fetch_universe: Optional[Sequence[str]] = None,
          thesis_states: Optional[Dict[str, dict]] = None,
          evidence_states: Optional[Dict[str, str]] = None,
          underfilled: Optional[Dict[str, dict]] = None,
          held_topups: Optional[Sequence[dict]] = None,
          deploy_floor_pct: Optional[float] = None,
          today: Optional[str] = None) -> dict:
    """THE candidate list. Every field states where it came from and refuses where it cannot."""
    _fi_mark("stock_candidates", "build")
    if not _flag():
        return {"state": "DISABLED", "candidates": [], "qualifying": [],
                "ranking_basis": RANKING_BASIS,
                "binding": "pipeline_disabled",
                "detail": ("rollback: V2_FLAGS['stock_candidate_pipeline'] is False. This is a "
                           "REFUSAL, not an empty result — `binding` says so, because an empty "
                           "list because nothing qualified and an empty list because the "
                           "pipeline did not run are different facts (R2.10).")}
    today = today or datetime.date.today().isoformat()
    values = _broker_values(portfolio_data)
    rows: List[dict] = []
    for key, route in (("deployable_stack", "main"), ("vci_watchlist", "vci")):
        for r in ((step9_pre or {}).get(key) or []):
            if isinstance(r, dict) and r.get("ticker"):
                rows.append((r, route))
    seen, cands, refusals = set(), [], []
    for r, route in rows:
        tk = r["ticker"]
        if tk in seen:
            continue
        seen.add(tk)
        try:
            q, reason = _qualifies(r, route)
        except CandidatesRefused as exc:
            # ⚑ CAUGHT PER CANDIDATE, NEVER SWALLOWED. One unadjudicable row must not lose the
            # other thirty — but a name that DISAPPEARS from the list while `binding` still
            # reads as though the list were complete is exactly FC-E, so the result below is
            # stamped REFUSED_PARTIAL and `binding` says `unadjudicated_present`. The name is
            # never admitted with `qualifies: False`, which is the property that matters:
            # a default False and a measured rejection are opposite in meaning (R2.10).
            refusals.append({"ticker": tk, "reason": str(exc)})
            continue
        corr = _correlation_record(tk, correlation_assessment)
        ts = (thesis_states or {}).get(tk) or {}
        cands.append({
            "ticker": tk, "route": route,
            "qualifies": q, "disqualified_reason": reason,
            # ⚑ BROKER TRUTH ONLY. 0.0 for a name the broker does not hold is a MEASURED zero
            # — it is the position size, not a missing value.
            "current_value_gbp": round(values.get(tk, 0.0), 2),
            "evidence_state": (evidence_states or {}).get(tk) or r.get("evidence_state"),
            "thesis_state": ts.get("state"),
            "thesis_state_rationale": ts.get("rationale"),
            "correlation": corr,
            "er_ca_margin_pp": _er_margin_pp(r, deploy_floor_pct),
            "band": r.get("decision_bucket") or r.get("tier"),
            "underfilled_obligation": (underfilled or {}).get(tk),
            RANKING_BASIS: r.get(RANKING_BASIS),
            "ranking_basis": RANKING_BASIS,
            "_source": "step9_pre." + ("vci_watchlist" if route == "vci"
                                       else "deployable_stack"),
        })

    # ── HELD TOP-UPS ARE CAPITAL DESTINATIONS TOO (ISA-0563, 02-Sep-2026) ──────────────────
    # ⚑ WHAT THIS CLOSES. The population above is step9_pre's `deployable_stack` + VCI list,
    # and `update_watchlist` REMOVES a name from the watchlist the moment it is bought. So a
    # position, once held, could never again be a destination for a pound: the router ranked
    # only names Raj does not own. Meanwhile `rerank_watchlist` has scored held and candidate
    # names on ONE Source Score since 04-Jul-2026 and tags each held name add_worthy /
    # retain_only / dead_money — the July build record's "gap 4: action_stack is the SUPERSET
    # for capital decisions" — and nothing ever consumed it. Measured 02-Sep-2026: MU scored
    # 67.8 against the 65 fresh-capital bar and was tagged add_worthy; the router never saw it.
    # ⚑ NO NEW POLICY IS INTRODUCED HERE and that is deliberate. The bar (APS_FRESH_CAPITAL_BAR),
    # the penalty a top-up carries against a fresh buy (APS_TOPUP_PENALTY), the replacement
    # margin (UPGRADE_DELTA) and the SIZE of the top-up (position_sizing.stock_max computes
    # ladder-target x NAV MINUS current value, which is a gap for a held name and a whole
    # position for a new one) are all already declared and already built. This wires the two.
    # ⚑ ONLY `add_worthy` QUALIFIES. retain_only means "own it, do not add" and dead_money means
    # "should not own it"; admitting either as a capital destination would invert the verdict.
    # A held name whose evidence state cannot be resolved is REFUSED BY NAME, never defaulted —
    # sizing keys off the rung and a guessed state would size real money (R2.10).
    for _h in (held_topups or []):
        _tk = (_h or {}).get("ticker")
        if not _tk or _tk in seen:
            continue
        seen.add(_tk)
        _axis = (_h.get("held_axis") or "").strip().lower()
        if _axis != "add_worthy":
            refusals.append({"ticker": _tk, "reason":
                             "held_axis=%r is not a capital destination; only add_worthy is "
                             "(retain_only = own it, add nothing; dead_money = should not own "
                             "it)." % (_h.get("held_axis"),)})
            continue
        _ev = (evidence_states or {}).get(_tk) or _h.get("evidence_state")
        if not _ev:
            refusals.append({"ticker": _tk, "reason":
                             "held top-up admitted by the action stack but its evidence_state "
                             "is unresolved, and the ladder rung — hence the top-up SIZE — is "
                             "keyed on it. Refused by name rather than sized on a default."})
            continue
        if _h.get(RANKING_BASIS) is None:
            refusals.append({"ticker": _tk, "reason":
                             "held top-up carries no %s, and the router orders on it (P3.4)."
                             % RANKING_BASIS})
            continue
        _ts = (thesis_states or {}).get(_tk) or {}
        cands.append({
            "ticker": _tk, "route": "held_topup",
            "qualifies": True,
            "disqualified_reason": None,
            "current_value_gbp": round(values.get(_tk, 0.0), 2),
            "evidence_state": _ev,
            "thesis_state": _ts.get("state"),
            "thesis_state_rationale": _ts.get("rationale"),
            "correlation": _correlation_record(_tk, correlation_assessment),
            "er_ca_margin_pp": None,
            "band": _h.get("action") or _h.get("action_label"),
            "underfilled_obligation": (underfilled or {}).get(_tk),
            RANKING_BASIS: _h.get(RANKING_BASIS),
            "ranking_basis": RANKING_BASIS,
            "held_axis": _axis,
            "_source": "action_stack.held_axis=add_worthy",
        })

    # ⚑ P3.4 — the ranking key is DECLARED and every candidate must carry it.
    missing_key = [c["ticker"] for c in cands if c.get(RANKING_BASIS) is None]
    if missing_key:
        raise CandidatesRefused(
            "ranking_basis is %r and %d candidate(s) do not carry it: %s. A list ordered by a "
            "key some members lack is ordered by accident, and P6 would then band on it."
            % (RANKING_BASIS, len(missing_key), missing_key[:12]))

    # ⚑ P3.5 — ISA-0022 containment. A candidate with no series is a FETCH GAP to be fixed,
    # not a design outcome, so it is named rather than quietly dropped.
    off_universe = []
    if fetch_universe is not None:
        uni = set(fetch_universe)
        off_universe = sorted(c["ticker"] for c in cands if c["ticker"] not in uni)

    qualifying = [c for c in cands if c["qualifies"]]
    rejected = [c for c in cands if not c["qualifies"]]
    # ⚑ P3-A7 — AN EMPTY LIST AND AN ALL-REJECTED LIST ARE DIFFERENT FACTS. Both give
    # stock_max == 0; the artefact must state WHICH, or the two are one output and two
    # meanings (R2.10).
    if refusals:
        # ⚑ THE LIST IS INCOMPLETE AND SAYS SO. Sizing on a list with unadjudicated names is
        # sizing on a list you cannot read the length of.
        binding = "unadjudicated_present"
    elif not cands:
        binding = "no_candidates_built"
    elif not qualifying:
        binding = "all_candidates_rejected"
    else:
        binding = "qualified_demand"
    return {
        "state": ("REFUSED_PARTIAL" if refusals else "OK"), "as_of": today,
        "candidates": cands, "qualifying": qualifying, "rejected": rejected,
        "n_candidates": len(cands), "n_qualifying": len(qualifying),
        "n_rejected": len(rejected),
        "binding": binding,
        "ranking_basis": RANKING_BASIS,
        "refusals": refusals, "n_refused": len(refusals),
        "off_fetch_universe": off_universe,
        "containment_ok": not off_universe,
        "rejected_reasons": {c["ticker"]: c["disqualified_reason"] for c in rejected},
        "detail": ("P3. `qualifies` is READ from the gate that computed it and is NEVER "
                   "defaulted; a False without a named reason REFUSES. current_value_gbp is "
                   "broker truth. correlation is never None. er_ca_margin_pp is None, never "
                   "0.0. Ranking basis is DECLARED as %r — not the retired /100." % RANKING_BASIS),
    }


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)[:200]) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    PD = {"stocks": [{"ticker": "ONT", "full_name": "Oxford Nanopore (LSE:ONT)",
                      "value_gbp": 997.92},
                     {"ticker": "AVGO", "full_name": "Broadcom (NASDAQ:AVGO)",
                      "value_gbp": 4915.78}]}
    CORR = {"candidates": {"NEW": {"measured": True, "rho_sleeve": 0.21,
                                   "rho_max_pairwise": 0.33, "rho_basis": "MEASURED_SHRUNK"}},
            "holdings": {}}

    def s9(rows):
        return {"deployable_stack": rows}

    good = {"ticker": "NEW", "t1_qualified": True, "source_score": 71.2,
            "expected_return_12_24m": 18.4, "decision_bucket": "DEPLOY"}
    r = build(portfolio_data=PD, step9_pre=s9([good]), correlation_assessment=CORR,
              deploy_floor_pct=15.8, fetch_universe=["NEW", "ONT.L", "AVGO"])
    ok("a qualifying candidate reaches the list", r["n_qualifying"] == 1 and r["state"] == "OK")
    ok("P3-A1 correlation is never None and states its basis",
       r["candidates"][0]["correlation"]["rho_basis"] == "MEASURED_SHRUNK")
    ok("P3-A1-neg a name with NO correlation record still gets one, adverse and declared",
       _correlation_record("ZZZ", CORR)["rho_basis"] == "UNMEASURED_ADVERSE_DEFAULT")
    ok("er_ca_margin_pp computed", abs(r["candidates"][0]["er_ca_margin_pp"] - 2.6) < 1e-6)
    ok("er_ca_margin_pp is None, NEVER 0.0, when E[r] is absent",
       _er_margin_pp({"expected_return_12_24m": None}, 15.8) is None)
    ok("...and None when the FLOOR is absent (R4.1 both ways)",
       _er_margin_pp({"expected_return_12_24m": 18.4}, None) is None)

    # P3-A2 — qualifies is never defaulted
    unadj = build(step9_pre=s9([{"ticker": "X", "source_score": 1}]))
    ok("P3-A2 an ABSENT gate verdict is REFUSED, never admitted as qualifies:False",
       unadj["n_candidates"] == 0 and unadj["n_refused"] == 1
       and unadj["refusals"][0]["ticker"] == "X", unadj)
    ok("P3-A2 ...and the artefact says the list is INCOMPLETE rather than looking complete",
       unadj["state"] == "REFUSED_PARTIAL" and unadj["binding"] == "unadjudicated_present",
       (unadj["state"], unadj["binding"]))
    ok("P3-A2 ...which is DISTINCT from an empty list and from an all-rejected list",
       len({unadj["binding"], "no_candidates_built", "all_candidates_rejected"}) == 3)
    ok("P3-A2 `_qualifies` itself RAISES, so the contract is directly testable",
       _raises(lambda: _qualifies({"ticker": "X"}, "main"), CandidatesRefused))
    ok("P3-A2-neg a clean list is state OK with no refusals",
       r["state"] == "OK" and r["n_refused"] == 0)
    ok("P3-A2 qualifies:False with NO named reason REFUSES",
       _raises(lambda: _qualifies({"ticker": "X", "t1_qualified": False}, "main"),
               CandidatesRefused))
    ok("P3-A2-neg qualifies:False WITH a named reason is accepted and the reason travels",
       _qualifies({"ticker": "X", "t1_qualified": False,
                   "forward_ineligible_reason": "E[r] below floor"}, "main")
       == (False, "E[r] below floor"))

    # P3-A6 — broker truth wins
    v = _broker_values(PD)
    ok("P3-A6 current_value_gbp is broker truth, and the LSE suffix is restored",
       v.get("ONT.L") == 997.92 and "ONT" not in v, v)
    held = build(portfolio_data=PD, step9_pre=s9([dict(good, ticker="ONT.L")]),
                 correlation_assessment=CORR, fetch_universe=["ONT.L"])
    ok("P3-A6 a held name carries its BROKER value, not a scored-row value",
       held["candidates"][0]["current_value_gbp"] == 997.92)

    # P3-A4 — ranking basis declared and present
    ok("P3-A4 a candidate missing the ranking key RAISES",
       _raises(lambda: build(step9_pre=s9([{"ticker": "Y", "t1_qualified": True}])),
               CandidatesRefused))
    ok("P3-A4 ranking_basis is source_score, NOT the retired /100",
       r["ranking_basis"] == "source_score" != "strategic_conviction_score")

    # P3-A5 — ISA-0022 containment
    off = build(step9_pre=s9([good]), correlation_assessment=CORR, fetch_universe=["OTHER"])
    ok("P3-A5 a candidate outside the fetch universe is NAMED",
       off["off_fetch_universe"] == ["NEW"] and not off["containment_ok"])
    ok("P3-A5-neg containment_ok when the candidate is in the universe", r["containment_ok"])

    # ⚑ P3-A7 — empty vs all-rejected are DIFFERENT
    empty = build(step9_pre=s9([]), correlation_assessment=CORR)
    allrej = build(step9_pre=s9([{"ticker": "R", "t1_qualified": False, "source_score": 40,
                                  "forward_ineligible_reason": "Part A below floor"}]),
                   correlation_assessment=CORR)
    ok("P3-A7 an EMPTY list and an ALL-REJECTED list produce DIFFERENT binding values",
       empty["binding"] == "no_candidates_built"
       and allrej["binding"] == "all_candidates_rejected"
       and empty["binding"] != allrej["binding"],
       (empty["binding"], allrej["binding"]))
    ok("P3-A7 both still yield zero qualifying uses",
       empty["n_qualifying"] == 0 and allrej["n_qualifying"] == 0)
    ok("P3-A7 the all-rejected case names a reason PER rejected ticker",
       allrej["rejected_reasons"] == {"R": "Part A below floor"})

    # P3-A3 — an unsourced channel is None, never False
    ok("P3-A3 an unsourced evidence_state stays None, never False",
       build(step9_pre=s9([good]), correlation_assessment=CORR
             )["candidates"][0]["evidence_state"] is None)

    # rollback
    import isa_policy as _p
    prev = _p.V2_FLAGS.get("stock_candidate_pipeline")
    _p.V2_FLAGS["stock_candidate_pipeline"] = False
    d = build(step9_pre=s9([good]))
    ok("rollback: DISABLED is a stated REFUSAL, not an empty result",
       d["state"] == "DISABLED" and d["binding"] == "pipeline_disabled")
    _p.V2_FLAGS["stock_candidate_pipeline"] = True
    ok("rollback-neg flag True ⇒ the pipeline builds again (control is not vacuous)",
       build(step9_pre=s9([good]), correlation_assessment=CORR)["state"] == "OK")
    if prev is None:
        _p.V2_FLAGS.pop("stock_candidate_pipeline", None)
    else:
        _p.V2_FLAGS["stock_candidate_pipeline"] = prev

    print("\nstock_candidates selftest: %d assertion(s), %d FAIL(s)%s"
          % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


_ASSERTS = [0]


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:                                                   # noqa: BLE001
        return False
    return False


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _o = print

        def print(*a, **k):                                             # noqa: A001
            if a and isinstance(a[0], str) and a[0].startswith(("  PASS", "  FAIL")):
                _ASSERTS[0] += 1
            _o(*a, **k)
        sys.exit(_selftest())
    print(json.dumps({"ranking_basis": RANKING_BASIS, "routes": list(ROUTES)}, indent=1))
