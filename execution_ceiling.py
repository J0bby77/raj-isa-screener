#!/usr/bin/env python3
"""
execution_ceiling.py - ISA-0740 (Raj 24-Sep-2026): the APPROVAL-PRESERVING EXECUTION CEILING.

⚑ WHAT THIS IS, AND WHAT IT IS NOT. An INTERIM_STEP_TO_TARGET execution invariant. It is NOT a
valuation model, NOT a fair value and NOT a replacement for canonical E[r] (the target
terminal-value E[r] is ISA-0744, SHADOW research). It answers exactly one question: given an
immutable, approved underwriting case, what is the highest price at which that SAME frozen economic
case still clears the CURRENT authorised hurdle?

    W_T          = approval_price x (1 + approved_Er) ^ h          (frozen on the case)
    Pmax         = W_T / (1 + current_authorised_hurdle) ^ h       (recomputed at execution)
    implied_Er(p) = (W_T / p) ^ (1/h) - 1

Two identities hold MECHANICALLY and are asserted on every calculation (never published otherwise):
    implied_Er(approval_price) == approved_Er
    implied_Er(Pmax)           == current_authorised_hurdle

SEMANTICS (Raj 24-Sep-2026):
  * frozen: the economic case (case_id = its content fingerprint), approval price, approved E[r],
    horizon, E[r] method identity, hurdle METHODOLOGY identity;
  * a routine numeric re-derivation of the hurdle under the SAME methodology does NOT invalidate the
    case - Pmax is recomputed against the current hurdle and its value/derivation/identity/as_of are
    stamped on the calculation;
  * a superseded economic case, a horizon change, an E[r] method change, a hurdle-methodology/policy
    change or a material issuer/event finding -> REVIEW_REQUIRED (never a silent recompute);
  * NO_AUTHORITY only when the underlying approved case is absent or invalid - NOT because
    approved_Er < current hurdle (a lower price can still clear it; that is published as a fact);
  * price <= Pmax is a NECESSARY execution condition only. `execution_check` never returns BUY:
    eligibility, evidence freshness, material-event review, E[r] admissibility, portfolio/risk/
    concentration and capital destination all still bind.

SHADOW (isa_policy.V2_FLAGS["execution_ceiling_shadow"]): published in the pre-run summary; no
consumer gates on it until Raj promotes it. Rollback: set the flag False -> DISABLED (UNKNOWN).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from typing import Dict, List, Optional

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = "APPROVAL-PRESERVING EXECUTION CEILING - not fair value, not an independent valuation (ISA-0740)"
SCHEMA_VERSION = "1.0.0"
IDENTITY_TOL = 1e-9

VALID = "VALID"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NO_AUTHORITY = "NO_AUTHORITY"
DISABLED = "DISABLED"
ERROR_IDENTITY = "ERROR_IDENTITY"

# the controls that still bind after the price condition (necessary-only contract)
REQUIRED_CONTROLS = ("eligibility", "evidence_freshness", "material_event_review",
                     "er_admissible", "portfolio_risk", "capital_destination")
PASS = "PASS"


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.flag("execution_ceiling_shadow"))
    except Exception:                                                   # noqa: BLE001
        return False                    # unknown -> DISABLED (UNKNOWN), never silently on


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def current_hurdle() -> dict:
    """The CURRENT authorised hurdle, from the ONE home the case also uses (underwriting)."""
    import underwriting as _uw
    return _uw.required_return_ref()


def current_method_id() -> Optional[str]:
    try:
        import expected_return as _er
        return _er.method_id()
    except Exception:                                                   # noqa: BLE001
        return None


def current_horizon_months() -> Optional[int]:
    try:
        import scoring_config as _sc
        return int(getattr(_sc, "ER_HORIZON_MONTHS"))
    except Exception:                                                   # noqa: BLE001
        return None


def _calc_id(case_id, hurdle, as_of):
    core = json.dumps({"case": case_id, "h": hurdle.get("value_pct"),
                       "m": hurdle.get("methodology_id"), "as_of": as_of}, sort_keys=True)
    return "XC-%s" % hashlib.sha256(core.encode("utf-8")).hexdigest()[:12]


def _case_authority(case: Optional[dict]) -> Optional[str]:
    """None when the underlying case can carry authority; else the NO_AUTHORITY reason."""
    if not case:
        return "NO_CASE"
    er = case.get("er") or {}
    if er.get("state") == "UNVERIFIED_LEGACY_PARSER":
        return "LEGACY_PARSER_CASE"          # every pre-ISA-0720 row (all of September 2026)
    if not case.get("admissible_for_positive_size"):
        return "CASE_NOT_ADMISSIBLE(%s)" % er.get("state")
    if _num(er.get("value_pct")) is None:
        return "MISSING_APPROVED_ER"
    if _num(er.get("horizon_months")) is None or _num(er.get("horizon_months")) <= 0:
        return "MISSING_HORIZON"
    p = _num((case.get("price_basis") or {}).get("price"))
    if p is None or p <= 0:
        return "MISSING_APPROVAL_PRICE"
    if _num(er.get("value_pct")) <= -100.0:
        return "NON_POSITIVE_TERMINAL_WEALTH"
    return None


def ceiling(case: Optional[dict], *, hurdle: Optional[dict] = None,
            method_id: Optional[str] = None, horizon_months: Optional[int] = None,
            newer_case_id: Optional[str] = None, material_event: Optional[dict] = None,
            as_of: Optional[str] = None) -> dict:
    """The ceiling for ONE frozen case against the CURRENT authorised hurdle.

    `newer_case_id`: the latest case for the same ticker if different from this one (superseded).
    `material_event`: {"material": bool, "ref": ...} from issuer/event review, if available."""
    _fi_mark("execution_ceiling", "ceiling")
    as_of = as_of or datetime.date.today().isoformat()
    out = {"schema_version": SCHEMA_VERSION, "label": LABEL, "as_of": as_of,
           "ticker": (case or {}).get("ticker"), "case_id": (case or {}).get("case_id"),
           "sufficient_for_buy": False}
    if not _flag():
        out.update(state=DISABLED, why="isa_policy.V2_FLAGS['execution_ceiling_shadow'] is False "
                                       "or unreadable - UNKNOWN, never a ceiling (R4.3)")
        return out
    na = _case_authority(case)
    if na:
        out.update(state=NO_AUTHORITY, reason=na,
                   why="the underlying approved case is absent or invalid; no ceiling is published")
        return out
    hurdle = hurdle if hurdle is not None else current_hurdle()
    H = _num(hurdle.get("value_pct"))
    if H is None or H <= -100.0:
        out.update(state=REVIEW_REQUIRED, reason="CURRENT_HURDLE_UNAVAILABLE",
                   why="current authorised hurdle unreadable: %s" % hurdle.get("why"))
        return out
    er = case["er"]
    E = float(er["value_pct"]) / 100.0
    h = float(er["horizon_months"]) / 12.0
    Pa = float(case["price_basis"]["price"])
    Hf = H / 100.0
    W = Pa * (1.0 + E) ** h
    Pmax = W / (1.0 + Hf) ** h
    impl = lambda p: (W / p) ** (1.0 / h) - 1.0                       # noqa: E731
    id1, id2 = impl(Pa) - E, impl(Pmax) - Hf
    reasons = []
    appr = case.get("required_return_ref") or {}
    cur_m = method_id if method_id is not None else current_method_id()
    cur_h = horizon_months if horizon_months is not None else current_horizon_months()
    if cur_h is not None and int(round(float(er["horizon_months"]))) != int(cur_h):
        reasons.append("HORIZON_CHANGED(%s->%s)" % (er["horizon_months"], cur_h))
    if er.get("method_id") and cur_m and er.get("method_id") != cur_m:
        reasons.append("ER_METHOD_CHANGED(%s->%s)" % (er.get("method_id"), cur_m))
    if not er.get("method_id") or not cur_m:
        reasons.append("ER_METHOD_IDENTITY_UNKNOWN")
    am, cm = appr.get("methodology_id"), hurdle.get("methodology_id")
    if not am or not cm:
        reasons.append("HURDLE_METHODOLOGY_IDENTITY_UNKNOWN")
    elif am != cm:
        reasons.append("HURDLE_METHODOLOGY_CHANGED")
    if newer_case_id and newer_case_id != case.get("case_id"):
        reasons.append("CASE_SUPERSEDED(%s)" % newer_case_id)
    if material_event and material_event.get("material"):
        reasons.append("MATERIAL_EVENT(%s)" % material_event.get("ref"))
    out.update({
        "approval": {"as_of": case.get("as_of"), "price": Pa,
                     "currency": case["price_basis"].get("currency"),
                     "er_pct": er["value_pct"], "horizon_months": er["horizon_months"],
                     "method_id": er.get("method_id"),
                     "hurdle_pct_at_approval": appr.get("value_pct"),
                     "hurdle_methodology_id": am},
        "current_hurdle": {"value_pct": H, "methodology_id": cm,
                           "derivation": hurdle.get("derivation"),
                           "policy_version": hurdle.get("policy_version"),
                           "as_of": hurdle.get("as_of") or as_of},
        "terminal_wealth_proxy": W, "pmax": Pmax, "pmax_over_approval_price": Pmax / Pa,
        "approved_er_below_current_hurdle": bool(E < Hf),
        "identity": {"implied_er_at_approval_minus_approved": id1,
                     "implied_er_at_pmax_minus_hurdle": id2},
        "calc_id": _calc_id(case.get("case_id"), hurdle, as_of),
    })
    if abs(id1) > IDENTITY_TOL or abs(id2) > IDENTITY_TOL:
        out.update(state=ERROR_IDENTITY, reason="IDENTITY_BROKEN",
                   why="an identity failed; the ceiling is not published (R4.3)")
        out.pop("pmax", None)
        return out
    if reasons:
        out.update(state=REVIEW_REQUIRED, reason=";".join(reasons),
                   why=("the frozen case no longer stands as approved; it must be reviewed or "
                        "re-underwritten - never recomputed at execution"))
    else:
        out.update(state=VALID, reason=None,
                   why=("frozen case valid; Pmax against the current authorised hurdle. NECESSARY "
                        "execution condition only - every other control still binds"))
    return out


def implied_er(result: dict, price: float) -> Optional[float]:
    """implied E[r] (percent) of the frozen case at `price`. None when no ceiling exists."""
    W = _num(result.get("terminal_wealth_proxy"))
    p = _num(price)
    h = _num(((result.get("approval") or {}).get("horizon_months")))
    if W is None or p is None or p <= 0 or not h:
        return None
    return 100.0 * ((W / p) ** (12.0 / h) - 1.0)


def execution_check(result: dict, price: float, *, price_currency: Optional[str] = None,
                    controls: Optional[Dict[str, str]] = None) -> dict:
    """The price condition AND the controls that still bind. Never returns BUY.

    -> {"verdict": BLOCKED_* | CEILING_SATISFIED_OTHER_CONTROLS_PASS, "sufficient_for_buy": False}"""
    controls = controls or {}
    blocked = []
    if result.get("state") != VALID:
        blocked.append("CASE_%s(%s)" % (result.get("state"), result.get("reason")))
    missing = [c for c in REQUIRED_CONTROLS if c not in controls]
    failed = [c for c in REQUIRED_CONTROLS if c in controls and controls[c] != PASS]
    if missing:
        blocked.append("CONTROL_UNKNOWN(%s)" % ",".join(missing))
    if failed:
        blocked.append("CONTROL_NOT_PASS(%s)" % ",".join("%s=%s" % (c, controls[c]) for c in failed))
    cur = (result.get("approval") or {}).get("currency")
    if price_currency is not None and cur is not None and price_currency != cur:
        blocked.append("CURRENCY_MISMATCH(%s vs %s)" % (price_currency, cur))
    pm = _num(result.get("pmax"))
    p = _num(price)
    price_ok = pm is not None and p is not None and p > 0 and p <= pm
    if pm is not None and p is not None and p > pm:
        blocked.append("PRICE_ABOVE_PMAX")
    verdict = ("BLOCKED" if blocked else "CEILING_SATISFIED_OTHER_CONTROLS_PASS")
    return {"verdict": verdict, "blocked_by": blocked, "price_condition_met": bool(price_ok),
            "implied_er_pct": implied_er(result, price), "sufficient_for_buy": False,
            "note": "price <= Pmax is necessary only; this check never authorises a BUY"}


def shadow_month(*, root: Optional[str] = None, month: Optional[str] = None,
                 as_of: Optional[str] = None, events: Optional[dict] = None,
                 cases: Optional[List[dict]] = None) -> dict:
    """SHADOW publication for the pre-run: one ceiling per case captured this month."""
    _fi_mark("execution_ceiling", "shadow_month")
    root = root or HERE
    as_of = as_of or datetime.date.today().isoformat()
    month = month or as_of[:7]
    if not _flag():
        return {"state": DISABLED, "why": "execution_ceiling_shadow flag off - UNKNOWN (R4.3)"}
    import underwriting as _uw
    allc = _uw.load(root)
    src = "store"
    if cases is not None:                    # this run's capture (a dry run writes nothing)
        src = "this_run_capture"
        have = {c.get("case_id") for c in allc}
        allc = allc + [c for c in cases if c.get("case_id") not in have]
    latest = {}
    for c in sorted(allc, key=lambda c: str(c.get("as_of") or "")):
        latest[c.get("ticker")] = c.get("case_id")
    cases = [c for c in (cases if cases is not None else allc)
             if str(c.get("as_of") or "")[:7] == month]
    hurdle = current_hurdle()
    rows, by_state = [], {}
    for c in cases:
        r = ceiling(c, hurdle=hurdle, newer_case_id=latest.get(c.get("ticker")),
                    material_event=(events or {}).get(c.get("ticker")), as_of=as_of)
        r["purpose"] = c.get("purpose")
        rows.append(r)
        by_state.setdefault(r["state"], []).append(r.get("ticker"))
    return {"state": "OK", "mode": "SHADOW", "label": LABEL, "as_of": as_of, "month": month,
            "case_source": src, "n_cases": len(cases), "by_state": {k: sorted(v) for k, v in by_state.items()},
            "current_hurdle": hurdle, "rows": rows,
            "consumers": "none - SHADOW (ISA-0740); no capital decision reads this",
            "basis": ("ISA-0740: W_T frozen on the case; Pmax against the current authorised "
                      "hurdle; identities asserted; necessary-only")}


# ─────────────────────────────────────────────────────────── selftest ─────────────

def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- %s" % detail))

    import isa_policy
    saved = isa_policy.V2_FLAGS.get("execution_ceiling_shadow")
    isa_policy.V2_FLAGS["execution_ceiling_shadow"] = True
    try:
        HM = {"value_pct": 15.7, "methodology_id": "HM-1", "derivation": {"x": 1},
              "policy_version": "P", "as_of": "2026-10-03"}
        base = {"case_id": "UWC-T", "ticker": "TST", "as_of": "2026-10-03",
                "admissible_for_positive_size": True,
                "price_basis": {"price": 100.0, "currency": "USD"},
                "er": {"state": "VALID_MECHANICAL", "value_pct": 25.0, "horizon_months": 12,
                       "method_id": "M1"},
                "required_return_ref": {"value_pct": 15.7, "methodology_id": "HM-1"}}
        kw = dict(hurdle=HM, method_id="M1", horizon_months=12)
        r = ceiling(base, **kw)
        ok("VALID case -> VALID", r["state"] == VALID, r)
        ok("MUST-FIRE identity 1: implied_Er(approval price) == approved E[r]",
           abs(implied_er(r, 100.0) - 25.0) < 1e-6, implied_er(r, 100.0))
        ok("MUST-FIRE identity 2: implied_Er(Pmax) == current hurdle",
           abs(implied_er(r, r["pmax"]) - 15.7) < 1e-6, implied_er(r, r["pmax"]))
        ok("Pmax = 100 x 1.25/1.157", abs(r["pmax"] - 100 * 1.25 / 1.157) < 1e-9, r["pmax"])
        # h != 12 months: both identities still hold
        r24 = ceiling(dict(base, er=dict(base["er"], horizon_months=24)), hurdle=HM,
                      method_id="M1", horizon_months=24)
        ok("h=24m identities", abs(implied_er(r24, 100.0) - 25.0) < 1e-6
           and abs(implied_er(r24, r24["pmax"]) - 15.7) < 1e-6)
        # routine numeric re-derivation (same methodology): recompute, NOT invalidate
        r2 = ceiling(base, hurdle=dict(HM, value_pct=16.4), method_id="M1", horizon_months=12)
        ok("MUST-FIRE: numeric hurdle re-derivation recomputes Pmax, case stays VALID",
           r2["state"] == VALID and abs(implied_er(r2, r2["pmax"]) - 16.4) < 1e-6
           and r2["pmax"] < r["pmax"] and r2["current_hurdle"]["value_pct"] == 16.4, r2)
        ok("calc id changes with the hurdle", r2["calc_id"] != r["calc_id"])
        # methodology change -> review
        r3 = ceiling(base, hurdle=dict(HM, methodology_id="HM-2"), method_id="M1", horizon_months=12)
        ok("MUST-FIRE: hurdle METHODOLOGY change -> REVIEW_REQUIRED",
           r3["state"] == REVIEW_REQUIRED and "HURDLE_METHODOLOGY_CHANGED" in r3["reason"], r3)
        ok("MUST-FIRE: horizon change -> REVIEW_REQUIRED",
           ceiling(base, hurdle=HM, method_id="M1", horizon_months=24)["state"] == REVIEW_REQUIRED)
        ok("MUST-FIRE: E[r] method change -> REVIEW_REQUIRED",
           ceiling(base, hurdle=HM, method_id="M2", horizon_months=12)["state"] == REVIEW_REQUIRED)
        ok("MUST-FIRE: superseded case -> REVIEW_REQUIRED",
           "CASE_SUPERSEDED" in (ceiling(base, newer_case_id="UWC-NEW", **kw).get("reason") or ""))
        ok("MUST-FIRE: material event -> REVIEW_REQUIRED",
           ceiling(base, material_event={"material": True, "ref": "EV1"}, **kw)["state"] == REVIEW_REQUIRED)
        ok("NEGATIVE CONTROL: same case id as latest -> not superseded",
           ceiling(base, newer_case_id="UWC-T", **kw)["state"] == VALID)
        ok("NEGATIVE CONTROL: non-material event -> VALID",
           ceiling(base, material_event={"material": False}, **kw)["state"] == VALID)
        # NO_AUTHORITY only for absent/invalid case - not for E < H
        lo = ceiling(dict(base, er=dict(base["er"], value_pct=10.0)), **kw)
        ok("NEGATIVE CONTROL: approved E[r] 10% < hurdle 15.7% is NOT NO_AUTHORITY",
           lo["state"] == VALID and lo["approved_er_below_current_hurdle"] and lo["pmax"] < 100.0, lo)
        ok("MUST-FIRE: legacy-parser (September) case -> NO_AUTHORITY",
           ceiling(dict(base, er=dict(base["er"], state="UNVERIFIED_LEGACY_PARSER"),
                        admissible_for_positive_size=False), **kw)["reason"] == "LEGACY_PARSER_CASE")
        ok("MUST-FIRE: missing case -> NO_AUTHORITY", ceiling(None, **kw)["state"] == NO_AUTHORITY)
        ok("MUST-FIRE: missing E[r] -> NO_AUTHORITY",
           ceiling(dict(base, er=dict(base["er"], value_pct=None)), **kw)["state"] == NO_AUTHORITY)
        ok("MUST-FIRE: inadmissible case -> NO_AUTHORITY",
           ceiling(dict(base, admissible_for_positive_size=False), **kw)["state"] == NO_AUTHORITY)
        ok("unknown hurdle methodology identity -> REVIEW_REQUIRED",
           ceiling(dict(base, required_return_ref={"value_pct": 15.7}), **kw)["state"] == REVIEW_REQUIRED)
        # necessary-only contract
        allpass = {c: PASS for c in REQUIRED_CONTROLS}
        x = execution_check(r, 105.0, price_currency="USD", controls=allpass)
        ok("valid case, price < Pmax, all controls PASS -> satisfied but NEVER a BUY",
           x["verdict"] == "CEILING_SATISFIED_OTHER_CONTROLS_PASS" and x["sufficient_for_buy"] is False)
        ok("MUST-FIRE: price above Pmax -> BLOCKED",
           "PRICE_ABOVE_PMAX" in execution_check(r, r["pmax"] * 1.01, controls=allpass)["blocked_by"])
        # the critical negative fixture: adverse/stale/invalidated case far below Pmax stays blocked
        adverse = ceiling(base, material_event={"material": True, "ref": "profit warning"}, **kw)
        xa = execution_check(adverse, 50.0, controls=allpass)
        ok("NEGATIVE FIXTURE: adverse-event case at half its Pmax is BLOCKED (price fall does not buy)",
           xa["verdict"] == "BLOCKED" and xa["price_condition_met"] is True
           and any("MATERIAL_EVENT" in b for b in xa["blocked_by"]), xa)
        xs = execution_check(r, 50.0, controls=dict(allpass, evidence_freshness="STALE"))
        ok("NEGATIVE FIXTURE: stale evidence at half Pmax is BLOCKED although price <= Pmax",
           xs["verdict"] == "BLOCKED" and xs["price_condition_met"] is True
           and any("evidence_freshness" in b for b in xs["blocked_by"]), xs)
        xi = execution_check(ceiling(dict(base, admissible_for_positive_size=False), **kw), 50.0,
                             controls=allpass)
        ok("NEGATIVE FIXTURE: invalidated (inadmissible) case at a low price is BLOCKED",
           xi["verdict"] == "BLOCKED")
        ok("MUST-FIRE: a control not supplied is UNKNOWN and BLOCKS",
           execution_check(r, 90.0, controls={})["verdict"] == "BLOCKED")
        ok("currency mismatch BLOCKS",
           execution_check(r, 90.0, price_currency="GBp", controls=allpass)["verdict"] == "BLOCKED")
        isa_policy.V2_FLAGS["execution_ceiling_shadow"] = False
        ok("ROLLBACK: flag off -> DISABLED, no Pmax", ceiling(base, **kw)["state"] == DISABLED
           and "pmax" not in ceiling(base, **kw))
    finally:
        isa_policy.V2_FLAGS["execution_ceiling_shadow"] = saved if saved is not None else True
    print("execution_ceiling selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(shadow_month(), indent=1, default=str)[:4000])
