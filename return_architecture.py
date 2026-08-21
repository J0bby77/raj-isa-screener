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
ER_BASES = ("declared_prior", "realised", "shrunk", "exposure_forward")
# ⚑ `exposure_forward` ADDED 20-Aug-2026 (ISA-0328). The FORWARD STRUCTURAL return built bottom-up
# from exposures: sum_k w_k*M_k + geometric adjustment - OCF + alpha(=0), read from
# fund_expected_return_[yyyy_mm].json. It is COMPUTED EVERY RUN and PUBLISHED in basis_study, and
# ER_BASIS_OPERATIVE is DELIBERATELY UNCHANGED at `declared_prior`.
# ⚑ WHY IT IS NOT OPERATIVE ON DAY ONE. On the 31-Jul-2026 book it moves Section A 11.0853 -> 6.16
# (INCONCLUSIVE -> FAIL), Section C 10.9860 -> ~6.86 and the shortfall +2.914 -> ~+7.040pp, and it
# puts ALL TWELVE funds below their declared floor — every bit of it 100% METHOD, 0% DATA (R2.3).
# Not one fund changed. The framework has a recorded name for shipping a policy move as the side
# effect of a measurement repair: D-C(ii). Switching the headline is Raj's, in one constant.

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

# ── ISA-0409 (20-Aug-2026) — WHAT A FAILING INVARIANT WITHDRAWS ───────────────────────────────
# ⚑ THE DEFECT. `monthly_isa_prerun` treated ANY failing invariant as grounds to leave Sections
# A/B/C "on the est_return basis" — the input register C4 proved not merely noisy but INVERTED
# (Scottish Mortgage 14.0% est against a realised 5-year of 0.22%, the highest in its bucket), and
# whose cache self-labels 8 of its 12 rows `[trailing 3yr]` under a header claiming they are
# forward (ISA-0402). Refusing arithmetic that does not reconcile is right. SUBSTITUTING AN INPUT
# THE FRAMEWORK HAS ALREADY PROVEN INVERTED IS NOT A REFUSAL, IT IS A SILENT DOWNGRADE — R4.3's
# obvious sibling, which had never been written down: a control that FIRES must not hand the
# decision to a worse input.
#
# ⚑ AND THE GATE WAS ALL-OR-NOTHING ACROSS UNRELATED QUANTITIES. I-RA-8 guards the M* golden
# fixture and says nothing about whether Sections A/B/C reconcile, yet it withdrew them. Every
# invariant now declares the SCOPE it guards, and only `sections`-scoped failures can withhold
# Sections A/B/C. An invariant withdraws the quantity it guards, and nothing else.
#
# ⚑ I-RA-5 IS SCOPED `thresholds`, AND THAT IS NOT A WIDENED TOLERANCE. Its own divergence note
# already states the conclusion: "the DERIVED value is operative (A19 invariant 6) and the legacy
# constant is now stale prose". A stale piece of prose cannot invalidate arithmetic that
# reconciles. The check is UNCHANGED — same pairs, same 0.15pp tolerance, still reported on every
# run — only what its failure DOES has changed. The frozen constants in target_weights.json are
# Raj's declared policy numbers and are deliberately NOT edited here (R2.14).
INVARIANT_SCOPE = {
    "I-RA-1": "sections",    # weights sum to 1 — a dropped sleeve corrupts every average
    "I-RA-2": "sections",    # Section C's two aggregation paths agree
    "I-RA-3": "sections",    # per-holding shortfall sums to the Section C shortfall
    "I-RA-4": "sections",    # no unmeasured holding carries a number
    "I-RA-5": "thresholds",  # derived vs frozen-constant parity — REPORTS staleness, gates nothing
    "I-RA-6": "sections",    # the declared prior was READ for every fund that carries one
    "I-RA-7": "mstar",       # M* substituted back reproduces the anchor
    "I-RA-8": "mstar",       # the M* golden fixture has not silently moved (ISA-0383)
}
ADOPTION_SCOPE = "sections"  # the only scope that may withhold Sections A/B/C

# ⚑ AND WHAT HAPPENS INSTEAD. Where a `sections`-scoped invariant fails, Sections A/B/C are
# published UNMEASURED with the failing invariant NAMED. `est_return` stays where it already
# correctly sits — as `est_basis_corroborator` — and may not become operative for anything.
# "I could not compute it" and "here is a number from an inverted input" must never produce the
# same output (R2.10).
ADOPTION_REFUSAL_BASIS = "UNMEASURED_INVARIANT_FAILED"

# ── COVERAGE ────────────────────────────────────────────────────────────────────────────
# ⚑ "missing" cannot be a number. Below this, no verdict is issued at all.
COVERAGE_FLOOR = 0.90

# ── BUCKET-MINIMUM PROVENANCE — RESOLVED by D-13 (Raj, 09-Aug-2026) ─────────────────────
# The divergence this block used to preserve: `fund_action_stack._bucket_minimums()` read keys
# `min_return` / `return_minimum`, neither of which exists in target_weights.json, so the
# documented "one home" always fell through to DEFAULT_BUCKET_MIN and the B1 ownership floor read
# 12.0% in the fund action stack and 9.0% in the policy file, in the same report, for the same fund.
#
# It was deliberately left unfixed because repairing the read alone would have LOOSENED B1 by 3pp
# as a side effect of a bug fix — the mistake D-C(ii) records. D-13 makes the policy call first:
# **B1 = 0.12**, the read is repaired, and the field is renamed `ownership_floor_return` because
# D-8 moved the return-EXPECTATION job out of it. Policy and code default now agree, so the
# measurement fix ships with a NULL behaviour delta (D-20: 100% method, 0% data).
#
# The publisher below stays. A resolved divergence still needs a live check — this is now the
# assertion that the two derivations continue to agree, not a report that they do not.
BUCKET_MIN_POLICY = "target_weights"   # "code_default_pending_decision" | "target_weights"

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


_EF_CACHE = {}


def _exposure_forward_pct(sedol):
    """The fund's forward STRUCTURAL E[r] from fund_expected_return (ISA-0328), or None.

    ⚑ NOT `expected_return_12_24m`. That is a 12-24 month SINGLE-NAME total return reading +53.4%
    on AVGO; this is an ~11-year structural return reading +3.06% on Polar. This module already
    documents having nearly been caught by that confusion once — see stock_inputs()."""
    global _EF_CACHE
    if not _EF_CACHE:
        _EF_CACHE = {"_loaded": True, "rows": {}}
        try:
            import re as _re
            pat = _re.compile(r"^fund_expected_return_(\d{4})_(\d{2})\.json$")
            best, key = None, None
            for f in os.listdir(HERE):
                m = pat.match(f)
                if m:
                    k = (int(m.group(1)), int(m.group(2)))
                    if key is None or k > key:
                        best, key = f, k
            if best:
                with open(os.path.join(HERE, best), encoding="utf-8") as fh:
                    doc = json.load(fh)
                if doc.get("state") != "DISABLED":
                    _EF_CACHE["rows"] = {sd: r.get("structural_er_pct")
                                         for sd, r in (doc.get("funds") or {}).items()
                                         if r.get("state") == "MEASURED"}
        except Exception:                                      # noqa: BLE001
            pass
    return _EF_CACHE.get("rows", {}).get(sedol)


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
              "shrunk": _shrunk(prior, realised),
              # ⚑ READ from the artefact, never recomputed here — one home (R4.4). Absent or
              # UNMEASURED stays None and is reported as UNMEASURED, never as zero (R4.1).
              "exposure_forward": _exposure_forward_pct(sedol)}
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
    2. `expected_return.compute_expected_return` returned `expected_return_12_24m: 0.0` with
       `er_confidence: 0.0` when every term was missing — live on ONT this month. A confident
       zero where a refusal belongs is the failure family this register catalogues, so
       confidence == 0 is treated as UNMEASURED here and was reported as a defect upstream.
       ⚑ FIXED UPSTREAM 09-Aug-2026 (D-24 §5): `er_status` is now a first-class field and a
       refused re-rate returns `er_rerate = None`, not 0. The confidence == 0 test below is
       RETAINED as a belt-and-braces check on older frames; `er_status` is the primary signal.
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
                # ⚑ D-24 §1.2 (09-Aug-2026). Section A/B/C recomputes E[r] at REVIEW time on
                # PORTFOLIO rows, with no screen anchor table in scope. It now reads the
                # PERSISTED table so Section C and the screen cannot disagree on the same name;
                # where none exists the re-rate is declared UNMEASURED, never silently zeroed.
                e = _er.expected_return_for_row(
                    row, anchor_table=_er.load_anchor_table(required=False),
                    allow_missing_anchor_table=True)
                conf_raw = e.get("er_confidence")
                basis_str = e.get("er_basis")
                # D-24: er_status is now FIRST-CLASS. Previously the only signal that every term
                # was missing was er_confidence == 0 — a confident zero where a refusal belonged.
                if e.get("er_status") == "unmeasured":
                    zero_conf.append(tkr)
                elif conf_raw is not None and float(conf_raw) > 0:
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
        out.append({"invariant": code, "holds": bool(ok), "detail": detail,
                    "scope": INVARIANT_SCOPE.get(code, "sections"),
                    "withholds_sections": (INVARIANT_SCOPE.get(code, "sections") == ADOPTION_SCOPE)})

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

    # ── I-RA-7 (D-8/D-12, 12-Aug-2026). M* substituted back must reproduce the anchor. ────
    im = _implied_m_block(inputs, thr["derived"]["section_c_on_track"])
    ic = im.get("identity_check") or {}
    _add("I-RA-7", (im["status"] == "computed" and ic.get("holds") is True) or
         im["status"] in ("BLOCKED", "INSUFFICIENT_COVERAGE", "REFUSED_NO_MARKET_SENSITIVITY"),
         (f"M* {im.get('m_star_pct')} at λ {im.get('leverage_lambda')}; substituting it back "
          f"through every holding reproduces the anchor (gap {ic.get('abs_gap_pp')}). Status "
          f"{im['status']} — a refusal is a pass here, a computed value failing the "
          f"substitution is not"))
    # ── I-RA-8. The register's published M* and this module's must not drift apart. ───────
    # ⚑ I-RA-8 RE-FOUNDED 19-Aug-2026 (ISA-0378) — see I-D23-5. The D-8 divergence is still
    #    published on every run under d8_reconciliation.basis = RETIRED_WITH_CAUSE; what is
    #    ASSERTED is the golden fixture, which names any input that has moved.
    d8 = im.get("d8_reconciliation") or {}
    # `betas` is not in scope here; read them the same way _implied_m_block does, so the
    # invariant and the block it judges cannot diverge on which alpha set they used.
    _betas, _ = fund_betas()
    gold = (mstar_golden_check(inputs, _betas) if _betas is not None
            else {"status": "NO_BETAS", "holds": None, "detail": "no measured betas on disk"})
    _add("I-RA-8", (im["status"] != "computed") or gold.get("holds") is True,
         (gold.get("detail") or "") +
         (f" | D-8's published {d8.get('d8_published_m_star_pct')}% is "
          f"{d8.get('basis')}, still published, no longer asserted"))
    return out


def adoption_gate(invariants):
    """ONE HOME for 'may Sections A/B/C be adopted this run, and if not, what happens instead'.

    ⚑ ISA-0409. The consumer (monthly_isa_prerun Step 9d) READS this and never re-derives it, so
    the module that owns the invariants owns what they withdraw — the alternative is two homes for
    one rule, which is how the all-or-nothing gate survived unnoticed in the first place (R4.4)."""
    unknown = sorted({i.get("invariant") for i in invariants
                      if i.get("invariant") not in INVARIANT_SCOPE})
    blocking = [i for i in invariants
                if not i["holds"] and i.get("scope") == ADOPTION_SCOPE]
    other = [i for i in invariants if not i["holds"] and i.get("scope") != ADOPTION_SCOPE]
    return {
        "adoption_scope": ADOPTION_SCOPE,
        "adoptable": not blocking and not unknown,
        "blocking_invariants": [i["invariant"] for i in blocking],
        "blocking_detail": {i["invariant"]: i["detail"] for i in blocking},
        "failing_out_of_scope": {i["invariant"]: {"scope": i.get("scope"), "detail": i["detail"]}
                                 for i in other},
        # ⚑ An invariant with no declared scope BLOCKS. A new invariant that nobody classified
        # must not silently inherit "does not matter" (R4.7 — a contract change RAISES).
        "unscoped_invariants": unknown,
        "on_refusal": ADOPTION_REFUSAL_BASIS,
        "refusal_semantics": (
            "Sections A/B/C publish UNMEASURED with the failing invariant NAMED. `est_return` is "
            "NOT substituted: it is retired as a decision input (register C4 proved it inverted) "
            "and stays only as `est_basis_corroborator`. A refusal that hands the decision to a "
            "worse input is a downgrade wearing the appearance of caution (ISA-0409, R2.10/R4.3)."),
        "note": ("an invariant withdraws the quantity it GUARDS and nothing else. I-RA-7/I-RA-8 "
                 "guard M* and withdraw M*; I-RA-5 reports that a frozen constant in "
                 "target_weights.json has gone stale against the operative anchor and gates "
                 "nothing, because the module already declares the derived value operative."),
    }


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
    fld = getattr(fas, "BUCKET_FLOOR_FIELD", "ownership_floor_return")
    pol = {k: v.get(fld) for k, v in ((tw or {}).get("buckets") or {}).items()
           if v.get(fld) is not None}
    rows = []
    for k in sorted(set(pol) | set(code)):
        p = None if pol.get(k) is None else round(float(pol[k]) * 100, 2)
        c = None if code.get(k) is None else round(float(code[k]) * 100, 2)
        rows.append({"bucket": k, "policy_pct": p, "code_default_pct": c,
                     "in_force_pct": round(float(in_force.get(k, 0)) * 100, 2) if k in in_force else None,
                     "agree": (p is not None and c is not None and abs(p - c) < 1e-9)})
    return {"rows": rows, "policy_setting": BUCKET_MIN_POLICY,
            "field": fld,
            "resolved_by": "D-13 (Raj, 09-Aug-2026)",
            "diagnosis": ("RESOLVED. `fund_action_stack._bucket_minimums()` read `min_return` / "
                          "`return_minimum`, keys that never existed in target_weights.json, so the "
                          "documented 'one home' always fell through to DEFAULT_BUCKET_MIN. D-13 set "
                          "B1 to 0.12, repaired the read and renamed the field to "
                          "`ownership_floor_return` (D-8 moved the return-expectation job out of it)."),
            "behaviour_delta": ("NONE. B1 policy 9% -> 12% matches the value already in force, so no "
                                "floor, band, FRS band or verdict moves. 100% method, 0% data (D-20)."),
            "live_check": ("_bucket_minimums() now RAISES BucketFloorDivergence on a legacy field "
                           "name, a missing policy value or any policy-vs-code disagreement, instead "
                           "of falling back to the code default silently.")}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# IMPLIED FORWARD MARKET RETURN M*  —  D-8 mechanism, built for D-23 (ISA-0158), 12-Aug-2026
#
# ⚑ WHAT M* IS, AND WHY IT IS THE ONE E[r] BASIS THAT NEEDS NO E[r] ASSUMPTION
# The three legacy bases each answer "what will this portfolio earn?" and each requires an input
# nobody owns — a declared prior per fund, a trailing window annualised, or a shrink of the two on
# two uncalibrated constants. D-8 records the cost: Section C's verdict swung 11pp on the choice,
# and `declared_prior` could not read On-track under ANY compliant allocation.
#
# M* inverts the question. Instead of assuming a market return and asking whether the portfolio
# clears the anchor, it asks: **what would the market have to return for this portfolio to clear
# the anchor?** That is an OUTPUT, so no market assumption is required to compute it — which is
# why D-23 can be built today while O-1 (the per-region M_va formula) stays open.
#
#   E[r]_portfolio(M) = Σ_i w_i·(α_i + β_i·M)  +  w_cash·r_cash          (cash has no β)
#                     = intercept + λ·M            where  λ = Σ_i w_i·β_i
#   M* = (anchor − intercept) / λ
#
# λ IS THE LEVERAGE D-12 ASKS TO BE PRINTED NEXT TO M. It is derived from measured betas and
# actual weights every run — never a constant. D-8's datapoint (M* = 11.91% at a 14.1% anchor)
# implies λ ≈ 0.845, and D-12's "0.85× levered to M" is the same number stated the other way.
#
# ⚑ THE ASSUMPTION THAT IS DECLARED, NOT BURIED (and it is O-1's category error, named)
# Each fund's β is measured against ITS OWN benchmark — S&P 500, UK All-Share, Asia ex-Japan,
# Japan. Summing w_i·β_i into a single λ and inverting for a single M is only meaningful under
# `IMPLIED_M_ASSUMPTION = "uniform_benchmark_return"`: every benchmark returns the same M. That
# is a real assumption and it is exactly what O-1 (ISA-0160) exists to replace with per-region
# M_va. It is stated on the output of every call so no reader can mistake it for a measurement.
# ⚑ It is also why M* is reported as a REQUIRED rate and not as a verdict: see below.
#
# ⚑ NO VERDICT IS ISSUED, AND THAT IS THE CORRECT ANSWER TODAY (R4.8)
# Turning M* into On-track/Watch/Flag needs a declared plausibility band for long-run equity
# returns — a judgement Raj owns and has not made. Guessing it here would persist an invented
# preference that is then reused unconditionally and becomes invisible. M* is published as a
# number, with λ beside it and the full sensitivity grid, and the band is a named open item.
# ═══════════════════════════════════════════════════════════════════════════════════════════

IMPLIED_M_ASSUMPTION = "uniform_benchmark_return"   # O-1 / ISA-0160 replaces this with M_va
BETA_STUDY_PATH = os.path.join(HERE, "beta_alpha_study_aug2026.json")

# ⚑ DECLARED, not measured (R13.1 basis = DECLARED). `beta_alpha_study` covers the twelve FUNDS
# only; no β has ever been measured for a single stock in this framework. 1.0 is the neutral
# declaration and it UNDERSTATES λ for a MU/AVGO-style sleeve, which RAISES M* — i.e. it errs
# toward requiring more of the market, which is the safe direction for a capital-gating hurdle.
# Falsified by: a measured single-stock β. Revisit: 2026-11-30 (with the O-2 stock-E[r] work).
STOCK_BETA_DECLARED = 1.0
STOCK_BETA_SENSITIVITY = (1.0, 1.3, 1.6)
# α: the intercept. "zero" credits no manager skill in a 12-year structural projection. The
# measured alternative is published beside it and NEVER blended (R6.2) — D-9 is the standing
# record of what happens when an alpha estimate is quietly turned into a decision.
IMPLIED_M_ALPHA_MODES = ("zero", "measured")
# ⚑ HEADLINE = "measured", AND THE CHOICE IS EVIDENCED, NOT PREFERRED (R4.8/R5.2).
# The first build set this to "zero" on the reasoning that a 12-year structural projection should
# credit no manager skill. Running both modes against D-8's own published datapoint settled it:
#   D-8 states M* = 11.91% at a 14.1% anchor.
#   measured-α reproduces 12.01% at a 14.1% anchor on the 30-Jun-2026 weights — 0.10pp out.
#   zero-α     produces    15.65% at the same anchor — 3.74pp out, i.e. a different quantity.
# So D-8's figure IS the measured-α basis, and a "zero" headline would have silently redefined
# the number the decision register publishes. **D-9 is the same lesson from the other side:**
# zeroing alpha because a t-statistic was small cost 2.18pp and is on the register as an error.
# `zero` remains in the grid as the sensitivity, never blended (R6.2).
IMPLIED_M_ALPHA_MODE_HEADLINE = "measured"
# D-8's published figure, held as the reconciliation target rather than as prose (R5.2).
# ── ISA-0310. The plausibility band for M*. Declared 13-Aug-2026; ONE HOME, in its own module.
import mstar_plausibility as MPB

# The horizon M* is solved over. Same target and end date the anchor is solved on in
# `derive_required_return.solve_required_annual_pct(1_000_000, ..., 2037-12-31, ...)`.
# ⚑ A percentile is a statement about a HORIZON. Reading M* against a distribution built on a
# different horizon would be the FC-B defect: plausible, and wrong.
TARGET_END_DATE = "2037-12-31"


def _horizon_years(as_of):
    """Years from `as_of` to the target date. Never defaulted — a missing date REFUSES."""
    if not as_of:
        return None
    a = dt.date.fromisoformat(str(as_of)[:10])
    e = dt.date.fromisoformat(TARGET_END_DATE)
    return (e - a).days / 365.25 if e > a else None


D8_PUBLISHED_M_STAR_PCT = 11.91
D8_PUBLISHED_AT_ANCHOR_PCT = 14.1
D8_RECONCILIATION_TOL_PP = 0.35

# ⚑⚑ ISA-0378 (19-Aug-2026). THE D-8 RECONCILIATION IS RETIRED WITH CAUSE, and the cause is that
# it stopped being INDEPENDENT — not that it stopped agreeing.
#
# It pinned two of its three inputs: the anchor (14.1%) and the weights (portfolio_data_jul_2026).
# It did not pin the ALPHAS. `beta_alpha_study_aug2026.json` is stamped 2026-08-13T10:59:58 — the
# mandate-benchmark build that closed ISA-0322 (Polar benchmarked against a US-only S&P sector ETF
# labelled "MSCI World Info Tech") and ISA-0323 (4 of 27 benchmark series defective, two silently
# missing distributions). D-8's 11.91% therefore INHERITS those defects, and a check that compares
# corrected alphas against a figure built from a defective mapping is not measuring drift, it is
# measuring the correction.
#
# It cannot be restored: re-running the reconciliation against the pre-correction store preserved
# in `_bak_benchmark_20260813/` returns INSUFFICIENT_COVERAGE in both alpha modes, because that
# study could not compute M* at all until Polar had a NAV series (ISA-0307). The route that
# produced 11.91% no longer exists in the code or on disk.
#
# ⚑ WHAT IS **NOT** DONE HERE. The tolerance is NOT widened, and 11.91% is NOT restated to
# whatever the code currently prints — that would be fitting the target to the output and would
# delete the invariant while appearing to keep it. The divergence stays PUBLISHED, with its cause,
# on every run (R6.2: publish the disagreement).
#
# WHAT REPLACES IT: a GOLDEN FIXTURE, which is the process rule the engineering standard already
# states. The fixture freezes the INPUTS' IDENTITY as well as the output, so the next time any
# input changes the battery reports a DIFF naming the input — rather than a permanently red
# assertion nobody can act on. That is the generalisable form; D-8's figure never had it.
D8_RECONCILIATION_BASIS = "RETIRED_WITH_CAUSE"
D8_RETIREMENT_CAUSE = (
    "The alphas were corrected by ISA-0322 / ISA-0323 on 13-Aug-2026, after D-8's figure was "
    "published. The reconciliation's two derivations are no longer independent of each other's "
    "defects, and the pre-correction inputs cannot be reproduced (INSUFFICIENT_COVERAGE from the "
    "13-Aug backup). Superseded by the M* golden fixture; see ISA-0378.")

MSTAR_GOLDEN_FIXTURE_FILE = "mstar_golden_fixture.json"
MSTAR_GOLDEN_TOL_PP = 0.35          # unchanged from D8_RECONCILIATION_TOL_PP, deliberately


class ImpliedMError(RuntimeError):
    """Raised where M* cannot be computed honestly. Never defaulted to a plausible number."""


def fund_betas(path=None, as_of=None):
    """Measured β and α per fund, with the point-in-time status stamped (R6.4).

    ⚑ A β measured over a window ENDING AFTER `as_of` is not point-in-time at `as_of`. For the
    D-23 retrospective at 30-Jun-2026 every β on file was measured to 2026-08, so every row is
    stamped `backfilled_not_pit` and is inadmissible as EVIDENCE — which is precisely D-23's
    "100% method, 0% data". Never deleted, never silently promoted: relabelled.
    """
    path = path or BETA_STUDY_PATH
    if not os.path.exists(path):
        return None, {"status": "MISSING", "path": os.path.basename(path),
                      "note": "no beta study on disk — M* cannot be computed (R4.3: BLOCKS)"}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    rows, skipped = {}, []
    for k, v in (doc.get("funds") or {}).items():
        sf = v.get("single_factor") or {}
        b, a = sf.get("beta"), sf.get("alpha_ann_pct")
        if b is None:
            skipped.append({"asset_id": k, "why": "no single_factor.beta"})
            continue
        win = v.get("window") or [None, None]
        pit = True
        if as_of and win[1]:
            pit = str(win[1]) <= str(as_of)[:7]
        rows[k] = {"beta": float(b), "alpha_ann_pct": (None if a is None else float(a)),
                   "alpha_t": sf.get("alpha_t"), "r_squared": sf.get("r_squared"),
                   "benchmark": v.get("benchmark"), "window": win,
                   "n_months": sf.get("n_months"),
                   "stamp_basis": "point_in_time" if pit else "backfilled_not_pit"}
    n_pit = sum(1 for r in rows.values() if r["stamp_basis"] == "point_in_time")
    return rows, {"status": "OK", "n": len(rows), "n_point_in_time": n_pit,
                  "n_backfilled_not_pit": len(rows) - n_pit, "skipped": skipped,
                  "generated_at": doc.get("generated_at"), "method": doc.get("method"),
                  "source": os.path.basename(path),
                  "admissibility": ("rows stamped backfilled_not_pit are inadmissible as evidence "
                                    "(R6.4); they are used here because D-23 is explicitly a "
                                    "method exercise, and that is stated on the result")}


def market_sensitivity(inputs, betas, stock_beta=None, alpha_mode="zero"):
    """λ = Σ w_i·β_i and the intercept, over ACTUAL weights. Cash carries β = 0, not β = missing.

    ⚑ R4.1. A fund with no measured β is NOT given one. It is counted as uncovered, and if
    uncovered weight breaks the coverage floor M* is refused outright rather than computed on a
    renormalised subset that quietly excludes the holdings nobody could measure.
    """
    sb = STOCK_BETA_DECLARED if stock_beta is None else float(stock_beta)
    if alpha_mode not in IMPLIED_M_ALPHA_MODES:
        raise ImpliedMError(f"alpha_mode {alpha_mode!r} not in {IMPLIED_M_ALPHA_MODES}")
    rows, uncovered = [], []
    lam = intercept = w_cov = w_all = 0.0
    for i in inputs:
        w = float(i.get("weight") or 0.0)
        w_all += w
        kind = i["kind"]
        if kind == "cash":
            er = (i.get("er_by_basis") or {}).get("declared_prior")
            if er is None:
                uncovered.append({"asset_id": i["asset_id"], "kind": kind, "weight": w,
                                  "why": "the money-market rate is undeclared (return_inputs.json)"})
                continue
            rows.append({"asset_id": i["asset_id"], "name": i["name"], "kind": kind, "weight": w,
                         "beta": 0.0, "beta_source": "cash is not market-sensitive by construction",
                         "alpha_ann_pct": float(er),
                         "alpha_source": "derived money-market rate (return_inputs / cash statement)"})
            intercept += w * float(er)
            w_cov += w
            continue
        if kind == "stock":
            rows.append({"asset_id": i["asset_id"], "name": i["name"], "kind": kind, "weight": w,
                         "beta": sb, "beta_source": f"DECLARED {sb} (no single-stock β measured)",
                         "alpha_ann_pct": 0.0, "alpha_source": "declared zero"})
            lam += w * sb
            w_cov += w
            continue
        b = (betas or {}).get(i["asset_id"])
        if not b:
            uncovered.append({"asset_id": i["asset_id"], "kind": kind, "weight": w,
                              "why": "no measured β in the beta/alpha study"})
            continue
        al = 0.0 if alpha_mode == "zero" else float(b.get("alpha_ann_pct") or 0.0)
        rows.append({"asset_id": i["asset_id"], "name": i["name"], "kind": kind, "weight": w,
                     "beta": b["beta"], "beta_source": f"measured vs {b['benchmark']} "
                                                       f"({b['n_months']}m, {b['stamp_basis']})",
                     "alpha_ann_pct": al,
                     "alpha_source": ("declared zero" if alpha_mode == "zero" else
                                      f"measured α {b.get('alpha_ann_pct')} (t={b.get('alpha_t')})"),
                     "stamp_basis": b["stamp_basis"]})
        lam += w * b["beta"]
        intercept += w * al
        w_cov += w
    return {"lambda": _round(lam, 8), "intercept_pct": _round(intercept, 8),
            # ⚑ EXACT, UNROUNDED, for the I-RA-7 identity. The first run of this invariant fired
            # at 5.4e-08 against a 1e-9 tolerance because λ and the intercept were rounded to 8dp
            # before M* was solved from them — the invariant correctly refusing to call two
            # DIFFERENTLY ROUNDED numbers "agreeing". `_sleeve()` carries `_exact_num`/`_exact_w`
            # for exactly this reason and the note there says exactly this. Second occurrence of
            # one bug class in one module is a pattern, not a coincidence.
            "_exact_lambda": lam, "_exact_intercept": intercept, "_exact_covered_weight": w_cov,
            "covered_weight": _round(w_cov, 8), "total_weight": _round(w_all, 8),
            "coverage": _round(w_cov / w_all if w_all else None, 6),
            "alpha_mode": alpha_mode, "stock_beta": sb,
            "rows": rows, "uncovered": uncovered,
            "assumption": IMPLIED_M_ASSUMPTION,
            "note": ("λ is the leverage of Section C to the market return (D-12). It is derived "
                     "from measured betas and actual weights every run and is never a constant.")}


def implied_market_return(inputs, anchor_pct, betas, stock_beta=None, alpha_mode="zero",
                          horizon_years=None):
    """M* — the forward market return this allocation requires to reach the anchor.

    Emits `Missing(reason)` semantics rather than a number wherever it cannot be honest:
    λ = 0 (an all-cash portfolio requires an infinite market return, which is a refusal, not a
    figure), or coverage below the floor.
    """
    ms = market_sensitivity(inputs, betas, stock_beta, alpha_mode)
    lam, inter = ms["_exact_lambda"], ms["_exact_intercept"]
    cov = ms["coverage"]
    if cov is not None and cov < COVERAGE_FLOOR:
        return {**ms, "status": "INSUFFICIENT_COVERAGE", "m_star_pct": None,
                "note": (f"β/E[r] coverage {cov:.1%} is below the {COVERAGE_FLOOR:.0%} floor — "
                         f"an M* computed on a renormalised subset would be a claim about the "
                         f"holdings that could not be measured")}
    if not lam or abs(lam) < 1e-9:
        return {**ms, "status": "REFUSED_NO_MARKET_SENSITIVITY", "m_star_pct": None,
                "note": ("λ = 0: nothing in this portfolio is market-sensitive, so no market "
                         "return reaches the anchor. That is a refusal, not a large number")}
    a = float(anchor_pct)
    w = float(ms["_exact_covered_weight"] or 0.0)
    # ⚑ RENORMALISED OVER COVERED WEIGHT, because that is what Section C does. `_sleeve()`
    # computes Σ w_i·er_i / W_covered, so M* must solve (intercept + λ·M)/W_covered = anchor, NOT
    # intercept + λ·M = anchor. The two coincide only at 100% coverage — which the August run
    # happens to have, so the wrong form would have produced the right number today and the
    # wrong one the first month a holding went unmeasured.
    m_star = (a * w - inter) / lam
    # ── I-RA-7. TWO INDEPENDENT DERIVATIONS MUST AGREE (R5.2). ───────────────────────────
    # (1) M* from the closed form above.
    # (2) FORWARD SUBSTITUTION: rebuild Σ w_i·(α_i + β_i·M*) holding by holding, renormalise over
    #     covered weight, and require it to reproduce the ANCHOR. The closed form can be right
    #     while the row-level model is wrong — a mis-signed α, or a β attached to the wrong row,
    #     cancels in the aggregate λ and does NOT cancel here.
    recon = sum(r["weight"] * (r["alpha_ann_pct"] + r["beta"] * m_star) for r in ms["rows"]) / w
    gap = abs(recon - a)
    return {**ms, "status": "computed", "m_star_pct": _round(m_star, 4),
            "anchor_pct": _round(a, 4),
            "leverage_lambda": _round(lam, 6),
            "leverage_note": (f"Section C moves {lam:.4f}pp for every 1pp of market return "
                              f"(D-12). {1.0/lam:.4f}pp of M covers 1pp of anchor."),
            "pp_of_m_per_pp_of_anchor": _round(1.0 / lam, 6),
            "identity_check": {"m_star_pct": _round(m_star, 10),
                               "forward_substituted_section_c_pct": _round(recon, 10),
                               "anchor_pct": _round(a, 10),
                               "covered_weight": _round(w, 8),
                               "abs_gap_pp": _round(gap, 12), "tolerance_pp": 1e-9,
                               "holds": bool(gap < 1e-9),
                               "note": ("substituting M* back through every holding row must "
                                        "reproduce the anchor exactly; the closed form and the "
                                        "row-level model are independent paths")},
            "assumption_declared": {
                "assumption": IMPLIED_M_ASSUMPTION,
                "meaning": ("every holding's own benchmark is assumed to return the same M. Each "
                            "β on file is measured against a DIFFERENT benchmark (S&P 500, UK "
                            "All-Share, Asia ex-Japan, Japan), so a single λ is only meaningful "
                            "under this assumption"),
                "replaced_by": "O-1 / ISA-0160 — the per-region M_va formula",
                "why_acceptable_today": ("M* is an OUTPUT, not an assumed input, so no regional "
                                         "market forecast is required to compute it (D-23)")},
            **_mstar_verdict(m_star, horizon_years)}


def _mstar_verdict(m_star_pct, horizon_years):
    """ISA-0310. The verdict on M*, delegated in full to the declared band. CLOSED 13-Aug-2026.

    ⚑ WHY THE PERCENTILE AND NOT TWO PERCENTAGE BOUNDS. D-12 measures ~1.2pp of M between Flag
    and On-track, so the live range of M* is narrower than any plausibility band drawn in
    percentage terms — a two-bound band would have frozen the verdict at a constant. The
    percentile keeps the same declared judgement and restores the resolution.
    """
    if horizon_years is None:
        return {"verdict": None,
                "verdict_withheld_reason": (
                    "no horizon was supplied, and a percentile is a statement about a horizon. "
                    "This is a refusal, not a default (R4.3).")}
    p = MPB.assess(m_star_pct, horizon_years, m_star_basis=MPB.MSTAR_BASIS)
    return {"verdict": p.get("verdict"), "verdict_withheld_reason": None, "plausibility": p}


def implied_m_sensitivity(inputs, anchor_pct, betas):
    """The full grid, never blended (R6.2). Both declared-α and measured-α, all three stock βs."""
    rows = []
    for am in IMPLIED_M_ALPHA_MODES:
        for sb in STOCK_BETA_SENSITIVITY:
            r = implied_market_return(inputs, anchor_pct, betas, stock_beta=sb, alpha_mode=am)
            rows.append({"alpha_mode": am, "stock_beta": sb, "status": r["status"],
                         "lambda": r.get("lambda"), "intercept_pct": r.get("intercept_pct"),
                         "m_star_pct": r.get("m_star_pct"),
                         "headline": bool(am == IMPLIED_M_ALPHA_MODE_HEADLINE and
                                          sb == STOCK_BETA_DECLARED)})
    span = [r["m_star_pct"] for r in rows if r["m_star_pct"] is not None]
    return {"rows": rows,
            "m_star_range_pct": ([min(span), max(span)] if span else None),
            "spread_pp": (_round(max(span) - min(span), 4) if span else None),
            "note": ("published as a grid because the two α modes answer different questions and "
                     "a blended figure would conceal which one moved the number (R6.2)")}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# D-23 · ISA-0158 — RETROSPECTIVE SECTION C AT 30-JUN-2026, ON THE IMPLIED-M* BASIS ONLY
#
# ⚑ WHY 30-JUN-2026 AND NOT ANY OTHER DATE. The anchor stored on 12-Jul-2026 (13.9 / 18.7) was
# solved on the 30-Jun-2026 portfolio value of £144,342.19. Every Section C ever published used
# an anchor derived on one date against weights measured on another. At 30-Jun-2026 the anchor and
# the weights are the same date for the first time, which is the whole content of D-23.
#
# ⚑ WHAT IS AND IS NOT PUBLISHED, AND WHY
# PUBLISHED: M*, λ, the sensitivity grid, and the per-holding β/α table. All of it is derived from
#            the 30-Jun weights and the anchor derived on that date.
# NOT PUBLISHED: the three legacy bases. `declared_prior` reads a policy file that is current, not
#            as-at-June; `realised` reads FRS windows computed in August. Reproducing them at
#            30-Jun would mean stamping August inputs with a June date, which is the FC-B defect
#            this framework has spent two months removing. D-23 says "implied-M* basis only", and
#            this is the reason that instruction is correct rather than merely convenient.
# ═══════════════════════════════════════════════════════════════════════════════════════════

RETROSPECTIVE_SECTION_C_DATE = "2026-06-30"


def _anchor_as_derived_on(as_of, state=None, expect_value_gbp=None):
    """The anchor as it was DERIVED, not as it stands today (R6.4 — point-in-time or labelled).

    Reads `target_state.derivation_history` for the row whose derivation used the valuation at
    `as_of`. Refuses rather than substituting today's anchor, which is the whole defect D-23 names.
    """
    state = state or json.load(open(os.path.join(HERE, "target_state.json"), encoding="utf-8"))
    want = str(as_of)[:10]
    cands = []
    for h in state.get("derivation_history") or []:
        # the row is identified by the valuation DATE it solved on where recorded, else by the
        # valuation VALUE matching the month-end portfolio value
        if str(h.get("portfolio_value_date", ""))[:10] == want:
            cands.append(("value_date", h))
    if not cands and expect_value_gbp is not None:
        # ⚑ The first cut took cands[0] from every row that merely HAD a value, so it would have
        # silently returned whichever derivation happened to sit first in the file. The fallback
        # must MATCH THE VALUATION to the penny, and an ambiguous match is refused, not picked.
        for h in state.get("derivation_history") or []:
            v = h.get("portfolio_value_gbp")
            if v is not None and abs(float(v) - float(expect_value_gbp)) < 0.51:
                cands.append(("valuation_value_match", h))
    if not cands:
        raise ImpliedMError(f"no derivation_history row identifiable at {want} (expected valuation "
                            f"{expect_value_gbp}) — refusing to use today's anchor for a "
                            f"retrospective, which IS the D-23 defect")
    if len({(c[1].get("derived_at"), c[1].get("operative_pct") or c[1].get("floor_pct"))
            for c in cands}) > 1:
        raise ImpliedMError(
            f"{len(cands)} derivation_history rows match {want} with DIFFERENT anchors: "
            f"{[(c[1].get('derived_at'), c[1].get('floor_pct')) for c in cands]} — refusing to "
            f"choose (R4.8)")
    how, row = cands[0]
    return {"floor_pct": row.get("floor_pct", row.get("operative_floor_pct")),
            "operative_pct": row.get("operative_pct"),
            "stretch_pct": row.get("stretch_pct"),
            "derived_at": row.get("derived_at"),
            "portfolio_value_gbp": row.get("portfolio_value_gbp"),
            "identified_by": how,
            "source": "target_state.derivation_history",
            "stamp_basis": "point_in_time",
            "note": ("the anchor AS DERIVED on this valuation, not the anchor as it stands today")}


def _beta_store_identity(base_dir=None):
    """The alpha set's IDENTITY, not its contents. A fixture that pins an output without pinning
    the identity of every input is what ISA-0378 was: it detects nothing until it detects
    everything. `generated_at` is what changed on 13-Aug-2026; the digest catches an edit that
    forgets to move it."""
    import hashlib
    p = BETA_STUDY_PATH if base_dir is None else os.path.join(base_dir, "beta_alpha_study_aug2026.json")
    if not os.path.exists(p):
        return {"path": os.path.basename(p), "status": "ABSENT",
                "generated_at": None, "sha256_12": None}
    raw = open(p, "rb").read()
    try:
        _d = json.loads(raw.decode("utf-8"))
        # `generated_at` sits at the TOP LEVEL of beta_alpha_study_aug2026.json, not under
        # `_meta` — reading the wrong key would have made the identity check silently blind to
        # the very field that moved on 13-Aug-2026 (ISA-0378).
        gen = _d.get("generated_at") or (_d.get("_meta") or {}).get("generated_at")
    except Exception:                                                       # noqa: BLE001
        gen = None
    return {"path": os.path.basename(p), "status": "PRESENT", "generated_at": gen,
            "sha256_12": hashlib.sha256(raw).hexdigest()[:12]}


def _golden_path(base_dir=None):
    return os.path.join(base_dir or HERE, MSTAR_GOLDEN_FIXTURE_FILE)


def mstar_golden_freeze(inputs, betas, *, portfolio_file, anchor_pct=None, base_dir=None,
                        frozen_on=None, note=None) -> dict:
    """Freeze the CURRENT M* computation — inputs' identity and output together — as the fixture
    the battery asserts against from now on (ISA-0378).

    ⚑ This is NOT a claim that today's M* is correct. It is a claim that the computation is
    STABLE, and that any future move in it has a named cause. The two are different assertions and
    conflating them is exactly how the D-8 check died.
    """
    a = D8_PUBLISHED_AT_ANCHOR_PCT if anchor_pct is None else anchor_pct
    modes = {}
    for am in IMPLIED_M_ALPHA_MODES:
        r = implied_market_return(inputs, a, betas, alpha_mode=am)
        modes[am] = {"m_star_pct": r.get("m_star_pct"), "status": r["status"]}
    return {
        "frozen_on": frozen_on or "UNSET",
        "anchor_pct": a,
        "portfolio_file": portfolio_file,
        "beta_store": _beta_store_identity(base_dir),
        "headline_mode": IMPLIED_M_ALPHA_MODE_HEADLINE,
        "by_alpha_mode": modes,
        "tolerance_pp": MSTAR_GOLDEN_TOL_PP,
        "supersedes": "D-8 reconciliation (ISA-0378)",
        "note": note or ("Frozen inputs AND output. A change in any of them must show up here as a "
                         "DIFF that names the input, not as a permanently red assertion."),
    }


def mstar_golden_check(inputs, betas, *, portfolio_file=None, base_dir=None) -> dict:
    """Assert the frozen fixture. Returns {status, holds, diffs, ...}.

    status ABSENT is NOT a pass and NOT a failure — it is 'no fixture has been frozen', which the
    caller must handle explicitly. A missing fixture silently reading as green would reproduce the
    class this whole item is about.
    """
    p = _golden_path(base_dir)
    if not os.path.exists(p):
        return {"status": "ABSENT", "holds": None, "diffs": [],
                "detail": f"no {MSTAR_GOLDEN_FIXTURE_FILE} on disk; freeze one with "
                          f"mstar_golden_freeze()"}
    fx = json.loads(open(p, encoding="utf-8").read())
    diffs = []
    live_store = _beta_store_identity(base_dir)
    for k in ("generated_at", "sha256_12"):
        if (fx.get("beta_store") or {}).get(k) != live_store.get(k):
            diffs.append(f"beta_store.{k}: frozen {(fx.get('beta_store') or {}).get(k)!r} "
                         f"vs live {live_store.get(k)!r}")
    if portfolio_file is not None and fx.get("portfolio_file") != portfolio_file:
        diffs.append(f"portfolio_file: frozen {fx.get('portfolio_file')!r} vs live {portfolio_file!r}")
    live = {}
    for am in IMPLIED_M_ALPHA_MODES:
        r = implied_market_return(inputs, fx.get("anchor_pct", D8_PUBLISHED_AT_ANCHOR_PCT),
                                  betas, alpha_mode=am)
        live[am] = {"m_star_pct": r.get("m_star_pct"), "status": r["status"]}
        want = ((fx.get("by_alpha_mode") or {}).get(am) or {}).get("m_star_pct")
        got = r.get("m_star_pct")
        tol = fx.get("tolerance_pp", MSTAR_GOLDEN_TOL_PP)
        if want is None or got is None:
            if want != got:
                diffs.append(f"{am}: frozen {want} vs live {got}")
        elif abs(got - want) > tol:
            diffs.append(f"{am}: frozen {want}% vs live {got}% ({got - want:+.4f}pp, tol {tol}pp)")
    return {"status": "CHECKED", "holds": not diffs, "diffs": diffs,
            "frozen_on": fx.get("frozen_on"), "anchor_pct": fx.get("anchor_pct"),
            "frozen": fx.get("by_alpha_mode"), "live": live,
            "beta_store_frozen": fx.get("beta_store"), "beta_store_live": live_store,
            "detail": ("the M* computation reproduces its frozen fixture" if not diffs else
                       "the M* computation has MOVED and every moved input is named: "
                       + "; ".join(diffs))}


def d8_reconciliation(inputs, betas):
    """⚑ TWO INDEPENDENT DERIVATIONS OF THE SAME PUBLISHED NUMBER (R5.2).

    D-8 states M* = 11.91% at a 14.1% anchor. That figure was arrived at in a conversation; this
    is code. If the two disagree, either the register is wrong or this module is, and both are
    worth knowing — so the disagreement is PUBLISHED rather than reconciled away (R6.2/D-20).
    """
    out = {}
    for am in IMPLIED_M_ALPHA_MODES:
        r = implied_market_return(inputs, D8_PUBLISHED_AT_ANCHOR_PCT, betas, alpha_mode=am)
        ms = r.get("m_star_pct")
        out[am] = {"m_star_pct": ms, "status": r["status"],
                   "delta_vs_d8_pp": (None if ms is None else _round(ms - D8_PUBLISHED_M_STAR_PCT, 4)),
                   "agrees": bool(ms is not None and
                                  abs(ms - D8_PUBLISHED_M_STAR_PCT) <= D8_RECONCILIATION_TOL_PP)}
    hit = [k for k, v in out.items() if v["agrees"]]
    return {"d8_published_m_star_pct": D8_PUBLISHED_M_STAR_PCT,
            "d8_published_at_anchor_pct": D8_PUBLISHED_AT_ANCHOR_PCT,
            "tolerance_pp": D8_RECONCILIATION_TOL_PP,
            "by_alpha_mode": out, "reproduced_by": hit,
            "headline_mode": IMPLIED_M_ALPHA_MODE_HEADLINE,
            # ⚑ ISA-0378. `holds` is still COMPUTED and still PUBLISHED — the divergence is a fact
            # about the framework and hiding it would be worse than the red suite was. It is no
            # longer ASSERTED: see basis/cause. The invariant it used to carry now sits on the
            # golden fixture (mstar_golden_check), which pins the inputs D-8's figure never did.
            "basis": D8_RECONCILIATION_BASIS,
            "cause": D8_RETIREMENT_CAUSE,
            "beta_store_now": _beta_store_identity(),
            "asserted": False,
            "holds": bool(IMPLIED_M_ALPHA_MODE_HEADLINE in hit),
            "note": ("this is what SELECTED the headline α mode in Aug-2026, and it is kept for "
                     "that provenance. It is no longer an assertion: ISA-0378 established that "
                     "the two derivations stopped being independent when ISA-0322/0323 corrected "
                     "the benchmark mapping D-8's figure was built on, and that the "
                     "pre-correction inputs cannot be reproduced. The DIVERGENCE is published "
                     "here every run; what the battery asserts is mstar_golden_check()")}


import datetime as _dt


def _implied_m_block(inputs, anchor_pct, as_of=None):
    """M*, λ and the sensitivity grid for the live run. Absent betas BLOCK; they never default."""
    betas, bmeta = fund_betas(as_of=as_of)
    if betas is None:
        return {"status": "BLOCKED", "m_star_pct": None, "beta_source": bmeta,
                "note": ("no measured betas on disk, so λ cannot be derived and M* is refused. "
                         "A leverage of 1.0 would be a plausible number and a false one (R4.3)")}
    m = implied_market_return(inputs, anchor_pct, betas,
                             alpha_mode=IMPLIED_M_ALPHA_MODE_HEADLINE,
                             horizon_years=_horizon_years(as_of))

    # ── ISA-0306. CAPTURE IS A PROPERTY OF PRODUCING THE ARTEFACT (R4.11). ───────────────────
    # M* is the first falsifiable forward statement the framework produces. Recording it in prose
    # would make the series depend on someone remembering (R14.1), so the emitter writes it. A
    # month already on file is NOT overwritten - a prediction is written once (R6.4).
    _capture = {"state": "SKIPPED", "reason": "M* not solved"}
    if m.get("m_star_pct") is not None:
        _stamp = (as_of.isoformat() if hasattr(as_of, "isoformat")
                  else (as_of or _dt.date.today().isoformat()))
        try:
            import forward_record as _fr
            _fr.record_m_star(as_of=str(_stamp), m_star_pct=m.get("m_star_pct"),
                              leverage_lambda=m.get("leverage_lambda"),
                              intercept_pct=m.get("intercept_pct"),
                              coverage_pct=(m.get("coverage") or {}).get("pct")
                              if isinstance(m.get("coverage"), dict) else m.get("coverage"),
                              anchor_pct=_round(anchor_pct, 4),
                              alpha_mode=IMPLIED_M_ALPHA_MODE_HEADLINE,
                              source="return_architecture._implied_m_block")
            _capture = {"state": "RECORDED", "as_of": str(_stamp)}
        except ValueError as _e:
            _capture = {"state": "ALREADY_ON_FILE", "as_of": str(_stamp), "note": str(_e)}
        except Exception as _e:                                       # noqa: BLE001
            # R2.10 - a failure to CAPTURE must never look like an absence of predictions.
            _capture = {"state": "CAPTURE_FAILED", "error": f"{type(_e).__name__}: {_e}"}

    return {"status": m["status"], "m_star_pct": m.get("m_star_pct"),
            "history_capture": _capture,
            "anchor_pct": _round(anchor_pct, 4),
            "leverage_lambda": m.get("leverage_lambda"),
            "leverage_note": m.get("leverage_note"),
            "pp_of_m_per_pp_of_anchor": m.get("pp_of_m_per_pp_of_anchor"),
            "intercept_pct": m.get("intercept_pct"), "coverage": m.get("coverage"),
            "uncovered": m.get("uncovered"), "alpha_mode": IMPLIED_M_ALPHA_MODE_HEADLINE,
            "assumption_declared": m.get("assumption_declared"),
            "identity_check": m.get("identity_check"),
            "verdict": m.get("verdict"),
            "verdict_withheld_reason": m.get("verdict_withheld_reason"),
            "plausibility": m.get("plausibility"),
            "horizon_years": _horizon_years(as_of),
            "beta_source": bmeta, "rows": m.get("rows"),
            "sensitivity": implied_m_sensitivity(inputs, anchor_pct, betas),
            "d8_reconciliation": d8_reconciliation(inputs, betas)}


def retrospective_section_c(as_of=None, portfolio=None, tw=None, metrics=None, state=None,
                            betas=None, out_path=None):
    """D-23. Section C at `as_of` on the implied-M* basis only."""
    as_of = str(as_of or RETROSPECTIVE_SECTION_C_DATE)[:10]
    if portfolio is None:
        raise ValueError("portfolio_data as at the retrospective date is required — the weights "
                         "are the whole point")
    pmeta = (portfolio.get("_meta") or {})
    if str(pmeta.get("data_date") and _dnorm(pmeta["data_date"])) != as_of:
        raise ImpliedMError(
            f"portfolio_data is stamped {pmeta.get('data_date')!r}, not {as_of} — a retrospective "
            f"computed on the wrong month's weights is worse than none (R4.2)")
    tw = tw if tw is not None else json.load(
        open(os.path.join(HERE, "target_weights.json"), encoding="utf-8"))
    anchor = _anchor_as_derived_on(
        as_of, state, expect_value_gbp=(portfolio.get("summary") or {}).get("total_value_gbp"))
    a_pct = float(anchor["operative_pct"])
    total_value = float((portfolio.get("summary") or {}).get("total_value_gbp") or 0.0)

    # Weights and the cash rate are all M* needs. FRS rows are NOT read: nothing here depends on
    # a declared prior, which is exactly why this basis is computable at a past date.
    #
    # ⚑ THE IDENTIFIER IS `ticker`, NOT `sedol`. `portfolio_data.funds[]` carries the SEDOL for an
    # OEIC and the exchange ticker for an ETF, both under the key `ticker`; `target_weights.funds`
    # and `fund_retention_score[].sedol` hold those same values. Reading `.get("sedol")` here
    # returned None for all twelve funds, collapsed the weight lookup onto a single holding, and
    # produced twelve identical weights summing to 0.713. **I-D23-1 caught it on the first run.**
    # A rule would not have found this; an invariant did — which is the whole of R5.2.
    frs_like = [{"sedol": f.get("ticker"), "name": f.get("name"),
                 "value_gbp": f.get("value_gbp"), "band": None}
                for f in (portfolio.get("funds") or [])]
    no_id = [f.get("name") for f in (portfolio.get("funds") or []) if not f.get("ticker")]
    if no_id:
        raise ImpliedMError(f"{len(no_id)} fund row(s) carry no identifier: {no_id} — a holding "
                            f"that cannot be identified cannot be weighted (R4.9)")
    fi = fund_inputs(frs_like, tw, total_value)
    si, _zc = stock_inputs(portfolio.get("stocks") or [], (metrics or {}).get("tickers") or {},
                           total_value, a_pct)
    cash_declared = cash_input()
    ci = cash_inputs(portfolio, total_value, cash_declared)
    inputs = fi + si + [ci]

    if betas is None:
        betas, bmeta = fund_betas(as_of=as_of)
    else:
        bmeta = {"status": "OK", "n": len(betas), "source": "caller-supplied"}
    if betas is None:
        raise ImpliedMError(f"no measured betas available — M* BLOCKS rather than guessing: {bmeta}")

    mstar = implied_market_return(inputs, a_pct, betas, alpha_mode=IMPLIED_M_ALPHA_MODE_HEADLINE,
                                  horizon_years=_horizon_years(as_of))
    grid = implied_m_sensitivity(inputs, a_pct, betas)
    w_sum = sum(float(i.get("weight") or 0.0) for i in inputs)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "item": "ISA-0158 (D-23)",
        "as_of": as_of,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "basis": "implied_market_return_m_star",
        "basis_note": ("the three legacy E[r] bases are NOT reproduced at this date: their inputs "
                       "(policy priors, FRS trailing windows) are current, not as-at, and "
                       "stamping them with a June date would be the FC-B defect. D-23 specifies "
                       "the M* basis only, and this is why"),
        "anchor_as_derived": anchor,
        "portfolio": {"total_value_gbp": _round(total_value, 2),
                      "data_date": pmeta.get("data_date"),
                      "source_file": pmeta.get("source_file"),
                      "n_funds": len(portfolio.get("funds") or []),
                      "n_stocks": len(portfolio.get("stocks") or []),
                      "weights_sum": _round(w_sum, 10)},
        "beta_source": bmeta,
        "cash_rate_input": cash_declared,
        "implied_m_star": mstar,
        "sensitivity": grid,
        "d8_reconciliation": d8_reconciliation(inputs, betas),
        "mstar_golden": mstar_golden_check(inputs, betas),
        "consistency_with_anchor": {
            "anchor_derived_at": anchor.get("derived_at"),
            "anchor_solved_on_value_gbp": anchor.get("portfolio_value_gbp"),
            "retrospective_value_gbp": _round(total_value, 2),
            "agree": bool(anchor.get("portfolio_value_gbp") is not None and
                          abs(float(anchor["portfolio_value_gbp"]) - total_value) < 0.51),
            "note": ("D-23's premise: the anchor and the weights must be the same valuation. This "
                     "field is the assertion of that premise, not a description of it")},
        "invariants": [
            {"invariant": "I-D23-1", "holds": bool(abs(w_sum - 1.0) < 1e-6),
             "detail": f"every holding's weight sums to {w_sum:.10f} at {as_of}"},
            {"invariant": "I-D23-2",
             "holds": bool((mstar.get("identity_check") or {}).get("holds") is True),
             "detail": ("substituting M* back through every holding row reproduces the anchor "
                        f"(gap {(mstar.get('identity_check') or {}).get('abs_gap_pp')})")},
            {"invariant": "I-D23-3",
             "holds": bool(anchor.get("portfolio_value_gbp") is not None and
                           abs(float(anchor["portfolio_value_gbp"]) - total_value) < 0.51),
             "detail": ("the anchor read here was DERIVED on this same valuation — the one thing "
                        "D-23 exists to establish")},
            # ⚑ I-D23-5 RE-FOUNDED 19-Aug-2026 (ISA-0378). It used to assert that the headline
            #    α mode reproduced D-8's published 11.91%. That figure was computed before the
            #    ISA-0322/0323 benchmark corrections and cannot be reproduced from anything on
            #    disk, so the assertion had become a test of how much the framework had improved.
            #    It now asserts a GOLDEN FIXTURE that pins the inputs' identity as well as the
            #    output — so a moved input is reported as a named DIFF, not as a standing failure.
            #    An ABSENT fixture is NOT a pass: it fails, and says to freeze one.
            {"invariant": "I-D23-5",
             "holds": bool((mstar_golden_check(inputs, betas) or {}).get("holds") is True),
             "detail": (mstar_golden_check(inputs, betas) or {}).get("detail")},
            # ── I-D23-4 INVERTED 13-Aug-2026 (ISA-0310 CLOSED). It previously asserted that NO
            #    verdict existed, which was the correct assertion while the band was undeclared.
            #    It now asserts the opposite AND re-derives it independently, so a silently
            #    dropped band would fail the battery instead of quietly restoring "no verdict".
            {"invariant": "I-D23-4",
             "holds": bool(mstar.get("verdict") is not None and mstar.get("verdict") ==
                           MPB.assess(mstar.get("m_star_pct"), _horizon_years(as_of),
                                      run_monte_carlo=False).get("verdict")),
             "detail": ("the M* verdict is issued from the declared plausibility band "
                        f"(ISA-0310) and re-derives to {mstar.get('verdict')!r} at "
                        f"{(mstar.get('plausibility') or {}).get('percentile_label')}")},
            {"invariant": "I-D23-6",
             "holds": bool((((mstar.get("plausibility") or {}).get("identity_check")) or {})
                           .get("holds") is True),
             "detail": ("the percentile agrees across three independent paths: erf forward CDF, "
                        "Acklam inverse CDF round-trip, and a seeded Monte Carlo draw")},
        ],
        "learning_L0": {
            "learnable_now": True,
            "task": ("M* is a falsifiable forward statement. Recording M* each month against the "
                     "realised market return of the following 12 months builds the first data "
                     "series in this framework that can test whether the required rate was ever "
                     "attainable — 0 observations today, 1 per month from now"),
            "first_observation_due": "2027-06-30",
            "store": "recorded on the register item; no store exists yet",
        },
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    return doc


def _dnorm(s):
    """`30-Jun-2026` / `2026-06-30` -> `2026-06-30`. Raises rather than guessing."""
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ImpliedMError(f"unparseable date {s!r} — refusing to guess")


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
    cash_declared = cash_input()
    ci = cash_inputs(portfolio, total_value, cash_declared)
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
        # ⚑ D-12 asks for the leverage to be PRINTED NEXT TO M EVERY RUN. R14.2/R4.11: that makes
        # it a property of producing the artefact rather than a prose step, so it is emitted here
        # instead of being left to whoever writes the email. D-8's headline basis and D-12's
        # leverage are the same computation and there is one home for it.
        "implied_market_return": _implied_m_block(inputs, a_pct),
        "bucket_minimum_divergence": bucket_minimum_divergence(tw),
        "invariants": inv,
        # ⚑ ISA-0409 — the consumer READS this and never re-derives it (R4.4).
        "adoption_gate": adoption_gate(inv),
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

    # ── D-8 / D-12 / D-23: implied M* ────────────────────────────────────────────────
    # β=1 everywhere and zero α: λ must be 1.0 and M* must BE the anchor. If λ or the intercept
    # is mis-signed this is the assertion that fails, because the answer is known by construction.
    _in1 = [_mk("F1", "F1", "fund", 50.0, 100.0, {"declared_prior": 9.0}, []),
            _mk("F2", "F2", "fund", 50.0, 100.0, {"declared_prior": 9.0}, [])]
    _b1 = {"F1": {"beta": 1.0, "alpha_ann_pct": 0.0, "benchmark": "X", "n_months": 60,
                  "stamp_basis": "point_in_time"},
           "F2": {"beta": 1.0, "alpha_ann_pct": 0.0, "benchmark": "X", "n_months": 60,
                  "stamp_basis": "point_in_time"}}
    _m1 = implied_market_return(_in1, 13.9, _b1, alpha_mode="zero")
    ok(_m1["status"] == "computed" and abs(_m1["lambda"] - 1.0) < 1e-9
       and abs(_m1["m_star_pct"] - 13.9) < 1e-6,
       f"β=1, α=0 => λ=1 and M* must BE the anchor; got λ={_m1.get('lambda')} "
       f"M*={_m1.get('m_star_pct')}")
    ok(_m1["identity_check"]["holds"], "I-RA-7 must hold in the closed-form case")
    # β=0.5 everywhere => λ=0.5 and M* must be exactly twice the anchor. D-12's leverage, tested.
    for _k in _b1:
        _b1[_k] = dict(_b1[_k], beta=0.5)
    _m2 = implied_market_return(_in1, 13.9, _b1, alpha_mode="zero")
    ok(abs(_m2["m_star_pct"] - 27.8) < 1e-6 and abs(_m2["pp_of_m_per_pp_of_anchor"] - 2.0) < 1e-9,
       f"λ=0.5 must double the required market return; got {_m2.get('m_star_pct')}")
    # a positive measured α must LOWER M* — the market has less work to do
    for _k in _b1:
        _b1[_k] = dict(_b1[_k], beta=1.0, alpha_ann_pct=2.0)
    _m3 = implied_market_return(_in1, 13.9, _b1, alpha_mode="measured")
    ok(abs(_m3["m_star_pct"] - 11.9) < 1e-6, f"α=+2 must lower M* by 2pp; got {_m3['m_star_pct']}")
    ok(implied_market_return(_in1, 13.9, _b1, alpha_mode="zero")["m_star_pct"] > _m3["m_star_pct"],
       "the two α modes must differ — a headline that ignored α would publish a different number")
    # NEGATIVE CONTROL: λ = 0 is a REFUSAL, not a very large number and not a ZeroDivisionError
    _cash_only = [_mk("CASH", "Cash", "cash", 100.0, 100.0, {"declared_prior": 1.757}, [])]
    _m4 = implied_market_return(_cash_only, 13.9, {}, alpha_mode="zero")
    ok(_m4["status"] == "REFUSED_NO_MARKET_SENSITIVITY" and _m4["m_star_pct"] is None,
       f"an all-cash portfolio must REFUSE M*, not report one; got {_m4['status']}")
    # NEGATIVE CONTROL: an unmeasurable holding above the coverage floor must refuse
    _in5 = _in1 + [_mk("F3", "F3", "fund", 100.0, 200.0, {"declared_prior": 9.0}, [])]
    _m5 = implied_market_return(_in5, 13.9, _b1, alpha_mode="zero")
    ok(_m5["status"] == "INSUFFICIENT_COVERAGE" and _m5["m_star_pct"] is None,
       f"50% of weight with no β must refuse, not renormalise silently; got {_m5['status']}")
    # no verdict is ever issued, on any path (R4.8)
    ok(_m1["verdict"] is None and _m1["verdict_withheld_reason"],
       "M* must publish NO verdict and must say why")
    ok(_m1["assumption_declared"]["assumption"] == IMPLIED_M_ASSUMPTION and
       "ISA-0160" in _m1["assumption_declared"]["replaced_by"],
       "the uniform-benchmark assumption must travel on the result, naming O-1")
    # β stamped not-point-in-time for a past date, and point-in-time for a current one (R6.4)
    _bb, _bm = fund_betas(as_of="2026-06-30")
    if _bb:
        ok(_bm["n_backfilled_not_pit"] > 0,
           "a β measured to 2026-08 must be stamped backfilled_not_pit at a 30-Jun-2026 as_of")
        ok(fund_betas(as_of="2026-12-31")[1]["n_backfilled_not_pit"] == 0,
           "...and point_in_time at a later as_of — negative control on the stamp")

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

    # ── ISA-0409: the adoption gate ─────────────────────────────────────────────────
    ok(all(i.get("scope") for i in inv),
       "every invariant emitted by check_invariants declares the SCOPE it guards")
    ok(all((i["scope"] == ADOPTION_SCOPE) == i["withholds_sections"] for i in inv),
       "only ADOPTION_SCOPE invariants are marked as withholding Sections A/B/C")
    ok(set(i["invariant"] for i in inv) <= set(INVARIANT_SCOPE),
       "every invariant this module emits is classified in INVARIANT_SCOPE")
    # ⚑ The gate is tested on a CONSTRUCTED invariant list, not on the fixture's own: the synthetic
    # fixture legitimately fails I-RA-5..8 (no target_state, no betas on a 100-unit toy book), and
    # a control must perturb exactly the thing it claims to test and nothing else.
    inv = [{"invariant": c, "holds": True, "detail": "fixture",
            "scope": INVARIANT_SCOPE[c],
            "withholds_sections": INVARIANT_SCOPE[c] == ADOPTION_SCOPE}
           for c in INVARIANT_SCOPE]
    g = adoption_gate(inv)
    ok(g["adoptable"] is True and not g["blocking_invariants"],
       "with every invariant holding, Sections A/B/C are adoptable")
    # an out-of-scope failure must NOT withhold the sections
    inv_m = [dict(i) for i in inv]
    for i in inv_m:
        if i["invariant"] == "I-RA-8":
            i["holds"] = False
    g_m = adoption_gate(inv_m)
    ok(g_m["adoptable"] is True and "I-RA-8" in (g_m["failing_out_of_scope"] or {}),
       "ISA-0409 CONTROL: an M*-scoped failure (I-RA-8 / ISA-0383) is REPORTED and does NOT "
       "withhold Sections A/B/C — an invariant withdraws the quantity it guards, and nothing else")
    inv_t = [dict(i) for i in inv]
    for i in inv_t:
        if i["invariant"] == "I-RA-5":
            i["holds"] = False
    ok(adoption_gate(inv_t)["adoptable"] is True,
       "ISA-0409 CONTROL: a stale frozen constant (I-RA-5) is REPORTED and does not withhold the "
       "sections — the module already declares the derived value operative, and stale prose cannot "
       "invalidate arithmetic that reconciles. The CHECK and its tolerance are unchanged.")
    # an in-scope failure MUST withhold, and the refusal must not name est_return as a substitute
    inv_s = [dict(i) for i in inv]
    for i in inv_s:
        if i["invariant"] == "I-RA-2":
            i["holds"] = False
    g_s = adoption_gate(inv_s)
    ok(g_s["adoptable"] is False and g_s["blocking_invariants"] == ["I-RA-2"],
       "ISA-0409 CONTROL: a sections-scoped failure DOES withhold Sections A/B/C")
    ok(g_s["on_refusal"] == ADOPTION_REFUSAL_BASIS
       and "est_return" in g_s["refusal_semantics"] and "NOT" in g_s["refusal_semantics"],
       "ISA-0409: the refusal names UNMEASURED as the outcome and states explicitly that "
       "est_return is NOT substituted (R2.10)")
    inv_u = [dict(i) for i in inv] + [{"invariant": "I-RA-99", "holds": True,
                                       "detail": "new", "scope": None,
                                       "withholds_sections": False}]
    ok(adoption_gate(inv_u)["adoptable"] is False
       and "I-RA-99" in adoption_gate(inv_u)["unscoped_invariants"],
       "ISA-0409 CONTROL: an invariant with NO declared scope BLOCKS — a new invariant nobody "
       "classified must not silently inherit 'does not matter' (R4.7)")

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
