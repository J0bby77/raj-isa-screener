#!/usr/bin/env python3
"""whole_book_stress.py - P-D (B08-R06 / R09, ISA-0888): deterministic stress, reverse stress and Raj's loss appetite
on the WHOLE ISA.

WHAT IT ANSWERS
---------------
"If markets break, what does THIS book lose, in pounds and against Raj's limit, and what does that do to the 2037
target?" - with every number carrying its basis (MEASURED from the book's own histories, PROXY, or a named BOUND).
Scenario losses are NOT probabilities and NOT forecasts (B08-R06). Nothing here sells anything.

THE APPETITE (Raj, 24-Oct-2026 ~17:45 BST, chat answers to three questions, verbatim choices):
  limit      "50% (Recommended)"                         -> isa_policy.RISK_APPETITE["max_drawdown_pct"] = 50
  response   "Review + guard new risk (Recommended)"     -> a breach is a Step 5 review item with a deadline; a
             new buy that makes the severe-scenario loss worse while it is at/above the limit is sent for REVIEW;
             nothing is sold automatically
  basis      "Fall from peak, flow-adjusted (Recommended)" -> wealth_record (flow-adjusted index) is the realised
             measure; the GBP loss against money paid in is shown alongside, never the trigger
The policy has ONE home (isa_policy.RISK_APPETITE); this module reads it and never restates it.

THE GOVERNING SEVERE SCENARIO (used for the appetite comparison and the guard)
------------------------------------------------------------------------------
SEVERE_GFC_SCALE: broad equity -50% (MSCI World GBP fell 46.66% peak-to-trough in 2001-03 - MSCI factsheet
30-Sep-2026; the Risk_Illustrations note recommended the 50% stress). EACH holding takes the WORSE of two bounded models:
  (a) uniform  - every equity pound falls 50% (correlations converge in a crash; diversification fails), and
  (b) beta     - the holding's Dimson beta to the broad-equity proxy x 50%,
capped at -100% (no leverage, no shorting in an ISA). Cash and the MMF fall 0. This is a per-holding worst-of-two
BOUND, chosen because (a) alone understates high-beta names (MU/AVGO beta ~2.3) and (b) alone understates non-US funds
whose beta to an S&P proxy is low in calm markets.

OTHER SCENARIOS (B08-R06): broad equity -20/-30/-50 (beta and uniform shown separately); historical replays of four
episodes on the current book (Q4-2018, COVID-2020, 2022 bear, 2025 tariff shock: own history where it exists, beta x
proxy where not - coverage published); AI-complex -40% (look-through lower..upper bound); sterling +15%; trust discount
+15pp (SMT); single-issuer and binary failure (-100%); funding interruption (contributions stop). Rates: no direct
duration is held - N/A with reason (the equity de-rating it would cause is inside the equity scenarios). Liquidity:
see scale_readiness (B08-R10).

REVERSE STRESS: (1) the broad-equity fall at which the governing rule reaches the appetite; (2) the whole-ISA loss at
which the return required to reach the GBP 1m floor by the target date exceeds the D1c guardrail (18%, existing
TARGET_ATTAINABILITY_REVIEW threshold in target_state.json - read, not invented).

ROLLBACK (R4.13): isa_policy.RISK_APPETITE["enforce_guard"] = False -> the guard publishes NOT_ENFORCED and never sets
REVIEW; the stress and the appetite comparison are still reported.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

SCHEMA = "whole_book_stress/1"
SEVERE_SHOCK = 0.50
SEVERE_ID = "SEVERE_GFC_SCALE"
EQUITY_SHOCKS = (0.20, 0.30, 0.50)
# Named bounds (not probabilities, not adopted thresholds - B08-R06). Each carries its reason.
NAMED_SHOCKS = {
    "AI_COMPLEX": (0.40, "AI/semis complex -40%: the 2022 drawdown of the semiconductor index was ~-45% peak-to-trough; "
                         "-40% is a round bound inside it"),
    "GBP_UP": (0.15, "sterling +15% against every foreign currency held (GBP/USD moved >15% in 2016 and 2022)"),
    "TRUST_DISCOUNT": (0.15, "a listed trust's discount widens 15 percentage points (SMT moved from a premium to a "
                             "~20% discount in 2022)"),
}
EPISODES = {
    "Q4_2018": ("2018-09-28", "2018-12-24", "Q4-2018 sell-off"),
    "COVID_2020": ("2020-02-19", "2020-03-23", "COVID crash"),
    "BEAR_2022": ("2021-12-31", "2022-10-12", "2022 rates/tech bear market"),
    "TARIFF_2025": ("2025-02-18", "2025-04-08", "2025 tariff shock"),
}
GUARD_TOL_GBP = 0.01


def appetite_policy(root: str = HERE) -> dict:
    """The ONE declared appetite (isa_policy.RISK_APPETITE). Unreadable -> UNDECLARED (never a default number)."""
    try:
        import isa_policy as _p
        a = dict(getattr(_p, "RISK_APPETITE"))
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "UNDECLARED", "why": "isa_policy.RISK_APPETITE unavailable: %s" % exc}
    v = a.get("max_drawdown_pct")
    if v is None or not isinstance(v, (int, float)) or not (0 < float(v) < 100):
        return dict(a, state="UNDECLARED", why="max_drawdown_pct is not a declared number in (0, 100)")
    return dict(a, state="DECLARED")


# ────────────────────────────────────────────────────────────────────────────── per-holding falls
def holding_falls(values: Dict[str, float], betas: Dict[str, dict], shock: float) -> Dict[str, dict]:
    """Per holding: uniform fall, beta fall, governing (worse of the two, capped at 100%)."""
    out = {}
    for k, v in values.items():
        b = (betas.get(k) or {}).get("beta_dimson")
        uni = shock
        bet = None if b is None else max(0.0, b) * shock
        gov = min(1.0, max(uni, bet if bet is not None else uni))
        out[k] = {"value_gbp": round(v, 2), "uniform": round(uni, 4), "beta": None if bet is None else round(min(bet, 1.0), 4),
                  "governing": round(gov, 4), "beta_basis": "MEASURED" if b is not None else "UNMEASURED->uniform"}
    return out


def severe_loss(values: Dict[str, float], betas: Dict[str, dict], nav: float, shock: float = SEVERE_SHOCK) -> dict:
    """The governing severe scenario on ONE book (pre- or post-trade). -> {loss_gbp, loss_pct_nav, per_holding}."""
    hf = holding_falls(values, betas, shock)
    loss = sum(h["value_gbp"] * h["governing"] for h in hf.values())
    return {"scenario": SEVERE_ID, "shock": shock, "loss_gbp": round(loss, 2),
            "loss_pct_nav": round(100.0 * loss / nav, 3) if nav else None, "per_holding": hf}


def _equity_scenarios(values, betas, nav):
    rows = []
    for s in EQUITY_SHOCKS:
        hf = holding_falls(values, betas, s)
        uni = sum(h["value_gbp"] * h["uniform"] for h in hf.values())
        bet_known = [h for h in hf.values() if h["beta"] is not None]
        bet = sum(h["value_gbp"] * h["beta"] for h in bet_known) + sum(h["value_gbp"] * s for h in hf.values()
                                                                        if h["beta"] is None)
        gov = sum(h["value_gbp"] * h["governing"] for h in hf.values())
        for kind, L, basis in (("uniform", uni, "BOUND: correlation 1, every equity pound falls %d%%" % (100 * s)),
                               ("beta", bet, "MEASURED betas to %s (unmeasured -> uniform)" % "the broad-equity proxy"),
                               ("governing", gov, "per-holding worse of uniform and beta, capped at 100%")):
            rows.append({"id": "BROAD_EQUITY_%d_%s" % (100 * s, kind.upper()), "shock_pct": 100 * s, "kind": kind,
                         "loss_gbp": round(L, 2), "loss_pct_nav": round(100 * L / nav, 2), "basis": basis})
    return rows


def episode_replays(values: Dict[str, float], nav: float, episode_returns: Dict[str, Dict[str, Optional[float]]],
                    betas: Dict[str, dict], proxy_key: str) -> List[dict]:
    """Replay each declared episode on the CURRENT book. A holding with its own history uses it (MEASURED); one
    without uses beta x the proxy's episode return (PROXY); with neither it takes the worst measured holding's return
    in that episode (BOUND). Coverage is published per episode."""
    rows = []
    for eid, (a, b, label) in EPISODES.items():
        prox = (episode_returns.get(proxy_key) or {}).get(eid)
        meas = [k for k in values if (episode_returns.get(k) or {}).get(eid) is not None]
        worst = min([(episode_returns[k][eid]) for k in meas], default=None)
        prox_basis = "PROXY"
        if prox is None and meas:
            # the broad-equity proxy predates its own history (VUAG lists 2019): use the value-weighted return of the
            # holdings that DO have history in the episode - the book's own market move - never one outlier name
            mv = sum(values[k] for k in meas)
            prox = sum(values[k] * episode_returns[k][eid] for k in meas) / mv if mv else None
            prox_basis = "PROXY_BOOK_AVERAGE"
        loss, cov, per = 0.0, {"MEASURED": 0.0, "PROXY": 0.0, "PROXY_BOOK_AVERAGE": 0.0, "BOUND": 0.0, "UNKNOWN": 0.0}, {}
        for k, v in values.items():
            r = (episode_returns.get(k) or {}).get(eid)
            basis = "MEASURED"
            if r is None:
                bta = (betas.get(k) or {}).get("beta_dimson")
                if prox is not None and bta is not None:
                    r, basis = max(-1.0, bta * prox), prox_basis
                elif worst is not None:
                    r, basis = worst, "BOUND"
                else:
                    basis = "UNKNOWN"
            per[k] = {"return": None if r is None else round(r, 4), "basis": basis}
            cov[basis] += v
            if r is not None:
                loss += -r * v                      # a gain in the episode offsets losses elsewhere (net book loss)
        rows.append({"id": "REPLAY_" + eid, "label": label, "window": [a, b], "loss_gbp": round(loss, 2),
                     "loss_pct_nav": round(100 * loss / nav, 2) if nav else None,
                     "coverage_gbp": {k: round(x, 2) for k, x in cov.items()},
                     "per_holding": per,
                     "basis": "current holdings replayed through the episode's own price path (net of any gains)"})
    return rows


# ────────────────────────────────────────────────────────────────────────────── goal effects
def _target():
    import json
    p = os.path.join(HERE, "target_state.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def required_return_pct(value_gbp: float, as_of: dt.date, which: str = "floor", schedule=None, ts=None) -> Optional[float]:
    """Required annual return from `value_gbp` at `as_of` to the target by its date, on the declared contribution
    schedule (derive_required_return.solve_required_annual_pct - the ONE solver). None if unreachable in the solver."""
    import derive_required_return as _d
    ts = ts or _target()
    tgt = float(ts["target_floor_gbp"] if which == "floor" else ts["target_stretch_gbp"])
    end = dt.date.fromisoformat(ts["target_date"])
    sch = ts.get("contribution_schedule") if schedule is None else schedule
    try:
        return _d.solve_required_annual_pct(tgt, float(value_gbp), as_of, end, sch, rounded=False)
    except Exception:                                                   # noqa: BLE001
        return None


def guardrail_pct(ts=None) -> Optional[float]:
    """The existing D1c upper guardrail ('>18 flags TARGET_ATTAINABILITY_REVIEW'), parsed from target_state - one home."""
    import re
    ts = ts or _target()
    m = re.search(r">\s*(\d+(?:\.\d+)?)\s*flags\s*TARGET_ATTAINABILITY_REVIEW", str(ts.get("guardrail_note") or ""))
    return float(m.group(1)) if m else None


def goal_effect(nav: float, loss_gbp: float, as_of: dt.date, ts=None) -> dict:
    ts = ts or _target()
    pre_f = required_return_pct(nav, as_of, "floor", ts=ts)
    post_f = required_return_pct(max(nav - loss_gbp, 1.0), as_of, "floor", ts=ts)
    pre_s = required_return_pct(nav, as_of, "stretch", ts=ts)
    post_s = required_return_pct(max(nav - loss_gbp, 1.0), as_of, "stretch", ts=ts)
    g = guardrail_pct(ts)
    return {"required_floor_pct_before": None if pre_f is None else round(pre_f, 2),
            "required_floor_pct_after": None if post_f is None else round(post_f, 2),
            "required_stretch_pct_before": None if pre_s is None else round(pre_s, 2),
            "required_stretch_pct_after": None if post_s is None else round(post_s, 2),
            "guardrail_pct": g,
            "attainability_review": (post_f is None) or (g is not None and post_f > g)}


def reverse_goal_loss(nav: float, as_of: dt.date, ts=None) -> dict:
    """The whole-ISA loss (GBP, % NAV) at which the floor's required return first exceeds the D1c guardrail."""
    ts = ts or _target()
    g = guardrail_pct(ts)
    if g is None:
        return {"state": "UNAVAILABLE", "why": "no D1c guardrail parsable from target_state.guardrail_note"}
    now = required_return_pct(nav, as_of, "floor", ts=ts)
    if now is None or now > g:
        return {"state": "ALREADY_ABOVE", "required_now_pct": now, "guardrail_pct": g}
    lo, hi = 0.0, nav * 0.999
    for _ in range(60):
        mid = (lo + hi) / 2
        r = required_return_pct(nav - mid, as_of, "floor", ts=ts)
        if r is None or r > g:
            hi = mid
        else:
            lo = mid
    return {"state": "OK", "loss_gbp": round(hi, 2), "loss_pct_nav": round(100 * hi / nav, 2), "guardrail_pct": g,
            "required_now_pct": round(now, 2)}


def reverse_appetite_shock(values, betas, nav, appetite_pct: float) -> dict:
    """The broad-equity fall at which the governing rule's book loss reaches the appetite."""
    def L(x):
        return 100.0 * sum(h["value_gbp"] * h["governing"] for h in holding_falls(values, betas, x).values()) / nav
    if L(1.0) < appetite_pct:
        return {"state": "NOT_REACHABLE", "why": "even a 100% equity fall leaves the loss below the appetite "
                                                 "(cash share too high)", "loss_at_100pct": round(L(1.0), 2)}
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if L(mid) >= appetite_pct:
            hi = mid
        else:
            lo = mid
    return {"state": "OK", "broad_equity_fall_pct": round(100 * hi, 2), "appetite_pct": appetite_pct}


# ────────────────────────────────────────────────────────────────────────────── guard (engine consumer)
def guard(pre_values: Dict[str, float], post_values: Dict[str, float], betas: Dict[str, dict], nav: float,
          appetite: Optional[dict] = None) -> dict:
    """Raj's 'review + guard new risk' for ONE plan: REVIEW_REQUIRED when the post-trade governing severe loss is at or
    above the appetite AND higher than before the trade. Never REFUSES, never sells (Raj 24-Oct-2026)."""
    ap = appetite if appetite is not None else appetite_policy()
    pre = severe_loss(pre_values, betas, nav)
    post = severe_loss(post_values, betas, nav)
    rec = {"pre_loss_pct": pre["loss_pct_nav"], "post_loss_pct": post["loss_pct_nav"],
           "pre_loss_gbp": pre["loss_gbp"], "post_loss_gbp": post["loss_gbp"],
           "delta_loss_gbp": round(post["loss_gbp"] - pre["loss_gbp"], 2), "scenario": SEVERE_ID}
    if ap.get("state") != "DECLARED":
        rec.update(state="UNDECLARED", why=ap.get("why"))
        return rec
    lim = float(ap["max_drawdown_pct"])
    rec["appetite_pct"] = lim
    worse = post["loss_gbp"] > pre["loss_gbp"] + GUARD_TOL_GBP
    breach = post["loss_pct_nav"] is not None and post["loss_pct_nav"] >= lim
    if not ap.get("enforce_guard", True):
        rec.update(state="NOT_ENFORCED", would_review=bool(worse and breach))
        return rec
    rec["state"] = "REVIEW_REQUIRED" if (worse and breach) else "PASS"
    rec["why"] = ("post-trade severe loss %.1f%% >= appetite %.0f%% and the plan adds GBP %.0f to it - Raj's explicit "
                  "approval is required (no automatic sale)" % (post["loss_pct_nav"], lim, rec["delta_loss_gbp"])
                  if rec["state"] == "REVIEW_REQUIRED" else
                  ("post-trade severe loss %.1f%% is below the %.0f%% appetite" % (post["loss_pct_nav"], lim)
                   if not breach else "the plan does not raise the severe-scenario loss"))
    return rec


# ────────────────────────────────────────────────────────────────────────────── run
def run(portfolio: dict, measure: dict, as_of: Optional[dt.date] = None, *, episode_returns=None,
        currency_exposure: Optional[dict] = None, factor: Optional[dict] = None, largest_issuer: Optional[dict] = None,
        binary_names: Iterable[str] = (), realised_drawdown: Optional[dict] = None, ts=None) -> dict:
    """All scenarios + reverse stress + appetite on the current book. `measure` = whole_book_risk.measure()."""
    _fi_mark("whole_book_stress", "run")
    import whole_book_risk as wbr
    as_of = as_of or dt.date.today()
    values, cash = wbr.book_values(portfolio)
    nav = float(measure.get("nav_gbp") or (sum(values.values()) + cash))
    betas = measure.get("betas_to_broad_equity") or {}
    ap = appetite_policy()
    out = {"schema": SCHEMA, "as_of": as_of.isoformat(), "nav_gbp": round(nav, 2), "cash_gbp": round(cash, 2),
           "measure_state": measure.get("state"), "appetite": {k: ap.get(k) for k in
                                                               ("state", "max_drawdown_pct", "basis", "response",
                                                                "declared_by", "declared_on", "why")},
           "doctrine": "scenario losses are deterministic illustrations on the current book - not probabilities, not "
                       "forecasts, not adopted thresholds (B08-R06); nothing here sells anything"}
    if measure.get("state") == "REFUSED":
        out.update(state="REFUSED", why="whole_book_risk REFUSED: %s" % measure.get("why"))
        return out
    sev = severe_loss(values, betas, nav)
    scen = _equity_scenarios(values, betas, nav)
    scen += episode_replays(values, nav, episode_returns or {}, betas, wbr.BROAD_EQUITY_PROXY)
    if factor and factor.get("lower_bound_pct") is not None:
        s, why = NAMED_SHOCKS["AI_COMPLEX"]
        lo = nav * float(factor["lower_bound_pct"]) / 100 * s
        hi = nav * float(factor.get("upper_bound_pct") or factor["lower_bound_pct"]) / 100 * s
        scen.append({"id": "AI_COMPLEX_40", "loss_gbp": round(lo, 2), "loss_gbp_upper": round(hi, 2),
                     "loss_pct_nav": round(100 * lo / nav, 2), "loss_pct_nav_upper": round(100 * hi / nav, 2),
                     "basis": "BOUNDED: look-through AI-complex %.1f%%..%.1f%% of NAV (factor_lookthrough) x -40%%; %s"
                              % (factor["lower_bound_pct"], factor.get("upper_bound_pct") or factor["lower_bound_pct"], why)})
    if currency_exposure and currency_exposure.get("non_gbp_gbp") is not None:
        s, why = NAMED_SHOCKS["GBP_UP"]
        L = float(currency_exposure["non_gbp_gbp"]) * (1 - 1 / (1 + s))
        scen.append({"id": "GBP_UP_15", "loss_gbp": round(L, 2), "loss_pct_nav": round(100 * L / nav, 2),
                     "basis": "%s non-GBP exposure GBP %.0f (%s) x (1 - 1/1.15); %s"
                              % (currency_exposure.get("verdict_type", "PROXY"), currency_exposure["non_gbp_gbp"],
                                 currency_exposure.get("basis"), why)})
    else:
        scen.append({"id": "GBP_UP_15", "state": "UNMEASURED", "why": "no currency exposure supplied"})
    trust = [f for f in (portfolio.get("funds") or []) if f.get("ticker") == "SMT"]
    if trust:
        s, why = NAMED_SHOCKS["TRUST_DISCOUNT"]
        L = float(trust[0]["value_gbp"]) * s
        scen.append({"id": "TRUST_DISCOUNT_15PP", "loss_gbp": round(L, 2), "loss_pct_nav": round(100 * L / nav, 2),
                     "basis": "SMT price falls 15%% with NAV unchanged; %s" % why})
    if largest_issuer and largest_issuer.get("gbp") is not None:
        L = float(largest_issuer["gbp"])
        scen.append({"id": "LARGEST_ISSUER_FAILS", "loss_gbp": round(L, 2), "loss_pct_nav": round(100 * L / nav, 2),
                     "basis": "%s (%s) goes to zero - %s" % (largest_issuer.get("name"), largest_issuer.get("source"),
                                                             "look-through incl. fund holdings")})
    bn = [k for k in binary_names if k in values]
    if bn:
        L = sum(values[k] for k in bn)
        scen.append({"id": "BINARY_POSITIONS_FAIL", "loss_gbp": round(L, 2), "loss_pct_nav": round(100 * L / nav, 2),
                     "basis": "every held VCI binary (%s) goes to zero" % ", ".join(bn)})
    scen.append({"id": "RATES_SHOCK", "state": "NOT_APPLICABLE",
                 "why": "no bond or cash-duration holding; the equity de-rating a rates shock causes is inside the equity "
                        "and AI-complex scenarios (no separate invented sensitivity)"})
    # funding interruption: contributions stop for good
    try:
        ts = ts or _target()
        f_now = required_return_pct(nav, as_of, "floor", ts=ts)
        f_none = required_return_pct(nav, as_of, "floor", schedule=[{"from": as_of.isoformat(), "monthly_gbp": 0}], ts=ts)
        scen.append({"id": "FUNDING_INTERRUPTION", "required_floor_pct_with_schedule": None if f_now is None else round(f_now, 2),
                     "required_floor_pct_no_contributions": None if f_none is None else round(f_none, 2),
                     "basis": "the declared contribution schedule (target_state.json) replaced by zero from today"})
    except Exception as exc:                                            # noqa: BLE001
        ts = None
        scen.append({"id": "FUNDING_INTERRUPTION", "state": "UNAVAILABLE", "why": str(exc)})
    for r in scen:
        if r.get("loss_gbp") is not None and ts is not None:
            try:
                r["goal"] = goal_effect(nav, float(r["loss_gbp"]), as_of, ts=ts)
            except Exception as exc:                                    # noqa: BLE001
                r["goal"] = {"state": "UNAVAILABLE", "why": str(exc)}
    out["scenarios"] = scen
    out["severe"] = {k: sev[k] for k in ("scenario", "shock", "loss_gbp", "loss_pct_nav")}
    out["severe"]["per_holding"] = sev["per_holding"]
    out["severe"]["rule"] = ("each holding takes the worse of a uniform %d%% fall and its Dimson beta to the broad-equity "
                             "proxy x %d%%, capped at 100%%; cash 0" % (100 * SEVERE_SHOCK, 100 * SEVERE_SHOCK))
    try:
        out["reverse_stress"] = {"goal": reverse_goal_loss(nav, as_of, ts=ts)}
    except Exception as exc:                                            # noqa: BLE001
        out["reverse_stress"] = {"goal": {"state": "UNAVAILABLE", "why": str(exc)}}
    # appetite verdicts (B08-R09)
    av = {"state": ap.get("state")}
    if ap.get("state") == "DECLARED":
        lim = float(ap["max_drawdown_pct"])
        out["reverse_stress"]["appetite"] = reverse_appetite_shock(values, betas, nav, lim)
        av.update(limit_pct=lim, severe_loss_pct=sev["loss_pct_nav"],
                  book_verdict="BOOK_EXCEEDS_APPETITE" if sev["loss_pct_nav"] >= lim else "WITHIN_APPETITE",
                  headroom_pp=round(lim - sev["loss_pct_nav"], 2))
        rd = realised_drawdown or {}
        cur = rd.get("current_drawdown_pct")
        av["realised"] = {"current_drawdown_pct": cur, "basis": rd.get("basis"), "as_of": rd.get("as_of"),
                          "verdict": ("UNMEASURED" if cur is None else
                                      ("REALISED_AT_OR_BEYOND_APPETITE" if -float(cur) >= lim else "WITHIN_APPETITE"))}
        items = []
        if av["book_verdict"] == "BOOK_EXCEEDS_APPETITE":
            items.append({"item": "BOOK_EXCEEDS_APPETITE", "gbp_at_stake": sev["loss_gbp"],
                          "deadline": "this review (Step 5)", "escalation": "Raj",
                          "why": "the governing severe scenario loses %.1f%% of the ISA against your %.0f%% limit; buys "
                                 "that raise it go to REVIEW; nothing is sold automatically" % (sev["loss_pct_nav"], lim)})
        if av["realised"]["verdict"] == "REALISED_AT_OR_BEYOND_APPETITE":
            items.append({"item": "REALISED_DRAWDOWN_AT_APPETITE", "gbp_at_stake": None, "deadline": "this review",
                          "escalation": "Raj", "why": "the flow-adjusted fall from peak has reached the limit"})
        av["review_items"] = items
    out["appetite_verdict"] = av
    out["state"] = "OK"
    out["line"] = render_line(out)
    return out


def render_line(s: dict) -> str:
    if s.get("state") != "OK":
        return "Whole-ISA stress: %s - %s" % (s.get("state"), s.get("why"))
    av = s.get("appetite_verdict") or {}
    sev = s["severe"]
    rev = (s.get("reverse_stress") or {})
    worst_rep = max((r for r in s["scenarios"] if str(r.get("id", "")).startswith("REPLAY_") and r.get("loss_gbp") is not None),
                    key=lambda r: r["loss_gbp"], default=None)
    parts = ["Whole-ISA stress: severe (world equity -50%%, worse of uniform/beta per holding) loses GBP %s = %.1f%%"
             % (format(round(sev["loss_gbp"]), ","), sev["loss_pct_nav"])]
    if av.get("state") == "DECLARED":
        parts.append("vs your %.0f%% limit: %s (%+.1fpp)" % (av["limit_pct"], av["book_verdict"], av["headroom_pp"]))
        ra = rev.get("appetite") or {}
        if ra.get("state") == "OK":
            parts.append("limit reached at a %.0f%% world-equity fall" % ra["broad_equity_fall_pct"])
    else:
        parts.append("appetite UNDECLARED")
    if worst_rep:
        parts.append("worst replay %s %.1f%%" % (worst_rep["label"], worst_rep["loss_pct_nav"]))
    rg = rev.get("goal") or {}
    if rg.get("state") == "OK":
        parts.append("a %.0f%% loss would push the GBP 1m floor's required return above %.0f%%"
                     % (rg["loss_pct_nav"], rg["guardrail_pct"]))
    return "; ".join(parts)


# ────────────────────────────────────────────────────────────────────────────── held-stock long history (CACHE)
# ⚑ The stock store holds 3 years (the risk window); the replay episodes go back to 2018 and scale_readiness needs
#   traded volume, so the HELD direct stocks (+ the VUAG ETF line) get one small long-history CACHE, declared in
#   artefact_contracts.json (CACHE, writer monthly_prerun): re-derivable, loss costs time not truth. One HTTP home
#   (stock_price_fetch._get / CHART); the FX direction rule (_assert_fx_direction) is reused, never re-written.
HISTORY_PATH = os.path.join(HERE, "held_stock_history.json")
HISTORY_RANGE = "10y"
HISTORY_MAX_AGE_DAYS = 6
_FX_FOR = {"USD": "GBPUSD=X", "EUR": "GBPEUR=X", "SEK": "GBPSEK=X", "CHF": "GBPCHF=X", "PLN": "GBPPLN=X",
           "JPY": "GBPJPY=X", "NOK": "GBPNOK=X", "DKK": "GBPDKK=X"}


def _chart(symbol, rng, get):
    import stock_price_fetch as _spf
    d = get(_spf.CHART.format(sym=symbol, rng=rng))
    res = (((d.get("chart") or {}).get("result") or [None])[0]) or {}
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    adj = (((ind.get("adjclose") or [{}])[0]) or {}).get("adjclose")
    q = ((ind.get("quote") or [{}])[0]) or {}
    close, vol = q.get("close") or [], q.get("volume") or []
    out = {}
    for i, t in enumerate(ts):
        c = close[i] if i < len(close) else None
        a = adj[i] if adj and i < len(adj) else None
        if c is None and a is None:
            continue
        out[dt.date.fromtimestamp(t).isoformat()] = {"close": c, "adj": a if a is not None else c,
                                                     "volume": vol[i] if i < len(vol) else None}
    return {"currency": ((res.get("meta") or {}).get("currency") or "").strip(), "daily": out,
            "total_return": bool(adj)}


def refresh_held_history(tickers, path: str = HISTORY_PATH, get=None, today: Optional[dt.date] = None) -> dict:
    """Refresh the cache for `tickers` older than HISTORY_MAX_AGE_DAYS. Never raises: a failed fetch keeps the old
    record and is NAMED (the replay then uses PROXY for that name). -> {state, refreshed, failed, path}."""
    import json
    import stock_price_fetch as _spf
    get = get or _spf._get
    today = today or dt.date.today()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:                                                   # noqa: BLE001
        doc = {"_what": "P-D held-stock long history (CACHE; whole_book_stress / scale_readiness)", "names": {}, "fx": {}}
    names, fx = doc.setdefault("names", {}), doc.setdefault("fx", {})
    refreshed, failed = [], {}
    for t in sorted(set(tickers)):
        rec = names.get(t) or {}
        age = None if not rec.get("fetched_on") else (today - dt.date.fromisoformat(rec["fetched_on"])).days
        if age is not None and age < HISTORY_MAX_AGE_DAYS:
            continue
        try:
            c = _chart(t, HISTORY_RANGE, get)
            cur = c["currency"]
            if cur in ("GBP", "GBp"):
                f_map = None
            elif cur in _FX_FOR:
                pair = _FX_FOR[cur]
                if pair not in fx or fx[pair].get("fetched_on") != today.isoformat():
                    fc = _chart(pair, HISTORY_RANGE, get)
                    lv = {d: r["close"] for d, r in fc["daily"].items() if r.get("close")}
                    for v in list(lv.values())[-5:]:
                        _spf._assert_fx_direction(pair, v)
                    fx[pair] = {"fetched_on": today.isoformat(), "daily": lv}
                f_map = fx[pair]["daily"]
            else:
                raise ValueError("currency %r has no declared FX route" % cur)
            fx_dates = sorted(f_map) if f_map else []
            import bisect
            for d, r in c["daily"].items():
                if cur == "GBP":
                    r["fx_to_gbp"] = 1.0
                elif cur == "GBp":
                    r["fx_to_gbp"] = 0.01
                else:
                    i = bisect.bisect_right(fx_dates, d) - 1
                    r["fx_to_gbp"] = (1.0 / f_map[fx_dates[i]]) if i >= 0 and f_map[fx_dates[i]] else None
            names[t] = {"currency": cur, "fetched_on": today.isoformat(), "total_return": c["total_return"],
                        "range": HISTORY_RANGE, "daily": c["daily"],
                        "source": "yahoo chart v8 (stock_price_fetch.CHART), FX %s" % (_FX_FOR.get(cur) or cur)}
            refreshed.append(t)
        except Exception as exc:                                        # noqa: BLE001
            failed[t] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)                       # atomic (ISA-0882 lesson: a cut-off write must not truncate)
    return {"state": "OK" if not failed else "PARTIAL", "refreshed": refreshed, "failed": failed, "path": path,
            "names": sorted(names)}


def load_held_history(path: str = HISTORY_PATH) -> dict:
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("names") or {}
    except Exception:                                                   # noqa: BLE001
        return {}


def _on_or_before(series: List[Tuple[dt.date, float]], d: dt.date, max_back: int = 7):
    best = None
    for x, c in series:
        if x <= d:
            best = (x, c)
        else:
            break
    return best if best and (d - best[0]).days <= max_back else None


def episode_returns(portfolio: dict, history: Optional[dict] = None, fund_series_fn=None, universe=None) -> dict:
    """{key: {episode_id: GBP total return or None}} for every held holding: funds from THE NAV route, stocks from
    the held-stock history (adjusted close x FX to GBP). None = no own history covering the episode."""
    import whole_book_risk as wbr
    if fund_series_fn is None or universe is None:
        import fund_performance as _fp
        universe = universe if universe is not None else _fp.load_universe()
        fund_series_fn = fund_series_fn or (lambda t, u: _fp.nav_series_for(t, u))
    history = load_held_history() if history is None else history
    out = {}

    def rets(series):
        r = {}
        for eid, (a, b, _l) in EPISODES.items():
            pa, pb = _on_or_before(series, dt.date.fromisoformat(a)), _on_or_before(series, dt.date.fromisoformat(b))
            r[eid] = (pb[1] / pa[1] - 1.0) if (pa and pb and pa[1]) else None
        return r
    for f in portfolio.get("funds") or []:
        t = f.get("ticker")
        try:
            s = fund_series_fn(t, (universe or {}).get(t)) or []
        except Exception:                                               # noqa: BLE001
            s = []
        out[wbr.FUND_PREFIX + t] = rets(sorted(s))
    for s_ in portfolio.get("stocks") or []:
        t = s_.get("ticker")
        rec = history.get(t) or {}
        ser = sorted((dt.date.fromisoformat(d), r["adj"] * r["fx_to_gbp"]) for d, r in (rec.get("daily") or {}).items()
                     if r.get("adj") and r.get("fx_to_gbp"))
        out[t] = rets(ser)
    return out


# ────────────────────────────────────────────────────────────────────────────── pre-run Step 6.10s (orchestration)
def prerun_step(summary: dict, portfolio: dict, stock_store: dict, month_label: str, root: str = HERE,
                as_of: Optional[dt.date] = None, fetch: bool = True, folder: Optional[str] = None) -> dict:
    """P-D Step 6.10s: whole-ISA risk -> stress/appetite -> flow-adjusted record -> scale study, ONE call. Each part
    fails into its own typed state (never silently absent, never a run blocker - R4.18 DEGRADE_ONLY: reporting and
    review inputs; the engine computes its own whole-book view). Writes four month-keyed RUN_OUTPUT artefacts and
    returns the summary blocks. `summary` is read for cash/exposure/factor/VCI inputs."""
    import json
    import whole_book_risk as wbr
    as_of = as_of or dt.date.today()
    res, arte = {}, {}
    hist_state = None
    tick = [s.get("ticker") for s in portfolio.get("stocks") or [] if s.get("ticker")] + ["VUAG.L"]
    if fetch:
        try:
            hist_state = refresh_held_history(tick, path=os.path.join(root, "held_stock_history.json"))
        except Exception as exc:                                        # noqa: BLE001
            hist_state = {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}
    hist = load_held_history(os.path.join(root, "held_stock_history.json"))
    try:
        m = wbr.measure(portfolio, stock_store, as_of, root=root)
    except Exception as exc:                                            # noqa: BLE001
        m = {"state": "REFUSED", "why": "%s: %s" % (type(exc).__name__, exc)}
    arte["whole_book_risk"] = m
    res["whole_book_risk"] = {k: m.get(k) for k in ("state", "why", "risk_window", "n_weeks", "sigma_book_ann",
                                                    "sigma_risky_ann", "gbp_vol_ann", "decomposition", "rho1_bound",
                                                    "coverage", "sensitivity", "static_book_path", "line")}
    try:
        import wealth_record as _wr
        cash = float(((portfolio.get("cash") or {}).get("value_gbp")) or 0.0)
        w = _wr.compute(folder=folder, as_of=as_of, nav_gbp=(portfolio.get("summary") or {}).get("total_value_gbp"),
                        cash_gbp=cash)
    except Exception as exc:                                            # noqa: BLE001
        w = {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc), "current_drawdown_pct": None}
    arte["wealth_record"] = w
    res["wealth_record"] = {k: w.get(k) for k in ("state", "why", "current_drawdown_pct", "max_drawdown_pct",
                                                  "max_drawdown_at", "last_covered_date", "money_view", "liquidity",
                                                  "goal", "line")}
    try:
        ex = summary.get("exposure_coverage") or {}
        cur = next((r for r in ex.get("rows") or [] if r.get("dimension") == "currency"), None)
        ccy = None
        if cur and cur.get("blocs_gbp"):
            ccy = {"non_gbp_gbp": round(sum(v for k, v in cur["blocs_gbp"].items() if k != "GBP"), 2),
                   "verdict_type": cur.get("verdict_type"), "basis": cur.get("basis")}
        iss = next((r for r in ex.get("rows") or [] if r.get("dimension") == "issuer"), None)
        nav = float((portfolio.get("summary") or {}).get("total_value_gbp") or 0.0)
        li = None
        if iss and iss.get("largest_issuer"):
            li = {"name": iss["largest_issuer"]["name"], "gbp": round(nav * float(iss["largest_issuer"]["pct"]) / 100, 2),
                  "source": "X-Ray top-10 look-through (exposure_coverage)"}
        fl = summary.get("factor_lookthrough") or {}
        er = episode_returns(portfolio, hist)
        bins = ((summary.get("vci_binary_risk") or {}).get("live_pending_binaries")) or []
        st = run(portfolio, m, as_of, episode_returns=er, currency_exposure=ccy, factor=fl, largest_issuer=li,
                 binary_names=bins, realised_drawdown={"current_drawdown_pct": w.get("current_drawdown_pct"),
                                                       "basis": "FLOW_ADJUSTED_PEAK (wealth_record)",
                                                       "as_of": w.get("last_covered_date")})
    except Exception as exc:                                            # noqa: BLE001
        st = {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}
    arte["whole_book_stress"] = st
    res["whole_book_stress"] = {k: st.get(k) for k in ("state", "why", "severe", "appetite", "appetite_verdict",
                                                       "reverse_stress", "line")}
    if isinstance(res["whole_book_stress"].get("severe"), dict):
        res["whole_book_stress"]["severe"] = {k: v for k, v in res["whole_book_stress"]["severe"].items()
                                              if k != "per_holding"}
    res["whole_book_stress"]["scenario_table"] = [{k: r.get(k) for k in ("id", "label", "loss_gbp", "loss_pct_nav",
                                                                         "loss_gbp_upper", "loss_pct_nav_upper", "state")}
                                                  for r in st.get("scenarios") or []]
    try:
        import scale_readiness as _sc
        with open(os.path.join(root, "target_weights.json"), encoding="utf-8") as fh:
            tw = json.load(fh)
        sc = _sc.assess(portfolio, {**hist, "VUAG": hist.get("VUAG.L")}, tw, as_of)
    except Exception as exc:                                            # noqa: BLE001
        sc = {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}
    arte["scale_readiness"] = sc
    res["scale_readiness"] = {k: sc.get(k) for k in ("state", "why", "line", "max_single_stock_position",
                                                     "participation_reference", "no_threshold")}
    res["held_stock_history"] = hist_state or {"state": "NOT_REFRESHED (fetch=False)"}
    for name, doc in arte.items():
        p = os.path.join(root, "%s_%s.json" % (name, month_label))
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, default=str)
        os.replace(tmp, p)
    items = ((st.get("appetite_verdict") or {}).get("review_items")) or []
    res["review_items"] = items
    res["lines"] = [x for x in (m.get("line"), st.get("line"), w.get("line"), sc.get("line")) if x]
    return res


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " - %s" % detail))
        if not cond:
            fails.append(name)
    betas = {"HI": {"beta_dimson": 2.4}, "LO": {"beta_dimson": 0.4}}
    hf = holding_falls({"HI": 100.0, "LO": 100.0, "NEW": 100.0}, betas, 0.5)
    ok("severe rule: a beta-2.4 name falls 100% (capped), not 120%", hf["HI"]["governing"] == 1.0, hf["HI"])
    ok("severe rule: a beta-0.4 name still falls the uniform 50% (correlations converge)", hf["LO"]["governing"] == 0.5)
    ok("severe rule: an unmeasured beta takes the uniform fall, never zero", hf["NEW"]["governing"] == 0.5)
    ap = {"state": "DECLARED", "max_drawdown_pct": 50, "enforce_guard": True}
    pre = {"LO": 50000.0}
    nav = 100000.0
    g1 = guard(pre, {"LO": 50000.0, "HI": 10000.0}, betas, nav, ap)
    ok("guard: 25% -> 35% severe loss stays below the 50% limit -> PASS", g1["state"] == "PASS", g1)
    pre2 = {"HI": 45000.0, "LO": 20000.0}
    g2 = guard(pre2, {"HI": 50000.0, "LO": 20000.0}, betas, nav, ap)
    ok("MUST-FIRE guard: a buy that deepens a severe loss at/above the limit -> REVIEW_REQUIRED (never REFUSED)",
       g2["state"] == "REVIEW_REQUIRED" and g2["delta_loss_gbp"] > 0, g2)
    g3 = guard(pre2, {"HI": 40000.0, "LO": 20000.0}, betas, nav, ap)
    ok("negative control: a SALE that reduces the severe loss is never sent for review", g3["state"] == "PASS", g3)
    g4 = guard(pre2, {"HI": 50000.0, "LO": 20000.0}, betas, nav, {"state": "UNDECLARED", "why": "x"})
    ok("negative control: an UNDECLARED appetite never sets REVIEW (not knowing never blocks)", g4["state"] == "UNDECLARED")
    g5 = guard(pre2, {"HI": 50000.0, "LO": 20000.0}, betas, nav, dict(ap, enforce_guard=False))
    ok("rollback: enforce_guard False -> NOT_ENFORCED with the would-review finding kept",
       g5["state"] == "NOT_ENFORCED" and g5["would_review"] is True)
    ra = reverse_appetite_shock({"HI": 45000.0, "LO": 20000.0}, betas, nav, 50.0)
    ok("reverse stress: the equity fall reaching the limit is solved (between 0 and 100%)",
       ra["state"] == "OK" and 0 < ra["broad_equity_fall_pct"] < 100, ra)
    er = {"A": {"COVID_2020": -0.30}, "FUND_VUAG": {"COVID_2020": -0.25}}
    rows = episode_replays({"A": 100.0, "B": 100.0}, 200.0, er, {"B": {"beta_dimson": 2.0}}, "FUND_VUAG")
    cov = next(r for r in rows if r["id"] == "REPLAY_COVID_2020")
    ok("replay: own history MEASURED, beta x proxy PROXY, coverage published",
       cov["per_holding"]["A"]["basis"] == "MEASURED" and cov["per_holding"]["B"]["basis"] == "PROXY"
       and abs(cov["loss_gbp"] - 80.0) < 1e-6, cov)
    if verbose:
        print("whole_book_stress selftest: %d FAIL" % len(fails))
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
