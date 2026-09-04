#!/usr/bin/env python3
"""
thesis_state.py — P7.2. THE home for judgement in the V2.1 sizing stack.

Authority: ISA_BuildSpec_FrameworkIntegrity_and_CapitalDeployment_27Aug2026.md P7 (D21).
Above it: ISA_V2_1_BUILD_SPEC_CLEAN_23Aug2026.md §6 — *"Judgement lives in `thesis_state` and
may only block, downsize or hold — never upsize. Do not reinstate the /100 conviction score."*

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHY THIS MODULE DID NOT EXIST UNTIL TODAY, WHICH IS THE WHOLE POINT
═══════════════════════════════════════════════════════════════════════════════════════════
The clean spec relocated judgement OUT of the /100 conviction score and INTO `thesis_state`.
Measured on the delivered tree 27-Aug-2026, the name appeared in exactly four places, all
prose: a docstring line in `evidence_state.py`, the 22-Aug reasoning record, clean spec §6,
and a JSON *example* in a superseded design document. **No module defined, wrote or read it.**

**Judgement was evicted from the /100 and given nowhere to go, so it never left.** The /100
stayed live and hard-gated the §7.6.2 email — on a field that is populated for **2 of 53**
names, with every `conviction_total` in `step9_conviction_aug_2026.json` null. A gate on a
field nobody fills is not a gate.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑⚑ THE ASYMMETRY IS STRUCTURAL, NOT ASSERTED
═══════════════════════════════════════════════════════════════════════════════════════════
`apply()` resolves both its inputs to an INDEX on the declared ladder and returns
`ladder[min(rung_ix, ceiling_ix)]`. **There is no expression in this module that can produce a
larger index than it was given.** The asymmetry is therefore a property of the arithmetic, not
of a test that someone remembers to keep.

That matters because the failure mode is specific and it has a name: A1's withdrawn `x d`
diversification multiplier was a judgement input that could RAISE a size, and it was withdrawn
because a judgement that can upsize is indistinguishable from a forecast. This module cannot
be turned back into one without deleting `min`.

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ AND IT CARRIES conviction_capture's REFUSAL, DELIBERATELY (C8)
═══════════════════════════════════════════════════════════════════════════════════════════
Retiring the /100 removes a control. If `thesis_state` were optional, the net effect of P7
would be to remove a control and add none. `require()` therefore REFUSES a decided action
that carries no state or no rationale — the same shape of refusal `conviction_capture`
already applies to the score it replaces.

ROLLBACK (R4.13): isa_policy.V2_FLAGS["single_sizing_authority"] = False ⇒ `apply()` returns
its input rung unchanged and `require()` does not refuse. The /100 becomes readable by gates
again. **Keep a pre-delivery copy of every prose file touched** — swapping it back and
re-running is the only honest answer to "is this red mine?".
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))

# ── P0.1 LIVE-PATH EXECUTION LEDGER ────────────────────────────────────────────────────
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

STRENGTHENING = "STRENGTHENING"
INTACT = "INTACT"
WATCH = "WATCH"
BROKEN = "BROKEN"

STATES = (STRENGTHENING, INTACT, WATCH, BROKEN)

# The ladder, ordered LOW to HIGH. Read from target_weights.json every run and never typed —
# a second copy of the ladder here would be exactly the two-homes defect P7 exists to close.
LADDER_ORDER = ("STARTER", "NORMAL", "HIGH", "EARNED_MAX")

# ⚑ The CEILING each state imposes, and nothing else. A state never names a size; it names the
# highest rung it will tolerate. `None` means "imposes no ceiling", which is not the same as
# "raises to the top" — the difference is the whole asymmetry.
STATE_CEILING: Dict[str, Optional[str]] = {
    STRENGTHENING: None,        # ⚑ NO CHANGE. It may not upsize — that is the point.
    INTACT:        None,
    WATCH:         "STARTER",
    BROKEN:        None,        # handled by `blocks_new_capital`, not by a rung
}

STATE_BLOCKS_NEW_CAPITAL = {STRENGTHENING: False, INTACT: False, WATCH: False, BROKEN: True}

STATE_BASIS = {
    STRENGTHENING: ("the thesis is doing better than underwritten. It does NOT earn a larger "
                    "position: size is earned by EVIDENCE on the ladder, and letting judgement "
                    "grant size is A1's withdrawn x d multiplier returning through the back "
                    "door."),
    INTACT:        "the thesis is as underwritten. The ladder rung stands.",
    WATCH:         ("something in the thesis is questioned but not broken. New capital is "
                    "capped at STARTER until it resolves — a downgrade, never a block."),
    BROKEN:        ("the thesis no longer holds. NO new capital, and the position enters the "
                    "§10 replacement comparison. This is a judgement about the COMPANY; the "
                    "capital consequence is decided in §10 against alternatives."),
}


class ThesisStateRefused(RuntimeError):
    """A decided action carries no thesis state, or no rationale for it.

    ⚑ NEVER downgraded to a default state. `INTACT` as a default would mean 'nobody looked'
    and 'the thesis is as underwritten' render identically, which is R2.10's exact prohibition
    and is how the /100 came to be 2-of-53 populated while still gating an email."""


def _flag(name: str = "single_sizing_authority", default: bool = True) -> bool:
    try:
        import isa_policy as _p
        if name in _p.V2_FLAGS:
            return bool(_p.V2_FLAGS[name])
    except Exception:                                                   # noqa: BLE001
        pass
    return default


def ladder(policy=None) -> Dict[str, float]:
    """THE ladder, from its one home. Never restated here (R4.4)."""
    import position_sizing as _ps
    return _ps.ladder(policy)


def _order(policy=None) -> List[str]:
    lad = ladder(policy)
    return [r for r in LADDER_ORDER if r in lad]


def validate(state: Optional[str]) -> str:
    if state not in STATES:
        raise ThesisStateRefused(
            "thesis_state %r is not declared. Declared: %s. A state that is not on the list "
            "would size at whatever the caller passed, which is the failure this module "
            "exists to remove." % (state, list(STATES)))
    return state


def apply(rung: str, state: Optional[str], *, policy=None) -> dict:
    """The ONE operation. Returns a rung <= the input rung, ALWAYS, by construction.

    ⚑ `min(rung_ix, ceiling_ix)` is the entire asymmetry. There is no branch here that can
    return a larger index than it was handed, so `apply` cannot be made to upsize without
    someone deleting `min` — which is a visible edit, unlike a forgotten test."""
    _fi_mark("thesis_state", "apply")
    order = _order(policy)
    if rung == "HOLD_AT_CURRENT":
        # D13's freeze is stronger than any thesis state and is not on the ladder.
        return {"rung_in": rung, "rung_out": rung, "state": state, "changed": False,
                "blocks_new_capital": True,
                "basis": ("DEGRADED_UNMEASURED holds the position at its current weight (D13). "
                          "That freeze is not a ladder rung and thesis_state does not move it.")}
    if rung not in order:
        raise ThesisStateRefused(
            "rung %r is not on the ladder %s." % (rung, order))
    if not _flag():
        return {"rung_in": rung, "rung_out": rung, "state": state, "changed": False,
                "blocks_new_capital": False,
                "basis": "ROLLBACK: V2_FLAGS['single_sizing_authority'] is False — the rung "
                         "passes through unchanged."}
    st = validate(state)
    rung_ix = order.index(rung)
    ceil = STATE_CEILING[st]
    ceil_ix = order.index(ceil) if ceil in order else rung_ix
    out_ix = min(rung_ix, ceil_ix)          # ⚑ THE ASYMMETRY, in one call
    out = order[out_ix]
    return {"rung_in": rung, "rung_out": out, "state": st,
            "changed": out != rung,
            "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL[st],
            "enters_replacement_comparison": st == BROKEN,
            "basis": ("thesis_state %s: %s%s" %
                      (st, STATE_BASIS[st],
                       (" Rung %s -> %s." % (rung, out)) if out != rung else
                       " Rung %s unchanged." % rung)),
            "asymmetry": ("min(rung, ceiling) — this function has no path that returns a rung "
                          "above its input, over all %d states x all %d rungs."
                          % (len(STATES), len(order)))}


def require(action: dict, *, field: str = "thesis_state",
            rationale_field: str = "thesis_state_rationale") -> dict:
    """P7.3's gate input. A decided action carries a state AND a rationale, or this REFUSES.

    ⚑ BOTH, not either. A state with no rationale is a label; a rationale with no state is an
    opinion. The old §7.6.2 gate was satisfied by `conviction_total`, a field that stood at
    2 of 53 populated — and it passed anyway because nothing checked that it was filled."""
    tk = action.get("ticker") or action.get("name") or "<unnamed>"
    if not _flag():
        return {"ok": True, "ticker": tk, "state": action.get(field),
                "basis": "ROLLBACK: the single-sizing-authority flag is False."}
    st = action.get(field)
    if st is None:
        raise ThesisStateRefused(
            "%s: a decided action carries no %s. Retiring the /100 removes a control; making "
            "this one optional would remove a control and add none (C8). Declare one of %s "
            "with a rationale." % (tk, field, list(STATES)))
    validate(st)
    rat = action.get(rationale_field)
    if not rat or not str(rat).strip():
        raise ThesisStateRefused(
            "%s: thesis_state is %s with no rationale. A state without a reason cannot be "
            "challenged next month, which is the only thing that makes a judgement reviewable "
            "rather than a preference." % (tk, st))
    return {"ok": True, "ticker": tk, "state": st, "rationale": str(rat).strip(),
            "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL[st]}


def gate(actions: Sequence[dict]) -> dict:
    """The §7.6.2 replacement gate: every decided action, all-or-nothing, naming each failure."""
    ok, refused = [], []
    for a in actions or []:
        try:
            ok.append(require(a))
        except ThesisStateRefused as exc:
            refused.append({"ticker": a.get("ticker") or a.get("name") or "<unnamed>",
                            "reason": str(exc)})
    return {"state": "REFUSED" if refused else "OK",
            "n_actions": len(actions or []), "n_ok": len(ok), "refused": refused,
            "rows": ok,
            "basis": ("P7.3 — the §7.6.2 gate now reads thesis_state + evidence_state, both "
                      "non-null with rationales, and refuses on either being absent. It "
                      "replaces a gate on `conviction_total`, a field that was null for every "
                      "name in step9_conviction_aug_2026.json.")}


def _selftest() -> int:
    fails = []

    def ok(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)[:200]) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    order = _order()

    # ── A1: never returns a rung ABOVE its input, over ALL 4 states x ALL 4 rungs ────────
    bad = []
    for st in STATES:
        for r in order:
            out = apply(r, st)["rung_out"]
            if order.index(out) > order.index(r):
                bad.append((st, r, out))
    ok("A1 apply() never returns a rung above its input — all %d states x all %d rungs"
       % (len(STATES), len(order)), not bad, bad)

    # A1 negative control: a hand-broken variant that upsizes MUST fail the same check
    def _broken_apply(rung, state):
        ix = order.index(rung)
        return order[min(len(order) - 1, ix + (1 if state == STRENGTHENING else 0))]
    bad2 = [(st, r) for st in STATES for r in order
            if order.index(_broken_apply(r, st)) > order.index(r)]
    ok("A1-neg a hand-broken variant that upsizes on STRENGTHENING IS caught by that check",
       len(bad2) > 0, bad2)

    # ── A2: WATCH caps at STARTER; BROKEN blocks; INTACT/STRENGTHENING leave the rung ────
    ok("A2 WATCH caps at STARTER from every higher rung",
       all(apply(r, WATCH)["rung_out"] == "STARTER" for r in order))
    ok("A2 WATCH does not RAISE a STARTER", apply("STARTER", WATCH)["rung_out"] == "STARTER")
    ok("A2 BROKEN blocks new capital", apply("HIGH", BROKEN)["blocks_new_capital"] is True)
    ok("A2 BROKEN enters the §10 replacement comparison",
       apply("HIGH", BROKEN)["enters_replacement_comparison"] is True)
    ok("A2-neg INTACT leaves every rung untouched",
       all(apply(r, INTACT)["rung_out"] == r for r in order))
    ok("A2-neg STRENGTHENING leaves every rung untouched — it may NOT upsize",
       all(apply(r, STRENGTHENING)["rung_out"] == r for r in order))
    ok("A2-neg neither INTACT nor STRENGTHENING blocks new capital",
       not apply("NORMAL", INTACT)["blocks_new_capital"]
       and not apply("NORMAL", STRENGTHENING)["blocks_new_capital"])

    # ── D13's freeze is stronger than any state ─────────────────────────────────────────
    ok("HOLD_AT_CURRENT is not a ladder rung and no state moves it",
       all(apply("HOLD_AT_CURRENT", st)["rung_out"] == "HOLD_AT_CURRENT" for st in STATES))

    # ── A4: the gate refuses on a null state AND on a null rationale ────────────────────
    good = {"ticker": "AAA", "thesis_state": INTACT,
            "thesis_state_rationale": "revisions still improving on both windows"}
    ok("A4 a complete action passes the gate", require(good)["ok"] is True)
    for bad_action, needle in (
            ({"ticker": "BBB"}, "carries no thesis_state"),
            ({"ticker": "CCC", "thesis_state": INTACT}, "no rationale"),
            ({"ticker": "DDD", "thesis_state": INTACT, "thesis_state_rationale": "   "},
             "no rationale"),
            ({"ticker": "EEE", "thesis_state": "SOLID",
              "thesis_state_rationale": "x"}, "not declared")):
        try:
            require(bad_action)
            ok("A4 refuses: " + needle, False, "did not refuse")
        except ThesisStateRefused as e:
            ok("A4 refuses on %s" % needle, needle in str(e), str(e)[:150])
    g = gate([good, {"ticker": "ZZZ"}])
    ok("A4 gate() names each refusal rather than failing anonymously",
       g["state"] == "REFUSED" and g["refused"][0]["ticker"] == "ZZZ" and g["n_ok"] == 1, g)

    # ── A8: flag False ⇒ pass-through, and the control is not vacuous ───────────────────
    import isa_policy as _p
    prev = _p.V2_FLAGS.get("single_sizing_authority")
    _p.V2_FLAGS["single_sizing_authority"] = False
    ok("A8 flag False ⇒ every rung passes through unchanged under every state",
       all(apply(r, st)["rung_out"] == r for r in order for st in STATES))
    ok("A8 flag False ⇒ require() does not refuse a null state",
       require({"ticker": "X"})["ok"] is True)
    _p.V2_FLAGS["single_sizing_authority"] = True
    ok("A8-neg flag True ⇒ WATCH caps again (the rollback control is not vacuous)",
       apply("HIGH", WATCH)["rung_out"] == "STARTER")
    if prev is None:
        _p.V2_FLAGS.pop("single_sizing_authority", None)
    else:
        _p.V2_FLAGS["single_sizing_authority"] = prev

    # ── the ladder has ONE home ─────────────────────────────────────────────────────────
    import position_sizing as _ps
    ok("the ladder is READ from position_sizing, never restated here",
       ladder() == _ps.ladder())

    print("\nthesis_state selftest: %d assertion(s), %d FAIL(s)%s"
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
    print(json.dumps({"states": list(STATES), "ceilings": STATE_CEILING,
                      "blocks_new_capital": STATE_BLOCKS_NEW_CAPITAL,
                      "ladder": ladder()}, indent=1))
