#!/usr/bin/env python3
"""
ai_disruption.py — AI-disruption score (redesign Part 1 §3.1, E4).

A judgement assessment, NOT a mechanical one: for each name the review records, quarterly, how
exposed the business is to AI disruption on a 0–5 scale WITH a written justification. The score is
durable (carried between runs) and re-assessed each quarter. A confirmed existential score (5) is a
hard disqualifier that the deployment_flags layer turns into an `ai_existential` cap; 4 is surfaced
for review. NOT wired into any scheduled run — additive store + helpers the review/learning step uses.

Scale (0–5):
  0 AI tailwind / clear beneficiary
  1 minimal exposure
  2 some disruption risk, manageable
  3 material risk to part of the business
  4 severe — a core revenue stream is threatened   (-> review flag)
  5 existential — business model structurally obsoleted by AI   (-> ai_existential disqualifier)

Store schema (ai_disruption.json):
  { schema_version, assessments: { TICKER: {score, justification, date, assessor, horizon} } }
"""
from __future__ import annotations
import argparse, json, os, datetime

SCHEMA_VERSION = "1.0"
AI_EXISTENTIAL_SCORE = 5      # hard disqualifier (ai_existential)
AI_SEVERE_SCORE = 4           # surfaced review flag
REASSESS_AFTER_DAYS = 92      # quarterly cadence — older than this -> stale, re-assess

SCALE = {
    0: "AI tailwind / beneficiary",
    1: "minimal exposure",
    2: "some disruption risk, manageable",
    3: "material risk to part of the business",
    4: "severe — core revenue stream threatened",
    5: "existential — business model structurally obsoleted by AI",
}


def _today() -> str:
    return datetime.date.today().isoformat()


def load_store(path: str) -> dict:
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "assessments": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "assessments": {}}
    if isinstance(d, dict) and isinstance(d.get("assessments"), dict):
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d
    return {"schema_version": SCHEMA_VERSION, "assessments": {}}


def save_store(store: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, default=str)
    os.replace(tmp, path)


def record_assessment(path, ticker, score, justification, assessor="review",
                      horizon="3-5yr", date=None) -> dict:
    """Record/replace a ticker's AI-disruption assessment. Requires a written justification —
    a judgement score with no reasoning is rejected (the whole point is the written case)."""
    score = int(score)
    if score not in SCALE:
        raise ValueError(f"score must be 0-5, got {score}")
    if not (justification or "").strip():
        raise ValueError("a written justification is required for an AI-disruption assessment")
    store = load_store(path)
    entry = {
        "score": score,
        "scale_label": SCALE[score],
        "justification": justification.strip(),
        "assessor": assessor,
        "horizon": horizon,
        "date": date or _today(),
    }
    store["assessments"][(ticker or "").upper()] = entry
    save_store(store, path)
    return entry


def _is_stale(date_str) -> bool:
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(str(date_str)[:10])).days
        return age > REASSESS_AFTER_DAYS
    except Exception:
        return False


def get_assessment(path, ticker) -> dict | None:
    """Return the assessment dict (with a `stale` flag) or None if never assessed."""
    a = load_store(path)["assessments"].get((ticker or "").upper())
    if a is None:
        return None
    a = dict(a)
    a["stale"] = _is_stale(a.get("date"))
    return a


def disqualifier_flags(path, ticker) -> list:
    """Disqualifier flags from the recorded assessment: ['ai_existential'] when score==5."""
    a = get_assessment(path, ticker)
    if a and int(a.get("score", 0)) >= AI_EXISTENTIAL_SCORE:
        return ["ai_existential"]
    return []


def review_flags(path, ticker) -> list:
    """Surfaced (non-capping) flags: severe (4) -> review; stale -> re-assess this quarter."""
    out = []
    a = get_assessment(path, ticker)
    if not a:
        return out
    if int(a.get("score", 0)) == AI_SEVERE_SCORE:
        out.append("ai_disruption_severe")
    if a.get("stale"):
        out.append("ai_disruption_reassess_due")
    return out


def flags_from_score(score) -> list:
    """Stateless helper for callers that already carry an ai_disruption_score field
    (e.g. deployment_flags): ['ai_existential'] when score>=5, else []."""
    try:
        return ["ai_existential"] if int(score) >= AI_EXISTENTIAL_SCORE else []
    except (TypeError, ValueError):
        return []


def default_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "ai_disruption.json")


def main():
    ap = argparse.ArgumentParser(description="AI-disruption assessment store (E4).")
    ap.add_argument("--path", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record an assessment")
    rec.add_argument("--ticker", required=True)
    rec.add_argument("--score", type=int, required=True, help="0-5")
    rec.add_argument("--justification", required=True)
    rec.add_argument("--horizon", default="3-5yr")
    g = sub.add_parser("get", help="get a ticker's assessment")
    g.add_argument("--ticker", required=True)
    sub.add_parser("list", help="list all assessments (with staleness)")

    a = ap.parse_args()
    if a.cmd == "record":
        e = record_assessment(a.path, a.ticker, a.score, a.justification, horizon=a.horizon)
        print(f"RECORDED {a.ticker.upper()} score={e['score']} ({e['scale_label']})")
    elif a.cmd == "get":
        e = get_assessment(a.path, a.ticker)
        print(json.dumps(e, indent=2) if e else f"NO_ASSESSMENT for {a.ticker.upper()}")
    elif a.cmd == "list":
        store = load_store(a.path)
        for t, e in sorted(store["assessments"].items()):
            print(f"{t:8} {e['score']}  {'STALE' if _is_stale(e.get('date')) else 'ok':5}  {e['scale_label']}")


if __name__ == "__main__":
    main()
