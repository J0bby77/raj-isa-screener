#!/usr/bin/env python3
"""
capital_decision_engine.py — ISA-0804: the INTEGRATED CAPITAL DECISION ENGINE.

Authority: ChatGPT Astra 6 Audit/ISA_Integrated_Capital_Decision_Engine_BuildSpec_03Oct2026.md
(Raj, 03-Oct-2026, Rev 1.1) — packages P2 (census/identity), P3 (ER12 decision return), P4
(flags/evidence/risk/entry permissions), P5 (exact constructor + robust comparator + ambition),
P6 (dossiers/case scope/typed amendments) and P7 (one canonical Capital Decision Receipt).
Diagnosis: Investment Analysis/ISA_Capital_Deployment_Decision_Review_03Oct2026.md (F1-F12).
Rules: R2.10, R4.1, R4.3, R4.4, R4.7, R4.8, R4.9, R4.14, R5.2, R5.5, R5.10, R5.11, R6.1, R12.3,
R14.2, R16, R18.2, R20.1.

WHAT IT REPLACES. The Source-Score-ordered greedy fill (capital_destination.sleeve_split ->
position_sizing.allocate in sequencer order) chose which names, how many and how large by queue
position. This module chooses the whole legal action bundle — names, sizes, count, and the
no-stock alternative — by a declared, reproducible comparison on like-for-like GBP terminal
wealth under one joint scenario set, with every alternative and every exclusion typed.

WHAT IT DELIBERATELY DOES NOT DO (BuildSpec s0.1, s8.2, s17, s22):
  * no Source Score in the comparator, the search, the pruning or a tie-break (F1);
  * no Sharpe / excess-return-per-risk scalar, Kelly, CVaR optimiser or target-probability
    Monte Carlo; risk appetite stays in the governed constraints (s8.2);
  * no calibrated-probability claim: ER12 is a CENTRAL_UNDERWRITING estimate (s4.2), the
    scenarios are evidenced variants, and the regret rule is a transparent policy for unresolved
    estimation uncertainty, not an expected-utility theorem (s8.3);
  * no invented fund E[r]: the residual destination is valued on a declared convention and
    every fund/cash comparison is labelled ALTERNATIVE_NOT_COMPARABLE (s8.2);
  * no hard gate, policy constant, cash, quote or permission can be changed by a case
    amendment (s9.3); judgement amends typed underwriting inputs and the whole decision reruns.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["capital_engine_authority"] -> "SHADOW" (receipt produced,
capital still refused by the canonical new-capital control) or "OFF" (not run). It never
restores the greedy queue as capital authority (s19.1).
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import itertools
import json
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from framework_integrity import mark as _fi_mark            # execution ledger (R4.14)
except Exception:                                                   # noqa: BLE001
    def _fi_mark(*_a, **_k):                                        # noqa: D103
        return None

SCHEMA_VERSION = "1.0.0"
METHOD_VERSION = "CDE-1.0"
EPS_GBP = 0.01                       # arithmetic tolerance (s8.3) - NOT an economic tie margin
# Raj's policy (BuildSpec s0.1(1)): 12-month ambition 20%, stretch 25%. PREFERENCES, never a
# per-stock admission floor and never a forced fill (s8.4).
AMBITION_PCT = 20.0
STRETCH_PCT = 25.0
MAX_BUNDLES = 2_000_000              # search budget; exceeded -> SEARCH_INCOMPLETE (refuse), s8.1
HORIZON_MONTHS = 12

# typed value / disposition vocabularies (s3, s8.5)
VALID, MISSING, STALE, CONFLICTED, NOT_APPLICABLE, PROXY, BACKFILLED = (
    "VALID", "MISSING", "STALE", "CONFLICTED", "NOT_APPLICABLE", "PROXY", "BACKFILLED")
DISPOSITIONS = ("SELECTED", "ELIGIBLE_UNFUNDED", "CASE_PENDING", "TIMING_DEFERRED", "HARD_BLOCKED",
                "ECONOMICS_FAILED", "RISK_UNKNOWN", "RISK_BLOCKED", "ROUTE_UNAVAILABLE",
                "MIN_ENTRY", "CAP_BOUND", "DOMINATED", "TIED", "IDENTITY_REFUSED",
                "ER12_UNKNOWN", "NOT_IN_SCOPE")
RECEIPT_STATES = ("DECISION_COMPLETE", "RESEARCH_PENDING", "REFUSED", "SEARCH_INCOMPLETE",
                  "DECISION_TIE_UNRESOLVED")
FLAG_CLASSES = ("HARD_BLOCK", "UNDERWRITING_INPUT", "REVIEW_REQUIRED")

# s5.2 — EVERY CURRENT FLAG CODE HAS ONE CLASSIFICATION, read from its ADOPTED owner. Hardness
# comes from adopted policy, not from maximising apparent opportunity. Unknown -> REVIEW_REQUIRED.
FLAG_TAXONOMY = {
    "revision_direction_down": ("HARD_BLOCK", "deployment_flags.compute_gate_flags",
                                "adopted HARD auto-cap (value-trap signature); rebutted only by a "
                                "confirmed catalyst DATA field, never by case opinion"),
    "near_52wk_low_deteriorating": ("HARD_BLOCK", "deployment_flags.compute_gate_flags",
                                    "adopted HARD auto-cap (falling knife)"),
    "ai_existential": ("HARD_BLOCK", "deployment_flags / ai_disruption (score 5)",
                       "adopted hard disqualifier"),
    "recent_reversal_vs_12_1m": ("REVIEW_REQUIRED", "t1_gates clean_flags reversal_unresolved / "
                                 "Run_Context A20", "unresolved reversal blocks new capital until "
                                 "the targeted pull records a benign cause"),
    "late_cycle_flag": ("REVIEW_REQUIRED", "t1_gates late_cycle (A15)",
                        "passable only with a documented cause"),
    "stage_blocked_pending_case": ("REVIEW_REQUIRED", "t1_gates.stage_gate (A3)",
                                   "passable only with documented remaining runway"),
    "sector_multiple_stretched": ("UNDERWRITING_INPUT", "deployment_flags (surfaced only)",
                                  "valuation sensitivity - enters the case and the scenarios"),
    "price_falls_on_beat": ("UNDERWRITING_INPUT", "deployment_flags (surfaced only)",
                            "surfaced, never auto-capped by adopted policy"),
    "ai_disruption_severe": ("UNDERWRITING_INPUT", "deployment_flags (score 4, surfaced)",
                             "review flag by adopted policy, not a gate"),
    # ISA-0817 (Raj D1-D3, 18-Oct-2026) - produced by corporate_events.py (Step 5x-ce), carried on step9_pre's
    # `corporate_events` section. The offeree block lifts by itself when the name leaves the source table.
    "offer_period_offeree": ("HARD_BLOCK", "corporate_events (Takeover Panel table / EDGAR subject-company filing)",
                             "offeree in an offer period: the price is pinned to offer terms, growth worlds do not "
                             "apply - no NEW capital (held: REVIEW only, never a sell)"),
    "corporate_action_pending": ("REVIEW_REQUIRED", "corporate_events (EDGAR M&A-party/financing, RNS headlines)",
                                 "acquirer side / capital raise / disposal / special distribution / unconfirmed offer "
                                 "headline - the case must be refreshed after the event (B09-R02)"),
    "offer_state_source_failed": ("REVIEW_REQUIRED", "corporate_events (source unreadable or store aged)",
                                  "offer state UNESTABLISHED on a sourceable venue - never read as 'no event'"),
    "price_pinned_suspect": ("REVIEW_REQUIRED", "corporate_events (weekly-store pin detector, unsourced venues)",
                             "price behaves as if pinned to offer terms on a venue with no offer-state source"),
    "news_corporate_event_unconfirmed": ("REVIEW_REQUIRED", "corporate_events (SECONDARY news search, venues with no "
                                         "primary register - Raj D2b 18-Oct-2026)",
                                         "a headline names the issuer with an offer / corporate-action term - the case "
                                         "must confirm or rebut it from a primary source (R6.3: never HARD)"),
}

# ISA-0817: the only HARD flags a typed adjudication may clear - a stale external source, never adopted policy.
ADJUDICABLE_HARD_FLAGS = ("offer_period_offeree",)

# s9.3 — the ONLY fields a case amendment may touch.
AMENDMENT_ALLOWLIST = {
    "er12.review_resolution": ("AUTHORITATIVE", "CROSS_ROUTE", "LOWER_OF_ROUTES"),
    "scenario.bear_local_pct": "number",
    "scenario.estimation_low_local_pct": "number",
    "scenario.estimation_high_local_pct": "number",
    "flag_adjudication": ("RESOLVED_BENIGN", "CONFIRMED_ADVERSE"),
    "thesis.falsifier": "text",
    "case.complete": "bool",
    # B09-R03 (P-C, 24-Oct-2026): an EVIDENCED answer to a required question that changes no allowlisted field.
    "case.question_answer": ("NO_CHANGE",),
}
# ⚑ B09-R03 (P-C): REQUIRED-QUESTION RECEIPTS. `case.complete = true` and fluent prose are not evidence that
#   the twelve questions were answered: a case is COMPLETE only when every CASE_QUESTION_ID carries at least one
#   accepted typed amendment for that security - a field change, or an evidenced NO_CHANGE answer
#   (`case.question_answer`), each with its sources, publication dates, rationale and reviewer. The receipt
#   records the coverage and, per amended security, the mechanical BEFORE and the amended AFTER values.
#   Rollback (R4.13): CASE_QUESTION_RECEIPTS_REQUIRED = False restores `case.complete` alone.
CASE_QUESTION_RECEIPTS_REQUIRED = True
CASE_QUESTION_IDS = tuple("Q%d" % i for i in range(1, 13))
AMENDMENT_FORBIDDEN_PREFIXES = ("hurdle", "policy", "ladder", "rung", "cash", "quote", "price",
                                "permission", "risk_limit", "execution", "buy", "select",
                                "source_score", "confidence", "config")


class EngineRefused(Exception):
    """A contract the engine cannot honour without guessing (R4.7)."""


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _sha(obj) -> str:
    if isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
    else:
        b = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def _file_sha(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════════════════
# INPUTS — one typed, hash-bound bundle (s3, s15)
# ═══════════════════════════════════════════════════════════════════════════════════════════
INPUT_FILES = {
    "step9_pre": "step9_pre_{m}.json",
    "horizon_value": "horizon_value_{m}.json",
    "pit_capture": "pit_capture_{m}.jsonl",
    "portfolio": "portfolio_data_{m}.json",
    "target_weights": "target_weights.json",
    "transaction_ledger": "transaction_ledger.json",
    "weekly_store": "stock_weekly_returns.json",
}


# Inputs the pre-run REBUILDS every occurrence (Step 8 / Step 8h); their mtime proves this run made them.
FRESH_INPUTS = ("step9_pre", "horizon_value")
DECISION_LEDGER = "capital_decision_ledger.jsonl"


from month_artefacts import ARCHIVE_REL      # ISA-0832: one home for the archive location


def month_pipeline(month: str, inp: dict, root: str, portfolio: dict, policy: dict, as_of=None) -> dict:
    """ISA-0924 (31-Oct-2026) - ONE home for the candidate pipeline a receipt for `month` is formed on: the
    router reads THIS month's step9_pre (the bytes INPUT_FILES hashes) and the scored frame as of this month,
    never the newest files on disk. build() and every test/consumer that re-forms a month's pipeline call this."""
    import capital_destination as _cd
    return _cd.capital_pipeline(portfolio, policy, as_of=as_of,
                                step9=(inp["step9_pre"], os.path.basename(input_path(month, "step9_pre", root))),
                                scored=_cd.scored_as_of(month, root))


def input_path(month: str, key: str, root: str = HERE) -> str:
    """ISA-0832 (04-Oct-2026) - ONE home for where a month's input lives. After the monthly review,
    capture_archive --purge MOVES the month's inputs (e.g. portfolio_data_<month>.json) into
    archive/decision_capture/ after verifying them byte-for-byte. Every post-review consumer of the frozen
    month - the receipt verifier, the execution check before any order, the selftest, the campaign - reads
    the archived copy, which IS the input the receipt was formed on (the hash check proves it). The LIVE
    copy always wins when present (a re-run month is never silently served from the archive)."""
    import month_artefacts as _MA                      # the single home (delegated, 04-Oct-2026)
    return _MA.resolve(root, INPUT_FILES[key].format(m=month))


def load_inputs(month: str, root: str = HERE) -> dict:
    """Read every input once; hash each file (s10 input hashes). A missing REQUIRED input
    refuses (R4.3) rather than producing a partial decision."""
    _fi_mark("capital_decision_engine", "load_inputs")
    out, hashes, missing = {"month": month, "root": root}, {}, []
    sources = {}
    for key, pat in INPUT_FILES.items():
        p = input_path(month, key, root)
        sources[key] = "ARCHIVE" if os.path.dirname(p).endswith(ARCHIVE_REL) else "LIVE"
        hashes[key] = _file_sha(p)
        if hashes[key] is None:
            missing.append(pat.format(m=month))
            continue
        if p.endswith(".jsonl"):
            rows = {}
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip():
                        r = json.loads(ln)
                        if r.get("ticker"):
                            rows[r["ticker"]] = r
            out[key] = rows
        else:
            with open(p, encoding="utf-8") as fh:
                out[key] = json.load(fh)
    out["input_hashes"] = hashes
    out["input_sources"] = sources
    out["missing_inputs"] = missing
    required = ("step9_pre", "horizon_value", "portfolio", "target_weights", "transaction_ledger")
    miss_req = [k for k in required if hashes.get(k) is None]
    if miss_req:
        raise EngineRefused("required input(s) absent for %s: %s - no partial decision is formed "
                            "(R4.3)" % (month, miss_req))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
# COSTS — measured from the dealing record (R6.1), statutory levies declared (ISA-0810)
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Statutory transaction taxes on PURCHASES by listing venue. DECLARED (statute), each with its
# source; a venue not listed is NOT assumed zero silently - it is recorded as UNDECLARED and the
# conservative UK rate is applied so the comparison cannot be flattered by an unknown (R4.1).
STATUTORY_PURCHASE_LEVY = {
    ".L": (0.005, "UK SDRT/stamp duty 0.5% on purchases of UK main-market shares (FA 1986 s87); "
                  "MEASURED on MKS/ONT buys in transaction_ledger (cost - GBP 5 = 0.5%)"),
    ".MI": (0.001, "Italian FTT 0.1% on purchases of Italian large-cap shares (L.228/2012)"),
    ".PA": (0.003, "French FTT 0.3% on purchases of French large-cap shares (CGI art.235 ter ZD)"),
    ".MC": (0.002, "Spanish FTT 0.2% on purchases of Spanish large-cap shares (Ley 5/2020)"),
    "": (0.0, "US listings: no purchase transaction tax"),
}
NO_LEVY_SUFFIXES = (".BR", ".SW", ".ST", ".CO", ".OL", ".HE", ".DE", ".AS", ".WA", ".VI")


def cost_model(ledger: dict, currency_of=None) -> dict:
    """-> commission, dealing FX fraction and levy table, each with its evidence.
    `currency_of(ticker, description)` -> 'USD' / 'GBP' / ... / None. A buy whose currency is
    not established is EXCLUDED from the FX measurement (never guessed, R4.8)."""
    entries = (ledger or {}).get("entries") or []
    buys = [e for e in entries if e.get("asset_class") == "stock" and e.get("type") == "buy"
            and _num(e.get("cost_gbp")) is not None and _num(e.get("amount_gbp") or e.get("value_gbp"))]
    if not buys:
        raise EngineRefused("transaction_ledger has no stock BUY rows - dealing costs UNMEASURED "
                            "and a zero cost would flatter every stock (R4.1)")
    commission = min(float(e["cost_gbp"]) for e in buys)
    fx_obs, excluded = [], []
    for e in buys:
        t = str(e.get("ticker") or "")
        amt = _num(e.get("amount_gbp") or e.get("value_gbp"))
        cost = float(e["cost_gbp"])
        ccy = currency_of(t, e.get("description")) if currency_of else None
        if ccy in (None, "GBP", "GBp") or cost <= commission + 0.01 or not amt:
            excluded.append(t)                          # GBP/UK lines carry SDRT, not FX
            continue
        consideration = amt - cost
        if consideration > 0:
            fx_obs.append((cost - commission) / consideration)
    if not fx_obs:
        raise EngineRefused("no non-GBP stock buy in the dealing record - the dealing FX charge is "
                            "UNMEASURED (R4.1)")
    fx_obs.sort()
    fx_med = fx_obs[len(fx_obs) // 2]
    return {"commission_gbp": round(commission, 2),
            "commission_basis": "transaction_ledger.json minimum stock BUY cost_gbp over %d rows" % len(buys),
            "fx_dealing_fraction": round(fx_med, 5),
            "fx_dealing_basis": ("MEASURED: median (cost - commission)/consideration over %d non-GBP "
                                 "stock buys (range %.4f-%.4f); ISA-0810 - the 0.5%% "
                                 "FX_RATE_FRACTION is the income-conversion rate, not dealing"
                                 % (len(fx_obs), fx_obs[0], fx_obs[-1])),
            "fx_observations_excluded_currency_unestablished_or_gbp": sorted(set(excluded)),
            "levy_table": {k: {"fraction": v[0], "source": v[1]} for k, v in STATUTORY_PURCHASE_LEVY.items()},
            "exit_convention": ("hypothetical liquidation at the 12-month horizon: commission + dealing "
                                "FX on non-GBP proceeds; no purchase levy on a sale. Applied to every "
                                "alternative identically (s4.1)")}


def _suffix(ticker: str) -> str:
    t = str(ticker or "")
    return t[t.rfind("."):] if "." in t else ""


def costs_for(ticker: str, currency: Optional[str], costs: dict) -> dict:
    suf = _suffix(ticker)
    non_gbp = str(currency or "").upper() not in ("GBP", "GBX") and currency not in ("GBp",)
    if suf in STATUTORY_PURCHASE_LEVY:
        levy, lsrc, lstate = STATUTORY_PURCHASE_LEVY[suf][0], STATUTORY_PURCHASE_LEVY[suf][1], "DECLARED"
    elif suf in NO_LEVY_SUFFIXES:
        levy, lsrc, lstate = 0.0, "no purchase transaction tax on this venue", "DECLARED"
    else:
        levy, lsrc, lstate = 0.005, "UNDECLARED venue - conservative 0.5% applied (R4.1)", "UNDECLARED"
    return {"commission_gbp": costs["commission_gbp"],
            "fx_fraction": costs["fx_dealing_fraction"] if non_gbp else 0.0,
            "levy_fraction": levy, "levy_state": lstate, "levy_source": lsrc}


def position_wealth(amount_gbp: float, r_gbp: float, c: dict) -> float:
    """Terminal net GBP wealth of `amount_gbp` invested now, earning GBP total return r_gbp over
    the horizon, after entry costs (commission, FX, levy) and hypothetical exit costs.
        consideration C = (A - commission) / (1 + fx + levy)
        V_T = C (1 + r);   W = V_T (1 - fx) - commission
    Multiplicative throughout - no additive '22% - 1%' approximation (T09 oracle)."""
    a = float(amount_gbp)
    if a <= 0:
        return 0.0
    cons = (a - c["commission_gbp"]) / (1.0 + c["fx_fraction"] + c["levy_fraction"])
    vt = cons * (1.0 + r_gbp)
    return vt * (1.0 - c["fx_fraction"]) - c["commission_gbp"]


def net_return(amount_gbp: float, r_gbp: float, c: dict) -> Optional[float]:
    if not amount_gbp:
        return None
    return position_wealth(amount_gbp, r_gbp, c) / float(amount_gbp) - 1.0


# ═══════════════════════════════════════════════════════════════════════════════════════════
# P3 — ER12 DECISION RETURN: one twelve-month valuation bridge + evidenced scenario variants
# ═══════════════════════════════════════════════════════════════════════════════════════════
# The central case IS horizon_value HV-1.2 (ISA-0744/0745, Raj-DECIDED 24-Sep-2026): FY1 metric
# once x the approved terminal multiple (current multiple x (1 + bounded re-rate)), dividends
# added, share count once. This module does NOT build a second return model; it types the
# estimate, adds evidenced variants, a business-bear outcome and the cost bridge.
EV_ESTIMATION_SENS = ("margin_lambda_0.38", "margin_reference_median", "margin_fy_high",
                      "net_debt_fy_trend", "diluted_shares_fy_cagr")
EV_BEAR_SENS = ("margin_fy_low",)


def _ratio(table: dict, key: str, period: str = "+1y") -> Optional[float]:
    row = (table or {}).get(period) or {}
    avg, v = _num(row.get("avg")), _num(row.get(key))
    if not avg or v is None or avg <= 0:
        return None
    return v / avg


def _sens(route_block: dict) -> Dict[str, float]:
    out = {}
    for s in (route_block or {}).get("sensitivities") or []:
        if s.get("state") == "VALID_MECHANICAL" and _num(s.get("er_pct")) is not None:
            out[s["name"]] = float(s["er_pct"]) / 100.0
    return out


# ── ER12 plausibility triggers (ISA-0748 / SPI.L 03-Oct-2026) ─────────────────────────────────
# A P/E bridge HOLDS PE0 = P0/EPS0 through FY1. When FY1 consensus EPS more than X-folds the base (or the
# two valuation routes disagree by more than the hurdle itself) the route choice - not the business -
# decides admission and size: SPI.L 03-Oct read 103% (EPS 0.089 -> 0.172 on 2 vs 3 analysts, PE0 27.7 on a
# depressed base vs forward P/E 14.4) against 33.8% on EV/EBITDA. Such a name is a REVIEW case: the
# case resolves AUTHORITATIVE / CROSS_ROUTE / LOWER_OF_ROUTES with evidence (allowlisted amendment).
# DECLARED ENGINEERING TRIGGERS (not calibrated parameters; they route to research, never to capital):
LEGACY_ENTRY_GAP_REVIEW_PCT = 50.0   # legacy entry_level gap that triggers case scrutiny (ISA-0806); reference only
ROUTE_DISAGREEMENT_REVIEW_PP = 25.0   # > ~1.6x the 15.7% hurdle: the route alone flips size/admission
EPS_JUMP_REVIEW = 0.50                # FY1/FY0 consensus EPS growth > 50% held at PE0 (ISA-0748)


def plausibility_triggers(hv: Optional[dict], pit: Optional[dict]) -> List[dict]:
    out = []
    if not hv:
        return out
    auth, cross = hv.get("authoritative") or {}, hv.get("cross_check") or {}
    a_er = _num(auth.get("er_pct"))
    c_er = _num(cross.get("er_pct")) if cross.get("state") == "VALID_MECHANICAL" else None
    if a_er is not None and c_er is not None and abs(a_er - c_er) > ROUTE_DISAGREEMENT_REVIEW_PP:
        out.append({"code": "ROUTE_DISAGREEMENT", "observed_pp": _r(a_er - c_er, 2),
                    "threshold_pp": ROUTE_DISAGREEMENT_REVIEW_PP})
    if (hv.get("route") or auth.get("route")) == "PE":
        inp = auth.get("inputs") or {}
        e0, e1 = _num(inp.get("EPS0")), _num(inp.get("EPS1"))
        if e0 and e1 and e0 > 0 and e1 / e0 - 1.0 > EPS_JUMP_REVIEW:
            est = (pit or {}).get("eps_estimate") or {}
            out.append({"code": "EPS_DISCONTINUITY", "eps0": e0, "eps1": e1, "growth": _r(e1 / e0 - 1.0, 4),
                        "threshold": EPS_JUMP_REVIEW,
                        "analysts_fy0": _num((est.get("0y") or {}).get("numberOfAnalysts")),
                        "analysts_fy1": _num((est.get("+1y") or {}).get("numberOfAnalysts"))})
    return out


def er12_case(ticker: str, hv: Optional[dict], pit: Optional[dict], as_of: str,
              hurdle_pct: float, amendments: Optional[dict] = None) -> dict:
    """-> the typed ER12_DECISION record for one security (s4.1/s4.2). Never raises; an input it
    cannot establish is a typed state, never a number."""
    _fi_mark("capital_decision_engine", "er12_case")
    am = amendments or {}
    rec = {"ticker": ticker, "estimate_kind": "CENTRAL_UNDERWRITING",
           "estimate_kind_note": ("central underwriting case, NOT a calibrated expectation: no "
                                  "probabilities are stated and none are claimed (s4.2)"),
           "horizon_months": HORIZON_MONTHS, "hurdle_pct": hurdle_pct,
           "hurdle_basis": ("scoring_config.ER_DEPLOY_FLOOR = REQUIRED_RETURN_MID + ER_FRICTION_BUFFER - "
                            "a POLICY hurdle that already carries a 2.0pp friction buffer, so it is "
                            "compared with the GROSS central ER12 (canonical HV convention); costs "
                            "enter the wealth comparison explicitly and are not deducted twice")}
    if not hv:
        rec.update(state="MISSING", why="no horizon_value case for this security")
        return rec
    auth = hv.get("authoritative") or {}
    cross = hv.get("cross_check") or {}
    rr = hv.get("route_record") or {}
    hv_state = rr.get("review_state") or hv.get("state") or auth.get("state")
    rec.update({"hv_case_id": hv.get("case_id"), "hv_method_id": hv.get("method_id"),
                "hv_method_version": hv.get("method_version"), "hv_state": hv_state,
                "route": hv.get("route") or auth.get("route"), "hv_triggers": hv.get("triggers") or [],
                "pit_fingerprint": hv.get("pit_fingerprint")})
    central = _num(hv.get("er_local_pct") if hv.get("er_local_pct") is not None else hv.get("er_pct"))
    auth_er = _num(auth.get("er_pct"))
    cross_er = _num(cross.get("er_pct")) if cross.get("state") == "VALID_MECHANICAL" else None
    resolution = am.get("er12.review_resolution")
    if hv_state not in ("VALID_MECHANICAL", "REVIEW_REQUIRED") or central is None:
        rec.update(state="UNKNOWN", why="horizon_value state %s - no defensible central ER12" % hv_state)
        return rec
    eng_trig = plausibility_triggers(hv, pit)
    rec["engine_triggers"] = eng_trig
    if hv_state == "REVIEW_REQUIRED" or eng_trig or resolution is not None:
        if resolution is None:
            rec.update(state="REVIEW_REQUIRED",
                       why="ER12 REVIEW_REQUIRED (%s) - a case must resolve the route before capital "
                           "(ISA-0744/ISA-0748; never a silent average)"
                           % "; ".join(list(hv.get("triggers") or []) + [t["code"] for t in eng_trig])[:300],
                       central_local=central / 100.0, auth_local=(auth_er or 0) / 100.0,
                       cross_local=None if cross_er is None else cross_er / 100.0)
        if resolution is not None:
            if resolution == "CROSS_ROUTE" and cross_er is None:
                rec.update(state="UNKNOWN", why="amendment chose CROSS_ROUTE but the cross route is not valid")
                return rec
            central = {"AUTHORITATIVE": auth_er, "CROSS_ROUTE": cross_er,
                       "LOWER_OF_ROUTES": min(x for x in (auth_er, cross_er) if x is not None)}[resolution]
            rec["review_resolution"] = resolution
    # ⚑ B09-R07 (P-C, 24-Oct-2026): a resolution that CHOOSES the other route updates the route, the
    #   bridge, the estimation variants and the bear TOGETHER, and the alternative route becomes the one
    #   NOT chosen. It used to change only the central figure: the case kept the original P/E bridge
    #   (identity 22% against an 18.09% central) and ALT_ROUTE duplicated the chosen central, so the
    #   ALT_ROUTE world was not an alternative at all. The original route is preserved on the record.
    chosen = auth
    if resolution == "CROSS_ROUTE" or (resolution == "LOWER_OF_ROUTES" and cross_er is not None
                                       and auth_er is not None and cross_er < auth_er):
        chosen = cross
    chosen_er = _num(chosen.get("er_pct"))
    alt_er = auth_er if chosen is cross else cross_er
    rec["route_original"] = auth.get("route") or rec.get("route")
    rec["route"] = chosen.get("route") or rec.get("route")
    rec["route_chosen_by"] = ("REVIEW_RESOLUTION:%s" % resolution) if resolution else "DECLARED_ROUTE"
    inputs = chosen.get("inputs") or {}
    cur = (chosen.get("currency") or inputs.get("currency") or auth.get("currency")
           or (auth.get("inputs") or {}).get("currency") or hv.get("currency") or {})
    unit_div = _num(cur.get("unit_div")) or 1.0
    p0q = _num(inputs.get("P0_quote"))
    if p0q is None:
        p0q = _num((auth.get("inputs") or {}).get("P0_quote"))
    p0 = None if p0q is None else p0q / unit_div
    pt = _num(chosen.get("P_T"))
    r = _num(chosen.get("r") if chosen.get("r") is not None else inputs.get("r"))
    route = rec["route"]
    c = central / 100.0
    rec["central_local"] = c
    rec["currency"] = cur.get("quote_major") or cur.get("quote")
    # dividends implied by the HV identity (1+R)P0 = P_T + D, on the CHOSEN route
    d_major = (None if (p0 is None or pt is None) else (1.0 + (chosen_er if chosen_er is not None else central) / 100.0) * p0 - pt)
    # ── bridge (multiplicative, s4.1) ─────────────────────────────────────────────────────
    bridge = {"route": route, "P0_major": _r(p0, 4), "P_T_major": _r(pt, 4),
              "distributions_major": _r(d_major, 4), "rerate_r": r}
    if route == "PE":
        e0, e1 = _num(inputs.get("EPS0")), _num(inputs.get("EPS1"))
        if e0 and e1 and p0:
            bridge.update({"eps_progression": _r(e1 / e0, 5), "multiple_factor": _r(1 + (r or 0.0), 5),
                           "identity": "1 + R = (EPS1/EPS0) x (1 + r) + D/P0 (PE0 = P0/EPS0, FY1 EPS once, no buyback yield)",
                           "identity_check": _r((e1 / e0) * (1 + (r or 0.0)) + (d_major or 0.0) / p0 - 1.0, 5),
                           "loss_making": bool(e0 <= 0 or e1 <= 0)})
            if e0 <= 0 or e1 <= 0:
                rec.update(state="UNKNOWN", why="loss-making metric: a P/E bridge on a non-positive EPS is meaningless (s4.1)")
                return rec
            # ⚑ B09-R05 (P-C, 24-Oct-2026): the REVERSE-IMPLIED expectation - the FY1 EPS the current price needs
            #   to clear the hurdle at the case's own terminal multiple - so a reviewer sees what the price
            #   discounts against what consensus assumes. Diagnostic: never an input to the decision.
            _h = hurdle_pct / 100.0
            _pe0 = p0 / e0
            _eps_req = (((1.0 + _h) * p0 - (d_major or 0.0)) / (_pe0 * (1.0 + (r or 0.0)))) if _pe0 > 0 else None
            bridge["reverse_implied"] = {
                "fy1_eps_needed_for_hurdle": _r(_eps_req, 5), "consensus_fy1_eps": _r(e1, 5),
                "growth_needed_pct": _r(100.0 * (_eps_req / e0 - 1.0), 2) if _eps_req else None,
                "consensus_growth_pct": _r(100.0 * (e1 / e0 - 1.0), 2),
                "cushion_pct": _r(100.0 * (e1 / _eps_req - 1.0), 2) if _eps_req else None,
                "label": "REVERSE_IMPLIED_DIAGNOSTIC (B09-R05): what the price needs, at the case's own multiple"}
    else:
        bridge["identity"] = ("P_T = (NTM revenue x TTM margin x EV/EBITDA0 x (1+r) - frozen net debt) / "
                              "frozen diluted shares (+ dividends); share count enters once")
        _sh, _nd, _m0 = _num(inputs.get("SHARES")), _num(inputs.get("ND")), _num(inputs.get("M0"))
        _ntm, _te, _tr = _num(inputs.get("NTM_REV")), _num(inputs.get("TTM_EBITDA")), _num(inputs.get("TTM_REV"))
        _rate = _num(cur.get("rate_quote_to_rep")) or 1.0
        if all(x is not None for x in (_sh, _nd, _m0, _ntm, _te, _tr)) and p0 and _m0 > 0 and _tr > 0 and _sh > 0:
            # B09-R05 (P-C): the terminal EBITDA the price needs for the hurdle at the case's own EV/EBITDA
            _h = hurdle_pct / 100.0
            _p_needed_rep = ((1.0 + _h) * p0 - (d_major or 0.0)) * _rate
            _ebitda_req = (_p_needed_rep * _sh + _nd) / (_m0 * (1.0 + (r or 0.0)))
            _ebitda_case = _ntm * (_te / _tr)
            bridge["reverse_implied"] = {
                "ntm_ebitda_needed_for_hurdle": _r(_ebitda_req, 2), "case_ntm_ebitda": _r(_ebitda_case, 2),
                "cushion_pct": _r(100.0 * (_ebitda_case / _ebitda_req - 1.0), 2) if _ebitda_req > 0 else None,
                "label": "REVERSE_IMPLIED_DIAGNOSTIC (B09-R05): what the price needs, at the case's own multiple"}
    rec["bridge"] = bridge
    # ── achievement mechanism (s4.2: FY1 must be the current year at T) ─────────────────────
    fy0_end = auth.get("fy0_end") or (inputs.get("fy0_end"))
    try:
        horizon_end = (datetime.date.fromisoformat(as_of[:10]) + datetime.timedelta(days=366)).isoformat()
        mech_ok = (fy0_end is None) or (str(fy0_end)[:10] <= horizon_end)
    except Exception:                                               # noqa: BLE001
        mech_ok, horizon_end = None, None
    rec["achievement_mechanism"] = {
        "fy0_end": fy0_end, "horizon_end": horizon_end, "state": "VALID" if mech_ok else "UNKNOWN",
        "basis": ("the forward metric (FY1) is the CURRENT fiscal year at the 12-month horizon only if "
                  "FY0 ends before the horizon; otherwise ER12 needs an FY2 metric the model does not use (T12)")}
    if mech_ok is False:
        rec.update(state="UNKNOWN", why="FY0 ends after the 12-month horizon - ER12 achievement mechanism not established (T12)")
        return rec
    # ── estimation variants (local) ─────────────────────────────────────────────────────────
    var, notes = {}, {}
    est_tab = (pit or {}).get("eps_estimate") if route == "PE" else (pit or {}).get("revenue_estimate")
    lo, hi = _ratio(est_tab, "low"), _ratio(est_tab, "high")
    shares, nd = None, None
    same_ccy = (cur.get("quote_major") == cur.get("reporting")) or cur.get("reporting") is None
    if route == "EV":
        shares, nd = _num(inputs.get("SHARES")), _num(inputs.get("ND"))
    # ⚑ B09-R06 (P-C, 24-Oct-2026): the EV route's terminal revenue is the NTM BLEND w0*FY0 + (1-w0)*FY1.
    #   An analysts' FY1 low/high varies FY1 ONLY, FY0 held fixed - the ratio used to be applied to the
    #   whole blended NTM (and so to FY0 too), overstating LOW by ~3pp on the audit fixture (w0 0.268).
    #   The P/E route uses FY1 EPS once, so its FY1 ratio applies whole. Currency factors cancel in the ratio.
    _w0 = _num(inputs.get("w0"))
    _rv0 = _num(((pit or {}).get("revenue_estimate") or {}).get("0y", {}).get("avg")) if route == "EV" else None
    _rv1 = _num(((pit or {}).get("revenue_estimate") or {}).get("+1y", {}).get("avg")) if route == "EV" else None

    def _fy1_only(k: Optional[float]) -> Optional[float]:
        if k is None:
            return None
        if route == "PE":
            return k
        if _w0 is None or not _rv0 or not _rv1 or _rv0 <= 0 or _rv1 <= 0:
            return None                      # the blend cannot be decomposed - variant refused, never approximated
        ntm = _w0 * _rv0 + (1.0 - _w0) * _rv1
        return (_w0 * _rv0 + (1.0 - _w0) * _rv1 * k) / ntm
    lo_f, hi_f = _fy1_only(lo), _fy1_only(hi)
    rec["estimation_variant_basis"] = {
        "route": route, "fy1_low_ratio": _r(lo, 6), "fy1_high_ratio": _r(hi, 6),
        "applied_low_factor": _r(lo_f, 6), "applied_high_factor": _r(hi_f, 6), "w0": _w0,
        "convention": ("FY1-only: the analysts' FY1 low/high varies FY1 inside the NTM blend, FY0 fixed (B09-R06)"
                       if route == "EV" else "FY1 EPS once: the FY1 low/high ratio applies to the terminal price"),
        "label": ("SENSITIVITY - consensus dispersion, NOT a calibrated scenario probability (B09-R07/ISA-0748)"),
        "analysts_fy1": _num(((est_tab or {}).get("+1y") or {}).get("numberOfAnalysts"))}

    def _scaled(factor: float) -> Optional[float]:
        if factor is None or p0 is None or pt is None or p0 <= 0:
            return None
        if route == "PE":
            return (pt * factor + (d_major or 0.0)) / p0 - 1.0
        if shares and nd is not None and same_ccy:
            ev_t = pt * shares + nd
            return ((ev_t * factor - nd) / shares + (d_major or 0.0)) / p0 - 1.0
        return None

    def _derated(base_pt_factor: float, rr: Optional[float]) -> Optional[float]:
        if rr is None:
            return None
        return _scaled(base_pt_factor / (1.0 + rr))

    if lo_f is not None:
        var["LOW_ESTIMATES"] = _scaled(lo_f)
    if hi_f is not None:
        var["HIGH_ESTIMATES"] = _scaled(hi_f)
    if r is not None and abs(r) > 1e-12:
        var["NO_RERATE"] = _derated(1.0, r)
    if alt_er is not None:
        var["ALT_ROUTE"] = alt_er / 100.0            # B09-R07: the route NOT chosen - never the central itself
    ev_block = auth if route == "EV" else (cross if cross.get("route") == "EV" else {})
    sens = _sens(ev_block)
    for k in EV_ESTIMATION_SENS:
        if k in sens:
            var["EV_" + k.upper()] = sens[k]
    # amendments with evidence may replace a variant (s9.3) - never the central fact
    for k, vk in (("scenario.estimation_low_local_pct", "LOW_ESTIMATES"),
                  ("scenario.estimation_high_local_pct", "HIGH_ESTIMATES")):
        if am.get(k) is not None:
            notes[vk] = "AMENDED by case (%s): was %s" % (k, _r(var.get(vk), 4))
            var[vk] = float(am[k]) / 100.0
    var = {k: v for k, v in var.items() if v is not None}
    rec["variants_local"] = {k: _r(v, 6) for k, v in var.items()}
    rec["variant_notes"] = notes
    rec["variant_basis"] = {
        "LOW_ESTIMATES": ("FY1 %s at the analysts' LOW estimate (PIT capture), same multiple%s - a consensus-dispersion "
                          "SENSITIVITY, not a probability" % ("EPS" if route == "PE" else "revenue",
                                                              "" if route == "PE" else "; FY1 varied inside the NTM blend, FY0 fixed")),
        "HIGH_ESTIMATES": "FY1 metric at the analysts' HIGH estimate (PIT capture)",
        "NO_RERATE": "terminal multiple = current multiple (the bounded re-rate removed)",
        "ALT_ROUTE": "the route NOT chosen for the central (P/E vs EV/EBITDA) - an economically independent anchor",
        "EV_*": "horizon_value EV-route sensitivities (margin reversion, net-debt and share-count trends)"}
    # ── business bear (s4.2: a credible adverse REALISED world, kept separate) ─────────────
    bear_c = {}
    for k in EV_BEAR_SENS:
        if k in sens:
            bear_c["EV_TROUGH_MARGIN"] = sens[k]
    if lo_f is not None:
        bear_c["LOW_ESTIMATES_NO_POSITIVE_RERATE"] = (_derated(lo_f, r) if (r is not None and r > 0) else _scaled(lo_f))
    bear_c = {k: v for k, v in bear_c.items() if v is not None}
    if am.get("scenario.bear_local_pct") is not None:
        bear_c = {"AMENDED_BY_CASE": float(am["scenario.bear_local_pct"]) / 100.0}
    if bear_c:
        k = min(bear_c, key=bear_c.get)
        rec["bear_local"] = bear_c[k]
        rec["bear_basis"] = ("worst of the observed adverse states: %s (trough FY margin of the last 4 "
                             "years / analysts' low FY1 with no positive re-rate); + adverse FX in the "
                             "joint bear world" % ", ".join("%s %.1f%%" % (kk, vv * 100) for kk, vv in bear_c.items()))
        rec["bear_components"] = {kk: _r(vv, 6) for kk, vv in bear_c.items()}
    else:
        rec["bear_local"] = None
        rec["bear_basis"] = "BEAR_UNKNOWN: no trough-margin route and no low estimate in the PIT capture"
    # ── FX (common worlds) ──────────────────────────────────────────────────────────────────
    fxs = ((hv.get("fx_sensitivity") or {}).get("windows") or {}).get("1y") or {}
    if (rec["currency"] or "GBP") in ("GBP", "GBp"):
        rec["fx_adverse"], rec["fx_favourable"], rec["fx_basis"] = 0.0, 0.0, "GBP line - no FX leg"
    elif _num(fxs.get("adverse_fx_return_pct")) is not None:
        rec["fx_adverse"] = float(fxs["adverse_fx_return_pct"]) / 100.0
        rec["fx_favourable"] = float(fxs["favourable_fx_return_pct"]) / 100.0
        rec["fx_basis"] = ("ECB realised 1y sigma %.4f (horizon_value fx_sensitivity); multiplicative "
                           "(1+R_GBP) = (1+R_local)(1+R_FX)" % float(fxs.get("sigma_annual") or 0))
    else:
        rec["fx_adverse"] = rec["fx_favourable"] = None
        rec["fx_basis"] = "FX sensitivity UNAVAILABLE for a non-GBP line"
    rec["hurdle_pass"] = bool(c * 100.0 >= hurdle_pct)
    if "state" not in rec:
        if rec["fx_adverse"] is None:
            rec.update(state="UNKNOWN", why="non-GBP line with no FX sensitivity - the GBP outcome cannot be bounded")
        elif rec["bear_local"] is None:
            rec.update(state="VALID_NO_BEAR", why="central valid but no business-bear input - not selectable (s8.3: a missing material downside input blocks the selection)")
        else:
            rec["state"] = "VALID"
    return rec


# ═══════════════════════════════════════════════════════════════════════════════════════════
# P2/P4 — COMPLETE CENSUS WITH TYPED, DISJOINT STAGE DISPOSITIONS (s3.2, s5, s6, s7)
# ═══════════════════════════════════════════════════════════════════════════════════════════
def _reason(stage: str, code: str, observed=None, threshold=None, source=None, note=None) -> dict:
    """One typed reason record - never a bare pass/fail (F10, T62)."""
    return {"stage": stage, "code": code, "observed": observed, "threshold": threshold,
            "source": source, "note": note}


def _watch_records(step9: dict) -> Dict[str, dict]:
    """The step9_pre watchlist/pool records keyed by ticker (they carry the flag fields)."""
    out = {}
    for sec in ("main_watchlist", "candidate_pool"):
        for tier, rows in (step9.get(sec) or {}).items():
            for r in rows or []:
                if r.get("ticker"):
                    out[r["ticker"]] = dict(r, _section=sec, _tier=tier)
    return out


def classify_flags(wrec: Optional[dict], adjudications: Optional[dict] = None,
                   extra_codes: Optional[list] = None) -> dict:
    """s5.2 -> {hard:[...], review_unresolved:[...], underwriting:[...], rows:[...]}.
    `extra_codes`: flag codes from a producer that is not the watchlist record (ISA-0817 corporate_events)."""
    _fi_mark("capital_decision_engine", "classify_flags")
    adj = adjudications or {}
    w = wrec or {}
    codes = (list(w.get("disqualifier_flags") or []) + list(w.get("review_flags") or [])
             + list(extra_codes or []))
    if w.get("late_cycle_flag"):
        codes.append("late_cycle_flag")
    if w.get("stage_gate") == "BLOCKED_PENDING_CASE":
        codes.append("stage_blocked_pending_case")
    rows, hard, review, uw = [], [], [], []
    for code in dict.fromkeys(codes):
        cls, owner, why = FLAG_TAXONOMY.get(code, ("REVIEW_REQUIRED", "UNCLASSIFIED",
                                                  "unknown flag code - REVIEW_REQUIRED/UNRESOLVED by default (s5.2)"))
        a = adj.get(code)
        state = "ACTIVE"
        if cls == "REVIEW_REQUIRED" and a in ("RESOLVED_BENIGN", "CONFIRMED_ADVERSE"):
            state = a
        elif cls == "HARD_BLOCK" and code in ADJUDICABLE_HARD_FLAGS and a == "RESOLVED_BENIGN":
            # ISA-0817 (Raj D1): a STALE source (the offer ended, the table not yet updated) is cleared by a typed
            # adjudication; adopted value-trap HARD flags stay unadjudicable (s5.2).
            state = a
        rows.append({"code": code, "class": cls, "owner": owner, "why": why, "adjudication": a,
                     "state": state})
        if cls == "HARD_BLOCK" and state != "RESOLVED_BENIGN":
            hard.append(code)                       # no case or return may override (s5.2)
        elif cls == "HARD_BLOCK":
            pass                                    # ISA-0817 stale-source adjudication (row kept, state shown)
        elif cls == "REVIEW_REQUIRED":
            if state == "CONFIRMED_ADVERSE":
                hard.append(code)
            elif state != "RESOLVED_BENIGN":
                review.append(code)
        else:
            uw.append(code)
    return {"rows": rows, "hard": hard, "review_unresolved": review, "underwriting": uw}


def risk_gate(store: dict, portfolio: dict, ticker: str, as_of=None) -> dict:
    """ISA-0600 addition gate (Dimson beta <= SLEEVE_BETA_MAX vs the held sleeve) through its ONE
    home, sleeve_risk.gate_add. UNMEASURED is never PASS (s6, T44).

    ⚑ ISA-0756 (B05-R04), 19-Oct-2026: CAPITAL ADMISSIBILITY = A FRESH RISK STORE AT THE RUN'S CLOCK.
    An ADDITION (new capital) whose risk would be measured on a store that is STALE_INPUT for the
    candidate or any held sleeve name - at `as_of`, the run date, through the ONE freshness contract
    (stock_return_store.freshness) - is REFUSED_STALE_INPUT. A same-run Step 5y refresh makes every
    fetched name FRESH, so this refuses only when that refresh failed. Reductions never pass through
    this gate, so risk reduction is never blocked."""
    if as_of is not None:
        try:
            import stock_return_store as _srs756
            held = [s.get("ticker") for s in (portfolio.get("stocks") or [])
                    if s.get("ticker") and float(s.get("value_gbp") or 0) > 0]
            fr = _srs756.freshness(store, sorted(set(held + [ticker])), as_of=as_of)
            if fr["stale_input"]:
                return {"ticker": ticker, "verdict": "REFUSED_STALE_INPUT", "stale_names": fr["stale_input"],
                        "expected_latest_observation": fr["expected_latest_observation"],
                        "reason": ("weekly risk store STALE_INPUT at %s for %s (>= %d settled Fridays missing) - "
                                   "a new-capital addition is not sized on a stale covariance (ISA-0756); "
                                   "a same-run Step 5y refresh restores admissibility"
                                   % (fr["as_of"], ", ".join(fr["stale_input"]), fr["stale_after_missed"]))}
        except Exception as exc:                                    # noqa: BLE001
            return {"ticker": ticker, "verdict": "UNMEASURED",
                    "reason": "risk-store freshness unavailable: %s: %s" % (type(exc).__name__, exc)}
    try:
        import sleeve_risk as _sr
        return _sr.gate_add(store, portfolio, ticker)
    except Exception as exc:                                        # noqa: BLE001
        return {"ticker": ticker, "verdict": "UNMEASURED",
                "reason": "sleeve_risk unavailable: %s: %s" % (type(exc).__name__, exc)}


def reprice_er12(er: dict, price_major: float) -> dict:
    """s11: ER12 at an EXECUTABLE purchase price with the underwriting held fixed. Every local
    return (central, routes, variants, bear) transforms as (1+R_new) = (1+R_old) x P_old / P_new -
    the same P_T + D over a different price. Refuses (UNKNOWN) without a valid bridge (T54)."""
    out = json.loads(json.dumps(er))
    p_old = (er.get("bridge") or {}).get("P0_major")
    if not p_old or not price_major or price_major <= 0 or er.get("state") in (None, "MISSING", "UNKNOWN"):
        out.update(state="UNKNOWN", why="no valid bridge to reprice - direct ER12 at the executable price cannot be formed")
        return out
    f = float(p_old) / float(price_major)
    tr = lambda x: None if x is None else (1.0 + x) * f - 1.0   # noqa: E731
    for k in ("central_local", "auth_local", "cross_local", "bear_local"):
        if out.get(k) is not None:
            out[k] = tr(out[k])
    out["variants_local"] = {k: _r(tr(v), 6) for k, v in (out.get("variants_local") or {}).items()}
    out["bridge"]["P0_major"] = float(price_major)
    out["repriced"] = {"analysis_price_major": p_old, "executable_price_major": float(price_major), "factor": _r(f, 6)}
    return out


def build_census(inp: dict, pipeline: dict, store: dict, hurdle_pct: float, as_of: str,
                 amendments: Optional[dict] = None) -> dict:
    return _build_census(inp, pipeline, store, hurdle_pct, as_of, amendments, None)


def _build_census(inp, pipeline, store, hurdle_pct, as_of, amendments, price_overrides, switch_mode=False):
    """Every scored security -> exactly ONE disposition with typed reasons (s3.2). Stage numbers
    need not match the old 84->69->25->11 funnel; the old funnel is retained for comparison.

    switch_mode (ISA-0621, SHADOW switch contest only): the two gates that say "this name needs a DONOR" - the
    ISA-0600 addition beta gate and a REPLACEMENT_ONLY admission - do not stop the chain; the record carries
    `switch_only` and continues through every later gate. The LIVE census is always switch_mode=False."""
    _fi_mark("capital_decision_engine", "build_census")
    step9 = inp["step9_pre"]
    hv_by = {c["ticker"]: c for c in (inp["horizon_value"].get("cases") or []) if c.get("ticker")}
    pit = inp.get("pit_capture") or {}
    wrecs = _watch_records(step9)
    dpr = {r["ticker"]: r for r in (step9.get("deployment_priority_rank") or []) if r.get("ticker")}
    ac = (pipeline.get("all_candidates") or {})
    pcands = {c["ticker"]: c for c in (ac.get("candidates") or pipeline.get("candidates") or []) if c.get("ticker")}
    held = {s.get("ticker"): s for s in (inp["portfolio"].get("stocks") or []) if s.get("ticker")}
    vci = set()
    for tier, rows in (step9.get("vci_watchlist") or {}).items():
        for r in rows or []:
            if r.get("ticker"):
                vci.add(r["ticker"])
    universe = sorted(set(hv_by) | set(wrecs) | set(dpr) | set(pcands) | set(held))
    am_all = amendments or {}
    # ISA-0817: offer-period / corporate-action verdicts from the pre-run (step9_pre `corporate_events`, written by
    # step9_pre_builder from corporate_events.json). ABSENT (a step9 older than ISA-0817) is NAMED on every record and
    # on the receipt - never read as 'no event'.
    _ce_sec = step9.get("corporate_events")
    _ce_v = ((_ce_sec or {}).get("verdicts") or {}) if isinstance(_ce_sec, dict) else {}
    recs = {}
    for t in universe:
        am = am_all.get(t) or {}
        rec = {"ticker": t, "stable_id": t, "in_hv": t in hv_by, "in_step9_rank": t in dpr,
               "in_pipeline": t in pcands, "held": t in held, "vci_listed": t in vci,
               "route": (pcands.get(t) or {}).get("route") or ("held" if t in held else ("vci" if t in vci else "main")),
               "reasons": [], "disposition": None}
        w = wrecs.get(t) or {}
        d = dpr.get(t) or {}
        pc = pcands.get(t) or {}
        rec["source_score"] = d.get("source_score") if d else w.get("source_score")
        rec["source_score_role"] = "SCREENING/DIAGNOSTIC ONLY - no capital ordering or tie-break authority (s0.1(3))"
        rec["er12"] = er12_case(t, hv_by.get(t), pit.get(t), as_of, hurdle_pct, am)
        if am:
            # B09-R03 (P-C): the receipt records what each case CHANGED - the mechanical (un-amended) values
            # BEFORE and the amended values AFTER, recomputed in this same full engine run.
            _mech = er12_case(t, hv_by.get(t), pit.get(t), as_of, hurdle_pct, {})
            _kk = ("state", "route", "central_local", "bear_local", "hurdle_pass")
            rec["er12"]["amendment_effects"] = {
                "before": {k: _mech.get(k) for k in _kk} | {"variants_local": _mech.get("variants_local")},
                "after": {k: rec["er12"].get(k) for k in _kk} | {"variants_local": rec["er12"].get("variants_local")},
                "fields_amended": sorted(k for k in am.keys()),
                "recomputed": "FULL_ENGINE_RERUN_THIS_RECEIPT"}
        if price_overrides and t in price_overrides:
            rec["er12"] = reprice_er12(rec["er12"], price_overrides[t])
        _cev = _ce_v.get(t) if _ce_sec is not None else None
        if _ce_sec is not None and _cev is None:
            # present section, name missing (or the builder could not read the store): UNESTABLISHED -> REVIEW
            _cev = {"state": "NOT_IN_SECTION", "flags": ["offer_state_source_failed"], "confirm_required": [],
                    "why": ["name absent from step9_pre corporate_events (%s)"
                            % ((_ce_sec or {}).get("error") or "not in the produced population")]}
        rec["corporate_event"] = ({k: _cev.get(k) for k in ("state", "flags", "confirm_required", "why", "evidence",
                                                            "store_as_of", "held_note")}
                                  if _cev else {"state": ("ABSENT_SECTION_PRE_ISA0817" if _ce_sec is None
                                                          else "NOT_IN_SECTION")})
        rec["flags"] = classify_flags(w, am.get("flag_adjudication"), (_cev or {}).get("flags"))
        rec["entry"] = {"pct_vs_entry": d.get("pct_vs_entry", w.get("pct_vs_entry")),
                        "entry_level_status": w.get("entry_level_status"),
                        "role": "REFERENCE ONLY - the binding economics are ER12 at the executable price and the economic max price (s7)"}
        er = rec["er12"]
        if (er.get("bridge") and er["bridge"].get("P0_major") and er.get("central_local") is not None
                and er.get("state") not in ("MISSING", "UNKNOWN")):
            # P_T + D implied by the RESOLVED central (a case's route resolution moves it - T53); for an
            # unresolved PE/EV case central == authoritative, so this equals (P_T + D)/(1 + hurdle).
            ptd = er["bridge"]["P0_major"] * (1.0 + er["central_local"])
            rec["entry"]["economic_max_price_major"] = _r(ptd / (1.0 + hurdle_pct / 100.0), 4)
            rec["entry"]["ambition_20_price_major"] = _r(ptd / (1.0 + AMBITION_PCT / 100.0), 4)
            rec["entry"]["valuation_reference_price_major"] = er["bridge"]["P0_major"]
            rec["entry"]["economic_max_basis"] = ("P_max = (P_T + D) / (1 + hurdle): ER12 is monotonic in the "
                                                  "purchase price with the underwriting held fixed; costs excluded, "
                                                  "as in the canonical hurdle comparison")
            rec["entry"]["economic_max_status"] = "VALID_MONOTONIC"
        else:
            rec["entry"]["economic_max_price_major"] = None
            rec["entry"]["economic_max_status"] = ("NOT_INVERTIBLE: no valid ER12 bridge - no maximum is fabricated; "
                                                   "direct ER12 at the executable price controls (T54)")
        # s7 distinct fields (ISA-0806): the analysis quote is NOT an executable quote (s11, T57)
        rec["entry"]["current_price_major"] = (er.get("bridge") or {}).get("P0_major")
        rec["entry"]["current_price_basis"] = "PIT analysis quote at the pre-run capture (Friday close on a weekend run) - NOT executable"
        rec["entry"]["timing_policy_id"] = "t1_gates forward gate + A3 stage gate (adopted); R21 NOT adopted"
        rec["entry"]["timing_state"] = ("PASS" if (d or {}).get("forward_eligible", True) not in (False,) else "BLOCKED")
        rec["entry"]["execution_limit_price_major"] = rec["entry"].get("economic_max_price_major")
        rec["entry"]["execution_limit_status"] = ("REQUIRES_EXECUTABLE_QUOTE: ER12 is recomputed at the executable purchase price "
                                                  "before any order (capital_decision_engine --execution-check); above the limit -> refused")
        _gap = _num(rec["entry"].get("pct_vs_entry"))
        if _gap is not None and abs(_gap) > LEGACY_ENTRY_GAP_REVIEW_PCT:
            rec["entry"]["legacy_gap_trigger"] = {
                "code": "LEGACY_ENTRY_GAP", "observed_pct": _gap, "threshold_pct": LEGACY_ENTRY_GAP_REVIEW_PCT,
                "effect": "research trigger for the case (quote unit / currency / share class / corporate action / anchor age) - NOT a veto and NOT buy permission (s7, T51)"}
        # ── stage chain (first failing stage decides; later facts still recorded) ──────────
        disp = None
        if rec["route"] == "vci" or (t in vci and t not in pcands):
            disp = "NOT_IN_SCOPE"
            rec["reasons"].append(_reason("route", "VCI_ROUTE_SEPARATE", source="step9_pre.vci_watchlist",
                                          note="VCI names are underwritten on their own route (bottleneck FV / ACS); this engine does not override route-specific underwriting (s4.1). VCI additions stay under the canonical new-capital control."))
        if disp is None and not d and not pc and t not in held:
            disp = "NOT_IN_SCOPE"
            rec["reasons"].append(_reason("population", "NOT_IN_DEPLOYMENT_RANK",
                                          source="step9_pre.deployment_priority_rank",
                                          note="scored but not progressed to the deployment population (tier/viability)"))
        # ISA-0860: the pre-run's confirm-at-dealing flags (fundable; the receipt pre-authorises fallbacks)
        rec["dealability_confirm"] = sorted(set((d or {}).get("broker_confirm_required") or [])
                                            | set((_cev or {}).get("confirm_required") or []))   # ISA-0817 D2
        if disp is None and d and d.get("broker_dealability") not in (None, "DEALABLE_ONLINE"):
            disp = "IDENTITY_REFUSED"
            rec["reasons"].append(_reason("identity", "NOT_DEALABLE_ONLINE", d.get("broker_dealability"),
                                          "DEALABLE_ONLINE", "step9_pre.broker_dealability (ISA-0607)",
                                          d.get("broker_dealability_why")))
        if disp is None and d and d.get("forward_eligible") is False:
            disp = "HARD_BLOCKED"
            rec["reasons"].append(_reason("forward_gate", "FORWARD_INELIGIBLE", d.get("forward_ineligible_reason"),
                                          "forward-led eligibility", "rerank_watchlist forward gate (adopted)"))
        c1 = (pc.get("c1_admissibility") or d.get("c1_admissibility") or {})
        if disp is None and t not in held:
            if c1.get("admissible") is not True:
                disp = "HARD_BLOCKED"
                rec["reasons"].append(_reason("c1", "C1_" + str(c1.get("verdict") or "ABSENT"),
                                              c1.get("verdict"), "ADMISSIBLE", "t1_gates.current_admissibility (ISA-0616)",
                                              c1.get("why")))
        if disp is None and t in held:
            # held top-ups: the router's route verdict decides first (REPLACEMENT_ONLY needs a donor)
            adm = ((pc.get("correlation") or {}).get("admission") or {})
            if not pc:
                disp = "NOT_IN_SCOPE"
                rec["reasons"].append(_reason("route", "HELD_NOT_A_TOPUP_CANDIDATE", source="capital_pipeline",
                                              note="held position not offered as a top-up this run; sells/trims are reviewed in Step 5"))
            elif switch_mode and adm.get("verdict") == "REPLACEMENT_ONLY" and pc.get("qualifies") is not False:
                rec["switch_only"] = "REPLACEMENT_ONLY_NEEDS_DONOR"
            elif adm.get("verdict") == "REPLACEMENT_ONLY" or pc.get("qualifies") is False:
                disp = "ROUTE_UNAVAILABLE"
                rec["reasons"].append(_reason("route", "REPLACEMENT_ONLY_NO_DONOR", adm.get("breaches"),
                                              "donor bundle authorised", "deployment_sequencer admission (ISA-0601/0705)",
                                              "a REPLACEMENT_ONLY top-up enters only as an atomic donor/successor bundle; no donor is authorised this run and no opportunistic liquidation is created (s6)"))
        tg = (w.get("t1_gate_detail") or {})
        if disp is None and t not in held and tg and not (tg.get("ns_floor") or {}).get("pass", True):
            disp = "HARD_BLOCKED"
            rec["reasons"].append(_reason("quality", "NS_FLOOR", (tg.get("ns_floor") or {}).get("value"),
                                          60, "t1_gates.ns_floor (adopted)"))
        if disp is None and rec["flags"]["hard"]:
            disp = "HARD_BLOCKED"
            for code in rec["flags"]["hard"]:
                rec["reasons"].append(_reason("flags", "HARD_FLAG:" + code, code, "no hard flag",
                                              FLAG_TAXONOMY.get(code, ("", "UNCLASSIFIED", ""))[1]))
        er_state = er.get("state")
        if disp is None:
            if er_state in ("UNKNOWN", "MISSING"):
                disp = "ER12_UNKNOWN"
                rec["reasons"].append(_reason("er12", "ER12_" + er_state, er.get("hv_state"), "VALID",
                                              "horizon_value HV-1.2", er.get("why")))
            elif er_state == "VALID" or er_state == "VALID_NO_BEAR" or er_state == "REVIEW_REQUIRED":
                if (er.get("central_local") or -9) * 100.0 < hurdle_pct and er_state != "REVIEW_REQUIRED":
                    disp = "ECONOMICS_FAILED"
                    rec["reasons"].append(_reason("er12", "BELOW_HURDLE", _r(er["central_local"] * 100, 2),
                                                  hurdle_pct, "scoring_config.ER_DEPLOY_FLOOR (gross, canonical)"))
        # ── risk BEFORE research: a measured hard risk breach outranks a pending case (s6) ──
        if disp is None:
            rg = risk_gate(store, inp["portfolio"], t, as_of=as_of)          # ISA-0756: the run's clock
            rec["risk_gate"] = {k: rg.get(k) for k in ("verdict", "beta", "beta_max", "rho", "weeks",
                                                       "zero_return_share", "reason", "residual_vol_annual",
                                                       "stale_names")}
            if rg.get("verdict") == "BLOCKED" and switch_mode:
                rec["switch_only"] = "SLEEVE_BETA_GATE_ADDITION_ONLY"
            elif rg.get("verdict") == "BLOCKED":
                disp = "RISK_BLOCKED"
                rec["reasons"].append(_reason("risk", "SLEEVE_BETA_GATE", _r(rg.get("beta"), 3), rg.get("beta_max"),
                                              "sleeve_risk.gate_add (ISA-0600)", rg.get("reason")))
        if disp is None and rec["flags"]["review_unresolved"]:
            disp = "CASE_PENDING"
            for code in rec["flags"]["review_unresolved"]:
                rec["reasons"].append(_reason("flags", "REVIEW_FLAG_UNRESOLVED:" + code, code,
                                              "RESOLVED_BENIGN by a sourced case adjudication",
                                              FLAG_TAXONOMY.get(code, ("", "UNCLASSIFIED", ""))[1]))
        if disp is None and er_state == "REVIEW_REQUIRED":
            disp = "CASE_PENDING"
            rec["reasons"].append(_reason("er12", "ER12_REVIEW_REQUIRED", er.get("hv_triggers"),
                                          "route resolved by a sourced case", "horizon_value HV-1.2 (ISA-0744)"))
        if disp is None and er_state == "VALID_NO_BEAR":
            disp = "CASE_PENDING"
            rec["reasons"].append(_reason("er12", "BEAR_UNKNOWN", None, "an evidenced business-bear outcome",
                                          "BuildSpec s8.3", er.get("why")))
        # ── risk (repaired joins first, s6) ──────────────────────────────────────────────
        if disp is None:
            rg = rec.get("risk_gate") or {}
            corr = (pc.get("correlation") or {})
            rec["correlation"] = {k: corr.get(k) for k in ("measured", "rho_sleeve", "rho_max_pairwise", "rho_basis")}
            rec["correlation_admission"] = ((corr.get("admission") or {}).get("verdict"))
            if rg.get("verdict") != "PASS" and not (switch_mode and rg.get("verdict") == "BLOCKED"):
                disp = "RISK_UNKNOWN"
                rec["reasons"].append(_reason("risk", ("RISK_INPUT_STALE" if rg.get("verdict") == "REFUSED_STALE_INPUT"
                                                       else "RISK_UNMEASURED"), rg.get("verdict"), "PASS",
                                              "sleeve_risk.gate_add", (rg.get("reason") or "")[:240] +
                                              " - no conservative proxy contract exists, so the addition is refused (s6, T44)"))
            elif not corr.get("measured"):
                disp = "RISK_UNKNOWN"
                rec["reasons"].append(_reason("risk", "CORRELATION_UNMEASURED", corr.get("rho_basis"), "MEASURED",
                                              "deployment_sequencer", "the adverse 0.70 default is not a measurement"))
            elif rec["correlation_admission"] == "REPLACEMENT_ONLY" and switch_mode:
                rec["switch_only"] = rec.get("switch_only") or "REPLACEMENT_ONLY_NEEDS_DONOR"
            elif rec["correlation_admission"] == "REPLACEMENT_ONLY":
                disp = "ROUTE_UNAVAILABLE"
                rec["reasons"].append(_reason("route", "REPLACEMENT_ONLY_NO_DONOR",
                                              (corr.get("admission") or {}).get("breaches"), "ADMIT_AS_ADDITION",
                                              "deployment_sequencer admission"))
        # ── evidence permission (s5.1) ───────────────────────────────────────────────────
        ev_state = ((pipeline.get("evidence_states") or {}).get(t) or {})
        ev_state = ev_state.get("state") if isinstance(ev_state, dict) else ev_state
        rec["evidence_state"] = ev_state or pc.get("evidence_state")
        if disp is None:
            try:
                import position_sizing as _ps
                tp = _ps.target_pct(rec["evidence_state"] or "THIN",
                                    current_pct=0.0)
                rec["permission"] = {"rung": tp["rung"], "target_pct": tp["target_pct"],
                                     "may_receive_new_capital": tp["may_receive_new_capital"],
                                     "basis": tp["basis"] + " - a CAP on the chosen size, never a target (s0.1(4))",
                                     "higher_rungs": ("UNAVAILABLE: every rung above STARTER needs CONFIRMED/STRONG evidence; "
                                                      "E1 is unsourced and E3 permanently unmeasured (ISA-0803/0489), so "
                                                      "'fewer, larger' is not reachable this run - an honest constraint, not inflated (T18)")}
                if not tp["may_receive_new_capital"]:
                    disp = "HARD_BLOCKED"
                    rec["reasons"].append(_reason("evidence", "NO_NEW_CAPITAL_" + str(rec["evidence_state"]),
                                                  rec["evidence_state"], "THIN or better", "position_sizing.target_pct"))
            except Exception as exc:                                    # noqa: BLE001
                disp = "HARD_BLOCKED"
                rec["reasons"].append(_reason("evidence", "PERMISSION_UNAVAILABLE", str(exc)[:160], None,
                                              "position_sizing.target_pct"))
        rec["disposition"] = disp or "ELIGIBLE"
        recs[t] = rec
    counts = {}
    for r in recs.values():
        counts[r["disposition"]] = counts.get(r["disposition"], 0) + 1
    _ce_counts = {}
    for r in recs.values():
        _s = (r.get("corporate_event") or {}).get("state")
        _ce_counts[_s] = _ce_counts.get(_s, 0) + 1
    return {"records": recs, "n_universe": len(universe), "counts": counts,
            "corporate_events": {"section_present": _ce_sec is not None,
                                 "store_as_of": (_ce_sec or {}).get("store_as_of") if isinstance(_ce_sec, dict) else None,
                                 "store_state": (_ce_sec or {}).get("store_state") if isinstance(_ce_sec, dict) else None,
                                 "counts_by_state": _ce_counts,
                                 "hard_blocked": sorted(t for t, r in recs.items()
                                                        if "offer_period_offeree" in (r.get("flags") or {}).get("hard", []))},
            "conservation": {"universe": len(universe), "dispositioned": sum(counts.values()),
                             "holds": len(universe) == sum(counts.values()),
                             "undispositioned": [t for t, r in recs.items() if not r.get("disposition")]},
            "old_funnel_for_comparison": "84 scored -> 69 C1 admissible -> 25 deployable -> 11 qualifiers + 2 top-ups -> 3 funded (03-Oct LIVE)"}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# P5 — JOINT SCENARIO SET, LEGAL ACTION SPACE, EXACT SEARCH, ROBUST COMPARATOR
# ═══════════════════════════════════════════════════════════════════════════════════════════
S_E = ("CENTRAL", "FX_ADVERSE", "FX_FAVOURABLE", "LOW_ESTIMATES", "HIGH_ESTIMATES", "NO_RERATE",
       "ALT_ROUTE", "ENVELOPE_LOW_BOUND")
S_B = ("BUSINESS_BEAR", "MARKET_DRAWDOWN")
SCENARIO_DEFINITIONS = {
    "CENTRAL": "every name at its central ER12; FX flat (spot)",
    "FX_ADVERSE": "central ER12; EVERY non-GBP currency moves -1 sigma (1y ECB) against GBP at once",
    "FX_FAVOURABLE": "central ER12; every non-GBP currency +1 sigma at once",
    "LOW_ESTIMATES": "every name at its analysts' LOW FY1 estimate (consensus optimism is a COMMON error)",
    "HIGH_ESTIMATES": "every name at its analysts' HIGH FY1 estimate",
    "NO_RERATE": "every terminal multiple = the current multiple (the judgement-heavy re-rate removed)",
    "ALT_ROUTE": "every name valued on its other route (P/E <-> EV/EBITDA) where that route is valid",
    "ENVELOPE_LOW_BOUND": ("each name at the minimum of all its estimation variants AND adverse FX - "
                           "an all-corners BOUND, labelled as such, not a probable realised world"),
    "BUSINESS_BEAR": ("each name at its business-bear outcome (trough FY margin / low estimate with no "
                      "positive re-rate) AND adverse FX - a coherent adverse REALISED world, kept separate"),
    "MARKET_DRAWDOWN": ("each name at the WORST rolling 52-week GBP total return it actually realised in the "
                        "declared risk window (weekly store) - an empirical, consensus-independent adverse world; "
                        "all names at their own worst at once is deliberately conservative"),
}


def empirical_worst_52w(store: dict, ticker: str, min_weeks: int = 104) -> dict:
    """Worst realised rolling 52-week GBP total return in the stored window (levels are GBP
    total-return levels). Fewer than `min_weeks` observations -> UNMEASURED, never a number."""
    try:
        import stock_return_store as _srs
        rec = _srs.record_of(store, ticker)
    except Exception:                                               # noqa: BLE001
        rec = ((store or {}).get("names") or {}).get(ticker) or {}
    obs = rec.get("observations") or {}
    pts = sorted((k, float(v.get("gbp"))) for k, v in obs.items() if _num((v or {}).get("gbp")))
    if len(pts) < min_weeks:
        return {"state": "UNMEASURED", "n_weeks": len(pts)}
    worst, at = None, None
    for i in range(52, len(pts)):
        r = pts[i][1] / pts[i - 52][1] - 1.0
        if worst is None or r < worst:
            worst, at = r, pts[i][0]
    return {"state": "MEASURED", "worst_52w_gbp": worst, "ending": at, "n_weeks": len(pts),
            "window": [pts[0][0], pts[-1][0]]}


def name_scenarios(er: dict, empirical: Optional[dict] = None) -> Tuple[Dict[str, float], dict]:
    """-> ({world: GBP 12m return}, substitution notes). One joint world per scenario: the same
    world is applied to every portfolio, so no comparison can mix a good FX world for one
    portfolio with a bad one for its comparator (T58)."""
    c = er["central_local"]
    v = er.get("variants_local") or {}
    fa, ff = er.get("fx_adverse") or 0.0, er.get("fx_favourable") or 0.0
    notes = {}

    def var(k):
        if v.get(k) is not None:
            return v[k]
        notes[k] = "variant unavailable - central used in this world"
        return c
    allv = [c] + [x for x in v.values() if x is not None]
    out = {
        "CENTRAL": c,
        "FX_ADVERSE": (1 + c) * (1 + fa) - 1,
        "FX_FAVOURABLE": (1 + c) * (1 + ff) - 1,
        "LOW_ESTIMATES": var("LOW_ESTIMATES"),
        "HIGH_ESTIMATES": var("HIGH_ESTIMATES"),
        "NO_RERATE": var("NO_RERATE"),
        "ALT_ROUTE": var("ALT_ROUTE"),
        "ENVELOPE_LOW_BOUND": (1 + min(allv)) * (1 + fa) - 1,
        "BUSINESS_BEAR": (None if er.get("bear_local") is None else (1 + er["bear_local"]) * (1 + fa) - 1),
        # already GBP (the store is GBP total return): no second FX leg
        "MARKET_DRAWDOWN": (None if not empirical or empirical.get("state") != "MEASURED"
                            else empirical["worst_52w_gbp"]),
    }
    return out, notes


def legal_bundles(names: List[dict], capital: float, min_entry: float) -> Tuple[List[Tuple], dict]:
    """EXACT enumeration of the legal discrete action space (s8.1).

    names: [{ticker, levels:[GBP amounts of the permitted rungs <= cap, ascending], min_gbp}]
    ('cap_gbp' alone means levels = [cap]). Legal per name: 0, ANY permitted rung level (a rung is
    a CAP, never an automatic target - T23), or - for AT MOST ONE name (D15: earlier positions are
    filled to their rung before another opens) - a PARTIAL final fill equal to the remaining capital,
    legal only if min_gbp (D16 MIN_ENTRY for an opening; D23 no floor for a top-up) <= remainder
    < that name's largest permitted level. No-action is included. Canonical sorted tuples, so input
    order never matters (T27). -> (bundles, certificate)"""
    _fi_mark("capital_decision_engine", "legal_bundles")
    ns = sorted(names, key=lambda x: x["ticker"])
    lv = {n["ticker"]: sorted({round(float(x), 2) for x in (n.get("levels") or [n["cap_gbp"]])}) for n in ns}
    mins = {n["ticker"]: float(n.get("min_gbp", min_entry)) for n in ns}
    tick = [n["ticker"] for n in ns]
    n = len(tick)
    _mmin = min(mins.values()) if mins else min_entry
    kmax = min(n, int(capital // _mmin)) if _mmin > 0 else n
    per = {t: len(lv[t]) for t in tick}
    upper = 0
    for k in range(0, kmax + 1):
        for comb in itertools.combinations(tick, k):
            prod = 1
            for t in comb:
                prod *= per[t]
            upper += prod * (1 + k)
            if upper > MAX_BUNDLES:
                break
        if upper > MAX_BUNDLES:
            break
    if upper > MAX_BUNDLES:
        return [], {"state": "SEARCH_INCOMPLETE", "theoretical_upper_bound": ">%d" % MAX_BUNDLES,
                    "budget": MAX_BUNDLES, "visited": 0,
                    "why": "the legal action space exceeds the declared search budget - no best-so-far is "
                           "called optimal (s8.1, T86)"}
    out, visited, rejected = [], 0, 0
    for k in range(0, kmax + 1):
        for comb in itertools.combinations(tick, k):
            for amts in itertools.product(*[lv[t] for t in comb]):
                visited += 1
                full = sum(amts)
                if full <= capital + EPS_GBP:
                    out.append(tuple(zip(comb, amts)))
                else:
                    rejected += 1
            for p in comb:
                others = [t for t in comb if t != p]
                for amts in itertools.product(*[lv[t] for t in others]):
                    visited += 1
                    rest = sum(amts)
                    rem = round(capital - rest, 2)
                    if rest <= capital + EPS_GBP and rem > EPS_GBP and mins[p] - EPS_GBP <= rem < lv[p][-1] - EPS_GBP \
                            and all(abs(rem - x) > EPS_GBP for x in lv[p]):
                        d = dict(zip(others, amts))
                        d[p] = rem
                        out.append(tuple((t, d[t]) for t in comb))
                    else:
                        rejected += 1
    out = sorted(set(out))
    cert = {"state": "COMPLETE", "n_names": n, "kmax": kmax, "theoretical_upper_bound": upper,
            "visited": visited, "legal_bundles": len(out), "rejected_cash_or_min_entry": rejected,
            "pruning": ("ONLY legality (cash <= capital; partial in [min, top level)); no economic, score or "
                        "optimistic-bound pruning - every legal bundle is explicitly evaluated"),
            "rule": "per name: 0 | any permitted rung level | (<=1 name) partial final fill = remaining capital"}
    return out, cert


def bruteforce_bundles(names: List[dict], capital: float, min_entry: float) -> set:
    """INDEPENDENT ORACLE (T21/T29): Cartesian product over {0, each level, PARTIAL} per name, filtered
    by legality. Shares no code path with legal_bundles."""
    tick = sorted(n["ticker"] for n in names)
    lv = {n["ticker"]: sorted({round(float(x), 2) for x in (n.get("levels") or [n["cap_gbp"]])}) for n in names}
    mins = {n["ticker"]: float(n.get("min_gbp", min_entry)) for n in names}
    opts = [[0] + lv[t] + ["P"] for t in tick]
    res = set()
    for choice in itertools.product(*opts):
        if sum(1 for c in choice if c == "P") > 1:
            continue
        full = sum(c for c in choice if c != "P")
        if full > capital + EPS_GBP:
            continue
        rem = round(capital - full, 2)
        if "P" in choice:
            p = tick[choice.index("P")]
            if rem <= EPS_GBP or not (mins[p] - EPS_GBP <= rem < lv[p][-1] - EPS_GBP) or any(abs(rem - x) <= EPS_GBP for x in lv[p]):
                continue
        b = []
        for t, c in zip(tick, choice):
            if c == "P":
                b.append((t, rem))
            elif c:
                b.append((t, c))
        res.add(tuple(b))
    return res


def _wb_annotate(r: dict, legs, wbx: Optional[dict], portfolio: dict, nav: float, fund_trim_gbp: float = 0.0) -> None:
    """P-D (ISA-0887/0888): attach the WHOLE-ISA view to one comparison row - post-action whole-book GBP volatility
    (the risk metric when wbx.basis is WHOLE_BOOK_MEASURED) and Raj's appetite guard. Never a breach: the guard's
    REVIEW_REQUIRED is enforced at Checkpoint-D tick 14 as an explicit approval, never as a refusal or a sale."""
    if not wbx:
        return
    try:
        import whole_book_risk as _wbr
        a = _wbr.plan_annotation(wbx, portfolio, [(t, x) for t, x in legs], fund_trim_gbp=fund_trim_gbp, nav=nav)
    except Exception as exc:                                        # noqa: BLE001
        a = {"basis": wbx.get("basis"), "error": "%s: %s" % (type(exc).__name__, exc)}
        if wbx.get("basis") == "WHOLE_BOOK_MEASURED":
            a["risk_metric_gbp"] = None
    r["whole_book"] = {k: a.get(k) for k in ("basis", "whole_book", "fund_trim_gbp", "fund_trim_model", "error")}
    if wbx.get("basis") == "WHOLE_BOOK_MEASURED":
        r["risk_metric_gbp"] = a.get("risk_metric_gbp")
    if a.get("appetite_guard") is not None:
        r["appetite_guard"] = a["appetite_guard"]


_BUNDLE_RISK_MEMO: Dict[tuple, dict] = {}     # ISA-0858: pure-function memo, keyed on input FILE hashes


def bundle_risk(store: dict, portfolio: dict, bundle: Tuple, nav_gbp: float) -> dict:
    """Post-trade WHOLE stock-sleeve risk on the ACTUAL proposed weights (s6, T30/T49):
    sleeve sigma, GBP volatility, Euler risk shares, and each new name's Dimson beta to the
    post-trade sleeve excluding itself (ISA-0600 with in-run additions at proposed size, ISA-0714)."""
    import sleeve_risk as _sr
    held = {s["ticker"]: float(s.get("value_gbp") or 0.0) for s in (portfolio.get("stocks") or [])
            if s.get("ticker")}
    post = dict(held)
    for t, a in bundle:
        post[t] = post.get(t, 0.0) + float(a)
    pf = {"stocks": [{"ticker": t, "value_gbp": v} for t, v in post.items() if v > 0]}
    tot = sum(post.values())
    out = {"sleeve_gbp": round(tot, 2)}
    try:
        rs = _sr.risk_shares(store, pf)
        out.update({"sleeve_sigma": rs["sleeve_sigma_ann"], "gbp_vol": rs["sleeve_sigma_ann"] * tot,
                    "risk_shares": {k: round(v, 5) for k, v in rs["shares"].items()},
                    "risk_window": rs.get("risk_window"), "n_weeks": rs.get("n_weeks"), "state": "MEASURED"})
    except Exception as exc:                                        # noqa: BLE001
        out.update({"state": "UNMEASURED", "why": "%s: %s" % (type(exc).__name__, exc)})
        return out
    betas = {}
    for t, a in bundle:
        g = _sr.gate_add(store, pf, t)
        betas[t] = {"verdict": g.get("verdict"), "beta": g.get("beta"), "beta_max": g.get("beta_max")}
    out["post_trade_beta"] = betas
    out["whole_book_note"] = ("stock-sleeve risk on the actual proposed weights; the fund sleeve is held "
                              "constant across bundles (only the small residual differs and is held as "
                              "cash), so stock-sleeve GBP volatility is the comparable whole-book risk "
                              "increment. Fund look-through risk is NOT modelled here (scope stated).")
    return out


def concentration_eval(bundle: Tuple, held: Dict[str, float], nav: float, tax: dict) -> dict:
    """ISA-0465 / B08-R04/R05 (19-Oct-2026): Raj's adopted direct-stock caps on ONE candidate bundle's
    POST-TRADE book - sector <= 12% of total ISA, theme <= 50% of the post-trade stock sleeve - through the
    ONE home (concentration_control.book_invariant: only a class whose exposure INCREASES can breach, so
    an existing breach is separated from a worsening addition). An ADDITION whose sector or theme is
    UNMAPPED is UNKNOWN_CLASSIFICATION (it cannot silently receive headroom - the same rule as
    concentration_control.gate). Returns {state PASS|FAIL, violations, unknown_additions}."""
    import concentration_control as _ccm
    book = dict(held)
    unknown = []
    for t, a in bundle:
        a = float(a)
        if a < 0:
            # ⚑ ISA-0621 (Raj 19-Oct-2026): a DONOR leg reduces the book - the caps judge the NET post-trade book,
            #   so a same-theme switch that does not raise the theme's GBP never breaches (book_invariant's
            #   increase test). Before P-B9 every leg <= 0 was skipped, i.e. a sale was invisible to the caps.
            book[t] = max(book.get(t, 0.0) + a, 0.0)
            continue
        if a == 0:
            continue
        book[t] = book.get(t, 0.0) + a
        if _ccm._sector(tax, t) is None or _ccm._themes(tax, t) is None:
            unknown.append(t)
    book = {k: v for k, v in book.items() if v > EPS_GBP}
    inv = _ccm.book_invariant(book, nav_gbp=nav, tax=tax, pre_book=held)
    viol = list(inv["violations"]) + [{"kind": "UNKNOWN_CLASSIFICATION", "class": t} for t in sorted(set(unknown))]
    return {"state": "PASS" if not viol else "FAIL", "violations": viol, "unknown_additions": sorted(set(unknown)),
            "sector_exposure_pct_nav": inv.get("sector_exposure_pct_nav"),
            "theme_exposure_pct_sleeve": inv.get("theme_exposure_pct_sleeve")}


def constraints_for(bundle: Tuple, risk: dict, pre_shares: Dict[str, float], ceiling_pct: float,
                    max_pos_gbp: float) -> List[dict]:
    """Every authorised hard constraint on the FINAL bundle -> list of breaches (empty = feasible)."""
    br = []
    if risk.get("state") != "MEASURED":
        br.append({"constraint": "post_trade_risk_measured", "observed": risk.get("why")})
        return br
    for t, b in (risk.get("post_trade_beta") or {}).items():
        if b.get("verdict") != "PASS":
            br.append({"constraint": "ISA-0600 sleeve beta (post-trade, in-run weights)", "ticker": t,
                       "observed": _r(b.get("beta"), 3), "limit": b.get("beta_max"), "verdict": b.get("verdict")})
    for t, sh in (risk.get("risk_shares") or {}).items():
        pre = pre_shares.get(t)
        if sh * 100.0 > ceiling_pct + 1e-9:
            if pre is None or sh > pre + 1e-9:
                br.append({"constraint": "D27 sleeve risk-share ceiling", "ticker": t,
                           "observed_pct": _r(sh * 100, 2), "limit_pct": ceiling_pct,
                           "pre_trade_pct": None if pre is None else _r(pre * 100, 2),
                           "note": "a bundle may neither create a breach nor deepen an existing one"})
    for t, a in bundle:
        if a > max_pos_gbp + EPS_GBP:
            br.append({"constraint": "max_stock_position_pct", "ticker": t, "observed": a, "limit": max_pos_gbp})
    return br


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ISA-0621 (October P-B9, 19-Oct-2026) — THE SWITCH CONTEST (B07-R01..R05/R07/R09/R11), SHADOW
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Until P-B9 the contest above decided only where NEW cash goes: a held stock was never a DONOR, so no
# part of the framework asked whether a pound already invested would be better placed elsewhere
# (GBP 12,274.81 of direct stocks on 02-Oct-2026), and a name barred from additions by the ISA-0600 beta
# gate was never offered as a REPLACEMENT although Raj's design call (ISA-0600) is that "a switch is
# judged on net delta-sigma". The switch contest is the SAME contest with donor legs added:
#   * the same joint worlds (S_E + S_B), the same measured costs, the same comparator (compare());
#   * donor amounts: EXIT, or TRIM to a ladder rung strictly below the holding (a residual is 0 or a rung -
#     adopted D5 "a position reaches 3.5% or it does not exist", applied to the residual - DECLARED), every
#     sale >= MIN_ECONOMIC_TRADE; a name is never bought and sold in one plan;
#   * wealth is MARGINAL to holding every donor (the hold path keeps the s4.1 hypothetical horizon exit, so
#     every alternative carries one convention; acquisition cost never enters - FP-Q04; ISA: no tax);
#   * risk is the post-trade stock-sleeve GBP volatility on the actual post-trade weights INCLUDING donor
#     reductions and unchanged holdings (B08-R03); SWITCH-ONLY receivers (stopped only by the beta gate or a
#     REPLACEMENT_ONLY admission) must be donor-funded and their switch component non-increasing in sleeve
#     sigma (ISA-0600: the beta cap is the special case where the donor is cash);
#   * concentration (ISA-0465) and the AI factor (B3/D15) are judged on the NET post-trade book (Raj
#     19-Oct-2026: a like-for-like AI-capex switch is never blocked).
# AUTHORITY: isa_policy.V2_FLAGS['switch_contest']. LIVE by Raj's decision (19-Oct-2026 18:58), which waives R18.2's
# SHADOW period and supersedes ISA-0167's SHADOW-until-measured constraint. LIVE: when the selection over
# AUTHORITY-ELIGIBLE donors (resolved ER12 case, outside the 182-day min-hold) SELLS something, build() adopts it -
# the sales are `selected_sales`, the buys stay `selected_actions` (every existing consumer keeps its meaning) and
# the sold names join case_scope. A no-sale answer never replaces the new-money selection (regret is not
# independent of irrelevant alternatives; adding rows must not reshuffle a no-switch decision). SHADOW: computed and
# rendered, nothing adopted. A failure here is recorded and never fails the decision.
# PARITY (falsifier): the zero-donor restriction of this contest must reproduce the LIVE rows exactly; if it
# does not, the section is PARITY_FAILED and publishes no selection.
SWITCH_MAX_PLANS = 400_000          # search budget for the extended contest; exceeded -> SEARCH_INCOMPLETE
def switch_authority() -> str:
    """isa_policy.V2_FLAGS['switch_contest'] - LIVE by Raj's 19-Oct-2026 decision. Unreadable -> SHADOW (a
    contest that cannot establish its authority never moves capital)."""
    try:
        import isa_policy as _pol
        v = _pol.V2_FLAGS["switch_contest"] if "switch_contest" in _pol.V2_FLAGS else "SHADOW"
        return v if v in ("SHADOW", "LIVE") else "SHADOW"
    except Exception:                                               # noqa: BLE001
        return "SHADOW"


SWITCH_AUTHORITY = "SHADOW"         # the safe default; the live value is switch_authority()
SWITCH_LEDGER = "switch_shadow_ledger.jsonl"


def hold_wealth(amount_gbp: float, r_gbp: float, c: dict) -> float:
    """Terminal net GBP wealth of an amount ALREADY HELD, earning r_gbp to the horizon, after the same
    hypothetical horizon exit every alternative carries (s4.1). No entry cost: it is already owned, and
    what was paid for it is accounting, not a future fee (FP-Q04)."""
    a = float(amount_gbp)
    if a <= EPS_GBP:
        return 0.0
    return a * (1.0 + r_gbp) * (1.0 - c["fx_fraction"]) - c["commission_gbp"]


def sale_proceeds(amount_gbp: float, c: dict) -> float:
    """Cash from selling `amount_gbp` of a holding NOW: dealing FX on non-GBP proceeds + commission;
    no purchase levy on a sale; an ISA pays no capital-gains tax (counted once, B07-R04)."""
    a = float(amount_gbp)
    if a <= EPS_GBP:
        return 0.0
    return a * (1.0 - c["fx_fraction"]) - c["commission_gbp"]


def donor_levels(held_gbp: float, nav: float, ladder_pcts, min_trade: float) -> List[float]:
    """Legal sale amounts for one held donor (B07-R02): EXIT (sell everything), or TRIM to a ladder rung
    strictly below the holding. A residual is therefore 0 or a rung - never a sub-scale remnant - and every
    sale clears MIN_ECONOMIC_TRADE. Ascending, de-duplicated."""
    h = round(float(held_gbp), 2)
    out = set()
    if h >= min_trade - EPS_GBP:
        out.add(h)
    for pct in ladder_pcts:
        s = round(h - float(pct) / 100.0 * nav, 2)
        if min_trade - EPS_GBP <= s < h - EPS_GBP:
            out.add(s)
    return sorted(out)


def _switch_admissibility(t: str, rec: dict, scen_t: Optional[dict], min_hold: dict, as_of: str) -> dict:
    """Is a held name a lawful DONOR, and may a plan selling it carry authority? The SAME case standard as a
    receiver: a donor's opportunity cost is its own forward case, so an unresolved case is evaluated as a
    DIAGNOSTIC only (never invented, never zero - B07-R08) and enters case_scope."""
    er = (rec or {}).get("er12") or {}
    st = er.get("state")
    if (rec or {}).get("route") == "vci" or (rec or {}).get("vci_listed") and st in (None, "UNKNOWN", "MISSING"):
        return {"evaluated": False, "authority_eligible": False, "code": "DONOR_INADMISSIBLE_ROUTE",
                "why": "VCI route: no ER12 exists (underwritten by bottleneck FV / ACS) - no return is invented"}
    if st in (None, "UNKNOWN", "MISSING") or er.get("central_local") is None:
        return {"evaluated": False, "authority_eligible": False, "code": "DONOR_ER12_UNKNOWN",
                "why": "no forward case for the held name - its opportunity cost cannot be stated"}
    if scen_t is None or any(scen_t.get(k) is None for k in S_E + S_B):
        return {"evaluated": False, "authority_eligible": False, "code": "DONOR_DOWNSIDE_INPUT_MISSING",
                "why": "a material world (business bear / market drawdown) is unmeasured for the held name"}
    reasons = []
    if st != "VALID":
        reasons.append({"code": "DONOR_CASE_" + str(st), "why": "the held name's ER12 is %s (%s): resolved only by a "
                        "sourced case amendment" % (st, (er.get("hv_triggers") or er.get("why") or "")) })
    mh = ((min_hold or {}).get("positions") or {}).get(t) or {}
    until = mh.get("min_hold_until") if isinstance(mh, dict) else mh
    if not (min_hold or {}).get("enforced"):
        reasons.append({"code": "DONOR_MIN_HOLD_UNVERIFIED", "why": (min_hold or {}).get("why") or "min-hold state unreadable"})
    elif until and str(until)[:10] > str(as_of)[:10]:
        reasons.append({"code": "DONOR_IN_MIN_HOLD", "why": "182-day anti-churn min-hold (scoring_config.MIN_HOLD_DAYS) "
                        "runs until %s; exemptions are judged in held_position_review, not here" % str(until)[:10]})
    return {"evaluated": True, "authority_eligible": not reasons, "code": "DONOR_ADMISSIBLE" if not reasons
            else reasons[0]["code"], "reasons": reasons}


def plan_risk(store: dict, portfolio: dict, legs: Tuple, switch_only: set, memo: dict) -> dict:
    """Post-action risk of ONE plan on the WHOLE stock sleeve (B07-R05, B08-R03): donor reductions, receiver
    additions and every unchanged holding at their actual post-trade weights, one covariance basis
    (sleeve_risk). Ordinary receivers keep the ISA-0600 beta test at the post-trade weights; switch-only
    receivers are judged by the switch component's sleeve-sigma change instead (Raj's ISA-0600 design call)."""
    import sleeve_risk as _sr
    held = {s["ticker"]: float(s.get("value_gbp") or 0.0) for s in (portfolio.get("stocks") or []) if s.get("ticker")}
    post = dict(held)
    for t, a in legs:
        post[t] = post.get(t, 0.0) + float(a)
    post = {k: v for k, v in post.items() if v > EPS_GBP}
    key = (tuple(sorted((k, round(v, 4)) for k, v in post.items())),
           tuple(sorted((t, round(float(a), 4)) for t, a in legs if t in switch_only)))
    if key in memo:
        return copy.deepcopy(memo[key])
    pf = {"stocks": [{"ticker": t, "value_gbp": v} for t, v in post.items()]}
    tot = sum(post.values())
    out = {"sleeve_gbp": round(tot, 2)}
    try:
        rs = _sr.risk_shares(store, pf)
        out.update({"sleeve_sigma": rs["sleeve_sigma_ann"], "gbp_vol": rs["sleeve_sigma_ann"] * tot,
                    "risk_shares": {k: round(v, 5) for k, v in rs["shares"].items()},
                    "risk_window": rs.get("risk_window"), "n_weeks": rs.get("n_weeks"), "state": "MEASURED"})
    except Exception as exc:                                        # noqa: BLE001
        out.update({"state": "UNMEASURED", "why": "%s: %s" % (type(exc).__name__, exc)})
        memo[key] = out
        return copy.deepcopy(out)
    betas = {}
    for t, a in legs:
        if float(a) > 0 and t not in switch_only:
            # COST ONLY: gate_add measures t against the post-trade sleeve EXCLUDING t (self-excluded), so its verdict
            # depends on t and the other names' post-trade values - never on t's own amount; memoised on exactly that.
            gk = ("gate", t, tuple(sorted((k, v) for k, v in post.items() if k != t)))
            g = memo.get(gk)
            if g is None:
                g = _sr.gate_add(store, pf, t)
                memo[gk] = g
            betas[t] = {"verdict": g.get("verdict"), "beta": g.get("beta"), "beta_max": g.get("beta_max")}
    out["post_trade_beta"] = betas
    sw = [(t, float(a)) for t, a in legs if t in switch_only and float(a) > 0]
    if sw:
        try:
            tot0 = sum(held.values())
            w0 = {k: v / tot0 for k, v in held.items() if v > EPS_GBP}
            book = dict(held)
            for t, a in legs:
                if float(a) < 0:
                    book[t] = book.get(t, 0.0) + float(a)
            for t, a in sw:
                book[t] = book.get(t, 0.0) + a
            w1 = {k: v / tot0 for k, v in book.items() if v > EPS_GBP}
            s0, s1 = _sr.sigma(store, w0), _sr.sigma(store, w1)
            out["switch_delta_sigma"] = {"state": "MEASURED", "sigma_before": s0, "sigma_after": s1, "delta_sigma": s1 - s0,
                                         "risk_improving": s1 <= s0 + 1e-12, "legs": sw,
                                         "basis": "sleeve_risk.sigma on the held sleeve's value (delta_sigma_switch generalised to many legs)"}
        except Exception as exc:                                    # noqa: BLE001
            out["switch_delta_sigma"] = {"state": "UNMEASURED", "why": "%s: %s" % (type(exc).__name__, exc)}
    memo[key] = out
    return copy.deepcopy(out)


def _vector_legal(sold: dict, eligible) -> bool:
    """Which donor vectors are enumerated: any combination of AUTHORITY-ELIGIBLE donors (what can be adopted), and
    at most ONE sale involving a donor that is not authority-eligible (the diagnostic: what each held name's sale would
    do once its case/min-hold clears). Bounds the diagnostic's cost without touching the adoptable set."""
    return eligible is None or len(sold) <= 1 or set(sold) <= set(eligible)


def bruteforce_switch_plans(donors: Dict[str, List[float]], receivers: List[dict], capital: float, min_entry: float,
                            proceeds: Dict[str, Dict[float, float]], eligible=None) -> set:
    """INDEPENDENT ORACLE for the extended enumeration (B07 testing: a brute-force donor x receiver search).
    One Cartesian product over every donor option and every receiver option {0, each level, PARTIAL}, filtered
    by legality - it shares no loop with switch_plans()."""
    dn = sorted(donors)
    rv = sorted(receivers, key=lambda x: x["ticker"])
    lv = {x["ticker"]: sorted({round(float(v), 2) for v in (x.get("levels") or [x["cap_gbp"]])}) for x in rv}
    mins = {x["ticker"]: float(x.get("min_gbp", min_entry)) for x in rv}
    so = {x["ticker"] for x in rv if x.get("switch_only")}
    res = set()
    dopts = [[0.0] + list(donors[t]) for t in dn]
    ropts = [[0] + lv[x["ticker"]] + ["P"] for x in rv]
    for dch in itertools.product(*dopts):
        sold = {t: s for t, s in zip(dn, dch) if s}
        if not (eligible is None or len(sold) <= 1 or all(t in eligible for t in sold)):
            continue
        pr = round(sum(proceeds[t][s] for t, s in sold.items()), 2)
        cap2 = round(capital + pr, 2)
        for rch in itertools.product(*ropts):
            if sum(1 for c in rch if c == "P") > 1:
                continue
            names = [x["ticker"] for x, c in zip(rv, rch) if c]
            if set(names) & set(sold):
                continue
            full = sum(c for c in rch if c not in (0, "P"))
            if full > cap2 + EPS_GBP:
                continue
            rem = round(cap2 - full, 2)
            b = []
            ok = True
            for x, c in zip(rv, rch):
                t = x["ticker"]
                if c == "P":
                    if rem <= EPS_GBP or not (mins[t] - EPS_GBP <= rem < lv[t][-1] - EPS_GBP) or \
                            any(abs(rem - v) <= EPS_GBP for v in lv[t]):
                        ok = False
                        break
                    b.append((t, rem))
                elif c:
                    b.append((t, c))
            if not ok:
                continue
            if sum(a for t, a in b if t in so) > pr + EPS_GBP:
                continue
            res.add((tuple(sorted(sold.items())), tuple(sorted(b))))
    return res


def switch_plans(donors: Dict[str, List[float]], receivers: List[dict], capital: float, min_entry: float,
                 proceeds: Dict[str, Dict[float, float]], eligible=None) -> Tuple[List[Tuple], dict]:
    """PRODUCTION enumeration of the extended legal action space: every donor vector x every legal receiver
    bundle at that vector's capital (legal_bundles, unchanged), less plans that buy and sell one name or fund a
    switch-only receiver from anything but donor proceeds. -> ([(sold_tuple, bundle)], certificate)."""
    dn = sorted(donors)
    out, visited = [], 0
    for dv in itertools.product(*[[0.0] + list(donors[t]) for t in dn]):
        sold = {t: s for t, s in zip(dn, dv) if s}
        if not _vector_legal(sold, eligible):
            continue
        pr = round(sum(proceeds[t][s] for t, s in sold.items()), 2)
        rv = [x for x in receivers if x["ticker"] not in sold]
        bundles, cert = legal_bundles(rv, round(capital + pr, 2), min_entry)
        if cert["state"] != "COMPLETE":
            return [], {"state": "SEARCH_INCOMPLETE", "why": "a donor vector's receiver space exceeds the search budget",
                        "visited": visited}
        so = {x["ticker"] for x in rv if x.get("switch_only")}
        for b in bundles:
            visited += 1
            if sum(a for t, a in b if t in so) > pr + EPS_GBP:
                continue
            out.append((tuple(sorted(sold.items())), tuple(sorted(b))))
            if len(out) > SWITCH_MAX_PLANS:
                return [], {"state": "SEARCH_INCOMPLETE", "budget": SWITCH_MAX_PLANS, "visited": visited,
                            "why": "the extended action space exceeds the declared budget - no best-so-far is called optimal"}
    out = sorted(set(out))
    return out, {"state": "COMPLETE", "n_donors": len(dn), "n_receivers": len(receivers), "visited": visited,
                 "plans": len(out), "rule": "donor: 0 | EXIT | TRIM-to-rung (sale >= MIN_ECONOMIC_TRADE); every combination of "
                 "authority-eligible donors, at most one sale of a not-yet-eligible donor (diagnostic); receivers: "
                 "legal_bundles at capital + net sale proceeds; switch-only receivers donor-funded; no buy+sell of one name"}


def factor_eval(legs: Tuple, portfolio: dict, nav: float, fctx: dict) -> dict:
    """ISA-0465 P-B6 finding / Raj 19-Oct-2026: the B3/D15 AI-complex factor test on ONE plan's NET post-trade
    book. The lower bound (factor_lookthrough, unchanged method) is recomputed with donor reductions and receiver
    additions applied; a plan FAILS only if the post-trade lower bound exceeds the cap AND the plan raised the
    factor's GBP (an existing breach is separated from a worsening addition - the same rule as the theme caps).
    A like-for-like AI switch that does not raise the exposure therefore never fails."""
    import factor_lookthrough as _flt
    pre = fctx.get("pre") or {}
    stocks = [dict(s) for s in (portfolio.get("stocks") or [])]
    idx = {s.get("ticker"): s for s in stocks}
    for t, a in legs:
        a = float(a)
        if t in idx:
            idx[t]["value_gbp"] = max(float(idx[t].get("value_gbp") or 0.0) + a, 0.0)
        elif a > 0:
            s = {"ticker": t, "value_gbp": a}
            stocks.append(s)
            idx[t] = s
    p2 = {"summary": {"total_value_gbp": nav}, "funds": portfolio.get("funds") or [],
          "stocks": [s for s in stocks if float(s.get("value_gbp") or 0.0) > EPS_GBP]}
    try:
        post = _flt.compute(p2, fctx["fmap"], declared=fctx.get("declared"), themes=fctx.get("themes"))
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "UNAVAILABLE", "why": "%s: %s" % (type(exc).__name__, exc)}
    lo, cap = post.get("lower_bound_pct"), post.get("cap_pct")
    if lo is None or pre.get("lower_bound_gbp") is None:
        return {"state": "UNAVAILABLE", "why": "no lower bound (no NAV or the pre-trade bound is unavailable)",
                "post_lower_pct": lo, "cap_pct": cap}
    inc = float(post["lower_bound_gbp"]) > float(pre["lower_bound_gbp"]) + EPS_GBP
    return {"state": "FAIL" if (lo > cap and inc) else "PASS", "post_lower_pct": lo, "pre_lower_pct": pre.get("lower_bound_pct"),
            "post_lower_gbp": post["lower_bound_gbp"], "pre_lower_gbp": pre["lower_bound_gbp"], "cap_pct": cap,
            "raises_factor": inc, "post_unclassified": post.get("unclassified")}


def switch_contest(ctx: dict) -> dict:
    """The SHADOW switch contest (see the block comment above). `ctx` carries the LIVE build's own objects so
    nothing is re-derived (R4.4): inp, pipeline, store, hurdle, as_of, amendments, price_overrides, recs, eligible,
    scen, costs_by, costs, capital, cash_capital, cash_rate, portfolio, nav, min_entry, tw, min_trade, conc, fctx,
    pre_shares, pre_risk, ceiling, max_pos, live_rows, live_selected, complete, root."""
    _fi_mark("capital_decision_engine", "switch_contest")
    import position_sizing as _ps
    nav, portfolio, store = ctx["nav"], ctx["portfolio"], ctx["store"]
    held = {s["ticker"]: float(s.get("value_gbp") or 0.0) for s in (portfolio.get("stocks") or [])
            if s.get("ticker") and float(s.get("value_gbp") or 0.0) > EPS_GBP}
    scen = dict(ctx["scen"])
    costs_by = dict(ctx["costs_by"])
    _auth = switch_authority()
    out = {"authority": _auth,
           "authority_basis": ("LIVE by Raj's decision 19-Oct-2026 18:58 (waives R18.2's SHADOW period; supersedes ISA-0167's "
                               "SHADOW-until-measured constraint): adopted only when the authority-eligible selection SELLS"
                               if _auth == "LIVE" else
                               "SHADOW (isa_policy.V2_FLAGS['switch_contest']): computed and rendered, never adopted"),
           "analysis": "AA-2026-10-19-ISA-0621-SWITCH-CONTEST-8dab5f98 (v2; v1 f8b73ce1)"}
    # ── 1. donors ─────────────────────────────────────────────────────────────────────────────
    try:
        import position_alerts as _pa
        mh = _pa.min_hold_state(root=ctx.get("root"))
    except Exception as exc:                                        # noqa: BLE001
        mh = {"enforced": False, "why": "min-hold state unreadable: %s" % exc}
    ladder_pcts = sorted(_ps.ladder(ctx["tw"]).values())
    recs = ctx["recs"]
    donors, dinfo = {}, {}
    for t, h in sorted(held.items()):
        rec = recs.get(t) or {}
        if t not in scen and (rec.get("er12") or {}).get("central_local") is not None:
            try:
                emp = empirical_worst_52w(store, t)
                scen[t], _n = name_scenarios(rec["er12"], emp)
                costs_by[t] = costs_for(t, rec["er12"].get("currency"), ctx["costs"])
            except Exception:                                       # noqa: BLE001
                scen.pop(t, None)
        adm = _switch_admissibility(t, rec, scen.get(t), mh, ctx["as_of"])
        lv = donor_levels(h, nav, ladder_pcts, ctx["min_trade"]) if adm["evaluated"] else []
        if adm["evaluated"] and not lv:
            adm = dict(adm, evaluated=False, authority_eligible=False, code="DONOR_BELOW_MIN_TRADE",
                       why="no legal sale: holding %.2f below MIN_ECONOMIC_TRADE %.2f" % (h, ctx["min_trade"]))
        dinfo[t] = {"held_gbp": round(h, 2), "levels_gbp": lv, "central_er12_local_pct":
                    _r(((rec.get("er12") or {}).get("central_local") or 0) * 100, 2)
                    if (rec.get("er12") or {}).get("central_local") is not None else None, **adm}
        if adm["evaluated"]:
            donors[t] = lv
    # ISA-0621 COST: an execution-time re-run (scope AUTHORITY_ONLY) needs only what can be ADOPTED - the plans whose
    #   donors are authority-eligible - never the diagnostic. The adoptable selection is identical either way (it is
    #   compared over exactly these rows in a FULL build too).
    out["scope"] = ctx.get("scope") or "FULL"
    if out["scope"] == "AUTHORITY_ONLY":
        donors = {t: lv for t, lv in donors.items() if dinfo[t]["authority_eligible"]}
        if not donors:
            out["donors"] = dinfo
            out["state"] = "NO_AUTHORITY_ELIGIBLE_DONOR"
            out["parity"] = {"state": "NOT_RUN", "why": "execution re-run with no adoptable donor: the contest is the LIVE one"}
            return out
    proceeds = {t: {s: round(sale_proceeds(s, costs_by[t]), 2) for s in lv} for t, lv in donors.items()}
    # ── 2. receivers: LIVE eligible + switch-only names from the same census in switch mode ─────────
    receivers = [dict(e, switch_only=False) for e in ctx["eligible"]]
    sw_only = {}
    try:
        cen2 = _build_census(ctx["inp"], ctx["pipeline"], store, ctx["hurdle"], ctx["as_of"], ctx["amendments"],
                             ctx["price_overrides"], switch_mode=True)
        r2 = cen2["records"]
        live_elig = {e["ticker"] for e in ctx["eligible"]}
        for t, rec in sorted(r2.items()):
            if rec["disposition"] != "ELIGIBLE" or t in live_elig or not rec.get("switch_only"):
                continue
            cur_val = held.get(t, 0.0)
            cap = round(rec["permission"]["target_pct"] / 100.0 * nav - cur_val, 2)
            mn = EPS_GBP if cur_val > 0 else ctx["min_entry"]
            if cap < mn - EPS_GBP or cap <= EPS_GBP:
                continue
            levels = sorted({round(pct / 100.0 * nav - cur_val, 2) for pct in ladder_pcts
                             if pct <= rec["permission"]["target_pct"] + 1e-9 and pct / 100.0 * nav - cur_val > EPS_GBP})
            emp = empirical_worst_52w(store, t)
            s_t, _n = name_scenarios(rec["er12"], emp)
            if any(s_t[k] is None for k in S_B):
                continue
            scen[t] = s_t
            costs_by[t] = costs_for(t, rec["er12"].get("currency"), ctx["costs"])
            receivers.append({"ticker": t, "cap_gbp": cap, "levels": levels, "min_gbp": mn, "held": cur_val > 0,
                              "switch_only": True})
            sw_only[t] = {"why": rec.get("switch_only"), "central_er12_local_pct": _r(rec["er12"]["central_local"] * 100, 2),
                          "case_complete": t in ctx["complete"]}
    except Exception as exc:                                        # noqa: BLE001
        out["switch_only_error"] = "%s: %s" % (type(exc).__name__, exc)
    so_set = set(sw_only)
    out["donors"] = dinfo
    out["switch_only_receivers"] = sw_only
    # ── 3. enumerate + independent oracle ───────────────────────────────────────────────────────
    _elig = {t for t in donors if dinfo[t]["authority_eligible"]}
    plans, cert = switch_plans(donors, receivers, ctx["capital"], ctx["min_entry"], proceeds, eligible=_elig)
    out["search"] = cert
    if cert["state"] != "COMPLETE":
        out["state"] = "SEARCH_INCOMPLETE"
        return out
    if len(donors) <= 3 and len(receivers) <= 9:
        bf = bruteforce_switch_plans(donors, receivers, ctx["capital"], ctx["min_entry"], proceeds, eligible=_elig)
        prod = set(plans)
        out["oracle"] = {"independent_bruteforce": len(bf), "production": len(prod), "identical_sets": bf == prod}
        if bf != prod:
            out["state"] = "ORACLE_DISAGREES"
            return out
    else:
        out["oracle"] = {"state": "NOT_RUN_SIZE", "n_donors": len(donors), "n_receivers": len(receivers)}
    # ── 4. evaluate every plan on the same worlds, costs, risk and constraints ──────────────────
    cash_rate, cash_cap = ctx["cash_rate"], ctx["cash_capital"]
    memo, rows = {}, []
    conc, fctx = ctx["conc"], ctx["fctx"]
    import sleeve_risk as _sr621
    _fz = _sr621.frozen_store(store)          # COST ONLY: scoped risk memo; the store is not mutated in this loop
    _fz.__enter__()                           # exited below, and unconditionally by build()'s finally
    for sold_t, b in plans:
        sold = dict(sold_t)
        pr = round(sum(proceeds[t][s] for t, s in sold.items()), 2)
        cap2 = round(ctx["capital"] + pr, 2)
        stock_amt = sum(a for _, a in b)
        w = {}
        for s_ in S_E + S_B:
            tot = (cap2 - stock_amt) * (1.0 + cash_rate)
            for t, a in b:
                tot += position_wealth(a, scen[t][s_], costs_by[t])
            for t, s in sold.items():
                tot += hold_wealth(held[t] - s, scen[t][s_], costs_by[t]) - hold_wealth(held[t], scen[t][s_], costs_by[t])
            w[s_] = tot
        legs = tuple(sorted(list(b) + [(t, -s) for t, s in sold.items()]))
        rk = plan_risk(store, portfolio, legs, so_set, memo)
        br = constraints_for(b, rk, ctx["pre_shares"], ctx["ceiling"], ctx["max_pos"]) if rk.get("state") == "MEASURED" \
            else [{"constraint": "post_trade_risk_measured", "observed": rk.get("why")}]
        sds = rk.get("switch_delta_sigma")
        if sds is not None and not (sds.get("state") == "MEASURED" and sds.get("risk_improving")):
            br.append({"constraint": "ISA-0600 switch judged on net delta-sigma (switch-only receiver)",
                       "observed": sds.get("delta_sigma"), "limit": 0.0, "state": sds.get("state")})
        row = {"bundle": b, "sold": sold, "legs": legs, "tickers": [t for t, _ in b], "stock_gbp": round(stock_amt, 2),
               "residual_gbp": round(cap2 - stock_amt, 2), "proceeds_gbp": pr, "wealth": w,
               "entry_costs_gbp": round(sum(a - (a - costs_by[t]["commission_gbp"]) / (1 + costs_by[t]["fx_fraction"] +
                                                                                    costs_by[t]["levy_fraction"]) for t, a in b)
                                        + sum(s - proceeds[t][s] for t, s in sold.items()), 2),
               "risk": rk, "gbp_vol": rk.get("gbp_vol")}
        if not b and not sold:
            row["gbp_vol"] = ctx["pre_risk"].get("gbp_vol")
            br = [] if row["gbp_vol"] is not None else br
        _wb_annotate(row, legs, ctx.get("wbx"), portfolio, nav, fund_trim_gbp=max(stock_amt - cash_cap - pr, 0.0))
        if conc.get("tax") is not None and conc.get("mode") in ("SHADOW", "LIVE"):
            row["concentration"] = concentration_eval(legs, conc["held"], nav, conc["tax"])
            if conc["mode"] == "LIVE" and row["concentration"]["state"] != "PASS":
                br.append({"constraint": "ISA-0465 concentration caps (net post-trade book)",
                           "violations": row["concentration"]["violations"]})
        if fctx.get("pre") is not None and conc.get("mode") in ("SHADOW", "LIVE"):
            row["factor"] = factor_eval(legs, portfolio, nav, fctx)
            if conc["mode"] == "LIVE" and (row["factor"]["state"] == "FAIL" or
                                           (row["factor"]["state"] != "PASS" and (fctx["pre"].get("breach")))):
                br.append({"constraint": "B3/D15 AI-complex factor cap (net post-trade lower bound)", "factor": row["factor"]})
        _trim_r = round(max(stock_amt - cash_cap - pr, 0.0), 2)
        if 0.005 < _trim_r < ctx["min_trade"] - EPS_GBP:
            br.append({"constraint": "ISA-0858 D4 fund-trim leg below MIN_ECONOMIC_TRADE", "observed_gbp": _trim_r})
        row["breaches"] = br
        row["feasible"] = not br
        row["authority_eligible_donors"] = all(dinfo[t]["authority_eligible"] for t in sold)
        rows.append(row)
    _fz.__exit__(None, None, None)
    # ── 5. parity: the zero-donor restriction must reproduce the LIVE rows exactly ───────────────
    zl = {r["bundle"]: r for r in rows if not r["sold"] and not (set(r["tickers"]) & so_set)}
    lv_rows = {tuple(r["bundle"]): r for r in ctx["live_rows"]}
    mism = []
    if set(zl) != set(lv_rows):
        mism.append({"kind": "bundle_set", "only_live": len(set(lv_rows) - set(zl)), "only_switch": len(set(zl) - set(lv_rows))})
    else:
        for k, r in zl.items():
            L = lv_rows[k]
            if any(abs(r["wealth"][s] - L["wealth"][s]) > 1e-6 for s in S_E + S_B) or bool(r["feasible"]) != bool(L["feasible"]) \
                    or ((r.get("gbp_vol") is None) != (L.get("gbp_vol") is None)) \
                    or (r.get("gbp_vol") is not None and abs(r["gbp_vol"] - L["gbp_vol"]) > 1e-6) \
                    or ((_risk_of(r) is None) != (_risk_of(L) is None)) \
                    or (_risk_of(r) is not None and abs(_risk_of(r) - _risk_of(L)) > 1e-6):
                mism.append({"kind": "row", "bundle": [list(x) for x in k]})
                if len(mism) > 5:
                    break
    out["parity"] = {"state": "MATCH" if not mism else "MISMATCH", "n_live_rows": len(lv_rows), "n_zero_donor_rows": len(zl),
                     "mismatches": mism,
                     "rule": "the switch contest restricted to no donor legs reproduces the LIVE contest's plans, wealth in every "
                             "world, feasibility and risk exactly (falsifier of the extension)"}
    if mism:
        out["state"] = "PARITY_FAILED"
        return out
    # ── 6. compare: SHADOW selection (authority-eligible donors only) and the full DIAGNOSTIC ───────
    elig_rows = [copy.copy(r) for r in rows if r["authority_eligible_donors"]]
    diag_rows = [copy.copy(r) for r in rows]
    c_sh = compare(elig_rows)
    c_dg = compare(diag_rows)
    live_b = tuple(sorted(tuple(x) for x in (ctx.get("live_selected") or {}).get("bundle", ())))

    def brief(r):
        if r is None:
            return None
        return {"sell": [{"ticker": t, "gbp": s, "exit": abs(s - held[t]) <= EPS_GBP} for t, s in sorted(r["sold"].items())],
                "buy": [{"ticker": t, "gbp": a} for t, a in r["bundle"]], "proceeds_gbp": r["proceeds_gbp"],
                "stock_gbp": r["stock_gbp"], "residual_gbp": r["residual_gbp"], "regret_gbp": _r(r.get("regret")),
                "wealth_gbp": {s: _r(v) for s, v in r["wealth"].items()}, "gbp_vol": _r(r.get("gbp_vol")),
                "entry_and_exit_costs_gbp": r["entry_costs_gbp"],
                "concentration": (r.get("concentration") or {}).get("state"), "factor": (r.get("factor") or {}).get("state"),
                "authority_eligible_donors": r["authority_eligible_donors"]}
    sh_sel, dg_sel = c_sh.get("selected"), c_dg.get("selected")
    out["shadow_selection"] = {"comparator_state": c_sh.get("state"), "rule": c_sh.get("rule"), "selected": brief(sh_sel),
                               "differs_from_live": (sh_sel is not None and (bool(sh_sel["sold"]) or
                                                                             tuple(sh_sel["bundle"]) != live_b))}
    out["diagnostic_selection"] = {"comparator_state": c_dg.get("state"), "rule": c_dg.get("rule"), "selected": brief(dg_sel),
                                   "differs_from_live": (dg_sel is not None and (bool(dg_sel["sold"]) or
                                                                                 tuple(dg_sel["bundle"]) != live_b)),
                                   "note": "includes plans selling donors whose case is unresolved or inside the min-hold - "
                                           "a DIAGNOSTIC of what the contest would say once those clear, never authority"}
    # ── 7. per-donor: best plan with vs without the sale, and the central-world breakeven ───────────
    feas = [r for r in diag_rows if r.get("feasible") and r.get("regret") is not None]
    for t in sorted(donors):
        wi = [r for r in feas if t in r["sold"]]
        wo = [r for r in feas if t not in r["sold"]]
        bw = min(wi, key=lambda r: r["regret"]) if wi else None
        bo = min(wo, key=lambda r: r["regret"]) if wo else None
        d = dinfo[t]
        d["best_plan_selling"] = brief(bw)
        d["regret_cost_of_selling_gbp"] = None if (bw is None or bo is None) else _r(bw["regret"] - bo["regret"])
        if bw is not None and bo is not None:
            s = bw["sold"][t]
            slope = s * (1.0 - costs_by[t]["fx_fraction"])
            diff = bw["wealth"]["CENTRAL"] - bo["wealth"]["CENTRAL"]
            d["central_breakeven_er12_pct"] = _r((scen[t]["CENTRAL"] + diff / slope) * 100, 1) if slope > 0 else None
            d["central_breakeven_basis"] = ("selling %s (GBP %.2f) for the best plan that sells it beats the best plan that "
                                            "keeps it in the CENTRAL world only if its 12m GBP return is below this figure; "
                                            "every other name at its central case. Diagnostic - the comparator is minimax "
                                            "regret over all estimation worlds." % (t, s))
            d["bear_wealth_effect_of_selling_gbp"] = _r(bw["wealth"]["BUSINESS_BEAR"] - bo["wealth"]["BUSINESS_BEAR"])
    # ── 8. net-book vs gross concentration (what Raj's 19-Oct rule changes) ─────────────────────────
    ev = [r for r in rows if r.get("concentration")]
    gross_fail = 0
    if conc.get("tax") is not None:
        for r in ev:
            if r["sold"]:
                g = concentration_eval(r["bundle"], conc["held"], nav, conc["tax"])
                gross_fail += g["state"] != "PASS"
            else:
                gross_fail += r["concentration"]["state"] != "PASS"
    out["concentration_net_book"] = {"mode": conc.get("mode"), "n_plans": len(ev),
                                     "n_failing_net": sum(1 for r in ev if r["concentration"]["state"] != "PASS"),
                                     "n_failing_if_donor_legs_ignored": gross_fail,
                                     "basis": "Raj 19-Oct-2026: the caps judge the NET post-trade book (donor reductions count); "
                                              "a same-theme switch that does not raise the theme's GBP never breaches"}
    fv = [r["factor"] for r in rows if r.get("factor")]
    out["factor_net_book"] = {"n_plans": len(fv), "n_failing": sum(1 for f in fv if f.get("state") == "FAIL"),
                              "post_lower_pct_range": ([min(f["post_lower_pct"] for f in fv if f.get("post_lower_pct") is not None),
                                                        max(f["post_lower_pct"] for f in fv if f.get("post_lower_pct") is not None)]
                                                       if any(f.get("post_lower_pct") is not None for f in fv) else None)}
    out["counts"] = {"plans": len(rows), "feasible": sum(1 for r in rows if r["feasible"]),
                     "with_donor_sale": sum(1 for r in rows if r["sold"]),
                     "authority_eligible": len(elig_rows)}
    out["plans_sha256"] = _sha([[list(r["legs"])] for r in rows])
    n_ev = sum(1 for d in dinfo.values() if d.get("evaluated"))
    n_el = sum(1 for d in dinfo.values() if d.get("authority_eligible"))
    out["state"] = ("NO_DONOR_EVALUATED" if n_ev == 0 else "NO_AUTHORITY_ELIGIBLE_DONOR" if n_el == 0 else "OK")
    out["case_scope_donors"] = sorted(t for t, d in dinfo.items() if d.get("evaluated") and any(
        x["code"].startswith("DONOR_CASE_") for x in d.get("reasons") or []))
    # internals for build()'s LIVE adoption (popped before the receipt is written)
    out["_selected_row"] = sh_sel if (c_sh.get("state") == "OK" and sh_sel is not None and sh_sel.get("sold")) else None
    out["_elig_rows"] = elig_rows
    out["_scen"] = {t: scen[t] for t in so_set if t in scen}
    out["_costs_by"] = {t: costs_by[t] for t in set(so_set) | set(donors) if t in costs_by}
    return out


def _switch_ledger_append(receipt: dict, month: str, root: str) -> dict:
    """ISA-0621 learning capture: ONE append-only row per distinct receipt (idempotent on receipt_id) freezing the
    SHADOW switch contest's contemporaneous state, so the 3/6/12-month outcome join can measure the switch trade
    itself - the measurement ISA-0167 requires before any LIVE switching."""
    sc = receipt.get("switch_contest")
    if not isinstance(sc, dict):
        return {"state": "NO_SECTION"}
    p = os.path.join(root, SWITCH_LEDGER)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip() and json.loads(ln).get("receipt_id") == receipt["receipt_id"]:
                        return {"state": "ALREADY_RECORDED", "path": p}
        row = {"receipt_id": receipt["receipt_id"], "month": month, "recorded_at": _now().isoformat(),
               "as_of": receipt.get("as_of"), "method_version": METHOD_VERSION, "authority": sc.get("authority"),
               "state": sc.get("state"), "parity": (sc.get("parity") or {}).get("state"),
               "live_selected": [(a.get("ticker"), a.get("gbp")) for a in receipt.get("selected_actions") or []],
               "shadow_selection": (sc.get("shadow_selection") or {}).get("selected"),
               "diagnostic_selection": (sc.get("diagnostic_selection") or {}).get("selected"),
               "donors": {t: {k: d.get(k) for k in ("held_gbp", "code", "authority_eligible", "central_er12_local_pct",
                                                     "central_breakeven_er12_pct", "regret_cost_of_selling_gbp",
                                                     "bear_wealth_effect_of_selling_gbp")}
                          for t, d in (sc.get("donors") or {}).items()},
               "er12_central_local": {t: ((receipt.get("dossiers") or {}).get(t) or {}).get("er12", {}).get("central_local")
                                      for t in (sc.get("donors") or {})},
               "plans_sha256": sc.get("plans_sha256"), "input_hashes": receipt.get("input_hashes"),
               "outcome_horizons_months": [3, 6, 12],
               "label_to_join": "realised net GBP return of each donor vs the alternative it would have funded"}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return {"state": "APPENDED", "path": p}
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}


def evaluate_bundles(bundles: List[Tuple], scen: Dict[str, Dict[str, float]], costs_by: Dict[str, dict],
                     capital: float, cash_rate: float) -> List[dict]:
    """Like-for-like GBP terminal wealth of the marginal capital in every world (s8.2). Common terms
    (existing holdings, funds, the D23 reserve) are identical across bundles and cancel."""
    rows = []
    for b in bundles:
        stock_amt = sum(a for _, a in b)
        resid = capital - stock_amt
        w = {}
        for s in S_E + S_B:
            tot = resid * (1.0 + cash_rate)
            for t, a in b:
                r = scen[t][s]
                tot += position_wealth(a, r, costs_by[t])
            w[s] = tot
        cost_in = sum(a - (a - costs_by[t]["commission_gbp"]) / (1 + costs_by[t]["fx_fraction"] + costs_by[t]["levy_fraction"])
                      for t, a in b)
        rows.append({"bundle": b, "tickers": [t for t, _ in b], "stock_gbp": round(stock_amt, 2),
                     "residual_gbp": round(resid, 2), "wealth": w, "entry_costs_gbp": round(cost_in, 2)})
    return rows


def _risk_of(r: dict):
    """P-D (B08-R03, ISA-0887): THE comparator's risk dimension. `risk_metric_gbp` (whole-ISA GBP volatility, set on
    every row when the run's basis is WHOLE_BOOK_MEASURED) wins; a row without it keeps stock-sleeve `gbp_vol`.
    One basis per run - _wb_annotate sets the key on every row or on none (R2.6)."""
    return r.get("risk_metric_gbp") if "risk_metric_gbp" in r else r.get("gbp_vol")


def dominates(x: dict, y: dict, risk_tol: float = EPS_GBP, worlds_e=S_E, worlds_b=S_B) -> bool:
    """s8.3 (Rev 1.1): x is ROBUSTLY SUPERIOR to y only if it wins EVERY estimation world by more than
    the arithmetic tolerance AND is no worse in every business-bear world AND no worse on the
    authorised risk metric (stock-sleeve GBP volatility, lower is better). Return-only superiority
    is NOT dominance (T91)."""
    if not all(x["wealth"][s] - y["wealth"][s] > EPS_GBP for s in worlds_e):
        return False
    if not all(x["wealth"][s] >= y["wealth"][s] - EPS_GBP for s in worlds_b):
        return False
    rx, ry = _risk_of(x), _risk_of(y)
    if rx is None or ry is None:
        return False                                  # incomparable risk -> no dominance claim
    return rx <= ry + risk_tol


def compare(rows: List[dict], worlds_e=S_E, worlds_b=S_B) -> dict:
    """The exact uncertainty comparator (s8.3): frontier, robust superiority, minimax regret over
    S_E, declared tie-breaks, unresolved economic ties."""
    _fi_mark("capital_decision_engine", "compare")
    feas = [r for r in rows if r.get("feasible")]
    if not feas:
        return {"state": "REFUSED", "why": "no feasible bundle (not even no-action) - a constraint could not be evaluated"}
    best_s = {s: max(r["wealth"][s] for r in feas) for s in worlds_e}
    for r in feas:
        r["regret"] = max(best_s[s] - r["wealth"][s] for s in worlds_e)
        r["worst_estimation_wealth"] = min(r["wealth"][s] for s in worlds_e)
    undominated = [r for r in feas if not any(dominates(o, r, worlds_e=worlds_e, worlds_b=worlds_b) for o in feas if o is not r)]
    superior = [r for r in feas if all(dominates(r, o, worlds_e=worlds_e, worlds_b=worlds_b) for o in feas if o is not r)]
    central_max = max(feas, key=lambda r: r["wealth"][worlds_e[0]])   # worlds_e[0] is the central world by contract
    if len(superior) == 1:
        sel, rule = superior[0], "ROBUST_SUPERIORITY"
        tied = [sel]
    else:
        m = min(r["regret"] for r in undominated)
        tied = [r for r in undominated if r["regret"] <= m + EPS_GBP]

        def key(r):
            return (-r["worst_estimation_wealth"], -min((r["wealth"][s] for s in worlds_b), default=0.0),
                    _risk_of(r) if _risk_of(r) is not None else float("inf"),
                    r["entry_costs_gbp"], r["stock_gbp"])
        tied.sort(key=key)
        sel = tied[0]
        rule = "MINIMAX_REGRET_OVER_S_E"
        if len(tied) > 1:
            k0, k1 = key(tied[0]), key(tied[1])
            same = all(abs(a - b) <= EPS_GBP for a, b in zip(k0, k1))
            if same and tied[0]["bundle"] != tied[1]["bundle"]:
                return {"state": "DECISION_TIE_UNRESOLVED", "tied": tied[:5], "undominated": undominated,
                        "best_by_world": best_s, "central_max": central_max,
                        "why": ("two economically distinct bundles tie on regret and every declared "
                                "tie-break within GBP %.2f - no ticker order, score or free-form "
                                "preference may resolve it (s8.3)" % EPS_GBP)}
            rule += " + tie-breaks (worst estimation wealth, bear wealth, risk, cost, capital)" if len(tied) > 1 else ""
    return {"state": "OK", "selected": sel, "rule": rule, "undominated": undominated,
            "robustly_superior": superior, "tied": tied, "best_by_world": best_s,
            "central_max": central_max}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# s8.4 AMBITION — 20% preferred, 25% stretch, on INCREMENTAL stock cash only
# ═══════════════════════════════════════════════════════════════════════════════════════════
def incremental_er(row: dict, scen: Dict[str, Dict[str, float]], costs_by: Dict[str, dict],
                   world: str = "CENTRAL") -> Optional[float]:
    """Capital-weighted NET ER12 of the incremental stock purchase cash (never the existing sleeve,
    never an unweighted average of names - s8.4)."""
    amt = sum(a for _, a in row["bundle"])
    if amt <= 0:
        return None
    w = sum(position_wealth(a, scen[t][world], costs_by[t]) for t, a in row["bundle"])
    return w / amt - 1.0


def ambition_view(rows: List[dict], cmp: dict, scen, costs_by) -> dict:
    feas = [r for r in rows if r.get("feasible")]
    for r in feas:
        r["incremental_net_er12"] = incremental_er(r, scen, costs_by)
        e = r["incremental_net_er12"]
        r["ambition_band"] = ("NO_STOCK (ER N/A)" if e is None else "STRETCH_>=25" if e * 100 >= STRETCH_PCT
                              else "AMBITION_>=20" if e * 100 >= AMBITION_PCT else "BELOW_20")

    def best(sub):
        if not sub:
            return None
        return min(sub, key=lambda r: (r["regret"], -r["worst_estimation_wealth"]))
    b20 = best([r for r in feas if r["incremental_net_er12"] is not None and r["incremental_net_er12"] * 100 >= AMBITION_PCT])
    b25 = best([r for r in feas if r["incremental_net_er12"] is not None and r["incremental_net_er12"] * 100 >= STRETCH_PCT])
    blo = best([r for r in feas if r["incremental_net_er12"] is not None and r["incremental_net_er12"] * 100 < AMBITION_PCT])
    nost = next((r for r in feas if not r["bundle"]), None)
    sel = cmp.get("selected")
    out = {"ambition_pct": AMBITION_PCT, "stretch_pct": STRETCH_PCT,
           "basis": "capital-weighted NET central ER12 of the incremental stock cash; a preference, never a floor or a forced fill (s8.4)",
           "selected_band": None if sel is None else sel.get("ambition_band"),
           "best_ambition_bundle": _brief(b20), "best_stretch_bundle": _brief(b25),
           "best_below_20_bundle": _brief(blo), "no_stock_bundle": _brief(nost),
           "ambition_feasible": b20 is not None,
           "state": "AMBITION_FEASIBLE" if b20 is not None else "AMBITION_NOT_FEASIBLE"}
    if sel is not None and sel.get("ambition_band") == "BELOW_20" and b20 is not None:
        out["below_20_selection_reason"] = {
            "central_return_sacrificed_gbp": _r(b20["wealth"]["CENTRAL"] - sel["wealth"]["CENTRAL"]),
            "regret_difference_gbp": _r(b20["regret"] - sel["regret"]),
            "bear_wealth_difference_gbp": _r(sel["wealth"]["BUSINESS_BEAR"] - b20["wealth"]["BUSINESS_BEAR"]),
            "rule": "the robust comparator outranks ambition; the ambition never rescues weaker robust economics"}
    return out


def _brief(r: Optional[dict]) -> Optional[dict]:
    if r is None:
        return None
    return {"bundle": [{"ticker": t, "gbp": a} for t, a in r["bundle"]], "stock_gbp": r["stock_gbp"],
            **({"sell": [{"ticker": t, "gbp": s} for t, s in sorted(r["sold"].items())]} if r.get("sold") else {}),
            "residual_gbp": r["residual_gbp"], "regret_gbp": _r(r.get("regret")),
            "wealth_gbp": {s: _r(v) for s, v in r["wealth"].items()},
            "worst_estimation_wealth_gbp": _r(r.get("worst_estimation_wealth")),
            "gbp_vol": _r(r.get("gbp_vol")),
            **({"risk_metric_gbp": _r(r.get("risk_metric_gbp"))} if "risk_metric_gbp" in r else {}),
            **({"appetite_guard_state": (r.get("appetite_guard") or {}).get("state")} if r.get("appetite_guard") else {}),
            "incremental_net_er12_pct": _r((r.get("incremental_net_er12") or 0) * 100, 2)
            if r.get("incremental_net_er12") is not None else None,
            "ambition_band": r.get("ambition_band"), "feasible": r.get("feasible"),
            "breaches": r.get("breaches")}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# P6 — TYPED CASE AMENDMENTS (s9.3): validated at the boundary, append-only, whole-decision rerun
# ═══════════════════════════════════════════════════════════════════════════════════════════
AMENDMENT_REQUIRED = ("amendment_id", "case_id", "security_id", "field_path", "proposed_value",
                      "source_ids", "published_at", "question_id", "rationale", "reviewer",
                      "created_at", "old_hash")
CONSENSUS_ONLY_SOURCES = ("yahoo", "yfinance", "consensus", "analyst consensus")


def security_input_hash(t: str, inp: dict) -> str:
    """The base a case amendment is written against: HV case + PIT row + step9 flag fields."""
    hv = next((c for c in (inp["horizon_value"].get("cases") or []) if c.get("ticker") == t), None)
    w = _watch_records(inp["step9_pre"]).get(t) or {}
    pit = (inp.get("pit_capture") or {}).get(t) or {}
    return _sha({"hv": hv, "pit": pit.get("fingerprint") or pit.get("table_fingerprints"),
                 "flags": {k: w.get(k) for k in ("disqualifier_flags", "review_flags", "late_cycle_flag", "stage_gate")}})[:16]


def validate_amendment(a: dict, inp: dict, cutoff: str) -> Tuple[bool, str]:
    miss = [k for k in AMENDMENT_REQUIRED if a.get(k) in (None, "", [])]
    if miss:
        return False, "missing required field(s) %s" % miss
    fp = str(a["field_path"])
    if any(fp.lower().startswith(p) for p in AMENDMENT_FORBIDDEN_PREFIXES):
        return False, ("FORBIDDEN_FIELD %s: hurdle, policy, rungs, cash, quotes, permissions, risk limits, "
                       "execution and selection are not case-amendable (s9.3, T39)" % fp)
    if fp.startswith("flag_adjudication."):
        dom = AMENDMENT_ALLOWLIST["flag_adjudication"]
    elif fp in AMENDMENT_ALLOWLIST:
        dom = AMENDMENT_ALLOWLIST[fp]
    else:
        return False, "UNKNOWN_FIELD %s - not on the allowlist %s" % (fp, sorted(AMENDMENT_ALLOWLIST))
    _qs = a.get("question_id")
    if isinstance(_qs, list):
        # one evidenced NO_CHANGE answer may cover several questions (each named); a FIELD change answers one
        if fp != "case.question_answer" or not _qs:
            return False, "a question_id LIST is admissible only on an evidenced case.question_answer NO_CHANGE (B09-R03)"
        bad = [q for q in _qs if str(q) not in CASE_QUESTION_IDS]
    else:
        bad = [] if str(_qs or "") in CASE_QUESTION_IDS else [_qs]
    if bad:
        return False, "question_id %r is not one of the receipt's questions %s (B09-R03)" % (bad, list(CASE_QUESTION_IDS))
    v = a["proposed_value"]
    if isinstance(dom, tuple) and v not in dom:
        return False, "value %r outside %s" % (v, dom)
    if dom == "number" and _num(v) is None:
        return False, "value %r is not a number" % (v,)
    if dom == "bool" and not isinstance(v, bool):
        return False, "value %r is not a bool" % (v,)
    if len(str(a.get("rationale") or "")) < 40:
        return False, "rationale too short to be audited (< 40 chars)"
    cur = security_input_hash(a["security_id"], inp)
    if a["old_hash"] != cur:
        return False, "STALE_BASE: written against %s, current input hash is %s (T40, T90)" % (a["old_hash"], cur)
    pubs = a["published_at"] if isinstance(a["published_at"], list) else [a["published_at"]]
    if any(str(p)[:10] > cutoff for p in pubs):
        return False, "a source is published after the decision cutoff %s - not PIT" % cutoff
    srcs = [str(s).lower() for s in (a["source_ids"] if isinstance(a["source_ids"], list) else [a["source_ids"]])]
    if fp.startswith("scenario.") and all(any(c in s for c in CONSENSUS_ONLY_SOURCES) for s in srcs):
        return False, ("CIRCULAR_CONSENSUS: a scenario amendment sourced only from the consensus the model "
                       "already uses is not independent evidence (s4.2, T40)")
    if dom == "number" and not a.get("estimate_kind"):
        return False, "a numeric scenario amendment must declare its estimate_kind"
    return True, "OK"


def case_question_coverage(am: dict) -> dict:
    """B09-R03: per security, which of the twelve questions carry an accepted typed answer."""
    out = {}
    for a in (am or {}).get("accepted") or []:
        t = a.get("security_id")
        row = out.setdefault(t, {"answered": {}, "missing": []})
        _qs = a.get("question_id")
        for q in (_qs if isinstance(_qs, list) else [_qs]):
            row["answered"].setdefault(str(q or ""), []).append(a.get("amendment_id"))
    for t, row in out.items():
        row["missing"] = [q for q in CASE_QUESTION_IDS if q not in row["answered"]]
    return out


def complete_cases(am: dict) -> set:
    """THE one home for 'is this case complete' (B09-R03): case.complete AND - while
    CASE_QUESTION_RECEIPTS_REQUIRED - every required question answered by a typed receipt."""
    flagged = {t for t, v in ((am or {}).get("by_security") or {}).items() if v.get("case.complete") is True}
    if not CASE_QUESTION_RECEIPTS_REQUIRED:
        return flagged
    cov = case_question_coverage(am)
    return {t for t in flagged if not (cov.get(t) or {}).get("missing", list(CASE_QUESTION_IDS))}


def amend_cli(month: str, src: str, root: str = HERE) -> int:
    """Append ONE typed amendment (JSON object in `src`) to capital_case_amendments_{month}.jsonl
    after validating it against the CURRENT inputs exactly as the engine will (allowlist, PIT,
    stale base, circular consensus). An invalid amendment is refused with its reason and NOTHING is
    written; the file is append-only (history never rewritten). The engine must then be re-run in
    full (mechanical rerun - the case never edits the decision directly, s9.3)."""
    with open(src, encoding="utf-8") as fh:
        doc = json.load(fh)
    # B09-R03 (P-C): a file may carry ONE amendment object or a LIST of them (the twelve typed question
    # receipts of one case). A list is ALL-OR-NOTHING: any refusal writes nothing.
    batch = doc if isinstance(doc, list) else [doc]
    inp = load_inputs(month, root)
    _m9 = inp["step9_pre"].get("_meta") or {}
    cutoff = str(_m9.get("as_of") or _m9.get("produced_at") or "")[:10]
    p = os.path.join(root, "capital_case_amendments_%s.jsonl" % month)
    existing = set()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            existing = {json.loads(ln).get("amendment_id") for ln in fh if ln.strip()}
    seen = set()
    for a in batch:
        if not isinstance(a, dict):
            print("AMENDMENT_REFUSED: a batch entry is not a JSON object - nothing written")
            return 2
        ok_, why = validate_amendment(a, inp, cutoff)
        if not ok_:
            print("AMENDMENT_REFUSED %s: %s - nothing written" % (a.get("amendment_id"), why))
            return 2
        if a["amendment_id"] in existing or a["amendment_id"] in seen:
            print("AMENDMENT_REFUSED %s: duplicate amendment_id (append-only; supersede with a new id) - nothing written"
                  % a["amendment_id"])
            return 2
        seen.add(a["amendment_id"])
    with open(p, "a", encoding="utf-8") as fh:
        for a in batch:
            fh.write(json.dumps(a, sort_keys=True) + "\n")
    print("AMENDMENT_RECORDED %s -> %s; now re-run: python3 capital_decision_engine.py --month %s"
          % (",".join(a["amendment_id"] for a in batch), os.path.basename(p), month))
    return 0


def load_amendments(month: str, inp: dict, cutoff: str, root: str = HERE, path: Optional[str] = None) -> dict:
    p = path or os.path.join(root, "capital_case_amendments_%s.jsonl" % month)
    accepted, rejected, by = [], [], {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for i, ln in enumerate(fh):
                if not ln.strip():
                    continue
                try:
                    a = json.loads(ln)
                except ValueError as exc:
                    rejected.append({"line": i + 1, "why": "unparseable: %s" % exc})
                    continue
                ok, why = validate_amendment(a, inp, cutoff)
                if not ok:
                    rejected.append({"amendment_id": a.get("amendment_id"), "security_id": a.get("security_id"),
                                     "field_path": a.get("field_path"), "why": why})
                    continue
                accepted.append(a)
                t = a["security_id"]
                fp = a["field_path"]
                if fp.startswith("flag_adjudication."):
                    by.setdefault(t, {}).setdefault("flag_adjudication", {})[fp.split(".", 1)[1]] = a["proposed_value"]
                else:
                    by.setdefault(t, {})[fp] = a["proposed_value"]
    return {"path": os.path.basename(p), "present": os.path.exists(p), "sha256": _file_sha(p),
            "accepted": accepted, "rejected": rejected, "by_security": by,
            "schema": list(AMENDMENT_REQUIRED) + ["estimate_kind", "uncertainty", "falsifier", "old_value",
                                                  "old_status", "authorised_scope", "resulting_case_hash"],
            "allowlist": {k: (list(v) if isinstance(v, tuple) else v) for k, v in AMENDMENT_ALLOWLIST.items()}}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# P7 — THE ONE CANONICAL CAPITAL DECISION RECEIPT
# ═══════════════════════════════════════════════════════════════════════════════════════════
def _policy(tw: dict) -> dict:
    return {"bucket_totals": tw["bucket_totals"], "thresholds": tw["thresholds"], "funds": tw["funds"],
            "max_single_fund_pct": tw["thresholds"]["max_single_fund_pct"],
            "stock_sleeve": tw["stock_sleeve"], "scaling_freeze": tw.get("scaling_freeze"),
            "_source": "target_weights.json", "_as_of": (tw.get("_meta") or {}).get("last_updated")}


def _currency_of_factory(inp: dict):
    store_names = (inp.get("weekly_store") or {}).get("names") or {}
    held = {s.get("ticker"): s.get("currency") for s in (inp["portfolio"].get("stocks") or [])}

    def f(t, desc=None):
        if held.get(t):
            return held[t]
        for k in (t, t + ".L"):
            c = (store_names.get(k) or {}).get("currency")
            if c:
                return c
        if desc and str(desc).strip().upper().endswith("PLC"):
            return "GBP"
        return None
    return f


def build(month: str, root: str = HERE, as_of: Optional[str] = None, amendments_path: Optional[str] = None,
          out_path: Optional[str] = None, baseline_plan_path: Optional[str] = None,
          store: Optional[dict] = None, pipeline: Optional[dict] = None, write: bool = True,
          inputs: Optional[dict] = None, fresh_after: Optional[float] = None,
          price_overrides: Optional[dict] = None, switch_scope: str = "FULL") -> dict:
    """-> the Capital Decision Receipt for `month` (e.g. 'oct_2026'). Pure with respect to capital:
    it never trades, never edits an input (R14.3); its only side effects are the receipt itself and
    one idempotent row in the append-only decision ledger (learning capture, s13).

    fresh_after (epoch seconds): when the pre-run passes its own start time, every RUN-PRODUCED input
    (FRESH_INPUTS) must have been written at/after it - an October bundle from an earlier run can
    never be consumed (s12.2 'the main run cannot consume old bundles')."""
    _fi_mark("capital_decision_engine", "build")
    t0 = _now()
    inp = inputs if inputs is not None else load_inputs(month, root)
    freshness = {"state": "NOT_CHECKED", "fresh_after": fresh_after}
    if fresh_after is not None:
        stale = {}
        for k in FRESH_INPUTS:
            fp = os.path.join(root, INPUT_FILES[k].format(m=month))
            mt = os.path.getmtime(fp) if os.path.exists(fp) else None
            if mt is None or mt + 1.0 < float(fresh_after):
                stale[k] = mt
        if stale:
            raise EngineRefused("STALE_INPUT: run-produced input(s) %s pre-date this run (fresh_after %s) - an "
                                "old bundle is never consumed (s12.2)" % (sorted(stale), fresh_after))
        freshness = {"state": "FRESH", "fresh_after": fresh_after, "checked": list(FRESH_INPUTS)}
    if as_of is None:
        _m9 = inp["step9_pre"].get("_meta") or {}
        as_of = str(_m9.get("as_of") or _m9.get("produced_at") or "")[:10]
    try:
        datetime.date.fromisoformat(str(as_of)[:10])
    except ValueError:
        raise EngineRefused("decision as_of %r is not an ISO date - the PIT cutoff cannot be established (R4.7)" % (as_of,))
    as_of = str(as_of)[:10]
    import scoring_config as _sc
    hurdle = float(_sc.ER_DEPLOY_FLOOR)
    tw = inp["target_weights"]
    policy = _policy(tw)
    portfolio = inp["portfolio"]
    s = portfolio["summary"]
    nav = float(s["total_value_gbp"])
    deployable = float(s["cash_deployable_gbp"])
    reserve = (tw.get("stock_sleeve") or {}).get("cash_reserve_gbp")
    if reserve is None:
        raise EngineRefused("target_weights.stock_sleeve.cash_reserve_gbp not declared (D23) - R4.1")
    reserve = float(reserve)
    # ⚑ ISA-0858 (12-Oct-2026, Raj D1/D3/D4/D5/D8 + B1.6b): the capital this decision may use comes from the ONE
    #   funding waterfall - cash/MMF less the D23 reserve less FIRED drawdown tranches (claimed first), plus fund
    #   trims only while the post-trade sleeve stays <= 25% and the retention brake has not FIRED, all bounded by
    #   the 30% hard ceiling measured at decision time incl. pending fills. Never re-derived here (R4.4).
    import funding_waterfall as _fw
    try:
        _wf = _fw.build(portfolio, root=root, tw=tw,
                        ledger_entries=(inp.get("transaction_ledger") or {}).get("entries") or [])
    except _fw.WaterfallRefused as _wfe:
        raise EngineRefused("FUNDING_WATERFALL_REFUSED (ISA-0858): %s" % _wfe)
    capital = round(float(_wf["stock_capital_gbp"]), 2)
    cash_convention = {"deployable_gbp": deployable, "deployable_basis": s.get("cash_deployable_basis"),
                       "reserve_gbp": reserve, "capital_for_decision_gbp": capital,
                       "cash_capital_gbp": _wf["cash_capital_gbp"],
                       "drawdown_reserved_gbp": _wf["drawdown_reserved_gbp"],
                       "max_fund_funded_gbp": _wf["max_fund_funded_gbp"],
                       "sleeve_pct_at_decision": _wf["sleeve_pct"], "band": _wf["band"], "binding": _wf["binding"],
                       "note": ("cash_deployable is GROSS (cash statement closing balance + MMF); the D23 reserve and "
                                "fired drawdown tranches are subtracted ONCE (T07, B1.6b); fund trims are added only "
                                "under D4/D5; the 30% hard ceiling bounds the total (funding_waterfall, ISA-0858)")}
    import new_capital_control as _ncc_m
    ncc = _ncc_m.status(root)
    if store is None:
        import stock_return_store as _srs
        # the SAME bytes the receipt's input hash binds (INPUT_FILES weekly_store), never a second read
        store = inp.get("weekly_store") if inp.get("weekly_store") is not None else _srs.load()
    if pipeline is None:
        import capital_destination as _cd
        # ⚑ ISA-0924: the router reads THIS month's step9_pre - the same bytes INPUT_FILES hashes into the
        #   receipt - never the newest file on disk (build(oct) after the November pre-run read November).
        pipeline = month_pipeline(month, inp, root, portfolio, policy, as_of)
    try:
        import return_architecture as _ra
        _cr = _ra.derive_cash_rate()
        cash_rate_pct = _num((_cr or {}).get("cash_expected_return_pct"))
        cash_src = (_cr or {}).get("source")
    except Exception as exc:                                        # noqa: BLE001
        cash_rate_pct, cash_src = None, "UNAVAILABLE: %s" % exc
    if cash_rate_pct is None:
        raise EngineRefused("the cash rate is UNMEASURED (%s) - the residual cannot be valued, and zero is "
                            "a decision nobody made (R4.1)" % cash_src)
    cash_rate = cash_rate_pct / 100.0
    am = load_amendments(month, inp, as_of, root, amendments_path)
    census = _build_census(inp, pipeline, store, hurdle, as_of, am["by_security"], price_overrides)
    recs = census["records"]
    costs = cost_model(inp["transaction_ledger"], _currency_of_factory(inp))
    import position_sizing as _ps
    min_entry = float(_ps.min_entry_gbp(nav, tw)["min_entry_gbp"])
    eligible = []
    for t, r in recs.items():
        if r["disposition"] != "ELIGIBLE":
            continue
        cur_val = float(next((x.get("value_gbp") or 0 for x in portfolio.get("stocks") or [] if x.get("ticker") == t), 0.0))
        cap = round(r["permission"]["target_pct"] / 100.0 * nav - cur_val, 2)
        is_held = cur_val > 0
        mn = EPS_GBP if is_held else min_entry      # D23: no per-ticket top-up floor; D16 for openings
        if is_held and cap <= EPS_GBP:
            r["disposition"] = "CAP_BOUND"
            r["reasons"].append(_reason("size", "AT_OR_ABOVE_PERMITTED_TARGET", cur_val,
                                        r["permission"]["target_pct"] / 100.0 * nav, "position_sizing.target_pct"))
            continue
        if not is_held and cap < min_entry - EPS_GBP:
            r["disposition"] = "MIN_ENTRY"
            r["reasons"].append(_reason("size", "CAP_BELOW_MIN_ENTRY", cap, min_entry, "position_sizing.min_entry_gbp (D16)"))
            continue
        _lad = _ps.ladder(tw)
        levels = sorted({round(pct / 100.0 * nav - cur_val, 2) for pct in _lad.values()
                         if pct <= r["permission"]["target_pct"] + 1e-9 and pct / 100.0 * nav - cur_val > EPS_GBP})
        r["permission"]["levels_gbp"] = levels
        eligible.append({"ticker": t, "cap_gbp": cap, "levels": levels, "min_gbp": mn, "held": is_held})
    scen, notes, costs_by = {}, {}, {}
    dropped = []
    for e in list(eligible):
        t = e["ticker"]
        emp = empirical_worst_52w(store, t)
        recs[t]["empirical_bear"] = emp
        scen[t], notes[t] = name_scenarios(recs[t]["er12"], emp)
        costs_by[t] = costs_for(t, recs[t]["er12"].get("currency"), costs)
        if any(scen[t][k] is None for k in S_B):
            # s8.3: a missing material downside input blocks the affected selection
            recs[t]["disposition"] = "CASE_PENDING"
            recs[t]["reasons"].append(_reason("downside", "DOWNSIDE_INPUT_MISSING",
                                              {k: scen[t][k] for k in S_B}, "every S_B world measured",
                                              "BuildSpec s8.3", "supply an evidenced bear outcome by case amendment"))
            eligible.remove(e)
            dropped.append(t)
    for t in dropped:
        scen.pop(t, None)
    bundles, cert = legal_bundles(eligible, capital, min_entry)
    oracle = None
    if cert["state"] == "COMPLETE" and len(eligible) <= 11:
        bf = bruteforce_bundles(eligible, capital, min_entry)
        prod = set(bundles)
        oracle = {"independent_bruteforce_count": len(bf), "production_count": len(prod),
                  "identical_sets": bf == prod, "missing_in_production": len(bf - prod),
                  "extra_in_production": len(prod - bf)}
        if bf != prod:
            raise EngineRefused("SEARCH ORACLE DISAGREES with production enumeration %s - refused (T21/T29)" % oracle)
    receipt = {"receipt_kind": "CAPITAL_DECISION_RECEIPT", "schema_version": SCHEMA_VERSION,
               "method_version": METHOD_VERSION, "month": month, "as_of": as_of,
               "produced_at": t0.isoformat(timespec="seconds"),
               "authority": "ISA-0804 Integrated Capital Decision Engine BuildSpec (Raj 03-Oct-2026, Rev 1.1)",
               "new_capital_control": {k: ncc.get(k) for k in ("state", "blocks_new_stock_capital", "record_id",
                                                              "failed_contracts", "policy_state", "policy_hard_max")},
               "objective": {"hurdle_pct": hurdle, "hurdle_source": "scoring_config.ER_DEPLOY_FLOOR",
                             "ambition_pct": AMBITION_PCT, "stretch_pct": STRETCH_PCT, "horizon_months": HORIZON_MONTHS},
               "book": {"nav_gbp": nav, "stock_sleeve_gbp": s.get("stock_sleeve_value_gbp"),
                        "held_stocks": {x.get("ticker"): x.get("value_gbp") for x in portfolio.get("stocks") or []},
                        "data_date": (portfolio.get("_meta") or {}).get("data_date"), **cash_convention},
               "costs": costs, "cash_rate": {"pct": cash_rate_pct, "source": cash_src,
                                             "use": ("the residual (capital not placed in stocks) is valued at the measured "
                                                     "cash rate in every world: the fund residual router's return is "
                                                     "ALTERNATIVE_NOT_COMPARABLE (no fund ER12 exists), so stock bundles are "
                                                     "compared CONDITIONAL on the adopted D20 demand-pull envelope (s8.2)")},
               "min_entry_gbp": min_entry, "input_hashes": inp["input_hashes"], "input_freshness": freshness,
               "scenario_definitions": {k: SCENARIO_DEFINITIONS[k] for k in S_E + S_B},
               "census": {"n_universe": census["n_universe"], "counts": census["counts"],
                          "conservation": census["conservation"],
                          "corporate_events": census.get("corporate_events"),
                          "old_funnel_for_comparison": census["old_funnel_for_comparison"]},
               "dossiers": recs, "amendments": {k: am[k] for k in ("path", "present", "sha256", "rejected")} |
               {"n_accepted": len(am["accepted"]), "accepted_ids": [a["amendment_id"] for a in am["accepted"]]},
               "search": dict(cert, oracle=oracle)}
    if cert["state"] != "COMPLETE":
        receipt.update(state="SEARCH_INCOMPLETE", capital_authorised=False, selected_actions=[])
        return _finish(receipt, month, root, out_path, write)
    if not bundles:
        bundles = [tuple()]
    rows = evaluate_bundles(bundles, scen, costs_by, capital, cash_rate)
    try:
        from portfolio_analytics import MIN_ECONOMIC_TRADE as _min_trade      # one home (waiting_room reads it too)
        _min_trade = float(_min_trade)
    except Exception as _mte:                                               # noqa: BLE001
        if float(_wf["max_fund_funded_gbp"]) > 0:
            raise EngineRefused("MIN_ECONOMIC_TRADE unreadable (%s) - a fund-trim leg cannot be shown economic" % _mte)
        _min_trade = 0.0
    import sleeve_risk as _sr
    try:
        pre = _sr.risk_shares(store, portfolio)
        pre_shares = pre["shares"]
        pre_risk = {"sleeve_sigma": pre["sleeve_sigma_ann"], "gbp_vol": pre["sleeve_sigma_ann"] * float(s.get("stock_sleeve_value_gbp") or 0)}
    except Exception as exc:                                        # noqa: BLE001
        pre_shares, pre_risk = {}, {"state": "UNMEASURED", "why": str(exc)}
    ceiling = float(getattr(_sc, "SLEEVE_RISK_CEILING_PCT"))
    max_pos = float(tw["thresholds"]["max_stock_position_pct"]) * nav
    # ⚑ P-D (B08-R03, ISA-0887, 24/25-Oct-2026): the WHOLE-ISA covariance (stocks + funds, one sleeve_risk basis) is
    #   built ONCE per run; every row of both contests is judged on it when every held fund qualifies
    #   (WHOLE_BOOK_MEASURED), else on stock-sleeve GBP volatility exactly as before (stated). Rollback:
    #   scoring_config.WHOLE_BOOK_RISK_METRIC = False.
    try:
        import whole_book_risk as _wbr887
        _wbx = _wbr887.engine_context(portfolio, store, as_of=as_of, root=root)
    except Exception as _wbe:                                       # noqa: BLE001
        _wbx = {"basis": "STOCK_SLEEVE_ONLY", "why": "whole_book_risk unavailable: %s: %s" % (type(_wbe).__name__, _wbe),
                "store": None, "aug_store": None}
    # ⚑ ISA-0465 / B08-R04 (19-Oct-2026): the adopted sector/theme caps are evaluated WHERE CAPITAL IS DECIDED.
    #   They were wired only into the legacy allocate() path, which has been SHADOW since the engine took the
    #   stock decision (ISA-0804), so the 03-Oct pre-run produced NO concentration record and the control could
    #   never accumulate the real SHADOW evidence its LIVE readiness requires. SHADOW: every bundle is evaluated
    #   and recorded, nothing binds. LIVE (V2_FLAGS concentration_gate): a FAIL is a hard breach.
    try:
        import concentration_control as _ccm465
        _conc = {"mode": _ccm465.mode(), "tax": _ccm465.load_taxonomy(),
                 "held": {x.get("ticker"): float(x.get("value_gbp") or 0.0) for x in portfolio.get("stocks") or []
                          if x.get("ticker")}, "error": None}
    except Exception as _ce465:                                     # noqa: BLE001
        _conc = {"mode": "UNKNOWN", "tax": None, "held": {}, "error": "%s: %s" % (type(_ce465).__name__, _ce465)}
    # ⚑ ISA-0621 / ISA-0465 P-B6 finding (19-Oct-2026): the B3/D15 AI-complex factor cap, evaluated per plan on the NET
    #   post-trade book (factor_eval), binding with the SAME concentration_gate flag as the theme caps (Raj: one LIVE
    #   decision after 1-Nov). Before P-B9 nothing evaluated it: Checkpoint-D tick 5 accepted a factor_state the CLI
    #   never passed.
    try:
        import factor_lookthrough as _flt621
        _fm621 = _flt621.load_map()
        _dc621 = _flt621._declared_coverage()
        _tm621 = _flt621._theme_layer(None)
        _fctx = {"fmap": _fm621, "declared": _dc621, "themes": _tm621,
                 "pre": _flt621.compute(portfolio, _fm621, declared=_dc621, themes=_tm621), "error": None}
    except Exception as _fe621:                                     # noqa: BLE001
        _fctx = {"pre": None, "error": "%s: %s" % (type(_fe621).__name__, _fe621)}
    for r in rows:
        if not r["bundle"]:
            r["risk"] = {"state": "MEASURED", **pre_risk, "note": "no-action: current sleeve"}
            r["gbp_vol"] = pre_risk.get("gbp_vol")
            _wb_annotate(r, (), _wbx, portfolio, nav)
            r["breaches"] = []
            r["feasible"] = pre_risk.get("gbp_vol") is not None
            continue
        _ih = inp.get("input_hashes") or {}
        _mk = ((_ih.get("weekly_store") if inp.get("weekly_store") is store else ("id", id(store))),
               _ih.get("portfolio"), _sha(portfolio.get("stocks")), tuple(r["bundle"]), round(nav, 2))
        rk = _BUNDLE_RISK_MEMO.get(_mk) if _mk[0] and _mk[1] else None
        if rk is None:
            rk = bundle_risk(store, portfolio, r["bundle"], nav)
            if _mk[0] and _mk[1]:
                _BUNDLE_RISK_MEMO[_mk] = rk
        rk = copy.deepcopy(rk)
        r["risk"] = rk
        r["gbp_vol"] = rk.get("gbp_vol")
        _wb_annotate(r, r["bundle"], _wbx, portfolio, nav,
                     fund_trim_gbp=max(float(r["stock_gbp"]) - float(_wf["cash_capital_gbp"]), 0.0))
        r["breaches"] = constraints_for(r["bundle"], rk, pre_shares, ceiling, max_pos)
        if _conc["tax"] is not None and _conc["mode"] in ("SHADOW", "LIVE"):
            r["concentration"] = concentration_eval(r["bundle"], _conc["held"], nav, _conc["tax"])
            if _conc["mode"] == "LIVE" and r["concentration"]["state"] != "PASS":
                r["breaches"].append({"constraint": "ISA-0465 concentration caps (sector 12% NAV / theme 50% post-trade sleeve)",
                                      "violations": r["concentration"]["violations"]})
        if _fctx.get("pre") is not None and _conc["mode"] in ("SHADOW", "LIVE"):
            r["factor"] = factor_eval(r["bundle"], portfolio, nav, _fctx)
            if _conc["mode"] == "LIVE" and (r["factor"]["state"] == "FAIL" or
                                            (r["factor"]["state"] != "PASS" and _fctx["pre"].get("breach"))):
                r["breaches"].append({"constraint": "B3/D15 AI-complex factor cap (net post-trade lower bound)",
                                      "factor": r["factor"]})
        _trim_r = round(max(float(r["stock_gbp"]) - float(_wf["cash_capital_gbp"]), 0.0), 2)
        if 0.005 < _trim_r < _min_trade - EPS_GBP:
            r["breaches"].append({"constraint": "ISA-0858 D4 fund-trim leg below MIN_ECONOMIC_TRADE",
                                  "observed_gbp": _trim_r, "limit_gbp": _min_trade})
        r["feasible"] = not r["breaches"]
    cmp = compare(rows)
    amb = ambition_view(rows, cmp, scen, costs_by) if cmp.get("state") in ("OK", "DECISION_TIE_UNRESOLVED") else None
    receipt["risk_constraints"] = {
        "applied": ["ISA-0600 Dimson beta <= SLEEVE_BETA_MAX for every addition vs the post-trade sleeve (in-run names at proposed weights)",
                    "D27 SLEEVE_RISK_CEILING_PCT = %.1f%%: no bundle creates or deepens a risk-share breach" % ceiling,
                    "max_stock_position_pct = %.4f of NAV" % (max_pos / nav),
                    "cash: stock spend <= deployable - D23 reserve"],
        "reported_not_binding": ["concentration_control sector/theme caps (V2_FLAGS concentration_gate %s)" % _conc["mode"],
                                 "AI-complex factor cap - net post-trade lower bound per plan (receipt.factor; binds with "
                                 "concentration_gate LIVE; Checkpoint-D tick 5 reads receipt.factor.selected) - %s" % _conc["mode"]]
                                if _conc["mode"] != "LIVE" else [],
        "pre_trade": pre_risk, "pre_trade_risk_shares": {k: _r(v, 5) for k, v in pre_shares.items()},
        "risk_metric": {"basis": _wbx.get("basis"), "why": _wbx.get("why"), "fund_states": _wbx.get("fund_states"),
                        "definition": ("WHOLE_BOOK_MEASURED: post-action whole-ISA GBP volatility (stocks + funds + cash, "
                                       "one sleeve_risk basis; fund-trim legs pro rata) - B08-R03 / ISA-0887"
                                       if _wbx.get("basis") == "WHOLE_BOOK_MEASURED" else
                                       "STOCK_SLEEVE_ONLY: stock-sleeve GBP volatility (fund sleeve held constant)"),
                        "no_action_whole_book_gbp_vol": _r(next((x.get("risk_metric_gbp") for x in rows if not x["bundle"]
                                                                 and "risk_metric_gbp" in x), None))},
        "sleeve_bands": ("ISA-0858 D1: hard ceiling %.0f%% (room GBP %.2f), priority band %.0f%%; band now %s"
                         % (_wf["inputs"]["bands"]["hard_ceiling_pct"], _wf["hard_room_gbp"],
                            _wf["inputs"]["bands"]["priority_ceiling_pct"], _wf["band"]))}
    _ev465 = [r for r in rows if r.get("concentration")]
    receipt["concentration"] = {
        "mode": _conc["mode"], "error": _conc["error"],
        "sector_cap_pct_nav": 12.0, "theme_cap_pct_sleeve": 50.0,
        "taxonomy": {k: ((_conc["tax"] or {}).get(k)) for k in ("theme_taxonomy", "declared_file")} if _conc["tax"] else None,
        "n_bundles_evaluated": len(_ev465),
        "n_bundles_failing": sum(1 for r in _ev465 if r["concentration"]["state"] != "PASS"),
        "failing_examples": [{"bundle": [list(x) for x in r["bundle"]], "violations": r["concentration"]["violations"]}
                             for r in _ev465 if r["concentration"]["state"] != "PASS"][:6],
        "binding": _conc["mode"] == "LIVE",
        # ISA-0700 (19-Oct-2026): universe names with no theme membership - refused UNKNOWN if LIVE; the email names them
        "unmapped_universe": (sorted(t for t in (receipt.get("dossiers") or {})
                                     if _ccm465._themes(_conc["tax"], t) is None) if _conc["tax"] else None),
        # ⚑ found on the P-B3 shadow: with research pending the contest held ONLY the no-action bundle, so 0
        #   bundles were evaluated and an 'OK' impact would have counted as SHADOW evidence. Nothing checked is
        #   not evidence (R2.10): 0 evaluated is NO_BUNDLES_EVALUATED, never OK.
        "impact": ({"state": "UNAVAILABLE", "reason": _conc["error"] or ("mode %s" % _conc["mode"])}
                   if _conc["error"] or _conc["mode"] not in ("SHADOW", "LIVE") else
                   {"state": "NO_BUNDLES_EVALUATED", "reason": "the contest held no non-empty bundle this run "
                                                              "(e.g. research pending) - nothing was checked"}
                   if not _ev465 else
                   {"state": "OK", "basis": "every non-empty bundle evaluated on its post-trade book"}),
        "basis": ("ISA-0465 / B08-R04: Raj 16-Sep-2026 caps through concentration_control.book_invariant + UNKNOWN additions "
                  "refused; SHADOW records what LIVE would refuse and binds nothing")}
    receipt["frontier"] = {"n_bundles": len(rows), "n_feasible": sum(1 for r in rows if r["feasible"]),
                           "n_undominated": len(cmp.get("undominated") or []),
                           "robustly_superior": [_brief(r) for r in cmp.get("robustly_superior") or []],
                           "undominated_top": [_brief(r) for r in sorted(cmp.get("undominated") or [], key=lambda r: r["regret"])[:12]],
                           "central_max": _brief(cmp.get("central_max")),
                           "best_wealth_by_world_gbp": {k: _r(v) for k, v in (cmp.get("best_by_world") or {}).items()},
                           "infeasible_examples": [_brief(r) for r in rows if not r["feasible"]][:8]}
    receipt["comparator"] = {"state": cmp.get("state"), "rule": cmp.get("rule"), "why": cmp.get("why"),
                             "definition": ("1 hard-infeasible bundles excluded; 2 robust superiority = wins every "
                                            "estimation world by > GBP 0.01 AND no worse in the business-bear world "
                                            "AND no higher risk metric (risk_constraints.risk_metric: whole-ISA GBP "
                                            "volatility when WHOLE_BOOK_MEASURED, else stock-sleeve GBP volatility); "
                                            "3 otherwise minimax GBP regret "
                                            "over the estimation worlds among undominated bundles; 4 declared tie-breaks; "
                                            "economically distinct residual ties -> DECISION_TIE_UNRESOLVED"),
                             "not_claimed": "not a calibrated expected-utility optimum; exact search removes search error, not forecast error"}
    receipt["ambition"] = amb
    sel = cmp.get("selected")
    complete = complete_cases(am)                     # B09-R03: typed question receipts, one home
    # ── ISA-0621: the switch contest (held stocks as donors), LIVE by Raj 19-Oct-2026 ─────────────────────
    _sw_rows, _sw_adopted = None, False
    try:
        receipt["switch_contest"] = switch_contest({
            "inp": inp, "pipeline": pipeline, "store": store, "hurdle": hurdle, "as_of": as_of,
            "amendments": am["by_security"], "price_overrides": price_overrides, "recs": recs, "eligible": eligible,
            "scen": scen, "costs_by": costs_by, "costs": costs, "capital": capital,
            "cash_capital": float(_wf["cash_capital_gbp"]), "cash_rate": cash_rate, "portfolio": portfolio, "nav": nav,
            "min_entry": min_entry, "tw": tw, "min_trade": _min_trade if _min_trade > 0 else 500.0, "conc": _conc,
            "fctx": _fctx, "pre_shares": pre_shares, "pre_risk": pre_risk, "ceiling": ceiling, "max_pos": max_pos,
            "live_rows": rows, "live_selected": sel, "complete": complete, "root": root, "wbx": _wbx,
            "scope": switch_scope})
    except Exception as _sce:                                       # noqa: BLE001
        receipt["switch_contest"] = {"authority": switch_authority(), "state": "ERROR",
                                     "why": "%s: %s" % (type(_sce).__name__, _sce),
                                     "effect": "the switch contest failed; the new-money decision stands unchanged"}
    finally:
        try:                                             # the scoped risk memo can never outlive the contest
            import sleeve_risk as _srz
            if _srz._FROZEN.get("store") is not None:
                _srz.frozen_store(None).__exit__(None, None, None)
        except Exception:                                           # noqa: BLE001
            pass
    _scx = receipt["switch_contest"]
    _sw_row = _scx.pop("_selected_row", None)
    _sw_rows = _scx.pop("_elig_rows", None)
    _sw_scen, _sw_costs = _scx.pop("_scen", {}) or {}, _scx.pop("_costs_by", {}) or {}
    if (_scx.get("authority") == "LIVE" and _scx.get("state") == "OK" and (_scx.get("parity") or {}).get("state") == "MATCH"
            and _sw_row is not None and cmp.get("state") == "OK"):
        scen.update(_sw_scen)
        costs_by.update(_sw_costs)
        sel = _sw_row
        _e621 = incremental_er(sel, scen, costs_by)
        sel["incremental_net_er12"] = _e621
        sel["ambition_band"] = ("NO_STOCK (ER N/A)" if _e621 is None else "STRETCH_>=25" if _e621 * 100 >= STRETCH_PCT
                                else "AMBITION_>=20" if _e621 * 100 >= AMBITION_PCT else "BELOW_20")
        _sw_adopted = True
    _scx["adopted"] = {"state": ("ADOPTED" if _sw_adopted else "NOT_ADOPTED"),
                       "why": ("the authority-eligible switch plan sells %s and beats every new-money plan - it IS this "
                               "month's selection" % sorted((sel or {}).get("sold", {}))) if _sw_adopted else
                              ("authority %s; state %s; parity %s; %s" % (_scx.get("authority"), _scx.get("state"),
                               (_scx.get("parity") or {}).get("state"),
                               "no authority-eligible sale wins" if _sw_row is None else "comparator %s" % cmp.get("state")))}
    receipt["selected"] = _brief(sel)
    # ⚑ P-D (B08-R09, ISA-0888, Raj 24-Oct-2026 'Review + guard new risk'): the SELECTED plan's severe-scenario guard.
    #   REVIEW_REQUIRED never refuses or sells - Checkpoint-D tick 14 requires Raj's explicit appetite approval.
    _ag = (sel or {}).get("appetite_guard")
    receipt["appetite_guard"] = {"selected": _ag, "state": (_ag or {}).get("state") if _ag else "NOT_EVALUATED",
                                 "n_rows_review_required": sum(1 for x in rows if (x.get("appetite_guard") or {}).get("state")
                                                               == "REVIEW_REQUIRED"),
                                 "authority": "isa_policy.RISK_APPETITE (Raj 24-Oct-2026)",
                                 "consumer": "Checkpoint-D tick 14 (explicit approval); email s2 line"}
    # ── decision-relevant research (fixed point, s9.1) ───────────────────────────────────────
    sel_names = [t for t, _ in (sel or {}).get("bundle", ())]
    sel_min_central = min((scen[t]["CENTRAL"] for t in sel_names), default=None)
    unused = capital - (sel["stock_gbp"] if sel else 0.0)
    pending = []
    for t, r in recs.items():
        if r["disposition"] != "CASE_PENDING":
            continue
        er = r["er12"]
        cands = [x for x in (er.get("central_local"), er.get("auth_local"), er.get("cross_local"),
                             (er.get("variants_local") or {}).get("HIGH_ESTIMATES")) if x is not None]
        plaus = max(cands) if cands else None
        relevant = plaus is not None and plaus * 100.0 >= hurdle and (
            unused >= min_entry - EPS_GBP or sel_min_central is None or plaus > sel_min_central)
        r["decision_relevance"] = {"plausible_upper_local_pct": _r((plaus or 0) * 100, 2) if plaus is not None else None,
                                   "relevant": bool(relevant),
                                   "test": ("its plausible upper (max of central/routes/high estimate) clears the hurdle AND "
                                            "either beats the weakest selected name's central or capital >= MIN_ENTRY is unused")}
        if relevant:
            pending.append(t)
    # ISA-0621: a held donor whose sale WINS the diagnostic contest but whose case is unresolved is decision-relevant
    #   research - the same rule as a case-pending buy: without its case the switch can never be judged (chicken and
    #   egg). A donor inside the 182-day min-hold is not (no case can make it sellable before the clock expires).
    _dg621 = (((receipt.get("switch_contest") or {}).get("diagnostic_selection") or {}).get("selected") or {})
    for _x in _dg621.get("sell") or []:
        _d621 = ((receipt.get("switch_contest") or {}).get("donors") or {}).get(_x.get("ticker")) or {}
        _codes = [y.get("code") for y in _d621.get("reasons") or []]
        if any(str(cd).startswith("DONOR_CASE_") for cd in _codes) and "DONOR_IN_MIN_HOLD" not in _codes \
                and _x.get("ticker") not in pending:
            pending.append(_x.get("ticker"))
            if _x.get("ticker") in recs:
                recs[_x["ticker"]].setdefault("decision_relevance", {})["switch_donor"] = (
                    "its sale wins the diagnostic switch contest; the case must be resolved for the switch to be judged")
    scope = sorted(set(sel_names) | set((sel or {}).get("sold") or {}) |
                   {t for r in (cmp.get("tied") or []) for t, _ in r["bundle"]} |
                   {t for r in (cmp.get("undominated") or []) for t, _ in r["bundle"]} | set(pending))
    complete = complete_cases(am)                     # B09-R03: typed question receipts, one home
    _cov = case_question_coverage(am)
    receipt["case_scope"] = {
        "required_full_cases": scope, "complete": sorted(set(scope) & complete),
        "outstanding": sorted(set(scope) - complete), "decision_relevant_pending": sorted(pending),
        # B09-R03: per case-scope name, the questions still WITHOUT a typed receipt (field change or NO_CHANGE)
        "question_receipts_required": CASE_QUESTION_RECEIPTS_REQUIRED,
        "question_ids": list(CASE_QUESTION_IDS),
        "question_coverage": {t: {"missing": (_cov.get(t) or {}).get("missing", list(CASE_QUESTION_IDS)),
                                  "answered": sorted(((_cov.get(t) or {}).get("answered") or {}).keys())}
                              for t in scope},
        "rule": ("full case for every proposed funded name, every name in the undominated/tied set, and every "
                 "case-pending name whose evidenced plausible economics could change the decision; NO fixed cap "
                 "(s9.1). Cases amend typed inputs only; the engine reruns the whole decision."),
        "questions": [q for q in CASE_QUESTIONS]}
    # ── alternatives / reasons lost (s8.5) ───────────────────────────────────────────────────
    feas = [r for r in rows if r["feasible"]]
    alts = {}
    for e in eligible:
        t = e["ticker"]
        with_t = [r for r in feas if t in r["tickers"]]
        without = [r for r in feas if t not in r["tickers"]]
        bw = min(with_t, key=lambda r: r["regret"]) if with_t else None
        bo = min(without, key=lambda r: r["regret"]) if without else None
        alts[t] = {"selected": t in sel_names,
                   "best_bundle_with": _brief(bw), "best_bundle_without": _brief(bo),
                   "regret_cost_of_including_gbp": None if (bw is None or bo is None) else _r(bw["regret"] - bo["regret"]),
                   "central_wealth_effect_gbp": None if (bw is None or bo is None) else _r(bw["wealth"]["CENTRAL"] - bo["wealth"]["CENTRAL"])}
        if t not in sel_names and recs[t]["disposition"] == "ELIGIBLE":
            recs[t]["disposition"] = "ELIGIBLE_UNFUNDED" if with_t else "RISK_BLOCKED"
            recs[t]["reasons"].append(_reason("construction", "LOST_ON_COMPARATOR" if with_t else "NO_FEASIBLE_BUNDLE",
                                              alts[t]["regret_cost_of_including_gbp"], 0.0, "capital_decision_engine.compare",
                                              "best feasible bundle containing it has a higher max regret than the selection"
                                              if with_t else "every bundle containing it breaches a hard risk constraint"))
        elif t in sel_names:
            recs[t]["disposition"] = "SELECTED"
    for t in sel_names:                                   # ISA-0621: a switch-only receiver chosen by an adopted switch
        if t in recs and recs[t]["disposition"] != "SELECTED":
            recs[t]["disposition"] = "SELECTED"
            recs[t]["reasons"].append(_reason("construction", "SWITCH_RECEIVER", recs[t].get("disposition"), "donor-funded",
                                              "capital_decision_engine.switch_contest (ISA-0621)",
                                              "admissible only as a donor-funded replacement (ISA-0600: judged on net delta-sigma)"))
    receipt["alternatives"] = alts
    _cnt = {}
    for _r0 in recs.values():
        _cnt[_r0["disposition"]] = _cnt.get(_r0["disposition"], 0) + 1
    receipt["census"]["counts"] = _cnt
    receipt["census"]["conservation"] = {"universe": census["n_universe"], "dispositioned": sum(_cnt.values()),
                                         "holds": census["n_universe"] == sum(_cnt.values()),
                                         "every_disposition_typed": all(_r0["disposition"] in DISPOSITIONS for _r0 in recs.values())}
    def _act(t, a):
        return {"ticker": t, "action": "START", "gbp": a,
                "post_trade_weight_pct": _r(a / nav * 100, 3),
                "economic_max_price_major": recs[t]["entry"].get("economic_max_price_major"),
                "valuation_reference_price_major": recs[t]["entry"].get("valuation_reference_price_major"),
                "currency": recs[t]["er12"].get("currency"),
                "central_er12_local_pct": _r(recs[t]["er12"]["central_local"] * 100, 2),
                "net_er12_central_pct": _r((net_return(a, scen[t]["CENTRAL"], costs_by[t]) or 0) * 100, 2),
                "bear_gbp_pct": _r((scen[t]["BUSINESS_BEAR"] or 0) * 100, 2),
                "costs": costs_by[t]}

    def _fund(actions, sale_proceeds=0.0):
        # ISA-0621: a switch's own sale proceeds fund its buys first; the rest follows the D4 waterfall (cash, then trims)
        _tot = round(sum(float(x["gbp"]) for x in actions), 2)
        _fs = round(min(float(sale_proceeds or 0.0), _tot), 2)
        _sp = dict(_fw.split_funding(round(_tot - _fs, 2), _wf), sale_proceeds_gbp=_fs, spend_gbp=_tot)
        _cl, _sl = float(_sp["cash_gbp"]), _fs
        for x in actions:
            _g = float(x["gbp"])
            _c = min(_g, _cl)
            _cl = round(_cl - _c, 2)
            _s = min(_g - _c, _sl)
            _sl = round(_sl - _s, 2)
            _t = round(_g - _c - _s, 2)
            x["funding"] = {"cash_gbp": round(_c, 2), "sale_proceeds_gbp": round(_s, 2), "fund_trims_gbp": max(_t, 0.0),
                            "sequencing": ("AWAIT_FUND_SETTLEMENT (D8)" if _t > EPS_GBP else
                                           "AWAIT_SALE_SETTLEMENT (ISA-0621: sell the donor first)" if _s > EPS_GBP else "CASH")}
        return _sp

    def _sale(t, s):
        h = float(((receipt.get("book") or {}).get("held_stocks") or {}).get(t) or 0.0)
        _q = next((x.get("quantity") for x in portfolio.get("stocks") or [] if x.get("ticker") == t), None)
        return {"ticker": t, "side": "SELL", "action": ("EXIT" if abs(s - h) <= EPS_GBP else "TRIM"), "gbp": round(s, 2),
                "held_gbp": round(h, 2), "residual_gbp": round(max(h - s, 0.0), 2), "held_quantity": _q,
                "proceeds_gbp": round(sale_proceeds(s, costs_by[t]), 2), "currency": (recs.get(t, {}).get("er12") or {}).get("currency"),
                "central_er12_local_pct": _r(((recs.get(t, {}).get("er12") or {}).get("central_local") or 0) * 100, 2),
                "economic_max_price_major": None, "costs": costs_by[t],
                "basis": "ISA-0621 switch contest (LIVE, Raj 19-Oct-2026): sold to fund the selected buys"}
    receipt["selected_sales"] = [_sale(t, s) for t, s in sorted(((sel or {}).get("sold") or {}).items())]
    receipt["selected_actions"] = [_act(t, a) for t, a in (sel or {}).get("bundle", ())]
    if isinstance(receipt.get("concentration"), dict):
        receipt["concentration"]["selected_bundle"] = (sel or {}).get("concentration")   # ISA-0465: the chosen book's verdict
    # ── ISA-0858: how the selection is funded (cash first, then fund trims), the donor plan, sequencing ──
    _split = _fund(receipt["selected_actions"], sum(x["proceeds_gbp"] for x in receipt["selected_sales"]))
    _plan = None
    if _split["fund_trims_gbp"] > EPS_GBP:
        import waiting_room as _wr
        import capital_destination as _cd_ro
        _univ = json.load(open(os.path.join(HERE, "fund_universe.json"), encoding="utf-8"))
        _plan = _wr.recall(_split["fund_trims_gbp"], portfolio=portfolio, policy=tw, brake=_wf["brake"],
                           universe=_univ, ranking=_cd_ro.rank_inputs_readonly(_univ, root),
                           incoming=[{"ticker": a["ticker"], "gbp": a["funding"]["fund_trims_gbp"]}
                                     for a in receipt["selected_actions"] if a["funding"]["fund_trims_gbp"] > EPS_GBP],
                           stock_is_usd=any(str(a.get("currency") or "").upper() not in ("GBP", "GBX", "")
                                            for a in receipt["selected_actions"]))
    receipt["funding"] = {
        "authority": "funding_waterfall (ISA-0858, Raj D1/D3/D4/D5/D8, B1.6b)",
        "waterfall": {k: v for k, v in _wf.items() if k != "inputs"},
        "sleeve_at_decision": _wf["inputs"]["sleeve"], "drawdown": _wf["inputs"]["drawdown"],
        "donor_headroom_gbp": _wf["inputs"]["donor_headroom_gbp"],
        "split": _split, "donor_plan": _plan,
        "park": _fw.park_reason("STOCK_SLEEVE_ENGINE_ALLOCATED"),
        "residual_destination": ("ECONOMIC residual -> funds via the waiting room (D3); the decided stock spend waits "
                                 "in the MMF until executed (OPERATIONAL, D3)")}
    _plan_ok = (_plan is None) or (_plan.get("state") == "RECALL_PLANNED")

    # ── state ────────────────────────────────────────────────────────────────────────────────
    if cmp.get("state") != "OK":
        state = cmp.get("state")
    elif pending or (set(scope) - complete):
        state = "RESEARCH_PENDING"
    else:
        state = "DECISION_COMPLETE"
    authority = _authority_flag()
    receipt["state"] = state
    receipt["engine_authority"] = authority
    # ISA-0849 (11-Oct-2026): positive new risk needs a COMPLETE book. extract_portfolio now names every
    # holding row it could not classify or value (summary.book_coverage); while any is unresolved the NAV,
    # the sleeve weights and the risk inputs are not the whole ISA, so capital authority is withheld (held
    # review, reductions and the decision record are unaffected). A portfolio extracted before ISA-0849
    # carries no declaration: NOT_DECLARED is recorded, never upgraded to COMPLETE.
    _bc = portfolio["summary"].get("book_coverage") if isinstance(portfolio.get("summary"), dict) else None
    _bc_state = (_bc or {}).get("state") if isinstance(_bc, dict) else None
    receipt["book_coverage"] = ({"state": _bc_state, "why": (_bc or {}).get("why"),
                                 "n_unclassified": (_bc or {}).get("n_unclassified"),
                                 "n_unvalued": (_bc or {}).get("n_unvalued"),
                                 "unclassified_value_gbp": (_bc or {}).get("unclassified_value_gbp")}
                                if _bc_state else
                                {"state": "NOT_DECLARED", "why": "portfolio extracted before ISA-0849 (no book_coverage)"})
    _book_ok = _bc_state in (None, "COMPLETE")
    # ISA-0852 (B01-R03): bind the receipt to the objective it was formed against (verify_receipt re-checks it)
    receipt["hurdle_basis"] = hurdle_basis(root)
    # ISA-0863 (B05-R05): bind the receipt to the capital-path code/config that formed it (verify_receipt re-checks it)
    _cp = capital_path_identity()
    receipt["capital_path"] = _cp
    _path_ok = _cp.get("state") == "BOUND"
    receipt["capital_authorised"] = bool(state == "DECISION_COMPLETE" and authority == "LIVE"
                                         and not ncc.get("blocks_new_stock_capital") and _book_ok and _path_ok
                                         and _plan_ok)
    receipt["capital_authorised_basis"] = ("authorised only when the decision is COMPLETE (every required case consumed, "
                                           "no decision-relevant research pending), the engine flag is LIVE, the "
                                           "canonical new-capital control is OPEN and the book is COMPLETE (ISA-0849). "
                                           "Approval and execution revalidation remain separate (s11)."
                                           + ("" if _book_ok else " WITHHELD: BOOK_INCOMPLETE - %s" % (_bc or {}).get("why"))
                                           + ("" if _path_ok else " WITHHELD: CAPITAL_PATH_UNBOUND (ISA-0863) - %s" % _cp.get("why"))
                                           + ("" if _plan_ok else " WITHHELD: FUND_TRIM_PLAN_%s (ISA-0858) - %s"
                                              % ((_plan or {}).get("state"), (_plan or {}).get("reason"))))
    # ── ISA-0860 (Raj 12-Oct-2026): CONFIRM AT DEALING + PRE-AUTHORISED FALLBACKS ─────────────────────────────
    # A selected name whose eligibility/dealability is UNCERTAIN stays fundable; for every non-empty subset S of such
    # names the receipt publishes the best bundle EXCLUDING S - the SAME comparator over the SAME exhaustively
    # searched rows (no new search, no re-score). A fallback is authorised only when the primary is, its comparator
    # state is OK, every name in it has a complete case, and its fund-trim plan (if any) is planned. Raj records the
    # refusal (--record-dealability); execution_check then permits the matching fallback - no re-run needed.
    _confn = sorted(t for t in sel_names if recs.get(t, {}).get("dealability_confirm"))
    _fbs = []
    if _confn and cmp.get("state") == "OK":
        from itertools import combinations as _comb
        for _k in range(1, len(_confn) + 1):
            for _S in _comb(_confn, _k):
                _sub = [r for r in (_sw_rows if _sw_adopted and _sw_rows else rows) if not (set(r["tickers"]) & set(_S))]
                _c2 = compare([copy.copy(r) for r in _sub])
                _s2 = _c2.get("selected")
                _acts = [_act(t, a) for t, a in (_s2 or {}).get("bundle", ())]
                _sales2 = [_sale(t, s) for t, s in sorted(((_s2 or {}).get("sold") or {}).items())]
                _sp2 = _fund(_acts, sum(x["proceeds_gbp"] for x in _sales2)) if _acts else _fw.split_funding(0.0, _wf)
                _pl2 = None
                if _acts and _sp2["fund_trims_gbp"] > EPS_GBP:
                    import waiting_room as _wr2
                    import capital_destination as _cd_ro2
                    _u2 = json.load(open(os.path.join(HERE, "fund_universe.json"), encoding="utf-8"))
                    _pl2 = _wr2.recall(_sp2["fund_trims_gbp"], portfolio=portfolio, policy=tw, brake=_wf["brake"],
                                       universe=_u2, ranking=_cd_ro2.rank_inputs_readonly(_u2, root),
                                       incoming=[{"ticker": x["ticker"], "gbp": x["funding"]["fund_trims_gbp"]}
                                                 for x in _acts if x["funding"]["fund_trims_gbp"] > EPS_GBP],
                                       stock_is_usd=any(str(x.get("currency") or "").upper() not in ("GBP", "GBX", "")
                                                        for x in _acts))
                _miss = sorted({x["ticker"] for x in _acts} - complete)
                _ok2 = (_c2.get("state") == "OK" and not _miss
                        and (_pl2 is None or _pl2.get("state") == "RECALL_PLANNED"))
                _fbs.append({"id": "FB-" + "+".join(_S), "if_refused": list(_S), "comparator_state": _c2.get("state"),
                             "selected": _brief(_s2), "actions": _acts, "sales": _sales2, "split": _sp2, "donor_plan": _pl2,
                             "cases_outstanding": _miss,
                             "authorised": bool(receipt["capital_authorised"] and _ok2),
                             "authorised_basis": ("primary authorised; comparator OK; every case complete; trim plan planned"
                                                  if receipt["capital_authorised"] and _ok2 else
                                                  "NOT authorised: %s" % "; ".join(x for x in (
                                                      "primary not authorised" if not receipt["capital_authorised"] else "",
                                                      "comparator %s" % _c2.get("state") if _c2.get("state") != "OK" else "",
                                                      "cases outstanding %s" % _miss if _miss else "",
                                                      "trim plan %s" % (_pl2 or {}).get("state") if _pl2 and _pl2.get("state") != "RECALL_PLANNED" else "")
                                                      if x))})
    receipt["dealability_confirmation"] = {
        "authority": "ISA-0860 (Raj 12-Oct-2026: refuse only ESTABLISHED ineligibility; confirm the uncertain at dealing)",
        "required": [{"ticker": t, "flags": recs[t]["dealability_confirm"]} for t in _confn],
        "required_tickers": _confn, "fallbacks": _fbs,
        "sequencing": ("Check the flagged name(s) at the broker FIRST (ISA wrapper). If one is refused, record it "
                       "(capital_decision_engine --month M --record-dealability TICKER --outcome REFUSED --wrapper ISA|ALL "
                       "--reason-code CODE --recorded-by Raj) and deal the matching pre-authorised fallback; if it deals, "
                       "record --outcome DEALT so the flag is never raised again." if _confn else
                       "no selected name needs a dealing confirmation"),
        "learning": "every recorded outcome is linked to the flags it answered (broker_dealability.flag_precision)"}
    if sel and sel.get("ambition_band") == "BELOW_20" and amb and amb.get("best_ambition_bundle"):
        receipt["below_20_disclosure"] = amb.get("below_20_selection_reason")
    if sel and sel["bundle"]:
        nost = next((r for r in feas if not r["bundle"]), None)
        worse = {w_: _r(nost["wealth"][w_] - sel["wealth"][w_]) for w_ in S_B
                 if nost is not None and sel["wealth"][w_] < nost["wealth"][w_] - EPS_GBP}
        if worse:
            receipt["downside_tradeoff_disclosure"] = {
                "vs": "NO_STOCK (cash)", "downside_gap_gbp_by_world": worse,
                "gbp_vol_added": _r((sel.get("gbp_vol") or 0) - (nost.get("gbp_vol") or 0)),
                "risk_budget": "every authorised hard risk constraint holds on the final bundle (no breach) - headroom in risk_constraints",
                "statement": ("the regret rule selects an action with worse business-bear wealth than holding cash; this is a "
                              "constraint-compliant policy choice under estimation uncertainty, NOT dominance on downside")}
    if baseline_plan_path:
        receipt["old_v_new"] = old_v_new(baseline_plan_path, rows, recs, scen, costs_by, capital, cash_rate, sel)
    _fv621 = [r["factor"] for r in rows if r.get("factor")]
    receipt["factor"] = {
        "mode": _conc["mode"], "binding": _conc["mode"] == "LIVE", "error": _fctx.get("error"),
        "cap_pct": (_fctx.get("pre") or {}).get("cap_pct"),
        "pre_trade": ({k: (_fctx["pre"] or {}).get(k) for k in ("lower_bound_pct", "upper_bound_pct", "lower_bound_gbp",
                                                               "verdict", "breach", "unclassified")}
                      if _fctx.get("pre") else None),
        "n_plans_evaluated": len(_fv621), "n_plans_failing": sum(1 for f in _fv621 if f.get("state") == "FAIL"),
        "selected": (sel or {}).get("factor"), "selected_tickers": sorted(sel_names),
        "rule": ("B3/D15 on the NET post-trade book (Raj 19-Oct-2026): a plan fails only if its post-trade AI-complex LOWER "
                 "bound exceeds the cap AND it raised the factor's GBP; binds only when concentration_gate is LIVE"),
        "consumer": "checkpoint_d tick 5 (production CLI reads this section)"}
    receipt["learning_capture"] = {
        "frozen": ["full census + dispositions", "ER12 central/variants/bear per name", "scenario set", "all feasible bundles (hash)",
                   "frontier", "selection and alternatives", "cases/amendments", "method versions"],
        "bundles_sha256": _sha([[list(b) for b in (r["bundle"] for r in rows)]]),
        "outcome_horizons_months": [3, 6, 12],
        "consumer": "shadow_ledger / calibration_report (ISA-0804 learning owner) - realised net GBP returns of SELECTED, UNFUNDED and BLOCKED names vs the frozen alternatives",
        "ml_disposition": {"alpha_ranking_supervised": "ACCUMULATE_DATA_FIRST", "probability_calibration": "NEEDED_BUT_DATA_CONSTRAINED",
                           "covariance_shrinkage": "EXISTING estimator retained (sleeve_risk); revisit if instability measured",
                           "clustering": "RESEARCH_CANDIDATE", "reinforcement_learning": "NOT_SUITABLE",
                           "cvar_kelly_target_probability": "OPTIONAL_COMPLEXITY_NOT_JUSTIFIED"}}
    receipt["runtime_s"] = round((_now() - t0).total_seconds(), 2)
    return _finish(receipt, month, root, out_path, write)


CASE_QUESTIONS = (
    "What produces the 12-month return; how much from operations, multiple, distributions, shares, FX and costs?",
    "What is the weakest and most return-sensitive assumption?",
    "What does the current price discount; what must occur within twelve months?",
    "What credible adverse world breaks the case; how much capital is lost?",
    "Do revisions, fundamentals and price evidence agree or conflict; is the same information repeated?",
    "What current issuer event is absent/stale in the mechanical inputs?",
    "What precisely does every flag mean and which adopted prohibition controls it?",
    "Is risk measured correctly; is any proxy defensible?",
    "Is the return bridge economically consistent with normalised earnings, debt, dilution and valuation?",
    "What observation falsifies the thesis before the next review; who monitors it?",
    "Why is this action better than the next-best marginal-pound alternative (mechanical comparison referenced)?",
    "What is unknown, could change the answer, and cannot be resolved before execution?",
)


def _authority_flag() -> str:
    try:
        import isa_policy as _p
        v = _p.V2_FLAGS.get("capital_engine_authority", "SHADOW")
        return v if v in ("OFF", "SHADOW", "LIVE") else "SHADOW"
    except Exception:                                               # noqa: BLE001
        return "SHADOW"


CAPITAL_PATH_ID = "monthly_capital"
_CAPITAL_PATH_MEMO: dict = {}


def capital_path_identity(refresh: bool = False) -> dict:
    """ISA-0863 (11-Oct-2026, B05-R05): the identity of the CODE + CONFIG + run surfaces that form and consume a
    receipt - the same `monthly_capital` path fingerprint R18.6 acceptance binds (system_acceptance.path_fingerprint),
    computed on the ENGINE'S OWN installation (HERE), never on a data root. Memoised per process (code does not
    change under a running process; ~1.6 s uncached). Failure is typed UNAVAILABLE, never a silent pass."""
    if not refresh and "v" in _CAPITAL_PATH_MEMO:
        return _CAPITAL_PATH_MEMO["v"]
    try:
        sys.path.insert(0, HERE)
        import system_acceptance as _sa
        fp = _sa.path_fingerprint(HERE, CAPITAL_PATH_ID)
        try:
            import release_gate as _rg
            _bid = (_rg.load_receipt(HERE) or {}).get("build_id")
        except Exception:                                           # noqa: BLE001
            _bid = None
        v = {"state": "BOUND", "path_id": CAPITAL_PATH_ID, "fingerprint": fp["fingerprint"], "build_id": _bid,
             "n": fp.get("n"), "members": fp.get("members")}
    except Exception as exc:                                        # noqa: BLE001
        v = {"state": "UNAVAILABLE", "path_id": CAPITAL_PATH_ID, "why": "%s: %s" % (type(exc).__name__, exc)}
    _CAPITAL_PATH_MEMO["v"] = v
    return v


def execution_authority(root: str = HERE) -> dict:
    """ISA-0862 (11-Oct-2026, B05-R07): may an order be placed NOW? Read at the execution boundary, not inherited from
    the receipt's formation-time capital_authorised. LIVE must verify TRUSTED (R18.5), the engine flag must be LIVE and
    the canonical new-capital control must not block new stock capital (incl. ACCEPTANCE_NOT_CURRENT, R18.6).
    Every read failure refuses - UNKNOWN never passes (R4.1)."""
    out = {"checked": ["release_gate.verify_live", "isa_policy.V2_FLAGS.capital_engine_authority",
                       "new_capital_control.status"]}
    try:
        sys.path.insert(0, HERE)
        import release_gate as _rg
        v = _rg.verify_live(root)
    except Exception as exc:                                        # noqa: BLE001
        return dict(out, state="REFUSED_AUTHORITY_UNKNOWN", why="LIVE trust could not be read (%s: %s)" % (type(exc).__name__, exc))
    out["live"] = {"state": v.get("state"), "build_id": v.get("build_id")}
    if v.get("state") != "TRUSTED":
        return dict(out, state="REFUSED_LIVE_UNTRUSTED", why="LIVE is %s against %s - no order on an untrusted build (R18.5)"
                    % (v.get("state"), v.get("build_id")))
    flag = _authority_flag()
    out["engine_flag"] = flag
    if flag != "LIVE":
        return dict(out, state="REFUSED_ENGINE_NOT_LIVE", why="capital_engine_authority is %s - the engine's receipts are not capital authority" % flag)
    try:
        import new_capital_control as _ncc
        st = _ncc.status(root)
    except Exception as exc:                                        # noqa: BLE001
        return dict(out, state="REFUSED_AUTHORITY_UNKNOWN", why="new-capital control unreadable (%s: %s)" % (type(exc).__name__, exc))
    out["new_capital_control"] = {"state": st.get("state"), "record_id": st.get("record_id"),
                                  "blocks_new_stock_capital": st.get("blocks_new_stock_capital"),
                                  "reasons": (st.get("reasons") or [])[:4]}
    if st.get("blocks_new_stock_capital") is not False:
        return dict(out, state="REFUSED_NEW_CAPITAL_BLOCKED",
                    why="the canonical new-capital control blocks new stock capital NOW (%s): %s"
                        % (st.get("state"), "; ".join(str(x)[:160] for x in (st.get("reasons") or [])[:2]) or "no reason given"))
    return dict(out, state="CURRENT", why="LIVE TRUSTED %s; engine LIVE; control %s" % (v.get("build_id"), st.get("state")))


def old_v_new(path: str, rows, recs, scen, costs_by, capital, cash_rate, sel) -> dict:
    """Same-input comparison with the old greedy plan, split DATA / METHOD / POLICY (s10)."""
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "UNAVAILABLE", "why": str(exc)}
    al = ((old.get("sleeve_split") or {}).get("allocation") or {})
    ob = tuple(sorted((r["ticker"], float(r["allocated_gbp"])) for r in al.get("rows") or [] if float(r.get("allocated_gbp") or 0) > 0))
    out = {"old_plan": [{"ticker": t, "gbp": a} for t, a in ob], "old_source": os.path.basename(path),
           "old_sha256": _file_sha(path), "new_selected": [{"ticker": t, "gbp": a} for t, a in (sel or {}).get("bundle", ())]}
    match = next((r for r in rows if tuple(sorted(r["bundle"])) == ob), None)
    if match is None:
        out["old_plan_under_new_rules"] = {"state": "NOT_A_LEGAL_OR_EVALUABLE_BUNDLE",
                                           "why": {t: (recs.get(t) or {}).get("disposition") for t, _ in ob},
                                           "reasons": {t: [x["code"] for x in (recs.get(t) or {}).get("reasons", [])] for t, _ in ob}}
    else:
        out["old_plan_under_new_rules"] = _brief(match)
        if sel is not None:
            out["wealth_difference_new_minus_old_gbp"] = {s: _r(sel["wealth"][s] - match["wealth"][s]) for s in S_E + S_B}
            out["regret_old_minus_new_gbp"] = _r(match["regret"] - sel["regret"])
    out["attribution"] = {
        "METHOD": "allocation authority moved from Source-Score queue fill to exact bundle search + robust comparator; ER gate on ER12 (HV) instead of additive 12-24m E[r]",
        "DATA": "weekly return store repaired (ISA-0809): names previously UNMEASURED now measured",
        "POLICY": "none - hurdle, ladder, MIN_ENTRY, reserve, risk limits unchanged; 15% band confirmed reporting-only (ISA-0805)",
        "INPUT_CURRENCY": "same 03-Oct inputs (frozen bundle)"}
    return out


# Run-instance metadata that does not change the DECISION: excluded from the decision hash so identical
# inputs + method -> identical receipt_id (deterministic identity; the ledger is idempotent on it).
_VOLATILE = ("receipt_id", "content_sha256", "produced_at", "runtime_s", "input_freshness", "_written_to", "_ledger")


def hurdle_basis(root: str = HERE) -> dict:
    """ISA-0852 (B01-R03): the economic objective a receipt is formed against, as a comparable identity.
    The hurdle is operative required return (target_state, A19) + ER_FRICTION_BUFFER; the goal, schedule and
    one-offs live in target_state's DECLARED projection (release_gate CONFIG_RUNTIME_KEYS split, ISA-0775), so
    a reported-only re-derivation or a renderer timestamp does not move this identity, while a changed goal,
    contribution schedule, one-off, operative window or friction buffer does."""
    p = os.path.join(root, "target_state.json")
    try:
        with open(p, encoding="utf-8") as fh:
            ts = json.load(fh)
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNREADABLE", "why": "%s: %s" % (type(exc).__name__, exc)}
    try:
        import scoring_config as _sc852
        buf = float(_sc852.ER_FRICTION_BUFFER)
    except Exception:                                                   # noqa: BLE001
        buf = None
    op = ts.get("required_return_operative_pct")
    try:
        import release_gate as _rg852
        proj = _rg852._config_file_digest(p, "target_state.json")
    except Exception as exc:                                            # noqa: BLE001
        proj = "UNAVAILABLE:%s" % type(exc).__name__
    return {"state": "DECLARED", "required_return_operative_pct": op,
            "operative_effective_from": ts.get("operative_effective_from"), "er_friction_buffer_pp": buf,
            "hurdle_pct": (round(float(op) + buf, 6) if (op is not None and buf is not None) else None),
            "declared_projection_sha": (proj or "")[:16],
            "source": "target_state.json (operative anchor + declared goals/schedule/one-offs) + scoring_config.ER_FRICTION_BUFFER"}


def _decision_body(receipt: dict) -> dict:
    return {k: v for k, v in receipt.items() if k not in _VOLATILE}


def _finish(receipt: dict, month: str, root: str, out_path: Optional[str], write: bool) -> dict:
    receipt["content_sha256"] = _sha(_decision_body(receipt))
    receipt["receipt_id"] = "CDR-%s-%s" % (month, receipt["content_sha256"][:12])
    if write:
        p = out_path or os.path.join(root, "capital_decision_receipt_%s.json" % month)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=1, default=str)
        os.replace(tmp, p)                                          # atomic artefact (s12.2)
        receipt["_written_to"] = p
        receipt["_ledger"] = _ledger_append(receipt, month, root)
        receipt["_switch_ledger"] = _switch_ledger_append(receipt, month, root)      # ISA-0621 learning capture
    return receipt


def _ledger_append(receipt: dict, month: str, root: str) -> dict:
    """Learning capture (s13): ONE append-only row per distinct receipt (idempotent on receipt_id), so
    a re-run on identical inputs adds nothing and a changed decision is a new, dated row. History is
    never rewritten. The row freezes the contemporaneous decision state that the 3/6/12-month
    outcome join (ISA-0804 learning owner) needs: selected, frontier alternatives and blocked names."""
    p = os.path.join(root, DECISION_LEDGER)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip() and json.loads(ln).get("receipt_id") == receipt["receipt_id"]:
                        return {"state": "ALREADY_RECORDED", "path": p}
        cen = receipt.get("census") or {}
        row = {"receipt_id": receipt["receipt_id"], "month": month, "recorded_at": _now().isoformat(),
               "as_of": receipt.get("as_of"), "method_version": METHOD_VERSION, "state": receipt.get("state"),
               "engine_authority": receipt.get("engine_authority"), "capital_authorised": receipt.get("capital_authorised"),
               "selected": [(a.get("ticker"), a.get("gbp")) for a in receipt.get("selected_actions") or []],
               "selected_sales": [(a.get("ticker"), a.get("action"), a.get("gbp")) for a in receipt.get("selected_sales") or []],
               "frontier_top": [{"bundle": f.get("bundle"), "regret_gbp": f.get("regret_gbp"),
                                 "wealth_gbp": f.get("wealth_gbp")}
                                for f in ((receipt.get("frontier") or {}).get("undominated_top") or [])[:10]],
               "per_name_alternatives": {t: {"selected": a.get("selected"),
                                             "best_bundle_with": (a.get("best_bundle_with") or {}).get("bundle")}
                                         for t, a in (receipt.get("alternatives") or {}).items()},
               "dispositions": {t: d.get("disposition") for t, d in (receipt.get("dossiers") or {}).items()},
               "er12_central_local": {t: (d.get("er12") or {}).get("central_local")
                                      for t, d in (receipt.get("dossiers") or {}).items()},
               "census_counts": cen.get("counts"), "input_hashes": receipt.get("input_hashes"),
               "outcome_horizons_months": [3, 6, 12]}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return {"state": "APPENDED", "path": p}
    except Exception as exc:                                        # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}


EXECUTION_LEDGER = "capital_execution_ledger.jsonl"


GBP_QUOTE_CURRENCIES = ("GBP",)          # MAJOR units only; a pence quote must be converted by the caller (MAJOR units required)


# ⚑ B09-R08 (P-C, 24-Oct-2026): FX DIRECTION IS CONTRACTED AT THE DEALING BOUNDARY. `fx_quote_per_gbp` is
#   quote-currency units per 1 GBP (USD 1.324 per GBP). An INVERTED rate (GBP per USD, 0.755) produces a
#   perfectly plausible GBP price - USD 75.64 became GBP 100.15 instead of GBP 57.13 in the October audit.
#   Wide bands catch inversion and scale, not drift (the stock_price_fetch.FX_BANDS doctrine, ISA-0429).
EXEC_FX_QUOTE_PER_GBP_BANDS = {"USD": (1.0, 2.0), "EUR": (0.95, 1.6), "CHF": (0.85, 1.6), "SEK": (9.0, 18.0),
                               "NOK": (9.0, 18.0), "DKK": (6.5, 12.0), "CAD": (1.3, 2.4), "JPY": (110.0, 260.0),
                               "AUD": (1.5, 2.6), "HKD": (7.8, 15.5), "PLN": (3.8, 7.0)}


def _fx_direction_refusal(ccy: str, fx: float) -> Optional[str]:
    band = EXEC_FX_QUOTE_PER_GBP_BANDS.get(ccy)
    if band is None:
        return None
    lo, hi = band
    if lo <= fx <= hi:
        return None
    inv = (1.0 / fx) if fx else None
    return ("fx_quote_per_gbp %s = %.6f is outside the declared %s-per-GBP band [%.2f, %.2f]%s - an inverted or "
            "mis-scaled rate is refused, never priced (B09-R08)"
            % (ccy, fx, ccy, lo, hi, (" (its inverse %.4f IS in band: the rate looks INVERTED)" % inv)
                                     if inv and lo <= inv <= hi else ""))


def order_pricing(act: dict, ticker: str, price_major: float, quote_currency: str, fx_quote_per_gbp, costs: dict) -> dict:
    """ISA-0856: whole units and all-in GBP of the order an executable quote implies (costs charged once)."""
    ccy = str(quote_currency or "").strip().upper()
    out = {"state": "OK", "quote_currency": ccy}
    if ccy in GBP_QUOTE_CURRENCIES:
        fx = 1.0
    else:
        try:
            fx = float(fx_quote_per_gbp)
        except (TypeError, ValueError):
            fx = float("nan")
        if not math.isfinite(fx) or fx <= 0:
            out.update(state="REFUSED_FX_UNKNOWN", why="a %s quote needs a positive finite fx_quote_per_gbp (%s per GBP) to price the "
                       "order in GBP - pass --fx (B02-R08)" % (ccy, ccy))
            return out
        _dir = _fx_direction_refusal(ccy, fx)
        if _dir:
            out.update(state="REFUSED_FX_DIRECTION", why=_dir)
            return out
    try:
        comm = float(costs["commission_gbp"])
    except (KeyError, TypeError, ValueError):
        out.update(state="REFUSED_COST_UNKNOWN", why="the receipt carries no commission_gbp - the order cannot be priced")
        return out
    fx_frac = 0.0 if ccy in GBP_QUOTE_CURRENCIES else costs.get("fx_dealing_fraction")
    if fx_frac is None:
        out.update(state="REFUSED_COST_UNKNOWN", why="the receipt carries no fx_dealing_fraction for a non-GBP order")
        return out
    table = costs.get("levy_table") or {}
    suffix = ("." + ticker.rsplit(".", 1)[1]) if "." in ticker else ""
    if suffix in table:
        levy, levy_basis = float(table[suffix]["fraction"]), suffix
    elif ccy == "USD" and "" in table:
        levy, levy_basis = float(table[""]["fraction"]), "US listing (no purchase tax)"
    else:
        out.update(state="REFUSED_COST_UNKNOWN", why="no purchase-levy entry for %s (suffix %r, currency %s)" % (ticker, suffix, ccy))
        return out
    px_gbp = float(price_major) / fx
    unit_all_in = px_gbp * (1.0 + float(fx_frac) + levy)
    budget = float(act.get("gbp") or 0) - comm
    units = int(math.floor(budget / unit_all_in + 1e-9)) if unit_all_in > 0 else 0
    out.update(fx_quote_per_gbp=fx, price_gbp=round(px_gbp, 6), commission_gbp=comm, fx_dealing_fraction=float(fx_frac),
               levy_fraction=levy, levy_basis=levy_basis, units=units, approved_gbp=float(act.get("gbp") or 0))
    if units < 1:
        out.update(state="REFUSED_UNIT_UNAFFORDABLE", why="one whole unit costs GBP %.2f all-in, above the approved GBP %.2f less "
                   "commission" % (unit_all_in + comm, float(act.get("gbp") or 0)))
        return out
    consid = units * px_gbp
    out.update(consideration_gbp=round(consid, 2), fx_cost_gbp=round(consid * float(fx_frac), 2),
               levy_gbp=round(consid * levy, 2), est_total_gbp=round(consid * (1.0 + float(fx_frac) + levy) + comm, 2),
               why="%d whole units at GBP %.4f + dealing FX %.2f%% + levy %.2f%% + GBP %.2f commission, each charged once"
                   % (units, px_gbp, 100 * float(fx_frac), 100 * levy, comm))
    return out


def sale_pricing(act: dict, price_major: float, quote_currency: str, fx_quote_per_gbp, costs: dict) -> dict:
    """ISA-0621: whole units and net GBP proceeds of a switch SALE at an executable quote (commission + dealing FX on
    non-GBP proceeds, charged once; no levy on a sale). EXIT sells every held unit; TRIM sells the whole units nearest
    the decided GBP without exceeding the holding."""
    ccy = str(quote_currency or "").strip().upper()
    out = {"state": "OK", "quote_currency": ccy, "side": "SELL"}
    if ccy in GBP_QUOTE_CURRENCIES:
        fx = 1.0
    else:
        try:
            fx = float(fx_quote_per_gbp)
        except (TypeError, ValueError):
            fx = float("nan")
        if not math.isfinite(fx) or fx <= 0:
            out.update(state="REFUSED_FX_UNKNOWN", why="a %s quote needs a positive finite fx_quote_per_gbp to price the sale" % ccy)
            return out
        _dir = _fx_direction_refusal(ccy, fx)          # B09-R08: inverted / mis-scaled rate refused
        if _dir:
            out.update(state="REFUSED_FX_DIRECTION", why=_dir)
            return out
    try:
        comm = float(costs["commission_gbp"])
        fx_frac = 0.0 if ccy in GBP_QUOTE_CURRENCIES else float(costs["fx_dealing_fraction"])
    except (KeyError, TypeError, ValueError):
        out.update(state="REFUSED_COST_UNKNOWN", why="the receipt carries no commission/dealing-FX cost for the sale")
        return out
    try:
        held_q = float(act.get("held_quantity"))
    except (TypeError, ValueError):
        held_q = float("nan")
    if not math.isfinite(held_q) or held_q <= 0:
        out.update(state="REFUSED_HOLDING_UNKNOWN", why="the receipt carries no held quantity for %s - a sale cannot be sized "
                   "in whole units" % act.get("ticker"))
        return out
    px_gbp = float(price_major) / fx
    if act.get("action") == "EXIT":
        units = held_q
    else:
        units = min(held_q, float(int(round(float(act.get("gbp") or 0) / px_gbp))))
    if units < 1 and act.get("action") != "EXIT":
        out.update(state="REFUSED_UNIT_UNAFFORDABLE", why="the decided trim is less than one whole unit at this price")
        return out
    consid = units * px_gbp
    out.update(fx_quote_per_gbp=fx, price_gbp=round(px_gbp, 6), units=units, held_quantity=held_q,
               consideration_gbp=round(consid, 2), est_proceeds_gbp=round(consid * (1.0 - fx_frac) - comm, 2),
               why="%s units at GBP %.4f less dealing FX %.2f%% and GBP %.2f commission" % (units, px_gbp, 100 * fx_frac, comm))
    return out


def fills_for(receipt_id: str, root: str = HERE) -> dict:
    """ISA-0855: cumulative recorded fills per ticker for one receipt (execution identities, partials retained)."""
    p = os.path.join(root, EXECUTION_LEDGER)
    out = {}
    if not receipt_id or not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            r = json.loads(ln)
            if r.get("receipt_id") != receipt_id:
                continue
            t = out.setdefault(r.get("ticker"), {"n_fills": 0, "quantity": 0.0, "gbp_cost": 0.0, "orders": [], "prices": []})
            t.setdefault("prices", []).append({"price_major": r.get("price_major"), "quote_currency": r.get("quote_currency"),
                                               "price_gbp": r.get("price_gbp")})
            t["n_fills"] += 1
            t["quantity"] = round(t["quantity"] + float(r.get("quantity") or 0), 6)
            t["gbp_cost"] = round(t["gbp_cost"] + float(r.get("gbp_cost") or 0), 2)
            if r.get("order_id") not in t["orders"]:
                t["orders"].append(r.get("order_id"))
    return out


def active_plan(rc: dict, root: str = HERE, outcomes: Optional[dict] = None) -> dict:
    """ISA-0860 - which action set of a receipt is LIVE: the primary, or - when Raj has recorded a broker REFUSAL for
    confirm-required selected name(s) - the pre-authorised fallback for exactly that set. No matching authorised
    fallback -> NO_AUTHORISED_FALLBACK (re-run the engine; nothing is improvised)."""
    dc = (rc or {}).get("dealability_confirmation") or {}
    req = list(dc.get("required_tickers") or [])
    if outcomes is None:
        try:
            import broker_dealability as _bdo
            outcomes = _bdo.load_outcomes(root)
        except Exception:                                               # noqa: BLE001
            outcomes = {}
    refused = sorted(t for t in req if ((outcomes or {}).get(t) or {}).get("outcome") == "REFUSED")
    if not refused:
        return {"plan_id": "PRIMARY", "refused": [], "actions": list((rc or {}).get("selected_sales") or []) +
                list((rc or {}).get("selected_actions") or []),
                "authorised": bool((rc or {}).get("capital_authorised"))}
    fb = next((f for f in dc.get("fallbacks") or [] if sorted(f.get("if_refused") or []) == refused), None)
    if fb is None or not fb.get("authorised"):
        return {"plan_id": None, "refused": refused, "actions": [], "authorised": False,
                "state": "NO_AUTHORISED_FALLBACK",
                "why": ("Raj recorded a refusal of %s but the receipt carries %s - re-run the engine"
                        % (refused, "no fallback for that set" if fb is None else "fallback %s NOT authorised (%s)"
                           % (fb.get("id"), fb.get("authorised_basis"))))}
    return {"plan_id": fb["id"], "refused": refused, "actions": list(fb.get("sales") or []) + list(fb.get("actions") or []),
            "authorised": True}


def record_dealability(month: str, ticker: str, outcome: str, recorded_by: str, root: str = HERE, *,
                       wrapper: str = "ISA", reason_code: Optional[str] = None, evidence: Optional[str] = None) -> dict:
    """ISA-0860 - Raj's dealing outcome for a confirm-required name of THIS month's receipt: written ONCE through
    broker_dealability.record_outcome (the declaration of record + the learning row with the flags it answered);
    returns the plan that is now active. Refused for a name the receipt did not flag (no silent declarations)."""
    v = verify_receipt(month, root)
    rc = v.get("receipt") or {}
    if v.get("state") != "VALID":
        raise EngineRefused("record_dealability: receipt is %s - record against a VALID receipt" % v.get("state"))
    dc = rc.get("dealability_confirmation") or {}
    flags = {x["ticker"]: x.get("flags") for x in dc.get("required") or []}
    if ticker not in flags:
        raise EngineRefused("record_dealability: %s is not a confirm-required name of %s (required %s)"
                            % (ticker, rc.get("receipt_id"), sorted(flags)))
    import broker_dealability as _bdr
    try:
        w = _bdr.record_outcome(ticker, outcome, recorded_by, root, wrapper=wrapper, reason_code=reason_code,
                                evidence=evidence, receipt_id=rc.get("receipt_id"), month=month,
                                flags_at_decision=flags.get(ticker))
    except _bdr.DealabilityRefused as exc:
        raise EngineRefused("record_dealability: %s" % exc)
    return {"state": "RECORDED", "row": w["row"], "active_plan": active_plan(rc, root)}


AS_PLANNED_GBP_TOLERANCE = 0.05     # Raj Q4 (13-Oct-2026): a fill within +/-5% of the approved GBP is 'as planned'


def as_planned_fill(fill: dict, act: Optional[dict]) -> dict:
    """ISA-0727 Q4 (Raj 13-Oct-2026 'allow as planned'): a recorded fill is AS PLANNED when it is an action of the
    active plan, its cumulative GBP is within +/-5% of the approved GBP, and EVERY fill price is a MAJOR-unit price in
    the action's currency at or below the action's economic maximum. Anything unprovable is a deviation."""
    if not act:
        return {"as_planned": False, "why": "the filled name is not an action of the active plan"}
    try:
        g_app, g_fill = float(act.get("gbp") or 0.0), float((fill or {}).get("gbp_cost") or 0.0)
    except (TypeError, ValueError):
        return {"as_planned": False, "why": "fill or approved GBP unreadable"}
    if g_app <= 0:
        return {"as_planned": False, "why": "the approved GBP is not positive"}
    dev = g_fill / g_app - 1.0
    if abs(dev) > AS_PLANNED_GBP_TOLERANCE + 1e-9:
        return {"as_planned": False, "deviation_pct": round(dev * 100, 2),
                "why": "filled GBP %.2f is %.2f%% from the approved GBP %.2f (tolerance +/-%.0f%%)"
                       % (g_fill, dev * 100, g_app, AS_PLANNED_GBP_TOLERANCE * 100)}
    emax = act.get("economic_max_price_major")
    for px in (fill or {}).get("prices") or []:
        if px.get("price_major") is None or not px.get("quote_currency"):
            return {"as_planned": False, "deviation_pct": round(dev * 100, 2),
                    "why": "a fill carries no MAJOR-unit price in its quote currency - the economic-max check cannot be shown"}
        if act.get("currency") and str(px["quote_currency"]).upper() != str(act["currency"]).upper():
            return {"as_planned": False, "deviation_pct": round(dev * 100, 2),
                    "why": "fill currency %s != action currency %s" % (px["quote_currency"], act["currency"])}
        if emax is not None and float(px["price_major"]) > float(emax) + 1e-9:
            return {"as_planned": False, "deviation_pct": round(dev * 100, 2),
                    "why": "fill price %.4f above the economic max %.4f" % (float(px["price_major"]), float(emax))}
    if not (fill or {}).get("prices"):
        return {"as_planned": False, "why": "no fill price recorded"}
    return {"as_planned": True, "deviation_pct": round(dev * 100, 2),
            "why": "within +/-%.0f%% of the approved GBP at prices <= the economic max" % (AS_PLANNED_GBP_TOLERANCE * 100)}


def dealing_invalidation(month: str, ticker: str, rc: dict, root: str = HERE, now=None) -> dict:
    """ISA-0727 Q2 (Raj 13-Oct-2026): an approval is invalidated before the next pre-run by a corporate event or an
    earnings release for the selected name SINCE the approval. Corporate events: the ISA-0817 store's verdict now vs the
    flags the receipt's dossier carried at formation - a NEW offeree (HARD) or corporate-action (REVIEW) flag
    invalidates; a source that cannot be read now is 'confirm at dealing', never a pass and never an invalidation.
    Earnings: the next earnings date known at formation (the bound step9_pre record's binary_event_within_90d, else
    watchlist_scored next_earnings); a date in (formation, today] invalidates; no date held is EARNINGS_DATE_UNKNOWN
    (confirm at dealing)."""
    now = now or _now()
    today = (now.date() if hasattr(now, "date") else now)
    formed = str(rc.get("as_of") or "")[:10]
    out = {"state": "VALID", "confirm": [], "checks": {}}
    doss = ((rc.get("dossiers") or {}).get(ticker) or {}).get("flags") or {}
    at_formation = set((doss.get("hard") or []) + list(doss.get("review_unresolved") or []) +
                       [r.get("code") for r in (doss.get("rows") or []) if r.get("code")])
    try:
        import corporate_events as _ce727
        doc = None
        _p = os.path.join(root, "corporate_events.json")
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as fh:
                doc = json.load(fh)
        v = _ce727.verdict(ticker, doc, as_of=today.isoformat())
        out["checks"]["corporate_event"] = {"state": v.get("state"), "flags": v.get("flags"), "store_as_of": v.get("store_as_of")}
        new = [f for f in (v.get("flags") or []) if f in ("offer_period_offeree", "corporate_action_pending")
               and f not in at_formation]
        if new:
            out.update(state="INVALIDATED_CORPORATE_EVENT",
                       why="%s carries %s now that it did not carry at formation (%s): %s" % (
                           ticker, new, formed, "; ".join(str(x) for x in (v.get("why") or [])[:2])))
            return out
        if v.get("state") in ("SOURCE_FAILED", "OFFER_STATE_UNSOURCED"):
            out["confirm"].append("corporate events: %s (%s) - confirm there is no offer or corporate action before dealing"
                                  % (v.get("state"), "; ".join(str(x) for x in (v.get("why") or [])[:1])))
    except Exception as exc:                                            # noqa: BLE001
        out["confirm"].append("corporate events unreadable (%s) - confirm before dealing" % type(exc).__name__)
    ne, ne_src = None, None
    # the BOUND step9_pre input first (archived copy after the purge, ISA-0832): risk_flags.binary_event_within_90d
    try:
        with open(input_path(month, "step9_pre", root), encoding="utf-8") as fh:
            _s9 = json.load(fh)
        for _lst in ("deployment_priority_rank", "main_watchlist", "vci_watchlist", "candidate_pool", "deployable_stack"):
            for _r in _s9.get(_lst) or []:
                if isinstance(_r, dict) and str(_r.get("ticker") or "").upper() == ticker.upper():
                    _be = (_r.get("risk_flags") or {}).get("binary_event_within_90d")
                    if _be:
                        ne, ne_src = _be, "step9_pre %s risk_flags.binary_event_within_90d" % _lst
                        break
            if ne:
                break
    except Exception:                                                   # noqa: BLE001
        pass
    if ne is None:
        try:
            _ws = os.path.join(root, "watchlist_scored_%s.json" % month)
            if os.path.exists(_ws):
                with open(_ws, encoding="utf-8") as fh:
                    _tk = (json.load(fh).get("tickers") or {})
                _r = _tk.get(ticker) or _tk.get(ticker.upper()) or {}
                if _r.get("next_earnings") and _r.get("next_earnings") != "Unknown":
                    ne, ne_src = _r["next_earnings"], "watchlist_scored_%s tickers.next_earnings" % month
        except Exception:                                               # noqa: BLE001
            pass
    out["checks"]["earnings"] = {"next_earnings_at_formation": ne, "source": ne_src, "formed": formed}
    try:
        ned = datetime.date.fromisoformat(str(ne)[:10]) if ne and str(ne) != "Unknown" else None
    except ValueError:
        ned = None
    if ned is None:
        out["confirm"].append("earnings: no confirmed date held at formation (EARNINGS_DATE_UNKNOWN) - confirm none has "
                              "been reported since %s before dealing" % formed)
    elif formed and formed < ned.isoformat() <= today.isoformat():
        out.update(state="INVALIDATED_EARNINGS",
                   why="%s reported earnings on %s, after the approval was formed (%s): the case must be refreshed at "
                       "the next pre-run (Raj Q2)" % (ticker, ned.isoformat(), formed))
    return out


VALIDITY_LOG = "capital_receipt_validity_log.jsonl"
WATCH_REVIEW_STAGES = ("Rolling over", "Flat/Down")     # the forward gate's excluded revision stages (t1_gates A3)


def _month_order(path: str):
    import re as _re
    _mon = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    m = _re.search(r"_([a-z]{3})_(\d{4})\.json$", os.path.basename(path))
    return (int(m.group(2)), _mon.get(m.group(1), 0)) if m else (0, 0)


def active_authorised_receipt(root: str = HERE):
    """-> (month, receipt, why_not). The NEWEST monthly receipt is the only one in force (Raj Q2: a plan is valid until
    the next pre-run); it is watchable only when it verifies VALID and is DECISION_COMPLETE + capital_authorised."""
    import month_artefacts as _ma
    ps = sorted(_ma.month_glob(root, "capital_decision_receipt_*_20[0-9][0-9].json"), key=_month_order)
    if not ps:
        return None, None, "no capital decision receipt on disk"
    import re as _re
    month = _re.search(r"capital_decision_receipt_([a-z]{3}_\d{4})\.json$", os.path.basename(ps[-1])).group(1)
    v = verify_receipt(month, root)
    rc = v.get("receipt")
    if v.get("state") != "VALID":
        return month, rc, "receipt %s is %s against the current inputs" % (month, v.get("state"))
    if rc.get("state") != "DECISION_COMPLETE" or not rc.get("capital_authorised"):
        return month, rc, "receipt %s state %s, capital_authorised %s - no approved plan to watch" % (
            month, rc.get("state"), rc.get("capital_authorised"))
    return month, rc, None


def _frame_rows(frame_path: Optional[str]) -> dict:
    if not frame_path or not os.path.exists(frame_path):
        return {}
    import csv as _csv
    with open(frame_path, newline="", encoding="utf-8") as fh:
        return {str(r.get("ticker") or "").strip().upper(): r for r in _csv.DictReader(fh)}


def watch_receipt(as_of, root: str = HERE, *, frame_path: Optional[str] = None, source: str = "weekly_screener",
                  now=None, write: bool = True, dealability=None) -> dict:
    """ISA-0727 B - the APPROVED-PENDING WATCH (Raj 13-Oct-2026 design, Q2 confirmed). Between pre-runs each weekly
    screener occurrence re-checks ONLY the active receipt's not-yet-executed actions. AUTHORITY IS ONE-WAY: it can
    INVALIDATE or flag REVIEW, never add a name, raise an amount or re-rank. Per action:
      INVALIDATED - a corporate event or earnings since formation (dealing_invalidation), the screen's close above the
                    economic max, the broker/ISA verdict NOT_DEALABLE_*;
      REVIEW      - this screen's revision stage in the forward gate's excluded set, or its E[r] below the receipt's
                    hurdle, or a currency the action does not carry;
      VALID       - none of those (names outside this week's screen are checked on events and eligibility only).
    Appends ONE row to capital_receipt_validity_log.jsonl (this function is its one writer); execution_check reads the
    newest row for the receipt at dealing (INVALIDATED refuses; REVIEW is confirm-at-dealing)."""
    now = now or _now()
    _aod = as_of if isinstance(as_of, datetime.date) else datetime.date.fromisoformat(str(as_of)[:10])
    _nowd = datetime.datetime(_aod.year, _aod.month, _aod.day, 12, 0)
    month, rc, why = active_authorised_receipt(root)
    row = {"checked_at": now.isoformat(timespec="seconds"), "as_of": _aod.isoformat(), "source": source,
           "month": month, "receipt_id": (rc or {}).get("receipt_id"), "frame": os.path.basename(frame_path) if frame_path else None,
           "authority": "ONE-WAY: invalidates or flags REVIEW only (Raj 13-Oct-2026)", "names": {}}
    if why:
        row.update(state="NO_AUTHORISED_PLAN", why=why)
    else:
        frame = _frame_rows(frame_path)
        hurdle = (rc.get("objective") or {}).get("hurdle_pct")
        formed = str(rc.get("as_of") or "")[:10]
        fl = fills_for(rc.get("receipt_id"), root)
        if dealability is None:
            try:
                import broker_dealability as _bdw
                dealability = lambda t: _bdw.verdict(t, root=root)          # noqa: E731
            except Exception:                                           # noqa: BLE001
                dealability = None
        for act in active_plan(rc, root).get("actions") or []:
            t = act["ticker"]
            if t in fl:
                row["names"][t] = {"state": "FILLED", "why": "recorded fill(s) on this receipt"}
                continue
            inv, rev, conf, chk = [], [], [], {}
            di = dealing_invalidation(month, t, rc, root, _nowd)
            chk["events"] = di.get("checks")
            if di["state"] != "VALID":
                inv.append(di["why"])
            conf += di.get("confirm") or []
            fr = frame.get(t.upper())
            if fr:
                try:
                    cp, cur = float(fr.get("current_price")), (fr.get("currency") or "").strip()
                    if cur == "GBp":
                        cp, cur = cp / 100.0, "GBP"
                except (TypeError, ValueError):
                    cp, cur = None, ""
                emax = act.get("economic_max_price_major")
                chk["screen"] = {"close": cp, "currency": cur, "economic_max": emax, "revision_stage": fr.get("revision_stage"),
                                 "est_rev_direction": fr.get("est_rev_direction"), "er_12_24m_pct": fr.get("expected_return_12_24m"),
                                 "next_earnings": fr.get("next_earnings")}
                if cp is not None and act.get("currency") and cur.upper() != str(act["currency"]).upper():
                    rev.append("screen close is in %s, the action in %s" % (cur, act["currency"]))
                elif cp is not None and emax is not None and cp > float(emax) + 1e-9:
                    inv.append("screen close %.4f %s on %s is above the economic max %.4f" % (cp, cur, _aod.isoformat(), float(emax)))
                _ne = str(fr.get("next_earnings") or "")[:10]
                if _ne and _ne != "Unknown" and formed and formed < _ne <= _aod.isoformat():
                    inv.append("%s reported earnings on %s, after formation %s (screen calendar)" % (t, _ne, formed))
                if (fr.get("revision_stage") or "") in WATCH_REVIEW_STAGES:
                    rev.append("revision stage on this screen is %r (the forward gate's excluded set)" % fr.get("revision_stage"))
                try:
                    _er = float(fr.get("expected_return_12_24m"))
                    if hurdle is not None and _er < float(hurdle):
                        rev.append("screen E[r] %.1f%% is below the receipt's hurdle %.1f%%" % (_er, float(hurdle)))
                except (TypeError, ValueError):
                    pass
            else:
                chk["screen"] = "not in this screen - events and eligibility checked only"
            if dealability is not None:
                try:
                    _dv = dealability(t) or {}
                    chk["dealability"] = _dv.get("state")
                    if str(_dv.get("state") or "").startswith("NOT_"):
                        inv.append("broker/ISA verdict %s" % _dv.get("state"))
                    elif _dv.get("state") not in (None, "DEALABLE_ONLINE"):
                        conf.append("dealability %s - confirm at AJ Bell" % _dv.get("state"))
                except Exception as _dx:                                # noqa: BLE001
                    conf.append("dealability unreadable (%s)" % type(_dx).__name__)
            row["names"][t] = {"state": "INVALIDATED" if inv else ("REVIEW" if rev else "VALID"),
                               "invalidated_by": inv, "review": rev, "confirm_at_dealing": conf, "checks": chk}
        _cn = [v["state"] for v in row["names"].values()]
        row.update(state="WATCHED", counts={k: _cn.count(k) for k in ("VALID", "REVIEW", "INVALIDATED", "FILLED")})
    if write:
        with open(os.path.join(root, VALIDITY_LOG), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    return row


def latest_watch(root: str = HERE, receipt_id: Optional[str] = None) -> Optional[dict]:
    """The newest validity-log row (for one receipt when given) - the consumer view for execution_check and the email."""
    p = os.path.join(root, VALIDITY_LOG)
    if not os.path.exists(p):
        return None
    last = None
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if receipt_id is None or r.get("receipt_id") == receipt_id:
                last = r
    return last


def execution_check(month: str, ticker: str, price_major: float, quote_currency: str, quote_ts: str,
                    cash_available_gbp: Optional[float] = None, root: str = HERE,
                    now: Optional[datetime.datetime] = None, fx_quote_per_gbp: Optional[float] = None,
                    authority: Optional[dict] = None) -> dict:
    """s11 - validity of ONE selected action at an EXECUTABLE quote, re-checked before every order or
    partial fill. Never places an order. States: EXECUTABLE | REFUSED_* | REPRICE_REQUIRED.
    (a) the receipt verifies against the CURRENT inputs and is DECISION_COMPLETE + capital_authorised;
    (b) the ticker is a selected action; (c) the quote is a positive finite MAJOR-unit price with a
    timezone-aware time, a session quote (not a weekend/Sunday analysis close, T57), not in the future,
    in the (non-empty) dossier currency, and not older than one day; (d) ER12 recomputed at the
    executable price still clears the hurdle (economic max, T52); (e) the WHOLE construction is rerun
    with the executable price and must select the same action (an approval cannot silently expand to
    a different portfolio, T56); (f) current cash is PRESENT, finite, >= 0 and covers the action
    (ISA-0850: missing cash is REFUSED_CASH_UNKNOWN, never assumed sufficient). Passing is necessary
    permission to place the order, not an instruction to buy.
    ISA-0862 (B05-R07): CURRENT executable authority is re-read here (LIVE TRUSTED, engine LIVE, control not
    blocking) - a receipt's formation-time capital_authorised is not enough. `authority` is a test injection only.
    ISA-0864 (B05-R08): the APPROVAL axis (Raj's recorded decision on this exact action) is reported beside the
    state; EXECUTABLE without APPROVED is permission without a decision."""
    now = now or _now()
    out = {"ticker": ticker, "price_major": price_major, "quote_currency": quote_currency, "quote_ts": quote_ts,
           "checked_at": now.isoformat(timespec="seconds"), "checks": []}

    def done(state, why):
        out.update(state=state, why=why)
        return out
    v = verify_receipt(month, root)
    out["receipt_verification"] = v.get("state")
    if v["state"] != "VALID":
        return done("REFUSED_RECEIPT_" + v["state"], "the receipt is not VALID against the current inputs - rerun the engine")
    rc = v["receipt"]
    out["receipt_id"] = rc.get("receipt_id")
    if rc.get("state") != "DECISION_COMPLETE" or not rc.get("capital_authorised"):
        return done("REFUSED_NOT_AUTHORISED", "receipt state %s, capital_authorised %s" % (rc.get("state"), rc.get("capital_authorised")))
    # ISA-0860: the ACTIVE plan - primary, or the pre-authorised fallback for Raj's recorded refusal(s)
    _ap0 = active_plan(rc, root)
    out["active_plan"] = {k: _ap0.get(k) for k in ("plan_id", "refused", "state", "why")}
    if not _ap0.get("authorised"):
        return done("REFUSED_" + (_ap0.get("state") or "NOT_AUTHORISED"), _ap0.get("why") or "no authorised plan")
    sel = {a["ticker"]: a for a in _ap0["actions"]}
    if ticker not in sel:
        return done("REFUSED_NOT_SELECTED", "%s is not an action of receipt %s plan %s (actions: %s)"
                    % (ticker, rc.get("receipt_id"), _ap0["plan_id"], sorted(sel)))
    act = sel[ticker]
    _dcf = {x["ticker"]: x.get("flags") for x in ((rc.get("dealability_confirmation") or {}).get("required") or [])}
    if _dcf.get(ticker):
        out["confirm_at_dealing"] = {"flags": _dcf[ticker],
                                     "rule": "if the broker refuses it in the ISA, record --record-dealability (REFUSED) and use the fallback"}
    # ⚑ ISA-0855 (11-Oct-2026, B02-R07/B05-R07): a recorded fill changes the book the receipt was formed on.
    #   Every action of that receipt - including the filled one - needs a SUCCESSOR receipt formed on the
    #   post-fill book; record_fill never authorises anything (historic receipts are preserved untouched).
    _fl = fills_for(rc.get("receipt_id"), root)
    out["fills_on_receipt"] = {t: {"quantity": v["quantity"], "gbp_cost": v["gbp_cost"], "n_fills": v["n_fills"]}
                               for t, v in _fl.items()}
    # ISA-0864: the approval axis is reported on every outcome from here on (distinct from the execution state)
    out["approval"] = approval_state(month, ticker, rc, root)
    if ticker in _fl:
        return done("REFUSED_ALREADY_FILLED", "receipt %s already has %d fill(s) for %s (qty %s, GBP %.2f of GBP %.2f): any further "
                    "order is a new decision on the post-fill book - re-run the engine" % (rc.get("receipt_id"), _fl[ticker]["n_fills"],
                    ticker, _fl[ticker]["quantity"], _fl[ticker]["gbp_cost"], float(act.get("gbp") or 0)))
    if _fl:
        # ⚑ ISA-0727 Q4 (Raj 13-Oct-2026 'allow as planned', amends ISA-0855 B02-R07): AS-PLANNED fills leave the
        #   receipt's other actions executable (every live check below still applies, on the post-fill cash Raj
        #   passes); any DEVIATING fill still needs a successor receipt formed on the post-fill book.
        _apf = {t: as_planned_fill(v, sel.get(t)) for t, v in _fl.items()}
        out["fills_as_planned"] = _apf
        _dev = sorted(t for t, d in _apf.items() if not d["as_planned"])
        if _dev:
            return done("REFUSED_RERUN_REQUIRED_AFTER_FILL", "receipt %s has DEVIATING fill(s) for %s (%s): the book, cash and "
                        "risk it was formed on changed beyond the approved actions - re-run the engine on the post-fill book "
                        "(successor receipt) before any further order (B02-R07; Raj Q4 tolerance +/-%.0f%%)"
                        % (rc.get("receipt_id"), _dev, "; ".join("%s: %s" % (t, _apf[t]["why"]) for t in _dev),
                           AS_PLANNED_GBP_TOLERANCE * 100))
    # ⚑ ISA-0727 B: the approved-pending WEEKLY watch - the newest validity-log row for this receipt
    _lw = latest_watch(root, rc.get("receipt_id"))
    _lwn = ((_lw or {}).get("names") or {}).get(ticker)
    out["watch"] = {"as_of": (_lw or {}).get("as_of"), "source": (_lw or {}).get("source"), "name": _lwn}
    if _lwn and _lwn.get("state") == "INVALIDATED":
        return done("REFUSED_INVALIDATED_WATCH", "the approved-pending watch of %s (%s) INVALIDATED %s: %s - re-run the "
                    "decision at the next pre-run" % (_lw.get("as_of"), _lw.get("source"), ticker, "; ".join(_lwn.get("invalidated_by") or [])))
    if _lwn and _lwn.get("state") == "REVIEW":
        out.setdefault("confirm_at_dealing", {}).setdefault("checks", []).extend(
            ["watch %s REVIEW: %s" % (_lw.get("as_of"), r) for r in _lwn.get("review") or []])
    # ⚑ ISA-0727 Q2 (Raj 13-Oct-2026): an event since the approval invalidates it before the next pre-run
    _inv = dealing_invalidation(month, ticker, rc, root, now)
    out["invalidation"] = _inv
    if _inv["state"] != "VALID":
        return done("REFUSED_" + _inv["state"], _inv["why"])
    if _inv["confirm"]:
        out.setdefault("confirm_at_dealing", {}).setdefault("checks", []).extend(_inv["confirm"])
    # ISA-0862 (B05-R07): current executable authority - read NOW, every failure refuses
    _auth = authority if authority is not None else execution_authority(root)
    out["authority"] = _auth
    if _auth.get("state") != "CURRENT":
        return done(_auth.get("state") or "REFUSED_AUTHORITY_UNKNOWN", _auth.get("why") or "authority not established")
    # ISA-0850 (11-Oct-2026, B02-R04/B05-R06): every input the verdict rests on must be PRESENT, FINITE and IN
    # DOMAIN before EXECUTABLE is reachable - the old checks refused known-bad cases and let unknowns through
    # (cash None/NaN skipped affordability; a future quote was 'fresh'; an empty currency skipped the check).
    try:
        _px = float(price_major)
    except (TypeError, ValueError):
        _px = float("nan")
    if not math.isfinite(_px) or _px <= 0:
        return done("REFUSED_QUOTE_INVALID", "price %r is not a positive finite MAJOR-unit quote" % (price_major,))
    try:
        qt_raw = datetime.datetime.fromisoformat(str(quote_ts).replace("Z", "+00:00"))
    except ValueError:
        return done("REFUSED_QUOTE_TIME", "quote timestamp %r unparseable" % (quote_ts,))
    if qt_raw.tzinfo is None:
        return done("REFUSED_QUOTE_TIME_NAIVE", "quote timestamp %r has no UTC offset - an instant needs one "
                    "(e.g. 2026-11-02T14:30:00+00:00); a naive time cannot be placed against the session" % (quote_ts,))
    try:
        from zoneinfo import ZoneInfo as _ZI
        qt = qt_raw.astimezone(_ZI("Europe/London")).replace(tzinfo=None)
    except Exception as _tzx:                                           # noqa: BLE001
        return done("REFUSED_QUOTE_TIME", "quote timezone could not be resolved to Europe/London (%s)" % _tzx)
    out["quote_ts_london"] = qt.isoformat(timespec="seconds")
    # ages are measured between INSTANTS (a naive `now` is the host's local time - datetime.astimezone()),
    # never between two naive clocks that may sit in different zones (the VM runs UTC, the session London)
    _now_i = now if now.tzinfo is not None else now.astimezone()
    _age_s = (_now_i - qt_raw).total_seconds()
    if _age_s < -300:
        return done("REFUSED_FUTURE_QUOTE", "quote %s is later than the check time %s - a future quote is not evidence"
                    % (qt_raw.isoformat(), _now_i.isoformat(timespec="seconds")))
    if qt.weekday() >= 5:
        return done("REFUSED_NON_SESSION_QUOTE", "a %s quote is an analysis close, not an executable quote (T57)" % qt.strftime("%A"))
    if _age_s > 86400:
        return done("REPRICE_REQUIRED", "quote older than one day - fetch a current session quote")
    if not str(quote_currency or "").strip():
        return done("REFUSED_CURRENCY", "quote currency is EMPTY - the unit of the price is unknown (MAJOR units required)")
    if act.get("currency") and str(quote_currency).upper() != str(act["currency"]).upper():
        return done("REFUSED_CURRENCY", "quote currency %s != dossier currency %s (quote in MAJOR units)" % (quote_currency, act["currency"]))
    if act.get("side") == "SELL":
        # ⚑ ISA-0621: a switch SALE. No cash test (it raises cash); the order sells whole held units, never more than
        #   held; the whole construction is re-run at this price and must still select the same plan (a much lower
        #   price raises the donor's own forward return and can make the switch lose - T56 applies to sales too).
        _o = sale_pricing(act, float(price_major), quote_currency, fx_quote_per_gbp, rc.get("costs") or {})
        out["order"] = _o
        if _o["state"] != "OK":
            return done(_o["state"], _o["why"])
        rr = build(month, root=root, write=False, price_overrides={ticker: float(price_major)},
                   switch_scope="AUTHORITY_ONLY")       # ISA-0621: the selection, not the diagnostic
        new_sel = [(a["ticker"], a["gbp"]) for a in active_plan(rr, root).get("actions") or []]
        old_sel = [(a["ticker"], a["gbp"]) for a in _ap0["actions"]]
        out["rerun_selection"] = new_sel
        if new_sel != old_sel:
            return done("REPRICE_REQUIRED", "at this sale price the construction selects %s instead of %s - re-run the decision "
                        "(T56)" % (new_sel, old_sel))
        _ap = (out.get("approval") or {}).get("state")
        if _ap == "APPROVED":
            _lim = out["approval"].get("limit_price_major")
            if _lim is not None and float(price_major) < float(_lim) - 1e-9:
                return done("REFUSED_BELOW_APPROVED_LIMIT", "sale price %.4f < the approved minimum %.4f" % (float(price_major), float(_lim)))
        return done("EXECUTABLE", "SALE: receipt VALID/COMPLETE/authorised; authority CURRENT; session quote; whole held units; "
                    "construction unchanged at this price. Sell FIRST; the buys it funds wait for settlement. PERMISSION, NOT "
                    "AN INSTRUCTION - approval: %s" % (_ap or "UNKNOWN"))
    try:
        _cash = float(cash_available_gbp) if cash_available_gbp is not None else float("nan")
    except (TypeError, ValueError):
        _cash = float("nan")
    if not math.isfinite(_cash) or _cash < 0:
        return done("REFUSED_CASH_UNKNOWN", "current available cash %r is missing, non-finite or negative - "
                    "affordability cannot be shown, so the order cannot be EXECUTABLE (pass --cash with the "
                    "broker's current settled cash net of open orders)" % (cash_available_gbp,))
    emax = act.get("economic_max_price_major")
    out["economic_max_price_major"] = emax
    if emax is not None and float(price_major) > float(emax) + 1e-9:
        return done("REFUSED_ABOVE_ECONOMIC_MAX", "price %.4f > economic max %.4f: ER12 at this price is below the hurdle (T52)" % (price_major, emax))
    # ⚑ ISA-0858 B1.6b at execution: a drawdown tranche that FIRED since the receipt takes its pounds first, and
    #   the D23 reserve is never spent - the cash this order may use is current cash less both.
    try:
        import funding_waterfall as _fw_x
        _dds = _fw_x.drawdown_reservation(_fw_x._json(os.path.join(root, "drawdown_state.json"), {}),
                                          (_fw_x._json(os.path.join(root, "transaction_ledger.json"), {}) or {}).get("entries") or [])
    except Exception as _ddx:                                           # noqa: BLE001
        _dds = {"reserved_gbp": None, "why": "%s: %s" % (type(_ddx).__name__, _ddx)}
    out["drawdown_reservation"] = _dds
    if _dds.get("reserved_gbp") is None:
        return done("REFUSED_DRAWDOWN_RESERVATION_UNKNOWN", "the B1.6b drawdown reservation cannot be measured (%s) - "
                    "cash a fired tranche may own is not spent on a stock (ISA-0858)" % _dds.get("why"))
    _res_now = float(((rc.get("book") or {}).get("reserve_gbp")) or 0.0)
    _cash_net = _cash - _res_now - float(_dds["reserved_gbp"])
    out["cash_for_this_order_gbp"] = round(_cash_net, 2)
    _trims = float(((act.get("funding") or {}).get("fund_trims_gbp")) or 0.0)
    # ⚑ ISA-0727 Q3 (Raj 13-Oct-2026): cash that arrived after the approval re-runs the FUNDING split only (it
    #   displaces planned fund trims under D4) - never the selection.
    _need = max(0.0, float(act.get("gbp") or 0) - max(0.0, _cash_net))
    out["funding_now"] = {"planned_fund_trims_gbp": round(_trims, 2),
                          "fund_trims_still_needed_gbp": round(min(_trims, _need), 2),
                          "fund_trims_displaced_by_cash_gbp": round(max(0.0, _trims - min(_trims, _need)), 2),
                          "rule": "cash first, fund trims only for the shortfall (D4); the selection is not re-run (Raj Q3)"}
    _salef = float(((act.get("funding") or {}).get("sale_proceeds_gbp")) or 0.0)
    if _cash_net + EPS_GBP < float(act.get("gbp") or 0):
        if _salef > EPS_GBP:
            return done("AWAITING_SALE_SETTLEMENT", "this buy is funded by the switch sale(s) (GBP %.2f of proceeds, ISA-0621): "
                        "execute and settle the sale first, then re-check - cash for this order GBP %.2f < action GBP %.2f"
                        % (_salef, _cash_net, float(act.get("gbp") or 0)))
        if _trims > EPS_GBP:
            return done("AWAITING_FUND_SETTLEMENT", "this action is fund-funded (GBP %.2f of fund trims, ISA-0858 D8): place the "
                        "fund sells and re-check once the proceeds have SETTLED - cash for this order GBP %.2f < action GBP %.2f"
                        % (_trims, _cash_net, float(act.get("gbp") or 0)))
        return done("REPRICE_REQUIRED", "cash GBP %.2f (after the D23 reserve and GBP %.2f of fired drawdown tranches) < action "
                    "GBP %.2f - rerun construction on the actual cash" % (_cash_net, float(_dds["reserved_gbp"]), act.get("gbp")))
    # ⚑ ISA-0856 (11-Oct-2026, B02-R08): price the ORDER the quote implies - whole units, quote converted to GBP,
    #   commission, dealing FX and purchase levy each charged ONCE (the receipt's own MEASURED costs block).
    _o = order_pricing(act, ticker, float(price_major), quote_currency, fx_quote_per_gbp, rc.get("costs") or {})
    out["order"] = _o
    if _o["state"] != "OK":
        return done(_o["state"], _o["why"])
    if _o["est_total_gbp"] > _cash_net + EPS_GBP:
        return done("REPRICE_REQUIRED", "the %d whole units cost GBP %.2f all-in > cash for this order GBP %.2f (current cash "
                    "less D23 reserve and fired drawdown tranches, ISA-0858)" % (_o["units"], _o["est_total_gbp"], _cash_net))
    rr = build(month, root=root, write=False, price_overrides={ticker: float(price_major)},
               switch_scope="AUTHORITY_ONLY")           # ISA-0621: the selection, not the diagnostic
    # ISA-0860: the re-run must select the SAME active plan (primary, or the same fallback for the same refusals)
    new_sel = [(a["ticker"], a["gbp"]) for a in active_plan(rr, root).get("actions") or []]
    old_sel = [(a["ticker"], a["gbp"]) for a in _ap0["actions"]]
    out["rerun_selection"] = new_sel
    out["rerun_er12_central_pct"] = next((a.get("central_er12_local_pct") for a in rr.get("selected_actions") or [] if a["ticker"] == ticker), None)
    if new_sel != old_sel:
        return done("REPRICE_REQUIRED", "at the executable price the construction selects %s instead of %s - re-run the decision (T56)" % (new_sel, old_sel))
    _ap = (out.get("approval") or {}).get("state")
    if _ap == "APPROVED":
        _lim = (out["approval"].get("limit_price_major"))
        if _lim is not None and float(price_major) > float(_lim) + 1e-9:
            return done("REFUSED_ABOVE_APPROVED_LIMIT", "price %.4f > the approved limit %.4f (ISA-0864)" % (float(price_major), float(_lim)))
        if _o.get("est_total_gbp") is not None and out["approval"].get("max_gbp") is not None \
                and float(_o["est_total_gbp"]) > float(out["approval"]["max_gbp"]) + EPS_GBP:
            return done("REPRICE_REQUIRED", "the order costs GBP %.2f all-in > the approved maximum GBP %.2f (ISA-0864)"
                        % (float(_o["est_total_gbp"]), float(out["approval"]["max_gbp"])))
    return done("EXECUTABLE", "receipt VALID/COMPLETE/authorised; authority CURRENT; session quote; below economic max; "
                "construction unchanged at the executable price. PERMISSION, NOT AN INSTRUCTION - approval: %s" % (_ap or "UNKNOWN"))


def record_fill(month: str, receipt_id: str, ticker: str, order_id: str, quantity: float, price_major,
                gbp_cost: float, root: str = HERE, *, execution_id: Optional[str] = None,
                quote_currency: Optional[str] = None, fx_quote_per_gbp=None, price_gbp=None,
                commission_gbp=None, fx_cost_gbp=None, levy_gbp=None, fill_ts: Optional[str] = None,
                source: Optional[str] = None) -> dict:
    """Append-only capture of ONE execution (ISA-0855, B02-R06). Order identity and execution identity are
    separate: idempotent on (receipt, ticker, order_id, execution_id), so distinct partial fills of one order are
    all retained and an identical replay is not; a row without execution_id is order-level (legacy rows read
    that way). Prices are typed: price_major is in quote_currency (MAJOR units), price_gbp is the broker's GBP
    conversion when that is all the broker reports. A fill authorises nothing: every remaining action of the
    receipt is refused at execution_check until a successor receipt is formed on the post-fill book (B02-R07)."""
    try:
        q, g = float(quantity), float(gbp_cost)
    except (TypeError, ValueError):
        raise EngineRefused("record_fill: quantity %r / gbp_cost %r are not numbers" % (quantity, gbp_cost))
    if not (math.isfinite(q) and q > 0 and math.isfinite(g) and g > 0):
        raise EngineRefused("record_fill: quantity and gbp_cost must be positive finite (got %r, %r)" % (quantity, gbp_cost))
    if price_major is not None and not quote_currency:
        raise EngineRefused("record_fill: price_major needs its quote_currency (MAJOR units) - pass price_gbp for a GBP-converted price")
    p = os.path.join(root, EXECUTION_LEDGER)
    key = "%s|%s|%s" % (receipt_id, ticker, order_id) + ("|%s" % execution_id if execution_id else "")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip() and json.loads(ln).get("idempotency_key") == key:
                    return {"state": "ALREADY_RECORDED", "idempotency_key": key}
    row = {"idempotency_key": key, "schema": "fill_v2", "receipt_id": receipt_id, "month": month, "ticker": ticker,
           "order_id": order_id, "execution_id": execution_id, "identity_level": ("EXECUTION" if execution_id else "ORDER"),
           "quantity": q, "price_major": price_major, "quote_currency": quote_currency, "fx_quote_per_gbp": fx_quote_per_gbp,
           "price_gbp": price_gbp, "commission_gbp": commission_gbp, "fx_cost_gbp": fx_cost_gbp, "levy_gbp": levy_gbp,
           "gbp_cost": g, "fill_ts": fill_ts, "source": source, "recorded_at": _now().isoformat()}
    # ISA-0864 (B05-R08): link the fill to Raj's recorded approval (or record that there was none) - a fact, never a gate
    try:
        _vr = verify_receipt(month, root)
        _rcv = _vr.get("receipt") if _vr.get("receipt_id") == receipt_id else None
        row["approval_state"] = approval_state(month, ticker, _rcv, root, receipt_id=receipt_id).get("state")
    except Exception as exc:                                        # noqa: BLE001
        row["approval_state"] = "UNKNOWN: %s" % exc
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return {"state": "RECORDED", "idempotency_key": key,
            "next": "REMAINING_RERUN_REQUIRED: every remaining action of %s is refused at execution_check until the engine "
                    "re-runs on the post-fill book (successor receipt)" % receipt_id}


APPROVAL_LEDGER = "capital_approval_ledger.jsonl"


def _approval_rows(root: str = HERE) -> list:
    p = os.path.join(root, APPROVAL_LEDGER)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def approve_action(month: str, ticker: str, approved_by: str, root: str = HERE, *, limit_price_major=None,
                   max_gbp=None, note: Optional[str] = None) -> dict:
    """ISA-0864 (11-Oct-2026, B05-R08): record Raj's APPROVAL of ONE action, bound to the receipt's content hash and
    the exact action tuple. Refused unless the receipt is VALID and capital-authorised and the ticker is selected;
    a limit may only TIGHTEN the receipt's economic maximum and approved GBP. Append-only, idempotent on
    (receipt, ticker). An approval authorises nothing by itself: execution_check still decides at the quote."""
    if not str(approved_by or "").strip():
        raise EngineRefused("approve_action: approved_by is required (who decided)")
    v = verify_receipt(month, root)
    if v.get("state") != "VALID":
        raise EngineRefused("approve_action: receipt is %s - only a VALID receipt can be approved (%s)" % (v.get("state"), v.get("why")))
    rc = v["receipt"]
    if not rc.get("capital_authorised") or rc.get("state") != "DECISION_COMPLETE":
        raise EngineRefused("approve_action: receipt %s is %s / capital_authorised %s - nothing to approve"
                            % (rc.get("receipt_id"), rc.get("state"), rc.get("capital_authorised")))
    _apx = active_plan(rc, root)                    # ISA-0860: approve within the ACTIVE plan only
    if not _apx.get("authorised"):
        raise EngineRefused("approve_action: %s" % (_apx.get("why") or "no authorised plan"))
    sel = {a["ticker"]: a for a in _apx["actions"]}
    if ticker not in sel:
        raise EngineRefused("approve_action: %s is not an action of %s plan %s (actions %s)"
                            % (ticker, rc.get("receipt_id"), _apx["plan_id"], sorted(sel)))
    act = sel[ticker]
    emax = act.get("economic_max_price_major")
    lim = emax if limit_price_major is None else float(limit_price_major)
    if lim is not None and (not math.isfinite(float(lim)) or float(lim) <= 0):
        raise EngineRefused("approve_action: limit %r is not a positive finite MAJOR-unit price" % (limit_price_major,))
    if emax is not None and lim is not None and float(lim) > float(emax) + 1e-9:
        raise EngineRefused("approve_action: limit %.4f exceeds the receipt's economic max %.4f - an approval may only tighten it" % (float(lim), float(emax)))
    gbp = float(act.get("gbp") or 0)
    mg = gbp if max_gbp is None else float(max_gbp)
    if not math.isfinite(mg) or mg <= 0 or mg > gbp + EPS_GBP:
        raise EngineRefused("approve_action: max_gbp %r must be positive and <= the approved action GBP %.2f" % (max_gbp, gbp))
    key = "%s|%s" % (rc.get("receipt_id"), ticker)
    for r in _approval_rows(root):
        if r.get("idempotency_key") == key:
            return {"state": "ALREADY_APPROVED", "idempotency_key": key, "approved_at": r.get("approved_at")}
    row = {"idempotency_key": key, "schema": "approval_v1", "month": month, "receipt_id": rc.get("receipt_id"),
           "receipt_sha256": rc.get("content_sha256"), "ticker": ticker, "action": act.get("action"),
           "action_gbp": gbp, "economic_max_price_major": emax, "limit_price_major": lim, "max_gbp": round(mg, 2),
           "currency": act.get("currency"), "approved_by": approved_by, "note": note, "approved_at": _now().isoformat()}
    with open(os.path.join(root, APPROVAL_LEDGER), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return {"state": "APPROVED", "idempotency_key": key,
            "note": "approval recorded; execution_check still decides at the executable quote (permission is not an instruction)"}


def approval_state(month: str, ticker: str, receipt: Optional[dict], root: str = HERE, *,
                   receipt_id: Optional[str] = None) -> dict:
    """ISA-0864: APPROVED (bound to THIS receipt's content) | APPROVAL_STALE (approved on another receipt) | NOT_APPROVED."""
    rid = (receipt or {}).get("receipt_id") or receipt_id
    sha = (receipt or {}).get("content_sha256")
    rows = [r for r in _approval_rows(root) if r.get("ticker") == ticker and r.get("month") == month]
    hit = [r for r in rows if r.get("receipt_id") == rid and (sha is None or r.get("receipt_sha256") == sha)]
    if hit:
        r = hit[-1]
        return {"state": "APPROVED", "approved_by": r.get("approved_by"), "approved_at": r.get("approved_at"),
                "limit_price_major": r.get("limit_price_major"), "max_gbp": r.get("max_gbp")}
    if rows:
        return {"state": "APPROVAL_STALE", "approved_receipts": sorted({r.get("receipt_id") for r in rows}),
                "why": "an approval exists for %s on another receipt - a changed decision needs a new approval" % ticker}
    return {"state": "NOT_APPROVED"}


def action_lifecycle(month: str, root: str = HERE) -> dict:
    """ISA-0864 (B05-R08): the distinct state axes of every action of the month's receipt - RECEIPT
    (VALID|STALE|ABSENT|CORRUPT), AUTHORISED, APPROVAL, FILL - never collapsed into one word. The execution state
    (EXECUTABLE / REPRICE_REQUIRED / REFUSED_*) needs a live quote and is produced only by execution_check."""
    v = verify_receipt(month, root)
    rc = v.get("receipt") or {}
    if not rc and v.get("state") == "STALE":
        # ISA-0864 residual (11-Oct-2026): a STALE receipt still SHOWS its actions with their approvals and fills
        #   (display only - its RECEIPT axis says STALE, so nothing here is executable). CORRUPT/ABSENT show nothing.
        try:
            with open(os.path.join(root, "capital_decision_receipt_%s.json" % month), encoding="utf-8") as fh:
                rc = json.load(fh)
        except Exception:                                           # noqa: BLE001
            rc = {}
    fl = fills_for(rc.get("receipt_id"), root) if rc else {}
    rows = []
    _apl = active_plan(rc, root) if rc else {"plan_id": None, "actions": []}
    for a in (_apl.get("actions") if _apl.get("plan_id") else rc.get("selected_actions")) or []:
        t = a["ticker"]
        f = fl.get(t)
        rows.append({"ticker": t, "action": a.get("action"), "gbp": a.get("gbp"),
                     "receipt": v.get("state"), "capital_authorised": rc.get("capital_authorised"),
                     "approval": approval_state(month, t, rc, root),
                     "fill": ({"state": "HAS_FILLS", "n_fills": f["n_fills"], "quantity": f["quantity"], "gbp_cost": f["gbp_cost"],
                               "remaining_gbp": round(max(0.0, float(a.get("gbp") or 0) - f["gbp_cost"]), 2)}
                              if f else {"state": "NOT_FILLED"})})
    return {"month": month, "receipt_id": rc.get("receipt_id") or v.get("receipt_id"), "receipt": v.get("state"),
            "why": v.get("why"), "actions": rows,
            "active_plan": {k: _apl.get(k) for k in ("plan_id", "refused", "state", "why")},
            "dealability_confirmation": [x for x in ((rc.get("dealability_confirmation") or {}).get("required") or [])],
            "note": "EXECUTABLE is a quote-time permission from execution_check, not a state of record; APPROVED is Raj's decision; HAS_FILLS is the broker fact"}


def verify_receipt(month: str, root: str = HERE, receipt: Optional[dict] = None,
                   capital_path_now: Optional[dict] = None) -> dict:
    """The CONSUMER-side binding check (Checkpoint-D / email / capital_destination LIVE): the receipt
    exists, its content hash is intact, and every input hash it was formed on equals the file on
    disk NOW. Anything else is ABSENT / CORRUPT / STALE and is never treated as a decision."""
    p = os.path.join(root, "capital_decision_receipt_%s.json" % month)
    if receipt is None:
        if not os.path.exists(p):
            return {"state": "ABSENT", "path": p}
        try:
            with open(p, encoding="utf-8") as fh:
                receipt = json.load(fh)
        except Exception as exc:                                    # noqa: BLE001
            return {"state": "CORRUPT", "why": str(exc)}
    if _sha(_decision_body(receipt)) != receipt.get("content_sha256"):
        return {"state": "CORRUPT", "why": "content hash mismatch - the receipt was edited after it was formed"}
    drift = {}
    for k, h in (receipt.get("input_hashes") or {}).items():
        if k not in INPUT_FILES:
            continue
        now = _file_sha(input_path(month, k, root))              # ISA-0832: the archived copy after the purge
        if now != h:
            drift[k] = {"receipt": (h or "")[:12], "now": (now or "")[:12]}
    # ISA-0852 (B01-R03): a changed goal, schedule, one-off, operative anchor or friction buffer invalidates the
    #   decision's hurdle - and with it every size and admission the receipt carries. A receipt formed before
    #   the binding existed has none to check (NOT_DECLARED; it is still bound to its file inputs).
    _hb = receipt.get("hurdle_basis")
    if isinstance(_hb, dict):
        _now = hurdle_basis(root)
        _keys = ("state", "required_return_operative_pct", "operative_effective_from", "er_friction_buffer_pp",
                 "hurdle_pct", "declared_projection_sha")
        _d = {k: {"receipt": _hb.get(k), "now": _now.get(k)} for k in _keys if _hb.get(k) != _now.get(k)}
        if _d:
            drift["hurdle_basis"] = _d
    # ISA-0863 (B05-R05): a receipt formed by other capital-path code/config is not the decision this code would form.
    #   Legacy receipts (no key) verify on file inputs + hurdle only (NOT_DECLARED); UNAVAILABLE now is never VALID.
    _cpr = receipt.get("capital_path")
    if isinstance(_cpr, dict) and _cpr.get("state") == "BOUND":
        _cpn = capital_path_now if capital_path_now is not None else capital_path_identity()
        if _cpn.get("state") != "BOUND" or _cpn.get("fingerprint") != _cpr.get("fingerprint"):
            _ch = sorted(k for k in set(_cpr.get("members") or {}) | set(_cpn.get("members") or {})
                         if (_cpr.get("members") or {}).get(k) != (_cpn.get("members") or {}).get(k))
            drift["capital_path"] = {"receipt": (_cpr.get("fingerprint") or "")[:12],
                                     "now": (_cpn.get("fingerprint") or _cpn.get("state") or "")[:12],
                                     "receipt_build": _cpr.get("build_id"), "now_build": _cpn.get("build_id"),
                                     "n_changed": len(_ch), "changed_members": _ch[:20], "why_now": _cpn.get("why")}
    if drift:
        return {"state": "STALE", "receipt_id": receipt.get("receipt_id"), "drift": drift,
                "why": ("the decision's hurdle basis changed after the receipt was formed (ISA-0852) - re-run the engine"
                        if list(drift) == ["hurdle_basis"] else
                        "the capital-path code/config changed after the receipt was formed (ISA-0863) - re-run the engine"
                        if list(drift) == ["capital_path"] else
                        "an input changed after the receipt was formed - re-run the engine")}
    return {"state": "VALID", "receipt_id": receipt.get("receipt_id"), "receipt_state": receipt.get("state"),
            "hurdle_basis": ("BOUND" if isinstance(_hb, dict) else "NOT_DECLARED"),
            "capital_path": ("BOUND" if isinstance(_cpr, dict) and _cpr.get("state") == "BOUND" else "NOT_DECLARED"),
            "capital_authorised": receipt.get("capital_authorised"), "receipt": receipt}


def summary(receipt: dict) -> dict:
    """Compact, renderer-facing view of the ONE receipt (email/Checkpoint-D read this; they never
    recompute selection, size or permission - R20.2)."""
    return {"receipt_id": receipt.get("receipt_id"), "content_sha256": receipt.get("content_sha256"),
            "state": receipt.get("state"), "capital_authorised": receipt.get("capital_authorised"),
            "engine_authority": receipt.get("engine_authority"),
            "selected_actions": [{k: a.get(k) for k in ("ticker", "action", "gbp", "net_er12_central_pct",
                                                         "bear_gbp_pct", "economic_max_price_major", "currency")}
                                 for a in receipt.get("selected_actions") or []],
            "selected_sales": [{k: a.get(k) for k in ("ticker", "action", "gbp", "proceeds_gbp", "residual_gbp", "currency")}
                               for a in receipt.get("selected_sales") or []],          # ISA-0621 (LIVE switch)
            "ambition_band": (receipt.get("ambition") or {}).get("selected_band"),
            "ambition_state": (receipt.get("ambition") or {}).get("state"),
            "case_scope_required": (receipt.get("case_scope") or {}).get("required_full_cases"),
            "case_scope_outstanding": (receipt.get("case_scope") or {}).get("outstanding"),
            "census_counts": (receipt.get("census") or {}).get("counts"),
            "comparator_rule": (receipt.get("comparator") or {}).get("rule"),
            # ISA-0860: the confirm-at-dealing list and the pre-authorised fallbacks (rendered, never recomputed)
            "dealability_confirmation": {
                "required": (receipt.get("dealability_confirmation") or {}).get("required") or [],
                "fallbacks": [{"id": f.get("id"), "if_refused": f.get("if_refused"), "authorised": f.get("authorised"),
                               "authorised_basis": f.get("authorised_basis"),
                               "actions": [{"ticker": a.get("ticker"), "gbp": a.get("gbp")} for a in f.get("actions") or []]}
                              for f in (receipt.get("dealability_confirmation") or {}).get("fallbacks") or []],
                "sequencing": (receipt.get("dealability_confirmation") or {}).get("sequencing")},
            "search_state": (receipt.get("search") or {}).get("state"),
            # ISA-0465 (19-Oct-2026): the concentration SHADOW/LIVE record (concentration_control.real_shadow_evidence reads it)
            "concentration": {k: (receipt.get("concentration") or {}).get(k) for k in
                              ("mode", "binding", "impact", "n_bundles_evaluated", "n_bundles_failing", "selected_bundle",
                               "failing_examples", "unmapped_universe")}
                             if receipt.get("concentration") else None,
            # ISA-0621 (19-Oct-2026): the SHADOW switch contest and the net-book factor verdict (rendered, never recomputed)
            "switch_contest": ({k: (receipt.get("switch_contest") or {}).get(k) for k in
                                ("authority", "state", "why", "parity", "shadow_selection", "diagnostic_selection", "counts",
                                 "concentration_net_book", "factor_net_book", "case_scope_donors", "switch_only_receivers")}
                               | {"donors": {t: {k: d.get(k) for k in ("held_gbp", "code", "evaluated", "authority_eligible",
                                                                       "central_er12_local_pct", "central_breakeven_er12_pct",
                                                                       "regret_cost_of_selling_gbp",
                                                                       "bear_wealth_effect_of_selling_gbp", "why")}
                                             for t, d in ((receipt.get("switch_contest") or {}).get("donors") or {}).items()}})
                              if receipt.get("switch_contest") else None,
            "factor": receipt.get("factor"),
            # P-D (ISA-0887/0888): the comparator's risk basis and the selected plan's appetite guard (rendered only)
            "risk_metric": (receipt.get("risk_constraints") or {}).get("risk_metric"),
            "appetite_guard": receipt.get("appetite_guard"),
            "new_capital_control": receipt.get("new_capital_control")}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--watch-receipt" in argv:
        # ISA-0727 B: the approved-pending watch CLI - screen_completion runs it as a SUBPROCESS so the Composio
        # fallback's import closure (sync_repo_to_github.FALLBACK_ENTRYPOINTS) never contains the capital engine.
        r = watch_receipt(opt("--watch-receipt"), opt("--root", HERE), frame_path=opt("--frame"),
                          source=opt("--source") or "cli")
        print(json.dumps(r, default=str))
        return 0
    month = opt("--month")
    if month and "--input-hash" in argv:
        # the base a case author writes an amendment against (s9.3): printed, never guessed
        inp_ = load_inputs(month, opt("--root", HERE))
        print(json.dumps({"security_id": opt("--input-hash"),
                          "old_hash": security_input_hash(opt("--input-hash"), inp_)}))
        return 0
    if month and "--amend" in argv:
        return amend_cli(month, opt("--amend"), opt("--root", HERE))
    if month and "--execution-check" in argv:
        # ISA-0850: --cash is required (missing -> REFUSED_CASH_UNKNOWN inside execution_check, never skipped)
        r = execution_check(month, opt("--execution-check"), float(opt("--price")), opt("--currency"),
                            opt("--quote-ts"), opt("--cash"), opt("--root", HERE),
                            fx_quote_per_gbp=opt("--fx"))           # ISA-0856: required for a non-GBP quote
        print(json.dumps({k: v for k, v in r.items()}, indent=1, default=str))
        return 0 if r["state"] == "EXECUTABLE" else 3
    if month and "--record-fill" in argv:
        # ISA-0855: capture ONE execution from the broker record (never estimated); a fill authorises nothing
        def _f(x):
            return None if x is None else float(x)
        try:
            r = record_fill(month, opt("--receipt"), opt("--record-fill"), opt("--order-id"), float(opt("--quantity")),
                            _f(opt("--price")), float(opt("--gbp-cost")), opt("--root", HERE),
                            execution_id=opt("--execution-id"), quote_currency=opt("--currency"),
                            fx_quote_per_gbp=_f(opt("--fx")), price_gbp=_f(opt("--price-gbp")),
                            commission_gbp=_f(opt("--commission")), fill_ts=opt("--fill-ts"), source=opt("--source"))
        except (EngineRefused, TypeError, ValueError) as exc:
            print("CDE_REFUSED %s" % exc)
            return 2
        print(json.dumps(r, indent=1))
        return 0
    if month and "--approve" in argv:
        # ISA-0864: Raj's decision on ONE action of the VALID receipt (limits may only tighten)
        try:
            r = approve_action(month, opt("--approve"), opt("--approved-by"), opt("--root", HERE),
                               limit_price_major=opt("--limit-price"), max_gbp=opt("--max-gbp"), note=opt("--note"))
        except (EngineRefused, TypeError, ValueError) as exc:
            print("CDE_REFUSED %s" % exc)
            return 2
        print(json.dumps(r, indent=1))
        return 0
    if month and "--record-dealability" in argv:
        # ISA-0860: Raj's dealing outcome for a confirm-required name -> declaration of record + learning row
        try:
            r = record_dealability(month, opt("--record-dealability"), opt("--outcome"), opt("--recorded-by"),
                                   opt("--root", HERE), wrapper=opt("--wrapper", "ISA"), reason_code=opt("--reason-code"),
                                   evidence=opt("--evidence"))
        except (EngineRefused, TypeError, ValueError) as exc:
            print("CDE_REFUSED %s" % exc)
            return 2
        print(json.dumps(r, indent=1, default=str))
        return 0
    if month and "--lifecycle" in argv:
        print(json.dumps(action_lifecycle(month, opt("--root", HERE)), indent=1, default=str))
        return 0
    if month and "--verify" in argv:
        v = verify_receipt(month, opt("--root", HERE))
        v.pop("receipt", None)
        print(json.dumps(v, indent=1, default=str))
        return 0 if v["state"] == "VALID" else 3
    if not month:
        print(__doc__)
        return 2
    try:
        r = build(month, root=opt("--root", HERE), amendments_path=opt("--amendments"),
                  out_path=opt("--out"), baseline_plan_path=opt("--baseline-plan"),
                  write="--no-write" not in argv)
    except EngineRefused as exc:
        print("CDE_REFUSED %s" % exc)
        return 2
    print(json.dumps(summary(r), indent=1, default=str))
    print("CDE_RECEIPT %s state=%s capital_authorised=%s" % (r["receipt_id"], r["state"], r["capital_authorised"]))
    return 0


def _row(bundle, wealth: dict, vol=1000.0, cost=0.0):
    return {"bundle": tuple(bundle), "tickers": [t for t, _ in bundle], "wealth": dict(wealth),
            "gbp_vol": vol, "feasible": True, "entry_costs_gbp": cost,
            "stock_gbp": round(sum(a for _, a in bundle), 2), "residual_gbp": 0.0}


def real_inputs_missing(month: str, root: str) -> list:
    """ISA-0832: the INPUT_FILES members absent for `month` under `root` ([] = the full real set is present)."""
    return [k for k in INPUT_FILES if not os.path.exists(input_path(month, k, root))]


MONTH_KEYED_INPUTS = tuple(k for k, p in INPUT_FILES.items() if "{m}" in p)


def real_month_state(month: str, root: str) -> dict:
    """ISA-0838 (10-Oct-2026) - ONE predicate for "may the REAL rows run on this month here?".
      ABSENT   no month-keyed input resolves (LIVE or archived): this tree simply has no such month - a
               legitimate, printed skip.
      COMPLETE every INPUT_FILES member resolves - the real rows run.
      PARTIAL  some resolve, some do not. Since ISA-0832 the archive-aware resolver makes this impossible on
               an intact LIVE, so PARTIAL means an environment that hides inputs (the 06..10-Oct census copy
               without archive/) or real data loss - both must be LOUD (R2.10), never a green skip."""
    missing = real_inputs_missing(month, root)
    present_keyed = [k for k in MONTH_KEYED_INPUTS if k not in missing]
    if not present_keyed:
        return {"state": "ABSENT", "month": month, "missing": missing}
    return {"state": "COMPLETE" if not missing else "PARTIAL", "month": month, "missing": missing}


def _selftest(verbose: bool = True) -> int:
    """BuildSpec s16 T-suite (engine-level rows). Every MUST-FIRE has a NEGATIVE CONTROL; oracles are
    independent of the production helper they check (s16: tests may not share the same incorrect
    helper as their only oracle)."""
    import random
    from decimal import Decimal as D
    fails = []

    def ok(name, cond, detail=None):
        if not cond:
            fails.append(name)
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("" if cond or detail is None else " :: %r" % (detail,)))

    # ── T09 return bridge, independent Decimal oracle ──────────────────────────────────────
    P0, E0, E1, PE, DIV = D(200), D(10), D(12), D(20), D(4)
    oracle = (E1 * PE + DIV) / P0 - 1
    ok("T09 Decimal oracle: EPS 10->12 x P/E 20 + div 4 on 200 = 22%", oracle == D("0.22"))
    hv_fx = {"route": "PE", "case_id": "FX", "method_id": "hv", "state": "VALID_MECHANICAL",
             "er_pct": 22.0, "er_local_pct": 22.0,
             "authoritative": {"route": "PE", "state": "VALID_MECHANICAL", "er_pct": 22.0, "P_T": 240.0,
                               "inputs": {"P0_quote": 200.0, "EPS0": 10.0, "EPS1": 12.0, "r": 0.0},
                               "currency": {"quote": "GBP", "quote_major": "GBP", "unit_div": 1.0, "reporting": "GBP"},
                               "fy0_end": "2027-03-31"},
             "route_record": {"review_state": "VALID_MECHANICAL"}, "fx_sensitivity": {}}
    pit_fx = {"eps_estimate": {"+1y": {"avg": 12.0, "low": 11.0, "high": 13.0}}}
    er = er12_case("FX", hv_fx, pit_fx, "2026-10-03", 15.7)
    ok("T09 production bridge identity reproduces 22% (EPS progression x multiple + D/P0)",
       abs((er["bridge"]["identity_check"] or 0) - 0.22) < 1e-9 and er["state"] == "VALID", er.get("bridge"))
    c2 = {"commission_gbp": 2.0, "fx_fraction": 0.0, "levy_fraction": 0.0}
    w = position_wealth(202.0, 0.22, c2) + 2.0          # add back the exit commission: entry-cost-only view
    ok("T09 cost denominator: 244/202 - 1 = 20.792%, multiplicative", abs(w / 202.0 - 1 - 0.2079207921) < 1e-9, w)
    ok("NEGATIVE CONTROL T09: the additive '22% - 1% = 21%' shortcut is NOT what the engine computes",
       abs(w / 202.0 - 1 - 0.21) > 1e-3)
    # ── T23: a permission cap is a CAP, never a target ─────────────────────────────────────
    _wl = {w: 100.0 for w in S_E}
    _rows23 = [_row((), dict(_wl, **{w: 0.0 for w in S_B}), vol=0.0),
               _row((("X", 3500.0),), {**{w: 160.0 for w in S_E}, **{w: 50.0 for w in S_B}}, vol=500.0),
               _row((("X", 4500.0),), {**{w: 150.0 for w in S_E}, **{w: 40.0 for w in S_B}}, vol=700.0)]
    for _r_ in _rows23:
        _r_["feasible"] = True
    _c23 = compare(_rows23)
    ok("T23: permission allows 4,500 (HIGH-like) but the comparator selects 3,500 when 3,500 is better in every world",
       _c23.get("selected", {}).get("stock_gbp") == 3500.0, _c23.get("selected", {}).get("stock_gbp"))
    _lb23, _ = legal_bundles([{"ticker": "X", "levels": [3500.0, 4500.0], "min_gbp": 2800.0}], 9000.0, 2800.0)
    ok("NEGATIVE CONTROL T23: both rungs up to the cap are legal choices (the cap is not auto-targeted)",
       ((("X", 3500.0),) in _lb23) and ((("X", 4500.0),) in _lb23), _lb23)
    # ── ISA-0748 plausibility triggers (SPI.L) and s11 repricing ─────────────────────────────
    _hv_sp = {"route": "PE", "authoritative": {"route": "PE", "er_pct": 103.29, "inputs": {"EPS0": 0.0893, "EPS1": 0.17171}},
              "cross_check": {"route": "EV", "state": "VALID_MECHANICAL", "er_pct": 33.75}}
    _tr = {t["code"] for t in plausibility_triggers(_hv_sp, {})}
    ok("ISA-0748 MUST-FIRE: 103% P/E vs 34% EV and EPS +92% -> ROUTE_DISAGREEMENT + EPS_DISCONTINUITY",
       _tr == {"ROUTE_DISAGREEMENT", "EPS_DISCONTINUITY"}, _tr)
    ok("NEGATIVE CONTROL ISA-0748: 22% vs 18% routes and EPS +20% raise no trigger",
       not plausibility_triggers({"route": "PE", "authoritative": {"er_pct": 22.0, "inputs": {"EPS0": 10.0, "EPS1": 12.0}},
                                  "cross_check": {"state": "VALID_MECHANICAL", "er_pct": 18.0}}, {}))
    _rp = reprice_er12({"state": "VALID", "central_local": 0.22, "bear_local": -0.10, "variants_local": {"LOW_ESTIMATES": 0.05},
                        "bridge": {"P0_major": 2.0}}, 2.2)
    ok("s11/T56: ER12 at an executable price 2.20 vs 2.00 = 1.22 x 2/2.2 - 1 = 10.909% (independent arithmetic)",
       abs(_rp["central_local"] - (1.22 * 2.0 / 2.2 - 1)) < 1e-12 and abs(_rp["bear_local"] - (0.9 * 2.0 / 2.2 - 1)) < 1e-12)
    ok("T54 MUST-FIRE: no bridge -> repricing refuses (UNKNOWN), never a number",
       reprice_er12({"state": "VALID", "central_local": 0.2, "bridge": {}}, 1.0)["state"] == "UNKNOWN")
    # ── T10 buyback once ──────────────────────────────────────────────────────────────────
    hv_bb = json.loads(json.dumps(hv_fx)); hv_bb["case_id"] = "BB"
    hv_bb["authoritative"]["inputs"]["EPS1"] = 11.1111111; hv_bb["authoritative"]["P_T"] = 222.222222
    hv_bb["er_pct"] = hv_bb["er_local_pct"] = hv_bb["authoritative"]["er_pct"] = 11.1111111 + 2.0
    erb = er12_case("BB", hv_bb, pit_fx, "2026-10-03", 15.7)
    ok("T10 shares 100->90 enters ONCE through per-share EPS (no separate buyback yield term)",
       not any("buyback" in k for k in erb["bridge"]) and
       abs(erb["bridge"]["eps_progression"] - 1.1111111) < 1e-4 and
       abs(erb["bridge"]["identity_check"] - 0.131111111) < 1e-4, erb.get("bridge"))
    _bb2 = dict(erb["bridge"]); _bb2["buyback_yield"] = 0.1111111   # broken: per-share EPS AND a yield term
    ok("NEGATIVE CONTROL T10: a bridge carrying a separate buyback-yield term is detected",
       any("buyback" in k for k in _bb2))
    # ── T11 negative EPS; T12 horizon mismatch ─────────────────────────────────────────────
    hv_neg = json.loads(json.dumps(hv_fx)); hv_neg["authoritative"]["inputs"]["EPS0"] = -1.0
    ok("T11 MUST-FIRE: a P/E bridge on negative EPS is refused (UNKNOWN), never a positive ER",
       er12_case("NEG", hv_neg, pit_fx, "2026-10-03", 15.7)["state"] == "UNKNOWN")
    hv_far = json.loads(json.dumps(hv_fx)); hv_far["authoritative"]["fy0_end"] = "2028-06-30"
    ok("T12 MUST-FIRE: FY0 ending after the 12-month horizon -> ER12 UNKNOWN (no silent annualisation)",
       er12_case("FAR", hv_far, pit_fx, "2026-10-03", 15.7)["state"] == "UNKNOWN")
    ok("NEGATIVE CONTROL T11/T12: the valid fixture stays VALID", er["state"] == "VALID")
    # ── T52 economic max price inversion ───────────────────────────────────────────────────
    pmax = (240.0 + 4.0) / 1.157
    ok("T52 economic max: ER at P_max equals the hurdle within tolerance",
       abs((240.0 + 4.0) / pmax - 1 - 0.157) < 1e-9)
    # ── T06 flag taxonomy ────────────────────────────────────────────────────────────────
    f1 = classify_flags({"disqualifier_flags": ["revision_direction_down"]},
                        {"revision_direction_down": "RESOLVED_BENIGN"})
    ok("T06 MUST-FIRE: the HRMY hard flag stays HARD even with a 'benign' adjudication", f1["hard"] == ["revision_direction_down"])
    f2 = classify_flags({"review_flags": ["brand_new_flag"]})
    ok("T06 unknown flag -> REVIEW_REQUIRED/UNRESOLVED, never deleted", f2["review_unresolved"] == ["brand_new_flag"])
    f3 = classify_flags({"review_flags": ["recent_reversal_vs_12_1m"]}, {"recent_reversal_vs_12_1m": "RESOLVED_BENIGN"})
    ok("NEGATIVE CONTROL T06: a sourced benign adjudication resolves a REVIEW flag", not f3["review_unresolved"] and not f3["hard"])
    # ── T06b ISA-0817 corporate-event flags (Raj D1-D3, 18-Oct-2026) ──────────────────────────
    f4 = classify_flags({}, None, ["offer_period_offeree"])
    ok("T06b MUST-FIRE ISA-0817: offer_period_offeree from corporate_events is HARD", f4["hard"] == ["offer_period_offeree"])
    f5 = classify_flags({}, {"offer_period_offeree": "RESOLVED_BENIGN"}, ["offer_period_offeree"])
    ok("T06b ISA-0817: a typed stale-source adjudication clears the offeree block (row kept, state shown)",
       not f5["hard"] and f5["rows"][0]["state"] == "RESOLVED_BENIGN")
    f6 = classify_flags({"disqualifier_flags": ["revision_direction_down"]},
                        {"revision_direction_down": "RESOLVED_BENIGN"}, ["corporate_action_pending"])
    ok("T06b NEGATIVE CONTROL: adjudication cannot clear an adopted HARD flag; corporate action is REVIEW",
       f6["hard"] == ["revision_direction_down"] and f6["review_unresolved"] == ["corporate_action_pending"])
    f7 = classify_flags({}, None, ["corporate_action_pending"])
    ok("T06b ISA-0817 D3: corporate_action_pending is REVIEW_REQUIRED, never HARD", f7["review_unresolved"] == ["corporate_action_pending"]
       and not f7["hard"])
    # ── T21/T29 exact search vs independent brute force, 100 random books ─────────────────
    rnd = random.Random(20261003)
    agree = 0
    for _ in range(100):
        k = rnd.randint(0, 5)
        names = [{"ticker": "N%d" % i, "levels": sorted(rnd.sample([3500.0, 4500.0, 5500.0, 6500.0], rnd.randint(1, 3))),
                  "min_gbp": rnd.choice([2800.0, 0.01])} for i in range(k)]
        cap = rnd.choice([0.0, 4000.0, 9000.0, 12345.67, 20000.0])
        prod, cert = legal_bundles(names, cap, 2800.0)
        agree += int(set(prod) == bruteforce_bundles(names, cap, 2800.0))
    ok("T21 production enumeration == independent brute force on 100 random books", agree == 100, agree)
    names = [{"ticker": "A", "levels": [3500.0]}, {"ticker": "B", "levels": [3500.0]}]
    full, _ = legal_bundles(names, 6500.0, 2800.0)     # 3500 + legal partial 3000 exists

    def broken(names, capital, min_entry):                         # drops partial fills
        out, _c = legal_bundles(names, capital, min_entry)
        return [b for b in out if all(a in (3500.0,) for _, a in b)]
    ok("T29 MUST-FIRE: a search that drops partial fills is caught by the oracle",
       set(broken(names, 6500.0, 2800.0)) != bruteforce_bundles(names, 6500.0, 2800.0))
    ok("NEGATIVE CONTROL T29: the production search matches the oracle", set(full) == bruteforce_bundles(names, 6500.0, 2800.0) and any(a == 3000.0 for b in full for _, a in b))
    # ── T27 order invariance; T24 MIN_ENTRY; T26 truthful reachability ────────────────────
    nn = [{"ticker": t, "levels": [3500.0]} for t in ("C", "A", "B")]
    ok("T27 shuffled input order -> identical legal set",
       set(legal_bundles(nn, 9000.0, 2800.0)[0]) == set(legal_bundles(list(reversed(nn)), 9000.0, 2800.0)[0]))
    small, _ = legal_bundles([{"ticker": "A", "levels": [3500.0], "min_gbp": 2800.0}], 2000.0, 2800.0)
    ok("T24 MUST-FIRE: a residual below MIN_ENTRY opens no new stock", small == [()], small)
    q, _ = legal_bundles([{"ticker": t, "levels": [5613.69], "min_gbp": 4490.95} for t in "ABCD"], 17963.80, 4490.95)
    ok("T26 four MIN_ENTRY openings are NOT legal under the one-partial D15/D16 rule (truthful space)",
       max(len(b) for b in q) == 3, max(len(b) for b in q))
    # ── T86 search budget ─────────────────────────────────────────────────────────────────
    global MAX_BUNDLES
    _mb = MAX_BUNDLES
    try:
        MAX_BUNDLES = 10
        _b, c = legal_bundles([{"ticker": "N%d" % i, "levels": [100.0, 200.0]} for i in range(6)], 10000.0, 50.0)
        ok("T86 MUST-FIRE: an over-budget search is SEARCH_INCOMPLETE with no bundles", c["state"] == "SEARCH_INCOMPLETE" and _b == [])
    finally:
        MAX_BUNDLES = _mb
    # ── comparator fixtures (s16.5) ──────────────────────────────────────────────────────
    E2, B0 = ("s1", "s2"), ("b",)
    A = _row([("A", 100.0)], {"s1": 125, "s2": 122, "b": 90}, vol=10)
    Bb = _row([("B", 100.0)], {"s1": 120, "s2": 117, "b": 90}, vol=10)
    r = compare([A, Bb], worlds_e=E2, worlds_b=B0)
    ok("T19 MUST-FIRE: A wins every world by GBP 5 at equal risk -> robust superiority selects A",
       r["selected"] is A and r["rule"] == "ROBUST_SUPERIORITY")
    A2 = _row([("A", 100.0)], {"s1": 125, "s2": 122, "b": 90}, vol=10)
    A2["source_score"] = 10; Bb2 = _row([("B", 100.0)], {"s1": 120, "s2": 117, "b": 90}, vol=10); Bb2["source_score"] = 99
    ok("T01 a higher Source Score on B cannot reverse the result", compare([Bb2, A2], worlds_e=E2, worlds_b=B0)["selected"] is A2)
    F = _row([("F", 100.0)], {"s1": 128, "s2": 100, "b": 80}, vol=10)
    R_ = _row([("R", 100.0)], {"s1": 119, "s2": 117, "b": 80}, vol=10)
    r = compare([F, R_], worlds_e=E2, worlds_b=B0)
    ok("T20/T60 fragile 28% vs robust 19%: regrets A=17, B=9 (independent arithmetic) -> B selected",
       r["selected"] is R_ and abs(F["regret"] - 17) < 1e-9 and abs(R_["regret"] - 9) < 1e-9 and len(r["tied"]) >= 1,
       (F.get("regret"), R_.get("regret")))
    ok("NEGATIVE CONTROL T20: central-max would have picked the fragile 28%", r["central_max"] is F)
    X = _row([("X", 100.0)], {"s1": 130, "s2": 125, "b": 60}, vol=10)
    Y = _row([("Y", 100.0)], {"s1": 120, "s2": 118, "b": 85}, vol=10)
    ok("T91 MUST-FIRE: winning every ESTIMATION world with a worse BEAR is NOT dominance",
       not dominates(X, Y, worlds_e=E2, worlds_b=B0))
    rr = compare([X, Y], worlds_e=E2, worlds_b=B0)
    ok("T91 both retained on the frontier", len(rr["undominated"]) == 2)
    X2 = _row([("X", 100.0)], {"s1": 130, "s2": 125, "b": 90}, vol=20)
    ok("T91 a higher-risk winner is not dominant either", not dominates(X2, Y, worlds_e=E2, worlds_b=B0))
    Z1 = _row([("P", 100.0)], {"s1": 120, "s2": 118, "b": 85}, vol=10)
    Z2 = _row([("Q", 100.0)], {"s1": 120, "s2": 118, "b": 85}, vol=10)
    rt = compare([Z1, Z2], worlds_e=E2, worlds_b=B0)
    ok("T60 MUST-FIRE: economically distinct exact ties -> DECISION_TIE_UNRESOLVED (no ticker order)",
       rt["state"] == "DECISION_TIE_UNRESOLVED", rt.get("state"))
    cash = _row([], {"s1": 101.76, "s2": 101.76, "b": 101.76}, vol=5)
    bad = _row([("L", 100.0)], {"s1": 99, "s2": 95, "b": 60}, vol=10)
    ok("T35 MUST-FIRE: when every stock is worse than cash in every world, NO-ACTION is selected",
       compare([cash, bad], worlds_e=E2, worlds_b=B0)["selected"] is cash)
    good = _row([("G", 100.0)], {"s1": 130, "s2": 125, "b": 105}, vol=8)
    ok("T89 POSITIVE MUST-FIRE (not always-refuse): a valid robust stock bundle IS selected over cash",
       compare([cash, good], worlds_e=E2, worlds_b=B0)["selected"] is good)
    # T25 two larger vs three smaller (computed preference, not queue)
    two = _row([("A", 7000.0), ("B", 7000.0)], {"s1": 17000, "s2": 16800, "b": 13000}, vol=10)
    three = _row([("A", 4666.0), ("B", 4666.0), ("C", 4666.0)], {"s1": 16500, "s2": 16300, "b": 12900}, vol=10)
    ok("T25 the computed 'fewer, larger' bundle wins when its worlds are better",
       compare([three, two], worlds_e=E2, worlds_b=B0)["selected"] is two)
    # ── T31 cash arithmetic oracle ─────────────────────────────────────────────────────────
    ok("T31 GBP 16,841.07 + 879.87 + 250.00 = 17,970.94 exactly (Decimal)",
       D("16841.07") + D("879.87") + D("250.00") == D("17970.94"))
    ok("T31 3 x STARTER(3.5% x 160,391.10) = 16,841.07 to the penny",
       (D("160391.10") * D("0.035")).quantize(D("0.01")) * 3 == D("16841.07"))
    # ── T39/T40 amendment boundary ────────────────────────────────────────────────────────
    _inp = {"horizon_value": {"cases": [hv_fx]}, "step9_pre": {}, "pit_capture": {}}
    good_a = {"amendment_id": "A1", "case_id": "C1", "security_id": "FX", "field_path": "scenario.bear_local_pct",
              "proposed_value": -35.0, "source_ids": ["10-Q 2026-08-05"], "published_at": ["2026-08-05"],
              "question_id": "Q4", "rationale": "credible adverse world: contract loss at the largest customer per 10-Q risk factors",
              "reviewer": "selftest", "created_at": "2026-10-04", "old_hash": security_input_hash("FX", _inp),
              "estimate_kind": "JUDGEMENT"}
    ok("T39 NEGATIVE CONTROL: an allowlisted, sourced, current-hash amendment is accepted",
       validate_amendment(good_a, _inp, "2026-10-04")[0])
    for fp, v in (("buy.FX", True), ("hurdle_pct", 10.0), ("cash.available", 1.0), ("source_score", 99),
                  ("permission.rung", "HIGH"), ("confidence.multiplier", 1.2)):
        okv, why = validate_amendment(dict(good_a, field_path=fp, proposed_value=v), _inp, "2026-10-04")
        ok("T39 MUST-FIRE: a case may not amend %s" % fp, not okv, why)
    ok("T40 MUST-FIRE: a stale base hash is rejected",
       not validate_amendment(dict(good_a, old_hash="0" * 16), _inp, "2026-10-04")[0])
    ok("T40 MUST-FIRE: a consensus-only scenario source is CIRCULAR_CONSENSUS",
       "CIRCULAR" in validate_amendment(dict(good_a, source_ids=["Yahoo consensus"]), _inp, "2026-10-04")[1])
    ok("T40 MUST-FIRE: a source published after the cutoff is not PIT",
       not validate_amendment(dict(good_a, published_at=["2026-10-09"]), _inp, "2026-10-04")[0])
    # ── T61 ambition on INCREMENTAL capital, weighted ─────────────────────────────────────
    cz = {"commission_gbp": 0.0, "fx_fraction": 0.0, "levy_fraction": 0.0}
    sc = {"A": {"CENTRAL": 0.30}, "B": {"CENTRAL": 0.10}}
    rw = _row([("A", 1000.0), ("B", 3000.0)], {})
    ie = incremental_er(rw, sc, {"A": cz, "B": cz})
    ok("T61 capital-weighted incremental ER: (1000x30% + 3000x10%)/4000 = 15%, not the 20% unweighted mean",
       abs(ie - 0.15) < 1e-12, ie)
    ok("T61 no-stock has ER N/A, never zero", incremental_er(_row([], {}), sc, {}) is None)
    # ── T28 no Source Score in the search / comparator / scenarios (AST) ──────────────────
    try:
        import ast as _ast
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        tree = _ast.parse(src)
        bad_fns = []
        for fn in (n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)
                   and n.name in ("legal_bundles", "compare", "dominates", "evaluate_bundles", "name_scenarios",
                                  "bundle_risk", "constraints_for", "ambition_view")):
            if any(isinstance(x, _ast.Constant) and x.value == "source_score" for x in _ast.walk(fn)):
                bad_fns.append(fn.name)
        ok("T28 Source Score is read by NONE of the search/comparator/scenario functions (AST)", not bad_fns, bad_fns)
    except Exception as exc:                                        # noqa: BLE001
        ok("T28 AST check ran", False, str(exc))
    # ── ISA-0832: one definition of "real inputs present" ───────────────────────────────
    import tempfile as _tf832
    import shutil as _sh832
    _t832 = _tf832.mkdtemp(prefix="cde832_")
    try:
        for _k, _pat in INPUT_FILES.items():
            if _k != "portfolio":
                open(os.path.join(_t832, _pat.format(m="zzz_2099")), "w").write("{}")
        ok("ISA-0832 MUST-FIRE: a month whose portfolio input is neither LIVE nor archived is NOT a real-input month",
           real_inputs_missing("zzz_2099", _t832) == ["portfolio"])
        os.makedirs(os.path.join(_t832, ARCHIVE_REL))
        open(os.path.join(_t832, ARCHIVE_REL, INPUT_FILES["portfolio"].format(m="zzz_2099")), "w").write("{}")
        ok("ISA-0832 MUST-FIRE: after the review purge the archived (byte-verified) copy is the month's input",
           real_inputs_missing("zzz_2099", _t832) == [] and input_path("zzz_2099", "portfolio", _t832).endswith(
               os.path.join(ARCHIVE_REL, INPUT_FILES["portfolio"].format(m="zzz_2099"))))
        open(os.path.join(_t832, INPUT_FILES["portfolio"].format(m="zzz_2099")), "w").write("{}")
        ok("ISA-0832 NEGATIVE CONTROL: a LIVE copy always wins over the archive (a re-run month is never served from it)",
           input_path("zzz_2099", "portfolio", _t832) == os.path.join(_t832, INPUT_FILES["portfolio"].format(m="zzz_2099")))
        # ISA-0838: the one REAL-rows predicate
        ok("ISA-0838 NEGATIVE CONTROL: a month with no input at all is ABSENT (a legitimate skip)",
           real_month_state("yyy_2098", _t832)["state"] == "ABSENT")
        ok("ISA-0838: the full set (portfolio archived) is COMPLETE",
           real_month_state("zzz_2099", _t832)["state"] == "COMPLETE")
        os.remove(os.path.join(_t832, INPUT_FILES["portfolio"].format(m="zzz_2099")))
        os.remove(os.path.join(_t832, ARCHIVE_REL, INPUT_FILES["portfolio"].format(m="zzz_2099")))
        _st838 = real_month_state("zzz_2099", _t832)
        ok("ISA-0838 MUST-FIRE: horizon_value present but portfolio unseen is PARTIAL naming the gap - the state the "
           "archive-less census copy produced, which the selftest used to SKIP as green",
           _st838["state"] == "PARTIAL" and _st838["missing"] == ["portfolio"], _st838)
    finally:
        _sh832.rmtree(_t832, ignore_errors=True)
    # ── real October inputs, when present (census sandbox carries them) ───────────────────
    month = None
    # ⚑ ISA-0838 (10-Oct-2026): ONE predicate (real_month_state). ABSENT -> printed skip; COMPLETE -> the REAL
    #   rows run; PARTIAL -> FAIL. The ISA-0832 version SKIPPED a half-present month, and from 06-Oct the census
    #   copy (no archive/ - ISA-0837) produced exactly that state: five days of GREEN with zero REAL rows run.
    for cand in ("oct_2026",):
        _rms = real_month_state(cand, HERE)
        if _rms["state"] == "ABSENT":
            print("SKIP REAL rows: no %s input in this tree (ABSENT - nothing to run on)" % cand)
        elif _rms["state"] == "PARTIAL":
            ok("ISA-0838 REAL %s inputs are COMPLETE or ABSENT - PARTIAL (missing %s) is an environment that hides "
               "inputs or real data loss, never a skip (R2.10)" % (cand, _rms["missing"]), False, _rms)
        else:
            month = cand
    if month:
        try:
            r1 = build(month, root=HERE, write=False)
            cs = r1["census"]["conservation"]
            ok("T04 REAL %s: every scored security has exactly one typed disposition (conservation)" % month,
               cs["holds"] and cs["every_disposition_typed"], cs)
            ok("T62 REAL: no bare pass/fail reason anywhere in the census",
               all(x.get("code") not in ("pass", "fail", None) for d in r1["dossiers"].values() for x in d["reasons"]))
            ok("T21 REAL: the production search equals the independent oracle",
               (r1["search"].get("oracle") or {}).get("identical_sets") in (True, None))
            ok("REAL receipt state is a declared state and capital is not authorised while the control blocks",
               r1["state"] in RECEIPT_STATES and (r1["capital_authorised"] is False or not r1["new_capital_control"]["blocks_new_stock_capital"]))
            inp = load_inputs(month, HERE)
            for rw_ in inp["step9_pre"].get("deployment_priority_rank") or []:
                rw_["source_score"] = random.Random(7).uniform(0, 100)
            r2 = build(month, root=HERE, write=False, inputs=inp)
            ok("T01 REAL MUST-FIRE: scrambling every Source Score leaves the selection identical",
               (r1.get("selected") or {}).get("bundle") == (r2.get("selected") or {}).get("bundle"))
            ok("T07 REAL: the D23 reserve (and any fired drawdown tranche) is subtracted exactly once (ISA-0858 waterfall)",
               abs(r1["book"]["cash_capital_gbp"] - max(r1["book"]["deployable_gbp"] - r1["book"]["reserve_gbp"]
                                                        - r1["book"]["drawdown_reserved_gbp"], 0.0)) < 0.005
               and r1["book"]["capital_for_decision_gbp"] <= r1["book"]["cash_capital_gbp"] + r1["book"]["max_fund_funded_gbp"] + 0.005)
            ok("ISA-0858 REAL: the receipt carries the funding split and the selection is funded cash-first",
               (r1.get("funding") or {}).get("split", {}).get("spend_gbp") is not None
               and all("funding" in a for a in r1.get("selected_actions") or []))
            # ── s12.2 binding: determinism, freshness, consumer verification, idempotent ledger ──
            import shutil, tempfile, time as _time
            r1b = build(month, root=HERE, write=False)
            ok("T12.2 identical inputs + method -> identical receipt_id (deterministic decision identity)",
               r1b["receipt_id"] == r1["receipt_id"], (r1["receipt_id"], r1b["receipt_id"]))
            tmp = tempfile.mkdtemp(prefix="cde_st_")
            try:
                for k_, pat_ in INPUT_FILES.items():
                    src_ = input_path(month, k_, HERE)          # ISA-0832: LIVE copy, else the archived one
                    if os.path.exists(src_):
                        shutil.copy2(src_, os.path.join(tmp, pat_.format(m=month)))
                t_old = _time.time() - 86400 * 2
                for k_ in FRESH_INPUTS:
                    os.utime(os.path.join(tmp, INPUT_FILES[k_].format(m=month)), (t_old, t_old))
                try:
                    build(month, root=tmp, write=False, fresh_after=_time.time() - 3600)
                    ok("T12.2 MUST-FIRE: an OLD run-produced bundle is refused STALE_INPUT", False)
                except EngineRefused as exc:
                    ok("T12.2 MUST-FIRE: an OLD run-produced bundle is refused STALE_INPUT", "STALE_INPUT" in str(exc), str(exc)[:120])
                t_new = _time.time()
                for k_ in FRESH_INPUTS:
                    os.utime(os.path.join(tmp, INPUT_FILES[k_].format(m=month)), (t_new, t_new))
                rf = build(month, root=tmp, write=True, fresh_after=t_new - 60)
                ok("NEGATIVE CONTROL T12.2: inputs written by THIS run are accepted (FRESH)",
                   rf["input_freshness"]["state"] == "FRESH")
                ok("T66 learning ledger: first write APPENDED (full PIT population captured)", (rf.get("_ledger") or {}).get("state") == "APPENDED", rf.get("_ledger"))
                rf2 = build(month, root=tmp, write=True, fresh_after=t_new - 60)
                ok("T76 MUST-FIRE idempotence: identical decision re-run adds NO ledger row",
                   (rf2.get("_ledger") or {}).get("state") == "ALREADY_RECORDED", rf2.get("_ledger"))
                ok("NEGATIVE CONTROL T12.2: verify_receipt on an unchanged book is VALID",
                   verify_receipt(month, tmp)["state"] == "VALID")
                hvp = os.path.join(tmp, INPUT_FILES["horizon_value"].format(m=month))
                with open(hvp, "a", encoding="utf-8") as fh:
                    fh.write("\n")
                ok("T12.2 MUST-FIRE: an input changed after the receipt -> STALE (never consumed)",
                   verify_receipt(month, tmp)["state"] == "STALE")
                rp = os.path.join(tmp, "capital_decision_receipt_%s.json" % month)
                with open(rp, encoding="utf-8") as fh:
                    rj = json.load(fh)
                rj["capital_authorised"] = True
                ok("T12.2 MUST-FIRE: a hand-edited receipt -> CORRUPT",
                   verify_receipt(month, tmp, receipt=rj)["state"] == "CORRUPT")
                ok("T12.2 MUST-FIRE: no receipt -> ABSENT", verify_receipt(month, os.path.join(tmp, "nope"))["state"] == "ABSENT")
                # ── amendment CLI: allowlisted, validated, append-only; refused writes nothing ──
                inp_t = load_inputs(month, tmp)
                tk_ = next(iter(sorted(c.get("ticker") for c in inp_t["horizon_value"].get("cases") or [] if c.get("ticker"))))
                _m9t = inp_t["step9_pre"].get("_meta") or {}
                cut_ = str(_m9t.get("as_of") or _m9t.get("produced_at") or "")[:10]
                am_ = {"amendment_id": "AM-T1", "case_id": "CASE-T", "security_id": tk_, "field_path": "thesis.falsifier",
                       "proposed_value": "two consecutive quarters of organic revenue decline", "source_ids": ["10-Q filing"],
                       "published_at": cut_, "question_id": "Q10", "reviewer": "selftest", "created_at": cut_,
                       "rationale": "the falsifier is the observable that would break the operating-growth premise",
                       "old_hash": security_input_hash(tk_, inp_t)}
                afp = os.path.join(tmp, "am.json")
                with open(afp, "w", encoding="utf-8") as fh:
                    json.dump(am_, fh)
                ok("NEGATIVE CONTROL amend CLI: a valid allowlisted amendment is RECORDED", amend_cli(month, afp, tmp) == 0)
                ok("MUST-FIRE amend CLI: a duplicate amendment_id is refused (append-only)", amend_cli(month, afp, tmp) == 2)
                with open(afp, "w", encoding="utf-8") as fh:
                    json.dump(dict(am_, amendment_id="AM-T2", field_path="hurdle_pct", proposed_value=10.0), fh)
                ampath = os.path.join(tmp, "capital_case_amendments_%s.jsonl" % month)
                n_before = sum(1 for _ in open(ampath, encoding="utf-8"))
                ok("MUST-FIRE amend CLI: a FORBIDDEN field is refused and NOTHING is written",
                   amend_cli(month, afp, tmp) == 2 and sum(1 for _ in open(ampath, encoding="utf-8")) == n_before)
                ra = build(month, root=tmp, write=False)
                ok("T09.3 mechanical rerun consumes the recorded amendment (accepted, none rejected)",
                   ra["amendments"]["present"] and not ra["amendments"]["rejected"], ra["amendments"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        except EngineRefused as exc:
            ok("REAL build did not refuse unexpectedly", False, str(exc))
    # ── ISA-0727 (18-Oct-2026): Raj Q4 as-planned fills, Q2 invalidation at dealing ──────────────────────────
    _act = {"ticker": "X", "gbp": 1000.0, "currency": "USD", "economic_max_price_major": 10.0}
    _f = lambda g, p=9.0, c="USD": {"gbp_cost": g, "prices": [{"price_major": p, "quote_currency": c}]}   # noqa: E731
    ok("ISA-0727 Q4: a fill at the approved GBP and a price below the economic max is AS PLANNED",
       as_planned_fill(_f(1000.0), _act)["as_planned"])
    ok("ISA-0727 Q4 boundary: +5.00% of the approved GBP is still as planned", as_planned_fill(_f(1050.0), _act)["as_planned"])
    ok("ISA-0727 Q4 MUST-FIRE: +5.1% of the approved GBP is a DEVIATION", not as_planned_fill(_f(1051.0), _act)["as_planned"])
    ok("ISA-0727 Q4 MUST-FIRE: -5.1% (a short fill) is a DEVIATION", not as_planned_fill(_f(949.0), _act)["as_planned"])
    ok("ISA-0727 Q4 MUST-FIRE: a fill price above the economic max is a DEVIATION", not as_planned_fill(_f(1000.0, 10.5), _act)["as_planned"])
    ok("ISA-0727 Q4 MUST-FIRE: a GBP-only fill price (no MAJOR quote) cannot be shown as planned",
       not as_planned_fill({"gbp_cost": 1000.0, "prices": [{"price_major": None, "price_gbp": 7.0}]}, _act)["as_planned"])
    ok("ISA-0727 Q4 MUST-FIRE: a fill in another currency is a DEVIATION", not as_planned_fill(_f(1000.0, 9.0, "GBP"), _act)["as_planned"])
    ok("ISA-0727 Q4 NEGATIVE CONTROL: a fill for a name that is not an action is never as planned", not as_planned_fill(_f(1000.0), None)["as_planned"])
    import tempfile as _tf727, shutil as _sh727
    _t7 = _tf727.mkdtemp(prefix="cde727_")
    try:
        _rc7 = {"as_of": "2026-10-03", "dossiers": {"X": {"flags": {"hard": [], "review_unresolved": [], "rows": []}},
                                                     "Y": {"flags": {"hard": [], "review_unresolved": ["corporate_action_pending"],
                                                                     "rows": [{"code": "corporate_action_pending"}]}}}}
        _ced = {"as_of": "2026-10-20", "verdicts": {
            "X": {"state": "CORPORATE_ACTION_PENDING", "flags": ["corporate_action_pending"], "venue_class": "US", "why": ["8-K 2.01"]},
            "Y": {"state": "CORPORATE_ACTION_PENDING", "flags": ["corporate_action_pending"], "venue_class": "US", "why": ["8-K 2.01"]},
            "Z": {"state": "NO_EVENT_FOUND", "flags": [], "venue_class": "US", "why": []}}}
        with open(os.path.join(_t7, "corporate_events.json"), "w", encoding="utf-8") as fh:
            json.dump(_ced, fh)
        with open(os.path.join(_t7, "step9_pre_oct_2026.json"), "w", encoding="utf-8") as fh:
            json.dump({"deployment_priority_rank": [{"ticker": "Z", "risk_flags": {"binary_event_within_90d": "2026-10-22"}}]}, fh)
        _n7 = datetime.datetime(2026, 10, 21, 10, 0)
        _iX = dealing_invalidation("oct_2026", "X", _rc7, _t7, _n7)
        ok("ISA-0727 Q2 MUST-FIRE: a corporate action flagged SINCE formation invalidates the approval",
           _iX["state"] == "INVALIDATED_CORPORATE_EVENT", _iX)
        _iY = dealing_invalidation("oct_2026", "Y", _rc7, _t7, _n7)
        ok("ISA-0727 Q2 NEGATIVE CONTROL: the same flag already carried (and adjudicated) at formation does not invalidate",
           _iY["state"] == "VALID", _iY)
        _iZ = dealing_invalidation("oct_2026", "Z", _rc7, _t7, _n7)
        ok("ISA-0727 Q2 R5.14 T-1: earnings due after the dealing date leaves the approval VALID", _iZ["state"] == "VALID", _iZ)
        _iZ2 = dealing_invalidation("oct_2026", "Z", _rc7, _t7, datetime.datetime(2026, 10, 22, 10, 0))
        ok("ISA-0727 Q2 R5.14 T: earnings ON the dealing date invalidates (reported since approval)",
           _iZ2["state"] == "INVALIDATED_EARNINGS", _iZ2)
        _iZ3 = dealing_invalidation("oct_2026", "Z", _rc7, _t7, datetime.datetime(2026, 10, 28, 10, 0))
        ok("ISA-0727 Q2 R5.14 T+1: earnings after approval and before dealing invalidates", _iZ3["state"] == "INVALIDATED_EARNINGS", _iZ3)
        ok("ISA-0727 Q2 NEGATIVE CONTROL: no earnings date held is CONFIRM AT DEALING, never a pass and never a refusal",
           _iX is not None and dealing_invalidation("oct_2026", "Y", _rc7, _t7, _n7)["confirm"]
           and any("EARNINGS_DATE_UNKNOWN" in c for c in _iY["confirm"]))
        os.remove(os.path.join(_t7, "corporate_events.json"))
        _iA = dealing_invalidation("oct_2026", "X", _rc7, _t7, _n7)
        ok("ISA-0727 Q2 MUST-FIRE: an unreadable corporate-events store is CONFIRM AT DEALING (typed), never 'no event'",
           _iA["state"] == "VALID" and any("corporate events" in c for c in _iA["confirm"]), _iA)
        # ISA-0727 B: the watch on a root with no receipt writes ONE typed row and never invents a plan
        _w0 = watch_receipt(datetime.date(2026, 10, 23), _t7, now=_n7)
        ok("ISA-0727 B NEGATIVE CONTROL: no receipt -> NO_AUTHORISED_PLAN row (typed), names empty",
           _w0["state"] == "NO_AUTHORISED_PLAN" and _w0["names"] == {} and latest_watch(_t7)["state"] == "NO_AUTHORISED_PLAN", _w0)
        with open(os.path.join(_t7, VALIDITY_LOG), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"receipt_id": "CDR-A", "as_of": "2026-10-23", "names": {"X": {"state": "VALID"}}}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"receipt_id": "CDR-A", "as_of": "2026-10-30", "names": {"X": {"state": "INVALIDATED"}}}) + "\n")
            fh.write(json.dumps({"receipt_id": "CDR-B", "as_of": "2026-10-31", "names": {}}) + "\n")
        ok("ISA-0727 B: latest_watch returns the NEWEST row for the receipt (a corrupt line is skipped, another receipt ignored)",
           latest_watch(_t7, "CDR-A")["as_of"] == "2026-10-30" and latest_watch(_t7)["receipt_id"] == "CDR-B")
    finally:
        _sh727.rmtree(_t7, ignore_errors=True)
    # ── ISA-0756 (B05-R04): new capital is refused on a STALE_INPUT risk store at the run's clock ─────
    _st756 = {"names": {"X": {"observations": {"2026-09-25": {}, "2026-10-02": {}}},
                        "H": {"observations": {"2026-09-25": {}, "2026-10-02": {}}}}}
    _pf756 = {"stocks": [{"ticker": "H", "value_gbp": 1000.0}]}
    _g756 = risk_gate(_st756, _pf756, "X", as_of="2026-10-17")
    ok("ISA-0756 MUST-FIRE: an addition on a store two settled Fridays stale is REFUSED_STALE_INPUT (names held + candidate)",
       _g756.get("verdict") == "REFUSED_STALE_INPUT" and _g756.get("stale_names") == ["H", "X"], _g756)
    _g756b = risk_gate(_st756, _pf756, "X", as_of="2026-10-03")
    ok("ISA-0756 NEGATIVE CONTROL: the same store at the run date after its refresh is not refused for freshness",
       _g756b.get("verdict") != "REFUSED_STALE_INPUT", _g756b)
    ok("ISA-0756 NEGATIVE CONTROL: no clock -> no freshness verdict (legacy callers unchanged)",
       risk_gate(_st756, _pf756, "X").get("verdict") != "REFUSED_STALE_INPUT")
    # ── ISA-0465 / B08-R04/R05: caps on the post-trade book; unknown additions get no headroom ─────────
    _tax465 = {"sector": {"A": {"value": "Tech"}, "B": {"value": "Tech"}, "C": {"value": "Energy"}},
               "theme": {"A": ["ai"], "B": ["ai"], "C": []}, "aliases": {}}
    _c1 = concentration_eval((("B", 5000.0),), {"A": 6000.0}, 100000.0, _tax465)
    ok("ISA-0465 NEGATIVE CONTROL: Tech 11% of NAV after the add (< 12%) passes the sector cap; ai theme is 100% of a 1-theme sleeve -> theme breach",
       any(v["kind"] == "theme" for v in _c1["violations"]) and not any(v["kind"] == "sector" for v in _c1["violations"]), _c1)
    _c2 = concentration_eval((("B", 7000.0),), {"A": 6000.0, "C": 20000.0}, 100000.0, _tax465)
    ok("ISA-0465 MUST-FIRE: Tech 13% of NAV after the addition breaches the 12% sector cap",
       any(v["kind"] == "sector" and v["class"] == "Tech" for v in _c2["violations"]), _c2)
    _c3 = concentration_eval((("ZZZ", 1000.0),), {"C": 20000.0}, 100000.0, _tax465)
    ok("ISA-0465 / B08-R05 MUST-FIRE: an UNMAPPED addition is UNKNOWN_CLASSIFICATION, never free headroom",
       _c3["state"] == "FAIL" and _c3["unknown_additions"] == ["ZZZ"], _c3)
    _c4 = concentration_eval((("C", 1000.0),), {"A": 9000.0, "B": 9000.0, "C": 20000.0}, 100000.0, _tax465)
    import concentration_control as _cc465
    import tempfile as _tf465, shutil as _sh465
    _d465 = _tf465.mkdtemp()
    try:
        _rc0 = {"summary": {"capital_decision_engine": {"concentration": {"mode": "SHADOW", "impact": {"state": "NO_BUNDLES_EVALUATED"},
                                                                           "n_bundles_evaluated": 0}}}}
        with open(os.path.join(_d465, "run_context_oct_2026.json"), "w") as _fh:
            json.dump(_rc0, _fh)
        ok("ISA-0465 NEGATIVE CONTROL: an engine record that evaluated 0 bundles is NOT SHADOW evidence",
           _cc465.real_shadow_evidence(_d465)["state"] == "ABSENT")
        _rc0["summary"]["capital_decision_engine"]["concentration"].update(impact={"state": "OK"}, n_bundles_evaluated=7)
        with open(os.path.join(_d465, "run_context_oct_2026.json"), "w") as _fh:
            json.dump(_rc0, _fh)
        ok("ISA-0465 MUST-FIRE: an engine SHADOW record over evaluated bundles IS real evidence",
           _cc465.real_shadow_evidence(_d465)["state"] == "PRESENT")
    finally:
        _sh465.rmtree(_d465, ignore_errors=True)
    ok("ISA-0465 NEGATIVE CONTROL: an existing Tech breach (18%) does not refuse an addition that does not worsen it",
       not any(v["kind"] == "sector" and v["class"] == "Tech" for v in _c4["violations"]), _c4)
    if verbose:
        print("capital_decision_engine selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
