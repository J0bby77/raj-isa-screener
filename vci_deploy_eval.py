#!/usr/bin/env python3
"""
vci_deploy_eval.py — the single forward-led "evaluate a VCI candidate for deployment" call. v2.

Spec: VCI_Forward_Led_Framework_Implementation_Jul2026.md §4/§9.2/§11 (FWDVCI) +
      VCI_Framework_Enhancements_Implementation_Jul2026.md E1/E2/E5/E7 (v2; E4 is orchestrated by
      vci_risk_budget at Step 8/pre-run, using the p/L/size this verdict exposes).

Composes bottleneck_fv (win-case FV + P25 CI + derived p·L floor + eligibility) + vci_source_score
(deployability rank, quality-ex-ACS8 + revisions) + §9.2 sizing (liquidity-capped) into ONE verdict
the VCI run, the monthly pre-run, and Step 8 all consume — one definition, no desync.

Network-free (no yfinance).
"""
from __future__ import annotations
import bottleneck_fv as _bfv
import vci_source_score as _vss

try:   # ISA-0699 execution-ledger observation (never raises into the caller)
    from framework_integrity import _mark as _fi_mark
except Exception:                                            # noqa: BLE001
    def _fi_mark(*_a, **_k):                                 # noqa: D103
        return None

try:
    import scoring_config as cfg
except Exception:
    cfg = object()


SCORING_VENUE_CAPTURED = "CAPTURED_AT_SCORING"
SCORING_VENUE_UNKNOWN = "UNKNOWN_NOT_CAPTURED"


def scoring_venue(exchange=None, currency=None, source=None, captured_on=None) -> dict:
    """ISA-0588 — the venue of the listing that was SCORED, recorded at SCORING. The exchange is
    the Yahoo exchange CODE the scoring pull returned for the scored symbol (NMS, NYQ, OSL ...).
    Absent -> typed UNKNOWN_NOT_CAPTURED with exchange None: never defaulted, never inferred
    from how the ticker looks. stock_price_fetch._provenance reads this record."""
    code = (str(exchange).strip().upper() if exchange else "") or None
    if code in ("N/A", "NONE", "UNKNOWN"):
        code = None
    return {"exchange": code, "currency": (currency or None),
            "status": SCORING_VENUE_CAPTURED if code else SCORING_VENUE_UNKNOWN,
            "source": source, "captured_on": captured_on}


def _normalise_scoring_venue(sv) -> dict:
    if isinstance(sv, dict) and sv.get("status") in (SCORING_VENUE_CAPTURED, SCORING_VENUE_UNKNOWN):
        if sv.get("status") == SCORING_VENUE_CAPTURED and not sv.get("exchange"):
            return scoring_venue(None, sv.get("currency"), sv.get("source"), sv.get("captured_on"))
        return dict(sv)
    return scoring_venue(None)


# ── VCI-A (27-Sep-2026; ISA-0588 residual, ISA-0667, ISA-0771) — TYPED DECISION-GRADE STATES ─────
# BuildSpec ISA_BuildSpec_VCI_Input_Population_Integrity_27Sep2026.md §7/§8/§10. A verdict states
# WHAT was measured, not only the outcome: identity, ACS, source score and FV lineage each carry a
# state, and every refusal that is not an ordinary measured rejection is a NAMED code in `refusals`.
ID_VERIFIED, ID_AMBIGUOUS, ID_MISSING, ID_CONFLICTED = "VERIFIED", "AMBIGUOUS", "MISSING", "CONFLICTED"
R_IDENTITY = "UNRESOLVED_SECURITY_IDENTITY"
R_ACS = "UNMEASURED_ACS"
R_SCORE = "UNMEASURED_SOURCE_SCORE"
R_FV = "MISSING_STRUCTURED_FV_INPUT"


def security_identity(ticker, sv: dict, declared_exchange=None, declared_source=None,
                      venue_conflict=None) -> dict:
    """ONE home for the candidate's decision-grade identity (BuildSpec §7). VERIFIED only when the
    venue was CAPTURED AT SCORING from the scored listing. A venue merely DECLARED by a surface that
    is known to default (update_watchlist's literal 'NASDAQ', ISA-0582) is AMBIGUOUS - visible, usable
    for research, never price/risk/deploy-ready. Nothing is defaulted or inferred from the symbol."""
    sv = _normalise_scoring_venue(sv)
    dec = (str(declared_exchange).strip().upper() if declared_exchange else "") or None
    base = {"ticker": ticker, "exchange": sv.get("exchange"), "currency": sv.get("currency"),
            "declared_exchange": dec, "provenance": None, "as_of": sv.get("captured_on"), "state": None}
    if venue_conflict:
        return dict(base, state=ID_CONFLICTED, provenance="venue_conflict", conflict=venue_conflict)
    if sv.get("status") == SCORING_VENUE_CAPTURED:
        return dict(base, state=ID_VERIFIED, provenance="CAPTURED_AT_SCORING:%s" % (sv.get("source") or "scorer"))
    if dec:
        return dict(base, state=ID_AMBIGUOUS,
                    provenance="DECLARED_ONLY:%s (not captured at scoring; a declared venue may be a "
                               "default - ISA-0582)" % (declared_source or "watchlist exchange column"))
    return dict(base, state=ID_MISSING, provenance="none")


def _c(name, default):
    return getattr(cfg, name, default)


try:   # D-24 Stage 6 / V-1 — THE one shared null-handling helper
    from gated_control import gated as _gated, unknown_controls as _unknown_controls
except Exception:                                            # noqa: BLE001
    def _gated(v, *, control_name, registry=None):
        ok = v is not None and not isinstance(v, bool)
        if registry is not None:
            registry[control_name] = "OK" if ok else "UNKNOWN"
        return ("OK" if ok else "UNKNOWN"), (not ok), (float(v) if ok else None)

    def _unknown_controls(reg):
        return sorted(k for k, v in (reg or {}).items() if v == "UNKNOWN")


def size_for(acs, signal_count, days_to_catalyst, eligible, adv_usd=None, portfolio_value=None):
    """§9.2 pre-catalyst starter size, E5 liquidity-capped.

    Returns (size_pct, note, liq_capped, liq_status) — D-24 Stage 6 added the fourth element:
    "APPLIED" | "NOT_BINDING" | "UNKNOWN". UNKNOWN means the control could not be evaluated and
    must not be read as "not capped".
    """
    if not eligible or acs is None:
        return 0.0, "not eligible", False, "NOT_APPLICABLE"
    hi = _c("VCI_HIGH_ACS", 80)
    exc = _c("VCI_EXCEPTIONAL_SIZE_PCT", 1.5)
    full = _c("VCI_STARTER_SIZE_PCT", 1.0)
    mid = _c("VCI_STARTER_SIZE_PCT_MID", 0.75)
    near = (days_to_catalyst is not None and days_to_catalyst < 90)
    if acs >= 85 and (signal_count or 0) >= 3 and near:
        base, note = exc, "exceptional (ACS>=85 + signals>=3 + catalyst<90d) -> 1.5% cap"
    elif acs >= hi:
        base, note = full, f"ACS>={hi} 'high' -> full {full}% ahead of catalyst"
    elif acs >= 75:
        base, note = mid, f"ACS 75-{hi-1} -> {mid}% ahead, scale to {full}% on confirmation"
    else:
        return 0.0, "below ACS floor", False, "NOT_APPLICABLE"

    # E5 liquidity cap: position value <= VCI_MAX_PCT_ADV of ADV
    # ⚑ D-24 Stage 6 / V-1 (09-Aug-2026). The old test was `if adv_usd and portfolio_value:` —
    # so a NULL adv_usd returned liq_capped = False on 9 of 9 observed names, and "the liquidity
    # cap did not bind" became indistinguishable from "the liquidity cap was never evaluated".
    # Same failure class as p_thesis=None -> p=0 and a missing re-rate -> 0. The boolean is kept
    # (callers test it directly) and the third state is carried alongside it, where it BLOCKS.
    reg = {}
    _, _, adv = _gated(adv_usd, control_name="adv_usd", registry=reg)
    _, _, pv = _gated(portfolio_value, control_name="portfolio_value", registry=reg)
    liq_capped = False
    liq_status = "NOT_BINDING"
    if adv is not None and pv is not None and pv > 0:
        liq_cap_pct = float(_c("VCI_MAX_PCT_ADV", 0.10)) * adv / pv * 100.0
        if liq_cap_pct < base:
            base = round(liq_cap_pct, 3)
            note += f" | ADV-capped to {base}%"
            liq_capped = True
            liq_status = "APPLIED"
    else:
        liq_status = "UNKNOWN"
        note += " | ⚑ liquidity cap NOT EVALUATED (" + ", ".join(_unknown_controls(reg)) + ")"
    return base, note, liq_capped, liq_status


def evaluate_candidate(*, ticker, acs, price, fv_inputs=None, bottleneck_fv_per_share=None,
                       asset_structure=None, has_catalyst=False, days_to_catalyst=None,
                       signal_count=0, mgmt_unstable=False, falls_on_beat=False,
                       analyst_fv_per_share=None, weights=None,
                       # --- v2 ---
                       catalyst_type=None, catalyst_domain=None, p_thesis=None, L=None,
                       acs_ex_acs8=None, revision_velocity=None,
                       adv_usd=None, portfolio_value=None, fv_prev=None, price_prev=None,
                       floor_mode=None, scoring_venue=None,
                       # --- VCI-A (27-Sep-2026) ---
                       fv_input_id=None, declared_exchange=None, declared_exchange_source=None,
                       venue_conflict=None):
    """Full forward-led v2 verdict for one candidate. Supply EITHER fv_inputs (§10.2 dict, enables
    the P25 CI) OR a precomputed bottleneck_fv_per_share (scalar -> manual confirm under E2)."""
    _fi_mark("vci_deploy_eval", "evaluate_candidate")   # VCI-A capabilities' producer (ISA-0699)
    # ISA-0771: structured inputs count only with LINEAGE (a journal record, by id or exact content).
    # An unlineaged hand dict still yields a displayed CI, but it is not decision-grade: the name is
    # refused by name (MISSING_STRUCTURED_FV_INPUT), never silently left to a manual flag.
    try:
        import vci_fv_inputs as _fvi
        _lin = _fvi.resolve(ticker, fv_inputs=fv_inputs or None, fv_input_id=fv_input_id)
    except Exception as _e:                                            # noqa: BLE001
        _lin = {"state": R_FV, "fv_input_id": None, "fv_inputs": None,
                "why": "lineage store unavailable (%s: %s)" % (type(_e).__name__, _e)}
    if _lin["state"] == "PRESENT":
        fv_inputs = dict(_lin["fv_inputs"])
    has_structured = (fv_inputs is not None and _lin["state"] == "PRESENT")
    if fv_inputs and not has_structured:
        fv_inputs = {k: v for k, v in fv_inputs.items() if k != "asset_structure"} or None

    # 1) win-case FV (+ P25 CI when structured) and asymmetry
    if fv_inputs:
        fv = _bfv.compute_bottleneck_fv_ci(fv_inputs, price, asset_structure=asset_structure,
                                           analyst_fv_per_share=analyst_fv_per_share)
    else:
        fv = _bfv.BottleneckFV(bottleneck_fv_per_share=bottleneck_fv_per_share,
                               asset_structure=(asset_structure or "single_asset"),
                               floor=_bfv.select_floor(asset_structure),
                               fv_source=("modeled" if bottleneck_fv_per_share else "estimated"))
        if bottleneck_fv_per_share and price and price > 0:
            fv.fv_asymmetry = round(bottleneck_fv_per_share / price, 4)
            fv.fv_asymmetry_p50 = fv.fv_asymmetry
            fv.fv_asymmetry_p25 = fv.fv_asymmetry          # no CI without structured inputs

    # 2) E1 derived p·L floor (mode-gated; falls back to fixed tier)
    if p_thesis is None or L is None:
        _p, _L = _bfv.lookup_priors(asset_structure, catalyst_type)
        p_thesis = p_thesis if p_thesis is not None else _p
        L = L if L is not None else _L
    fv.floor, fv.floor_source = _bfv.derive_floor(asset_structure, catalyst_type, p_thesis, L,
                                                  days_to_catalyst, mode=floor_mode)

    if acs is not None:
        try:
            acs = float(acs)
        except (TypeError, ValueError):
            acs = None
    # 3) §4 eligibility (P25 asymmetry; structured mandate; cross-check) — network-free
    elig = _bfv.evaluate_deploy_eligibility(
        acs_total=acs, fv=fv, has_catalyst=has_catalyst, mgmt_unstable=mgmt_unstable,
        falls_on_beat=falls_on_beat, has_structured_inputs=has_structured)

    # 3b) E5 liquidity floor-gate: below min ADV -> not auto-eligible (manual)
    reasons = list(elig.reasons)
    deploy_eligible = elig.deploy_eligible
    require_manual = elig.require_manual_confirm
    min_adv = float(_c("VCI_MIN_ADV_USD", 0) or 0)
    if adv_usd is not None and min_adv and float(adv_usd) < min_adv:
        deploy_eligible = False
        require_manual = True
        reasons.append(f"adv {adv_usd:.0f} < min {min_adv:.0f}:illiquid_manual")

    # 3c) VCI-A typed states + named refusals (identity blocks price/risk/deploy readiness)
    refusals = []
    ident = security_identity(ticker, scoring_venue, declared_exchange, declared_exchange_source,
                              venue_conflict)
    if ident["state"] != ID_VERIFIED:
        refusals.append(R_IDENTITY)
        if deploy_eligible:
            deploy_eligible = False
        reasons.append("%s:%s" % (R_IDENTITY, ident["state"]))
    acs_state = "MEASURED" if acs is not None else "UNMEASURED"
    if acs is None:
        refusals.append(R_ACS)
    structured_fv_state = "PRESENT" if has_structured else R_FV
    if not has_structured:
        refusals.append(R_FV)

    # 4) deployability rank (advisory) — quality-ex-ACS8 + revisions; rank uses P50 asymmetry
    rank_asym = fv.fv_asymmetry_p50 if fv.fv_asymmetry_p50 is not None else fv.fv_asymmetry
    vss, _vss_c = _vss.compute_vci_source_score(
        fv_asymmetry=rank_asym, floor=fv.floor, acs=acs, acs_ex_acs8=acs_ex_acs8,
        days_to_catalyst=days_to_catalyst, signal_count=signal_count,
        revision_velocity=revision_velocity, weights=weights, return_components=True)
    vss_state = _vss_c.get("state") or {}
    if vss is None:
        refusals.append(R_SCORE)

    # ── ISA-0171 (V-2). A COUNTDOWN TO AN UNNAMED EVENT MAY NOT MOVE A SIZE. ────────────────
    # days_to_catalyst shrinks the position via size_for(); catalyst_type selects p_thesis and L
    # via lookup_priors(). Measured 16-Aug-2026: all 9 live names carry the days and none carries
    # the type, so one half of the pair was moving capital while the other silently defaulted.
    # R4.3 - a control fed a null BLOCKS. The days are refused, and the refusal is stated.
    try:
        import vci_integrity as _vi
        _coh = _vi.catalyst_coherence({"days_to_catalyst": days_to_catalyst,
                                       "catalyst_type": catalyst_type})
    except Exception as _e:                                          # noqa: BLE001
        _coh = {"state": f"CHECK_UNAVAILABLE: {type(_e).__name__}", "days_usable": days_to_catalyst,
                "blocks": False}
    _days_for_sizing = _coh.get("days_usable")
    if _coh.get("blocks"):
        reasons.append(f"catalyst_incoherent:{_coh['state']}")

    # 5) size (E5 liquidity-capped)
    size_pct, size_note, liq_capped, liq_status = size_for(
        acs, signal_count, _days_for_sizing, deploy_eligible,
        adv_usd=adv_usd, portfolio_value=portfolio_value)
    if liq_status == "UNKNOWN" and size_pct:
        # V-1: an unevaluable control never certifies a size. It escalates to a human instead.
        require_manual = True

    # 6) E7 compression cause (needs prior FV / price)
    cause = _bfv.compression_cause(fv.bottleneck_fv_per_share, fv_prev, price, price_prev)

    # 7) E4 inputs (risk budget consumes these downstream)
    expected_loss = round((size_pct or 0.0) * float(L if L is not None else 1.0)
                          * (1.0 - float(p_thesis if p_thesis is not None else 0.0)), 4)

    return {
        "ticker": ticker, "acs": acs, "acs_ex_acs8": acs_ex_acs8, "price": price,
        "asset_structure": fv.asset_structure, "fv_floor": fv.floor, "floor_source": fv.floor_source,
        "p_thesis": p_thesis, "L": L, "catalyst_type": catalyst_type, "catalyst_domain": catalyst_domain,
        "bottleneck_fv_per_share": fv.bottleneck_fv_per_share,
        "fv_p25": fv.fv_p25, "fv_p50": fv.fv_p50, "fv_p75": fv.fv_p75,
        "fv_asymmetry": fv.fv_asymmetry_p50 if fv.fv_asymmetry_p50 is not None else fv.fv_asymmetry,
        "fv_asymmetry_p25": fv.fv_asymmetry_p25, "fv_source": fv.fv_source,
        "fv_crosscheck_warn": fv.fv_crosscheck_warn,
        "deploy_eligible": deploy_eligible, "require_manual_confirm": require_manual,
        "eligibility_reasons": reasons,
        "vci_source_score": vss, "rank_mode": _c("VCI_RANK_MODE", "advisory"),
        "revision_velocity": revision_velocity,
        "size_pct": size_pct, "size_note": size_note, "size_liquidity_capped": liq_capped,
        "size_liquidity_status": liq_status,          # D-24 V-1: APPLIED|NOT_BINDING|UNKNOWN
        "adv_usd": adv_usd, "expected_loss_pct_isa": expected_loss,
        "asymmetry_compression_cause": cause,
        "days_to_catalyst": days_to_catalyst, "signal_count": signal_count,
        "catalyst_coherence": _coh, "days_to_catalyst_used_for_sizing": _days_for_sizing,
        # ISA-0588: carried verbatim from scoring; a re-price is not a re-scoring, so it never
        # re-derives or infers a venue. Absent -> typed UNKNOWN_NOT_CAPTURED.
        "scoring_venue": _normalise_scoring_venue(scoring_venue),
        # VCI-A typed states (27-Sep-2026)
        "security_identity": ident, "acs_state": acs_state,
        "vci_source_score_state": vss_state, "structured_fv_state": structured_fv_state,
        "fv_input_id": _lin.get("fv_input_id"), "fv_lineage_why": _lin.get("why"),
        "refusals": refusals,
    }


# ═══════════════════════════════════════════════════════════════════════════════════════
# ISA-0617 / ISA-0657 / ISA-0667 — THE REFUSALS EXIST; NOTHING EVER READ THEM
# ═══════════════════════════════════════════════════════════════════════════════════════
# `eligibility_reasons` has been produced on every row of every run since this module shipped
# and, measured on the delivered tree 12-Sep-2026, had ZERO production consumers — one test
# file and nothing else. R4.14: PRODUCED without CONSUMED is not live. So the September run
# published `vci_deploy_eligible: []` with no warning anywhere, and four names read as
# considered-and-declined when in truth they had never been scored.
#
# ⚑⚑ AND THE REASON STRINGS THEMSELVES CANNOT TELL THE TWO APART. The live rows carry
#    `acs: 0` — a placeholder from a VCI run that never computed one (acs_breakdown is "",
#    part_b_score is null) — and the refusal therefore reads **"acs 0.0 < 75 floor"**, which
#    is the sentence a genuinely bad name would produce. R2.10: "I could not measure it" and
#    "it is bad" must never produce the same output. A zero that fails a floor is the most
#    dangerous shape available, because it is CONSERVATIVE — nothing deploys, nothing breaks,
#    and nobody ever looks.
#
# This block adds one home (R4.4) for the distinction, consumed by the pre-run.

UNMEASURED, MEASURED_REJECT, ELIGIBLE = "UNMEASURED", "MEASURED_REJECT", "ELIGIBLE"

# control -> the entry fields that must be present for that control to have been EVALUATED.
_CONTROL_INPUTS = {
    "acs_quality_floor":   ("acs",),
    "fv_asymmetry_floor":  ("fv_asymmetry",),
    "catalyst_present":    ("catalyst_type",),
    "catalyst_domain":     ("catalyst_domain",),
    "expected_loss":       ("p_thesis", "L", "size_pct"),
}


def acs_is_scored(entry: dict) -> bool:
    """ISA-0667. Was the ACS actually COMPUTED, or is the number a placeholder?

    A VCI row that was never scored stores `acs_score: 0` with an empty `acs_breakdown` and a
    null `part_b_score`. Zero is a legitimate ACS in principle, so the test is not `acs != 0`
    — it is whether the score's own workings exist. R2.11: derive from primitives rather than
    accepting a single field's value at face value."""
    if entry.get("acs") is None and entry.get("acs_score") is None:
        return False
    breakdown = str(entry.get("acs_breakdown") or "").strip()
    if breakdown:
        return True
    if entry.get("part_b_score") is not None:
        return True
    # no workings and no Part B: a bare 0 is a placeholder, a bare non-zero is a real score
    return bool(entry.get("acs") or entry.get("acs_score"))


def classify_refusal(entry: dict) -> dict:
    """Split an ineligible row into UNMEASURED vs MEASURED_REJECT, naming the controls.

    UNMEASURED is a FRAMEWORK defect and must reach a human; MEASURED_REJECT is the system
    working. Publishing them as one list is what let four unscored names look declined."""
    if entry.get("deploy_eligible"):
        return {"state": ELIGIBLE, "ticker": entry.get("ticker"),
                "unmeasured": [], "measured_rejects": [], "reasons": []}
    unmeasured, measured = [], []
    scored = acs_is_scored(entry)
    floor = float(_c("VCI_ACS_FLOOR", 75))
    for control, fields in _CONTROL_INPUTS.items():
        if control == "acs_quality_floor":
            if not scored:
                unmeasured.append("acs_quality_floor(acs never computed)")
            elif float(entry.get("acs") or 0) < floor:
                measured.append("acs_quality_floor(%.1f < %.1f)" % (float(entry.get("acs") or 0), floor))
            continue
        missing = [f for f in fields if entry.get(f) is None]
        if missing:
            unmeasured.append("%s(missing:%s)" % (control, ",".join(missing)))
    reasons = list(entry.get("eligibility_reasons") or [])
    if not scored:
        # R2.13 — the corrected reading REPLACES the original in place, marked.
        reasons = [("acs UNSCORED (placeholder 0; no acs_breakdown, no part_b_score) — "
                    "SUPERSEDES the stored reason %r, which stated a measured rejection"
                    % r) if r.startswith("acs ") else r for r in reasons]
    named = [r for r in (entry.get("refusals") or []) if r in (R_IDENTITY, R_ACS, R_SCORE, R_FV)]
    return {"state": UNMEASURED if unmeasured else MEASURED_REJECT,
            "ticker": entry.get("ticker"),
            "unmeasured": unmeasured, "measured_rejects": measured, "reasons": reasons,
            "named_refusals": named}


def refusal_report(entries) -> dict:
    """The whole VCI set, classified. THIS is what the pre-run must put in summary.warnings.

    R4.9 — a reader that cannot match a row COUNTS it. `n_unmeasured` is published even when
    it is zero, so the absence of the figure and a figure of zero are distinguishable."""
    rows = [classify_refusal(e) for e in (entries or [])]
    unm = [r for r in rows if r["state"] == UNMEASURED]
    return {
        "n_rows": len(rows),
        "n_eligible": sum(1 for r in rows if r["state"] == ELIGIBLE),
        "n_unmeasured": len(unm),
        "n_measured_reject": sum(1 for r in rows if r["state"] == MEASURED_REJECT),
        "unmeasured_tickers": [r["ticker"] for r in unm],
        # VCI-A: named refusals by category (identity / ACS / score / FV lineage), published even
        # when empty so 'none' and 'not computed' differ (R4.9).
        "named_refusals": {code: sorted(r["ticker"] for r in rows if code in (r.get("named_refusals") or []))
                           for code in (R_IDENTITY, R_ACS, R_SCORE, R_FV)},
        "rows": rows,
        "warnings": [
            ("VCI %s: REFUSED, NOT REJECTED — the framework could not evaluate %s. "
             "An empty deploy-eligible list on this row is a measurement gap, not a verdict "
             "(R2.10/R4.3, ISA-0617/0657/0667)."
             % (r["ticker"], "; ".join(r["unmeasured"])))
            for r in unm
        ],
    }


def rank_eligible(entries):
    """(eligible_sorted_desc_by_vci_source_score, ineligible). §11 deployment order."""
    eligible = [e for e in entries if e.get("deploy_eligible")]
    ineligible = [e for e in entries if not e.get("deploy_eligible")]
    # ISA-0667: only a MEASURED score orders; an unrankable name keeps its place at the end with a
    # named refusal instead of silently sorting as 0.
    for e in eligible:
        if e.get("vci_source_score") is None and R_SCORE not in (e.get("refusals") or []):
            e.setdefault("refusals", []).append(R_SCORE)
    eligible.sort(key=lambda e: (e.get("vci_source_score") is not None, e.get("vci_source_score") or 0),
                  reverse=True)
    return eligible, ineligible


def refresh_at_live_price(entries, price_lookup, weights=None, portfolio_value=None):
    """Monthly pre-run recompute: re-price fv_asymmetry, re-derive floor, re-score + re-rank at the
    CURRENT price. Rolls fv_prev/price_prev in for E7. `price_lookup(ticker)->price`."""
    out = []
    for e in entries:
        px = price_lookup(e.get("ticker"))
        # ISA-0667: a placeholder ACS (no workings) is UNMEASURED, never a numeric 0 (one home:
        # acs_is_scored). A genuinely scored value passes through unchanged.
        _acs = e.get("acs") if e.get("acs") is not None else e.get("acs_score")
        _acs = _acs if acs_is_scored(e) else None
        v = evaluate_candidate(
            ticker=e.get("ticker"), acs=_acs, price=px,
            fv_input_id=e.get("fv_input_id"),
            declared_exchange=e.get("exchange"), declared_exchange_source="vci_watchlist.exchange",
            venue_conflict=e.get("venue_conflict"),
            fv_inputs=e.get("fv_inputs"),
            bottleneck_fv_per_share=(None if e.get("fv_inputs") else e.get("bottleneck_fv_per_share")),
            asset_structure=e.get("asset_structure"),
            has_catalyst=bool(e.get("has_catalyst", e.get("catalyst_type"))),
            days_to_catalyst=e.get("days_to_catalyst"), signal_count=e.get("signal_count", 0),
            mgmt_unstable=e.get("mgmt_unstable", False), falls_on_beat=e.get("falls_on_beat", False),
            analyst_fv_per_share=e.get("analyst_fv_per_share"),
            catalyst_type=e.get("catalyst_type"), catalyst_domain=e.get("catalyst_domain"),
            p_thesis=e.get("p_thesis"), L=e.get("L"),
            acs_ex_acs8=e.get("acs_ex_acs8"), revision_velocity=e.get("revision_velocity"),
            adv_usd=e.get("adv_usd"), portfolio_value=portfolio_value,
            fv_prev=e.get("bottleneck_fv_per_share_prev", e.get("fv_prev")),
            price_prev=e.get("price_prev"), weights=weights,
            scoring_venue=e.get("scoring_venue"))
        merged = dict(e); merged.update(v)
        if _acs is None:
            merged["acs_score"] = None          # ISA-0667: the placeholder 0 is not carried forward
        # roll snapshots for next run's E7 comparison
        merged["bottleneck_fv_per_share_prev"] = v.get("bottleneck_fv_per_share")
        merged["price_prev"] = px
        out.append(merged)
    elig, inelig = rank_eligible(out)
    return elig + inelig


def selftest_refusals(verbose: bool = True) -> int:
    """ISA-0617 / 0657 / 0667 liveness reference.

    liveness_ref: vci_deploy_eval.selftest_refusals
    Consumed by monthly_isa_prerun step 6.5 (summary.vci_refusals + summary.warnings)."""
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    unscored = {"ticker": "UNS", "acs": 0, "acs_score": 0, "acs_breakdown": "",
                "part_b_score": None, "deploy_eligible": False, "fv_asymmetry": None,
                "catalyst_type": None, "catalyst_domain": None, "p_thesis": 0.35, "L": 0.6,
                "size_pct": 0.0, "eligibility_reasons": ["acs 0.0 < 75 floor"]}
    r = classify_refusal(unscored)
    ok(r["state"] == UNMEASURED,
       "a row whose ACS was never computed must classify UNMEASURED, not MEASURED_REJECT — "
       "the live Sep-2026 rows carried acs 0 with an empty breakdown and read as declined")
    ok(any("never computed" in u for u in r["unmeasured"]), r["unmeasured"])
    ok(any("SUPERSEDES" in x for x in r["reasons"]),
       "R2.13: the corrected reading replaces the stored 'acs 0.0 < 75 floor' in place, marked")

    # ⚑ NEGATIVE CONTROL (R5.5): a genuinely measured, genuinely bad name must still be
    #   REJECTED, or the fix has simply stopped the framework ever rejecting anything.
    scored_bad = dict(unscored, ticker="BAD", acs=41, acs_score=41,
                      acs_breakdown="A1 8 A2 7 A3 6 ...", part_b_score=17,
                      fv_asymmetry=1.2, catalyst_type="phase2_biotech",
                      catalyst_domain="biotech_readout",
                      eligibility_reasons=["acs 41.0 < 75 floor"])
    r2 = classify_refusal(scored_bad)
    ok(r2["state"] == MEASURED_REJECT,
       "a scored name below the quality floor is a MEASURED_REJECT — the system working, not "
       "a warning (R5.8: test the test, or this check only ever says UNMEASURED)")
    ok(not r2["unmeasured"] and r2["measured_rejects"], r2)
    ok(not any("SUPERSEDES" in x for x in r2["reasons"]),
       "a real score's reason string is left alone")

    ok(acs_is_scored(scored_bad) and not acs_is_scored(unscored),
       "acs_is_scored discriminates on the WORKINGS (acs_breakdown / part_b_score), not on "
       "acs != 0 — zero is a legitimate score in principle (R2.11)")

    rep = refusal_report([unscored, scored_bad, dict(unscored, ticker="OK", deploy_eligible=True)])
    ok(rep["n_rows"] == 3 and rep["n_unmeasured"] == 1 and rep["n_measured_reject"] == 1
       and rep["n_eligible"] == 1, rep)
    ok(len(rep["warnings"]) == 1 and "UNS" in rep["warnings"][0],
       "exactly one warning, naming the unmeasured row — a measured rejection is not a warning")
    ok("n_unmeasured" in rep,
       "R4.9: the count is published even when zero, so an absent figure and a zero differ")

    if verbose:
        print("vci_deploy_eval.selftest_refusals: %d assertions, 0 failed" % n)
    return n


def _selftest(verbose: bool = True) -> int:
    """R5.5 — the module's named selftest. Delegates to selftest_refusals and adds the
    eligibility controls, so a refactor that empties either FAILS the census rather than
    passing with zero assertions (the vacuous pass of ISA-0348 / ISA-0513)."""
    n = selftest_refusals(verbose=False)

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    ABCL = dict(latent_tam_usd_bn=20.0, capture_share=0.12, steady_margin=0.35,
                exit_multiple=7.0, fully_diluted_shares=300e6, fx_to_local=1.0,
                asset_structure="platform")
    v = evaluate_candidate(ticker="ABCL", acs=78, acs_ex_acs8=74, price=8.11, fv_inputs=ABCL,
                           asset_structure="platform", has_catalyst=True, days_to_catalyst=80,
                           signal_count=6, catalyst_type="phase2_biotech",
                           catalyst_domain="biotech_readout", revision_velocity=0.6)
    ok(v["fv_asymmetry_p25"] < v["fv_asymmetry"],
       "the P25 asymmetry must be CONSERVATIVE relative to the P50 (E2)")
    ok(v["eligibility_reasons"] is not None,
       "every verdict carries its reasons — the field this build's consumer reads")

    # ⚑ NEGATIVE CONTROL (R5.5): a thin, illiquid name MUST NOT auto-deploy.
    v2 = evaluate_candidate(ticker="THIN", acs=82, price=10, bottleneck_fv_per_share=30,
                            asset_structure="platform", has_catalyst=True,
                            days_to_catalyst=120, signal_count=5, adv_usd=10_000,
                            portfolio_value=150_000)
    ok(v2["require_manual_confirm"],
       "⚑ NEGATIVE CONTROL: ADV 10k against a 150k book is below VCI_MIN_ADV_USD and MUST "
       "force manual confirmation — a liquidity control that never blocks is not a control")
    # ⚑ NEGATIVE CONTROL: a countdown to an UNNAMED catalyst must not move a size (ISA-0171).
    v3 = evaluate_candidate(ticker="NONAME", acs=80, price=10, bottleneck_fv_per_share=30,
                            asset_structure="platform", has_catalyst=True,
                            days_to_catalyst=90, signal_count=5, catalyst_type=None)
    ok(v3["catalyst_coherence"]["state"] == "INCOHERENT_DAYS_WITHOUT_TYPE",
       "⚑ NEGATIVE CONTROL: days_to_catalyst with catalyst_type None must be INCOHERENT — "
       "the days shrink the size while the type sets p and L, so using one and defaulting the "
       "other lets an unnamed event move capital (ISA-0171)")
    ok(v3["days_to_catalyst_used_for_sizing"] is None,
       "...and the days must be REFUSED as a sizing input, not merely flagged")
    # ⚑ ISA-0588 — venue is recorded at SCORING and typed when absent.
    ok(v3["scoring_venue"]["status"] == SCORING_VENUE_UNKNOWN and v3["scoring_venue"]["exchange"] is None,
       "⚑ MUST-FIRE: a verdict with no scoring venue carries typed UNKNOWN_NOT_CAPTURED, never a default")
    v4 = evaluate_candidate(ticker="QBTS", acs=76, price=16.8, bottleneck_fv_per_share=45.0,
                            asset_structure="platform", scoring_venue=scoring_venue("nms", "USD", "fixture"))
    ok(v4["scoring_venue"] == {"exchange": "NMS", "currency": "USD", "status": SCORING_VENUE_CAPTURED,
                               "source": "fixture", "captured_on": None},
       "POSITIVE CONTROL: a captured scoring venue is carried verbatim (code upper-cased)")
    ok(scoring_venue("N/A")["status"] == SCORING_VENUE_UNKNOWN,
       "NEGATIVE CONTROL: the screener's 'N/A' placeholder is not a venue")
    ok(_normalise_scoring_venue({"exchange": "", "status": SCORING_VENUE_CAPTURED})["status"]
       == SCORING_VENUE_UNKNOWN, "NEGATIVE CONTROL: CAPTURED with no code is re-typed UNKNOWN")
    # a re-price carries the scoring record forward and never erases a watchlist `exchange`
    _e = {"ticker": "QBTS", "acs": 76, "bottleneck_fv_per_share": 45.0, "asset_structure": "platform",
          "exchange": "NASDAQ", "scoring_venue": scoring_venue("NMS", "USD", "fixture")}
    _r = refresh_at_live_price([_e], price_lookup=lambda t: 17.0)[0]
    ok(_r["scoring_venue"]["exchange"] == "NMS" and _r["exchange"] == "NASDAQ",
       "a monthly re-price carries the scoring venue and leaves the watchlist exchange untouched")
    n += selftest_vci_a(verbose=False)
    if verbose:
        print("vci_deploy_eval._selftest: %d assertions, 0 failed" % n)
    return n


def selftest_vci_a(verbose: bool = True) -> int:
    """VCI-A (27-Sep-2026) must-fires + negative controls through evaluate_candidate / the monthly
    re-price (refresh_at_live_price) - the real consumers, not a unit call of a helper.
    liveness_ref: vci_deploy_eval.selftest_vci_a (ISA-0667 / ISA-0588 / ISA-0771)."""
    import tempfile, shutil, os as _os
    import vci_fv_inputs as _fvi
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)
    tmp = tempfile.mkdtemp(prefix="vcia_")
    old_here = _fvi.HERE
    try:
        _fvi.HERE = tmp
        Q = {"latent_tam_usd_bn": 60.0, "capture_share": 0.10, "steady_margin": 0.35, "exit_multiple": 8.0,
             "fully_diluted_shares": 372.6e6, "fx_to_local": 1.0}
        base = dict(ticker="QBTS", acs=76, price=16.8, asset_structure="platform", has_catalyst=True,
                    days_to_catalyst=60, signal_count=5, catalyst_type="revenue_scaling",
                    catalyst_domain="quantum", p_thesis=0.55, L=0.6, acs_ex_acs8=72,
                    adv_usd=279.5e6, portfolio_value=146000)
        sv = scoring_venue("NMS", "USD", "fixture")
        # §10: modelled FV with NO structured record -> named refusal, not a silent manual flag
        v0 = evaluate_candidate(bottleneck_fv_per_share=45.09, scoring_venue=sv, **base)
        ok(v0["structured_fv_state"] == R_FV and R_FV in v0["refusals"] and not v0["deploy_eligible"],
           "ISA-0771 MUST-FIRE: modelled FV without a structured record -> MISSING_STRUCTURED_FV_INPUT: %s" % v0["refusals"])
        # an unlineaged hand dict is also refused (it is exactly what the Aug run left in a memory note)
        v1 = evaluate_candidate(fv_inputs=dict(Q), scoring_venue=sv, **base)
        ok(v1["structured_fv_state"] == R_FV and v1["fv_input_id"] is None,
           "ISA-0771: an unlineaged hand dict is not decision-grade")
        # adding ONE valid record makes the same candidate evaluable, no code change
        rec = _fvi.record("QBTS", Q, valuation_as_of="2026-09-13", author="fixture")
        v2 = evaluate_candidate(fv_inputs=dict(Q), scoring_venue=sv, **base)
        ok(v2["structured_fv_state"] == "PRESENT" and v2["fv_input_id"] == rec["fv_input_id"]
           and R_FV not in v2["refusals"], "ISA-0771: a recorded input is resolved by content and referenced")
        ok(v2["deploy_eligible"] and v2["security_identity"]["state"] == ID_VERIFIED,
           "POSITIVE CONTROL: verified identity + lineaged FV + ACS 76 + P25 >= floor deploys: %s"
           % v2["eligibility_reasons"])
        v2b = evaluate_candidate(fv_input_id=rec["fv_input_id"], scoring_venue=sv, **base)
        ok(v2b["fv_input_id"] == rec["fv_input_id"] and v2b["deploy_eligible"], "an explicit id is authoritative")
        # §7 identity: a DECLARED venue alone (the defaulted 'NASDAQ' shape) is not verified
        v3 = evaluate_candidate(fv_input_id=rec["fv_input_id"], declared_exchange="NASDAQ", **base)
        ok(v3["security_identity"]["state"] == ID_AMBIGUOUS and not v3["deploy_eligible"]
           and R_IDENTITY in v3["refusals"],
           "ISA-0588 NEGATIVE CONTROL: a defaulted/declared-only venue must NOT verify identity")
        v4 = evaluate_candidate(fv_input_id=rec["fv_input_id"], **base)
        ok(v4["security_identity"]["state"] == ID_MISSING and R_IDENTITY in v4["refusals"],
           "ISA-0588 MUST-FIRE: no venue -> MISSING, refused by name, never defaulted")
        v5 = evaluate_candidate(fv_input_id=rec["fv_input_id"], scoring_venue=sv,
                                venue_conflict={"scoring_venue": "NMS", "other": "NYQ"}, **base)
        ok(v5["security_identity"]["state"] == ID_CONFLICTED and not v5["deploy_eligible"],
           "ISA-0588: a venue conflict is typed CONFLICTED and refuses")
        # §8 typed scoring: unmeasured ACS
        v6 = evaluate_candidate(**dict(base, acs=None, acs_ex_acs8=None), fv_input_id=rec["fv_input_id"], scoring_venue=sv)
        ok(v6["acs_state"] == "UNMEASURED" and v6["vci_source_score"] is None and R_ACS in v6["refusals"]
           and R_SCORE in v6["refusals"] and "acs UNMEASURED:refused_not_rejected" in v6["eligibility_reasons"],
           "ISA-0667 MUST-FIRE: unmeasured ACS -> no numeric ACS, no numeric source score, named: %s" % v6["refusals"])
        v7 = evaluate_candidate(**dict(base, acs=41, acs_ex_acs8=37), fv_input_id=rec["fv_input_id"], scoring_venue=sv)
        ok(v7["acs_state"] == "MEASURED" and v7["vci_source_score"] is not None
           and any(r.startswith("acs 41.0 <") for r in v7["eligibility_reasons"]),
           "ISA-0667 NEGATIVE CONTROL: a genuinely low measured ACS scores and is an ordinary rejection")
        # the monthly re-price: the live Sep row shape (placeholder acs 0) no longer becomes 0 / 7.5
        row = {"ticker": "INFQ", "acs": 0, "acs_score": 0, "acs_breakdown": "", "part_b_score": None,
               "fv_inputs": {}, "bottleneck_fv_per_share": None, "exchange": "NYSE", "scoring_venue": None,
               "vci_source_score": 7.5}
        rr = refresh_at_live_price([row], price_lookup=lambda t: 20.0)[0]
        ok(rr["acs"] is None and rr["acs_score"] is None and rr["vci_source_score"] is None
           and rr["acs_state"] == "UNMEASURED",
           "ISA-0667 MUST-FIRE through the pre-run consumer: placeholder 0 -> UNMEASURED, 7.5 -> None: %s"
           % {k: rr.get(k) for k in ("acs", "acs_score", "vci_source_score")})
        rep = refusal_report([rr, v2])
        ok(rep["named_refusals"][R_ACS] == ["INFQ"] and rep["named_refusals"][R_IDENTITY] == ["INFQ"]
           and "INFQ" in rep["unmeasured_tickers"], "named refusals reach the pre-run report by category")
        # write-boundary contract
        ok(vci_watchlist_contract_breaches([rr, v2]) == [], "C0 typed rows satisfy the write-boundary contract")
        bad = dict(rr, acs=0, vci_source_score=7.5)
        ok(len(vci_watchlist_contract_breaches([bad])) == 2,
           "C1 NEGATIVE CONTROL: a numeric ACS with no workings and a numeric score with unmeasured "
           "inputs both FIRE at the write boundary")
        tie = [dict(v2), dict(v2, ticker="QBTS2")]
        ok(vci_watchlist_contract_breaches(tie) == [], "C2 identical inputs may tie (ties are legal)")
        tie2 = [dict(v2), dict(v7, vci_source_score=v2["vci_source_score"])]
        ok(any("identical" in b for b in vci_watchlist_contract_breaches(tie2)),
           "C3 MUST-FIRE: identical non-null scores from DIFFERENT inputs fire (the 7.5 shape)")
    finally:
        _fvi.HERE = old_here
        shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print("vci_deploy_eval.selftest_vci_a: %d assertions, 0 failed" % n)
    return n


_SCORE_INPUT_KEYS = ("fv_asymmetry", "fv_floor", "acs", "acs_ex_acs8", "days_to_catalyst", "signal_count",
                     "revision_velocity")


def vci_watchlist_contract_breaches(rows) -> list:
    """ISA-0667 R5.1 WRITE-BOUNDARY CONTRACT for vci_watchlist rows. [] when clean.
    (1) no numeric ACS without its workings (acs_is_scored); (2) no numeric source score while a
    mandatory input is unmeasured; (3) no two rows with the same non-null score unless their score
    inputs are identical (the constant-7.5 shape)."""
    out = []
    for r in rows or []:
        t = r.get("ticker")
        if r.get("acs") is not None and not acs_is_scored(r):
            out.append("%s: numeric acs %r with no workings (placeholder presented as measured)" % (t, r.get("acs")))
        if r.get("vci_source_score") is not None:
            q = r.get("acs_ex_acs8") if r.get("acs_ex_acs8") is not None else r.get("acs")
            st = _vss.source_score_state(r.get("fv_asymmetry"), r.get("fv_floor"), q)
            if st["state"] != _vss.SCORE_MEASURED:
                out.append("%s: numeric vci_source_score %r with unmeasured %s" % (t, r.get("vci_source_score"), st["missing"]))
    seen = {}
    for r in rows or []:
        sc = r.get("vci_source_score")
        if sc is None:
            continue
        key = tuple(r.get(k) for k in _SCORE_INPUT_KEYS)
        if sc in seen and seen[sc][1] != key:
            out.append("%s and %s carry an identical source score %r from different inputs" % (seen[sc][0], r.get("ticker"), sc))
        seen.setdefault(sc, (r.get("ticker"), key))
    return out


if __name__ == "__main__":
    ABCL = dict(latent_tam_usd_bn=20.0, capture_share=0.12, steady_margin=0.35,
                exit_multiple=7.0, fully_diluted_shares=300e6, fx_to_local=1.0, asset_structure="platform")
    v = evaluate_candidate(ticker="ABCL", acs=78, acs_ex_acs8=74, price=8.11, fv_inputs=ABCL,
                           asset_structure="platform", has_catalyst=True, days_to_catalyst=80,
                           signal_count=6, catalyst_type="phase2_biotech", catalyst_domain="biotech_readout",
                           revision_velocity=0.6)
    print("ABCL:", "asymP50", v["fv_asymmetry"], "asymP25", v["fv_asymmetry_p25"],
          "floor", v["fv_floor"], v["floor_source"], "elig", v["deploy_eligible"],
          "src", v["vci_source_score"], "size", v["size_pct"], "EL", v["expected_loss_pct_isa"])
    assert v["fv_asymmetry_p25"] < v["fv_asymmetry"]         # P25 conservative
    # E5 liquidity cap: ADV $10k vs £150k book -> 10% of ADV = $1k ~ 0.67% cap; also < min ADV -> manual
    v2 = evaluate_candidate(ticker="THIN", acs=82, price=10, bottleneck_fv_per_share=30,
                            asset_structure="platform", has_catalyst=True, days_to_catalyst=120,
                            signal_count=5, adv_usd=10_000, portfolio_value=150_000)
    print("THIN:", "elig", v2["deploy_eligible"], "size", v2["size_pct"], "capped",
          v2["size_liquidity_capped"], "manual", v2["require_manual_confirm"])
    assert v2["require_manual_confirm"]   # ADV 10k < VCI_MIN_ADV_USD 1e6 -> illiquid manual
    print("vci_deploy_eval v2 self-test PASSED")
    selftest_refusals()
