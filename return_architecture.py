#!/usr/bin/env python3
"""
return_architecture.py — ranked build item #1 (06-Aug-2026). Pre-run Step 6.08.

⚑ THE GAP THIS CLOSES, IN ONE SENTENCE
Section C is the single number that answers "is this portfolio on track for £1m", and it has
NEVER been computed. `analytics_data_aug_2026.json` carries `section_c.total_return: null,
status: "pending_section_a"`, and `email_prefill` renders it as
`"[Claude fills after Section A complete]"`. The most consequential arithmetic in the review was
a hand-typed sentence.

⚑ AND SECTION A WAS COMPUTED FROM THE INPUT THE FRAMEWORK ALREADY KNOWS IS INVERTED
`section_a.fund_rows[].est_return_pct` on the live August run holds values stamped
`"jul2026_run: ... Est. return 11.0% (S&P earnings growth ~11% + 1.2% yield - 0.07% OCF ...)"` —
prose I typed LAST MONTH, carried forward, and multiplied by portfolio weights. It scores
Scottish Mortgage 14.0%, the highest in bucket B3, on a realised 5-year return of 0.22%. That is
register C4, still live in the one place C4 was never applied. **This module is where
`est_return` stops driving a decision.**

WHAT THIS EMITS
    expected_return_inputs[]   ONE declared input per asset — basis, source, as_of, confidence
    thresholds                 every gate DERIVED from the A19 anchor, with legacy parity checked
    section_a / section_b / section_c   mechanical, with two independent derivations asserted
    shortfall_attribution[]    w_i x (anchor - er_i) per holding — sums EXACTLY to the shortfall
    levers[]                   what each available action is worth, in pp, with feasibility
    basis_study                every basis computed every run, so the choice stays Raj's

⚑ WHAT IT DOES NOT DO. It does not trade, size, rebalance or escalate. It states the gap and
prices the levers. Step 8 decides.

────────────────────────────────────────────────────────────────────────────────────────────
⚑⚑ THE DESIGN DECISION THAT MATTERS, STATED BEFORE THE CODE

There are two DIFFERENT expected-return questions in this framework and conflating them is how
Section C came to be meaningless:

  1. OWNERSHIP  — "does this holding still earn its place against its peers?"
     The right evidence is REALISED: the golden source's trailing windows. This is what the FRS
     and the anchor rule already use, correctly, and nothing here changes it.

  2. STRUCTURE  — "is this ALLOCATION capable of reaching £1.0m by 2037?"
     Realised return is the WRONG input for this. The fund sleeve's realised median across
     windows is ~20% p.a., which would declare the portfolio comfortably on track — but that
     number is a 2021-26 bull run annualised, and a 12-year compounding projection built on it
     is a forecast that the last five years repeat. It would be an inverted signal in the
     opposite direction to `est_return`: wrong in the reassuring direction, which is worse.

So the OPERATIVE basis for Section A/B/C is the **declared long-run expectation per holding**,
which already exists and has never been read: `target_weights.funds[].min_expected_return`
(B1 9% / B2 12% / B3 13%, with per-fund overrides). It is dated, sourced, policy-owned, and it
answers the structural question. Realised and shrunk bases are computed EVERY RUN and published
beside it (`basis_study`) so the choice is visible and reversible — the same discipline
`return_adequacy_basis_study` uses for median-vs-minimum, and the same decision shape Raj
resolved as D-A.

⚑ H5 APPLIES TO THE SHRINK CONSTANTS AND IS STATED, NOT BURIED. `ER_SHRINK_WEIGHT` and
`ER_EXCESS_CAP_PP` are uncalibrated and not backtestable on the history available. They are used
by the `shrunk` basis ONLY, which is not operative. Nothing that decides capital depends on them
today.
────────────────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1

# ── BASIS SELECTION ─────────────────────────────────────────────────────────────────────
# One home. Reversible in one line. Every basis is computed every run regardless.
ER_BASIS_OPERATIVE = "declared_prior"        # "declared_prior" | "realised" | "shrunk"
ER_BASES = ("declared_prior", "realised", "shrunk")

# ⚑ UNCALIBRATED (register H5). Consumed by the `shrunk` basis ONLY, which is not operative.
ER_SHRINK_WEIGHT = 0.35      # weight on realised evidence; 1-w on the declared prior
ER_EXCESS_CAP_PP = 6.0       # a holding may never be credited more than prior + this

# ── THRESHOLD OFFSETS — every gate is anchor-derived (A19 invariant 6) ──────────────────
# These offsets are chosen so that at TODAY's anchor (13.9) each derived threshold reproduces
# the legacy hardcoded constant EXACTLY. That is deliberate: the mechanism ships, the policy
# does not move. `threshold_divergences[]` reports any pair that stops agreeing.
SECTION_C_WATCH_OFFSET_PP  = -0.9    # anchor - 0.9 = 13.0  (legacy total_isa_watch_low)
STOCK_SLEEVE_PREMIUM_PP    = 4.1     # anchor + 4.1 = 18.0  (legacy stock_sleeve_on_track)
STOCK_SLEEVE_WATCH_OFFSET_PP = 1.1   # anchor + 1.1 = 15.0  (legacy stock_sleeve_watch_low)
THRESHOLD_PARITY_TOL_PP    = 0.15    # beyond this a derived/legacy pair is a DIVERGENCE

# ── COVERAGE ────────────────────────────────────────────────────────────────────────────
# ⚑ "missing" cannot be a number. Below this, no verdict is issued at all.
COVERAGE_FLOOR = 0.90

# ── BUCKET-MINIMUM PROVENANCE — a live divergence, preserved not resolved ───────────────
# `fund_action_stack._bucket_minimums()` documents itself as "One home: read from
# target_weights.json when present so a threshold cannot say one thing here and another there."
# It reads keys `min_return` / `return_minimum`. **The key in target_weights.json is
# `min_expected_return`.** The read has therefore NEVER matched and the module has always used
# its own DEFAULT_BUCKET_MIN — so the B1 ownership floor is 12.0% in the fund action stack and
# 9.0% in the policy file, in the same report, for the same fund.
#
# Fixing the read would LOOSEN the B1 floor by 3pp as a side effect of a bug fix. That is
# exactly the mistake D-C(ii) records (a measurement fix shipping an undecided policy change),
# so it is NOT done here. The divergence is measured, published every run, and left for Raj.
BUCKET_MIN_POLICY = "code_default_pending_decision"   # "code_default_pending_decision" | "target_weights"

CASH_INPUTS_PATH = os.path.join(HERE, "return_inputs.json")


def _round(v, n=4):
    return None if v is None else round(float(v), n)


# ─────────────────────────────────────────────────────────────── anchor + thresholds
def anchor_state():
    """THE anchor (A19). target_state.json is the authority; scoring_config is the loader."""
    try:
        import scoring_config as _c
        ts = getattr(_c, "TARGET_STATE", {}) or {}
        return {
            "operative_pct": float(_c.REQUIRED_RETURN_MID),
            "floor_pct": ts.get("required_return_floor_pct"),
            "stretch_pct": ts.get("required_return_stretch_pct"),
            "derived_at": ts.get("derived_at"),
            "guardrail_state": ts.get("guardrail_state"),
            "source": "target_state.json via scoring_config (A19)",
        }
    except Exception as e:                                     # noqa: BLE001
        raise RuntimeError(f"anchor unreadable — refusing to invent one: {e}") from e


def _legacy_thresholds(tw):
    t = (tw or {}).get("thresholds", {}) or {}
    return {
        "section_c_on_track": _pc(t.get("total_isa_on_track")),
        "section_c_watch": _pc(t.get("total_isa_watch_low")),
        "section_b_on_track": _pc(t.get("stock_sleeve_on_track")),
        "section_b_watch": _pc(t.get("stock_sleeve_watch_low")),
        "section_a_min": _pc(t.get("fund_sleeve_weighted_avg_min")),
    }


def _pc(v):
    return None if v is None else round(float(v) * 100.0, 4)


def thresholds(anchor_pct, tw=None):
    """Every return gate, DERIVED from the anchor, with legacy parity asserted not assumed."""
    try:
        import scoring_config as _c
        fund_bands = dict(getattr(_c, "FUND_GATE_BANDS", {}) or {})
    except Exception:                                          # noqa: BLE001
        fund_bands = {}
    a = float(anchor_pct)
    derived = {
        "section_a_pass": fund_bands.get("pass"),
        "section_a_inconclusive": fund_bands.get("inconclusive"),
        "section_b_on_track": round(a + STOCK_SLEEVE_PREMIUM_PP, 1),
        "section_b_watch": round(a + STOCK_SLEEVE_WATCH_OFFSET_PP, 1),
        "section_c_on_track": round(a, 1),
        "section_c_watch": round(a + SECTION_C_WATCH_OFFSET_PP, 1),
    }
    legacy = _legacy_thresholds(tw)
    divergences = []
    # ⚑ `target_weights.thresholds.fund_sleeve_weighted_avg_min` (0.12) is NOT a stale twin of a
    # derived gate — it is a SINGLE threshold that the A11/D8 two-band scheme (PASS / INCONCLUSIVE
    # / FAIL) superseded outright. Comparing it to either band would manufacture a divergence out
    # of a structural change. It is reported as superseded so nobody reads it as authoritative,
    # and it is excluded from the parity invariant, which exists to catch DRIFT.
    superseded = [{
        "constant": "target_weights.thresholds.fund_sleeve_weighted_avg_min",
        "value_pct": legacy.get("section_a_min"),
        "superseded_by": "scoring_config.FUND_GATE_BANDS (A11/D8, anchor-derived)",
        "current_bands": {"pass": derived.get("section_a_pass"),
                          "inconclusive": derived.get("section_a_inconclusive")},
        "note": ("a single pass/fail line replaced by two bands. The constant still sits in the "
                 "policy file looking authoritative and governs nothing — delete it or mark it, "
                 "but do not let a future reader gate on it."),
    }] if legacy.get("section_a_min") is not None else []
    for dk, lk in (("section_b_on_track", "section_b_on_track"),
                   ("section_b_watch", "section_b_watch"),
                   ("section_c_on_track", "section_c_on_track"),
                   ("section_c_watch", "section_c_watch")):
        d, l = derived.get(dk), legacy.get(lk)
        if d is None or l is None:
            continue
        if abs(d - l) > THRESHOLD_PARITY_TOL_PP:
            divergences.append({
                "threshold": dk, "derived_pct": d, "legacy_pct": l,
                "delta_pp": round(d - l, 2),
                "operative": "derived",
                "note": ("the anchor has moved away from the frozen constant in "
                         "target_weights.json — the DERIVED value is operative (A19 invariant 6) "
                         "and the legacy constant is now stale prose"),
            })
    return {"derived": derived, "legacy_frozen": legacy, "divergences": divergences,
            "superseded_constants": superseded,
            "basis": ("all gates = f(target_state.required_return_operative_pct). Offsets chosen "
                      "so that at the 12-Jul-2026 anchor of 13.9 each derived gate reproduces the "
                      "legacy constant exactly; divergence is reported, never silent.")}


# ────────────────────────────────────────────────────────────── declared cash input
DEFAULT_CASH_INPUTS = {
    "cash_expected_return_pct": None,
    "as_of": None,
    "source": None,
    "_note": ("⚑ NOT a code constant. Cash is currently ~7% of the ISA and Section C has always "
              "credited it 0% implicitly, which is a claim about the money-market rate, made by "
              "omission, that nobody checked. Declare the rate with a source and a date, or the "
              "cash weight is reported as UNMEASURED and excluded from coverage."),
}


def derive_cash_rate(cash_statement=None):
    """⚑ The money-market rate, from the ONE primary source that exists: the interest AJ Bell
    actually credited, over the balances Raj actually held.

    A published headline rate is a rate on a product. This is the rate on THIS account —
    time-weighted over the real balance path, which is the only way to divide an interest
    payment by a balance that moved between £214 and £10,803 inside the period.

    Returned with `observations`, because one interest payment is one observation. Same
    discipline as the trust NAV series: the number is usable and its thinness is stated.
    """
    try:
        import extract_cash_statement as ecs
        res = cash_statement if cash_statement is not None else ecs.parse()
    except Exception as _e:                                    # noqa: BLE001
        return {"cash_expected_return_pct": None, "as_of": None,
                "source": f"cash statement unreadable ({type(_e).__name__}: {_e})",
                "observations": 0}
    rows = sorted((r for r in (res.get("rows") or []) if r.get("date")),
                  key=lambda r: str(r["date"]))
    interest = [r for r in rows if str(r.get("category", "")).upper() == "INTEREST"]
    if not rows or not interest:
        return {"cash_expected_return_pct": None, "as_of": res.get("as_of"),
                "source": ("no INTEREST row in the cash statement — the rate is UNMEASURED. "
                           "It is NOT assumed to be zero: zero is a claim about the market."),
                "observations": 0}
    end = dt.date.fromisoformat(str(interest[-1]["date"])[:10])
    start = dt.date.fromisoformat(str(rows[0]["date"])[:10])
    # time-weighted average balance: integrate the running balance over the period
    segs = []
    for i, r in enumerate(rows):
        d = dt.date.fromisoformat(str(r["date"])[:10])
        if d > end:
            break
        nxt = dt.date.fromisoformat(str(rows[i + 1]["date"])[:10]) if i + 1 < len(rows) else end
        nxt = min(nxt, end)
        days = (nxt - d).days
        if days > 0:
            segs.append((float(r.get("balance_gbp") or 0.0), days))
    tot_days = sum(d for _, d in segs)
    avg = (sum(b * d for b, d in segs) / tot_days) if tot_days else None
    paid = sum(float(r.get("receipt_gbp") or 0.0) for r in interest)
    if not avg or avg <= 0 or not tot_days:
        return {"cash_expected_return_pct": None, "as_of": res.get("as_of"),
                "source": "average cash balance not derivable over the interest period",
                "observations": len(interest)}
    rate = paid / avg * (365.0 / tot_days) * 100.0
    return {
        "cash_expected_return_pct": round(rate, 3),
        "as_of": end.isoformat(),
        "source": (f"AJ Bell cash statement — £{paid:,.2f} gross interest credited over "
                   f"{tot_days} days on a time-weighted average balance of £{avg:,.2f} "
                   f"({start.isoformat()} to {end.isoformat()}), annualised"),
        "observations": len(interest),
        "confidence": "low" if len(interest) < 4 else "medium",
        "caveat": ("⚑ ONE interest payment on record. Usable, and thin — stated rather than "
                   "smoothed. AJ Bell publishes ~2.00% on ISA cash and expects to receive "
                   "0.10% below to 0.15% above Bank Rate, so a derived figure materially away "
                   "from that band is a reason to re-read the statement, not to average it."),
        "opportunity_cost_note": ("this is the rate idle cash EARNS. The gap between it and the "
                                  "required return is the price of the waiting room, and it is "
                                  "reported per pound in shortfall_attribution."),
    }


def cash_input(path=CASH_INPUTS_PATH, derive=True, cash_statement=None):
    """DERIVED from the statement first; the declared file is an OVERRIDE, never a default.
    A hand-entered rate that silently outranks the primary source is how a stored value comes
    to say one thing and be another."""
    out = dict(DEFAULT_CASH_INPUTS)
    if derive:
        d = derive_cash_rate(cash_statement)
        if d.get("cash_expected_return_pct") is not None:
            out.update(d); out["basis"] = "derived_from_cash_statement"
    try:
        with open(path, encoding="utf-8") as f:
            f_over = json.load(f)
    except Exception:                                          # noqa: BLE001
        f_over = {}
    # ── mechanical sanity band, not prose. A derived rate is primary; a published rate is the
    # only independent reference that exists, so the two are COMPARED rather than trusted.
    ref = (f_over.get("_published_reference") or {})
    rp = ref.get("aj_bell_isa_cash_rate_pct")
    dv = out.get("cash_expected_return_pct")
    if rp is not None and dv is not None:
        out["sanity_vs_published"] = {
            "derived_pct": dv, "published_pct": rp, "delta_pp": round(dv - rp, 3),
            "published_source": ref.get("source"), "published_as_of": ref.get("as_of"),
            "within_0_50pp": bool(abs(dv - rp) <= 0.50),
            "note": ("interest accrues on CLEARED balances and settlement timing shifts the "
                     "denominator, so an exact match is not expected. A gap beyond 0.50pp "
                     "means re-read the statement rather than average the two."),
        }
    if f_over.get("cash_expected_return_pct") is not None:
        out.update({k: f_over.get(k) for k in ("cash_expected_return_pct", "as_of", "source")})
        out["basis"] = "declared_override"
        out["override_note"] = ("return_inputs.json overrides the statement-derived rate — the "
                                "derived value is retained below for comparison")
        out["derived_for_comparison"] = derive_cash_rate(cash_statement) if derive else None
    return out


# ───────────────────────────────────────────────────────── expected-return inputs
def _mk(asset_id, name, kind, value_gbp, total_value, er_by_basis, basis_notes,
        prior_pct=None, realised_pct=None, confidence=None, corroborators=None,
        unmeasured_reason=None, bucket=None):
    """ONE declared input. `er_by_basis` maps basis -> pct or None. None is NEVER 0."""
    return {
        "asset_id": asset_id, "name": name, "kind": kind, "bucket": bucket,
        "value_gbp": _round(value_gbp, 2),
        "weight": _round((value_gbp / total_value) if total_value else None, 8),
        "weight_pct": _round(100.0 * value_gbp / total_value if total_value else None, 4),
        "er_by_basis": {b: _round(er_by_basis.get(b), 4) for b in ER_BASES},
        "er_pct": _round(er_by_basis.get(ER_BASIS_OPERATIVE), 4),
        "prior_pct": _round(prior_pct, 4),
        "realised_pct": _round(realised_pct, 4),
        "confidence": confidence,
        "measured": er_by_basis.get(ER_BASIS_OPERATIVE) is not None,
        "unmeasured_reason": unmeasured_reason,
        "basis_notes": basis_notes,
        "corroborators": corroborators or {},
    }


def _shrunk(prior, realised):
    """Bounded blend. Never credits a holding more than prior + ER_EXCESS_CAP_PP."""
    if prior is None:
        return None
    if realised is None:
        return prior
    v = (1.0 - ER_SHRINK_WEIGHT) * prior + ER_SHRINK_WEIGHT * realised
    return min(v, prior + ER_EXCESS_CAP_PP)


def fund_inputs(frs_rows, tw, total_value):
    """Fund E[r] inputs.

    prior    = target_weights.funds[sedol].min_expected_return — the DECLARED long-run
               expectation. ⚑ Read here for the first time; `fund_action_stack` looks for
               `min_return`/`return_minimum`, which do not exist in that file.
    realised = fund_action_stack's own `return_adequacy_value` — IMPORTED, never recomputed,
               so Section A and the FRS can never disagree about what a fund earned.
    """
    twf = (tw or {}).get("funds", {}) or {}
    out = []
    for r in frs_rows:
        sedol = r.get("sedol")
        pol = twf.get(sedol) or {}
        prior = pol.get("min_expected_return")
        prior = None if prior is None else float(prior) * 100.0
        realised = r.get("return_adequacy_value")
        realised = None if realised is None else float(realised)
        notes = []
        unmeasured = None
        if prior is None:
            unmeasured = (f"no declared long-run expectation for {sedol} in "
                          f"target_weights.funds — refusing to assume a bucket default, because "
                          f"a fund absent from the policy file is a policy gap, not a 12%")
        if realised is None:
            notes.append("realised return unavailable from the golden source — the `realised` "
                         "and `shrunk` bases are UNMEASURED for this fund, never zero")
        if r.get("window_split"):
            notes.append("windows disagree (WINDOW_SPLIT) — the realised basis is a window "
                         "choice for this fund and is reported with that caveat")
        er = {"declared_prior": prior,
              "realised": realised,
              "shrunk": _shrunk(prior, realised)}
        conf = ("high" if prior is not None and realised is not None and not r.get("window_split")
                else "medium" if prior is not None else None)
        out.append(_mk(
            sedol, r.get("name"), "fund", r.get("value_gbp") or 0.0, total_value, er,
            notes, prior_pct=prior, realised_pct=realised, confidence=conf,
            unmeasured_reason=unmeasured, bucket=r.get("bucket"),
            corroborators={
                "frs": r.get("frs"), "frs_band": r.get("band"),
                "realised_windows": r.get("windows"),
                "return_adequacy_basis": r.get("return_adequacy_basis"),
                "mwr_annualised_pct": r.get("mwr_annualised_pct"),
                "xray_5y_ann": r.get("xray_5y_ann"),
                "bucket_minimum_pct_in_force": r.get("bucket_minimum_pct"),
            }))
    return out


def stock_inputs(stocks, metrics_tickers, total_value, anchor_pct):
    """Stock E[r] inputs.

    prior    = anchor + STOCK_SLEEVE_PREMIUM_PP — the DECLARED sleeve expectation, now
               anchor-derived (it reproduces the legacy 18.0 exactly at today's anchor).
    realised = the name's forward E[r] from `expected_return`, reported as evidence.

    ⚑ TWO THINGS ARE DELIBERATELY NOT DONE HERE.
    1. The forward E[r] is a 12-24 MONTH total expected return on a single name. AVGO reads
       +53.4% and MU +58.5% on live August data (growth clamped at the +50 cap, full re-rate
       credit). Feeding those into a 12-year compounding projection would put the stock sleeve's
       contribution to Section C at ~4pp and declare the portfolio comfortably on track on the
       strength of a semiconductor upcycle. The forward E[r] is the right input for the
       DEPLOYMENT gate (ER_DEPLOY_FLOOR) and the wrong one for the structural question.
    2. `expected_return.compute_expected_return` returns `expected_return_12_24m: 0.0` with
       `er_confidence: 0.0` when every term is missing — live on ONT this month. A confident
       zero where a refusal belongs is the failure family this register catalogues, so
       confidence == 0 is treated as UNMEASURED here and reported as a defect upstream.
    """
    try:
        import expected_return as _er
    except Exception:                                          # noqa: BLE001
        _er = None
    prior = float(anchor_pct) + STOCK_SLEEVE_PREMIUM_PP
    out, zero_conf = [], []
    for s in stocks or []:
        tkr = (s.get("ticker") or "").upper()
        fwd, conf_raw, basis_str = None, None, None
        row = (metrics_tickers or {}).get(tkr)
        if row is not None and _er is not None:
            try:
                e = _er.expected_return_for_row(row)
                conf_raw = e.get("er_confidence")
                basis_str = e.get("er_basis")
                if conf_raw is not None and float(conf_raw) > 0:
                    fwd = float(e.get("expected_return_12_24m"))
                else:
                    zero_conf.append(tkr)
            except Exception as _e:                            # noqa: BLE001
                basis_str = f"error: {type(_e).__name__}"
        notes = ["prior = A19 anchor + declared single-stock premium "
                 f"{STOCK_SLEEVE_PREMIUM_PP}pp (reproduces the legacy 18.0% assumption exactly "
                 "at today's anchor, and now moves with it)"]
        if fwd is None:
            notes.append("forward 12-24m E[r] UNMEASURED for this name — reported as absent, "
                         "never as zero")
        er = {"declared_prior": prior, "realised": fwd, "shrunk": _shrunk(prior, fwd)}
        out.append(_mk(
            tkr, s.get("name"), "stock", s.get("value_gbp") or 0.0, total_value, er, notes,
            prior_pct=prior, realised_pct=fwd,
            confidence="high" if fwd is not None else "medium",
            corroborators={"forward_er_12_24m": fwd, "er_confidence": conf_raw,
                           "er_basis": basis_str, "gain_pct": s.get("gain_pct")}))
    return out, zero_conf


def cash_inputs(portfolio, total_value, declared):
    """Cash + MMF as ONE declared input.

    ⚑ Cash has always entered Section C at an implicit 0%. That is not "excluded" — it is a
    claim that the money-market rate is zero, made by omission. At ~7% of the ISA the difference
    between 0% and a real ~4% is ~0.3pp on the total, which is a quarter of a rung on the
    Section C ladder. The rate is DECLARED with a source and a date, or the weight is UNMEASURED.
    """
    summ = (portfolio or {}).get("summary", {}) or {}
    cash_v = float(summ.get("cash_effective_gbp") or 0.0)
    mmf_v = float(summ.get("mmf_value_gbp") or 0.0)
    v = cash_v if mmf_v <= 0 else cash_v          # cash_effective already includes the MMF
    rate = declared.get("cash_expected_return_pct")
    rate = None if rate is None else float(rate)
    notes = ["cash and any money-market holding are one input: the MMF is a waiting room, and "
             "pricing it as an equity destination would let idle capital hide inside the "
             "return test"]
    unmeasured = None
    if rate is None:
        unmeasured = ("no declared money-market rate in return_inputs.json — the cash weight is "
                      "UNMEASURED. It is NOT credited 0%, because 0% is an assertion about the "
                      "market that nobody has made or dated.")
    er = {b: rate for b in ER_BASES}
    return _mk("CASH", "Cash + money-market", "cash", v, total_value, er, notes,
               prior_pct=rate, realised_pct=None,
               confidence="high" if rate is not None else None,
               unmeasured_reason=unmeasured,
               corroborators={"cash_effective_gbp": cash_v, "mmf_value_gbp": mmf_v,
                              "declared_as_of": declared.get("as_of"),
                              "declared_source": declared.get("source")})


# ────────────────────────────────────────────────────────────────── sections A / B / C
def _sleeve(inputs, kinds, basis, label):
    rows = [i for i in inputs if i["kind"] in kinds]
    w_tot = sum(i["weight"] or 0.0 for i in rows)
    covered = [i for i in rows if i["er_by_basis"].get(basis) is not None]
    w_cov = sum(i["weight"] or 0.0 for i in covered)
    if w_cov <= 0:
        return {"label": label, "value_pct": None, "weight": _round(w_tot, 8),
                "covered_weight": 0.0, "coverage": 0.0, "n": len(rows), "n_covered": 0,
                "status": "UNMEASURED",
                "note": "no holding in this sleeve carries a measured expected return under "
                        f"basis '{basis}' — reported as unmeasured, never as zero",
                "uncovered": [i["asset_id"] for i in rows]}
    num = sum((i["weight"] or 0.0) * i["er_by_basis"][basis] for i in covered)
    return {
        "label": label,
        "value_pct": _round(num / w_cov, 4),          # renormalised WITHIN the covered part
        "weighted_numerator": _round(num, 10),        # kept for the I-RA-2 identity
        # ⚑ EXACT, UNROUNDED. The first cut compared a 4dp-rounded average against a 10dp
        # recombination and I-RA-2 fired at 3.3e-05 — the invariant correctly refusing to call
        # two DIFFERENTLY ROUNDED numbers "agreeing". An identity must be tested on the values,
        # not on their presentation.
        "_exact_num": num, "_exact_w": w_cov,
        "weight": _round(w_tot, 8), "covered_weight": _round(w_cov, 8),
        "coverage": _round(w_cov / w_tot if w_tot else None, 6),
        "n": len(rows), "n_covered": len(covered), "status": "computed",
        "uncovered": [i["asset_id"] for i in rows if i["er_by_basis"].get(basis) is None],
    }


def _verdict(value, on_track, watch, labels=("On track", "Watch", "Flag")):
    if value is None or on_track is None or watch is None:
        return None
    if value >= on_track:
        return labels[0]
    if value >= watch:
        return labels[1]
    return labels[2]


def sections(inputs, basis, thr, anchor_pct):
    """Sections A / B / C, mechanically, with the identity between two aggregation paths
    ASSERTED rather than assumed (I-RA-2)."""
    a = _sleeve(inputs, ("fund",), basis, "Fund sleeve")
    b = _sleeve(inputs, ("stock",), basis, "Stock sleeve")
    c_cash = _sleeve(inputs, ("cash",), basis, "Cash + MMF")
    total = _sleeve(inputs, ("fund", "stock", "cash"), basis, "Total ISA")

    # ── I-RA-2. Two INDEPENDENT aggregation paths must agree exactly. ──────────────────
    # bottom-up  : sum over every holding of weight x E[r], renormalised over covered weight
    # via-sleeves: the three sleeve averages recombined by their covered weights
    # They are arithmetically identical only if nothing is dropped, double-counted or
    # renormalised inconsistently — which is precisely what a silent coverage bug looks like.
    identity = None
    if total["status"] == "computed":
        via = sum((s.get("_exact_num") or 0.0) for s in (a, b, c_cash))
        w_all = total.get("_exact_w") or 0.0
        via_pct = (via / w_all) if w_all else None
        bottom = total.get("_exact_num") / w_all if w_all else None
        gap = None if (via_pct is None or bottom is None) else abs(via_pct - bottom)
        identity = {"bottom_up_pct": _round(bottom, 10), "via_sleeves_pct": _round(via_pct, 10),
                    "abs_gap_pp": _round(gap, 14), "tolerance_pp": 1e-12,
                    "holds": bool(gap is not None and gap < 1e-12),
                    "note": ("two aggregation paths over the same holdings: straight across every "
                             "position, and via the three sleeve averages recombined by covered "
                             "weight. Tested on exact values, not rounded ones.")}

    d = thr["derived"]
    sa = dict(a); sa.update({
        "bands": {"pass": d.get("section_a_pass"), "inconclusive": d.get("section_a_inconclusive")},
        "verdict": _verdict(a["value_pct"], d.get("section_a_pass"), d.get("section_a_inconclusive"),
                            ("PASS", "INCONCLUSIVE", "FAIL")),
    })
    sb = dict(b); sb.update({
        "bands": {"on_track": d.get("section_b_on_track"), "watch": d.get("section_b_watch")},
        "verdict": _verdict(b["value_pct"], d.get("section_b_on_track"), d.get("section_b_watch")),
    })
    sc = dict(total); sc.update({
        "bands": {"on_track": d.get("section_c_on_track"), "watch": d.get("section_c_watch")},
        "verdict": _verdict(total["value_pct"], d.get("section_c_on_track"), d.get("section_c_watch")),
        "anchor_pct": _round(anchor_pct, 4),
        "shortfall_pp": (None if total["value_pct"] is None
                         else _round(anchor_pct - total["value_pct"], 4)),
        "cash_sleeve": c_cash,
        "identity_check": identity,
    })
    # I-RA-4. Below the coverage floor no verdict is issued at all.
    for s in (sa, sb, sc):
        cov = s.get("coverage")
        if cov is not None and cov < COVERAGE_FLOOR:
            s["verdict_withheld"] = s.pop("verdict", None)
            s["verdict"] = "INSUFFICIENT_COVERAGE"
            s["verdict_note"] = (f"coverage {cov:.1%} is below the {COVERAGE_FLOOR:.0%} floor — "
                                 f"a verdict on {1 - cov:.1%} of unmeasured weight would be a "
                                 f"claim about holdings that were never read")
    return {"section_a": sa, "section_b": sb, "section_c": sc}


# ───────────────────────────────────────────────────────────── shortfall attribution
def shortfall_attribution(inputs, basis, anchor_pct, covered_weight):
    """⚑ *Every pound must earn its place*, made arithmetic.

    contribution_i = (w_i / W_covered) x (anchor - er_i)

    Summed over every covered holding this is EXACTLY the total shortfall (I-RA-3). A holding
    earning above the anchor contributes NEGATIVELY — it is carrying the ones that do not.
    This is the first time the framework can say which pounds are short and by how much.
    """
    if not covered_weight:
        return {"rows": [], "sum_pp": None, "holds": False,
                "note": "no covered weight — attribution refused"}
    rows = []
    for i in inputs:
        er = i["er_by_basis"].get(basis)
        if er is None:
            continue
        w = (i["weight"] or 0.0) / covered_weight
        rows.append({
            "asset_id": i["asset_id"], "name": i["name"], "kind": i["kind"],
            "bucket": i.get("bucket"),
            "weight_of_covered_pct": _round(100.0 * w, 4),
            "er_pct": _round(er, 4),
            "gap_vs_anchor_pp": _round(anchor_pct - er, 4),
            "contribution_to_shortfall_pp": _round(w * (anchor_pct - er), 6),
        })
    rows.sort(key=lambda r: -(r["contribution_to_shortfall_pp"] or 0))
    total = sum(r["contribution_to_shortfall_pp"] for r in rows)
    return {"rows": rows, "sum_pp": _round(total, 6),
            "note": ("positive = this holding is dragging the portfolio below the required "
                     "return; negative = it is carrying the others")}


# ───────────────────────────────────────────────────────────────────────────── levers
def _blocked(name, why, mechanism):
    return {"lever": name, "delta_pp": None, "feasible": False, "blocked_reason": why,
            "mechanism": mechanism, "assumptions": []}


def levers(inputs, basis, thr, anchor_pct, sect, tw, frs_rows, portfolio):
    """What each available action is worth, in pp of TOTAL portfolio expected return.

    ⚑ THEY DO NOT ADD UP AND ARE NOT SUMMED. Every lever below competes for the same pounds —
    deploying the cash into the best destination and rebalancing to target weights are two
    claims on one pot. A combined figure is emitted only for a DECLARED disjoint pair, named.
    """
    out = []
    total_value = float(((portfolio or {}).get("summary") or {}).get("total_value_gbp") or 0.0)
    covered_w = sect["section_c"].get("covered_weight") or 0.0
    by_id = {i["asset_id"]: i for i in inputs}
    funds = [i for i in inputs if i["kind"] == "fund"]
    band_of = {r.get("sedol"): r.get("band") for r in (frs_rows or [])}

    # ── 1. Deploy idle cash into the best sanctioned destination ──────────────────────
    cash = by_id.get("CASH")
    eligible = [f for f in funds
                if band_of.get(f["asset_id"]) == "HOLD/ADD"
                and f["er_by_basis"].get(basis) is not None]
    dest = max(eligible, key=lambda f: f["er_by_basis"][basis]) if eligible else None
    dep_gbp = float(((portfolio or {}).get("summary") or {}).get("cash_deployable_gbp") or 0.0)
    mech = ("deployable cash weight x (destination E[r] - cash E[r]), destination = the "
            "highest-E[r] fund the FRS still sanctions for new money (HOLD/ADD)")
    if cash is None or cash["er_by_basis"].get(basis) is None:
        out.append(_blocked("deploy_idle_cash",
                            "the money-market rate is undeclared (return_inputs.json), so the "
                            "gain from deploying cash cannot be priced. Declaring it is a "
                            "one-line change and it is the single cheapest unlock on this list.",
                            mech))
    elif dest is None:
        out.append(_blocked("deploy_idle_cash",
                            "no fund is currently FRS-eligible for new money. ⚑ An ownership "
                            "floor that leaves capital nowhere sanctioned to go is not enforcing "
                            "'no idle cash' — it is obstructing it.", mech))
    else:
        dw = (dep_gbp / total_value) if total_value else 0.0
        delta = dw * (dest["er_by_basis"][basis] - cash["er_by_basis"][basis])
        out.append({"lever": "deploy_idle_cash", "delta_pp": _round(delta, 4), "feasible": True,
                    "blocked_reason": None, "mechanism": mech,
                    "detail": {"deployable_gbp": _round(dep_gbp, 2),
                               "destination": dest["asset_id"], "destination_name": dest["name"],
                               "destination_er_pct": dest["er_by_basis"][basis],
                               "cash_er_pct": cash["er_by_basis"][basis]},
                    "assumptions": ["destination must still clear the H9 look-through gate and "
                                    "its policy band before this is actioned — priced here, not "
                                    "authorised here"]})

    # ── 2. Rebalance every fund to its declared target weight ─────────────────────────
    twf = (tw or {}).get("funds", {}) or {}
    num_now = num_tgt = w_now = w_tgt = 0.0
    missing_target = []
    for f in funds:
        er = f["er_by_basis"].get(basis)
        t = (twf.get(f["asset_id"]) or {}).get("target_pct")
        if er is None:
            continue
        if t is None:
            missing_target.append(f["asset_id"])
            continue
        num_now += (f["weight"] or 0.0) * er; w_now += (f["weight"] or 0.0)
        num_tgt += float(t) * er;             w_tgt += float(t)
    mech2 = ("fund sleeve E[r] at declared target weights minus at actual weights, both "
             "renormalised within the sleeve, then scaled by the fund sleeve's share of the ISA")
    if w_now <= 0 or w_tgt <= 0:
        out.append(_blocked("rebalance_to_target_weights",
                            "target weights unavailable for the measured funds", mech2))
    else:
        delta = (num_tgt / w_tgt - num_now / w_now) * w_now
        out.append({"lever": "rebalance_to_target_weights", "delta_pp": _round(delta, 4),
                    "feasible": True, "blocked_reason": None, "mechanism": mech2,
                    "detail": {"sleeve_er_at_actual_pct": _round(num_now / w_now, 4),
                               "sleeve_er_at_target_pct": _round(num_tgt / w_tgt, 4),
                               "funds_without_target_weight": missing_target},
                    "assumptions": ["policy-sanctioned by construction — these ARE the declared "
                                    "target weights; dealing costs and min-hold are not priced"]})

    # ── 3. Grow the stock sleeve to the Phase-2 trigger ───────────────────────────────
    stocks = [i for i in inputs if i["kind"] == "stock"]
    s_w = sum(i["weight"] or 0.0 for i in stocks)
    trig = ((tw or {}).get("thresholds") or {}).get("phase_transition_pct")
    fund_er = sect["section_a"].get("value_pct")
    stock_er = sect["section_b"].get("value_pct")
    mech3 = ("(phase-2 stock-sleeve trigger - actual stock weight) x (stock sleeve E[r] - fund "
             "sleeve E[r]); funded from the fund sleeve pro rata")
    if trig is None or fund_er is None or stock_er is None:
        out.append(_blocked("grow_stock_sleeve_to_phase2", "sleeve E[r] or the phase trigger is "
                                                           "unmeasured", mech3))
    else:
        dw = max(0.0, float(trig) - s_w)
        out.append({"lever": "grow_stock_sleeve_to_phase2",
                    "delta_pp": _round(dw * (stock_er - fund_er), 4), "feasible": True,
                    "blocked_reason": None, "mechanism": mech3,
                    "detail": {"stock_weight_now_pct": _round(100 * s_w, 2),
                               "phase2_trigger_pct": _round(100 * float(trig), 2),
                               "stock_sleeve_er_pct": stock_er, "fund_sleeve_er_pct": fund_er},
                    "assumptions": [
                        "⚑ NOT currently sanctioned at this size. The sleeve-confidence "
                        "roadmap sets Stage 1 at ONE position of 0.75%, so this prices the "
                        "STRUCTURAL value of the shift, not an action available this month",
                        "assumes new stock capital earns the declared sleeve premium, which is "
                        "an assumption about the sleeve and not a forecast for any name"]})

    # ── 4. Resume the paused standing order ──────────────────────────────────────────
    # ⚑ This one moves the BAR, not the ball. It lowers the required return; it does not raise
    # the expected one. Reporting it in the same units without saying so would be a category
    # error — so it carries `moves` explicitly.
    mech4 = ("re-solve the A19 required return with the contribution schedule resumed vs paused "
             "(derive_required_return's own solver — no second arithmetic)")
    try:
        import derive_required_return as drr
        st = json.load(open(os.path.join(HERE, "target_state.json"), encoding="utf-8"))
        pv = float(((portfolio or {}).get("summary") or {}).get("total_value_gbp")
                   or st["portfolio_value_gbp"])
        vd = dt.date.fromisoformat(st["portfolio_value_date"])
        td = dt.date.fromisoformat(st["target_date"])
        sched = st["contribution_schedule"]
        resumed = [dict(s) for s in sched]
        live = max((float(s["monthly_gbp"]) for s in sched), default=0.0)
        if live > 0:
            resumed = [{"from": vd.isoformat(), "monthly_gbp": live}]
        now_pct = drr.solve_required_annual_pct(float(st["target_floor_gbp"]), pv, vd, td, sched)
        res_pct = drr.solve_required_annual_pct(float(st["target_floor_gbp"]), pv, vd, td, resumed)
        out.append({"lever": "resume_standing_order", "delta_pp": _round(now_pct - res_pct, 4),
                    "feasible": True, "blocked_reason": None, "mechanism": mech4,
                    "moves": "the required return DOWN — not the expected return up",
                    "detail": {"required_now_pct": now_pct, "required_if_resumed_pct": res_pct,
                               "monthly_gbp": live,
                               "schedule_now": [f"{s['monthly_gbp']}/mo from {s['from']}"
                                                for s in sched]},
                    "assumptions": ["S/O paused since Jul-26 on job security — a personal "
                                    "constraint, not a portfolio one. Priced, not recommended"]})
    except Exception as _e:                                    # noqa: BLE001
        out.append(_blocked("resume_standing_order", f"{type(_e).__name__}: {_e}", mech4))

    # ── 5. Move the weight the FRS will not fund — DECOMPOSED ────────────────────────
    # ⚑ The first cut of this lever reported a single "+1.04pp from upgrading unsanctioned
    # weight". That number was almost entirely a BUCKET REALLOCATION wearing a fund-selection
    # label, and the distinction is the whole point:
    #
    #   Under the operative `declared_prior` basis every fund in a bucket shares one expected
    #   return, so swapping a RETAIN-ONLY B1 fund for a HOLD/ADD B1 fund is worth EXACTLY ZERO.
    #   All of the apparent gain came from moving B1 money (9%) into a B2/B3 mandate (12-13%) —
    #   which is a change to the RISK of the portfolio, not an upgrade of its managers.
    #
    # Reporting those as one figure would tell Raj he can close a third of the gap by picking
    # better funds. He cannot. The structural gap is an allocation fact. Split, and both halves
    # priced under both bases so the disagreement between them is visible.
    mech5 = ("weight in RETAIN-ONLY / DEAD MONEY / WINDOW_SPLIT funds x (best sanctioned "
             "destination E[r] - that fund's E[r]), split by whether the destination sits in "
             "the SAME bucket (manager selection) or a different one (allocation)")
    if dest is None:
        out.append(_blocked("switch_within_bucket", "no FRS-sanctioned destination exists", mech5))
        out.append(_blocked("reallocate_across_buckets", "no FRS-sanctioned destination exists", mech5))
    else:
        same_g = cross_g = 0.0
        same_m, cross_m = [], []
        for f in funds:
            er = f["er_by_basis"].get(basis)
            if er is None or band_of.get(f["asset_id"]) in (None, "HOLD/ADD", "UNSCORED"):
                continue
            peers = [e for e in eligible if e.get("bucket") == f.get("bucket")]
            best_same = max(peers, key=lambda e: e["er_by_basis"][basis]) if peers else None
            g_same = ((f["weight"] or 0.0) * (best_same["er_by_basis"][basis] - er)
                      if best_same else 0.0)
            g_all = (f["weight"] or 0.0) * (dest["er_by_basis"][basis] - er)
            rec = {"asset_id": f["asset_id"], "bucket": f.get("bucket"),
                   "band": band_of.get(f["asset_id"]), "er_pct": er}
            if g_same > 0:
                same_g += g_same
                same_m.append({**rec, "to": best_same["asset_id"], "gain_pp": _round(g_same, 4)})
            if g_all - max(g_same, 0.0) > 0:
                cross_g += g_all - max(g_same, 0.0)
                cross_m.append({**rec, "to": dest["asset_id"], "to_bucket": dest.get("bucket"),
                                "gain_pp": _round(g_all - max(g_same, 0.0), 4)})
        out.append({"lever": "switch_within_bucket", "delta_pp": _round(same_g, 4),
                    "feasible": bool(same_m),
                    "blocked_reason": None if same_m else
                    "no same-bucket switch gains anything under the operative basis",
                    "mechanism": mech5, "detail": {"moved": same_m},
                    "assumptions": [
                        "⚑ under `declared_prior` this is near-zero and comes ONLY from "
                        "per-fund overrides in target_weights (Vanguard Japan is declared at "
                        "8% inside a 9% B1 bucket; Polar at 13% inside a 12% B2). It is NOT a "
                        "measure of manager quality on this basis — that lives under the "
                        "`realised` basis in basis_study, and the two disagree by design.",
                        "RETAIN-ONLY withholds NEW money; it is not a sell instruction"]})
        out.append({"lever": "reallocate_across_buckets", "delta_pp": _round(cross_g, 4),
                    "feasible": bool(cross_m),
                    "blocked_reason": None if cross_m else "no cross-bucket move gains anything",
                    "mechanism": mech5, "detail": {"moved": cross_m,
                                                   "destination_bucket": dest.get("bucket")},
                    "assumptions": [
                        "⚑ THIS IS A RISK DECISION, NOT A FUND UPGRADE. It moves money from a "
                        "lower-expectation bucket to a higher-expectation one, which is what "
                        "the bucket structure exists to control. It breaches the declared "
                        "bucket bands by construction and is priced, not proposed",
                        "ignores the crystallisation cost of exiting a closed-end holding at a "
                        "discount (trust_discount.py) and the concentration consequence (L1)"]})

    disjoint = ["deploy_idle_cash", "rebalance_to_target_weights"]
    vals = [l["delta_pp"] for l in out
            if l["lever"] in disjoint and l["feasible"] and l["delta_pp"] is not None]
    return {"levers": out,
            "combined_disjoint": {"levers": disjoint,
                                  "delta_pp": _round(sum(vals), 4) if vals else None,
                                  "why_only_these": ("cash deployment moves NEW money in; "
                                                     "rebalancing moves EXISTING weight between "
                                                     "funds. They do not claim the same pounds. "
                                                     "Every other pair on this list does.")},
            "not_summable_note": ("⚑ the individual figures above must NOT be added. They "
                                  "compete for the same capital and several are not authorised "
                                  "actions at all.")}


# ─────────────────────────────────────────────────────────────────────────── invariants
def check_invariants(inputs, sect, attrib, thr, tw, frs_rows):
    """Contracts at the artefact boundary. Each asserts that TWO independent derivations agree,
    which is the only defence that has ever caught this class of defect."""
    out = []

    def _add(code, ok, detail):
        out.append({"invariant": code, "holds": bool(ok), "detail": detail})

    w = sum(i["weight"] or 0.0 for i in inputs)
    _add("I-RA-1", abs(w - 1.0) < 1e-6,
         f"every holding's weight sums to {w:.10f}; a sleeve silently dropped from the "
         f"denominator is invisible in the average and obvious here")

    idc = (sect["section_c"].get("identity_check") or {})
    _add("I-RA-2", idc.get("holds") is True,
         f"Section C bottom-up {idc.get('bottom_up_pct')} vs recombined from sleeves "
         f"{idc.get('via_sleeves_pct')} (gap {idc.get('abs_gap_pp')})")

    sf = sect["section_c"].get("shortfall_pp")
    ss = attrib.get("sum_pp")
    ok3 = (sf is None and ss is None) or (
        sf is not None and ss is not None and abs(sf - ss) < 1e-4)
    _add("I-RA-3", ok3,
         f"per-holding shortfall contributions sum to {ss} against a Section C shortfall of {sf}")

    bad = [i["asset_id"] for i in inputs
           if not i["measured"] and i["er_pct"] is not None]
    _add("I-RA-4", not bad,
         f"no unmeasured holding carries a number ({len(bad)} violations{': ' + ','.join(bad) if bad else ''})")

    _add("I-RA-5", not thr["divergences"],
         f"{len(thr['divergences'])} derived/legacy threshold pair(s) disagree beyond "
         f"{THRESHOLD_PARITY_TOL_PP}pp")

    twf = (tw or {}).get("funds", {}) or {}
    read = [i["asset_id"] for i in inputs if i["kind"] == "fund" and i["prior_pct"] is not None]
    have = [s for s in (r.get("sedol") for r in (frs_rows or [])) if (twf.get(s) or {}).get("min_expected_return") is not None]
    _add("I-RA-6", len(read) == len(have) and len(read) > 0,
         f"the declared long-run expectation was READ for {len(read)} of {len(have)} funds that "
         f"carry one in target_weights.json — a default silently standing in for a policy value "
         f"is the defect this invariant exists to catch")
    return out


def basis_study(inputs, thr, anchor_pct, tw, frs_rows, portfolio):
    """Every basis, every run. The choice stays visible and stays Raj's."""
    rows = []
    for b in ER_BASES:
        s = sections(inputs, b, thr, anchor_pct)
        rows.append({
            "basis": b, "operative": b == ER_BASIS_OPERATIVE,
            "section_a_pct": s["section_a"]["value_pct"], "section_a_verdict": s["section_a"].get("verdict"),
            "section_b_pct": s["section_b"]["value_pct"], "section_b_verdict": s["section_b"].get("verdict"),
            "section_c_pct": s["section_c"]["value_pct"], "section_c_verdict": s["section_c"].get("verdict"),
            "shortfall_pp": s["section_c"].get("shortfall_pp"),
            "coverage": s["section_c"].get("coverage"),
        })
    return {"rows": rows,
            "definitions": {
                "declared_prior": ("target_weights.funds[].min_expected_return for funds; A19 "
                                   "anchor + declared single-stock premium for the stock sleeve; "
                                   "declared money-market rate for cash. Answers the STRUCTURAL "
                                   "question and invents nothing."),
                "realised": ("fund_action_stack.return_adequacy_value (the golden source's "
                             "trailing windows under the declared statistic) for funds; the "
                             "12-24m forward E[r] for stocks. Right for the OWNERSHIP question; "
                             "a bull run annualised if used for a 12-year projection."),
                "shrunk": (f"prior + {ER_SHRINK_WEIGHT} x (realised - prior), capped at prior + "
                           f"{ER_EXCESS_CAP_PP}pp. ⚑ BOTH CONSTANTS UNCALIBRATED (H5). Not "
                           f"operative; published so the sensitivity is visible."),
            },
            "note": ("the operative basis decides Section A/B/C and therefore whether the review "
                     "reports a shortfall at all. It is stated here every run so the choice can "
                     "never be made by whichever line of code got there first.")}


def bucket_minimum_divergence(tw):
    """Publish the live disagreement between the policy file and the fund action stack."""
    try:
        import fund_action_stack as fas
        code = dict(fas.DEFAULT_BUCKET_MIN)
        in_force = fas._bucket_minimums()
    except Exception as _e:                                    # noqa: BLE001
        return {"error": f"{type(_e).__name__}: {_e}"}
    pol = {k: v.get("min_expected_return") for k, v in ((tw or {}).get("buckets") or {}).items()
           if v.get("min_expected_return") is not None}
    rows = []
    for k in sorted(set(pol) | set(code)):
        p = None if pol.get(k) is None else round(float(pol[k]) * 100, 2)
        c = None if code.get(k) is None else round(float(code[k]) * 100, 2)
        rows.append({"bucket": k, "policy_pct": p, "code_default_pct": c,
                     "in_force_pct": round(float(in_force.get(k, 0)) * 100, 2) if k in in_force else None,
                     "agree": (p is not None and c is not None and abs(p - c) < 1e-9)})
    return {"rows": rows, "policy_setting": BUCKET_MIN_POLICY,
            "diagnosis": ("fund_action_stack._bucket_minimums() reads `min_return` / "
                          "`return_minimum`; target_weights.json stores `min_expected_return`. "
                          "The read has never matched, so the documented 'one home' has always "
                          "fallen through to DEFAULT_BUCKET_MIN."),
            "why_not_fixed_here": ("repairing the read would move the B1 ownership floor from "
                                   "12% to 9% as a SIDE EFFECT of a bug fix — the exact mistake "
                                   "D-C(ii) records. Measured and published; the policy move is "
                                   "Raj's, and it is one constant.")}


# ──────────────────────────────────────────────────────────────────────────────── build
def build(as_of=None, portfolio=None, frs=None, tw=None, metrics=None, out_path=None):
    as_of = as_of or dt.date.today()
    if portfolio is None:
        raise ValueError("portfolio_data is required — the weights are the whole point")
    tw = tw if tw is not None else json.load(
        open(os.path.join(HERE, "target_weights.json"), encoding="utf-8"))
    frs_rows = (frs or {}).get("fund_retention_score") or []
    total_value = float((portfolio.get("summary") or {}).get("total_value_gbp") or 0.0)
    anchor = anchor_state()
    a_pct = anchor["operative_pct"]
    thr = thresholds(a_pct, tw)

    fi = fund_inputs(frs_rows, tw, total_value)
    si, zero_conf = stock_inputs(portfolio.get("stocks") or [],
                                 (metrics or {}).get("tickers") or {}, total_value, a_pct)
    ci = cash_inputs(portfolio, total_value, cash_input())
    inputs = fi + si + [ci]

    sect = sections(inputs, ER_BASIS_OPERATIVE, thr, a_pct)
    attrib = shortfall_attribution(inputs, ER_BASIS_OPERATIVE, a_pct,
                                   sect["section_c"].get("covered_weight") or 0.0)
    lev = levers(inputs, ER_BASIS_OPERATIVE, thr, a_pct, sect, tw, frs_rows, portfolio)
    inv = check_invariants(inputs, sect, attrib, thr, tw, frs_rows)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "anchor": anchor,
        "operative_basis": ER_BASIS_OPERATIVE,
        "thresholds": thr,
        "expected_return_inputs": inputs,
        **sect,
        "shortfall_attribution": attrib,
        **lev,
        "basis_study": basis_study(inputs, thr, a_pct, tw, frs_rows, portfolio),
        "bucket_minimum_divergence": bucket_minimum_divergence(tw),
        "invariants": inv,
        "defects_observed": ([{
            "code": "ER-ZERO-CONF",
            "detail": (f"expected_return.compute_expected_return returned 0.0 with "
                       f"er_confidence 0.0 for {', '.join(zero_conf)} — a confident zero where a "
                       f"refusal belongs. Treated as UNMEASURED here; the upstream module still "
                       f"emits the zero to every other consumer."),
        }] if zero_conf else []),
        "est_return_status": {
            "role": "RETIRED as a decision input",
            "detail": ("`est_return_pct` no longer contributes to Section A, B or C. It is "
                       "retained on the fund rows as a CORROBORATOR only, so the C4 evidence "
                       "keeps accumulating. On the August run its values were prose stamped "
                       "'jul2026_run:' — estimates typed by hand a month earlier and multiplied "
                       "by portfolio weights."),
        },
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    return doc


# ────────────────────────────────────────────────────────────────────────────── selftest
def _fixture(total=100.0):
    return {"summary": {"total_value_gbp": total, "cash_effective_gbp": 10.0,
                        "cash_deployable_gbp": 10.0, "mmf_value_gbp": 0.0},
            "stocks": [{"ticker": "XX", "name": "Test Co", "value_gbp": 10.0}]}


def _selftest():
    n = 0

    def ok(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    # ── thresholds derive from the anchor and reproduce the legacy constants ──────────
    tw = json.load(open(os.path.join(HERE, "target_weights.json"), encoding="utf-8"))
    a = anchor_state()["operative_pct"]
    t = thresholds(a, tw)
    ok(t["derived"]["section_c_on_track"] == round(a, 1), "Section C on-track must BE the anchor")
    ok(t["derived"]["section_b_on_track"] == round(a + STOCK_SLEEVE_PREMIUM_PP, 1), "B derived")
    ok(abs(t["derived"]["section_b_on_track"] - t["legacy_frozen"]["section_b_on_track"]) <= THRESHOLD_PARITY_TOL_PP,
       "derived stock threshold must reproduce the legacy 18.0 at today's anchor")
    ok(abs(t["derived"]["section_c_watch"] - t["legacy_frozen"]["section_c_watch"]) <= THRESHOLD_PARITY_TOL_PP,
       "derived Section C watch must reproduce the legacy 13.0")
    # negative control: move the anchor and the parity check MUST fire
    t2 = thresholds(a + 3.0, tw)
    ok(t2["divergences"], "a moved anchor must produce reported divergences, not silence")

    # ── a missing input never becomes a number ───────────────────────────────────────
    m = _mk("Z", "Z", "fund", 10.0, 100.0, {"declared_prior": None, "realised": None,
                                            "shrunk": None}, [], unmeasured_reason="none")
    ok(m["er_pct"] is None and m["measured"] is False, "missing must stay missing")
    ok(_shrunk(None, 20.0) is None, "no prior => no shrunk value, not 20")
    ok(_shrunk(10.0, None) == 10.0, "no evidence => fall back to the prior, stated")
    ok(_shrunk(10.0, 100.0) == 10.0 + ER_EXCESS_CAP_PP, "shrunk must respect the excess cap")

    # ── sleeve arithmetic + the two-derivation identity ──────────────────────────────
    inputs = [
        _mk("F1", "F1", "fund", 50.0, 100.0, {"declared_prior": 10.0, "realised": 20.0, "shrunk": 13.5}, []),
        _mk("F2", "F2", "fund", 30.0, 100.0, {"declared_prior": 12.0, "realised": None, "shrunk": 12.0}, []),
        _mk("S1", "S1", "stock", 10.0, 100.0, {"declared_prior": 18.0, "realised": 50.0, "shrunk": 24.0}, []),
        _mk("CASH", "cash", "cash", 10.0, 100.0, {"declared_prior": 4.0, "realised": 4.0, "shrunk": 4.0}, []),
    ]
    s = sections(inputs, "declared_prior", t, a)
    ok(abs(s["section_a"]["value_pct"] - (50 * 10 + 30 * 12) / 80.0) < 1e-9, "Section A arithmetic")
    ok(s["section_c"]["identity_check"]["holds"], "I-RA-2 must hold on a clean fixture")
    exp_c = (50 * 10 + 30 * 12 + 10 * 18 + 10 * 4) / 100.0
    ok(abs(s["section_c"]["value_pct"] - exp_c) < 1e-9, f"Section C arithmetic ({exp_c})")
    ok(abs(s["section_c"]["shortfall_pp"] - (a - exp_c)) < 1e-9, "shortfall = anchor - Section C")

    # coverage: F2 has no realised value, so the realised basis must lose exactly its weight
    sr = sections(inputs, "realised", t, a)
    ok(abs(sr["section_a"]["coverage"] - 50 / 80.0) < 1e-9, "coverage must be weight-based")
    ok("F2" in sr["section_a"]["uncovered"], "an uncovered fund must be NAMED")

    # ── attribution sums EXACTLY to the shortfall ────────────────────────────────────
    at = shortfall_attribution(inputs, "declared_prior", a, s["section_c"]["covered_weight"])
    ok(abs(at["sum_pp"] - s["section_c"]["shortfall_pp"]) < 1e-9, "I-RA-3 exactness")
    # F1 (50% weight, 3.9pp below the anchor) outranks CASH (10% weight, 9.9pp below it).
    # WEIGHT x GAP, not gap alone — a small holding far below the bar is not the problem.
    ok(at["rows"][0]["asset_id"] == "F1", "the largest drag is weight x gap, not the worst rate")
    ok(abs(at["rows"][0]["contribution_to_shortfall_pp"] - 0.5 * (a - 10.0)) < 1e-9, "F1 drag arithmetic")
    neg = [r for r in at["rows"] if r["contribution_to_shortfall_pp"] < 0]
    ok(any(r["asset_id"] == "S1" for r in neg), "a holding above the anchor must contribute NEGATIVELY")

    # ── coverage floor withholds a verdict rather than guessing ──────────────────────
    thin = [_mk("F1", "F1", "fund", 50.0, 100.0, {"declared_prior": None, "realised": None, "shrunk": None}, []),
            _mk("F2", "F2", "fund", 50.0, 100.0, {"declared_prior": 12.0, "realised": 12.0, "shrunk": 12.0}, [])]
    st = sections(thin, "declared_prior", t, a)
    ok(st["section_a"]["verdict"] == "INSUFFICIENT_COVERAGE", "below the floor, no verdict")
    ok(st["section_a"].get("verdict_withheld") is not None, "the withheld verdict must be retained")

    # ── verdict bands ────────────────────────────────────────────────────────────────
    ok(_verdict(a + 1, round(a, 1), round(a - 0.9, 1)) == "On track", "band: on track")
    ok(_verdict(a - 0.5, round(a, 1), round(a - 0.9, 1)) == "Watch", "band: watch")
    ok(_verdict(a - 5, round(a, 1), round(a - 0.9, 1)) == "Flag", "band: flag")
    ok(_verdict(None, 1, 2) is None, "no value => no verdict")

    # ── the declared prior is READ from policy, not defaulted ────────────────────────
    frs_rows = [{"sedol": "VUAG", "name": "V", "bucket": "B1", "value_gbp": 100.0,
                 "return_adequacy_value": 15.47, "band": "RETAIN-ONLY", "frs": 50.6}]
    fi = fund_inputs(frs_rows, tw, 100.0)
    ok(fi[0]["prior_pct"] == 9.0, f"VUAG prior must read 9.0 from target_weights, got {fi[0]['prior_pct']}")
    ok(fi[0]["realised_pct"] == 15.47, "realised must be IMPORTED from the FRS row, not recomputed")
    unknown = fund_inputs([{"sedol": "NOPE", "name": "n", "value_gbp": 1.0,
                            "return_adequacy_value": 9.9}], tw, 100.0)
    ok(unknown[0]["prior_pct"] is None and unknown[0]["unmeasured_reason"],
       "a fund absent from the policy file must be UNMEASURED, never given a bucket default")

    # ── cash refuses to invent a rate ────────────────────────────────────────────────
    c = cash_inputs(_fixture(), 100.0, {"cash_expected_return_pct": None})
    ok(c["er_pct"] is None and c["unmeasured_reason"], "undeclared cash rate must be UNMEASURED")
    ok(cash_inputs(_fixture(), 100.0, {"cash_expected_return_pct": 4.2})["er_pct"] == 4.2, "declared rate is used")

    # ── stock inputs: zero-confidence E[r] is a refusal, not a zero ──────────────────
    si, zc = stock_inputs([{"ticker": "QQ", "name": "Q", "value_gbp": 10.0}], {}, 100.0, a)
    ok(si[0]["prior_pct"] == a + STOCK_SLEEVE_PREMIUM_PP, "stock prior is anchor-derived")
    ok(si[0]["er_by_basis"]["realised"] is None, "no metrics row => no forward E[r], not zero")

    # ── invariants report, and a broken fixture must FAIL them ──────────────────────
    inv = check_invariants(inputs, s, at, t, tw, frs_rows)
    ok(all(i["holds"] for i in inv if i["invariant"] in ("I-RA-1", "I-RA-2", "I-RA-3", "I-RA-4")),
       "core invariants must hold on the clean fixture")
    broken = [dict(i) for i in inputs]
    broken[0]["weight"] = 0.20                      # weights no longer sum to 1
    inv_b = check_invariants(broken, s, at, t, tw, frs_rows)
    ok(not next(i for i in inv_b if i["invariant"] == "I-RA-1")["holds"],
       "NEGATIVE CONTROL: a dropped weight must break I-RA-1")

    print(f"return_architecture SELF-TEST OK — {n} assertions")
    return n


def main():
    ap = argparse.ArgumentParser(description="Return architecture — one E[r] input, mechanical A/B/C.")
    ap.add_argument("--portfolio"); ap.add_argument("--frs"); ap.add_argument("--metrics")
    ap.add_argument("--out"); ap.add_argument("--as-of")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); return
    p = json.load(open(a.portfolio, encoding="utf-8")) if a.portfolio else None
    frs = json.load(open(a.frs, encoding="utf-8")) if a.frs else None
    met = json.load(open(a.metrics, encoding="utf-8")) if a.metrics else None
    d = build(dt.date.fromisoformat(a.as_of) if a.as_of else None, p, frs, None, met, a.out)
    sc, sa, sb = d["section_c"], d["section_a"], d["section_b"]
    print(f"\nANCHOR {d['anchor']['operative_pct']}% (derived {d['anchor']['derived_at']}) | "
          f"basis = {d['operative_basis']}")
    print(f"  Section A fund sleeve   {sa['value_pct']}%  {sa.get('verdict')}   "
          f"(coverage {sa.get('coverage')})")
    print(f"  Section B stock sleeve  {sb['value_pct']}%  {sb.get('verdict')}")
    print(f"  Section C TOTAL ISA     {sc['value_pct']}%  {sc.get('verdict')}   "
          f"shortfall {sc.get('shortfall_pp')}pp")
    print("\n  Largest drags on the required return:")
    for r in d["shortfall_attribution"]["rows"][:5]:
        print(f"    {r['asset_id']:<10} {r['weight_of_covered_pct']:>6.2f}%  E[r] {r['er_pct']}%  "
              f"-> {r['contribution_to_shortfall_pp']:+.3f}pp")
    print("\n  Levers:")
    for l in d["levers"]:
        if l["feasible"]:
            print(f"    {l['lever']:<32} {l['delta_pp']:+.3f}pp")
        else:
            print(f"    {l['lever']:<32} BLOCKED — {l['blocked_reason'][:80]}")
    bad = [i for i in d["invariants"] if not i["holds"]]
    print(f"\n  invariants: {len(d['invariants']) - len(bad)}/{len(d['invariants'])} hold")
    for b in bad:
        print(f"    ⚑ {b['invariant']}: {b['detail']}")


if __name__ == "__main__":
    main()
