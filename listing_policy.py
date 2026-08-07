#!/usr/bin/env python3
"""
listing_policy.py — ONE HOME for "which line of an issuer do we rank, and is it even equity?"
Built 05-Aug-2026 on Raj's instruction to bring register item M3 back into scope and remove the
duplicates outright, rather than merely flagging them.

TWO RULES, DELIBERATELY SEPARATED — they answer different questions and one is a judgement.

RULE 1 — NON-COMMON INSTRUMENTS ARE NOT RANKABLE.  Not a judgement. A mandatory convertible
preferred depositary share and a tangible equity unit are not growth equity, and Run_Context
step 8 has always mandated entity-type exclusion (`STRUCTURAL_NON_APPLICABLE`). It simply never
ran, because `classify_security_type()` had zero callers. Evidence from the 24-Jul-2026 NASDAQ
frame:
    GOOGM / GOOGN   Alphabet mandatory convertible preferred depositary shares   source 22.6
    SMCIP           Super Micro mandatory convertible preferred depositary shares source 24.9
    NOVTU           Novanta TANGIBLE EQUITY UNITS                                 source 54.1
NOVTU is the one that shows the cost: a tangible equity unit is a purchase contract bundled with
an amortising senior note, and it **outranked the actual company (NOVT, 38.6) by 15.5 points**.

RULE 2 — ONE LINE PER ISSUER.  A judgement, so it is made ONCE, written down, and not
re-litigated monthly. Eight issuers had multiple rankable lines on that one frame; Bel Fuse had
BOTH classes above the SUMMARY floor (BELFA 72.2, BELFB 70.7) competing for the same slots.

    Preference order, applied in sequence and recorded:
      1. common over non-common               (Rule 1 has usually already removed these)
      2. GREATER TRADED LIQUIDITY             — the line you can actually deal in. This is the
                                                economically correct tie-break, not a cosmetic
                                                one: an illiquid super-voting B class carries a
                                                real execution cost against the same claim on
                                                the same cash flows. Liberty Global B ranked
                                                HIGHEST of its three lines at 71.6 while the
                                                liquid A class sat at 58.7.
      3. larger market capitalisation
      4. shorter, then alphabetical ticker    — pure determinism, never a preference

⚑ WHAT THIS DOES NOT DO. It does not merge, average or re-score anything. The losing lines are
MARKED and excluded from ranking, never deleted — they stay in `full_data` and in
`constituents_history.csv` with the reason and the winner named, because "which line did the
screen see and reject, and why" is exactly the kind of fact this framework keeps losing.

⚑ AND IT DOES NOT DECIDE ASML. `preferred_listing.json` lets a choice be PINNED by hand. The
ASML NY-registry vs Amsterdam question turns on withholding treatment and AJ Bell's European
dealing terms — that is Raj's call with the broker, not something a liquidity heuristic should
silently settle. A pinned choice always wins over the computed one and records who pinned it.
"""
from __future__ import annotations
import datetime as _dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PREFERRED_PATH = os.path.join(HERE, "preferred_listing.json")

STATUS_NON_COMMON = "STRUCTURAL_NON_APPLICABLE"
STATUS_DUPLICATE = "STRUCTURAL_DUPLICATE_LINE"
RANKABLE = "CANDIDATE_RANKABLE"


def _get(r, k, default=None):
    v = r.get(k, default) if hasattr(r, "get") else default
    return default if (v is None or v != v) else v


def _f(v):
    try:
        if v is None or v != v:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_preferred():
    try:
        with open(PREFERRED_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("issuers", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_preferred(chosen, path=None):
    """Persist the resolved choices so a decision made once is not remade every month.
    Existing PINNED entries are never overwritten by a computed one."""
    path = path or PREFERRED_PATH
    existing = {}
    try:
        with open(path, encoding="utf-8") as f:
            existing = (json.load(f) or {}).get("issuers", {})
    except Exception:
        pass
    for key, rec in chosen.items():
        if existing.get(key, {}).get("pinned"):
            continue
        existing[key] = rec
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_meta": {
            "purpose": "One ranked line per issuer. `pinned: true` entries are Raj's decisions "
                       "and are NEVER overwritten by the computed preference.",
            "pin_how": "set pinned:true and ticker:<the line to rank>, with pinned_reason",
        }, "issuers": existing}, f, indent=2, sort_keys=True)
    return existing


def apply(rows, get=None, persist=True, preferred=None):
    """Mark non-common and duplicate lines in place. Returns a report; mutates `final_status`.

    Only rows currently CANDIDATE_RANKABLE are touched — a name already rejected by a gate keeps
    the reason it was already given, because overwriting it would destroy the more informative
    fact.
    """
    from security_type import classify_security_type
    from universe_hygiene import normalise_issuer
    g = get or (lambda r, k: _get(r, k))

    rep = {"non_common_excluded": [], "duplicates_excluded": [], "issuers_resolved": 0,
           "pinned_used": [], "unresolved_ties": []}
    pinned = load_preferred() if preferred is None else preferred

    # ── Rule 1 ──────────────────────────────────────────────────────────────────────
    for r in rows:
        if str(g(r, "final_status") or "") != RANKABLE:
            continue
        st = classify_security_type(g(r, "company"), g(r, "ticker"))
        r["security_type"] = st
        if st == "non_common":
            r["final_status"] = STATUS_NON_COMMON
            r["exclusion_reason"] = ("non-common instrument (preferred/depositary wrapper, "
                                     "tangible equity unit or note) — not growth equity")
            rep["non_common_excluded"].append(
                {"ticker": g(r, "ticker"), "company": str(g(r, "company"))[:70]})

    # ── Rule 2 ──────────────────────────────────────────────────────────────────────
    groups = {}
    for r in rows:
        if str(g(r, "final_status") or "") != RANKABLE:
            continue
        key = normalise_issuer(g(r, "company"))
        if key:
            groups.setdefault(key, []).append(r)

    chosen = {}
    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        rep["issuers_resolved"] += 1
        pin = pinned.get(key)
        winner = None
        informed_choice = False
        if pin and pin.get("ticker"):
            winner = next((r for r in grp if str(g(r, "ticker")) == pin["ticker"]), None)
            if winner is not None:
                rep["pinned_used"].append({"issuer": key, "ticker": pin["ticker"],
                                           "reason": pin.get("pinned_reason")})
        if winner is None:
            def rank_key(r):
                liq = _f(g(r, "adv_value")) or _f(g(r, "avg_volume_3m"))
                cap = _f(g(r, "mkt_cap")) or _f(g(r, "market_cap"))
                tk = str(g(r, "ticker") or "")
                return (-(liq if liq is not None else -1),
                        -(cap if cap is not None else -1), len(tk), tk)
            informed = any(_f(g(r, "adv_value")) is not None
                            or _f(g(r, "avg_volume_3m")) is not None
                            or _f(g(r, "mkt_cap")) is not None for r in grp)
            if not informed:
                # ⚑ REFUSE. With no liquidity and no market cap, the only thing separating the
                # lines is the ticker string, and collapsing on that would silently exclude a
                # line on the basis of alphabetical order. On the 24-Jul-2026 NASDAQ frame that
                # would have kept GOOG over GOOGL and Z over ZG — capital-relevant choices made
                # by string length. Dropping the WRONG line is strictly worse than keeping both:
                # a duplicate is visible and recoverable, an arbitrary exclusion is neither.
                #
                # This resolves itself from 07-Aug-2026, when `adv_value` and `mkt_cap` are
                # captured at formation. Until then the issuer is reported, unresolved, by name.
                rep["unresolved_ties"].append(
                    {"issuer": key, "lines": [str(g(r, "ticker")) for r in grp],
                     "chosen": None, "action": "NOT collapsed",
                     "note": "no liquidity or market-cap data on any line — refusing to choose. "
                             "Pin the preferred line in preferred_listing.json, or wait for a "
                             "frame carrying formation liquidity (captured from 07-Aug-2026)."})
                continue
            ordered = sorted(grp, key=rank_key)
            winner = ordered[0]
            informed_choice = True
        wt = str(g(winner, "ticker"))
        for r in grp:
            if r is winner:
                r["listing_role"] = "primary"
                continue
            r["final_status"] = STATUS_DUPLICATE
            r["listing_role"] = "secondary"
            r["exclusion_reason"] = (f"same issuer as {wt}, which is the ranked line "
                                     f"(greater traded liquidity / market cap)")
            rep["duplicates_excluded"].append(
                {"ticker": str(g(r, "ticker")), "issuer": key, "kept": wt,
                 "dropped_source": _f(g(r, "screen_source")),
                 "kept_source": _f(g(winner, "screen_source"))})
        # ⚑ ONLY an INFORMED or PINNED choice is persisted. An earlier build wrote every
        # resolution to preferred_listing.json including ones made by ticker ordering on a frame
        # that carried no liquidity data — and because a stored preference is reused
        # unconditionally, that arbitrary choice would have become permanent and invisible,
        # outliving the data gap that caused it. A guess must not be allowed to harden into a
        # decision.
        _pinned = bool(pin and pin.get("ticker"))
        if _pinned or informed_choice:
            chosen[key] = {"ticker": wt, "pinned": bool(pin and pin.get("pinned")),
                           "lines": [str(g(r, "ticker")) for r in grp],
                           "basis": "pinned" if _pinned else "liquidity/mktcap",
                           "decided_on": _dt.date.today().isoformat()}
    if persist and chosen:
        try:
            save_preferred(chosen)
        except Exception as e:
            rep["persist_error"] = f"{type(e).__name__}: {e}"
    return rep


def _selftest():
    rows = [
        {"ticker": "GOOGL", "company": "Alphabet Inc. Class A Common Stock",
         "final_status": RANKABLE, "adv_value": 9e9, "mkt_cap": 2e12, "screen_source": 71.0},
        {"ticker": "GOOG", "company": "Alphabet Inc. Class C Common Stock",
         "final_status": RANKABLE, "adv_value": 6e9, "mkt_cap": 2e12, "screen_source": 69.8},
        {"ticker": "GOOGM", "company": "Alphabet Inc. Depositary Shares representing a 1/20th "
                                       "Interest in a Share of Series A Mandatory Convertible "
                                       "Preferred Stock",
         "final_status": RANKABLE, "screen_source": 22.6},
        {"ticker": "NOVTU", "company": "Novanta Inc. Tangible Equity Units",
         "final_status": RANKABLE, "adv_value": 5e5, "screen_source": 54.1},
        {"ticker": "NOVT", "company": "Novanta Inc. Common Stock",
         "final_status": RANKABLE, "adv_value": 4e7, "screen_source": 38.6},
        {"ticker": "LBTYB", "company": "Liberty Global Ltd. Class B Common Shares",
         "final_status": RANKABLE, "adv_value": 2e5, "screen_source": 71.6},
        {"ticker": "LBTYA", "company": "Liberty Global Ltd. Class A Common Shares",
         "final_status": RANKABLE, "adv_value": 8e7, "screen_source": 58.7},
        {"ticker": "AAPL", "company": "Apple Inc. Common Stock",
         "final_status": RANKABLE, "adv_value": 1e10, "screen_source": 60.0},
        {"ticker": "FAILED", "company": "Someco Inc. Common Stock",
         "final_status": "HARD_GATE_FAIL", "screen_source": 90.0},
    ]
    rep = apply(rows, persist=False, preferred={})
    by = {r["ticker"]: r for r in rows}

    assert by["GOOGM"]["final_status"] == STATUS_NON_COMMON, "preferred wrapper not excluded"
    assert by["NOVTU"]["final_status"] == STATUS_NON_COMMON, "tangible equity unit not excluded"
    assert by["NOVT"]["final_status"] == RANKABLE, \
        "the actual company must survive when its wrapper is removed"
    # ⚑ the specific defect: the wrapper outranked the company. After Rule 1 it cannot.
    assert by["NOVTU"]["final_status"] != RANKABLE and by["NOVT"]["final_status"] == RANKABLE

    assert by["GOOGL"]["final_status"] == RANKABLE and by["GOOGL"]["listing_role"] == "primary", \
        "the most liquid Alphabet line must be the ranked one"
    assert by["GOOG"]["final_status"] == STATUS_DUPLICATE, "second Alphabet line not collapsed"
    # liquidity must beat SCORE — the whole point. LBTYB scores higher and is far less liquid.
    assert by["LBTYA"]["final_status"] == RANKABLE, "liquid A class must be the ranked line"
    assert by["LBTYB"]["final_status"] == STATUS_DUPLICATE, \
        "higher-scoring but illiquid B class must NOT be the ranked line"

    assert by["AAPL"]["final_status"] == RANKABLE, "a single-line issuer must be untouched"
    assert by["FAILED"]["final_status"] == "HARD_GATE_FAIL", \
        "an already-rejected row must keep its original, more informative reason"
    assert all("exclusion_reason" in r for r in rows
               if r["final_status"] in (STATUS_NON_COMMON, STATUS_DUPLICATE)), \
        "every exclusion must carry a reason"
    assert len(rep["non_common_excluded"]) == 2 and len(rep["duplicates_excluded"]) == 2

    # a PINNED choice overrides the computed one, even against liquidity
    # rebuilt from scratch: `rows` has already been mutated by the call above, so reusing it
    # would test a group that no longer exists
    rows2 = [{"ticker": "LBTYB", "company": "Liberty Global Ltd. Class B Common Shares",
              "final_status": RANKABLE, "adv_value": 2e5, "screen_source": 71.6},
             {"ticker": "LBTYA", "company": "Liberty Global Ltd. Class A Common Shares",
              "final_status": RANKABLE, "adv_value": 8e7, "screen_source": 58.7}]
    rep2 = apply(rows2, persist=False,
                 preferred={"liberty global": {"ticker": "LBTYB", "pinned": True,
                                               "pinned_reason": "test pin"}})
    assert {r["ticker"]: r["final_status"] for r in rows2}["LBTYA"] == STATUS_DUPLICATE, \
        "a pinned line must win over the liquidity heuristic"
    assert rep2["pinned_used"], "pin use not reported"

    # idempotency — re-running must not cascade further exclusions
    before = [r["final_status"] for r in rows]
    apply(rows, persist=False, preferred={})
    assert [r["final_status"] for r in rows] == before, "apply() is not idempotent"

    # missing liquidity everywhere must be REPORTED, not silently resolved
    rows3 = [{"ticker": "XA", "company": "Xco Inc. Class A Common Stock", "final_status": RANKABLE},
             {"ticker": "XB", "company": "Xco Inc. Class B Common Stock", "final_status": RANKABLE}]
    rep3 = apply(rows3, persist=False, preferred={})
    assert rep3["unresolved_ties"], "an uninformed tie must be reported"
    assert all(r["final_status"] == RANKABLE for r in rows3), \
        "an uninformed tie must NOT collapse — an arbitrary exclusion is worse than a duplicate"
    assert rep3["unresolved_ties"][0]["chosen"] is None
    print("SELFTEST PASS — 18 assertions (non-common excluded x2 incl. the tangible equity unit "
          "that outranked its own company, company survives, liquidity beats score on Liberty "
          "Global and Alphabet, single-line issuer untouched, prior rejection preserved, every "
          "exclusion carries a reason, pinned overrides the heuristic, idempotent, uninformed "
          "tie reported AND refused rather than guessed)")
    return True


if __name__ == "__main__":
    sys.exit(0 if _selftest() else 1)
