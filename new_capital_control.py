#!/usr/bin/env python3
"""
new_capital_control.py — ISA-0807 / ISA-0805 / ISA-0804: ONE canonical, append-only control that
says whether NEW STOCK CAPITAL may be deployed this occurrence, and ONE stock-sleeve policy contract.

Authority: ChatGPT Astra 6 Audit/ISA_Integrated_Capital_Decision_Engine_BuildSpec_03Oct2026.md
(Raj, 03-Oct-2026) s1.2 (ISA-0805 policy contract) and s19.1 (deadline failure protocol:
"Emit a machine-consumed BLOCK_NEW_STOCK_CAPITAL occurrence state with exact failed contracts,
preserve deployable cash and explicit valid independent fund/holding/risk-reduction workflows ...
never ... an apparently normal old Source Score queue"). Rules: R4.1, R4.3, R4.4, R4.7, R4.13,
R7.7, R14.1, R14.2, R18.5.

WHY THIS EXISTS (ISA-0807). Before this module the only supported way to stop new stock capital was
the P4.7 rollback flag `isa_policy.V2_FLAGS['demand_pull_live'] = False`, which routes EVERY stock
pound to the fund sleeve. With the ISA-0390 recall leg barred until 01-Nov (override) / 01-Dec
(mechanical) that off-switch would itself make a 4-8 week allocation decision. A deferral must
defer: blocked stock capital is HELD AS CASH (the existing residual-routing/MMF rule prices it),
never handed to funds.

STATE CONTRACT (s12.2 vocabulary):
  Dashboard/state/new_capital_control.jsonl   APPEND_ONLY_HISTORY   owner/writer: this module's CLI
  Each record is hash-chained (prev_sha = sha256 of the previous raw line). An absent, unreadable,
  unparseable or broken-chain store reads UNKNOWN, and UNKNOWN BLOCKS (R4.3: a control fed a null
  never returns PASS). Runtime code never writes signed config.

SCOPE OF A BLOCK: every POSITIVE new stock exposure - new positions and top-ups, main (forward-led)
and VCI routes. NOT in scope (preserved): sells, trims, risk-reduction and thesis-break exits;
fund-sleeve rebalancing and switches that are funded inside the fund sleeve; cash/MMF handling.

LIFTING A BLOCK requires a named acceptance reference (the SYSTEM_ACCEPTANCE receipt of the
replacement capital path). A lift with no reference is refused (R4.7).

ROLLBACK (R4.13): append an OPEN record with acceptance reference 'ROLLBACK:<reason>'; or restore
the preceding Trusted Build, whose router does not read this store (the store is then inert history).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_REL = os.path.join("Dashboard", "state", "new_capital_control.jsonl")
SCHEMA_VERSION = "1.0.0"

BLOCK = "BLOCK_NEW_STOCK_CAPITAL"
OPEN = "OPEN"
UNKNOWN = "UNKNOWN"
RECORD_STATES = (BLOCK, OPEN)
READ_STATES = (BLOCK, OPEN, UNKNOWN)
SCOPE = "ALL_POSITIVE_NEW_STOCK_EXPOSURE"
SCOPE_DETAIL = ("new positions AND top-ups on every stock route (forward-led main route and VCI). "
                "Sells, trims, risk-reduction and thesis exits, fund-sleeve rebalances funded inside "
                "the fund sleeve, and cash/MMF handling are NOT blocked.")
CASH_TREATMENT = ("HELD_AS_CASH_PENDING_STOCK_DECISION - blocked stock capital is not routed to the "
                  "fund sleeve (the ISA-0390 recall leg is barred, so routing it to funds would make "
                  "an allocation decision the block exists to defer); the residual-routing / MMF rule "
                  "prices and places it as cash.")

STATE_CONTRACT = {
    "path": STORE_REL.replace(os.sep, "/"),
    "class": "APPEND_ONLY_HISTORY",
    "owner": "new_capital_control",
    "writers": ["new_capital_control.append_record (CLI --block / --open)"],
    "readers": ["capital_destination.sleeve_split", "checkpoint_d.main (CLI)",
                "build_monthly_isa_email.main", "Run_Context Step 10 (CLI --status)"],
    "absent_or_corrupt": "UNKNOWN -> blocks new stock capital",
}

# Policy states (ISA-0805)
POLICY_RESOLVED = "RESOLVED"
POLICY_CONFLICT = "POLICY_AUTHORITY_CONFLICT"
POLICY_UNKNOWN = "UNKNOWN"
NOT_A_POLICY_LIMIT = "NOT_A_POLICY_LIMIT"


class ControlRefused(Exception):
    """A write that would make the control ambiguous or unevidenced (R4.7)."""


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def store_path(root: str = HERE) -> str:
    return os.path.join(root, STORE_REL)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0805 — ONE STOCK-SLEEVE POLICY CONTRACT (s1.2)
# ═══════════════════════════════════════════════════════════════════════════════════════════
def stock_sleeve_policy(tw: Optional[dict] = None, root: str = HERE) -> dict:
    """-> the canonical stock_sleeve_policy contract, read from target_weights.json (signed config).

    hard_max is a policy LIMIT on new stock capital only when the signed config declares one.
    `sleeve_ceiling: null` with a basis recording its removal is NOT_A_POLICY_LIMIT - an explicit
    state, different from a missing required risk constraint. Any other shape is a conflict or
    unknown and blocks new stock capital through status()."""
    src_path = os.path.join(root, "target_weights.json")
    if tw is None:
        try:
            with open(src_path, encoding="utf-8") as fh:
                tw = json.load(fh)
        except Exception as exc:                                        # noqa: BLE001
            return {"policy_id": "STOCK_SLEEVE_POLICY", "state": POLICY_UNKNOWN,
                    "why": "target_weights.json unreadable (%s: %s)" % (type(exc).__name__, exc)}
    ss = tw.get("stock_sleeve") or {}
    th = tw.get("thresholds") or {}
    meta = tw.get("_meta") or {}
    blob = json.dumps({"stock_sleeve": ss, "thresholds": th}, sort_keys=True, default=str)
    conflicts, unknowns = [], []

    if "sleeve_ceiling" not in ss:
        unknowns.append("target_weights.stock_sleeve.sleeve_ceiling is ABSENT - neither a limit nor "
                        "a recorded removal (R4.1)")
        hard = {"state": POLICY_UNKNOWN}
    elif ss.get("sleeve_ceiling") is None:
        basis = str(ss.get("sleeve_ceiling_basis") or "")
        if "REMOVED" in basis.upper():
            hard = {"state": NOT_A_POLICY_LIMIT, "value": None, "basis": basis[:400],
                    "source": "target_weights.stock_sleeve.sleeve_ceiling = null + sleeve_ceiling_basis",
                    "decision_refs": ["clean spec s2 (26-Aug-2026)", "ISA-0421 DECISION", "D20"]}
        else:
            conflicts.append("sleeve_ceiling is null but its basis does not record a removal - an "
                             "undeclared null cannot be read as 'no limit'")
            hard = {"state": POLICY_CONFLICT}
    else:
        try:
            v = float(ss["sleeve_ceiling"])
            hard = {"state": "HARD_LIMIT", "value": v,
                    "basis": str(ss.get("sleeve_ceiling_basis") or "")[:400],
                    "source": "target_weights.stock_sleeve.sleeve_ceiling"}
            if "REMOVED" in str(ss.get("sleeve_ceiling_basis") or "").upper():
                conflicts.append("sleeve_ceiling carries a value (%s) while its basis records it "
                                 "REMOVED" % v)
        except (TypeError, ValueError):
            conflicts.append("sleeve_ceiling %r is not a number" % (ss.get("sleeve_ceiling"),))
            hard = {"state": POLICY_CONFLICT}

    lo, hi = ss.get("phase1_target_low"), ss.get("phase1_target_high")
    band_basis = str(ss.get("phase1_target_basis") or "")
    target_range = {"value_pct": [None if lo is None else round(lo * 100, 4),
                                  None if hi is None else round(hi * 100, 4)],
                    "role": ("REPORTING_ONLY" if "REPORTING" in band_basis.upper() else UNKNOWN),
                    "basis": band_basis[:300], "source": "target_weights.stock_sleeve.phase1_target_*"}
    if target_range["role"] != "REPORTING_ONLY":
        unknowns.append("phase1 band role is not declared REPORTING in its basis")

    removed = ss.get("_removed_caps_26aug2026") or {}
    trig_live = th.get("phase_transition_pct")
    transition = {
        "value_pct": None if trig_live is None else round(float(trig_live) * 100, 4),
        "live_consumers": ["portfolio_analytics phase_status (reporting)",
                           "Run_Context Step 6.7 (fund-weight Phase-2 transition)"],
        "role": "FUND_WEIGHT_TRANSITION_TRIGGER - not a cap on new stock capital",
        "config_note": ("stock_sleeve._removed_caps_26aug2026 records the transition test as removed "
                        "with the ceiling, while thresholds.phase_transition_pct is still read by "
                        "portfolio_analytics and Run_Context 6.7" if removed and trig_live is not None
                        else None),
        "admission_effect": "NONE",
    }
    # ⚑ The transition question is a FUND-sleeve policy question (it re-targets fund weights after
    #   two months >= trigger). It does not bear on whether a pound may enter the stock sleeve, so
    #   its internal inconsistency is reported (and registered) without blocking stock admission.
    risk_limits = {"max_stock_position_pct": th.get("max_stock_position_pct"),
                   "sizing_ladder_pct": ss.get("sizing_ladder_pct"),
                   "cash_reserve_gbp": ss.get("cash_reserve_gbp"),
                   "note": ("route/position/correlation/concentration limits are owned by "
                            "position_sizing, deployment_sequencer and concentration_control")}
    state = (POLICY_CONFLICT if conflicts else POLICY_UNKNOWN if unknowns else POLICY_RESOLVED)
    return {
        "policy_id": "STOCK_SLEEVE_POLICY",
        "version": "%s|%s" % (ss.get("policy_version"), meta.get("last_updated")),
        "schema_version": SCHEMA_VERSION,
        "source": "target_weights.json",
        "source_hash": _sha(blob)[:16],
        "state": state,
        "hard_max": hard,
        "target_range": target_range,
        "transition_trigger": transition,
        "risk_limits": risk_limits,
        "breach_route": ("NOT_APPLICABLE - there is no hard sleeve maximum; a weight above the "
                         "reporting band is published, not trimmed" if hard.get("state") == NOT_A_POLICY_LIMIT
                         else "REFUSE the addition that would cross hard_max" if hard.get("state") == "HARD_LIMIT"
                         else "BLOCK new stock capital until the authority is resolved"),
        "conflicts": conflicts,
        "unknowns": unknowns,
        "effective_at": "2026-08-26",
        "basis": ("ISA-0805 resolution (03-Oct-2026): the signed config and ISA-0421 agree the 15% "
                  "Phase-1 figure is a REPORTING band and not a cap; contradicting Run_Context prose "
                  "was reconciled in the same change."),
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════
# THE CONTROL STORE
# ═══════════════════════════════════════════════════════════════════════════════════════════
def read_records(root: str = HERE) -> dict:
    """-> {records:[...], state_of_store: OK|ABSENT|CORRUPT, errors:[...]}. Never raises."""
    p = store_path(root)
    if not os.path.exists(p):
        return {"records": [], "state_of_store": "ABSENT", "errors": ["store absent: %s" % STORE_REL]}
    recs, errs = [], []
    prev_sha = None
    try:
        with open(p, encoding="utf-8") as fh:
            raw_lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
    except Exception as exc:                                            # noqa: BLE001
        return {"records": [], "state_of_store": "CORRUPT",
                "errors": ["unreadable: %s: %s" % (type(exc).__name__, exc)]}
    for i, ln in enumerate(raw_lines):
        try:
            r = json.loads(ln)
        except ValueError as exc:
            errs.append("line %d unparseable: %s" % (i + 1, exc))
            break
        if r.get("prev_sha") != prev_sha:
            errs.append("line %d hash chain broken (prev_sha %r, expected %r) - history was edited"
                        % (i + 1, r.get("prev_sha"), prev_sha))
            break
        if r.get("state") not in RECORD_STATES:
            errs.append("line %d state %r outside %s" % (i + 1, r.get("state"), RECORD_STATES))
            break
        recs.append(r)
        prev_sha = _sha(ln)
    return {"records": recs, "state_of_store": "CORRUPT" if errs else "OK", "errors": errs,
            "tail_sha": prev_sha}


def status(root: str = HERE, policy: Optional[dict] = None) -> dict:
    """The one reading every consumer uses. `blocks_new_stock_capital` is True unless the latest
    valid record is OPEN AND the stock-sleeve policy is RESOLVED."""
    rd = read_records(root)
    pol = policy if policy is not None else stock_sleeve_policy(root=root)
    last = rd["records"][-1] if rd["records"] else None
    reasons = []
    if rd["state_of_store"] != "OK":
        st = UNKNOWN
        reasons.append("control store %s: %s" % (rd["state_of_store"], "; ".join(rd["errors"])[:300]))
    else:
        st = last["state"]
    if pol.get("state") != POLICY_RESOLVED:
        reasons.append("stock_sleeve_policy %s: %s" % (pol.get("state"),
                       "; ".join((pol.get("conflicts") or []) + (pol.get("unknowns") or []) +
                                 ([pol.get("why")] if pol.get("why") else []))[:300]))
    blocks = (st != OPEN) or (pol.get("state") != POLICY_RESOLVED)
    # ⚑ ISA-0828 (R18.6, 04-Oct-2026): an OPEN control is only as good as the acceptance it cites. Before
    #   this, status() reported OPEN from the last record and never asked whether its SYSTEM_ACCEPTANCE was
    #   still CURRENT, so an intersecting promotion left new stock capital OPEN on a stale acceptance.
    #   The cited acceptance must be CURRENT for the monthly capital path (or a governed ROLLBACK reference);
    #   anything else - STALE, unreadable - BLOCKS (R4.3). A fixture book with no Trusted receipt is
    #   UNGOVERNED_ROOT (capital_run_authority REFUSES such a tree upstream: NO_RECEIPT).
    acceptance = None
    if st == OPEN:
        try:
            import system_acceptance as _sa828
            acceptance = _sa828.acceptance_currency((last or {}).get("acceptance_ref"), root)
        except Exception as exc:                                        # noqa: BLE001
            acceptance = {"state": "UNKNOWN", "why": "acceptance currency unreadable (%s: %s)" % (type(exc).__name__, exc)}
        if acceptance.get("state") not in ("CURRENT", "ROLLBACK", "UNGOVERNED_ROOT"):
            blocks = True
            reasons.insert(0, "ACCEPTANCE_NOT_CURRENT (ISA-0828, R18.6): the OPEN record cites %s, which is %s - %s. "
                              "Renew system acceptance on the delivered build and re-open citing it."
                           % ((last or {}).get("acceptance_ref"), acceptance.get("state"), str(acceptance.get("why"))[:300]))
    if st == BLOCK and last:
        reasons.insert(0, "BLOCK_NEW_STOCK_CAPITAL recorded %s by %s: %s"
                       % (last.get("recorded_at"), last.get("recorded_by"), last.get("reason")))
    return {
        "control": "new_capital_control",
        "schema_version": SCHEMA_VERSION,
        "state": st,
        "blocks_new_stock_capital": bool(blocks),
        "scope": SCOPE if blocks else None,
        "scope_detail": SCOPE_DETAIL if blocks else None,
        "cash_treatment": CASH_TREATMENT if blocks else None,
        "failed_contracts": (last or {}).get("failed_contracts") if st == BLOCK else [],
        "occurrence": (last or {}).get("occurrence"),
        "record_id": (last or {}).get("record_id"),
        "acceptance_ref": (last or {}).get("acceptance_ref") if st == OPEN else None,
        "acceptance": acceptance,
        "reasons": reasons,
        "policy_state": pol.get("state"),
        "policy_hard_max": (pol.get("hard_max") or {}).get("state"),
        "store": STATE_CONTRACT["path"],
        "n_records": len(rd["records"]),
    }


def append_record(root: str = HERE, *, state: str, occurrence: str, reason: str, recorded_by: str,
                  failed_contracts=None, acceptance_ref: Optional[str] = None,
                  now: Optional[datetime.datetime] = None) -> dict:
    """The ONLY writer. Refuses an ambiguous record (R4.7)."""
    if state not in RECORD_STATES:
        raise ControlRefused("state %r outside %s" % (state, RECORD_STATES))
    if not str(reason or "").strip() or not str(recorded_by or "").strip():
        raise ControlRefused("a control record needs a reason and who recorded it")
    if not str(occurrence or "").strip():
        raise ControlRefused("a control record names the occurrence it was set for (e.g. 2026-10-04)")
    fc = [str(x).strip() for x in (failed_contracts or []) if str(x).strip()]
    if state == BLOCK and not fc:
        raise ControlRefused("BLOCK_NEW_STOCK_CAPITAL must name >= 1 failed contract (s19.1: 'with "
                             "exact failed contracts')")
    if state == OPEN and not str(acceptance_ref or "").strip():
        raise ControlRefused("lifting the block requires a named acceptance reference (the "
                             "SYSTEM_ACCEPTANCE receipt of the replacement capital path, or "
                             "'ROLLBACK:<reason>')")
    rd = read_records(root)
    if rd["state_of_store"] == "CORRUPT":
        raise ControlRefused("store is CORRUPT (%s) - repair is a governed act, not an append"
                             % "; ".join(rd["errors"]))
    now = now or _now()
    seq = len(rd["records"]) + 1
    rec = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "NCC-%s-%03d" % (now.strftime("%Y%m%d"), seq),
        "state": state,
        "occurrence": str(occurrence),
        "scope": SCOPE,
        "failed_contracts": fc,
        "acceptance_ref": (str(acceptance_ref).strip() if acceptance_ref else None),
        "reason": str(reason).strip(),
        "recorded_by": str(recorded_by).strip(),
        "recorded_at": now.isoformat(timespec="seconds"),
        "prev_sha": rd.get("tail_sha"),
    }
    p = store_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    line = json.dumps(rec, sort_keys=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    back = read_records(root)
    if back["state_of_store"] != "OK" or not back["records"] or back["records"][-1]["record_id"] != rec["record_id"]:
        raise ControlRefused("write did not read back cleanly: %s" % back["errors"])
    return rec


def blocks_new_stock_capital(root: str = HERE) -> bool:
    return bool(status(root)["blocks_new_stock_capital"])


# ═══════════════════════════════════════════════════════════════════════════════════════════
def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    fails = []

    def ok(name, cond, detail=None):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond or detail is None else " :: %r" % (detail,)))

    base = tempfile.mkdtemp(prefix="ncc_selftest_")
    try:
        os.makedirs(os.path.join(base, "Dashboard", "state"))
        # policy fixtures
        tw_ok = {"_meta": {"last_updated": "2026-08-20"},
                 "thresholds": {"phase_transition_pct": 0.15, "max_stock_position_pct": 0.065},
                 "stock_sleeve": {"sleeve_ceiling": None, "policy_version": "ISA_V2_1",
                                  "sleeve_ceiling_basis": "REMOVED as policy, 26-Aug-2026",
                                  "phase1_target_low": 0.10, "phase1_target_high": 0.15,
                                  "phase1_target_basis": "RETAINED FOR REPORTING ONLY",
                                  "_removed_caps_26aug2026": {"transition_trigger_pct": 0.15},
                                  "cash_reserve_gbp": 250}}
        with open(os.path.join(base, "target_weights.json"), "w") as fh:
            json.dump(tw_ok, fh)
        p = stock_sleeve_policy(root=base)
        ok("ISA-0805 MUST-FIRE: a recorded removal is NOT_A_POLICY_LIMIT and RESOLVED",
           p["state"] == POLICY_RESOLVED and p["hard_max"]["state"] == NOT_A_POLICY_LIMIT, p)
        ok("ISA-0805: the 15% transition trigger is reported with admission_effect NONE",
           p["transition_trigger"]["value_pct"] == 15.0 and p["transition_trigger"]["admission_effect"] == "NONE")
        bad = json.loads(json.dumps(tw_ok)); bad["stock_sleeve"]["sleeve_ceiling"] = 0.15
        ok("NEGATIVE CONTROL: a ceiling value with a REMOVED basis is POLICY_AUTHORITY_CONFLICT",
           stock_sleeve_policy(bad)["state"] == POLICY_CONFLICT)
        hard = json.loads(json.dumps(tw_ok)); hard["stock_sleeve"]["sleeve_ceiling"] = 0.20
        hard["stock_sleeve"]["sleeve_ceiling_basis"] = "Raj decision X: hard cap"
        hp = stock_sleeve_policy(hard)
        ok("ISA-0805: an explicit later hard cap is a HARD_LIMIT, RESOLVED",
           hp["state"] == POLICY_RESOLVED and hp["hard_max"]["state"] == "HARD_LIMIT" and hp["hard_max"]["value"] == 0.20, hp)
        miss = json.loads(json.dumps(tw_ok)); del miss["stock_sleeve"]["sleeve_ceiling"]
        ok("NEGATIVE CONTROL: an absent sleeve_ceiling key is UNKNOWN, never 'no limit'",
           stock_sleeve_policy(miss)["state"] == POLICY_UNKNOWN)
        und = json.loads(json.dumps(tw_ok)); und["stock_sleeve"]["sleeve_ceiling_basis"] = "tbd"
        ok("NEGATIVE CONTROL: a null ceiling without a recorded removal is a CONFLICT",
           stock_sleeve_policy(und)["state"] == POLICY_CONFLICT)

        # store fixtures
        s0 = status(base)
        ok("NEGATIVE CONTROL: an ABSENT store reads UNKNOWN and BLOCKS (R4.3)",
           s0["state"] == UNKNOWN and s0["blocks_new_stock_capital"] is True, s0)
        try:
            append_record(base, state=BLOCK, occurrence="2026-10-04", reason="x", recorded_by="t")
            ok("NEGATIVE CONTROL: BLOCK without a failed contract must fail", False)
        except ControlRefused:
            ok("NEGATIVE CONTROL: BLOCK without a failed contract must fail", True)
        try:
            append_record(base, state=OPEN, occurrence="2026-10-04", reason="x", recorded_by="t")
            ok("NEGATIVE CONTROL: OPEN without an acceptance reference must fail", False)
        except ControlRefused:
            ok("NEGATIVE CONTROL: OPEN without an acceptance reference must fail", True)
        r1 = append_record(base, state=BLOCK, occurrence="2026-10-04", reason="engine not accepted",
                           recorded_by="selftest", failed_contracts=["ISA-0804 constructor"])
        s1 = status(base)
        ok("MUST-FIRE: a BLOCK record blocks and names its failed contracts",
           s1["blocks_new_stock_capital"] and s1["state"] == BLOCK and s1["failed_contracts"] == ["ISA-0804 constructor"], s1)
        ok("MUST-FIRE: a block declares cash treatment HELD_AS_CASH and the full scope",
           s1["cash_treatment"].startswith("HELD_AS_CASH") and s1["scope"] == SCOPE)
        append_record(base, state=OPEN, occurrence="2026-10-11", reason="engine accepted",
                      recorded_by="selftest", acceptance_ref="SA-TEST-1")
        s2 = status(base)
        ok("POSITIVE CONTROL (control is not vacuous): OPEN + RESOLVED policy does not block",
           s2["blocks_new_stock_capital"] is False and s2["acceptance_ref"] == "SA-TEST-1", s2)
        ok("NEGATIVE CONTROL: OPEN record + POLICY_AUTHORITY_CONFLICT still blocks",
           status(base, policy=stock_sleeve_policy(bad))["blocks_new_stock_capital"] is True)
        # ── ISA-0828 (R18.6): OPEN is only as good as the acceptance it cites ───────────────────────
        import system_acceptance as _sa_t
        _orig_ac = _sa_t.acceptance_currency
        try:
            _sa_t.acceptance_currency = lambda ref, root=None, path_id=None: {"state": "STALE", "why": "fixture: intersecting change"}
            _s = status(base)
            ok("ISA-0828 MUST-FIRE (handoff case 16): an OPEN record citing a STALE acceptance BLOCKS new stock capital",
               _s["blocks_new_stock_capital"] is True and _s["reasons"][0].startswith("ACCEPTANCE_NOT_CURRENT"), _s)
            _sa_t.acceptance_currency = lambda ref, root=None, path_id=None: {"state": "CURRENT", "why": "fixture"}
            ok("ISA-0828 NEGATIVE CONTROL: the same OPEN record citing a CURRENT acceptance does not block",
               status(base)["blocks_new_stock_capital"] is False)

            def _boom(ref, root=None, path_id=None):
                raise RuntimeError("store unreadable")
            _sa_t.acceptance_currency = _boom
            ok("ISA-0828 FAIL-CLOSED: an unreadable acceptance BLOCKS (R4.3)",
               status(base)["blocks_new_stock_capital"] is True)
        finally:
            _sa_t.acceptance_currency = _orig_ac
        # tamper: edit history -> chain breaks -> UNKNOWN -> blocks
        sp = store_path(base)
        with open(sp, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        tampered = json.loads(lines[0]); tampered["reason"] = "edited"
        with open(sp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(tampered, sort_keys=True) + "\n" + "\n".join(lines[1:]) + "\n")
        s3 = status(base)
        ok("NEGATIVE CONTROL: an edited history breaks the hash chain, reads UNKNOWN and BLOCKS",
           s3["state"] == UNKNOWN and s3["blocks_new_stock_capital"], s3)
        try:
            append_record(base, state=OPEN, occurrence="x", reason="r", recorded_by="b", acceptance_ref="A")
            ok("NEGATIVE CONTROL: appending to a CORRUPT store must fail", False)
        except ControlRefused:
            ok("NEGATIVE CONTROL: appending to a CORRUPT store must fail", True)
        ok("record ids are sequential and dated", r1["record_id"].startswith("NCC-"))
        # T64 / ISA-0805 consistency pair through its real function
        try:
            sys.path.insert(0, HERE)
            import consistency_check as _cc
            _pol = stock_sleeve_policy(tw_ok)
            _stale = ("AND (c) total stock sleeve (main + asymmetric combined) does not breach the "
                      "Phase 1 ceiling (15% of ISA).")
            _e1 = _cc.pair_stock_sleeve_policy_single_home(run_context_text=_stale, policy=_pol)
            ok("MUST-FIRE (T64): Run_Context calling 15% a stock ceiling fails the single-home pair",
               any("CEILING" in e for e in _e1), _e1)
            _e2 = _cc.pair_stock_sleeve_policy_single_home(
                run_context_text="the 10-15% band is REPORTING ONLY", policy=_pol)
            ok("NEGATIVE CONTROL (T64): reconciled prose passes the single-home pair",
               not any("CEILING" in e for e in _e2), _e2)
            _e3 = _cc.pair_stock_sleeve_policy_single_home(
                run_context_text="x", policy=stock_sleeve_policy(bad))
            ok("MUST-FIRE: a POLICY_AUTHORITY_CONFLICT is an ERROR in the pair",
               any("POLICY_AUTHORITY_CONFLICT" in e for e in _e3), _e3)
        except ImportError as exc:
            ok("consistency_check importable for the T64 pair", False, str(exc))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if verbose:
        print("new_capital_control selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    def opts(name):
        return [argv[i + 1] for i, a in enumerate(argv) if a == name and i + 1 < len(argv)]
    root = opt("--root", HERE)
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--policy" in argv:
        print(json.dumps(stock_sleeve_policy(root=root), indent=1, default=str))
        return 0
    if "--open" in argv:
        # ISA-0812: an OPEN cites a CURRENT system acceptance of the delivered build (or a governed rollback).
        _acc = str(opt("--acceptance") or "")
        if not _acc.startswith("ROLLBACK:"):
            try:
                import system_acceptance as _sa
                _cur = _sa.current(root)
            except Exception as exc:                                    # noqa: BLE001
                _cur = {"state": "UNREADABLE", "why": str(exc)}
            if _cur.get("state") != "CURRENT" or _cur.get("sa_id") != _acc.split()[0]:
                print("NCC_REFUSED --open needs the CURRENT system acceptance id of the LIVE build (got %r; current %s)"
                      % (_acc, json.dumps(_cur, default=str)))
                return 2
    if "--block" in argv or "--open" in argv:
        try:
            rec = append_record(root, state=(BLOCK if "--block" in argv else OPEN),
                                occurrence=opt("--occurrence"), reason=opt("--reason"),
                                recorded_by=opt("--by"), failed_contracts=opts("--failed-contract"),
                                acceptance_ref=opt("--acceptance"))
        except ControlRefused as exc:
            print("NCC_REFUSED %s" % exc)
            return 2
        print("NCC_RECORDED %s" % json.dumps(rec, sort_keys=True))
        return 0
    st = status(root)
    print(json.dumps(st, indent=1, default=str))
    return 3 if st["blocks_new_stock_capital"] else 0


if __name__ == "__main__":
    sys.exit(main())
