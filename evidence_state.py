#!/usr/bin/env python3
"""
evidence_state.py — the counted-channel evidence classifier. V2.1-B (ISA-0355), SHADOW ONLY.
Authority: clean spec s6; amendment A16; Raj decisions D12 and D13 (26-Aug-2026, ISA-0437).

═══════════════════════════════════════════════════════════════════════════════════════════
WHAT THIS REPLACES AND WHY
═══════════════════════════════════════════════════════════════════════════════════════════
The legacy /100 conviction score is retired from sizing. It is populated on 2 of 53 names; D8-D10
are 30% of it and depend on CASH and OTHER HOLDINGS, so the same company's conviction changes when
the cash balance changes; `target_upside` appears in D1 AND D7 and `stress` in D4 AND D7, so
duplicated inputs carry larger effective weights than declared; and balance-sheet scores average
9.69/10 with 11 of 13 at ten, so that dimension ranks nothing.

V2.1's replacement was a four-state vector whose STRONG definition contained five undefined terms
("sustained", "multiple", "no material", "corroborated", "or equivalent") with no counter, no
threshold and no persistence window — judgement at the single rung where the money is. Under the
ladder, STRONG vs CONFIRMED is 5.5% vs 4.5%, about GBP 1,560 per position.

A16's answer, built here: COUNTED CHANNELS. "Sustained", "multiple", "corroborated" become
FOUR CHANNELS, TWO RUNS. Every test is a comparison against a stored number (M0 end to end).

═══════════════════════════════════════════════════════════════════════════════════════════
THE THREE STRUCTURAL PROPERTIES
═══════════════════════════════════════════════════════════════════════════════════════════
1. G IS A GATE, NEVER A COUNTED CHANNEL. No amount of positive evidence buys past missing data.
   This is what preserves non-compensation structurally, which prose could not deliver.

2. TWO DISTINCT FAMILIES FOR CONFIRMED. "A name confirmed by three flavours of analyst optimism
   is confirmed by one thing." ⚑ On the VCI route this bites hardest and Raj declared the
   taxonomy on 26-Aug (D12): FV asymmetry, ACS>=75 and the named catalyst are ONE family
   (`vci_composite`), because ACS ALREADY CONTAINS the other two. Treating them as three would
   have made the two-family rule automatically satisfied by any two channels — a control that
   cannot bind.

3. JUDGEMENT MAY ONLY BLOCK, DOWNSIZE OR HOLD — NEVER UPSIZE. It lives in `thesis_state`.
   `evidence_state`, which GRANTS capital, is untouched by opinion.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ DEGRADED IS TWO THINGS AND THEY MUST NOT PRODUCE THE SAME OUTPUT (R2.10, Raj D13)
═══════════════════════════════════════════════════════════════════════════════════════════
The clean spec wrote "DEGRADED: G fails, or a confirmed channel reverses". Those are
"I could not measure it" and "it got worse", and R2.10 forbids them yielding one output.

  DEGRADED_UNMEASURED  G fails. FREEZE: no new capital, no top-up, target held at current.
                       Does NOT enter challenger comparison — an unmeasured incumbent against a
                       measured challenger is decided by MEASURABILITY, not quality, and the
                       incumbent loses for a reason that is not about the business.
                       ESCALATES to DEGRADED_REVERSED after 2 consecutive failed runs, because
                       persistent unmeasurability is itself information.
  DEGRADED_REVERSED    A confirmed channel reversed. Enters the s10 pairwise comparison THAT
                       RUN. Challenger clears -> SELL/REPLACE. No challenger -> trim to STARTER
                       and route the freed capital via capital_destination. Never receives new
                       capital. Carries `evidence_reversal`, which is in MIN_HOLD_EXEMPT (D14) —
                       without that the whole machine is inert until 04-Oct-2026.
"""
from __future__ import annotations

import datetime
import json
from typing import Dict, List, Optional

POLICY_VERSION = "ISA_V2_1"

DEGRADED_UNMEASURED = "DEGRADED_UNMEASURED"
DEGRADED_REVERSED = "DEGRADED_REVERSED"
THIN = "THIN"
CONFIRMED = "CONFIRMED"
STRONG = "STRONG"
EARNED_MAX = "EARNED_MAX"

STATE_TO_RUNG = {
    DEGRADED_UNMEASURED: "HOLD_AT_CURRENT",
    DEGRADED_REVERSED: "STARTER",
    THIN: "STARTER",
    CONFIRMED: "NORMAL",
    STRONG: "HIGH",
    EARNED_MAX: "EARNED_MAX",
}

STRONG_MIN_CHANNELS = 4
STRONG_MIN_RUNS = 2
CONFIRMED_MIN_CHANNELS = 2
CONFIRMED_MIN_FAMILIES = 2
EARNED_MAX_MIN_DAYS = 182
UNMEASURED_ESCALATION_RUNS = 2

# ── CHANNEL DEFINITIONS. `family` is the load-bearing field (D12). ───────────────────────
CHANNELS = {
    "main": [
        ("E1", "analyst_revision", "fundamental",
         "90d revision breadth > 0 AND FY1 estimate up over 90d"),
        ("E2", "delivered_results", "fundamental",
         "most recent reported quarter beat consensus on BOTH revenue and EPS"),
        ("E3", "guidance", "fundamental",
         "forward guide raised, or reaffirmed above prior consensus"),
        ("E4", "relative_price", "price",
         "12-1 month relative return > 0 AND 6m relative > 0 (never 3m)"),
        ("E5", "route_native", "route",
         "forward_axis_score above frame median"),
    ],
    "vci": [
        ("V1", "nvidia_pattern", "structural", "NVIDIA-pattern signals >= 5/6"),
        ("V2", "fv_asymmetry", "vci_composite", "FV asymmetry >= applied floor"),
        ("V3", "acs", "vci_composite", "ACS >= 75"),
        ("V4", "named_catalyst", "vci_composite", "named catalyst with a date, not lapsed"),
        ("V5", "alignment", "alignment", "founder/insider alignment not reversed"),
    ],
}

# ⚑ E4 IS 12-1 MONTH AND THAT IS DELIBERATE. The June-2026 backtest measured 3-month momentum at
# rank-IC -0.023 (t -0.88, 40% hit) and 12-1 month at +0.038 (t +1.68, 62% hit) over 280 names and
# 42 formation dates. An evidence channel on the 3-month signal would be built on the one lookback
# the panel says carries NEGATIVE forward information.


def families_for(route: str) -> Dict[str, str]:
    return {cid: fam for cid, _n, fam, _d in CHANNELS[route]}


def classify(channels: Dict[str, Optional[bool]], *, route: str = "main",
             coverage_ok: bool, prior_state: Optional[str] = None,
             prior_confirmed: Optional[List[str]] = None,
             runs_at_strong: int = 0, unmeasured_runs: int = 0,
             days_held: int = 0, post_entry_earnings_survived: int = 0) -> dict:
    """Classify one name. Returns the state, the audit string, and WHY.

    `channels` maps channel id -> True (confirming) / False (not confirming) / None (unmeasured).
    ⚑ None is NOT False. An unmeasured channel cannot confirm and cannot reverse; conflating the
    two is the FC-F pattern that flipped DENY->ADMIT on QBTS."""
    if route not in CHANNELS:
        raise ValueError(f"unknown route {route!r}; declared routes: {sorted(CHANNELS)}")
    fams = families_for(route)
    known = set(fams)
    unknown = set(channels) - known
    if unknown:
        raise ValueError(f"unknown channel id(s) for route {route!r}: {sorted(unknown)}. "
                         f"A channel is declared in CHANNELS or it does not exist.")

    confirming = sorted(c for c, v in channels.items() if v is True)
    unmeasured = sorted(c for c, v in channels.items() if v is None)
    fam_set = sorted({fams[c] for c in confirming})

    prior_confirmed = prior_confirmed or []
    reversed_ch = sorted(c for c in prior_confirmed if channels.get(c) is False)

    base = {
        "policy_version": POLICY_VERSION, "route": route,
        "confirming": confirming, "families": fam_set,
        "unmeasured_channels": unmeasured, "reversed_channels": reversed_ch,
        "coverage_ok": coverage_ok, "prior_state": prior_state,
    }

    # ── 1. REVERSAL takes precedence over coverage. A confirmed channel that has turned is a
    #      fact about the business; a coverage gap is a fact about our data. If both are true the
    #      business fact governs, because it is the one that is actionable.
    if reversed_ch:
        st = DEGRADED_REVERSED
        return {**base, "state": st, "rung": STATE_TO_RUNG[st],
                "min_hold_exempt_reason": "evidence_reversal",
                "enters_challenger_comparison": True,
                "may_receive_new_capital": False,
                "audit": f"{st} ({','.join(reversed_ch)} reversed)",
                "why": (f"{len(reversed_ch)} previously-confirmed channel(s) reversed: "
                        f"{', '.join(reversed_ch)}. Enters the pairwise comparison THIS RUN as "
                        f"incumbent. If a challenger clears the bar -> SELL/REPLACE; if none "
                        f"does -> trim to STARTER and route the freed capital via "
                        f"capital_destination. Carries evidence_reversal, which is in "
                        f"MIN_HOLD_EXEMPT (D14).")}

    # ── 2. COVERAGE GATE. G is never a counted channel.
    if not coverage_ok:
        esc = unmeasured_runs + 1 >= UNMEASURED_ESCALATION_RUNS
        st = DEGRADED_REVERSED if esc else DEGRADED_UNMEASURED
        return {**base, "state": st, "rung": STATE_TO_RUNG[st],
                "unmeasured_runs": unmeasured_runs + 1,
                "escalated": esc,
                "min_hold_exempt_reason": "evidence_reversal" if esc else None,
                "enters_challenger_comparison": esc,
                "may_receive_new_capital": False,
                "audit": (f"{st} (coverage gate failed, run {unmeasured_runs + 1}"
                          + (", ESCALATED)" if esc else ")")),
                "why": (("Coverage has now failed for "
                         f"{unmeasured_runs + 1} consecutive runs, at or beyond the "
                         f"{UNMEASURED_ESCALATION_RUNS}-run limit. Persistent unmeasurability is "
                         f"itself information, so this escalates to the REVERSED treatment "
                         f"(D13).")
                        if esc else
                        ("The coverage gate G failed, so the state is UNMEASURED, not bad. The "
                         "position FREEZES: no new capital, no top-up, target held at current. "
                         "It does NOT enter challenger comparison — an unmeasured incumbent "
                         "against a measured challenger is decided by measurability rather than "
                         "quality (R2.10, D13). Escalates after "
                         f"{UNMEASURED_ESCALATION_RUNS} consecutive failed runs."))}

    n = len(confirming)

    # ── 3. EARNED_MAX — STRONG plus two clocks, both read from the lot table.
    if (n >= STRONG_MIN_CHANNELS and runs_at_strong + 1 >= STRONG_MIN_RUNS
            and days_held >= EARNED_MAX_MIN_DAYS and post_entry_earnings_survived >= 1):
        st = EARNED_MAX
        return {**base, "state": st, "rung": STATE_TO_RUNG[st],
                "runs_at_strong": runs_at_strong + 1, "days_held": days_held,
                "enters_challenger_comparison": False, "may_receive_new_capital": True,
                "audit": f"{st} ({','.join(confirming)} · {runs_at_strong + 1} runs · "
                         f"{days_held}d · {post_entry_earnings_survived} earnings survived)",
                "why": (f"STRONG held for {runs_at_strong + 1} runs, {days_held} days held "
                        f"(>= {EARNED_MAX_MIN_DAYS}) and {post_entry_earnings_survived} "
                        f"post-entry earnings event(s) survived.")}

    # ── 4. STRONG — persistence is a COUNTER, not an adjective.
    if n >= STRONG_MIN_CHANNELS and runs_at_strong + 1 >= STRONG_MIN_RUNS:
        st = STRONG
        return {**base, "state": st, "rung": STATE_TO_RUNG[st],
                "runs_at_strong": runs_at_strong + 1,
                "enters_challenger_comparison": False, "may_receive_new_capital": True,
                "audit": f"{st} ({','.join(confirming)} · {runs_at_strong + 1} runs)",
                "why": (f"{n} channels confirming (>= {STRONG_MIN_CHANNELS}), held for "
                        f"{runs_at_strong + 1} consecutive runs (>= {STRONG_MIN_RUNS}), no "
                        f"reversal. That counter IS the whole content of the word STRONG.")}

    # ── 5. CONFIRMED — the two-family rule (D12).
    if n >= CONFIRMED_MIN_CHANNELS and len(fam_set) >= CONFIRMED_MIN_FAMILIES:
        st = CONFIRMED
        return {**base, "state": st, "rung": STATE_TO_RUNG[st],
                "runs_at_strong": (runs_at_strong + 1) if n >= STRONG_MIN_CHANNELS else 0,
                "enters_challenger_comparison": False, "may_receive_new_capital": True,
                "audit": f"{st} ({','.join(confirming)} · {len(fam_set)} families)",
                "why": (f"{n} channels from {len(fam_set)} distinct families "
                        f"({', '.join(fam_set)})."
                        + (f" Note: {n} channels confirm but only {len(fam_set)} family, so this "
                           f"is CONFIRMED and not STRONG."
                           if n >= STRONG_MIN_CHANNELS else ""))}

    # ── 6. THIN
    st = THIN
    single_fam = n >= CONFIRMED_MIN_CHANNELS and len(fam_set) < CONFIRMED_MIN_FAMILIES
    return {**base, "state": st, "rung": STATE_TO_RUNG[st], "runs_at_strong": 0,
            "enters_challenger_comparison": False, "may_receive_new_capital": True,
            "audit": f"{st} ({','.join(confirming) if confirming else 'none'})",
            "why": (f"{n} channel(s) confirming, all from the single family "
                    f"'{fam_set[0]}'. Two channels from ONE family is one piece of evidence "
                    f"wearing two hats, which is exactly what the family rule exists to catch."
                    if single_fam else
                    f"{n} channel(s) confirming, below the {CONFIRMED_MIN_CHANNELS} required.")}


def _selftest():
    M = lambda **kw: {c: kw.get(c) for c, *_ in [(x[0],) for x in CHANNELS["main"]]}
    # THIN: one channel
    r = classify({"E1": True, "E2": False, "E3": False, "E4": False, "E5": False},
                 coverage_ok=True)
    assert r["state"] == THIN and r["rung"] == "STARTER", r

    # CONFIRMED needs TWO FAMILIES — two fundamentals is NOT enough
    r = classify({"E1": True, "E2": True, "E3": False, "E4": False, "E5": False},
                 coverage_ok=True)
    assert r["state"] == THIN, r
    assert "single family" in r["why"], r["why"]
    r = classify({"E1": True, "E2": False, "E3": False, "E4": True, "E5": False},
                 coverage_ok=True)
    assert r["state"] == CONFIRMED and r["rung"] == "NORMAL", r

    # THREE fundamentals still not CONFIRMED — the A16 headline case
    r = classify({"E1": True, "E2": True, "E3": True, "E4": False, "E5": False},
                 coverage_ok=True)
    assert r["state"] == THIN, r

    # STRONG needs 4 channels AND 2 runs
    ch4 = {"E1": True, "E2": True, "E3": True, "E4": True, "E5": False}
    r1 = classify(ch4, coverage_ok=True, runs_at_strong=0)
    assert r1["state"] == CONFIRMED and r1["runs_at_strong"] == 1, r1
    r2 = classify(ch4, coverage_ok=True, runs_at_strong=1)
    assert r2["state"] == STRONG and r2["rung"] == "HIGH", r2

    # EARNED_MAX adds two clocks
    r3 = classify(ch4, coverage_ok=True, runs_at_strong=1, days_held=200,
                  post_entry_earnings_survived=1)
    assert r3["state"] == EARNED_MAX, r3
    r4 = classify(ch4, coverage_ok=True, runs_at_strong=1, days_held=200,
                  post_entry_earnings_survived=0)
    assert r4["state"] == STRONG, "no earnings survived -> not EARNED_MAX"

    # G is a GATE: five confirming channels do NOT buy past missing data
    r5 = classify({"E1": True, "E2": True, "E3": True, "E4": True, "E5": True},
                  coverage_ok=False)
    assert r5["state"] == DEGRADED_UNMEASURED, r5
    assert r5["may_receive_new_capital"] is False
    assert r5["enters_challenger_comparison"] is False, "unmeasured must NOT be compared"
    assert r5["rung"] == "HOLD_AT_CURRENT"

    # ...and it ESCALATES on the second consecutive failure
    r6 = classify({"E1": True}, coverage_ok=False, unmeasured_runs=1)
    assert r6["state"] == DEGRADED_REVERSED and r6["escalated"] is True, r6
    assert r6["min_hold_exempt_reason"] == "evidence_reversal"

    # a REVERSAL beats coverage and enters comparison immediately
    r7 = classify({"E1": False, "E2": True, "E3": False, "E4": True, "E5": False},
                  coverage_ok=True, prior_confirmed=["E1", "E2"])
    assert r7["state"] == DEGRADED_REVERSED and r7["reversed_channels"] == ["E1"], r7
    assert r7["enters_challenger_comparison"] is True
    assert r7["may_receive_new_capital"] is False

    # ⚑ the two DEGRADED causes must differ in OUTPUT, not just in label (R2.10)
    assert r5["rung"] != r7["rung"]
    assert r5["enters_challenger_comparison"] != r7["enters_challenger_comparison"]

    # None is not False: an unmeasured channel cannot reverse
    r8 = classify({"E1": None, "E2": True, "E3": False, "E4": True, "E5": False},
                  coverage_ok=True, prior_confirmed=["E1", "E2"])
    assert r8["state"] == CONFIRMED, r8
    assert r8["unmeasured_channels"] == ["E1"]

    # ── VCI route, D12: FV asymmetry + ACS + catalyst are ONE family ──────────────────
    v = classify({"V2": True, "V3": True, "V4": True, "V1": False, "V5": False},
                 route="vci", coverage_ok=True)
    assert v["state"] == THIN, ("three vci_composite channels are ONE family", v)
    assert v["families"] == ["vci_composite"], v
    v2 = classify({"V2": True, "V3": True, "V4": True, "V1": True, "V5": False},
                  route="vci", coverage_ok=True)
    assert v2["state"] == CONFIRMED and set(v2["families"]) == {"structural", "vci_composite"}, v2
    v3 = classify({"V1": True, "V5": True, "V2": False, "V3": False, "V4": False},
                  route="vci", coverage_ok=True)
    assert v3["state"] == CONFIRMED, v3

    # unknown channel / route must RAISE, never read as absent
    for bad in (lambda: classify({"NOPE": True}, coverage_ok=True),
                lambda: classify({"E1": True}, route="zzz", coverage_ok=True)):
        try:
            bad(); raise AssertionError("should have raised")
        except ValueError:
            pass

    # every state maps to a rung, and no rung is below STARTER
    assert set(STATE_TO_RUNG) == {DEGRADED_UNMEASURED, DEGRADED_REVERSED, THIN, CONFIRMED,
                                  STRONG, EARNED_MAX}
    assert set(STATE_TO_RUNG.values()) <= {"HOLD_AT_CURRENT", "STARTER", "NORMAL", "HIGH",
                                           "EARNED_MAX"}
    print("evidence_state selftest OK (28 assertions)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    print(json.dumps({"routes": {k: [dict(zip(("id", "name", "family", "test"), c))
                                     for c in v] for k, v in CHANNELS.items()},
                      "state_to_rung": STATE_TO_RUNG}, indent=1))
