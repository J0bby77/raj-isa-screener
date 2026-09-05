#!/usr/bin/env python3
"""
deployment_sequencer.py — P6. Correlation orders the deployment QUEUE. It never touches a score.

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md P6 (ISA-0464).
Built 28-Aug-2026.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑⚑ THE PROBLEM THIS EXISTS FOR: A GATE CANNOT MAKE A MULTI-NAME DECISION
═══════════════════════════════════════════════════════════════════════════════════════════
Measured on the delivered tree, two synthetic candidates 0.97 correlated **with each other**
and ~0.17 with the sleeve:

    CASE A  both absent from `returns_by_name` -> both measured=False -> adverse 0.70
            -> both REPLACEMENT_ONLY.  **The 0.97 is never measured at all.**
    CASE B  both present -> rho(C1,C2) = 0.970
            -> C1 rho_max 0.761 -> REPLACEMENT_ONLY
            -> C2 rho_max 0.761 -> REPLACEMENT_ONLY

`admission()`'s own docstring says *"you do not own both; you own the better one."*
**The mechanism owns NEITHER.** And whether the gate is candidate-aware at all depends on how
a caller populated one dict — an undeclared contract with two opposite failure modes: silently
buy both, or block both.

⚑ **A RANKED LIST IS ONLY A VALID DECISION OBJECT IF YOU TAKE FROM IT ONE NAME AT A TIME.**
The moment you take two, the marginal value of #2 depends on #1. The framework builds a list
as if you were taking one, and then takes N from it. Sequencing with **re-measurement after
every pick** is the only correct form.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHAT THIS MODULE MAY AND MAY NOT DO
═══════════════════════════════════════════════════════════════════════════════════════════
Correlation **CAPS** (at STARTER, when unmeasured), **GATES** (REPLACEMENT_ONLY) and
**ORDERS**. It does NOT size, and it does NOT enter the score.

⚑ **Why it may never enter the score:** a per-name forecast that changes because you bought
something *else* is not a forecast. It becomes non-comparable month to month, path-dependent
on your own trading, and it resurrects **A1's withdrawn `x d` multiplier** through the back
door. Enforced by AST in `consistency_check.pair_ranking_modules_correlation_free`.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑⚑ P6.3 THE NOISE GATE — and it is a self-critique, not a refinement
═══════════════════════════════════════════════════════════════════════════════════════════
The 26-Aug design applied *"divide the threshold by the SD"* to Raj's ratchet **and not to its
own sequencer**. SE(rho) at n = 104 is +/-0.0995. A sequencer that reorders on ANY rho
difference is resolving noise and calling it a decision.

**Reordering requires the rho_sleeve gap to exceed `RHO_REORDER_SE_MULTIPLE x SE(rho)`.**
Otherwise the band's declared order stands, and the fire rate is published (P0.3).

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["deployment_sequencer"] = False` ⇒ `sequence()` returns
strict `ranking_basis` order with `method: "DISABLED_DECLARED_ORDER"` — and `allocate()` says
so in §2 rather than implying a sequence happened.
"""
from __future__ import annotations

import csv
import datetime
import itertools
import math
import json
import os
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

# ⚑ DECLARED, registered in threshold_register.json, and its fire rate is published.
RHO_REORDER_SE_MULTIPLE = 1.0

# Exact enumeration is affordable below this; above it, greedy — and the method is STATED.
EXACT_ENUMERATION_CAP = 5000

SCORE_PANEL = os.path.join(HERE, "score_panel.csv")


class SequencerRefused(RuntimeError):
    """The sequencer cannot order this queue on measured grounds.

    ⚑ It REFUSES rather than ordering on an assumption — an order that looks measured and is
    not is worse than a declared fallback, because only one of the two can be challenged."""


def _flag(name: str = "deployment_sequencer", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


# ══════════════════════════════════════════════════════════════════════════════════════
# P6.1 — BAND, DO NOT RANK. The width is MEASURED.
# ══════════════════════════════════════════════════════════════════════════════════════
def measure_score_se(panel_path: Optional[str] = None) -> dict:
    """SE of a single `source_score` reading, from the run-to-run dispersion of the panel.

    ⚑ THIS IS THE QUANTITY THE BANDING NEEDS, and it is DERIVED, never typed. Two names whose
    scores differ by less than this are statistically indistinguishable, so ordering them
    finely is resolving noise. It corroborates Raj's own 04-Jul finding — momentum rank-IC
    0.019, t 0.58, *"fine rank order is low-signal"* — which was declared and never implemented.

    ⚑ AND IF IT CANNOT BE MEASURED THE SEQUENCER REFUSES TO BAND (P6.1). Banding on an assumed
    width would let an UNMEASURED quantity override a MEASURED one, which is the wrong way
    round."""
    p = panel_path or SCORE_PANEL
    if not os.path.exists(p):
        return {"measured": False, "se": None, "n": 0,
                "reason": "score_panel.csv is absent — the SE of source_score is UNMEASURED"}
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as exc:                                            # noqa: BLE001
        return {"measured": False, "se": None, "n": 0,
                "reason": "score_panel.csv unreadable: %s" % str(exc)[:120]}
    by: Dict[str, List[Tuple[str, float]]] = {}
    for r in rows:
        try:
            v = float(r.get("source_score"))
        except (TypeError, ValueError):
            continue
        by.setdefault(r.get("ticker") or "", []).append((r.get("run_date") or "", v))
    deltas = []
    for series in by.values():
        series.sort()
        for a, b in zip(series, series[1:]):
            if a[0] != b[0]:
                deltas.append(b[1] - a[1])
    if len(deltas) < 30:
        return {"measured": False, "se": None, "n": len(deltas),
                "reason": ("only %d run-to-run observations — too few to measure the SE of a "
                           "reading. A width fitted to 30 points would be a width fitted to "
                           "the sample." % len(deltas))}
    sd_delta = statistics.pstdev(deltas)
    se = sd_delta / (2 ** 0.5)          # SD of a DIFFERENCE of two readings -> SE of one
    return {"measured": True, "se": round(se, 4), "sd_of_change": round(sd_delta, 4),
            "n": len(deltas), "n_tickers": sum(1 for v in by.values() if len(v) >= 2),
            "basis": ("SD of the run-to-run change in source_score across %d observations on "
                      "%d tickers, divided by sqrt(2) because a change is the difference of "
                      "two readings." % (len(deltas), sum(1 for v in by.values() if len(v) >= 2)))}


def measure_rho_se(pairs: Dict[str, object], *, min_weeks: int = 30) -> dict:
    """SE of a measured pairwise correlation — the second half of the P6.3 noise gate.

    ⚑ ISA-0601. `measure_score_se` existed and `se_rho` did not, so `sequence()` was called with
    `se_rho=None` and its own noise gate took the `len(scored) < 2 or se_rho is None` branch on
    EVERY comparison: 29 suppressed reorders, 0 fired, correlation ordering nothing. A gate with
    one of its two thresholds permanently absent is not a conservative gate, it is an off one.

    MEASURED, not declared: Fisher's standard error (1 - rho^2) / sqrt(n - 3) is computed per
    pair from that pair's OWN measured `weeks`, and the median is returned. Reporting the median
    rather than the mean keeps one short series from setting the threshold for the whole book.
    """
    ses, used = [], 0
    for _k, v in (pairs or {}).items():
        rho = v.get("rho") if isinstance(v, dict) else v
        wk = v.get("weeks") if isinstance(v, dict) else None
        if rho is None or not wk or wk < min_weeks:
            continue
        ses.append((1.0 - float(rho) ** 2) / math.sqrt(float(wk) - 3.0))
        used += 1
    if not ses:
        return {"se": None, "n_pairs": 0,
                "reason": ("no pair carried both a rho and >= %d weeks, so SE(rho) is UNMEASURED "
                           "— the noise gate must say the declared order stands, not pretend to "
                           "a threshold (R2.10)" % min_weeks)}
    ses.sort()
    med = ses[len(ses) // 2] if len(ses) % 2 else 0.5 * (ses[len(ses) // 2 - 1] + ses[len(ses) // 2])
    return {"se": round(med, 6), "n_pairs": used,
            "min": round(ses[0], 6), "max": round(ses[-1], 6),
            "basis": ("median Fisher SE (1 - rho^2)/sqrt(n-3) across %d measured pairs, each on "
                      "its own week count" % used)}


def band(candidates: Sequence[dict], *, ranking_basis: str = "source_score",
         se: Optional[float] = None, panel_path: Optional[str] = None) -> dict:
    """Group candidates into statistically indistinguishable bands of width SE.

    ⚑ REFUSES TO BAND when the SE is unmeasurable, falls back to strict declared order, and
    SAYS SO — the fallback is visible in the artefact, never inferred."""
    m = {"measured": True, "se": se} if se is not None else measure_score_se(panel_path)
    ranked = sorted(candidates, key=lambda c: -(c.get(ranking_basis) or 0.0))
    if not m.get("measured"):
        return {"banded": False, "bands": [list(ranked)], "se": None,
                "method": "STRICT_DECLARED_ORDER",
                "reason": ("REFUSING TO BAND: %s. Falling back to strict %s order. A band "
                           "width nobody measured would let an assumed quantity override a "
                           "measured one." % (m.get("reason"), ranking_basis)),
                "se_measurement": m}
    width = float(m["se"])
    bands: List[List[dict]] = []
    for c in ranked:
        v = c.get(ranking_basis) or 0.0
        if bands and abs((bands[-1][0].get(ranking_basis) or 0.0) - v) <= width:
            bands[-1].append(c)
        else:
            bands.append([c])
    return {"banded": True, "bands": bands, "se": width, "method": "BANDED_ON_MEASURED_SE",
            "n_bands": len(bands),
            "reason": ("band width = the measured SE of one %s reading (%.3f pts). Names "
                       "inside one band are statistically indistinguishable, so correlation "
                       "— and nothing else — breaks the tie."
                       % (ranking_basis, width)),
            "se_measurement": m}


# ══════════════════════════════════════════════════════════════════════════════════════
# CORRELATION HELPERS — read, never re-derived into a score
# ══════════════════════════════════════════════════════════════════════════════════════
def _rho(a: str, b: str, matrix: Dict[str, float]) -> Optional[float]:
    if a == b:
        return 1.0
    for k in ("%s|%s" % (a, b), "%s|%s" % (b, a)):
        if k in matrix and matrix[k] is not None:
            v = matrix[k]
            # ⚑ ISA-0601: `correlation_engine` publishes {"rho": .., "weeks": ..} per pair while
            # every caller here wants the scalar. Accepting BOTH shapes at the one place that
            # reads a pair is the fix; the alternative was each caller unwrapping it, which is
            # how the wrapper-instead-of-pairs bug got in.
            if isinstance(v, dict):
                v = v.get("rho")
            return float(v) if v is not None else None
    return None


def rho_to_set(cand: str, held: Sequence[str], matrix: Dict[str, float],
               weights: Optional[Dict[str, float]] = None) -> dict:
    """rho of `cand` against the sleeve AS IT WILL THEN STAND (P6.4).

    ⚑ THE WHOLE POINT OF THE MODULE. Recomputing against the sleeve INCLUDING names admitted
    earlier in the same run is what makes the second pick a different decision from the first.
    Without it, two 0.97-correlated candidates each look fine against the old book."""
    if not held:
        return {"rho_sleeve": None, "rho_max_pairwise": None, "measured": False,
                "basis": "EMPTY_SET — nothing to correlate against yet"}
    pairs = [(h, _rho(cand, h, matrix)) for h in held]
    got = [(h, v) for h, v in pairs if v is not None]
    if not got:
        # A2.3's direction: unmeasured is ADVERSE, never optimistic.
        return {"rho_sleeve": None, "rho_max_pairwise": None, "measured": False,
                "basis": "UNMEASURED_ADVERSE_DEFAULT",
                "unmeasured_against": sorted(h for h, v in pairs if v is None)}
    if weights:
        tw = sum(abs(weights.get(h, 0.0)) for h, _ in got)
        rs = (sum(abs(weights.get(h, 0.0)) * v for h, v in got) / tw) if tw else None
    else:
        rs = sum(v for _, v in got) / len(got)
    mx = max(got, key=lambda x: x[1])
    return {"rho_sleeve": round(rs, 4) if rs is not None else None,
            "rho_max_pairwise": round(mx[1], 4), "rho_max_against": mx[0],
            "measured": True, "n_measured_pairs": len(got),
            "unmeasured_against": sorted(h for h, v in pairs if v is None),
            "basis": "MEASURED"}


def sigma_p(names: Sequence[str], weights: Dict[str, float], sigmas: Dict[str, float],
            matrix: Dict[str, float]) -> Optional[float]:
    """Portfolio sigma of a NAME SET. A missing rho defaults to 1.0 — the most ADVERSE value,
    so an unmeasured pair can never flatter the set that contains it."""
    ns = [n for n in names if sigmas.get(n)]
    if not ns:
        return None
    var = 0.0
    for a in ns:
        for b in ns:
            r = _rho(a, b, matrix)
            var += (weights.get(a, 1.0) * weights.get(b, 1.0)
                    * sigmas[a] * sigmas[b] * (1.0 if r is None else r))
    return var ** 0.5 if var > 0 else None


# ══════════════════════════════════════════════════════════════════════════════════════
# P6.2 / P6.5 — LEXICOGRAPHIC SELECTION, EXACT WHERE AFFORDABLE
# ══════════════════════════════════════════════════════════════════════════════════════
def _band_index(bands: List[List[dict]]) -> Dict[str, int]:
    return {c["ticker"]: i for i, b in enumerate(bands) for c in b}


def choose(qualifying: Sequence[dict], k: int, *, bands: List[List[dict]],
           held: Sequence[str], matrix: Dict[str, float], sigmas: Dict[str, float],
           weights: Optional[Dict[str, float]] = None) -> dict:
    """Pick k names. Lexicographic, in the framework's own idiom (ISA-0386 C1-C5, A7 P1-P5):

        1. MAXIMISE the number of names drawn from the highest bands — conviction ALWAYS
           dominates, and correlation never promotes a lower-band name over a higher one.
        2. THEN minimise the resulting sleeve sigma_p.

    ⚑ Correlation only ever breaks a tie between statistically indistinguishable ideas.

    ⚑ EXACT, NOT GREEDY, AT THIS SCALE (P6.5). At |Q| <= 10 and k <= 3 the exact answer costs
    nothing, which removes the greedy-suboptimality objection entirely — and the method used is
    STATED, because "we took a shortcut" and "we computed the answer" are different facts."""
    tickers = [c["ticker"] for c in qualifying]
    k = max(0, min(k, len(tickers)))
    bi = _band_index(bands)
    if k == 0:
        return {"chosen": [], "method": "NONE", "reason": "k = 0 — no capital to place"}
    n_comb = 1
    for i in range(k):
        n_comb = n_comb * (len(tickers) - i) // (i + 1)
    exact = n_comb <= EXACT_ENUMERATION_CAP

    def key(sub: Sequence[str]) -> tuple:
        # (1) band profile, best-first: fewer low-band picks is better -> minimise the sorted
        #     band indices lexicographically. (2) then minimise sigma_p of held+sub.
        prof = tuple(sorted(bi.get(t, len(bands)) for t in sub))
        sp = sigma_p(list(held) + list(sub), weights or {}, sigmas, matrix)
        return (prof, sp if sp is not None else float("inf"))

    if exact:
        best = min(itertools.combinations(tickers, k), key=key)
        chosen, method = list(best), "EXACT_ENUMERATION"
        reason = ("all C(%d,%d) = %d subsets enumerated — the exact answer is affordable at "
                  "this scale, so no greedy-suboptimality objection arises"
                  % (len(tickers), k, n_comb))
    else:
        chosen, method = [], "GREEDY"
        pool = list(tickers)
        for _ in range(k):
            pick = min(pool, key=lambda t: key(chosen + [t]))
            chosen.append(pick)
            pool.remove(pick)
        reason = ("C(%d,%d) = %d exceeds the %d cap — greedy, and SAID SO. A greedy answer "
                  "reported as an exact one is a result that cannot be challenged."
                  % (len(tickers), k, n_comb, EXACT_ENUMERATION_CAP))
    return {"chosen": chosen, "method": method, "reason": reason,
            "n_subsets_considered": n_comb if exact else k * len(tickers),
            "sigma_p_of_result": sigma_p(list(held) + chosen, weights or {}, sigmas, matrix)}


# ══════════════════════════════════════════════════════════════════════════════════════
# THE ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════════════
def sequence(candidates, *, held: Sequence[str] = (), matrix: Optional[Dict] = None,
             sigmas: Optional[Dict[str, float]] = None,
             weights: Optional[Dict[str, float]] = None,
             k: Optional[int] = None, se_rho: Optional[float] = None,
             ranking_basis: str = "source_score",
             rho_pairwise_gate: float = 0.70, rho_sleeve_gate: float = 0.60,
             panel_path: Optional[str] = None) -> dict:
    """-> the deployment ORDER over the qualified set, with the displacement published."""
    _fi_mark("deployment_sequencer", "sequence")
    cands = list(candidates.get("qualifying") if isinstance(candidates, dict) else candidates)
    matrix = matrix or {}
    sigmas = sigmas or {}
    declared = sorted(cands, key=lambda c: -(c.get(ranking_basis) or 0.0))
    declared_order = [c["ticker"] for c in declared]

    if not _flag():
        return {"state": "DISABLED", "basis": ranking_basis, "order": declared_order,
                "qualifying": declared, "method": "DISABLED_DECLARED_ORDER",
                "displacement": [],
                "detail": ("rollback: V2_FLAGS['deployment_sequencer'] is False. The order is "
                           "the DECLARED conviction order and §2 must say so — a fallback "
                           "order presented as a sequenced one is a claim nobody can check.")}
    if not cands:
        return {"state": "OK", "basis": ranking_basis, "order": [], "qualifying": [],
                "method": "EMPTY", "displacement": [],
                "detail": "no qualifying candidates to sequence"}

    b = band(cands, ranking_basis=ranking_basis, panel_path=panel_path)
    bands = b["bands"]

    # ── P6.4 SEQUENTIAL ADMISSION, re-measuring after EVERY pick ──────────────────────
    admitted: List[str] = list(held)
    order, records, reorders, suppressed = [], [], [], []
    remaining = list(declared)
    bi = _band_index(bands)
    while remaining:
        # only names in the best available band compete — conviction dominates (P6.2)
        best_band = min(bi.get(c["ticker"], len(bands)) for c in remaining)
        tier = [c for c in remaining if bi.get(c["ticker"], len(bands)) == best_band]
        measured = [(c, rho_to_set(c["ticker"], admitted, matrix, weights)) for c in tier]
        declared_first = tier[0]
        if len(tier) == 1:
            pick, rec = measured[0]
        else:
            # ⚑ P6.3 THE NOISE GATE. Reorder ONLY when the rho_sleeve gap beats the SE.
            scored = [(c, r) for c, r in measured if r.get("rho_sleeve") is not None]
            if len(scored) < 2 or se_rho is None:
                pick, rec = measured[0]
                suppressed.append({"band": best_band, "reason": ("rho unmeasured or SE(rho) "
                                   "unavailable — the band's declared order stands")})
            else:
                lo = min(scored, key=lambda x: x[1]["rho_sleeve"])
                gap = abs(lo[1]["rho_sleeve"]
                          - next(r["rho_sleeve"] for c, r in scored
                                 if c["ticker"] == declared_first["ticker"]))
                threshold = RHO_REORDER_SE_MULTIPLE * float(se_rho)
                if lo[0]["ticker"] != declared_first["ticker"] and gap > threshold:
                    pick, rec = lo
                    reorders.append({"band": best_band, "promoted": pick["ticker"],
                                     "displaced": declared_first["ticker"],
                                     "rho_gap": round(gap, 4),
                                     "threshold": round(threshold, 4),
                                     "line": ("%s admitted ahead of %s — same band, rho_sleeve "
                                              "%.3f vs %.3f (gap %.3f > %.1f x SE %.4f)"
                                              % (pick["ticker"], declared_first["ticker"],
                                                 lo[1]["rho_sleeve"],
                                                 next(r["rho_sleeve"] for c, r in scored
                                                      if c["ticker"] == declared_first["ticker"]),
                                                 gap, RHO_REORDER_SE_MULTIPLE, float(se_rho)))})
                else:
                    pick, rec = next((c, r) for c, r in measured
                                     if c["ticker"] == declared_first["ticker"])
                    if lo[0]["ticker"] != declared_first["ticker"]:
                        suppressed.append({
                            "band": best_band, "would_promote": lo[0]["ticker"],
                            "rho_gap": round(gap, 4), "threshold": round(threshold, 4),
                            "reason": ("gap %.3f <= %.1f x SE(rho) %.4f — inside the noise of "
                                       "the measurement, so the band's declared order stands"
                                       % (gap, RHO_REORDER_SE_MULTIPLE, float(se_rho)))})
        # ⚑ ADMISSION AGAINST THE SLEEVE AS IT WILL THEN STAND — the F6 fix.
        rmax = rec.get("rho_max_pairwise")
        rsl = rec.get("rho_sleeve")
        breach = ((rmax is not None and rmax >= rho_pairwise_gate)
                  or (rsl is not None and rsl >= rho_sleeve_gate))
        verdict = "REPLACEMENT_ONLY" if breach else ("ADMIT" if rec.get("measured")
                                                    else "ADMIT_CAPPED_STARTER")
        records.append({"ticker": pick["ticker"], "band": best_band, "verdict": verdict,
                        "correlation_at_decision": rec,
                        "against": list(admitted),
                        "note": ("measured against the sleeve AS IT WILL THEN STAND, including "
                                 "names admitted earlier in THIS run (P6.4)")})
        if verdict != "REPLACEMENT_ONLY":
            admitted.append(pick["ticker"])
            order.append(pick["ticker"])
        remaining = [c for c in remaining if c["ticker"] != pick["ticker"]]

    out = {
        "state": "OK", "basis": ranking_basis,
        "order": order, "declared_order": declared_order,
        "qualifying": [c for c in declared if c["ticker"] in set(order)],
        "records": records,
        "replacement_only": [r["ticker"] for r in records
                             if r["verdict"] == "REPLACEMENT_ONLY"],
        "banding": {k2: b[k2] for k2 in ("banded", "se", "method", "reason", "n_bands")
                    if k2 in b},
        "noise_gate": {"multiple": RHO_REORDER_SE_MULTIPLE, "se_rho": se_rho,
                       "threshold": (RHO_REORDER_SE_MULTIPLE * float(se_rho))
                                    if se_rho is not None else None,
                       "n_reorders": len(reorders), "n_suppressed": len(suppressed),
                       "fire_rate": (len(reorders) / (len(reorders) + len(suppressed))
                                     if (reorders or suppressed) else None)},
        # ⚑ P6.6 PUBLISH THE DISPLACEMENT. A reordering you cannot see is a reordering you
        # cannot challenge.
        "displacement": [r["line"] for r in reorders],
        "suppressed_reorders": suppressed,
        "detail": ("P6. Correlation ORDERS the queue and never enters the score. Conviction "
                   "bands dominate; correlation breaks ties only between statistically "
                   "indistinguishable names, and only when the gap beats the SE."),
    }
    if k is not None:
        out["selection"] = choose(out["qualifying"], k, bands=bands, held=list(held),
                                  matrix=matrix, sigmas=sigmas, weights=weights)
    return out


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)[:220]) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    # ── P6-A1 — THE F6 CASE, and BOTH verdicts (pre-build and post-build) ─────────────
    # Two candidates 0.97 correlated with each other, ~0.17 with the sleeve.
    C1 = {"ticker": "C1", "source_score": 70.0}
    C2 = {"ticker": "C2", "source_score": 69.5}
    HELD = ["AVGO", "MU"]
    M = {"AVGO|MU": 0.45, "C1|C2": 0.97,
         "C1|AVGO": 0.17, "C1|MU": 0.17, "C2|AVGO": 0.18, "C2|MU": 0.16}
    SIG = {"AVGO": 0.54, "MU": 0.71, "C1": 0.60, "C2": 0.60}

    # PRE-BUILD verdict: the single-name gate evaluated INDEPENDENTLY blocks both, because each
    # sees the other's 0.97 as its rho_max against a set that already contains it.
    import correlation_engine as ce
    pre = []
    for c in ("C1", "C2"):
        other = "C2" if c == "C1" else "C1"
        rmax = max(_rho(c, h, M) for h in HELD + [other])
        pre.append("REPLACEMENT_ONLY" if rmax >= 0.70 else "ADMIT")
    ok("P6-A1 PRE-BUILD: evaluated independently against a set containing BOTH, the gate "
       "blocks BOTH (the F6 defect)", pre == ["REPLACEMENT_ONLY", "REPLACEMENT_ONLY"], pre)

    r = sequence([C1, C2], held=HELD, matrix=M, sigmas=SIG, se_rho=0.0995, panel_path=None)
    verdicts = {x["ticker"]: x["verdict"] for x in r["records"]}
    ok("P6-A1 POST-BUILD: the FIRST is admitted", verdicts.get("C1") in
       ("ADMIT", "ADMIT_CAPPED_STARTER"), verdicts)
    ok("P6-A1 POST-BUILD: the SECOND is REPLACEMENT_ONLY after re-measurement against the "
       "sleeve as it THEN stands", verdicts.get("C2") == "REPLACEMENT_ONLY", verdicts)
    ok("P6-A1 exactly ONE of the pair is admitted — the mechanism owns the better one",
       r["order"] == ["C1"], r["order"])
    c2rec = next(x for x in r["records"] if x["ticker"] == "C2")
    ok("P6-A1 ...and C1 is IN the set C2 was measured against (that is the whole fix)",
       "C1" in c2rec["against"], c2rec["against"])

    # ── P6-A2 — the noise gate, both directions ──────────────────────────────────────
    LO = {"ticker": "LO", "source_score": 70.0}   # declared first
    HI = {"ticker": "HI", "source_score": 69.9}   # same band, lower rho
    M2 = {"LO|AVGO": 0.30, "LO|MU": 0.30, "HI|AVGO": 0.28, "HI|MU": 0.28}
    small = sequence([LO, HI], held=HELD, matrix={**M2, "AVGO|MU": 0.45},
                     sigmas={**SIG, "LO": 0.5, "HI": 0.5}, se_rho=0.0995,
                     panel_path=None)
    ok("P6-A2 a rho gap of 0.02, BELOW 1.0 x SE(rho) 0.0995, does NOT reorder",
       small["order"][0] == "LO" and small["noise_gate"]["n_reorders"] == 0, small["order"])
    ok("P6-A2 ...and the suppression is PUBLISHED with its numbers, not silent",
       bool(small["suppressed_reorders"]), small["suppressed_reorders"])
    M3 = {"LO|AVGO": 0.60, "LO|MU": 0.60, "HI|AVGO": 0.10, "HI|MU": 0.10, "AVGO|MU": 0.45}
    big = sequence([LO, HI], held=HELD, matrix=M3, sigmas={**SIG, "LO": 0.5, "HI": 0.5},
                   se_rho=0.0995, panel_path=None)
    ok("P6-A2-neg a rho gap of 0.50, ABOVE the threshold, DOES reorder",
       big["order"][0] == "HI" and big["noise_gate"]["n_reorders"] == 1, big["order"])
    ok("P6-A8 the displacement line renders when the order differs from the band order",
       big["displacement"] and "admitted ahead of" in big["displacement"][0],
       big["displacement"])
    ok("P6-A8-neg identical orders render NO displacement line", not small["displacement"])

    # ── P6-A3 — band order dominates: correlation never promotes a lower band ─────────
    HIGH = {"ticker": "HIGHBAND", "source_score": 90.0}
    LOWB = {"ticker": "LOWBAND", "source_score": 40.0}    # far outside one SE
    M4 = {"HIGHBAND|AVGO": 0.65, "HIGHBAND|MU": 0.65,
          "LOWBAND|AVGO": 0.01, "LOWBAND|MU": 0.01, "AVGO|MU": 0.45}
    dom = sequence([HIGH, LOWB], held=HELD, matrix=M4,
                   sigmas={**SIG, "HIGHBAND": 0.5, "LOWBAND": 0.5},
                   se_rho=0.0995, panel_path=None)
    # ⚑ THE PROPERTY IS ABOUT THE PICK ORDER, NOT THE ADMISSION VERDICT, and the first
    # version of this assertion conflated them. Here HIGHBAND is CONSIDERED first (band
    # dominance held) and is then correctly REPLACEMENT_ONLY because its rho_sleeve 0.65
    # breaches the 0.60 sleeve gate. That is two rules working, not one failing — but
    # `order` holds only ADMITTED names, so asserting on it tested the wrong thing.
    ok("P6-A3 the higher band is CONSIDERED first, whatever its rho",
       dom["records"][0]["ticker"] == "HIGHBAND", [r["ticker"] for r in dom["records"]])
    ok("P6-A3 ...and a gate breach is an ADMISSION verdict, not a demotion in the order",
       dom["records"][0]["verdict"] == "REPLACEMENT_ONLY"
       and dom["records"][0]["correlation_at_decision"]["rho_sleeve"] >= 0.60,
       dom["records"][0]["verdict"])
    # the same test with rho values that do NOT trip either gate — band dominance alone
    M5 = {"HIGHBAND|AVGO": 0.40, "HIGHBAND|MU": 0.40,
          "LOWBAND|AVGO": 0.01, "LOWBAND|MU": 0.01, "AVGO|MU": 0.45}
    dom2 = sequence([HIGH, LOWB], held=HELD, matrix=M5,
                    sigmas={**SIG, "HIGHBAND": 0.5, "LOWBAND": 0.5},
                    se_rho=0.0995, panel_path=None)
    ok("P6-A3 with neither gate tripped, the LOWER band is never admitted first even though "
       "its rho is 0.39 better", dom2["order"] == ["HIGHBAND", "LOWBAND"], dom2["order"])
    ok("P6-A3-neg ...and inside ONE band that same rho advantage DOES reorder",
       big["order"][0] == "HI")

    # ── P6-A4 — unmeasurable SE ⇒ refuse to band, fall back, and SAY SO ───────────────
    nb = band([C1, C2], panel_path="/nonexistent/score_panel.csv")
    ok("P6-A4 an unmeasurable SE REFUSES to band and falls back to declared order",
       nb["banded"] is False and nb["method"] == "STRICT_DECLARED_ORDER", nb["method"])
    ok("P6-A4 ...and says so in words a reader can act on", "REFUSING TO BAND" in nb["reason"])
    live = measure_score_se()
    ok("P6-A4-neg the LIVE panel DOES measure an SE (the control is not vacuous)",
       live["measured"] and live["se"] > 0, live)

    # ── P6-A5 — exact vs greedy, and the method is STATED ────────────────────────────
    six = [{"ticker": "T%d" % i, "source_score": 70.0 - i * 0.1} for i in range(6)]
    bs = band(six, se=5.0)
    ch = choose(six, 2, bands=bs["bands"], held=[], matrix={}, sigmas={t["ticker"]: 0.5 for t in six})
    ok("P6-A5 exact enumeration at |Q|=6, k=2", ch["method"] == "EXACT_ENUMERATION"
       and ch["n_subsets_considered"] == 15, ch)
    many = [{"ticker": "N%03d" % i, "source_score": 70.0} for i in range(60)]
    bm = band(many, se=5.0)
    chg = choose(many, 5, bands=bm["bands"], held=[], matrix={},
                 sigmas={t["ticker"]: 0.5 for t in many})
    ok("P6-A5 greedy ABOVE the cap, and the method used is STATED",
       chg["method"] == "GREEDY" and "greedy" in chg["reason"].lower(), chg["method"])

    # ── P6.4 re-measurement is real: rho_to_set changes as the set grows ──────────────
    a = rho_to_set("C2", HELD, M)
    b2 = rho_to_set("C2", HELD + ["C1"], M)
    ok("P6.4 rho_max_pairwise RISES once C1 joins the set — re-measurement is not cosmetic",
       b2["rho_max_pairwise"] > a["rho_max_pairwise"], (a, b2))
    ok("P6.4 an empty held set is EMPTY_SET, not a measured zero",
       rho_to_set("C1", [], M)["measured"] is False)

    # ── rollback ─────────────────────────────────────────────────────────────────────
    import isa_policy as _p
    prev = _p.V2_FLAGS.get("deployment_sequencer")
    _p.V2_FLAGS["deployment_sequencer"] = False
    d = sequence([C1, C2], held=HELD, matrix=M, sigmas=SIG, se_rho=0.0995)
    ok("rollback: DISABLED returns the DECLARED order and says §2 must state it",
       d["state"] == "DISABLED" and d["order"] == ["C1", "C2"]
       and d["method"] == "DISABLED_DECLARED_ORDER", d["method"])
    _p.V2_FLAGS["deployment_sequencer"] = True
    ok("rollback-neg flag True ⇒ sequencing resumes (control is not vacuous)",
       sequence([C1, C2], held=HELD, matrix=M, sigmas=SIG,
                se_rho=0.0995)["state"] == "OK")
    if prev is None:
        _p.V2_FLAGS.pop("deployment_sequencer", None)
    else:
        _p.V2_FLAGS["deployment_sequencer"] = prev

    print("\ndeployment_sequencer selftest: %d assertion(s), %d FAIL(s)%s"
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
    print(json.dumps({"rho_reorder_se_multiple": RHO_REORDER_SE_MULTIPLE,
                      "exact_enumeration_cap": EXACT_ENUMERATION_CAP,
                      "score_se": measure_score_se()}, indent=1))
