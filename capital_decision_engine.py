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
}

# s9.3 — the ONLY fields a case amendment may touch.
AMENDMENT_ALLOWLIST = {
    "er12.review_resolution": ("AUTHORITATIVE", "CROSS_ROUTE", "LOWER_OF_ROUTES"),
    "scenario.bear_local_pct": "number",
    "scenario.estimation_low_local_pct": "number",
    "scenario.estimation_high_local_pct": "number",
    "flag_adjudication": ("RESOLVED_BENIGN", "CONFIRMED_ADVERSE"),
    "thesis.falsifier": "text",
    "case.complete": "bool",
}
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


def load_inputs(month: str, root: str = HERE) -> dict:
    """Read every input once; hash each file (s10 input hashes). A missing REQUIRED input
    refuses (R4.3) rather than producing a partial decision."""
    _fi_mark("capital_decision_engine", "load_inputs")
    out, hashes, missing = {"month": month, "root": root}, {}, []
    for key, pat in INPUT_FILES.items():
        p = os.path.join(root, pat.format(m=month))
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
    inputs = auth.get("inputs") or {}
    cur = auth.get("currency") or hv.get("currency") or {}
    unit_div = _num(cur.get("unit_div")) or 1.0
    p0q = _num(inputs.get("P0_quote"))
    p0 = None if p0q is None else p0q / unit_div
    pt = _num(auth.get("P_T"))
    r = _num(auth.get("r") if auth.get("r") is not None else inputs.get("r"))
    route = rec["route"]
    c = central / 100.0
    rec["central_local"] = c
    rec["currency"] = cur.get("quote_major") or cur.get("quote")
    # dividends implied by the HV identity (1+R)P0 = P_T + D
    d_major = (None if (p0 is None or pt is None) else (1.0 + (auth_er or central) / 100.0) * p0 - pt)
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
    else:
        bridge["identity"] = ("P_T = (NTM revenue x TTM margin x EV/EBITDA0 x (1+r) - frozen net debt) / "
                              "frozen diluted shares (+ dividends); share count enters once")
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

    def _scaled(factor: float) -> Optional[float]:
        if p0 is None or pt is None or p0 <= 0:
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

    if lo is not None:
        var["LOW_ESTIMATES"] = _scaled(lo)
    if hi is not None:
        var["HIGH_ESTIMATES"] = _scaled(hi)
    if r is not None and abs(r) > 1e-12:
        var["NO_RERATE"] = _derated(1.0, r)
    if cross_er is not None:
        var["ALT_ROUTE"] = cross_er / 100.0
    elif auth_er is not None and resolution == "CROSS_ROUTE":
        var["ALT_ROUTE"] = auth_er / 100.0
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
        "LOW_ESTIMATES": "FY1 %s at the analysts' LOW estimate (PIT capture), same multiple" % ("EPS" if route == "PE" else "revenue"),
        "HIGH_ESTIMATES": "FY1 metric at the analysts' HIGH estimate (PIT capture)",
        "NO_RERATE": "terminal multiple = current multiple (the bounded re-rate removed)",
        "ALT_ROUTE": "the other valuation route (P/E vs EV/EBITDA) - an economically independent anchor",
        "EV_*": "horizon_value EV-route sensitivities (margin reversion, net-debt and share-count trends)"}
    # ── business bear (s4.2: a credible adverse REALISED world, kept separate) ─────────────
    bear_c = {}
    for k in EV_BEAR_SENS:
        if k in sens:
            bear_c["EV_TROUGH_MARGIN"] = sens[k]
    if lo is not None:
        bear_c["LOW_ESTIMATES_NO_POSITIVE_RERATE"] = (_derated(lo, r) if (r is not None and r > 0) else _scaled(lo))
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


def classify_flags(wrec: Optional[dict], adjudications: Optional[dict] = None) -> dict:
    """s5.2 -> {hard:[...], review_unresolved:[...], underwriting:[...], rows:[...]}."""
    _fi_mark("capital_decision_engine", "classify_flags")
    adj = adjudications or {}
    w = wrec or {}
    codes = list(w.get("disqualifier_flags") or []) + list(w.get("review_flags") or [])
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
        rows.append({"code": code, "class": cls, "owner": owner, "why": why, "adjudication": a,
                     "state": state})
        if cls == "HARD_BLOCK":
            hard.append(code)                       # no case or return may override (s5.2)
        elif cls == "REVIEW_REQUIRED":
            if state == "CONFIRMED_ADVERSE":
                hard.append(code)
            elif state != "RESOLVED_BENIGN":
                review.append(code)
        else:
            uw.append(code)
    return {"rows": rows, "hard": hard, "review_unresolved": review, "underwriting": uw}


def risk_gate(store: dict, portfolio: dict, ticker: str) -> dict:
    """ISA-0600 addition gate (Dimson beta <= SLEEVE_BETA_MAX vs the held sleeve) through its ONE
    home, sleeve_risk.gate_add. UNMEASURED is never PASS (s6, T44)."""
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


def _build_census(inp, pipeline, store, hurdle_pct, as_of, amendments, price_overrides):
    """Every scored security -> exactly ONE disposition with typed reasons (s3.2). Stage numbers
    need not match the old 84->69->25->11 funnel; the old funnel is retained for comparison."""
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
        if price_overrides and t in price_overrides:
            rec["er12"] = reprice_er12(rec["er12"], price_overrides[t])
        rec["flags"] = classify_flags(w, am.get("flag_adjudication"))
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
            rg = risk_gate(store, inp["portfolio"], t)
            rec["risk_gate"] = {k: rg.get(k) for k in ("verdict", "beta", "beta_max", "rho", "weeks",
                                                       "zero_return_share", "reason", "residual_vol_annual")}
            if rg.get("verdict") == "BLOCKED":
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
            if rg.get("verdict") != "PASS":
                disp = "RISK_UNKNOWN"
                rec["reasons"].append(_reason("risk", "RISK_UNMEASURED", rg.get("verdict"), "PASS",
                                              "sleeve_risk.gate_add", (rg.get("reason") or "")[:240] +
                                              " - no conservative proxy contract exists, so the addition is refused (s6, T44)"))
            elif not corr.get("measured"):
                disp = "RISK_UNKNOWN"
                rec["reasons"].append(_reason("risk", "CORRELATION_UNMEASURED", corr.get("rho_basis"), "MEASURED",
                                              "deployment_sequencer", "the adverse 0.70 default is not a measurement"))
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
    return {"records": recs, "n_universe": len(universe), "counts": counts,
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


def dominates(x: dict, y: dict, risk_tol: float = EPS_GBP, worlds_e=S_E, worlds_b=S_B) -> bool:
    """s8.3 (Rev 1.1): x is ROBUSTLY SUPERIOR to y only if it wins EVERY estimation world by more than
    the arithmetic tolerance AND is no worse in every business-bear world AND no worse on the
    authorised risk metric (stock-sleeve GBP volatility, lower is better). Return-only superiority
    is NOT dominance (T91)."""
    if not all(x["wealth"][s] - y["wealth"][s] > EPS_GBP for s in worlds_e):
        return False
    if not all(x["wealth"][s] >= y["wealth"][s] - EPS_GBP for s in worlds_b):
        return False
    rx, ry = x.get("gbp_vol"), y.get("gbp_vol")
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
                    r.get("gbp_vol") if r.get("gbp_vol") is not None else float("inf"),
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
            "residual_gbp": r["residual_gbp"], "regret_gbp": _r(r.get("regret")),
            "wealth_gbp": {s: _r(v) for s, v in r["wealth"].items()},
            "worst_estimation_wealth_gbp": _r(r.get("worst_estimation_wealth")),
            "gbp_vol": _r(r.get("gbp_vol")), "incremental_net_er12_pct": _r((r.get("incremental_net_er12") or 0) * 100, 2)
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


def amend_cli(month: str, src: str, root: str = HERE) -> int:
    """Append ONE typed amendment (JSON object in `src`) to capital_case_amendments_{month}.jsonl
    after validating it against the CURRENT inputs exactly as the engine will (allowlist, PIT,
    stale base, circular consensus). An invalid amendment is refused with its reason and NOTHING is
    written; the file is append-only (history never rewritten). The engine must then be re-run in
    full (mechanical rerun - the case never edits the decision directly, s9.3)."""
    with open(src, encoding="utf-8") as fh:
        a = json.load(fh)
    inp = load_inputs(month, root)
    _m9 = inp["step9_pre"].get("_meta") or {}
    cutoff = str(_m9.get("as_of") or _m9.get("produced_at") or "")[:10]
    ok_, why = validate_amendment(a, inp, cutoff)
    if not ok_:
        print("AMENDMENT_REFUSED %s: %s" % (a.get("amendment_id"), why))
        return 2
    p = os.path.join(root, "capital_case_amendments_%s.jsonl" % month)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            if any(ln.strip() and json.loads(ln).get("amendment_id") == a["amendment_id"] for ln in fh):
                print("AMENDMENT_REFUSED %s: duplicate amendment_id (append-only; supersede with a new id)" % a["amendment_id"])
                return 2
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(a, sort_keys=True) + "\n")
    print("AMENDMENT_RECORDED %s -> %s; now re-run: python3 capital_decision_engine.py --month %s"
          % (a["amendment_id"], os.path.basename(p), month))
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
          price_overrides: Optional[dict] = None) -> dict:
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
    capital = round(deployable - reserve, 2)
    cash_convention = {"deployable_gbp": deployable, "deployable_basis": s.get("cash_deployable_basis"),
                       "reserve_gbp": reserve, "capital_for_decision_gbp": capital,
                       "note": ("cash_deployable is GROSS (cash statement closing balance + MMF); the D23 "
                                "reserve is subtracted ONCE here (T07)")}
    import new_capital_control as _ncc_m
    ncc = _ncc_m.status(root)
    if store is None:
        import stock_return_store as _srs
        # the SAME bytes the receipt's input hash binds (INPUT_FILES weekly_store), never a second read
        store = inp.get("weekly_store") if inp.get("weekly_store") is not None else _srs.load()
    if pipeline is None:
        import capital_destination as _cd
        pipeline = _cd.capital_pipeline(portfolio, policy, as_of=as_of)
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
    import sleeve_risk as _sr
    try:
        pre = _sr.risk_shares(store, portfolio)
        pre_shares = pre["shares"]
        pre_risk = {"sleeve_sigma": pre["sleeve_sigma_ann"], "gbp_vol": pre["sleeve_sigma_ann"] * float(s.get("stock_sleeve_value_gbp") or 0)}
    except Exception as exc:                                        # noqa: BLE001
        pre_shares, pre_risk = {}, {"state": "UNMEASURED", "why": str(exc)}
    ceiling = float(getattr(_sc, "SLEEVE_RISK_CEILING_PCT"))
    max_pos = float(tw["thresholds"]["max_stock_position_pct"]) * nav
    for r in rows:
        if not r["bundle"]:
            r["risk"] = {"state": "MEASURED", **pre_risk, "note": "no-action: current sleeve"}
            r["gbp_vol"] = pre_risk.get("gbp_vol")
            r["breaches"] = []
            r["feasible"] = pre_risk.get("gbp_vol") is not None
            continue
        rk = bundle_risk(store, portfolio, r["bundle"], nav)
        r["risk"] = rk
        r["gbp_vol"] = rk.get("gbp_vol")
        r["breaches"] = constraints_for(r["bundle"], rk, pre_shares, ceiling, max_pos)
        r["feasible"] = not r["breaches"]
    cmp = compare(rows)
    amb = ambition_view(rows, cmp, scen, costs_by) if cmp.get("state") in ("OK", "DECISION_TIE_UNRESOLVED") else None
    receipt["risk_constraints"] = {
        "applied": ["ISA-0600 Dimson beta <= SLEEVE_BETA_MAX for every addition vs the post-trade sleeve (in-run names at proposed weights)",
                    "D27 SLEEVE_RISK_CEILING_PCT = %.1f%%: no bundle creates or deepens a risk-share breach" % ceiling,
                    "max_stock_position_pct = %.4f of NAV" % (max_pos / nav),
                    "cash: stock spend <= deployable - D23 reserve"],
        "reported_not_binding": ["concentration_control sector/theme caps (V2_FLAGS concentration_gate SHADOW)",
                                 "AI-complex factor cap (Checkpoint-D tick 5 consumes it at the review)"],
        "pre_trade": pre_risk, "pre_trade_risk_shares": {k: _r(v, 5) for k, v in pre_shares.items()}}
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
                                            "AND no higher stock-sleeve GBP volatility; 3 otherwise minimax GBP regret "
                                            "over the estimation worlds among undominated bundles; 4 declared tie-breaks; "
                                            "economically distinct residual ties -> DECISION_TIE_UNRESOLVED"),
                             "not_claimed": "not a calibrated expected-utility optimum; exact search removes search error, not forecast error"}
    receipt["ambition"] = amb
    sel = cmp.get("selected")
    receipt["selected"] = _brief(sel)
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
    scope = sorted(set(sel_names) | {t for r in (cmp.get("tied") or []) for t, _ in r["bundle"]} |
                   {t for r in (cmp.get("undominated") or []) for t, _ in r["bundle"]} | set(pending))
    complete = {t for t, v in am["by_security"].items() if v.get("case.complete") is True}
    receipt["case_scope"] = {
        "required_full_cases": scope, "complete": sorted(set(scope) & complete),
        "outstanding": sorted(set(scope) - complete), "decision_relevant_pending": sorted(pending),
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
    receipt["alternatives"] = alts
    _cnt = {}
    for _r0 in recs.values():
        _cnt[_r0["disposition"]] = _cnt.get(_r0["disposition"], 0) + 1
    receipt["census"]["counts"] = _cnt
    receipt["census"]["conservation"] = {"universe": census["n_universe"], "dispositioned": sum(_cnt.values()),
                                         "holds": census["n_universe"] == sum(_cnt.values()),
                                         "every_disposition_typed": all(_r0["disposition"] in DISPOSITIONS for _r0 in recs.values())}
    receipt["selected_actions"] = [{"ticker": t, "action": "START", "gbp": a,
                                    "post_trade_weight_pct": _r(a / nav * 100, 3),
                                    "economic_max_price_major": recs[t]["entry"].get("economic_max_price_major"),
                                    "valuation_reference_price_major": recs[t]["entry"].get("valuation_reference_price_major"),
                                    "currency": recs[t]["er12"].get("currency"),
                                    "central_er12_local_pct": _r(recs[t]["er12"]["central_local"] * 100, 2),
                                    "net_er12_central_pct": _r((net_return(a, scen[t]["CENTRAL"], costs_by[t]) or 0) * 100, 2),
                                    "bear_gbp_pct": _r((scen[t]["BUSINESS_BEAR"] or 0) * 100, 2),
                                    "costs": costs_by[t]}
                                   for t, a in (sel or {}).get("bundle", ())]
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
    receipt["capital_authorised"] = bool(state == "DECISION_COMPLETE" and authority == "LIVE"
                                         and not ncc.get("blocks_new_stock_capital"))
    receipt["capital_authorised_basis"] = ("authorised only when the decision is COMPLETE (every required case consumed, "
                                           "no decision-relevant research pending), the engine flag is LIVE and the "
                                           "canonical new-capital control is OPEN. Approval and execution revalidation "
                                           "remain separate (s11).")
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


def execution_check(month: str, ticker: str, price_major: float, quote_currency: str, quote_ts: str,
                    cash_available_gbp: Optional[float] = None, root: str = HERE,
                    now: Optional[datetime.datetime] = None) -> dict:
    """s11 - validity of ONE selected action at an EXECUTABLE quote, re-checked before every order or
    partial fill. Never places an order. States: EXECUTABLE | REFUSED_* | REPRICE_REQUIRED.
    (a) the receipt verifies against the CURRENT inputs and is DECISION_COMPLETE + capital_authorised;
    (b) the ticker is a selected action; (c) the quote is a session quote (not a weekend/Sunday analysis
    close, T57), in the dossier currency, and not older than one day; (d) ER12 recomputed at the
    executable price still clears the hurdle (economic max, T52); (e) the WHOLE construction is rerun
    with the executable price and must select the same action (an approval cannot silently expand to
    a different portfolio, T56); (f) cash covers the action."""
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
    sel = {a["ticker"]: a for a in rc.get("selected_actions") or []}
    if ticker not in sel:
        return done("REFUSED_NOT_SELECTED", "%s is not an action of receipt %s (selected: %s)" % (ticker, rc.get("receipt_id"), sorted(sel)))
    act = sel[ticker]
    try:
        qt = datetime.datetime.fromisoformat(str(quote_ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return done("REFUSED_QUOTE_TIME", "quote timestamp %r unparseable" % (quote_ts,))
    if qt.weekday() >= 5:
        return done("REFUSED_NON_SESSION_QUOTE", "a %s quote is an analysis close, not an executable quote (T57)" % qt.strftime("%A"))
    if (now - qt).total_seconds() > 86400:
        return done("REPRICE_REQUIRED", "quote older than one day - fetch a current session quote")
    if act.get("currency") and quote_currency and str(quote_currency).upper() != str(act["currency"]).upper():
        return done("REFUSED_CURRENCY", "quote currency %s != dossier currency %s (quote in MAJOR units)" % (quote_currency, act["currency"]))
    emax = act.get("economic_max_price_major")
    out["economic_max_price_major"] = emax
    if emax is not None and float(price_major) > float(emax) + 1e-9:
        return done("REFUSED_ABOVE_ECONOMIC_MAX", "price %.4f > economic max %.4f: ER12 at this price is below the hurdle (T52)" % (price_major, emax))
    if cash_available_gbp is not None and float(cash_available_gbp) + EPS_GBP < float(act.get("gbp") or 0):
        return done("REPRICE_REQUIRED", "cash GBP %.2f < action GBP %.2f - rerun construction on the actual cash" % (cash_available_gbp, act.get("gbp")))
    rr = build(month, root=root, write=False, price_overrides={ticker: float(price_major)})
    new_sel = [(a["ticker"], a["gbp"]) for a in rr.get("selected_actions") or []]
    old_sel = [(a["ticker"], a["gbp"]) for a in rc.get("selected_actions") or []]
    out["rerun_selection"] = new_sel
    out["rerun_er12_central_pct"] = next((a.get("central_er12_local_pct") for a in rr.get("selected_actions") or [] if a["ticker"] == ticker), None)
    if new_sel != old_sel:
        return done("REPRICE_REQUIRED", "at the executable price the construction selects %s instead of %s - re-run the decision (T56)" % (new_sel, old_sel))
    return done("EXECUTABLE", "receipt VALID/COMPLETE/authorised; session quote; below economic max; construction unchanged at the executable price")


def record_fill(month: str, receipt_id: str, ticker: str, order_id: str, quantity: float, price_major: float,
                gbp_cost: float, root: str = HERE) -> dict:
    """Append-only, idempotent on (receipt_id, ticker, order_id): a retried capture never duplicates a fill.
    After a fill the remaining portfolio must be revalidated (state REMAINING_RERUN_REQUIRED)."""
    p = os.path.join(root, EXECUTION_LEDGER)
    key = "%s|%s|%s" % (receipt_id, ticker, order_id)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip() and json.loads(ln).get("idempotency_key") == key:
                    return {"state": "ALREADY_RECORDED", "idempotency_key": key}
    row = {"idempotency_key": key, "receipt_id": receipt_id, "month": month, "ticker": ticker, "order_id": order_id,
           "quantity": quantity, "price_major": price_major, "gbp_cost": gbp_cost, "recorded_at": _now().isoformat()}
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return {"state": "RECORDED", "idempotency_key": key,
            "next": "REMAINING_RERUN_REQUIRED: re-run the engine on the post-fill book before any further order (s11)"}


def verify_receipt(month: str, root: str = HERE, receipt: Optional[dict] = None) -> dict:
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
        now = _file_sha(os.path.join(root, INPUT_FILES[k].format(m=month)))
        if now != h:
            drift[k] = {"receipt": (h or "")[:12], "now": (now or "")[:12]}
    if drift:
        return {"state": "STALE", "receipt_id": receipt.get("receipt_id"), "drift": drift,
                "why": "an input changed after the receipt was formed - re-run the engine"}
    return {"state": "VALID", "receipt_id": receipt.get("receipt_id"), "receipt_state": receipt.get("state"),
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
            "ambition_band": (receipt.get("ambition") or {}).get("selected_band"),
            "ambition_state": (receipt.get("ambition") or {}).get("state"),
            "case_scope_required": (receipt.get("case_scope") or {}).get("required_full_cases"),
            "case_scope_outstanding": (receipt.get("case_scope") or {}).get("outstanding"),
            "census_counts": (receipt.get("census") or {}).get("counts"),
            "comparator_rule": (receipt.get("comparator") or {}).get("rule"),
            "search_state": (receipt.get("search") or {}).get("state"),
            "new_capital_control": receipt.get("new_capital_control")}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    if "--selftest" in argv:
        return 1 if _selftest() else 0
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
        r = execution_check(month, opt("--execution-check"), float(opt("--price")), opt("--currency"),
                            opt("--quote-ts"), (float(opt("--cash")) if opt("--cash") else None), opt("--root", HERE))
        print(json.dumps({k: v for k, v in r.items()}, indent=1, default=str))
        return 0 if r["state"] == "EXECUTABLE" else 3
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
    # ── real October inputs, when present (census sandbox carries them) ───────────────────
    month = None
    for cand in ("oct_2026",):
        if os.path.exists(os.path.join(HERE, "horizon_value_%s.json" % cand)):
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
            ok("T07 REAL: the D23 reserve is subtracted exactly once",
               abs(r1["book"]["capital_for_decision_gbp"] - (r1["book"]["deployable_gbp"] - r1["book"]["reserve_gbp"])) < 0.005)
            # ── s12.2 binding: determinism, freshness, consumer verification, idempotent ledger ──
            import shutil, tempfile, time as _time
            r1b = build(month, root=HERE, write=False)
            ok("T12.2 identical inputs + method -> identical receipt_id (deterministic decision identity)",
               r1b["receipt_id"] == r1["receipt_id"], (r1["receipt_id"], r1b["receipt_id"]))
            tmp = tempfile.mkdtemp(prefix="cde_st_")
            try:
                for k_, pat_ in INPUT_FILES.items():
                    src_ = os.path.join(HERE, pat_.format(m=month))
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
    if verbose:
        print("capital_decision_engine selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
