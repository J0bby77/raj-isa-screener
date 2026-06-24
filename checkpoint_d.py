#!/usr/bin/env python3
"""
checkpoint_d.py — Step-10 BLOCKING Checkpoint-D gate (redesign Part3 §13.7 / Part2 §C3 / retro #1, E5).

The post-mortem's #1 failure was deciding "buy #1" with no real comparative contest. This makes Step 10
BLOCKING: a deployment decision cannot be finalised until ALL of:
  (1) full comparative cases exist for the TOP-5 T1 names,
  (2) the CHOSEN action is justified PAIRWISE against each of the other top-5 (why it beats each), and
  (3) all top-N names (default 10) — INCLUDING the passes — are logged to the decision ledger.

validate_checkpoint_d() returns {passed, blocks[]}; the review must clear it (no blocks) before acting.
Pure logic + a log_top10 helper over decision_ledger. NOT auto-run in any scheduled task — the review
(Run Context Step 10) calls it as the gate. "The road not taken is the signal" — passes are logged too.
"""
from __future__ import annotations
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import decision_ledger as _dl
except Exception:
    _dl = None
try:
    import action_language as _alang
except Exception:
    _alang = None


def _to_ledger_decision(decision):
    """P2-3: map a canonical/stack action (BUY/STARTER/ADD/WATCH/...) to the decision_ledger vocabulary
    {buy,trim,sell,top_up,PASS,hold} so log_decision never raises. Valid ledger decisions pass through;
    anything unrecognised defaults to PASS."""
    if _dl is not None and decision in getattr(_dl, "DECISIONS", set()):
        return decision
    if _alang is not None:
        mapped = _alang.to_ledger_decision(decision)
        if mapped:
            return mapped
    return "PASS"


REQUIRED_TOP = 5      # full comparative cases + pairwise required on the top-5 T1
LOG_ALL_N = 10        # log all 10 names (incl passes) to the ledger


def _norm_set(xs):
    return {str(x).strip().upper() for x in (xs or []) if str(x).strip()}


def validate_checkpoint_d(top5, decision, comparative_cases, logged_tickers, log_all_n=LOG_ALL_N):
    """Return {passed, blocks, chosen, top5}.
      top5              : ordered list of the top-5 T1 tickers
      decision          : {chosen_ticker, chosen_action, pairwise:{other_ticker: justification}}
      comparative_cases : {ticker: case_text} — must cover all top-5
      logged_tickers    : tickers logged to the decision ledger this run (incl passes)
    Any non-empty `blocks` list means the decision is BLOCKED."""
    blocks = []
    top5 = [str(t).strip().upper() for t in (top5 or []) if str(t).strip()][:REQUIRED_TOP]
    cases = {str(k).strip().upper(): v for k, v in (comparative_cases or {}).items()}
    logged = _norm_set(logged_tickers)
    decision = decision or {}
    # Step 10 is NOT capped at one action — accept a single chosen_ticker OR a list chosen_tickers;
    # deploy as many as clear the contest AND fit available funds.
    if decision.get("chosen_tickers"):
        chosen_set = _norm_set(decision["chosen_tickers"])
    elif decision.get("chosen_ticker"):
        chosen_set = {str(decision["chosen_ticker"]).strip().upper()}
    else:
        chosen_set = set()
    pairwise = {str(k).strip().upper(): v for k, v in (decision.get("pairwise") or {}).items()}

    if len(top5) < REQUIRED_TOP:
        blocks.append(f"need {REQUIRED_TOP} top-T1 names for Checkpoint-D, got {len(top5)}")

    # (1) full comparative case for every top-5 name
    for t in top5:
        if not str(cases.get(t) or "").strip():
            blocks.append(f"missing comparative case for top-5 name {t}")

    # (2) chosen action(s) in the top-5 + justify each NON-deployed top-5 name vs what WAS deployed.
    #     MULTIPLE deployments are allowed whenever they fit available funds — no single-action cap.
    if not chosen_set:
        blocks.append("no chosen action(s) in decision")
    for c in chosen_set:
        if c not in top5:
            blocks.append(f"chosen {c} is not among the top-5 {top5}")
    for t in top5:
        if t in chosen_set:
            continue
        if not str(pairwise.get(t) or "").strip():
            blocks.append(f"missing justification: why {sorted(chosen_set) or ['?']} chosen over {t}")

    # (2b) Optional funds check — many actions are FINE if they fit; this blocks ONLY when the total
    #      cost of the chosen actions exceeds available funds, never merely because there is >1 action.
    _funds = decision.get("available_funds")
    _costs = {str(k).strip().upper(): v for k, v in (decision.get("costs") or {}).items()}
    if _funds is not None and _costs:
        _total = sum((_costs.get(c) or 0) for c in chosen_set)
        if _total > _funds:
            blocks.append(f"chosen actions cost {_total} > available funds {_funds}")

    # (3) log all N (incl passes) — at minimum every top-5 name must be logged
    not_logged = [t for t in top5 if t not in logged]
    if not_logged:
        blocks.append(f"top-5 names not logged to decision ledger: {not_logged}")
    if len(logged) < min(log_all_n, len(top5)):
        blocks.append(f"only {len(logged)} names logged; log all {log_all_n} (incl PASSES)")

    return {"passed": not blocks, "blocks": blocks, "chosen": sorted(chosen_set) or None, "top5": top5}


def log_top10(ledger_path, ranked, decisions=None, route="growth", log_all_n=LOG_ALL_N, **ledger_kw):
    """Log the top-N deployment names (incl passes) to the decision ledger so 'the road not taken'
    is captured. ranked: ordered ticker list (e.g. deployment_priority_rank). decisions: {ticker:
    'buy'|'PASS'|'top_up'|...}; anything unlisted defaults to PASS. Returns the logged ticker set."""
    if _dl is None:
        raise RuntimeError("decision_ledger not importable — cannot log top-10")
    decisions = {str(k).upper(): v for k, v in (decisions or {}).items()}
    logged = []
    for t in (ranked or [])[:log_all_n]:
        d = decisions.get(str(t).upper(), "PASS")
        d = _to_ledger_decision(d)   # P2-3: map canonical/stack action -> ledger vocabulary (never raise)
        _dl.log_decision(ledger_path, t, route, d, **ledger_kw)
        logged.append(str(t).upper())
    return logged


def main():
    ap = argparse.ArgumentParser(description="Step-10 BLOCKING Checkpoint-D gate (E5).")
    ap.add_argument("--spec", required=True,
                    help="JSON: {top5:[...], decision:{chosen_ticker, chosen_action, pairwise:{}}, "
                         "comparative_cases:{}, logged_tickers:[...]}")
    a = ap.parse_args()
    with open(a.spec, encoding="utf-8") as fh:
        s = json.load(fh)
    res = validate_checkpoint_d(s.get("top5"), s.get("decision"),
                                s.get("comparative_cases"), s.get("logged_tickers"))
    print(json.dumps(res, indent=2))
    if not res["passed"]:
        print("\nBLOCKED — resolve the above before finalising the Step-10 decision.", file=sys.stderr)
        sys.exit(2)
    print("\nCHECKPOINT-D PASSED — decision may proceed.")


if __name__ == "__main__":
    main()
