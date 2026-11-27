#!/usr/bin/env python3
"""
risk_review_state.py - P8 stabilisation (ISA-0915 / ISA-0916, 25-Nov-2026): Raj's STANDING risk authority and
persistent risk-review episodes. Policy stays in isa_policy.RISK_APPETITE; this module records EVENTS and
AUTHORITY only and is never a second threshold home (R4.4).

THE APPROVED DESIGN IT IMPLEMENTS (Approved_Risk_Design_Content_19Nov2026.md, sha abfa15b2...; Raj 19-Nov "ok i
approve the redesign"):
  1. Standing authority. Raj's ORIGINAL permanent severe-limit approval (01-Nov-2026, live in session: "I approve
     any losses above the severe crash scenario limit of 50%" - permanent; recorded in the Checkpoint-D spec
     _review_nov2026/checkpoint_d_spec_nov2026.json decision.appetite_approval, receipt CDR-nov_2026-9a863550e717)
     is preserved with provenance and revocation state, WITHOUT monthly renewal or a new expiry. It is separate
     from approval of an investment action (capital_approval_ledger) and from valid data/account/release
     authority (execution_check). It is never narrowed retrospectively.
  2. Separate measures. The severe scenario (whole_book_stress SEVERE_GFC_SCALE) is a DIAGNOSTIC: a positive
     severe-loss delta or a new month alone never demands a fresh waiver while the standing authority is VALID.
     The ACTUAL flow-adjusted peak drawdown (wealth_record) is the observed measure: reaching the declared 50%
     opens ONE persistent review episode, and the standing severe authority does NOT cover it.
  5. Persistent reviews. A known exposure stays visible in the same episode (cumulative change vs the accepted
     reference) - no rolling reset legitimises repeated small additions; a changed policy (limit/basis) puts the
     authority OUT_OF_SCOPE and the guard needs a new decision.

THE STORE: Dashboard/state/risk_review_episodes.d/ - one immutable record file per event via
durable_store.write_record (P1: the only validated multi-writer append on the shared OneDrive mount; a .jsonl
single-write append interleaves - ISA-0936). Event kinds:
  STANDING_AUTHORITY_GRANTED  (migration record: migrated=True, original_machine_judgement=False - the 01-Nov
                               approval was a human decision recorded in a session spec, not a machine judgement)
  STANDING_AUTHORITY_REVOKED  (Raj only)
  EPISODE_OPENED / EPISODE_CLOSED  (one open episode per (constraint, policy fingerprint) at a time)

THE ONE PERMISSION FUNCTION: appetite_permission(guard, receipt_id, decision_approval=None) is read by
Checkpoint-D (tick 14 and the typed no-addition test), capital_decision_engine.approve_action, execution_check
and the receipt/email line, so every consumer reaches the same answer (ISA-0916: execution_check/approve_action
previously never re-checked the appetite).

ROLLBACK (R4.13): STANDING_AUTHORITY_ENFORCED = False -> appetite_permission ignores the standing authority and
every REVIEW_REQUIRED plan needs the per-receipt approval again (the pre-P8 tick-14 behaviour); the store and the
episodes are still read and published.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STORE_DIR = os.path.join("Dashboard", "state", "risk_review_episodes.d")
STANDING_AUTHORITY_ENFORCED = True          # ROLLBACK (R4.13): False -> per-receipt approval only (pre-P8)

SEVERE = "SEVERE_SCENARIO_APPETITE"         # the diagnostic guard (whole_book_stress.guard / book verdict)
REALISED = "REALISED_DRAWDOWN_APPETITE"     # the observed flow-adjusted peak drawdown measure

MIGRATION_ID = "RRA-2026-11-01-severe-standing"
MIGRATION_SOURCE = "_review_nov2026/checkpoint_d_spec_nov2026.json"
MIGRATION_QUOTE = ("Raj Jobanputra, live in session 01-Nov-2026: 'I approve any losses above the severe crash "
                   "scenario limit of 50%' - permanent; Raj will redesign the control")
MIGRATION_RECEIPT = "CDR-nov_2026-9a863550e717"
# the accepted reference at approval: receipt CDR-nov_2026-9a863550e717 appetite_guard.selected (as formed 01-Nov)
MIGRATION_REFERENCE = {"pre_loss_gbp": 82213.72, "post_loss_gbp": 88155.28, "pre_loss_pct": 52.749,
                       "post_loss_pct": 56.562, "scenario": "SEVERE_GFC_SCALE", "appetite_pct": 50.0}

try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                   # noqa: BLE001
    def _fi_mark(*_a, **_k):
        return None


def _store(root: str) -> str:
    return os.path.join(root, STORE_DIR)


def policy_fingerprint(policy: Optional[dict] = None) -> Optional[str]:
    """The parts of isa_policy.RISK_APPETITE an authority is scoped to. A different limit, basis or response is a
    different policy: the old authority does not cover it (approved design point 5)."""
    if policy is None:
        try:
            import isa_policy as _p
            policy = dict(getattr(_p, "RISK_APPETITE"))
        except Exception:                                           # noqa: BLE001
            return None
    try:
        core = {"max_drawdown_pct": float(policy["max_drawdown_pct"]), "basis": str(policy["basis"]),
                "response": str(policy["response"])}
    except Exception:                                               # noqa: BLE001
        return None
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def read_events(root: str = HERE) -> dict:
    """Every event, oldest first, plus any CORRUPT_SEGMENT (typed, never dropped - FC-I)."""
    import durable_store as _ds
    d = _store(root)
    if not os.path.isdir(d):
        return {"state": "ABSENT", "events": [], "corrupt": []}
    recs = _ds.read_records(d)
    good = [r for r in recs if isinstance(r, dict) and r.get("kind") != _ds.CORRUPT_SEGMENT and r.get("event")]
    bad = [r for r in recs if not (isinstance(r, dict) and r.get("event"))]
    good.sort(key=lambda r: (str(r.get("recorded_at") or ""), str(r.get("event_id") or "")))
    return {"state": "CORRUPT" if bad else "OK", "events": good, "corrupt": bad}


def _write(root: str, rec: dict) -> dict:
    import durable_store as _ds
    os.makedirs(_store(root), exist_ok=True)
    return _ds.write_record(_store(root), rec["event_id"], rec)


def seed_standing_authority(root: str = HERE, now: Optional[dt.datetime] = None) -> dict:
    """Idempotent migration record of Raj's 01-Nov permanent approval. Reads the original spec and REFUSES to seed
    if the quoted approval is not in it (no invented authority). Provenance names the source bytes' sha256."""
    src = os.path.join(root, MIGRATION_SOURCE)
    try:
        with open(src, "rb") as fh:
            raw = fh.read()
        ap = (json.loads(raw.decode("utf-8")).get("decision") or {}).get("appetite_approval") or {}
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "REFUSED_SOURCE_UNREADABLE", "why": "%s: %s" % (type(exc).__name__, exc)}
    if ap.get("approved_by") != MIGRATION_QUOTE or ap.get("receipt_id") != MIGRATION_RECEIPT:
        return {"state": "REFUSED_SOURCE_MISMATCH", "why": "the 01-Nov spec does not carry the quoted permanent approval",
                "found": ap}
    rec = {"event_id": MIGRATION_ID, "event": "STANDING_AUTHORITY_GRANTED", "constraint": SEVERE,
           "scope": {"policy_fingerprint": policy_fingerprint(), "covers": "severe-scenario diagnostic guard "
                     "(whole_book_stress.guard REVIEW_REQUIRED and BOOK_EXCEEDS_APPETITE)",
                     "does_not_cover": ["REALISED_DRAWDOWN_APPETITE (actual flow-adjusted drawdown)",
                                        "investment assent (capital_approval_ledger)", "data/account/release authority",
                                        "any other constraint"]},
           "granted_by": "Raj", "granted_on": "2026-11-01", "words": MIGRATION_QUOTE, "expiry": None,
           "renewal": "NONE (permanent; approved design point 1)", "receipt_at_grant": MIGRATION_RECEIPT,
           "accepted_reference": dict(MIGRATION_REFERENCE),
           "provenance": {"source": MIGRATION_SOURCE, "source_sha256": hashlib.sha256(raw).hexdigest(),
                          "migrated": True, "original_machine_judgement": False,
                          "migrated_by": "P8 stabilisation (ISA-0915/0916)",
                          "note": "a human decision recorded in the 01-Nov Checkpoint-D spec; this record preserves it, "
                                  "it does not pretend a machine judgement existed"},
           "recorded_at": "2026-11-01T00:00:00+00:00",
           "migrated_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")}
    for e in read_events(root)["events"]:
        if e.get("event_id") == MIGRATION_ID:
            return {"state": "ALREADY_SEEDED", "event_id": MIGRATION_ID}
    _write(root, rec)
    return {"state": "SEEDED", "event_id": MIGRATION_ID}


def standing_authority(constraint: str = SEVERE, root: str = HERE, policy: Optional[dict] = None,
                       events: Optional[list] = None) -> dict:
    """VALID | REVOKED | OUT_OF_SCOPE (policy changed since the grant) | ABSENT | CORRUPT (store unreadable -> never
    treated as authority, R4.3)."""
    _fi_mark("risk_review_state", "standing_authority")
    if events is None:
        ev = read_events(root)
        if ev["state"] == "CORRUPT":
            return {"state": "CORRUPT", "why": "%d unreadable risk-review record(s)" % len(ev["corrupt"])}
        events = ev["events"]
    grants = [e for e in events if e.get("event") == "STANDING_AUTHORITY_GRANTED" and e.get("constraint") == constraint]
    if not grants:
        return {"state": "ABSENT", "constraint": constraint}
    g = grants[-1]
    rev = [e for e in events if e.get("event") == "STANDING_AUTHORITY_REVOKED" and e.get("revokes") == g.get("event_id")]
    base = {"constraint": constraint, "authority_id": g.get("event_id"), "granted_on": g.get("granted_on"),
            "words": g.get("words"), "accepted_reference": g.get("accepted_reference"),
            "migrated": (g.get("provenance") or {}).get("migrated")}
    if rev:
        return dict(base, state="REVOKED", revoked_on=rev[-1].get("recorded_at"), revoked_by=rev[-1].get("revoked_by"))
    fp_now = policy_fingerprint(policy)
    fp_g = (g.get("scope") or {}).get("policy_fingerprint")
    if fp_now is None or fp_now != fp_g:
        return dict(base, state="OUT_OF_SCOPE", why="isa_policy.RISK_APPETITE changed since the grant (fingerprint %s "
                    "-> %s): a changed policy needs Raj's new decision" % (fp_g, fp_now))
    return dict(base, state="VALID")


def appetite_permission(guard: Optional[dict], receipt_id: Optional[str], *, decision_approval: Optional[dict] = None,
                        root: str = HERE, authority: Optional[dict] = None) -> dict:
    """THE one answer to 'may this plan's appetite REVIEW proceed?'.
    NOT_REQUIRED (guard not REVIEW_REQUIRED) | STANDING_AUTHORITY (VALID grant covering the severe guard) |
    RECEIPT_APPROVAL (decision.appetite_approval naming this receipt) | REFUSED_APPETITE_APPROVAL_ABSENT.
    `authority` is a test injection only."""
    _fi_mark("risk_review_state", "appetite_permission")
    st = (guard or {}).get("state")
    if st != "REVIEW_REQUIRED":
        return {"state": "NOT_REQUIRED", "guard_state": st or "NOT_EVALUATED"}
    sa = authority if authority is not None else standing_authority(SEVERE, root)
    sel = (guard or {}).get("selected") or {}
    cum = None
    ref = sa.get("accepted_reference") or {}
    if sel.get("post_loss_gbp") is not None and ref.get("post_loss_gbp") is not None:
        cum = {"post_loss_gbp_now": sel.get("post_loss_gbp"), "post_loss_gbp_at_grant": ref.get("post_loss_gbp"),
               "change_since_grant_gbp": round(float(sel["post_loss_gbp"]) - float(ref["post_loss_gbp"]), 2),
               "attribution": "UNATTRIBUTED (trade/market/cash/model split not yet built - ISA-0967)"}
    if STANDING_AUTHORITY_ENFORCED and sa.get("state") == "VALID":
        return {"state": "STANDING_AUTHORITY", "authority_id": sa.get("authority_id"), "granted_on": sa.get("granted_on"),
                "words": sa.get("words"), "cumulative": cum,
                "why": "Raj's permanent severe-limit authority (%s) covers this severe-scenario REVIEW; no fresh waiver is "
                       "needed (approved design point 2). Investment assent is still separate." % sa.get("granted_on")}
    ap = decision_approval or {}
    if str(ap.get("approved_by") or "").strip() and ap.get("receipt_id") == receipt_id:
        return {"state": "RECEIPT_APPROVAL", "approved_by": ap.get("approved_by"), "receipt_id": receipt_id,
                "standing_authority": sa.get("state"), "cumulative": cum}
    return {"state": "REFUSED_APPETITE_APPROVAL_ABSENT", "standing_authority": sa.get("state"),
            "standing_why": sa.get("why"), "cumulative": cum,
            "why": "the plan raises the severe-scenario loss to %s%% against the %s%% limit; standing authority is %s%s and "
                   "no approval names receipt %s" % (sel.get("post_loss_pct"), sel.get("appetite_pct"), sa.get("state"),
                                                       "" if STANDING_AUTHORITY_ENFORCED else " (not enforced: rollback)",
                                                       receipt_id)}


def open_episode(constraint: str, root: str = HERE, events: Optional[list] = None) -> Optional[dict]:
    events = read_events(root)["events"] if events is None else events
    fp = policy_fingerprint()
    opened = [e for e in events if e.get("event") == "EPISODE_OPENED" and e.get("constraint") == constraint
              and (e.get("scope") or {}).get("policy_fingerprint") == fp]
    closed = {e.get("closes") for e in events if e.get("event") == "EPISODE_CLOSED"}
    live = [e for e in opened if e.get("event_id") not in closed]
    return live[-1] if live else None


def observe_realised(realised: Optional[dict], root: str = HERE, *, write: bool = False,
                     now: Optional[dt.datetime] = None) -> dict:
    """The OBSERVED measure. realised = whole_book_stress appetite_verdict.realised. At/beyond the limit opens ONE
    persistent episode (never a second while one is open; never re-opened monthly). Below the limit an open episode
    stays open until Raj closes it (a recovery is evidence, not a decision). UNMEASURED never opens or closes."""
    _fi_mark("risk_review_state", "observe_realised")
    v = (realised or {}).get("verdict")
    ev = read_events(root)
    if ev["state"] == "CORRUPT":
        return {"state": "CORRUPT", "why": "risk-review store unreadable - the realised review state is UNKNOWN"}
    cur = open_episode(REALISED, root, ev["events"])
    if v == "UNMEASURED" or v is None:
        return {"state": "UNMEASURED", "open_episode": cur}
    if v == "REALISED_AT_OR_BEYOND_APPETITE":
        if cur:
            return {"state": "EPISODE_CONTINUES", "episode_id": cur["event_id"], "opened_on": cur.get("recorded_at"),
                    "current_drawdown_pct": realised.get("current_drawdown_pct")}
        t = (now or dt.datetime.now(dt.timezone.utc))
        rec = {"event_id": "RRE-%s-realised" % t.strftime("%Y%m%dT%H%M%S"), "event": "EPISODE_OPENED",
               "constraint": REALISED, "scope": {"policy_fingerprint": policy_fingerprint()},
               "reference": {"current_drawdown_pct": realised.get("current_drawdown_pct"),
                             "basis": realised.get("basis"), "as_of": realised.get("as_of")},
               "escalation": "Raj - a new decision (the standing severe authority does not cover the actual drawdown)",
               "recorded_at": t.isoformat(timespec="seconds")}
        if write:
            _write(root, rec)
        return {"state": "EPISODE_OPENED" if write else "WOULD_OPEN_EPISODE", "episode_id": rec["event_id"],
                "record": rec}
    return {"state": "WITHIN_APPETITE", "open_episode": cur}


def _selftest(verbose: bool = True) -> int:
    import shutil
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %r" % (detail,)))
        if not cond:
            fails.append(name)
    tmp = tempfile.mkdtemp(prefix="rrs_selftest_")
    try:
        os.makedirs(os.path.join(tmp, "_review_nov2026"))
        spec = {"decision": {"appetite_approval": {"approved_by": MIGRATION_QUOTE, "receipt_id": MIGRATION_RECEIPT}}}
        with open(os.path.join(tmp, MIGRATION_SOURCE), "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        guard = {"state": "REVIEW_REQUIRED", "selected": {"post_loss_pct": 57.1, "appetite_pct": 50.0,
                                                          "post_loss_gbp": 89000.0}}
        p0 = appetite_permission(guard, "CDR-dec", root=tmp)
        ok("NEGATIVE CONTROL: no standing authority and no receipt approval -> REVIEW refuses",
           p0["state"] == "REFUSED_APPETITE_APPROVAL_ABSENT" and p0["standing_authority"] == "ABSENT", p0)
        s1 = seed_standing_authority(tmp)
        s2 = seed_standing_authority(tmp)
        ok("seed: migration record written once (idempotent)", s1["state"] == "SEEDED" and s2["state"] == "ALREADY_SEEDED",
           (s1, s2))
        ev = read_events(tmp)["events"]
        ok("seed: provenance says migrated, not an original machine judgement",
           ev and ev[0]["provenance"]["migrated"] is True and ev[0]["provenance"]["original_machine_judgement"] is False)
        p1 = appetite_permission(guard, "CDR-dec", root=tmp)
        ok("MUST-FIRE: a NEW month's REVIEW_REQUIRED plan passes on the permanent authority (no fresh waiver)",
           p1["state"] == "STANDING_AUTHORITY" and p1["cumulative"]["change_since_grant_gbp"] == round(89000.0 - 88155.28, 2), p1)
        p2 = appetite_permission({"state": "PASS"}, "CDR-dec", root=tmp)
        ok("a plan the guard passes needs no permission", p2["state"] == "NOT_REQUIRED", p2)
        sa_changed = standing_authority(SEVERE, tmp, policy={"max_drawdown_pct": 40, "basis": "FLOW_ADJUSTED_PEAK",
                                                             "response": "REVIEW_AND_GUARD_NEW_RISK"})
        ok("NEGATIVE CONTROL: a changed limit puts the authority OUT_OF_SCOPE", sa_changed["state"] == "OUT_OF_SCOPE", sa_changed)
        p3 = appetite_permission(guard, "CDR-dec", root=tmp, authority=sa_changed)
        ok("NEGATIVE CONTROL: out-of-scope authority -> refuses", p3["state"] == "REFUSED_APPETITE_APPROVAL_ABSENT", p3)
        p4 = appetite_permission(guard, "CDR-dec", root=tmp, authority=sa_changed,
                                 decision_approval={"approved_by": "Raj 06-Dec", "receipt_id": "CDR-dec"})
        ok("a per-receipt approval naming THIS receipt still works", p4["state"] == "RECEIPT_APPROVAL", p4)
        p5 = appetite_permission(guard, "CDR-dec", root=tmp, authority=sa_changed,
                                 decision_approval={"approved_by": "Raj", "receipt_id": "CDR-nov"})
        ok("NEGATIVE CONTROL: an approval naming ANOTHER receipt does not carry", p5["state"].startswith("REFUSED"), p5)
        _write(tmp, {"event_id": "RRA-revoke-test", "event": "STANDING_AUTHORITY_REVOKED", "revokes": MIGRATION_ID,
                     "revoked_by": "Raj", "recorded_at": "2026-12-10T00:00:00+00:00"})
        ok("NEGATIVE CONTROL: a revoked authority refuses", appetite_permission(guard, "CDR-dec", root=tmp)["state"]
           == "REFUSED_APPETITE_APPROVAL_ABSENT")
        r0 = observe_realised({"verdict": "WITHIN_APPETITE", "current_drawdown_pct": -2.3}, tmp, write=True)
        ok("realised: within appetite opens nothing", r0["state"] == "WITHIN_APPETITE" and r0["open_episode"] is None, r0)
        r1 = observe_realised({"verdict": "REALISED_AT_OR_BEYOND_APPETITE", "current_drawdown_pct": -51.0}, tmp, write=True)
        r2 = observe_realised({"verdict": "REALISED_AT_OR_BEYOND_APPETITE", "current_drawdown_pct": -53.0}, tmp, write=True)
        ok("MUST-FIRE: the actual drawdown at the limit opens ONE episode; the next month continues it",
           r1["state"] == "EPISODE_OPENED" and r2["state"] == "EPISODE_CONTINUES" and r2["episode_id"] == r1["episode_id"],
           (r1, r2))
        r3 = observe_realised({"verdict": "WITHIN_APPETITE", "current_drawdown_pct": -30.0}, tmp, write=True)
        ok("a recovery does not close the episode (Raj closes it)", (r3.get("open_episode") or {}).get("event_id")
           == r1["episode_id"], r3)
        r4 = observe_realised({"verdict": "UNMEASURED"}, tmp)
        ok("UNMEASURED never opens or closes", r4["state"] == "UNMEASURED", r4)
        with open(os.path.join(_store(tmp), "zz_bad.json"), "w") as fh:
            fh.write("{not json")
        ok("NEGATIVE CONTROL: an unreadable record makes the authority CORRUPT, never VALID (R4.3)",
           standing_authority(SEVERE, tmp)["state"] == "CORRUPT")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print("risk_review_state selftest: %d FAIL" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    if "--seed" in sys.argv:
        print(json.dumps(seed_standing_authority(HERE), indent=1))
        sys.exit(0)
    print(json.dumps(standing_authority(SEVERE, HERE), indent=1, default=str))
