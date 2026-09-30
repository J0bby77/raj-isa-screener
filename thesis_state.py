#!/usr/bin/env python3
"""
thesis_state.py — P7.2. THE home for judgement in the V2.1 sizing stack.

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md P7 (D21).
Above it: ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md §6 — *"Judgement lives in `thesis_state` and
may only block, downsize or hold — never upsize. Do not reinstate the /100 conviction score."*

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHY THIS MODULE DID NOT EXIST UNTIL TODAY, WHICH IS THE WHOLE POINT
═══════════════════════════════════════════════════════════════════════════════════════════
The clean spec relocated judgement OUT of the /100 conviction score and INTO `thesis_state`.
Measured on the delivered tree 27-Aug-2026, the name appeared in exactly four places, all
prose: a docstring line in `evidence_state.py`, the 22-Aug reasoning record, clean spec §6,
and a JSON *example* in a superseded design document. **No module defined, wrote or read it.**

**Judgement was evicted from the /100 and given nowhere to go, so it never left.** The /100
stayed live and hard-gated the §7.6.2 email — on a field that is populated for **2 of 53**
names, with every `conviction_total` in `step9_conviction_aug_2026.json` null. A gate on a
field nobody fills is not a gate.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑⚑ THE ASYMMETRY IS STRUCTURAL, NOT ASSERTED
═══════════════════════════════════════════════════════════════════════════════════════════
`apply()` resolves both its inputs to an INDEX on the declared ladder and returns
`ladder[min(rung_ix, ceiling_ix)]`. **There is no expression in this module that can produce a
larger index than it was given.** The asymmetry is therefore a property of the arithmetic, not
of a test that someone remembers to keep.

That matters because the failure mode is specific and it has a name: A1's withdrawn `x d`
diversification multiplier was a judgement input that could RAISE a size, and it was withdrawn
because a judgement that can upsize is indistinguishable from a forecast. This module cannot
be turned back into one without deleting `min`.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ AND IT CARRIES conviction_capture's REFUSAL, DELIBERATELY (C8)
═══════════════════════════════════════════════════════════════════════════════════════════
Retiring the /100 removes a control. If `thesis_state` were optional, the net effect of P7
would be to remove a control and add none. `require()` therefore REFUSES a decided action
that carries no state or no rationale — the same shape of refusal `conviction_capture`
already applies to the score it replaces.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["single_sizing_authority"] = False ⇒ `apply()` returns
its input rung unchanged and `require()` does not refuse. The /100 becomes readable by gates
again. **Keep a pre-delivery copy of every prose file touched** — swapping it back and
re-running is the only honest answer to "is this red mine?".
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))

# ── P0.1 LIVE-PATH EXECUTION LEDGER ────────────────────────────────────────────────────
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

STRENGTHENING = "STRENGTHENING"
INTACT = "INTACT"
WATCH = "WATCH"
BROKEN = "BROKEN"

STATES = (STRENGTHENING, INTACT, WATCH, BROKEN)

# The ladder, ordered LOW to HIGH. Read from target_weights.json every run and never typed —
# a second copy of the ladder here would be exactly the two-homes defect P7 exists to close.
LADDER_ORDER = ("STARTER", "NORMAL", "HIGH", "EARNED_MAX")

# ⚑ The CEILING each state imposes, and nothing else. A state never names a size; it names the
# highest rung it will tolerate. `None` means "imposes no ceiling", which is not the same as
# "raises to the top" — the difference is the whole asymmetry.
STATE_CEILING: Dict[str, Optional[str]] = {
    STRENGTHENING: None,        # ⚑ NO CHANGE. It may not upsize — that is the point.
    INTACT:        None,
    WATCH:         "STARTER",
    BROKEN:        None,        # handled by `blocks_new_capital`, not by a rung
}

STATE_BLOCKS_NEW_CAPITAL = {STRENGTHENING: False, INTACT: False, WATCH: False, BROKEN: True}

STATE_BASIS = {
    STRENGTHENING: ("the thesis is doing better than underwritten. It does NOT earn a larger "
                    "position: size is earned by EVIDENCE on the ladder, and letting judgement "
                    "grant size is A1's withdrawn x d multiplier returning through the back "
                    "door."),
    INTACT:        "the thesis is as underwritten. The ladder rung stands.",
    WATCH:         ("something in the thesis is questioned but not broken. New capital is "
                    "capped at STARTER until it resolves — a downgrade, never a block."),
    BROKEN:        ("the thesis no longer holds. NO new capital, and the position enters the "
                    "§10 replacement comparison. This is a judgement about the COMPANY; the "
                    "capital consequence is decided in §10 against alternatives."),
}


class ThesisStateRefused(RuntimeError):
    """A decided action carries no thesis state, or no rationale for it.

    ⚑ NEVER downgraded to a default state. `INTACT` as a default would mean 'nobody looked'
    and 'the thesis is as underwritten' render identically, which is R2.10's exact prohibition
    and is how the /100 came to be 2-of-53 populated while still gating an email."""


def _flag(name: str = "single_sizing_authority", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


def ladder(policy=None) -> Dict[str, float]:
    """THE ladder, from its one home. Never restated here (R4.4)."""
    import position_sizing as _ps
    return _ps.ladder(policy)


def _order(policy=None) -> List[str]:
    lad = ladder(policy)
    return [r for r in LADDER_ORDER if r in lad]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0466 — THE PRODUCER. `apply()` had zero call sites and nothing populated the field.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# The clean spec relocated judgement OUT of the /100 conviction score and INTO thesis_state.
# The module shipped 28-Aug-2026 — the item's TITLE still says it "was never built", which is
# stale; its corrective action was narrowed on 02-Sep and records the build. What remained is
# that `stock_candidates.build()` accepts a `thesis_states` argument and NO CALLER SUPPLIES
# ONE, so the cap-never-raise layer was inert: judgement was evicted from the /100 and given
# nowhere to go, so it never left.
#
# ⚑ ABSENCE REFUSES. A held name with no declared state does NOT default to INTACT. Defaulting
#   would recreate the exact defect this module replaced — a gate on a field nobody fills,
#   which is how the /100 hard-gated the §7.6.2 email on a value populated for 2 of 53 names.

STORE_FILE = "thesis_states.json"


def _load_declared(root: Optional[str] = None) -> dict:
    """{TICKER: {state, rationale}} from the declared store. Missing file -> {} (and every
    lookup then REFUSES, which is the correct degradation)."""
    import json as _json
    import os as _os
    root = root or _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(root, STORE_FILE)
    if not _os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = _json.load(fh) or {}
    out = {}
    for tk, row in (doc.get("states") or {}).items():
        out[str(tk).upper()] = row
        for a in (row.get("aliases") or []):
            out[str(a).upper()] = row
    return out


def state_for(ticker: str, states: Optional[dict] = None, root: Optional[str] = None) -> dict:
    """The declared state for one held name, or a REFUSAL naming what is missing."""
    states = load_states(root) if states is None else states
    row = states.get(str(ticker or "").upper())
    if not row:
        raise ThesisStateRefused(
            "%s is held and has no declared thesis_state in %s. Absence is REFUSED, not read "
            "as INTACT: a judgement layer that defaults is a judgement layer that never fires "
            "(ISA-0466, R4.3)." % (ticker, STORE_FILE))
    if not str(row.get("rationale") or "").strip():
        raise ThesisStateRefused(
            "%s declares thesis_state %r with no rationale. A state without a reason is the "
            "conviction score's failure repeated under a new name (P7.3)."
            % (ticker, row.get("state")))
    validate(row.get("state"))
    return row


def cap_rung(ticker: str, rung: str, *, states: Optional[dict] = None, root: Optional[str] = None,
             policy=None) -> dict:
    """Evidence sets the rung; judgement may only lower it. The one live entry point."""
    row = state_for(ticker, states, root)
    out = apply(rung, row["state"], policy=policy)
    out["ticker"] = ticker
    out["rationale"] = row.get("rationale")
    out["declared_by"] = row.get("declared_by")
    # ⚑ ISA-0760: a journal-derived state names the contract/evaluation it came from, and an
    #   evaluation with an UNKNOWN condition cannot support NEW capital (R4.3). This can only
    #   ADD a block — it never removes one and never raises a rung (the asymmetry holds).
    if row.get("thesis_evaluation_id"):
        out["thesis_contract_id"] = row.get("thesis_contract_id")
        out["thesis_evaluation_id"] = row.get("thesis_evaluation_id")
        out["thesis_contract_basis"] = row.get("thesis_contract_basis")
        if row.get("evaluation_complete") is False:
            out["blocks_new_capital"] = True
            out["thesis_evidence_incomplete"] = sorted(
                k for k, v in (row.get("condition_status") or {}).items() if v == "UNKNOWN")
            out["basis"] = (out.get("basis") or "") + (
                " ISA-0760: condition(s) %s are UNKNOWN in evaluation %s, so no NEW capital "
                "until they are observed." % (", ".join(out["thesis_evidence_incomplete"]),
                                              row.get("thesis_evaluation_id")))
    else:
        out["thesis_contract_state"] = NOT_CAPTURED
    return out


def validate(state: Optional[str]) -> str:
    if state not in STATES:
        raise ThesisStateRefused(
            "thesis_state %r is not declared. Declared: %s. A state that is not on the list "
            "would size at whatever the caller passed, which is the failure this module "
            "exists to remove." % (state, list(STATES)))
    return state


def apply(rung: str, state: Optional[str], *, policy=None) -> dict:
    """The ONE operation. Returns a rung <= the input rung, ALWAYS, by construction.

    ⚑ `min(rung_ix, ceiling_ix)` is the entire asymmetry. There is no branch here that can
    return a larger index than it was handed, so `apply` cannot be made to upsize without
    someone deleting `min` — which is a visible edit, unlike a forgotten test."""
    _fi_mark("thesis_state", "apply")
    order = _order(policy)
    if rung == "HOLD_AT_CURRENT":
        # D13's freeze is stronger than any thesis state and is not on the ladder.
        return {"rung_in": rung, "rung_out": rung, "state": state, "changed": False,
                "blocks_new_capital": True,
                "basis": ("DEGRADED_UNMEASURED holds the position at its current weight (D13). "
                          "That freeze is not a ladder rung and thesis_state does not move it.")}
    if rung not in order:
        raise ThesisStateRefused(
            "rung %r is not on the ladder %s." % (rung, order))
    if not _flag():
        return {"rung_in": rung, "rung_out": rung, "state": state, "changed": False,
                "blocks_new_capital": False,
                "basis": "ROLLBACK: V2_FLAGS['single_sizing_authority'] is False — the rung "
                         "passes through unchanged."}
    st = validate(state)
    rung_ix = order.index(rung)
    ceil = STATE_CEILING[st]
    ceil_ix = order.index(ceil) if ceil in order else rung_ix
    out_ix = min(rung_ix, ceil_ix)          # ⚑ THE ASYMMETRY, in one call
    out = order[out_ix]
    return {"rung_in": rung, "rung_out": out, "state": st,
            "changed": out != rung,
            "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL[st],
            "enters_replacement_comparison": st == BROKEN,
            "basis": ("thesis_state %s: %s%s" %
                      (st, STATE_BASIS[st],
                       (" Rung %s -> %s." % (rung, out)) if out != rung else
                       " Rung %s unchanged." % rung)),
            "asymmetry": ("min(rung, ceiling) — this function has no path that returns a rung "
                          "above its input, over all %d states x all %d rungs."
                          % (len(STATES), len(order)))}


def require(action: dict, *, field: str = "thesis_state",
            rationale_field: str = "thesis_state_rationale") -> dict:
    """P7.3's gate input. A decided action carries a state AND a rationale, or this REFUSES.

    ⚑ BOTH, not either. A state with no rationale is a label; a rationale with no state is an
    opinion. The old §7.6.2 gate was satisfied by `conviction_total`, a field that stood at
    2 of 53 populated — and it passed anyway because nothing checked that it was filled."""
    tk = action.get("ticker") or action.get("name") or "<unnamed>"
    if not _flag():
        return {"ok": True, "ticker": tk, "state": action.get(field),
                "basis": "ROLLBACK: the single-sizing-authority flag is False."}
    st = action.get(field)
    if st is None:
        raise ThesisStateRefused(
            "%s: a decided action carries no %s. Retiring the /100 removes a control; making "
            "this one optional would remove a control and add none (C8). Declare one of %s "
            "with a rationale." % (tk, field, list(STATES)))
    validate(st)
    rat = action.get(rationale_field)
    if not rat or not str(rat).strip():
        raise ThesisStateRefused(
            "%s: thesis_state is %s with no rationale. A state without a reason cannot be "
            "challenged next month, which is the only thing that makes a judgement reviewable "
            "rather than a preference." % (tk, st))
    return {"ok": True, "ticker": tk, "state": st, "rationale": str(rat).strip(),
            "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL[st]}


def gate(actions: Sequence[dict]) -> dict:
    """The §7.6.2 replacement gate: every decided action, all-or-nothing, naming each failure."""
    ok, refused = [], []
    for a in actions or []:
        try:
            ok.append(require(a))
        except ThesisStateRefused as exc:
            refused.append({"ticker": a.get("ticker") or a.get("name") or "<unnamed>",
                            "reason": str(exc)})
    return {"state": "REFUSED" if refused else "OK",
            "n_actions": len(actions or []), "n_ok": len(ok), "refused": refused,
            "rows": ok,
            "basis": ("P7.3 — the §7.6.2 gate now reads thesis_state + evidence_state, both "
                      "non-null with rationales, and refuses on either being absent. It "
                      "replaces a gate on `conviction_total`, a field that was null for every "
                      "name in step9_conviction_aug_2026.json.")}



# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0760 (27-Sep-2026) — THE THESIS CONTRACT / EVALUATION JOURNAL
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Authority: ISA_BuildSpec_ISA0760_Investment_Case_Lineage_26Sep2026.md §5.1C, §5.2, §5.3, §7.
#
# ⚑ WHY THIS EXISTS. The retired `project_isa_trades_log.md` was a denormalised bucket holding
#   broker facts + valuation anchors + thesis-break conditions, maintained by hand by the Sunday
#   session. Package A (ISA-0759/0124) correctly removed the dependency and marked the orphaned
#   fields UNKNOWN. Those semantics now have ONE home each:
#     • execution facts           -> transaction_ledger.json (never thesis semantics)
#     • decision-time valuation   -> underwriting_cases.jsonl (observations.valuation_multiple)
#     • thesis conditions + their -> THIS journal (append-only CONTRACT / EVALUATION records)
#       observed status
#   `thesis_states.json` stays the store every consumer reads; for a ticker with a journal
#   EVALUATION it becomes a GENERATED projection (project()), never a second hand-kept truth.
#
# ⚑ NO BACKFILL (R7.5). A held position with no contemporaneous contract is
#   NOT_CAPTURED_CONTEMPORANEOUSLY. A contract written at a later review is CURRENT_REUNDERWRITTEN
#   and is REFUSED if labelled ENTRY_CONTEMPORANEOUS while the transaction ledger shows the
#   position was entered in an earlier month.
#
# ⚑ NO NEW STATE PRECEDENCE. The aggregate is the reviewer's declared D21 state (STATES). The
#   journal enforces only what the words already mean: a thesis-break condition observed BROKEN
#   is a thesis break (Run_Context Step 5 — "a thesis-break fired" -> SELL review), so the
#   aggregate is BROKEN; an UNKNOWN condition may not be read as INTACT/STRENGTHENING and the
#   evaluation cannot support NEW capital (R4.3 — missing is never a pass).
JOURNAL_FILE = "thesis_records.jsonl"
JOURNAL_SCHEMA = "1.0.0"
CONTRACT = "CONTRACT"
EVALUATION = "EVALUATION"
RECORD_TYPES = (CONTRACT, EVALUATION)
BASIS_ENTRY = "ENTRY_CONTEMPORANEOUS"
BASIS_REUNDERWRITTEN = "CURRENT_REUNDERWRITTEN"
CONTRACT_BASES = (BASIS_ENTRY, BASIS_REUNDERWRITTEN)
NOT_CAPTURED = "NOT_CAPTURED_CONTEMPORANEOUSLY"
COND_KINDS = ("QUANTITATIVE", "EVENT", "QUALITATIVE")
COND_RESULTS = ("CLEAR", "WATCH", "BROKEN", "UNKNOWN")
OPERATORS = ("<", "<=", ">", ">=", "==", "!=")
_COND_REQUIRED = {
    "QUANTITATIVE": ("field", "source", "operator", "threshold", "unit", "max_age_days"),
    "EVENT": ("event", "source_contract"),
    "QUALITATIVE": ("question", "evidence_required", "review_by"),
}
_CONTRACT_REQUIRED = ("ticker", "route_id", "effective_from", "basis", "thesis_summary",
                      "conditions", "contract_version", "created_by", "evidence_basis")
_EVAL_REQUIRED = ("thesis_contract_id", "ticker", "as_of", "run_id", "condition_results",
                  "aggregate_thesis_state", "state_rationale", "reviewer_or_automation")


class ThesisRecordRefused(ThesisStateRefused):
    """A journal record that would be invalid, ambiguous or a rewrite of history. Never
    repaired or defaulted: the caller fixes the record (R4.3, R6.4)."""


def journal_path(root: Optional[str] = None) -> str:
    return os.path.join(root or HERE, JOURNAL_FILE)


def _h(obj) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _date(x, what):
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except Exception:                                                   # noqa: BLE001
        raise ThesisRecordRefused("%s %r is not an ISO date" % (what, x))


def load_journal(root: Optional[str] = None) -> List[dict]:
    """Every record, in APPEND ORDER (the sequence is the identity of 'newest', never mtime).
    A corrupt line REFUSES the whole read (R4.9): nothing past it is trusted."""
    p = journal_path(root)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise ThesisRecordRefused("%s line %d is not JSON (%s) — the journal is corrupt; "
                                          "nothing is read past it (R4.9)" % (JOURNAL_FILE, i, exc))
            rec["_seq"] = i
            out.append(rec)
    return out


def _contracts(recs):
    return {r["thesis_contract_id"]: r for r in recs if r.get("record_type") == CONTRACT}


def _validate_condition(c, idx):
    if not isinstance(c, dict):
        raise ThesisRecordRefused("condition #%d is not an object" % idx)
    cid = str(c.get("condition_id") or "").strip()
    if not cid:
        raise ThesisRecordRefused("condition #%d has no condition_id" % idx)
    kind = c.get("kind")
    if kind not in COND_KINDS:
        raise ThesisRecordRefused("condition %s kind %r is not declared (%s)"
                                  % (cid, kind, ", ".join(COND_KINDS)))
    if not str(c.get("description") or "").strip():
        raise ThesisRecordRefused("condition %s has no description" % cid)
    missing = [k for k in _COND_REQUIRED[kind] if c.get(k) in (None, "")]
    if missing:
        raise ThesisRecordRefused(
            "%s condition %s is missing %s. A %s condition without them cannot be evaluated the "
            "same way twice, which is what makes it a condition rather than a mood (§5.2)."
            % (kind, cid, missing, kind.lower()))
    if kind == "QUANTITATIVE":
        if c["operator"] not in OPERATORS:
            raise ThesisRecordRefused("condition %s operator %r is not one of %s"
                                      % (cid, c["operator"], OPERATORS))
        try:
            float(c["threshold"])
            if int(c["max_age_days"]) <= 0:
                raise ValueError
        except Exception:                                               # noqa: BLE001
            raise ThesisRecordRefused("condition %s threshold/max_age_days must be numeric "
                                      "(max_age_days > 0)" % cid)
    if kind == "QUALITATIVE":
        _date(c["review_by"], "condition %s review_by" % cid)
    return cid


def first_entry_date(ticker: str, root: Optional[str] = None) -> Optional[str]:
    """The FIRST broker BUY of a ticker, from transaction_ledger.json (execution truth only)."""
    try:
        import position_alerts as _pa
        mh = _pa.min_hold_from_ledger(os.path.join(root or HERE, "transaction_ledger.json"),
                                      root=root or HERE) or {}
    except Exception:                                                   # noqa: BLE001
        return None
    row = mh.get(str(ticker).upper()) or {}
    return row.get("position_first_entry_date")


def validate_contract(rec: dict, recs: Optional[List[dict]] = None,
                      root: Optional[str] = None) -> dict:
    recs = load_journal(root) if recs is None else recs
    missing = [k for k in _CONTRACT_REQUIRED if rec.get(k) in (None, "", [])]
    if missing:
        raise ThesisRecordRefused("CONTRACT is missing %s" % missing)
    if rec["basis"] not in CONTRACT_BASES:
        raise ThesisRecordRefused("basis %r is not declared (%s)" % (rec["basis"], CONTRACT_BASES))
    route = str(rec["route_id"]).lower()
    if route in ("a", "b", "c", "path a", "path b", "path c", "path_a", "path_b", "path_c"):
        raise ThesisRecordRefused("route_id %r is a retired A/B/C path — use the current "
                                  "canonical route (growth / vci)" % rec["route_id"])
    eff = _date(rec["effective_from"], "effective_from")
    if "source_underwriting_case_id" not in rec:
        raise ThesisRecordRefused("CONTRACT must carry source_underwriting_case_id (None only "
                                  "with underwriting_case_absent_reason)")
    if rec.get("source_underwriting_case_id") is None and \
            not str(rec.get("underwriting_case_absent_reason") or "").strip():
        raise ThesisRecordRefused("source_underwriting_case_id is None with no "
                                  "underwriting_case_absent_reason — an absent case is named, "
                                  "never silent (R4.3)")
    conds = rec["conditions"]
    if not isinstance(conds, list) or not conds:
        raise ThesisRecordRefused("a contract with no conditions has no falsifier (§5.2)")
    ids = [_validate_condition(c, i) for i, c in enumerate(conds, 1)]
    if len(set(ids)) != len(ids):
        raise ThesisRecordRefused("duplicate condition_id in contract: %s" % ids)
    try:
        ver = int(rec["contract_version"])
    except Exception:                                                   # noqa: BLE001
        raise ThesisRecordRefused("contract_version must be an integer")
    tk = str(rec["ticker"]).upper()
    lineage = [c for c in _contracts(recs).values() if str(c.get("ticker")).upper() == tk]
    if ver == 1:
        if rec.get("supersedes_contract_id"):
            raise ThesisRecordRefused("version 1 supersedes nothing")
    else:
        prev = _contracts(recs).get(rec.get("supersedes_contract_id") or "")
        if not prev or str(prev.get("ticker")).upper() != tk:
            raise ThesisRecordRefused("version %d must name the %s contract it supersedes "
                                      "(supersedes_contract_id)" % (ver, tk))
        if int(prev.get("contract_version") or 0) != ver - 1:
            raise ThesisRecordRefused("version %d must supersede version %d, not %s"
                                      % (ver, ver - 1, prev.get("contract_version")))
    for c in lineage:
        if int(c.get("contract_version") or 0) == ver:
            raise ThesisRecordRefused(
                "%s contract version %d already exists (%s). A changed condition is a NEW version "
                "that supersedes it; history is never rewritten in place (§13.8)."
                % (tk, ver, c.get("thesis_contract_id")))
    if rec["basis"] == BASIS_ENTRY:
        if not rec.get("source_underwriting_case_id"):
            raise ThesisRecordRefused("ENTRY_CONTEMPORANEOUS requires the decision-time "
                                      "underwriting case id")
        fe = first_entry_date(tk, root)
        if fe and _date(fe, "first entry") < eff.replace(day=1):
            raise ThesisRecordRefused(
                "%s was first bought %s (transaction ledger); a contract written %s is a "
                "CURRENT_REUNDERWRITTEN case, never the original entry thesis (R7.5 — no "
                "backfilled judgement)." % (tk, fe, eff.isoformat()))
    return rec


def build_contract(**kw) -> dict:
    """A CONTRACT record with its derived identity. Validation happens in record()."""
    rec = {"schema_version": JOURNAL_SCHEMA, "record_type": CONTRACT,
           "supersedes_contract_id": None, "source_decision_id": None, "build_id": None,
           "policy_identity": None}
    rec.update(kw)
    rec["ticker"] = str(rec.get("ticker") or "").upper()
    core = {k: v for k, v in rec.items() if k not in ("record_id", "thesis_contract_id",
                                                        "recorded_at")}
    h = _h(core)[:10]
    rec["thesis_contract_id"] = "TC-%s-%s-v%s-%s" % (rec["ticker"],
                                                      str(rec.get("effective_from"))[:10],
                                                      rec.get("contract_version"), h)
    rec["record_id"] = "TR-" + rec["thesis_contract_id"]
    return rec


def validate_evaluation(rec: dict, recs: Optional[List[dict]] = None,
                        root: Optional[str] = None) -> dict:
    recs = load_journal(root) if recs is None else recs
    missing = [k for k in _EVAL_REQUIRED if rec.get(k) in (None, "", [])]
    if missing:
        raise ThesisRecordRefused("EVALUATION is missing %s" % missing)
    con = _contracts(recs).get(rec["thesis_contract_id"])
    if not con:
        raise ThesisRecordRefused("EVALUATION references unknown contract %s"
                                  % rec["thesis_contract_id"])
    if str(con.get("ticker")).upper() != str(rec["ticker"]).upper():
        raise ThesisRecordRefused("EVALUATION ticker %s does not match contract ticker %s"
                                  % (rec["ticker"], con.get("ticker")))
    if _date(rec["as_of"], "as_of") < _date(con["effective_from"], "effective_from"):
        raise ThesisRecordRefused("an evaluation cannot predate its contract")
    want = [c["condition_id"] for c in con["conditions"]]
    got = [r.get("condition_id") for r in rec["condition_results"] if isinstance(r, dict)]
    if sorted(got) != sorted(want) or len(got) != len(set(got)):
        raise ThesisRecordRefused(
            "EVALUATION must report every contract condition exactly once (UNKNOWN with a "
            "reason where it could not be observed): contract %s, evaluation %s"
            % (sorted(want), sorted(got)))
    results = []
    for r in rec["condition_results"]:
        res = r.get("result")
        if res not in COND_RESULTS:
            raise ThesisRecordRefused("condition %s result %r is not one of %s"
                                      % (r.get("condition_id"), res, COND_RESULTS))
        if not str(r.get("reason") or "").strip():
            raise ThesisRecordRefused("condition %s result has no reason" % r.get("condition_id"))
        if res != "UNKNOWN" and not (r.get("source") and r.get("as_of")):
            raise ThesisRecordRefused("condition %s result %s needs its source and as_of "
                                      "(only UNKNOWN may lack evidence)" % (r.get("condition_id"),
                                                                             res))
        results.append(res)
    agg = rec["aggregate_thesis_state"]
    validate(agg)
    if "BROKEN" in results and agg != BROKEN:
        raise ThesisRecordRefused(
            "a thesis-break condition is BROKEN but the aggregate is %s. A fired thesis-break "
            "condition IS a thesis break (Run_Context Step 5); declare BROKEN or amend the "
            "contract by a new version first." % agg)
    if "UNKNOWN" in results and agg in (INTACT, STRENGTHENING):
        raise ThesisRecordRefused(
            "a condition is UNKNOWN but the aggregate is %s. Missing evidence is never read as an "
            "intact thesis (R4.3); declare WATCH or BROKEN." % agg)
    # ⚑ GOVERNANCE PRESERVED (Run_Context Step 5: a held thesis_state is changed by Raj's
    #   declaration). The run may RECORD an unchanged or LOWER state from evidence — that can
    #   only block or downsize (the asymmetry) — but a RAISED state needs `approved_by`.
    _rank = {BROKEN: 0, WATCH: 1, INTACT: 2, STRENGTHENING: 3}
    _prev_ev = latest_evaluations(root, recs).get(str(rec["ticker"]).upper())
    _prev = (_prev_ev or {}).get("aggregate_thesis_state") or \
        ((_load_declared(root).get(str(rec["ticker"]).upper()) or {}).get("state"))
    if _prev in _rank and _rank[agg] > _rank[_prev] and \
            not str(rec.get("approved_by") or "").strip():
        raise ThesisRecordRefused(
            "%s: %s -> %s RAISES the thesis state; a raise lifts a capital ceiling and needs Raj's "
            "declaration (approved_by). The run may record an unchanged or lower state."
            % (rec["ticker"], _prev, agg))
    for e in recs:
        if (e.get("record_type") == EVALUATION and e.get("thesis_contract_id") ==
                rec["thesis_contract_id"] and e.get("run_id") == rec["run_id"]
                and e.get("evaluation_id") != rec.get("evaluation_id")):
            raise ThesisRecordRefused(
                "run %s already recorded a DIFFERENT evaluation (%s) of %s — a retry may not "
                "append a conflicting judgement; use a new run_id for a genuine re-review."
                % (rec["run_id"], e.get("evaluation_id"), rec["thesis_contract_id"]))
    return rec


def build_evaluation(**kw) -> dict:
    rec = {"schema_version": JOURNAL_SCHEMA, "record_type": EVALUATION,
           "evidence_state": None, "event_review_id": None, "source_refs": [], "build_id": None}
    rec.update(kw)
    rec["ticker"] = str(rec.get("ticker") or "").upper()
    core = {k: v for k, v in rec.items() if k not in ("record_id", "evaluation_id",
                                                        "recorded_at")}
    rec["evaluation_id"] = "TE-%s-%s-%s" % (rec["ticker"], str(rec.get("as_of"))[:10],
                                            _h(core)[:10])
    rec["record_id"] = "TR-" + rec["evaluation_id"]
    return rec


def record(rec: dict, *, root: Optional[str] = None, dry_run: bool = False,
           project_after: bool = True) -> dict:
    """Append ONE validated record. Identical re-submission is an idempotent no-op; a record
    that would rewrite or contradict history REFUSES. Returns {written, record_id}."""
    _fi_mark("thesis_state", "record")
    recs = load_journal(root)
    have = {r.get("record_id"): r for r in recs}
    rid = rec.get("record_id")
    if not rid:
        raise ThesisRecordRefused("record has no record_id — build it with build_contract / "
                                  "build_evaluation")
    if rec.get("record_type") not in RECORD_TYPES:
        raise ThesisRecordRefused("record_type %r is not one of %s"
                                  % (rec.get("record_type"), RECORD_TYPES))
    if rec.get("schema_version") != JOURNAL_SCHEMA:
        raise ThesisRecordRefused("schema_version %r is not %s" % (rec.get("schema_version"),
                                                                    JOURNAL_SCHEMA))
    if rid in have:
        prev = {k: v for k, v in have[rid].items() if k not in ("_seq", "recorded_at")}
        cur = {k: v for k, v in rec.items() if k not in ("_seq", "recorded_at")}
        if prev != cur:
            raise ThesisRecordRefused("record_id %s exists with different content — records "
                                      "are immutable" % rid)
        return {"written": False, "existing": True, "record_id": rid}
    if rec["record_type"] == CONTRACT:
        validate_contract(rec, recs, root)
    else:
        validate_evaluation(rec, recs, root)
    out = dict(rec)
    out.pop("_seq", None)
    out["recorded_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        with open(journal_path(root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, sort_keys=True, default=str, ensure_ascii=False) + "\n")
        if project_after and rec["record_type"] == EVALUATION:
            project(root=root)
    return {"written": not dry_run, "existing": False, "record_id": rid}


def latest_evaluations(root: Optional[str] = None, recs=None) -> Dict[str, dict]:
    """{TICKER: newest EVALUATION} — newest by (as_of, append sequence), never by file mtime."""
    recs = load_journal(root) if recs is None else recs
    out = {}
    for r in recs:
        if r.get("record_type") != EVALUATION:
            continue
        tk = str(r.get("ticker")).upper()
        k = (str(r.get("as_of"))[:10], r.get("_seq", 0))
        if tk not in out or k >= (str(out[tk].get("as_of"))[:10], out[tk].get("_seq", 0)):
            out[tk] = r
    return out


def _row_from_evaluation(ev: dict, con: dict) -> dict:
    res = [r.get("result") for r in ev.get("condition_results") or []]
    return {"state": ev["aggregate_thesis_state"], "rationale": ev["state_rationale"],
            "declared_by": ev.get("reviewer_or_automation"),
            "source": JOURNAL_FILE,
            "thesis_contract_id": ev["thesis_contract_id"],
            "thesis_contract_version": con.get("contract_version"),
            "thesis_contract_basis": con.get("basis"),
            "thesis_evaluation_id": ev["evaluation_id"],
            "evaluated_as_of": ev.get("as_of"),
            "condition_status": {r.get("condition_id"): r.get("result")
                                 for r in ev.get("condition_results") or []},
            "evaluation_complete": "UNKNOWN" not in res,
            "underwriting_case_id": con.get("source_underwriting_case_id")}


def load_states(root: Optional[str] = None) -> dict:  # noqa: F811  (ISA-0760 overlay)
    """{TICKER: row}. The declared store, OVERLAID by the newest journal evaluation per ticker.
    With no journal record for a ticker its declared row is returned unchanged, so an empty
    journal is byte-for-byte the pre-ISA-0760 behaviour (decision parity)."""
    base = _load_declared(root)
    try:
        recs = load_journal(root)
    except ThesisRecordRefused:
        raise
    if not recs:
        return base
    cons = _contracts(recs)
    for tk, ev in latest_evaluations(root, recs).items():
        con = cons.get(ev.get("thesis_contract_id")) or {}
        prior = base.get(tk) or {}
        row = _row_from_evaluation(ev, con)
        if prior.get("aliases"):
            row["aliases"] = prior["aliases"]
        base[tk] = row
        for a in row.get("aliases") or []:
            base[str(a).upper()] = row
    return base


def lineage_for(ticker: str, root: Optional[str] = None) -> dict:
    """The thesis identity a decision binds. A ticker with no contract is typed
    NOT_CAPTURED_CONTEMPORANEOUSLY — never a reconstructed contract (R7.5)."""
    tk = str(ticker).upper()
    recs = load_journal(root)
    cons = [c for c in _contracts(recs).values() if str(c.get("ticker")).upper() == tk]
    if not cons:
        return {"ticker": tk, "state": NOT_CAPTURED, "thesis_contract_id": None,
                "thesis_evaluation_id": None,
                "why": "no thesis contract was recorded for %s; its thesis is not reconstructed" % tk}
    superseded = {c.get("supersedes_contract_id") for c in cons if c.get("supersedes_contract_id")}
    cur = [c for c in cons if c["thesis_contract_id"] not in superseded]
    cur = sorted(cur, key=lambda c: int(c.get("contract_version") or 0))[-1]
    ev = latest_evaluations(root, recs).get(tk)
    ev_ok = ev if ev and ev.get("thesis_contract_id") == cur["thesis_contract_id"] else None
    return {"ticker": tk, "state": cur.get("basis"),
            "thesis_contract_id": cur["thesis_contract_id"],
            "thesis_contract_version": cur.get("contract_version"),
            "thesis_evaluation_id": (ev_ok or {}).get("evaluation_id"),
            "aggregate_thesis_state": (ev_ok or {}).get("aggregate_thesis_state"),
            "evaluation_state": ("CURRENT" if ev_ok else "NOT_EVALUATED_AGAINST_CURRENT_CONTRACT"),
            "underwriting_case_id": cur.get("source_underwriting_case_id")}


def qualitative_overdue(root: Optional[str] = None, today: Optional[str] = None) -> Dict[str, list]:
    """QUALITATIVE conditions whose review_by has passed since their last evaluation (§13.9)."""
    today = today or datetime.date.today().isoformat()
    recs = load_journal(root)
    cons = _contracts(recs)
    out = {}
    for tk, ev in latest_evaluations(root, recs).items():
        con = cons.get(ev.get("thesis_contract_id")) or {}
        for c in con.get("conditions") or []:
            if c.get("kind") == "QUALITATIVE" and str(c.get("review_by"))[:10] < today[:10]:
                out.setdefault(tk, []).append(c["condition_id"])
    return out


def project(root: Optional[str] = None, dry_run: bool = False) -> dict:
    """Regenerate thesis_states.json rows for every journal-evaluated ticker. Declared rows for
    tickers with no evaluation are carried unchanged (legacy, typed via lineage_for)."""
    path = os.path.join(root or HERE, STORE_FILE)
    doc = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh) or {}
    states = dict(doc.get("states") or {})
    recs = load_journal(root)
    cons = _contracts(recs)
    changed = []
    for tk, ev in latest_evaluations(root, recs).items():
        row = _row_from_evaluation(ev, cons.get(ev.get("thesis_contract_id")) or {})
        if (states.get(tk) or {}).get("aliases"):
            row["aliases"] = states[tk]["aliases"]
        if states.get(tk) != row:
            changed.append(tk)
        states[tk] = row
    doc["states"] = states
    doc["_projection"] = ("ISA-0760: rows carrying thesis_evaluation_id are GENERATED from %s by "
                          "thesis_state.project() — edit the journal, never these rows."
                          % JOURNAL_FILE)
    if changed and not dry_run:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    return {"changed": changed, "dry_run": dry_run}


def projection_drift(root: Optional[str] = None) -> List[str]:
    """Tickers whose thesis_states.json row differs from the newest journal evaluation."""
    path = os.path.join(root or HERE, STORE_FILE)
    doc = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh) or {}
    states = doc.get("states") or {}
    recs = load_journal(root)
    cons = _contracts(recs)
    bad = []
    for tk, ev in latest_evaluations(root, recs).items():
        row = _row_from_evaluation(ev, cons.get(ev.get("thesis_contract_id")) or {})
        have = {k: v for k, v in (states.get(tk) or {}).items() if k != "aliases"}
        if have != row:
            bad.append(tk)
    return bad


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)[:200]) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    order = _order()

    # ── A1: never returns a rung ABOVE its input, over ALL 4 states x ALL 4 rungs ────────
    bad = []
    for st in STATES:
        for r in order:
            out = apply(r, st)["rung_out"]
            if order.index(out) > order.index(r):
                bad.append((st, r, out))
    ok("A1 apply() never returns a rung above its input — all %d states x all %d rungs"
       % (len(STATES), len(order)), not bad, bad)

    # A1 negative control: a hand-broken variant that upsizes MUST fail the same check
    def _broken_apply(rung, state):
        ix = order.index(rung)
        return order[min(len(order) - 1, ix + (1 if state == STRENGTHENING else 0))]
    bad2 = [(st, r) for st in STATES for r in order
            if order.index(_broken_apply(r, st)) > order.index(r)]
    ok("A1-neg a hand-broken variant that upsizes on STRENGTHENING IS caught by that check",
       len(bad2) > 0, bad2)

    # ── A2: WATCH caps at STARTER; BROKEN blocks; INTACT/STRENGTHENING leave the rung ────
    ok("A2 WATCH caps at STARTER from every higher rung",
       all(apply(r, WATCH)["rung_out"] == "STARTER" for r in order))
    ok("A2 WATCH does not RAISE a STARTER", apply("STARTER", WATCH)["rung_out"] == "STARTER")
    ok("A2 BROKEN blocks new capital", apply("HIGH", BROKEN)["blocks_new_capital"] is True)
    ok("A2 BROKEN enters the §10 replacement comparison",
       apply("HIGH", BROKEN)["enters_replacement_comparison"] is True)
    ok("A2-neg INTACT leaves every rung untouched",
       all(apply(r, INTACT)["rung_out"] == r for r in order))
    ok("A2-neg STRENGTHENING leaves every rung untouched — it may NOT upsize",
       all(apply(r, STRENGTHENING)["rung_out"] == r for r in order))
    ok("A2-neg neither INTACT nor STRENGTHENING blocks new capital",
       not apply("NORMAL", INTACT)["blocks_new_capital"]
       and not apply("NORMAL", STRENGTHENING)["blocks_new_capital"])

    # ── D13's freeze is stronger than any state ─────────────────────────────────────────
    ok("HOLD_AT_CURRENT is not a ladder rung and no state moves it",
       all(apply("HOLD_AT_CURRENT", st)["rung_out"] == "HOLD_AT_CURRENT" for st in STATES))

    # ── A4: the gate refuses on a null state AND on a null rationale ────────────────────
    good = {"ticker": "AAA", "thesis_state": INTACT,
            "thesis_state_rationale": "revisions still improving on both windows"}
    ok("A4 a complete action passes the gate", require(good)["ok"] is True)
    for bad_action, needle in (
            ({"ticker": "BBB"}, "carries no thesis_state"),
            ({"ticker": "CCC", "thesis_state": INTACT}, "no rationale"),
            ({"ticker": "DDD", "thesis_state": INTACT, "thesis_state_rationale": "   "},
             "no rationale"),
            ({"ticker": "EEE", "thesis_state": "SOLID",
              "thesis_state_rationale": "x"}, "not declared")):
        try:
            require(bad_action)
            ok("A4 refuses: " + needle, False, "did not refuse")
        except ThesisStateRefused as e:
            ok("A4 refuses on %s" % needle, needle in str(e), str(e)[:150])
    g = gate([good, {"ticker": "ZZZ"}])
    ok("A4 gate() names each refusal rather than failing anonymously",
       g["state"] == "REFUSED" and g["refused"][0]["ticker"] == "ZZZ" and g["n_ok"] == 1, g)

    # ── A8: flag False ⇒ pass-through, and the control is not vacuous ───────────────────
    import isa_policy as _p
    prev = _p.V2_FLAGS.get("single_sizing_authority")
    _p.V2_FLAGS["single_sizing_authority"] = False
    ok("A8 flag False ⇒ every rung passes through unchanged under every state",
       all(apply(r, st)["rung_out"] == r for r in order for st in STATES))
    ok("A8 flag False ⇒ require() does not refuse a null state",
       require({"ticker": "X"})["ok"] is True)
    _p.V2_FLAGS["single_sizing_authority"] = True
    ok("A8-neg flag True ⇒ WATCH caps again (the rollback control is not vacuous)",
       apply("HIGH", WATCH)["rung_out"] == "STARTER")
    if prev is None:
        _p.V2_FLAGS.pop("single_sizing_authority", None)
    else:
        _p.V2_FLAGS["single_sizing_authority"] = prev

    # ── the ladder has ONE home ─────────────────────────────────────────────────────────
    import position_sizing as _ps
    ok("the ladder is READ from position_sizing, never restated here",
       ladder() == _ps.ladder())


    # ═════════════ ISA-0760 — THE THESIS JOURNAL (J-series, in an isolated root) ═════════════
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tj0760_")
    try:
        shutil.copy(os.path.join(HERE, STORE_FILE), os.path.join(tmp, STORE_FILE))
        with open(os.path.join(tmp, "transaction_ledger.json"), "w") as fh:
            json.dump({"entries": [{"date": "2026-03-10", "type": "buy", "asset_class": "stock",
                                    "ticker": "MU", "quantity": 1}]}, fh)
        before = _load_declared(tmp)
        ok("J0 parity: empty journal -> load_states() == the declared store, row for row",
           load_states(tmp) == before)
        q = {"condition_id": "Q1", "kind": "QUANTITATIVE", "description": "HBM revenue growth",
             "field": "hbm_rev_yoy", "source": "issuer 10-Q", "operator": ">=", "threshold": 0.2,
             "unit": "ratio", "max_age_days": 120}
        ev_c = {"condition_id": "E1", "kind": "EVENT", "description": "no HBM4 qualification loss",
                "event": "HBM4 qualification loss at a hyperscaler",
                "source_contract": "issuer_freshness event review"}
        ql = {"condition_id": "L1", "kind": "QUALITATIVE", "description": "pricing discipline",
              "question": "Is DRAM supply discipline holding?", "evidence_required": "earnings call",
              "review_by": "2027-01-31"}
        base_c = dict(ticker="ZZT", route_id="growth", effective_from="2026-10-04",
                      basis=BASIS_ENTRY, thesis_summary="fixture", conditions=[q, ev_c, ql],
                      contract_version=1, created_by="selftest", evidence_basis="fixture",
                      source_underwriting_case_id="UWC-2026-10-03-ZZT-abc")
        c1 = build_contract(**base_c)
        ok("J1 a valid ENTRY contract records", record(c1, root=tmp)["written"] is True)
        ok("J2 an identical re-submission is an idempotent no-op (retry-safe)",
           record(c1, root=tmp)["existing"] is True and len(load_journal(tmp)) == 1)

        def refuses(name, fn, needle):
            try:
                fn()
                ok(name, False, "did not refuse")
            except ThesisRecordRefused as e:
                ok(name, needle in str(e), str(e)[:160])

        refuses("J3 a second version-1 contract (mutation in place) REFUSES",
                lambda: record(build_contract(**dict(base_c, thesis_summary="edited")), root=tmp),
                "already exists")
        refuses("J4 QUANTITATIVE without threshold REFUSES",
                lambda: record(build_contract(**dict(base_c, ticker="ZQ1", conditions=[
                    {k: v for k, v in q.items() if k != "threshold"}])), root=tmp), "missing")
        refuses("J5 EVENT without source_contract REFUSES",
                lambda: record(build_contract(**dict(base_c, ticker="ZQ2", conditions=[
                    {k: v for k, v in ev_c.items() if k != "source_contract"}])), root=tmp),
                "missing")
        refuses("J6 QUALITATIVE without review_by REFUSES",
                lambda: record(build_contract(**dict(base_c, ticker="ZQ3", conditions=[
                    {k: v for k, v in ql.items() if k != "review_by"}])), root=tmp), "missing")
        refuses("J7 a retired A/B/C path as route REFUSES",
                lambda: record(build_contract(**dict(base_c, ticker="ZQ4", route_id="Path A")),
                               root=tmp), "retired")
        refuses("J8 ENTRY_CONTEMPORANEOUS on a position first bought in an earlier month REFUSES "
                "(no backfilled entry thesis)",
                lambda: record(build_contract(**dict(base_c, ticker="MU")), root=tmp),
                "CURRENT_REUNDERWRITTEN")
        cmu = build_contract(**dict(base_c, ticker="MU", basis=BASIS_REUNDERWRITTEN))
        ok("J9 the same MU contract labelled CURRENT_REUNDERWRITTEN records",
           record(cmu, root=tmp)["written"] is True)
        refuses("J10 a contract with no underwriting case and no absent-reason REFUSES",
                lambda: record(build_contract(**dict(base_c, ticker="ZQ5",
                                                     basis=BASIS_REUNDERWRITTEN,
                                                     source_underwriting_case_id=None)), root=tmp),
                "absent_reason")

        def res(cid, r, src=True):
            d = {"condition_id": cid, "result": r, "reason": "fixture %s" % r}
            if src:
                d.update(source="fixture", as_of="2026-10-04")
            return d

        def ev(results, agg, run="R1", tk="ZZT", cid=None):
            return build_evaluation(thesis_contract_id=cid or c1["thesis_contract_id"], ticker=tk,
                                    as_of="2026-10-04", run_id=run,
                                    condition_results=results, aggregate_thesis_state=agg,
                                    state_rationale="fixture rationale for %s" % agg,
                                    reviewer_or_automation="selftest")
        refuses("J11 an evaluation of an unknown contract REFUSES",
                lambda: record(ev([res("Q1", "CLEAR")], INTACT, cid="TC-NOPE"), root=tmp),
                "unknown contract")
        refuses("J12 an evaluation omitting a contract condition REFUSES",
                lambda: record(ev([res("Q1", "CLEAR"), res("E1", "CLEAR")], INTACT), root=tmp),
                "exactly once")
        refuses("J13 a BROKEN condition under an INTACT aggregate REFUSES",
                lambda: record(ev([res("Q1", "BROKEN"), res("E1", "CLEAR"), res("L1", "CLEAR")],
                                  INTACT), root=tmp), "thesis break")
        refuses("J14 an UNKNOWN condition under an INTACT aggregate REFUSES (missing != intact)",
                lambda: record(ev([res("Q1", "UNKNOWN", False), res("E1", "CLEAR"),
                                   res("L1", "CLEAR")], INTACT), root=tmp), "never read")
        refuses("J15 a CLEAR result with no source REFUSES (only UNKNOWN may lack evidence)",
                lambda: record(ev([res("Q1", "CLEAR", False), res("E1", "CLEAR"),
                                   res("L1", "CLEAR")], INTACT), root=tmp), "source")
        e_ok = ev([res("Q1", "CLEAR"), res("E1", "CLEAR"), res("L1", "CLEAR")], INTACT)
        ok("J16 a complete CLEAR evaluation records and PROJECTS", record(e_ok, root=tmp)["written"])
        st = load_states(tmp)
        ok("J16 load_states carries the journal state WITH its contract/evaluation ids",
           st["ZZT"]["state"] == INTACT and st["ZZT"]["thesis_evaluation_id"] == e_ok["evaluation_id"]
           and st["ZZT"]["thesis_contract_id"] == c1["thesis_contract_id"])
        ok("J17 the projection equals the newest evaluation (no drift)", projection_drift(tmp) == [])
        refuses("J18 a retry of run R1 with a DIFFERENT judgement REFUSES",
                lambda: record(ev([res("Q1", "WATCH"), res("E1", "CLEAR"), res("L1", "CLEAR")],
                                  WATCH), root=tmp), "conflicting")
        # must-fire through cap_rung (the held-review decision field)
        cr = cap_rung("ZZT", "EARNED_MAX", states=load_states(tmp), root=tmp)
        ok("MF1 CLEAR/INTACT -> no block, rung unchanged, ids bound",
           cr["blocks_new_capital"] is False and cr["rung_out"] == "EARNED_MAX"
           and cr["thesis_evaluation_id"] == e_ok["evaluation_id"])
        e_b = ev([res("Q1", "BROKEN"), res("E1", "CLEAR"), res("L1", "CLEAR")], BROKEN, run="R2")
        record(e_b, root=tmp)
        cr = cap_rung("ZZT", "EARNED_MAX", states=load_states(tmp), root=tmp)
        ok("MF2 a BROKEN condition reaches the existing BROKEN block (+ §10 replacement)",
           cr["blocks_new_capital"] is True and cr["enters_replacement_comparison"] is True)
        e_u = ev([res("Q1", "UNKNOWN", False), res("E1", "CLEAR"), res("L1", "CLEAR")], WATCH,
                 run="R3")
        refuses("J25 BROKEN -> WATCH (a RAISE) without approved_by REFUSES (governance)",
                lambda: record(e_u, root=tmp), "approved_by")
        e_u = ev([res("Q1", "UNKNOWN", False), res("E1", "CLEAR"), res("L1", "CLEAR")], WATCH,
                 run="R3")
        e_u["approved_by"] = "Raj (selftest)"
        e_u = build_evaluation(**{k: v for k, v in e_u.items()
                                  if k not in ("evaluation_id", "record_id", "schema_version",
                                               "record_type")})
        record(e_u, root=tmp)
        cr = cap_rung("ZZT", "EARNED_MAX", states=load_states(tmp), root=tmp)
        ok("MF3 an UNKNOWN condition blocks NEW capital although WATCH alone would not",
           cr["blocks_new_capital"] is True and cr["thesis_evidence_incomplete"] == ["Q1"]
           and cr["rung_out"] == "STARTER")
        ok("MF3-neg WATCH with every condition observed does NOT block (the block is the "
           "UNKNOWN, not the state)",
           apply("EARNED_MAX", WATCH)["blocks_new_capital"] is False)
        cr_leg = cap_rung("MU", "EARNED_MAX", states=load_states(tmp), root=tmp)
        ok("MF4 a legacy holding with no evaluation keeps its declared state, typed "
           "NOT_CAPTURED_CONTEMPORANEOUSLY, and is not blocked by the absence",
           cr_leg["thesis_contract_state"] == NOT_CAPTURED
           and cr_leg["state"] == before["MU"]["state"])
        # newest-by-identity, not by mtime: an older as_of appended LATER does not win
        e_old = ev([res("Q1", "CLEAR"), res("E1", "CLEAR"), res("L1", "CLEAR")], WATCH, run="R0")
        record(e_old, root=tmp)
        ok("J19 newest = (as_of, append sequence); state follows the last-appended same-day record",
           load_states(tmp)["ZZT"]["thesis_evaluation_id"] == e_old["evaluation_id"])
        # versioning
        c2 = build_contract(**dict(base_c, contract_version=2,
                                   supersedes_contract_id=c1["thesis_contract_id"],
                                   thesis_summary="v2", effective_from="2026-11-01"))
        ok("J20 version 2 supersedes version 1", record(c2, root=tmp)["written"])
        lin = lineage_for("ZZT", tmp)
        ok("J20 lineage_for names v2 as current and flags that it has not yet been evaluated",
           lin["thesis_contract_id"] == c2["thesis_contract_id"]
           and lin["evaluation_state"] == "NOT_EVALUATED_AGAINST_CURRENT_CONTRACT")
        ok("J21 a ticker with no contract is NOT_CAPTURED_CONTEMPORANEOUSLY",
           lineage_for("AVGO", tmp)["state"] == NOT_CAPTURED)
        # hand-edit of a projected row is DETECTED (negative control for projection_drift)
        with open(os.path.join(tmp, STORE_FILE)) as fh:
            d = json.load(fh)
        d["states"]["ZZT"]["state"] = STRENGTHENING
        with open(os.path.join(tmp, STORE_FILE), "w") as fh:
            json.dump(d, fh)
        ok("J22 a hand-edited projected row is caught by projection_drift()",
           projection_drift(tmp) == ["ZZT"])
        with open(journal_path(tmp), "a") as fh:
            fh.write("{not json\n")
        refuses("J23 a corrupt journal line REFUSES the read (R4.9)", lambda: load_journal(tmp),
                "corrupt")
        bad = dict(c1)
        bad["schema_version"] = "0.9"
        refuses("J24 an undeclared schema_version REFUSES",
                lambda: record(dict(bad, record_id="TR-x"), root=tempfile.mkdtemp()), "schema_version")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nthesis_state selftest: %d assertion(s), %d FAIL(s)%s"
          % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


_ASSERTS = [0]

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _o = print

        def print(*a, **k):                                             # noqa: A001
            if a and isinstance(a[0], str) and a[0].startswith(("  PASS", "  FAIL")):
                _ASSERTS[0] += 1
            _o(*a, **k)
        sys.exit(_selftest())
    # ISA-0760 CLI — the Sunday session writes contracts/evaluations here, never by hand-editing
    #   thesis_states.json. A JSON file holds ONE record spec or a list; ids are derived.
    if "--record" in sys.argv:
        _specs = json.load(open(sys.argv[sys.argv.index("--record") + 1], encoding="utf-8"))
        _specs = _specs if isinstance(_specs, list) else [_specs]
        _dry = "--dry-run" in sys.argv
        _out = []
        for _sp in _specs:
            _sp = dict(_sp)
            _rt = _sp.pop("record_type", None)
            _rec = (build_contract(**_sp) if _rt == CONTRACT else
                    build_evaluation(**_sp) if _rt == EVALUATION else None)
            if _rec is None:
                raise SystemExit("record_type must be CONTRACT or EVALUATION")
            _r = record(_rec, dry_run=_dry)
            _r["id"] = _rec.get("thesis_contract_id") or _rec.get("evaluation_id")
            _out.append(_r)
        print(json.dumps(_out, indent=1))
        sys.exit(0)
    if "--lineage" in sys.argv:
        print(json.dumps(lineage_for(sys.argv[sys.argv.index("--lineage") + 1]), indent=1))
        sys.exit(0)
    if "--project" in sys.argv:
        print(json.dumps(project(dry_run="--dry-run" in sys.argv), indent=1))
        sys.exit(0)
    if "--check" in sys.argv:
        _recs = load_journal()
        _drift = projection_drift()
        print(json.dumps({"records": len(_recs), "projection_drift": _drift,
                          "qualitative_overdue": qualitative_overdue()}, indent=1))
        sys.exit(1 if _drift else 0)
    print(json.dumps({"states": list(STATES), "ceilings": STATE_CEILING,
                      "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL,
                      "ladder": ladder()}, indent=1))
