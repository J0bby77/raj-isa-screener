#!/usr/bin/env python3
"""
underwriting.py — ISA-0722 (with ISA-0720 / ISA-0721). THE canonical underwriting-case store and
the E[r] lifecycle: original (decision-month) E[r], current (monthly) E[r], execution E[r].

Authority: ChatGPT Astra 6 Audit/ISA_Oct2026_Forecast_Underwriting_Integrity_BuildSpec_20Sep2026.md
§5.2 (five concepts never collapsed), §6 (Raj's mandatory E[r] lifecycle), §7.2 (case contract),
§8 (event invalidation), §12 (irreversible capture); Raj D26 (ISA-0672: E[r] horizon 12 months);
ISA_Engineering_Rules.md R4.1, R4.2, R4.11, R6.4, R6.5, R20.1, R20.2, R20.4.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
Until 23-Sep-2026 an expected return existed only as a COLUMN — `expected_return_12_24m` in each
month's watchlist_scored / step9_pre, overwritten the next month — and its growth term was the
trailing quarterly `earningsGrowth` under a forward name (ISA-0720). `retention.record_entry`
(ISA-0418) was built to persist an entry E[r] and was called by nothing: position_underwriting.json
has never held a lot. So no decision's E[r] can be recovered, no prediction can be scored against
its outcome, and nothing could report Original | Current | delta for a single holding.

⚑ FIVE CONCEPTS, NEVER COLLAPSED (§5.2): forecast OBSERVATIONS (what a source said, for which
period); the underwriting CASE (the reconciled economic case); E[r] (the return the case implies,
with its horizon and authority class); the REQUIRED return / hurdle (a comparator only — stored as
a reference, never as a forecast); and the CAPITAL DECISION (decision_ledger), which binds a case
id at the moment it is written.

⚑ APPEND-ONLY. A case is written once under a content-derived id and never edited. The CURRENT
E[r] is the latest case; the ORIGINAL is the case the position's first positive capital decision
bound — resolved from the ledger, never from a second hand-kept tracker. A legacy holding with no
contemporaneous case renders NOT_CAPTURED_CONTEMPORANEOUSLY; nothing is reconstructed (R7.5).

⚑ NOT A SECOND E[r] ENGINE. The scalar is `expected_return.compute_expected_return`'s, consumed
as stamped on the row. This module types it, identifies it and keeps it.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["underwriting_cases"] = False` — capture writes nothing and
the report returns state DISABLED, which reads as UNKNOWN, never as "every holding underwritten".
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from typing import Dict, List, Optional

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"
STORE = "underwriting_cases.jsonl"

# authority classes (§7.2) and the non-numeric states a case may carry (§6.3)
MECHANICAL = "MECHANICAL"
STRUCTURED_JUDGEMENT = "STRUCTURED_JUDGEMENT"
NOT_QUANTIFIABLE = "NOT_QUANTIFIABLE"
VALID_STATES = ("VALID_MECHANICAL", "VALID_MECHANICAL_PROXY", "VALID_STRUCTURED_JUDGEMENT")
NOT_CAPTURED = "NOT_CAPTURED_CONTEMPORANEOUSLY"
PURPOSES = ("HELD_CURRENT", "CANDIDATE", "DECISION", "SELFTEST")


class UnderwritingRefused(RuntimeError):
    """Raised rather than writing a case that would misstate what it is (R4.3/R4.7)."""


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.flag("underwriting_cases"))
    except Exception:                                                   # noqa: BLE001
        return True


def _today() -> str:
    return datetime.date.today().isoformat()


def _num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def store_path(root=None) -> str:
    return os.path.join(root or HERE, STORE)


def required_return_ref():
    """The hurdle this case is COMPARED against — a reference, never a forecast (§5.2 item 4).

    ⚑ ISA-0740 (24-Sep-2026): also the ONE home of the hurdle's METHODOLOGY identity. The
    methodology is the derivation formula + the declared (policy) constants + POLICY_VERSION; the
    anchor REQUIRED_RETURN_MID moves with NAV every month and is deliberately NOT part of it, so a
    routine numeric re-derivation keeps the identity while a policy change breaks it."""
    as_of = _today()
    try:
        import isa_policy
        import scoring_config as _sc
        buf = getattr(_sc, "ER_FRICTION_BUFFER", None)
        pv = getattr(isa_policy, "POLICY_VERSION", None)
        mid = "ER_DEPLOY_FLOOR=REQUIRED_RETURN_MID+ER_FRICTION_BUFFER(%s)|policy=%s" % (buf, pv)
        return {"id": "isa_policy.derived('ER_DEPLOY_FLOOR')",
                "value_pct": isa_policy.derived("ER_DEPLOY_FLOOR"),
                "methodology_id": mid, "policy_version": pv, "as_of": as_of,
                "derivation": {"REQUIRED_RETURN_MID": getattr(_sc, "REQUIRED_RETURN_MID", None),
                               "ER_FRICTION_BUFFER": buf},
                "role": "COMPARATOR_ONLY - required return / deploy floor, not an expected return"}
    except Exception as exc:                                            # noqa: BLE001
        return {"id": "isa_policy.derived('ER_DEPLOY_FLOOR')", "value_pct": None,
                "methodology_id": None, "as_of": as_of,
                "role": "COMPARATOR_ONLY", "why": "unavailable (%s)" % type(exc).__name__}


_required_return_ref = required_return_ref        # legacy private name (callers unchanged)


def _er_state_of(row: dict) -> str:
    """The typed E[r] state carried by a row. A row produced before ISA-0720/0721 carries no
    forward-growth provenance and no state; its growth term may be the trailing quarterly figure,
    so it is UNVERIFIED_LEGACY_PARSER and never admissible (R4.3)."""
    st = row.get("er_state")
    if st:
        return st
    if row.get("expected_return_12_24m") is None:
        return "MISSING_REQUIRED_INPUT"
    return "UNVERIFIED_LEGACY_PARSER"


def case_from_row(ticker, row, *, as_of, purpose, route, source_artefact=None,
                  event_review_id=None, note=None) -> dict:
    """A MECHANICAL case from a stamped screen/pre-run row. Consumes; never recomputes."""
    if purpose not in PURPOSES:
        raise UnderwritingRefused("purpose %r is not declared (%s)" % (purpose, ", ".join(PURPOSES)))
    r = row or {}
    st = _er_state_of(r)
    er = _num(r.get("expected_return_12_24m"))
    if st not in VALID_STATES:
        er_value = None if st == "MISSING_REQUIRED_INPUT" else er
    else:
        er_value = er
    case = {
        "schema_version": SCHEMA_VERSION, "case_kind": "UNDERWRITING_CASE",
        "ticker": str(ticker).upper(), "as_of": as_of, "purpose": purpose, "route": route,
        "authority_class": MECHANICAL if st in VALID_STATES else NOT_QUANTIFIABLE,
        "observations": {
            "fwd_eps_growth": {"value": r.get("fwd_eps_growth"),
                               "basis": r.get("fwd_eps_growth_basis"),
                               "period": r.get("fwd_eps_growth_period"),
                               "source_field": r.get("fwd_eps_growth_source")},
            # the trailing quantity is RETAINED and LABELLED; it is not an input (ISA-0720)
            "eps_growth_trailing_q": {"value": r.get("eps_growth_trailing_q"),
                                      "basis": "HISTORICAL_TRAILING_QUARTER_YOY",
                                      "used_in_er": False},
        },
        "price_basis": {"price": _num(r.get("current_price") or r.get("price")),
                        "currency": r.get("currency"), "as_of": as_of},
        "er": {"state": st, "value_pct": er_value,
               # a partial / unverified figure is kept for AUDIT, labelled, never admissible
               "raw_scalar_pct": er if er_value is None else None,
               "horizon_months": r.get("er_horizon_months"),
               "quantity_basis": r.get("er_quantity_basis"),
               "method_id": r.get("er_method_id"),
               "components_pct": {"growth": r.get("er_growth"), "rerate": r.get("er_rerate"),
                                  "yield": r.get("er_yield")},
               "confidence": r.get("er_confidence"),
               "confidence_semantics": "COVERAGE_WEIGHT - NOT a probability (§7.2)",
               "basis": r.get("er_basis")},
        "required_return_ref": _required_return_ref(),
        "event_review_id": event_review_id,
        "event_state": ("EVENT_REVIEWED" if event_review_id else
                        "EVENT_REVIEW_UNAVAILABLE - ISA-0713 has not produced an issuer review "
                        "for this month; the case is NOT event-cleared"),
        "source_artefact": source_artefact,
        "note": note,
    }
    case["admissible_for_positive_size"] = bool(st in VALID_STATES and er_value is not None)
    case["case_id"] = case_id(case)
    return case


def case_not_quantifiable(ticker, *, as_of, purpose, route, reason, state, refs=None) -> dict:
    """A NAMED non-numeric case (§6.3) — a VCI binary governed by the expected-loss budget, a
    post-event name awaiting successor underwriting, a holding with no scored row. Never 0."""
    case = {"schema_version": SCHEMA_VERSION, "case_kind": "UNDERWRITING_CASE",
            "ticker": str(ticker).upper(), "as_of": as_of, "purpose": purpose, "route": route,
            "authority_class": NOT_QUANTIFIABLE,
            "er": {"state": state, "value_pct": None, "horizon_months": None,
                   "method_id": None, "why": reason},
            "required_return_ref": _required_return_ref(),
            "refs": refs or {}, "admissible_for_positive_size": False}
    case["case_id"] = case_id(case)
    return case


def case_id(case: dict) -> str:
    core = {k: case.get(k) for k in ("ticker", "as_of", "purpose", "route", "authority_class",
                                     "er", "observations", "price_basis", "event_review_id",
                                     "source_artefact", "refs")}
    h = hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return "UWC-%s-%s-%s" % (case.get("as_of"), case.get("ticker"), h[:10])


# ───────────────────────────────────────────────────────────── the append-only store ─────

def load(root=None) -> List[dict]:
    p = store_path(root)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError as exc:
                    raise UnderwritingRefused("%s line %d is not JSON (%s) - the store is "
                                              "corrupt; nothing is read past it (R4.9)"
                                              % (STORE, i, exc))
    return out


def append(cases: List[dict], root=None, dry_run=False) -> dict:
    """Write each case ONCE. An existing case_id is left untouched (append-only, R6.4)."""
    _fi_mark("underwriting", "append")
    have = {c.get("case_id") for c in load(root)}
    new = [c for c in cases if c.get("case_id") not in have]
    if new and not dry_run:
        with open(store_path(root), "a", encoding="utf-8") as fh:
            for c in new:
                fh.write(json.dumps(c, sort_keys=True, default=str, ensure_ascii=False) + "\n")
    return {"n_offered": len(cases), "n_written": 0 if dry_run else len(new),
            "n_existing": len(cases) - len(new), "dry_run": bool(dry_run)}


def cases_for(ticker, root=None) -> List[dict]:
    t = str(ticker).upper()
    return sorted((c for c in load(root) if c.get("ticker") == t),
                  key=lambda c: str(c.get("as_of") or ""))


def case_for_decision(ticker, date, root=None) -> Optional[dict]:
    """The case a capital decision dated `date` binds: the latest case for the ticker dated in
    the SAME month on or before the decision. Older months do not qualify (a stale case is not
    the decision-month case, §6.1)."""
    d = str(date or "")
    cands = [c for c in cases_for(ticker, root)
             if str(c.get("as_of") or "")[:7] == d[:7] and str(c.get("as_of") or "") <= d]
    return cands[-1] if cands else None


def latest_case(ticker, root=None, month=None) -> Optional[dict]:
    cs = cases_for(ticker, root)
    if month:
        cs = [c for c in cs if str(c.get("as_of") or "")[:7] == month]
    return cs[-1] if cs else None


# ─────────────────────────────────────────────────── ORIGINAL / CURRENT / DELTA ─────

def original_for(ticker, *, ledger_path, position_first_entry_date=None, root=None) -> dict:
    """The ORIGINAL E[r]: the case bound by the position's FIRST positive capital decision.

    Resolved from decision_ledger (the decision owns the binding; this module never keeps a
    second tracker). A decision without a bound case, or no decision at all, is
    NOT_CAPTURED_CONTEMPORANEOUSLY — never a reconstructed figure (R7.5)."""
    try:
        import decision_ledger as _dl
        ents = _dl.load_ledger(ledger_path).get("entries", [])
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNKNOWN", "why": "ledger unreadable (%s)" % exc}
    t = str(ticker).upper()
    buys = sorted((e for e in ents if str(e.get("ticker") or "").upper() == t
                   and str(e.get("decision") or "").lower() in ("buy", "top_up")),
                  key=lambda e: str(e.get("date") or ""))
    if position_first_entry_date:
        lo = (datetime.date.fromisoformat(str(position_first_entry_date)[:10])
              - datetime.timedelta(days=62)).isoformat()
        buys = [e for e in buys if str(e.get("date") or "") >= lo]
    bound = [e for e in buys if e.get("underwriting_case_id")]
    if not bound:
        return {"state": NOT_CAPTURED, "decision_id": (buys[0].get("decision_id") if buys else None),
                "why": ("no positive capital decision on this position carries an underwriting "
                        "case id - its original E[r] was never recorded and is not reconstructed")}
    first = bound[0]
    cid = first["underwriting_case_id"]
    case = next((c for c in load(root) if c.get("case_id") == cid), None)
    return {"state": "CAPTURED" if case else "CASE_ID_UNRESOLVED",
            "decision_id": first.get("decision_id"), "case_id": cid,
            "er": (case or {}).get("er") or first.get("er_at_decision"),
            "decision_date": first.get("date")}


def delta(original: dict, current: dict) -> dict:
    """Original -> current. A method change is flagged, never presented as like-for-like."""
    oe, ce = (original or {}).get("er") or {}, (current or {}).get("er") or {}
    ov, cv = _num(oe.get("value_pct")), _num(ce.get("value_pct"))
    if ov is None or cv is None:
        return {"delta_pp": None, "comparable": False,
                "why": "no delta: original %s / current %s" % (oe.get("state") or (original or {}).get("state"),
                                                              ce.get("state"))}
    if oe.get("method_id") != ce.get("method_id"):
        return {"delta_pp": None, "raw_delta_pp_non_comparable": round(cv - ov, 1),
                "comparable": False, "flag": "METHOD_CHANGED",
                "why": "method %s -> %s: the raw difference mixes data and method change (R20.4)"
                       % (oe.get("method_id"), ce.get("method_id"))}
    return {"delta_pp": round(cv - ov, 1), "comparable": True, "why": "same method"}


# ─────────────────────────────────────────── the monthly capture (pre-run, R4.11) ─────

def _declared_binaries(root):
    try:
        import position_sizing as _ps
        return _ps.load_declared_binaries(root) or {}
    except Exception:                                                   # noqa: BLE001
        return {}


def _aliases(declared):
    out = {}
    for t, v in (declared or {}).items():
        if isinstance(v, dict):
            for a in v.get("aliases") or []:
                out[str(t).upper()] = str(a).upper()
    return out


_LAST_CAPTURE: Dict[str, object] = {}


def capture_month(*, portfolio_path, scored_path=None, step9_path=None, as_of=None, root=None,
                  ledger_path=None, lifecycle=None, min_hold=None, dry_run=False) -> dict:
    """ONE monthly capture: a CURRENT case for N of N held direct stocks, a CANDIDATE case for
    every T1-qualified stack row, and the Original | Current | delta rows the report renders.

    `lifecycle`: summary.held_position_review.lifecycle (so a post-event name is typed from the
    ONE lifecycle engine rather than re-decided here)."""
    _fi_mark("underwriting", "capture_month")
    root = root or HERE
    as_of = as_of or _today()
    ledger_path = ledger_path or os.path.join(root, "decision_ledger.json")
    if not _flag():
        return {"state": "DISABLED", "why": "isa_policy.V2_FLAGS['underwriting_cases'] is False "
                                            "- UNKNOWN, never 'every holding underwritten' (R4.3)"}
    with open(portfolio_path, encoding="utf-8") as fh:
        pf = json.load(fh)
    stocks = [s for s in (pf.get("stocks") or []) if s.get("ticker")]
    scored = {}
    if scored_path and os.path.exists(scored_path):
        with open(scored_path, encoding="utf-8") as fh:
            scored = (json.load(fh).get("tickers") or {})
    declared = _declared_binaries(root)
    alias = _aliases(declared)
    src = os.path.basename(scored_path) if scored_path else None
    cases, rows = [], []
    for s in stocks:
        t = str(s["ticker"]).upper()
        d = declared.get(t) or {}
        lc = (lifecycle or {}).get(t) or {}
        if d.get("is_binary"):
            if lc.get("state") in ("EXIT", "SUCCESSOR_VCI", "PATH_A_RECLASSIFIED"):
                c = case_not_quantifiable(
                    t, as_of=as_of, purpose="HELD_CURRENT", route="vci",
                    state="NOT_DEFENSIBLY_QUANTIFIABLE",
                    reason=("post-event: the ONE lifecycle engine decided %s (%s); a scalar E[r] "
                            "is not defensible from routine data" % (lc.get("state"),
                                                                     ", ".join(lc.get("reason_codes") or []))),
                    refs={"lifecycle_decision_id": lc.get("decision_id"),
                          "lifecycle_input_id": lc.get("input_id")})
            else:
                c = case_not_quantifiable(
                    t, as_of=as_of, purpose="HELD_CURRENT", route="vci",
                    state="NOT_DEFENSIBLY_QUANTIFIABLE",
                    reason=("a VCI binary: its capital is governed by p_thesis / L and the 1.5% "
                            "expected-loss budget; a growth+re-rate E[r] is not its case (QBTS "
                            "semantics: a win-case FV is not an expected return)"))
        else:
            row = scored.get(t) or scored.get(alias.get(t, ""))
            if row is None:
                c = case_not_quantifiable(t, as_of=as_of, purpose="HELD_CURRENT", route="growth",
                                          state="MISSING_ROW",
                                          reason="no row for %s in %s - the held name was not "
                                                 "scored this month" % (t, src))
            else:
                c = case_from_row(t, row, as_of=as_of, purpose="HELD_CURRENT", route="growth",
                                  source_artefact=src)
        cases.append(c)
        mh = ((min_hold or {}).get("positions") or {}).get(t) or {}
        orig = original_for(t, ledger_path=ledger_path,
                            position_first_entry_date=mh.get("position_first_entry_date"),
                            root=root)
        rows.append({"ticker": t, "value_gbp": s.get("value_gbp"),
                     "original": orig, "original_er_pct": ((orig.get("er") or {}).get("value_pct")),
                     "original_state": orig.get("state"),
                     "current_case_id": c["case_id"], "current_state": c["er"]["state"],
                     "current_er_pct": c["er"].get("value_pct"),
                     "horizon_months": c["er"].get("horizon_months"),
                     "method_id": c["er"].get("method_id"),
                     "admissible_for_positive_size": c.get("admissible_for_positive_size"),
                     "event_state": c.get("event_state"),
                     "delta": delta(orig, c)})
    # candidates: every T1-qualified stack row gets a case, so the decision can bind it
    n_cand = 0
    if step9_path and os.path.exists(step9_path):
        with open(step9_path, encoding="utf-8") as fh:
            s9 = json.load(fh)
        held = {r["ticker"] for r in rows}
        for r in s9.get("deployable_stack") or []:
            if not r.get("t1_qualified") or not r.get("ticker"):
                continue
            t = str(r["ticker"]).upper()
            if t in held:
                continue                      # the held case above is the current case
            srow = dict(scored.get(t) or {}, **{k: v for k, v in r.items() if v is not None})
            cases.append(case_from_row(t, srow, as_of=as_of, purpose="CANDIDATE", route="growth",
                                       source_artefact=os.path.basename(step9_path)))
            n_cand += 1
    wr = append(cases, root=root, dry_run=dry_run)
    # ISA-0740: the in-process hand-off to the SHADOW execution ceiling (Step 8x), so a dry run
    # (which writes nothing) still exercises the calculation on this month's real cases.
    _LAST_CAPTURE.clear()
    _LAST_CAPTURE.update({"as_of": as_of, "cases": list(cases), "dry_run": bool(dry_run)})
    n_exp = len(stocks)
    by_state = {}
    for r in rows:
        by_state.setdefault(r["current_state"], []).append(r["ticker"])
    return {"state": "OK", "as_of": as_of, "n_expected": n_exp, "n_cases_held": len(rows),
            "complete": len(rows) == n_exp, "by_state": by_state, "rows": rows,
            "n_candidate_cases": n_cand, "write": wr,
            "case_ids": [c.get("case_id") for c in cases],
            "not_captured_original": sorted(r["ticker"] for r in rows
                                            if r["original_state"] == NOT_CAPTURED),
            "basis": ("ISA-0722: one append-only case per held stock per month (N of N, "
                      "denominated by the broker book) + one per T1 candidate; ORIGINAL resolved "
                      "from the first positive decision's bound case; delta only like-for-like.")}


# ─────────────────────────────────────────────────────────── selftest ─────────────

def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:240]) if not cond else ""))
        if not cond:
            fails.append(name)

    td = tempfile.mkdtemp(prefix="uw_")
    good = {"expected_return_12_24m": 19.5, "er_state": "VALID_MECHANICAL", "er_horizon_months": 12,
            "er_method_id": "expected_return@aaa", "fwd_eps_growth": 0.111,
            "fwd_eps_growth_basis": "CONSENSUS_FORWARD", "fwd_eps_growth_period": "+1y",
            "fwd_eps_growth_source": "growth_estimates[+1y].stockTrend",
            "eps_growth_trailing_q": 0.635, "current_price": 90.0}
    c1 = case_from_row("GOOD", good, as_of="2026-10-03", purpose="CANDIDATE", route="growth")
    ok("a VALID mechanical case is admissible, carries its horizon and method, and keeps the "
       "hurdle as a SEPARATE comparator", c1["admissible_for_positive_size"]
       and c1["er"]["horizon_months"] == 12 and c1["required_return_ref"]["role"].startswith("COMPARATOR"), c1)
    ok("the trailing quantity is retained and labelled NOT USED (ISA-0720)",
       c1["observations"]["eps_growth_trailing_q"]["used_in_er"] is False)
    miss = case_from_row("MISS", {"expected_return_12_24m": None, "er_state": "MISSING_REQUIRED_INPUT"},
                         as_of="2026-10-03", purpose="CANDIDATE", route="growth")
    ok("⚑ MUST-FIRE: a MISSING E[r] is a named state with NO scalar and is not admissible (never 0)",
       miss["er"]["value_pct"] is None and not miss["admissible_for_positive_size"], miss["er"])
    legacy = case_from_row("LEG", {"expected_return_12_24m": 44.4}, as_of="2026-10-03",
                           purpose="HELD_CURRENT", route="growth")
    ok("⚑ MUST-FIRE: a row from BEFORE the parser fix (no state, no provenance) is "
       "UNVERIFIED_LEGACY_PARSER and not admissible - the COCO 44.4% shape",
       legacy["er"]["state"] == "UNVERIFIED_LEGACY_PARSER" and not legacy["admissible_for_positive_size"]
       and legacy["er"]["value_pct"] == 44.4, legacy["er"])
    w1 = append([c1, miss], root=td)
    w2 = append([c1], root=td)
    ok("append-only: a case is written once; re-offering it writes nothing",
       w1["n_written"] == 2 and w2["n_written"] == 0 and len(load(td)) == 2, (w1, w2))
    ok("the decision-month case binds; a case from a DIFFERENT month does not",
       case_for_decision("GOOD", "2026-10-05", root=td)["case_id"] == c1["case_id"]
       and case_for_decision("GOOD", "2026-11-02", root=td) is None)
    # original: decision binds, later cases do not rewrite it
    import decision_ledger as _dl
    lp = os.path.join(td, "decision_ledger.json")
    e = _dl.log_decision(lp, "GOOD", "growth", "buy", date="2026-10-05")
    ok("⚑ MUST-FIRE (R4.11): writing a BUY decision BINDS the decision-month case id and an E[r] "
       "snapshot, at the moment of writing", e.get("underwriting_case_id") == c1["case_id"]
       and (e.get("er_at_decision") or {}).get("value_pct") == 19.5, e)
    c2 = case_from_row("GOOD", dict(good, expected_return_12_24m=12.0), as_of="2026-11-02",
                       purpose="HELD_CURRENT", route="growth")
    append([c2], root=td)
    _dl.log_decision(lp, "GOOD", "growth", "top_up", date="2026-11-04")
    o = original_for("GOOD", ledger_path=lp, root=td)
    ok("⚑ MUST-FIRE: the ORIGINAL E[r] is immutable - a later monthly case and a later TOP-UP "
       "(its own tranche) do not rewrite it", o["case_id"] == c1["case_id"]
       and o["er"]["value_pct"] == 19.5, o)
    dd = delta(o, c2)
    ok("delta is like-for-like under the same method (-7.5pp)", dd["comparable"] and dd["delta_pp"] == -7.5, dd)
    c3 = case_from_row("GOOD", dict(good, er_method_id="expected_return@bbb"), as_of="2026-11-02",
                       purpose="HELD_CURRENT", route="growth")
    ok("⚑ MUST-FIRE: a METHOD CHANGE is flagged, never presented as a like-for-like delta",
       delta(o, c3).get("flag") == "METHOD_CHANGED" and delta(o, c3)["delta_pp"] is None)
    ok("NEGATIVE CONTROL: a legacy holding with no bound decision is NOT_CAPTURED_CONTEMPORANEOUSLY, "
       "never a reconstructed figure",
       original_for("NEVER", ledger_path=lp, root=td)["state"] == NOT_CAPTURED)
    # N of N on the real book (read-only: dry_run, isolated ledger copy)
    pf = os.path.join(HERE, "portfolio_data_sep_2026.json")
    sc = os.path.join(HERE, "watchlist_scored_sep_2026.json")
    if os.path.exists(pf) and os.path.exists(sc):
        lp2 = os.path.join(td, "dl_real.json")
        if os.path.exists(os.path.join(HERE, "decision_ledger.json")):
            shutil.copy(os.path.join(HERE, "decision_ledger.json"), lp2)
        cap = capture_month(portfolio_path=pf, scored_path=sc, as_of="2026-10-03", root=HERE,
                            ledger_path=lp2, dry_run=True,
                            lifecycle={"ABCL": {"state": "EXIT", "reason_codes": ["SUCCESSOR_UNPRICEABLE_BY_NATURE"],
                                                "decision_id": "DEC-x", "input_id": "LCI-x"}})
        ok("⚑ MUST-FIRE (real Sep-2026 book): N held -> N cases, every one a named state",
           cap["complete"] and cap["n_cases_held"] == cap["n_expected"] >= 6, cap.get("by_state"))
        st = {r["ticker"]: r["current_state"] for r in cap["rows"]}
        ok("the pre-fix artefact's Path-A names read UNVERIFIED_LEGACY_PARSER (their E[r] used "
           "trailing growth); ABCL and QBTS read NOT_DEFENSIBLY_QUANTIFIABLE",
           st.get("COCO") == "UNVERIFIED_LEGACY_PARSER"
           and st.get("ABCL") == "NOT_DEFENSIBLY_QUANTIFIABLE"
           and st.get("QBTS") == "NOT_DEFENSIBLY_QUANTIFIABLE", st)
        ok("every held name's ORIGINAL is NOT_CAPTURED_CONTEMPORANEOUSLY today - the truth, not a "
           "back-fill", set(cap["not_captured_original"]) == set(st), cap["not_captured_original"])
        ok("NEGATIVE CONTROL: a dry run writes nothing", cap["write"]["n_written"] == 0)
    shutil.rmtree(td, ignore_errors=True)
    if verbose:
        print("underwriting selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(1 if _selftest() else 0)
    print(__doc__)
