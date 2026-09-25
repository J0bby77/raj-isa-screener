#!/usr/bin/env python3
"""
capital_authorisation.py — ISA-0705 / ISA-0706 / ISA-0708: ONE final capital-authorisation
receipt, consumed by the report and the execution path.

Authority: ChatGPT Astra 6 Audit/ISA_Wave2_Consolidated_BuildSpec_19Sep2026.md §4, §5.2-5.5,
§7.4, §10, §12, §13, §14; ISA_Engineering_Rules.md R4.4, R4.5, R5.1, R20.1, R20.2.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
Wave 2 addresses one systemic weakness: **a route-native fact, eligibility result, size, risk
result or intermediate disposition can exist without becoming the unique, current,
decision-effective capital authority consumed by the final report and execution path.**

Three measured manifestations, all on the live book:

  ISA-0705  a candidate the correlation gate classified REPLACEMENT_ONLY was dropped from the
            sequencer's order and then FUNDED IN FULL as an addition — GBP 6,794.48 on the
            aug-2026 fixture book.
  ISA-0706  two computations of the held-binary L1 budget disagreed: 0.305593%/1.194407% from
            the declared home against a WITHHELD budget from a second copy that assembled its
            own rows, which made every held VCI size UNEVALUATED.
  ISA-0708  two implementations of each name's sleeve-risk share — one deciding a capital
            REFUSAL (the D27 ceiling), the other the REVIEW FLAG — measured side by side for
            the first time on 20-Sep-2026 and differing by up to 3.0pp (AVGO 28.24% vs 25.25%).

⚑⚑ THIS RECEIPT OWNS INTEGRATION AND PERMISSION. IT CONSUMES, AND NEVER RECOMPUTES,
MODULE-OWNED ECONOMICS AND RISK (§5.2). It is the place where "the sequencer said
REPLACEMENT_ONLY" and "the router funded it" can no longer be two facts that never meet.

⚑ A BLOCKED NEW-CAPITAL DECISION HAS NO FABRICATED 0% LIQUIDATION TARGET (§5.2). Refusing to
add is not deciding to sell, and the two must not share a field.

⚑ REPORTS AND EMAILS EXPLAIN; THEY NEVER INDEPENDENTLY DECIDE (§4 / R20.2).

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["capital_authorisation"] = False` — `authorise()`
returns state DISABLED, which reads as UNKNOWN and never as AUTHORISED.
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

# ── §10 canonical capital states ────────────────────────────────────────────────────────
AUTHORISED = "AUTHORISED"
BLOCKED_UNMEASURED = "BLOCKED_UNMEASURED"
REJECTED_MEASURED = "REJECTED_MEASURED"
NO_ADD = "NO_ADD"
EXIT_REVIEW_REQUIRED = "EXIT_REVIEW_REQUIRED"
SUPERSEDED = "SUPERSEDED"
CAPITAL_STATES = (AUTHORISED, BLOCKED_UNMEASURED, REJECTED_MEASURED, NO_ADD,
                  EXIT_REVIEW_REQUIRED, SUPERSEDED)

# Routes a receipt may carry. REPLACEMENT_ONLY is a ROUTE, not a footnote: it must survive
# from eligibility to final authorisation or ISA-0705 recurs (§5.3).
ROUTE_ADDITION = "ADDITION"
ROUTE_REPLACEMENT_ONLY = "REPLACEMENT_ONLY"
ROUTE_HELD_TOPUP = "HELD_TOPUP"
ROUTE_VCI = "VCI"
ROUTES = (ROUTE_ADDITION, ROUTE_REPLACEMENT_ONLY, ROUTE_HELD_TOPUP, ROUTE_VCI)

# The §5.2 field set. Declared as data so a consumer can assert the shape rather than trust it.
RECEIPT_FIELDS = (
    "ledger_decision_id", "underwriting_case_id",
    "eligibility_status", "sizing_status", "holding_review_status", "risk_status",
    "funding_status", "permission_status", "capital_status", "route", "target_weight",
    "target_gbp", "current_exposure_gbp", "incremental_gbp", "binding_constraints",
    "reason_codes", "input_snapshot_id", "event_review_id", "decision_id",
    "supersedes_decision_id", "build_id", "approval_reference", "execution_status",
)


class AuthorisationRefused(RuntimeError):
    """Raised when the receipt cannot be formed without inventing a fact."""


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.V2_FLAGS.get("capital_authorisation", True))
    except Exception:                                                   # noqa: BLE001
        return True


def _today() -> str:
    return datetime.date.today().isoformat()


# ═════════════════════════════════════════════════════════════════════════════════════════
# ISA-0727 (M14, 23-Sep-2026) — APPROVAL IS BOUND TO THE STATE IT WAS FORMED ON
# ═════════════════════════════════════════════════════════════════════════════════════════
# ⚑ One home for "what material state did this plan decide on". The router binds it into every
#   receipt; the report re-derives it with the SAME function before rendering the plan as current.
#   Materiality is IDENTITY-level: a component either is the one the plan used or it is not. No
#   tolerance is invented here - the execution-price envelope and which holdings/cash changes
#   between approval and EXECUTION are material are Raj decisions not yet taken (ISA-0727).
APPROVAL_COMPONENTS = ("build_id", "config_roll", "portfolio_snapshot", "opportunity_set_id",
                       "underwriting_store", "broker_inputs", "capital_amount_gbp")


def _sha_file(path) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except (OSError, TypeError):
        return None


def input_state(root: Optional[str] = None, *, portfolio_path=None, step9: Optional[dict] = None,
                amount_gbp=None) -> dict:
    """-> {input_snapshot_id, components{...}}. An unreadable component is the literal string
    'ABSENT', so its later appearance is a CHANGE, never silently equal."""
    root = root or HERE
    comp = {}
    try:
        with open(os.path.join(root, "Dashboard", "state", "trusted_build.json"), encoding="utf-8") as fh:
            comp["build_id"] = json.load(fh).get("build_id") or "ABSENT"
    except (OSError, ValueError):
        comp["build_id"] = "ABSENT"
    try:
        import release_gate as _rg
        comp["config_roll"] = (_rg.config_fingerprint(root).get("roll") or "ABSENT")[:16]
    except Exception:                                                   # noqa: BLE001
        comp["config_roll"] = "ABSENT"
    _pp = str(portfolio_path) if portfolio_path else None
    comp["portfolio_snapshot"] = ("%s:%s" % (os.path.basename(_pp), _sha_file(_pp))
                                  if _pp and _sha_file(_pp) else "ABSENT")
    comp["opportunity_set_id"] = (((step9 or {}).get("opportunity_set") or {})
                                  .get("opportunity_set_id") or "ABSENT")
    comp["underwriting_store"] = _sha_file(os.path.join(root, "underwriting_cases.jsonl")) or "ABSENT"
    comp["broker_inputs"] = "%s:%s" % (_sha_file(os.path.join(root, "stock_symbol_map.json")) or "ABSENT",
                                       _sha_file(os.path.join(root, "broker_venues.json")) or "ABSENT")
    comp["capital_amount_gbp"] = (round(float(amount_gbp), 2) if amount_gbp is not None else "ABSENT")
    canon = json.dumps({k: comp[k] for k in APPROVAL_COMPONENTS}, sort_keys=True, default=str)
    return {"input_snapshot_id": "ISN-" + hashlib.sha256(canon.encode()).hexdigest()[:16],
            "components": comp, "materiality": "identity-level (no tolerance declared)"}


def approval_reference(receipt: dict) -> str:
    """The per-name approval identity: the state it was formed on + what it authorises + the
    identities it consumed. Two receipts with the same reference authorise the same thing."""
    cc = receipt.get("consumed_calculations") or {}
    canon = json.dumps([receipt.get("input_snapshot_id"), receipt.get("build_id"), receipt.get("ticker"),
                        receipt.get("route"), receipt.get("target_gbp"), receipt.get("incremental_gbp"),
                        receipt.get("underwriting_case_id"), receipt.get("event_review_id"),
                        receipt.get("broker_dealability"), cc.get("risk_share_calc_id"),
                        cc.get("vci_budget_calc_id"), receipt.get("capital_status")],
                       sort_keys=True, default=str)
    return "APR-" + hashlib.sha256(canon.encode()).hexdigest()[:16]


def approval_validity(binding: Optional[dict], current: dict) -> dict:
    """-> {state: VALID | STALE | UNBOUND, changed: [component, ...]}. STALE names every
    component whose identity differs; UNBOUND = the plan carries no binding (pre-control)."""
    if not binding or not binding.get("components"):
        return {"state": "UNBOUND", "changed": [],
                "why": "the plan carries no approval binding (it pre-dates ISA-0727)"}
    b, c = binding["components"], (current or {}).get("components") or {}
    changed = [k for k in APPROVAL_COMPONENTS if str(b.get(k)) != str(c.get(k))]
    return {"state": "STALE" if changed else "VALID", "changed": changed,
            "bound_input_snapshot_id": binding.get("input_snapshot_id"),
            "current_input_snapshot_id": (current or {}).get("input_snapshot_id"),
            "why": ("the plan was formed on a different %s - re-run the router (Step 6.10) before "
                    "rendering or acting on it" % ", ".join(changed)) if changed else
                   "every bound component is the one the plan used"}


def decision_identity(*, as_of, ticker, route, input_snapshot_id=None) -> str:
    basis = "|".join([str(as_of), str(ticker).upper(), str(route),
                      str(input_snapshot_id or "")])
    return "CAP-%s-%s-%s-%s" % (as_of, str(route).upper(), str(ticker).upper(),
                                hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12])


def authorise(*, ticker, route, as_of=None, build_id=None,
              eligibility, sizing, risk, funding, permission,
              current_exposure_gbp=0.0, input_snapshot_id=None, event_review_id=None,
              supersedes_decision_id=None, approval_reference=None,
              execution_status="NOT_EXECUTED", holding_review=None,
              membership=None, ledger_decision_id=None, underwriting=None) -> dict:
    """The single final authorisation for ONE name. Integration and permission only.

    Every argument is a RESULT from the module that owns it, and each is expected to carry the
    identity of the calculation it came from:
      `sizing`  {status, target_weight, target_gbp, budget_calc_id?, binding_constraint?}
      `risk`    {status, risk_share_calc_id?, binding?}
      `funding` {status, donor?, released_gbp?, donor_state?}
    Nothing here recomputes a size, a budget or a risk share (§5.2).
    """
    _fi_mark("capital_authorisation", "authorise")
    if not _flag():
        return {"state": "DISABLED", "capital_status": BLOCKED_UNMEASURED,
                "why": ("isa_policy.V2_FLAGS['capital_authorisation'] is False. DISABLED reads "
                        "as UNKNOWN and never as AUTHORISED (R4.3).")}
    if route not in ROUTES:
        raise AuthorisationRefused(
            "unknown route %r. The routes are DECLARED (%s); a route invented at the final "
            "boundary is the seam ISA-0705 lives in (R4.8)." % (route, ", ".join(ROUTES)))
    as_of = as_of or _today()
    reasons, binding = [], []

    # ── the four module verdicts, consumed ────────────────────────────────────────────
    elig_ok = str((eligibility or {}).get("status", "")).upper() in ("ELIGIBLE", "OK", "PASS")
    size_ok = str((sizing or {}).get("status", "")).upper() in ("SIZED", "OK")
    risk_ok = str((risk or {}).get("status", "")).upper() in ("WITHIN", "OK", "PASS")
    fund_ok = str((funding or {}).get("status", "")).upper() in ("FUNDED", "OK", "AVAILABLE")
    perm_ok = str((permission or {}).get("status", "")).upper() in ("GRANTED", "OK", "APPROVED")

    target_gbp = float((sizing or {}).get("target_gbp") or 0.0)
    current = float(current_exposure_gbp or 0.0)

    # ── ISA-0705 — the replacement-only invariant, enforced HERE as well as at sizing ──
    #    §5.3: the route must SURVIVE to final authorisation, and it is the receipt that
    #    proves it did. Two gates are better than one for an invariant whose failure mode is
    #    "the verdict existed and nothing consumed it".
    donor = (funding or {}).get("donor")
    donor_state = str((funding or {}).get("donor_state") or "").upper()
    released = float((funding or {}).get("released_gbp") or 0.0)
    if route == ROUTE_REPLACEMENT_ONLY:
        if not donor or donor_state not in ("REALISED", "AUTHORISED"):
            reasons.append("REPLACEMENT_ONLY_NO_ADMISSIBLE_DONOR")
            binding.append("ISA-0705: replacement-only means NO net incremental capital. With "
                           "no REALISED or AUTHORISED donor release this buy is not "
                           "authorised — it does not become a standalone addition.")
            fund_ok = False
        elif target_gbp - current > released + 0.005:
            reasons.append("REPLACEMENT_EXCEEDS_DONOR_RELEASE")
            binding.append("ISA-0705: the proposed buy (GBP %.2f) exceeds the donor release "
                           "(GBP %.2f from %s). The excess would be net new exposure."
                           % (target_gbp - current, released, donor))
            fund_ok = False

    # ── ISA-0706 — a VCI/held size must name ITS budget calculation ───────────────────
    if route in (ROUTE_VCI, ROUTE_HELD_TOPUP) and size_ok:
        if not (sizing or {}).get("budget_calc_id"):
            reasons.append("VCI_SIZE_WITHOUT_BUDGET_CALC_ID")
            binding.append("ISA-0706: this size names no VCI budget calc_id, so it cannot be "
                           "shown to come from the single authoritative budget. A parallel "
                           "budget calculation may not determine capital.")
            size_ok = False

    # ── ISA-0685 (20-Sep-2026) — HELD IS NOT ADMITTED ─────────────────────────────────
    #    A holding with no recorded admission may be sized, risk-counted and reported, but it
    #    may not hold NEW-CAPITAL PRIORITY — so it cannot be AUTHORISED for an addition on the
    #    strength of being owned. It is not refused as a position; it is refused as a claim.
    mem_state = (membership or {}).get("state")
    if (target_gbp - current) > 0.005 and membership is not None:
        if not (membership or {}).get("may_hold_new_capital_priority"):
            reasons.append("NOT_ADMITTED_%s" % (mem_state or "UNKNOWN"))
            binding.append("ISA-0685: %s — %s" % (mem_state,
                                                  (membership or {}).get("why", "no verdict")))
            elig_ok = False

    # ── ISA-0721/0722 — an ADDITION needs an admissible underwriting case ─────────────
    #    Only enforced when the caller supplies the case lookup (`underwriting` not None), so an
    #    un-updated caller is not silently refused (R4.7 is honoured by the explicit argument).
    if (target_gbp - current) > 0.005 and underwriting is not None:
        if not (underwriting or {}).get("admissible_for_positive_size"):
            reasons.append("NO_ADMISSIBLE_UNDERWRITING_CASE")
            binding.append("ISA-0721: no admissible underwriting case (%s) - positive new size "
                           "cannot stand on a missing or invalid expected return"
                           % (((underwriting or {}).get("er") or {}).get("state") or "ABSENT"))
            elig_ok = False

    # ── ISA-0708 — a risk verdict must name ITS risk-share calculation ────────────────
    if risk_ok and not (risk or {}).get("risk_share_calc_id"):
        reasons.append("RISK_VERDICT_WITHOUT_RISK_SHARE_CALC_ID")
        binding.append("ISA-0708: this risk verdict names no risk_share_calc_id, so the D27 "
                       "ceiling and the review flag cannot be proved to be reading one "
                       "calculation.")
        risk_ok = False

    for label, okv, blob in (("ELIGIBILITY", elig_ok, eligibility), ("SIZING", size_ok, sizing),
                             ("RISK", risk_ok, risk), ("FUNDING", fund_ok, funding),
                             ("PERMISSION", perm_ok, permission)):
        if not okv:
            reasons.append("%s_%s" % (label, str((blob or {}).get("status", "UNKNOWN")).upper()))
            w = (blob or {}).get("why") or (blob or {}).get("binding")
            if w:
                binding.append("%s: %s" % (label, w))

    incremental = round(max(target_gbp - current, 0.0), 2)
    if route == ROUTE_REPLACEMENT_ONLY:
        # §5.3 — by construction, and stated in the receipt rather than left to be inferred.
        net_incremental = 0.0
    else:
        net_incremental = incremental

    if elig_ok and size_ok and risk_ok and fund_ok and perm_ok:
        capital_status = AUTHORISED if incremental > 0 else NO_ADD
    elif any(str((b or {}).get("status", "")).upper() in ("UNKNOWN", "UNMEASURED", "UNEVALUATED",
                                                          "UNRESOLVED_DATA_FAILURE")
             for b in (eligibility, sizing, risk, funding, permission)):
        capital_status = BLOCKED_UNMEASURED
    else:
        capital_status = REJECTED_MEASURED

    receipt = {
        "receipt_kind": "CAPITAL_AUTHORISATION_RECEIPT",
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of, "ticker": str(ticker).upper(),
        "eligibility_status": (eligibility or {}).get("status"),
        "admission_status": mem_state,
        "sizing_status": (sizing or {}).get("status"),
        "holding_review_status": (holding_review or {}).get("status"),
        "risk_status": (risk or {}).get("status"),
        "funding_status": (funding or {}).get("status"),
        "permission_status": (permission or {}).get("status"),
        "capital_status": capital_status,
        "route": route,
        "target_weight": (sizing or {}).get("target_weight"),
        "target_gbp": round(target_gbp, 2),
        "current_exposure_gbp": round(current, 2),
        "incremental_gbp": net_incremental,
        "gross_incremental_gbp": incremental,
        "binding_constraints": binding,
        "reason_codes": reasons,
        "input_snapshot_id": input_snapshot_id,
        "event_review_id": event_review_id,
        "decision_id": decision_identity(as_of=as_of, ticker=ticker, route=route,
                                         input_snapshot_id=input_snapshot_id),
        "supersedes_decision_id": supersedes_decision_id,
        "build_id": build_id,
        "approval_reference": approval_reference,
        "execution_status": execution_status,
        # ⚑ ISA-0717 (23-Sep-2026): the CANONICAL ledger decision in force for this name when the
        #   receipt was formed. `decision_id` names the authorisation; this names the decision it
        #   sits on, so the report, the approval and execution reconciliation join ONE lineage
        #   rather than each naming its own (R4.4). None = no current decision exists.
        "ledger_decision_id": ledger_decision_id,
        # ⚑ ISA-0722 (23-Sep-2026): the underwriting CASE this receipt sits on (consumed, never
        #   recomputed). An inadmissible or absent case cannot support an ADDITION: the receipt
        #   says so as a reason code rather than letting a size stand on no expected return.
        # ⚑ ISA-0607 (23-Sep-2026): the broker verdict the candidate list applied (consumed).
        "broker_dealability": (((eligibility or {}).get("broker_dealability") or {}).get("state")
                               if isinstance((eligibility or {}).get("broker_dealability"), dict)
                               else (eligibility or {}).get("broker_dealability")),
        "underwriting_case_id": (underwriting or {}).get("case_id"),
        "underwriting_er_state": ((underwriting or {}).get("er") or {}).get("state"),
        # ⚑ the calculation identities this receipt CONSUMED — not recomputed (§5.2)
        "consumed_calculations": {
            "vci_budget_calc_id": (sizing or {}).get("budget_calc_id"),
            "risk_share_calc_id": (risk or {}).get("risk_share_calc_id"),
            "sequencer_verdict": (eligibility or {}).get("sequencer_verdict"),
            "donor": donor, "donor_state": donor_state or None,
            "donor_released_gbp": released or None,
            "membership_state": mem_state,
            "membership_why": (membership or {}).get("why"),
        },
        # §5.2 — a blocked new-capital decision has NO fabricated liquidation target.
        "liquidation_target_pct": None,
        "liquidation_note": ("refusing to ADD is not deciding to SELL. A blocked new-capital "
                             "decision carries no 0% target, because a 0% target is an exit "
                             "instruction and nobody issued one (§5.2)."),
    }
    return receipt


REQUIRED_IDENTITIES = ("risk_share_calc_id", "event_review_id")
REQUIRED_IDENTITIES_ROUTE = {ROUTE_VCI: ("vci_budget_calc_id",),
                             ROUTE_HELD_TOPUP: ("vci_budget_calc_id",)}


def operational_readiness(receipts) -> dict:
    """§19 — may this receipt boundary leave SHADOW?

    ⚑ IT LEAVES SHADOW ONLY WHEN EVERY REQUIRED IDENTITY IS ACTUALLY PRESENT ON EVERY RECEIPT,
    not when the wiring exists. Wiring an identity in and never producing it is precisely the
    'code exists therefore it is live' error (R4.14), and `event_review_id` is the honest test:
    it is produced by ISA-0713's issuer reviews, which no real run has yet written.

    Returns the per-identity coverage so the answer is a MEASUREMENT, not a judgement.
    """
    rows = list(receipts or [])
    missing = {}
    for r in rows:
        cc = r.get("consumed_calculations") or {}
        need = list(REQUIRED_IDENTITIES) + list(REQUIRED_IDENTITIES_ROUTE.get(r.get("route"), ()))
        for k in need:
            if not cc.get(k) and not r.get(k):
                missing.setdefault(k, []).append(r.get("ticker"))
    ready = bool(rows) and not missing
    return {"ready_to_leave_shadow": ready, "n_receipts": len(rows),
            "missing_identities": {k: sorted(set(v)) for k, v in missing.items()},
            "required": list(REQUIRED_IDENTITIES),
            "required_by_route": {k: list(v) for k, v in REQUIRED_IDENTITIES_ROUTE.items()},
            "why": ("every required identity is present on every receipt; the boundary may be "
                    "promoted out of SHADOW by a build that also proves its consumers"
                    if ready else
                    ("no receipts to assess" if not rows else
                     "SHADOW: %s" % "; ".join(
                         "%s absent on %s" % (k, ", ".join(sorted(set(v))))
                         for k, v in missing.items())))}


def check_invariants(receipts) -> dict:
    """R5.1 — the Wave 2 §14 acceptance invariants, over a set of receipts."""
    rows = list(receipts or [])
    breaches = []
    for r in rows:
        if r.get("route") == ROUTE_REPLACEMENT_ONLY and float(r.get("incremental_gbp") or 0) > 0:
            breaches.append({"ticker": r.get("ticker"), "invariant": "ISA-0705",
                             "why": ("a REPLACEMENT_ONLY receipt carries net incremental "
                                     "capital of GBP %.2f — replacement-only means no net "
                                     "new exposure" % float(r["incremental_gbp"]))})
        if (r.get("capital_status") == AUTHORISED
                and r.get("route") in (ROUTE_VCI, ROUTE_HELD_TOPUP)
                and not (r.get("consumed_calculations") or {}).get("vci_budget_calc_id")):
            breaches.append({"ticker": r.get("ticker"), "invariant": "ISA-0706",
                             "why": "AUTHORISED VCI/held capital with no VCI budget calc_id"})
        if (r.get("capital_status") == AUTHORISED
                and not (r.get("consumed_calculations") or {}).get("risk_share_calc_id")):
            breaches.append({"ticker": r.get("ticker"), "invariant": "ISA-0708",
                             "why": "AUTHORISED capital with no risk_share_calc_id"})
        if (float(r.get("incremental_gbp") or 0) > 0 and r.get("broker_dealability") is not None
                and r.get("broker_dealability") != "DEALABLE_ONLINE"):
            breaches.append({"ticker": r.get("ticker"), "invariant": "ISA-0607",
                             "why": ("net new capital GBP %.2f on a venue the broker cannot deal "
                                     "ONLINE (broker_dealability=%s)"
                                     % (float(r["incremental_gbp"]), r.get("broker_dealability")))})
        if r.get("capital_status") != AUTHORISED and r.get("liquidation_target_pct") is not None:
            breaches.append({"ticker": r.get("ticker"), "invariant": "§5.2",
                             "why": "a blocked decision carries a fabricated liquidation target"})
    ids = [r.get("risk_share_calc_id") or (r.get("consumed_calculations") or {}).get("risk_share_calc_id")
           for r in rows]
    ids = sorted({i for i in ids if i})
    if len(ids) > 1:
        breaches.append({"ticker": None, "invariant": "ISA-0708",
                         "why": ("%d distinct risk_share_calc_ids across one run's receipts "
                                 "(%s) — one run, one risk-share calculation"
                                 % (len(ids), ", ".join(ids)))})
    _bd_unver = sorted(str(r.get("ticker")) for r in rows
                       if float(r.get("incremental_gbp") or 0) > 0 and r.get("broker_dealability") is None)
    return {"ok": not breaches, "n_receipts": len(rows), "breaches": breaches,
            "broker_dealability_not_carried": _bd_unver,
            "risk_share_calc_ids": ids,
            "why": ("every Wave 2 capital invariant holds over %d receipt(s)" % len(rows)
                    if not breaches else
                    "%d invariant breach(es): %s"
                    % (len(breaches), "; ".join(b["invariant"] for b in breaches)))}


# ═════════════════════════════════════════════════════════════════════════════════════════
# SELFTEST — the §14 acceptance list is the test list
# ═════════════════════════════════════════════════════════════════════════════════════════

def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:220]) if not cond else ""))
        if not cond:
            fails.append(name)

    E = {"status": "ELIGIBLE"}
    S = {"status": "SIZED", "target_gbp": 6794.48, "target_weight": 1.0}
    R = {"status": "WITHIN", "risk_share_calc_id": "RSHR-8d9e445ab212"}
    F = {"status": "FUNDED"}
    P = {"status": "GRANTED"}

    r = authorise(ticker="D", route=ROUTE_ADDITION, as_of="2026-09-20", build_id="TB-TEST",
                  eligibility=E, sizing=S, risk=R, funding=F, permission=P)
    ok("the receipt carries every §5.2 field, as DATA a consumer can assert on",
       all(f in r for f in RECEIPT_FIELDS), sorted(set(RECEIPT_FIELDS) - set(r)))
    ok("a fully-cleared addition is AUTHORISED with its incremental figure",
       r["capital_status"] == AUTHORISED and r["incremental_gbp"] == 6794.48, r)

    # ── ISA-0705 ──────────────────────────────────────────────────────────────────────
    r705 = authorise(ticker="C", route=ROUTE_REPLACEMENT_ONLY, as_of="2026-09-20",
                     eligibility=E, sizing=S, risk=R, funding=F, permission=P)
    ok("⚑ MUST-FIRE ISA-0705: a REPLACEMENT_ONLY receipt with NO donor is not authorised, and "
       "its net incremental capital is ZERO — the route survives to final authorisation",
       r705["capital_status"] != AUTHORISED and r705["incremental_gbp"] == 0.0
       and "REPLACEMENT_ONLY_NO_ADMISSIBLE_DONOR" in r705["reason_codes"], r705)
    r705b = authorise(ticker="C", route=ROUTE_REPLACEMENT_ONLY, as_of="2026-09-20",
                      eligibility=E, sizing=S, risk=R, permission=P,
                      funding={"status": "FUNDED", "donor": "H", "donor_state": "PROPOSED",
                               "released_gbp": 7000.0})
    ok("⚑ MUST-FIRE ISA-0705: a PROPOSED donor leg cannot pay for a replacement buy",
       r705b["capital_status"] != AUTHORISED
       and "REPLACEMENT_ONLY_NO_ADMISSIBLE_DONOR" in r705b["reason_codes"], r705b)
    r705c = authorise(ticker="C", route=ROUTE_REPLACEMENT_ONLY, as_of="2026-09-20",
                      eligibility=E, sizing=S, risk=R, permission=P,
                      funding={"status": "FUNDED", "donor": "H", "donor_state": "REALISED",
                               "released_gbp": 3000.0})
    ok("⚑ MUST-FIRE ISA-0705: a buy LARGER than its donor release is refused — the excess "
       "would be net new exposure, which is exactly what replacement-only forbids",
       "REPLACEMENT_EXCEEDS_DONOR_RELEASE" in r705c["reason_codes"], r705c)
    r705d = authorise(ticker="C", route=ROUTE_REPLACEMENT_ONLY, as_of="2026-09-20",
                      eligibility=E, sizing=dict(S, target_gbp=3000.0), risk=R, permission=P,
                      funding={"status": "FUNDED", "donor": "H", "donor_state": "REALISED",
                               "released_gbp": 3000.0})
    ok("POSITIVE CONTROL ISA-0705: a paired REALISED donor covering the buy IS authorised, at "
       "ZERO net incremental exposure — a control that can only refuse is not a control",
       r705d["capital_status"] == AUTHORISED and r705d["incremental_gbp"] == 0.0
       and r705d["consumed_calculations"]["donor"] == "H", r705d)

    # ── ISA-0706 ──────────────────────────────────────────────────────────────────────
    r706 = authorise(ticker="ABCL", route=ROUTE_VCI, as_of="2026-09-20",
                     eligibility=E, sizing=S, risk=R, funding=F, permission=P)
    ok("⚑ MUST-FIRE ISA-0706: a VCI size naming NO budget calc_id cannot authorise capital — "
       "a parallel budget calculation may not decide",
       r706["capital_status"] != AUTHORISED
       and "VCI_SIZE_WITHOUT_BUDGET_CALC_ID" in r706["reason_codes"], r706)
    r706b = authorise(ticker="ABCL", route=ROUTE_VCI, as_of="2026-09-20", eligibility=E,
                      sizing=dict(S, budget_calc_id="VCIB-6b150bc3e615"), risk=R,
                      funding=F, permission=P)
    ok("POSITIVE CONTROL ISA-0706: the same size WITH the authoritative budget id authorises, "
       "and the id is recorded on the receipt",
       r706b["capital_status"] == AUTHORISED
       and r706b["consumed_calculations"]["vci_budget_calc_id"] == "VCIB-6b150bc3e615")

    # ── ISA-0708 ──────────────────────────────────────────────────────────────────────
    r708 = authorise(ticker="MU", route=ROUTE_ADDITION, as_of="2026-09-20", eligibility=E,
                     sizing=S, risk={"status": "WITHIN"}, funding=F, permission=P)
    ok("⚑ MUST-FIRE ISA-0708: a risk verdict naming NO risk_share_calc_id cannot authorise — "
       "the ceiling and the review flag must be provably on one calculation",
       r708["capital_status"] != AUTHORISED
       and "RISK_VERDICT_WITHOUT_RISK_SHARE_CALC_ID" in r708["reason_codes"], r708)

    # ── §5.2 — no fabricated liquidation target, and no route invented at the boundary ──
    # ⚑ Checked over the DECISION fields, not the explanatory note — the note says the word
    #   "SELL" precisely in order to say the receipt does not issue one, and a scan that
    #   cannot tell an instruction from its explanation is self-reference, not evidence.
    _decision_values = json.dumps({k: r705.get(k) for k in RECEIPT_FIELDS})
    ok("⚑ a BLOCKED decision carries NO liquidation target and no sell instruction: refusing "
       "to ADD is not deciding to SELL, and the two must not share a field",
       r705["liquidation_target_pct"] is None
       and not any(w in _decision_values.upper() for w in ("SELL", "LIQUIDAT", "EXIT")),
       _decision_values[:300])
    raised = False
    try:
        authorise(ticker="X", route="SOMETHING_NEW", eligibility=E, sizing=S, risk=R,
                  funding=F, permission=P)
    except AuthorisationRefused:
        raised = True
    ok("NEGATIVE CONTROL: a route invented at the final boundary is REFUSED — that seam is "
       "where ISA-0705 lived", raised)

    # ── the invariant checker ─────────────────────────────────────────────────────────
    inv = check_invariants([r, r705d, r706b])
    ok("POSITIVE CONTROL: a clean receipt set satisfies every Wave 2 invariant",
       inv["ok"] and inv["n_receipts"] == 3, inv)
    bad = dict(r705, incremental_gbp=6794.48)
    ok("⚑ MUST-FIRE: a REPLACEMENT_ONLY receipt carrying net incremental capital is a named "
       "invariant breach — this is the ISA-0705 defect, caught at the receipt level",
       not check_invariants([bad])["ok"]
       and check_invariants([bad])["breaches"][0]["invariant"] == "ISA-0705")
    two = check_invariants([r, dict(r706b, consumed_calculations=dict(
        r706b["consumed_calculations"], risk_share_calc_id="RSHR-other0000"))])
    ok("⚑ MUST-FIRE ISA-0708: two distinct risk_share_calc_ids in ONE run's receipts is a "
       "breach — one run, one risk-share calculation",
       not two["ok"] and any(b["invariant"] == "ISA-0708" for b in two["breaches"]), two)
    ok("NEGATIVE CONTROL: an AUTHORISED receipt with no risk_share_calc_id is caught by the "
       "invariant checker as well as by authorise() — two gates, because the failure mode is "
       "'the verdict existed and nothing consumed it'",
       not check_invariants([dict(r, consumed_calculations={"risk_share_calc_id": None})])["ok"])

    # ── ISA-0607 at the receipt: the broker verdict is carried and policed ────────────
    _wse = dict(r, broker_dealability="NOT_DEALABLE_ONLINE", incremental_gbp=6578.53)
    ok("MUST-FIRE ISA-0607: net new capital on a NOT_DEALABLE_ONLINE venue is an invariant breach",
       any(b["invariant"] == "ISA-0607" for b in check_invariants([_wse])["breaches"]))
    _onl = dict(r, broker_dealability="DEALABLE_ONLINE")
    ok("NEGATIVE CONTROL ISA-0607: the same receipt on a DEALABLE_ONLINE venue must not breach on 0607",
       not any(b["invariant"] == "ISA-0607" for b in check_invariants([_onl])["breaches"]))
    _rbd = authorise(ticker="HALO", route=ROUTE_ADDITION, as_of="2026-09-23",
                     eligibility=dict(E, broker_dealability={"state": "DEALABLE_ONLINE", "venue": "NMS"}),
                     sizing=S, risk=R, funding=F, permission=P)
    ok("the receipt carries the candidate list's broker verdict (consumed, not recomputed)",
       _rbd.get("broker_dealability") == "DEALABLE_ONLINE", _rbd.get("broker_dealability"))

    # ── ISA-0727 (M14): the approval is bound to its state; every component can make it STALE ──
    import tempfile as _tf, shutil as _sh
    _td = _tf.mkdtemp(prefix="ca_m14_")
    os.makedirs(os.path.join(_td, "Dashboard", "state"))
    def _w(rel, txt):
        with open(os.path.join(_td, rel), "w", encoding="utf-8") as fh:
            fh.write(txt)
    _w("Dashboard/state/trusted_build.json", json.dumps({"build_id": "TB-A"}))
    _w("pf.json", "{}"); _w("stock_symbol_map.json", "{}"); _w("broker_venues.json", "{}")
    _w("underwriting_cases.jsonl", "")
    _s9 = {"opportunity_set": {"opportunity_set_id": "OPS-1"}}
    _st = lambda **k: input_state(_td, portfolio_path=k.get("pp", os.path.join(_td, "pf.json")),
                                  step9=k.get("s9", _s9), amount_gbp=k.get("amt", 1000.0))
    _b = _st()
    ok("NEGATIVE CONTROL ISA-0727: unchanged inputs keep the approval VALID",
       approval_validity(_b, _st())["state"] == "VALID")
    _muts = {"opportunity_set_id": lambda: _st(s9={"opportunity_set": {"opportunity_set_id": "OPS-2"}}),
             "capital_amount_gbp": lambda: _st(amt=1200.0)}
    for _k, _fn in _muts.items():
        _v = approval_validity(_b, _fn())
        ok("MUST-FIRE ISA-0727: a changed %s makes the approval STALE and names it" % _k,
           _v["state"] == "STALE" and _v["changed"] == [_k], _v)
    for _k, _rel, _txt in (("portfolio_snapshot", "pf.json", '{"x":1}'),
                           ("underwriting_store", "underwriting_cases.jsonl", '{"case_id":"C"}'),
                           ("broker_inputs", "broker_venues.json", '{"v":1}'),
                           ("build_id", "Dashboard/state/trusted_build.json", json.dumps({"build_id": "TB-B"})),
                           ("config_roll", "target_weights.json", '{"w": 1}')):
        _b0 = _st()
        _w(_rel, _txt)
        _v = approval_validity(_b0, _st())
        ok("MUST-FIRE ISA-0727: a changed %s makes the approval STALE and names it" % _k,
           _v["state"] == "STALE" and _k in _v["changed"], _v)
    ok("ISA-0727: a plan with no binding is UNBOUND (pre-control), never VALID",
       approval_validity(None, _st())["state"] == "UNBOUND")
    _r1 = dict(r, input_snapshot_id="ISN-1", build_id="TB-A")
    ok("ISA-0727: the approval reference is deterministic and moves with what it authorises",
       approval_reference(_r1) == approval_reference(dict(_r1))
       and approval_reference(_r1) != approval_reference(dict(_r1, underwriting_case_id="UWC-other")))
    _sh.rmtree(_td, ignore_errors=True)

    # ── ISA-0685 at the receipt, and the SHADOW gate ─────────────────────────────────
    _und = authorise(ticker="ABCL", route=ROUTE_ADDITION, as_of="2026-09-20", eligibility=E,
                     sizing=S, risk=R, funding=F, permission=P,
                     membership={"state": "ADMITTED_UNDECIDED",
                                 "may_hold_new_capital_priority": False,
                                 "why": "held, no admitting decision"})
    ok("⚑ MUST-FIRE ISA-0685: an ADMITTED_UNDECIDED holding cannot be AUTHORISED for an "
       "addition — being owned is not being admitted, and the receipt says which",
       _und["capital_status"] != AUTHORISED
       and _und["admission_status"] == "ADMITTED_UNDECIDED"
       and any(c.startswith("NOT_ADMITTED") for c in _und["reason_codes"]), _und)
    _adm = authorise(ticker="COCO", route=ROUTE_ADDITION, as_of="2026-09-20", eligibility=E,
                     sizing=S, risk=R, funding=F, permission=P,
                     membership={"state": "ADMITTED_DECIDED",
                                 "may_hold_new_capital_priority": True, "why": "admitted"})
    ok("POSITIVE CONTROL ISA-0685: an admitted holding authorises normally — the contract must "
       "not block legitimate capital",
       _adm["capital_status"] == AUTHORISED and _adm["admission_status"] == "ADMITTED_DECIDED")
    _noadd = authorise(ticker="ABCL", route=ROUTE_ADDITION, as_of="2026-09-20", eligibility=E,
                       sizing=dict(S, target_gbp=1000.0), risk=R, funding=F, permission=P,
                       current_exposure_gbp=1000.0,
                       membership={"state": "ADMITTED_UNDECIDED",
                                   "may_hold_new_capital_priority": False, "why": "x"})
    ok("⚑ NEGATIVE CONTROL ISA-0685: ADMITTED_UNDECIDED bites only on NEW capital. With zero "
       "increment the receipt is NO_ADD, not a refusal — the holding is not being told to go",
       _noadd["capital_status"] == NO_ADD
       and not any(c.startswith("NOT_ADMITTED") for c in _noadd["reason_codes"]), _noadd)

    _full = dict(r706b, consumed_calculations=dict(r706b["consumed_calculations"],
                                                   event_review_id="IR-2026-10-04-ABCL"))
    ok("⚑ the SHADOW gate is a MEASUREMENT: with every required identity present on every "
       "receipt the boundary is ready to leave SHADOW",
       operational_readiness([_full])["ready_to_leave_shadow"] is True,
       operational_readiness([_full]))
    _miss = operational_readiness([r706b])
    ok("⚑ MUST-FIRE: a missing event_review_id keeps the boundary in SHADOW and NAMES the "
       "identity and the names it is absent on — 'the wiring exists' is not liveness (R4.14)",
       _miss["ready_to_leave_shadow"] is False
       and _miss["missing_identities"].get("event_review_id") == ["ABCL"], _miss)
    ok("NEGATIVE CONTROL: no receipts at all is NOT ready — an empty set proves nothing",
       operational_readiness([])["ready_to_leave_shadow"] is False)
    ok("ISA-0706: a VCI receipt additionally requires its budget calc_id to leave SHADOW",
       "vci_budget_calc_id" in operational_readiness([])["required_by_route"][ROUTE_VCI])

    # ── ISA-0721/0722 ─────────────────────────────────────────────────────────────────
    _bad = authorise(ticker="U", route=ROUTE_ADDITION, as_of="2026-10-03", eligibility=E,
                     sizing=S, risk=R, funding=F, permission=P,
                     underwriting={"case_id": "UWC-x", "er": {"state": "MISSING_REQUIRED_INPUT"},
                                   "admissible_for_positive_size": False})
    ok("⚑ MUST-FIRE ISA-0721: an ADDITION on an inadmissible underwriting case is NOT authorised",
       _bad["capital_status"] != AUTHORISED
       and "NO_ADMISSIBLE_UNDERWRITING_CASE" in _bad["reason_codes"]
       and _bad["underwriting_case_id"] == "UWC-x", _bad)
    _good = authorise(ticker="U", route=ROUTE_ADDITION, as_of="2026-10-03", eligibility=E,
                      sizing=S, risk=R, funding=F, permission=P,
                      underwriting={"case_id": "UWC-y", "er": {"state": "VALID_MECHANICAL"},
                                    "admissible_for_positive_size": True})
    ok("NEGATIVE CONTROL ISA-0722: an admissible case leaves an otherwise-cleared addition "
       "AUTHORISED and the receipt NAMES the case", _good["capital_status"] == AUTHORISED
       and _good["underwriting_case_id"] == "UWC-y", _good)

    if verbose:
        print("\ncapital_authorisation selftest: %d FAIL(s)%s"
              % (len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    print(json.dumps({"schema_version": SCHEMA_VERSION, "states": CAPITAL_STATES,
                      "routes": ROUTES, "fields": RECEIPT_FIELDS}, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
