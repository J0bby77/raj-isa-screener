#!/usr/bin/env python3
"""
action_language.py — canonical action vocabulary (redesign Proposal §10, E7).

The post-mortem flagged AMBIGUOUS action labels — "Buy Now Candidate", "Buy Now — Confirm Entry",
"Buy on Pullback", "Hold / Watch" — that blur the line between *an instruction* and *a status*, and
that differ between the action stack (BUY/STARTER/TOP_UP/...) and the Step-9 decision buckets. This
module is the SINGLE source of the unambiguous vocabulary, plus a normaliser that maps any legacy /
stack / VCI label onto it. Pure helper — additive; callers add `canonical_action` / `action_label`
fields without changing their existing labels.

Canonical actions — each is an unambiguous INSTRUCTION:
  BUY    full new position now
  START  partial/starter position now (divergence or entry-not-confirmed → size-capped, confirm at review)
  ADD    top-up an existing holding (only above the fresh-capital bar; never average down)
  TRIM   reduce an existing holding (below hold-floor / better use of capital)
  SELL   exit (disqualifier, thesis-break, or dead money with a funded replacement)
  HOLD   retain, no action (above hold-floor, below add-bar) — a STATUS, never in the action stack
  WATCH  no position, not yet eligible — monitor for entry/catalyst (a STATUS)
"""

CANONICAL_ACTIONS = ["BUY", "START", "ADD", "TRIM", "SELL", "HOLD", "WATCH"]

DESCRIPTIONS = {
    "BUY":   "Buy now — full position. Eligible, at/within entry, no disqualifier.",
    "START": "Start a partial position now — divergence (estimates up / price down) or entry not yet "
             "confirmed; size-capped, confirm at review.",
    "ADD":   "Add to an existing holding — only above the fresh-capital bar; never average down.",
    "TRIM":  "Reduce an existing holding — score below the hold-floor or capital better used elsewhere.",
    "SELL":  "Exit — disqualifier, thesis-break, or dead money with a funded replacement.",
    "HOLD":  "Retain, no action — above the hold-floor, below the add-bar.",
    "WATCH": "No position, not yet eligible — monitor for entry or catalyst.",
}

# Aliases: action-stack actions + legacy Step-9 decision buckets + VCI buckets -> canonical.
_ALIASES = {
    # action-stack
    "TOP_UP": "ADD", "TOPUP": "ADD", "STARTER": "START",
    # legacy main-watchlist decision buckets
    "BUY NOW CANDIDATE": "BUY",
    "BUY NOW": "BUY",
    "BUY NOW — CONFIRM ENTRY (PROVISIONAL)": "START",
    "BUY NOW - CONFIRM ENTRY (PROVISIONAL)": "START",
    "BUY NOW — PROBATION (SCORE 60-69)": "START",
    "BUY NOW - PROBATION (SCORE 60-69)": "START",
    "BUY ON PULLBACK": "WATCH",
    "BUY ON PULLBACK (PROVISIONAL ENTRY)": "WATCH",
    "HOLD / WATCH": "HOLD",
    "REMOVE / REJECT": "SELL",
    "RE-SCORE REQUIRED": "WATCH",
    "THESIS REVIEW REQUIRED": "SELL",
    "ENTRY LEVEL REQUIRED": "WATCH",
    # VCI buckets
    "DEPLOY NOW (ASYMMETRIC)": "BUY",
    "MONITOR ENTRY (ASYMMETRIC)": "WATCH",
    "WATCH (ASYMMETRIC)": "WATCH",
    "BELOW VCI THRESHOLD": "WATCH",
}


def normalize_action(label):
    """Map any action-stack action, legacy decision-bucket, or VCI label to a canonical action.
    Returns the canonical string (one of CANONICAL_ACTIONS) or None if unrecognised."""
    if not label:
        return None
    s = str(label).strip().upper()
    if s in DESCRIPTIONS:
        return s
    return _ALIASES.get(s)


def describe(action):
    """One-line description of a canonical action (None if unknown)."""
    return DESCRIPTIONS.get(str(action or "").strip().upper())


def label_for(action):
    """Human display label 'ACTION — description' for emails / Run Context, or the raw input
    if it can't be normalised (so nothing is silently dropped)."""
    canon = normalize_action(action)
    if canon is None:
        return str(action) if action is not None else ""
    return f"{canon} — {DESCRIPTIONS[canon]}"


# Canonical/stack action -> decision_ledger vocabulary {buy, top_up, trim, sell, hold, PASS} (M1).
# A STARTER/START is a (partial) buy; ADD/TOP_UP is a top-up; WATCH is the road-not-taken (PASS).
_LEDGER_DECISION = {
    "BUY": "buy", "START": "buy", "ADD": "top_up", "TRIM": "trim",
    "SELL": "sell", "HOLD": "hold", "WATCH": "PASS",
}


def to_ledger_decision(action):
    """Map any canonical / action-stack action to the decision_ledger decision vocabulary so logging
    never raises (decision_ledger only accepts buy/trim/sell/top_up/PASS/hold). None if unrecognised."""
    canon = normalize_action(action)
    return _LEDGER_DECISION.get(canon) if canon else None
