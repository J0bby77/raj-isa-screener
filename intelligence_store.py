#!/usr/bin/env python3
"""
intelligence_store.py — Shared intelligence store. Dashboard spec §7.7.

THE GAP THIS CLOSES
-------------------
The mid-month brief writes `project_isa_preprocess_[mmm_yyyy].md` to memory plus an email.
The monthly review's Step 3 reads the remaining emails and its findings land only in the
review email. Nothing accumulates: the second pass REPLACES the first rather than extending
it, so a signal seen on the 10th is gone by the 1st.

This store is APPEND-ONLY within a month. The monthly pass adds items and may mark an earlier
item `superseded_by` — with a reason — but it can never rewrite or delete one. A record you
can quietly revise is not a record.

  item = {id, date, source_name, source_tier(1|2|3), pass, tickers[], funds[],
          category, summary, thesis_impact, confidence, links[], superseded_by?}

CLI:
  python3 intelligence_store.py --month sep_2026 --add items.json --pass midmonth
  python3 intelligence_store.py --month sep_2026 --supersede ID --by ID --reason "..."
  python3 intelligence_store.py --month sep_2026 --report
  python3 intelligence_store.py --selftest
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1
PASSES = {"midmonth", "monthly_step3"}
CATEGORIES = {"thesis_evidence", "macro", "new_candidate", "risk_flag"}
IMPACTS = {"reinforcing", "neutral", "weakening"}
TIERS = {1, 2, 3}


class AppendOnlyViolation(RuntimeError):
    """Raised when a write would alter or remove an item that already exists."""


def path_for(month_label, here=None):
    return os.path.join(here or HERE, f"intelligence_{month_label}.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def new_doc(month_label):
    return {"schema_version": SCHEMA_VERSION, "month": month_label,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "items": [], "supersessions": []}


def item_id(it):
    """Deterministic, content-derived: the same finding submitted twice is the same item, so a
    re-run of a pass cannot inflate the record."""
    basis = "|".join([str(it.get("date", "")), str(it.get("source_name", "")),
                      str(it.get("pass", "")), str(it.get("summary", ""))[:200]])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def validate_item(it, idx=0):
    errs = []
    tag = f"item[{idx}]"
    for f in ("date", "source_name", "summary"):
        if not str(it.get(f) or "").strip():
            errs.append(f"{tag}: {f} is required")
    if it.get("source_tier") not in TIERS:
        errs.append(f"{tag}: source_tier {it.get('source_tier')!r} not in {sorted(TIERS)}")
    if it.get("pass") not in PASSES:
        errs.append(f"{tag}: pass {it.get('pass')!r} not in {sorted(PASSES)}")
    if it.get("category") not in CATEGORIES:
        errs.append(f"{tag}: category {it.get('category')!r} not in {sorted(CATEGORIES)}")
    if it.get("thesis_impact") not in IMPACTS:
        errs.append(f"{tag}: thesis_impact {it.get('thesis_impact')!r} not in {sorted(IMPACTS)}")
    if not isinstance(it.get("tickers", []), list) or not isinstance(it.get("funds", []), list):
        errs.append(f"{tag}: tickers and funds must be lists")
    if not (it.get("tickers") or it.get("funds")) and it.get("category") != "macro":
        errs.append(f"{tag}: only a 'macro' item may name no ticker and no fund — otherwise "
                    f"the finding cannot be joined to anything")
    c = it.get("confidence")
    if c is not None and not (isinstance(c, (int, float)) and 0 <= float(c) <= 1):
        errs.append(f"{tag}: confidence must be null or 0..1, got {c!r}")
    return errs


def add(month_label, items, pass_name=None, here=None, strict=True):
    """Append. Existing items are NEVER modified; a duplicate id is skipped, not overwritten."""
    p = path_for(month_label, here)
    doc = _load(p) or new_doc(month_label)
    before = {i["id"]: json.dumps(i, sort_keys=True) for i in doc["items"]}
    errs, added, dupes = [], [], []
    for i, raw in enumerate(items):
        it = dict(raw)
        if pass_name:
            it.setdefault("pass", pass_name)
        it.setdefault("tickers", [])
        it.setdefault("funds", [])
        it.setdefault("links", [])
        it.setdefault("confidence", None)
        e = validate_item(it, i)
        if e:
            errs.extend(e)
            continue
        it["id"] = item_id(it)
        it["recorded_at"] = datetime.now().isoformat(timespec="seconds")
        if it["id"] in before:
            dupes.append(it["id"])
            continue
        doc["items"].append(it)
        added.append(it["id"])
    if errs and strict:
        raise ValueError("intelligence items failed validation:\n  - " + "\n  - ".join(errs))
    after = {i["id"]: json.dumps(i, sort_keys=True) for i in doc["items"]}
    for k, v in before.items():
        if after.get(k) != v:
            raise AppendOnlyViolation(f"item {k} was modified — the store is append-only")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return {"added": added, "duplicates_skipped": dupes, "errors": errs, "total": len(doc["items"])}


def supersede(month_label, old_id, by_id, reason, here=None):
    """Mark an earlier item superseded. The original text is left EXACTLY as written."""
    if not str(reason or "").strip():
        raise ValueError("a supersession requires a reason — without one it is indistinguishable "
                         "from deleting an inconvenient finding")
    p = path_for(month_label, here)
    doc = _load(p)
    if not doc:
        raise FileNotFoundError(p)
    ids = {i["id"] for i in doc["items"]}
    for i in (old_id, by_id):
        if i not in ids:
            raise ValueError(f"unknown item id {i!r}")
    if old_id == by_id:
        raise ValueError("an item cannot supersede itself")
    for it in doc["items"]:
        if it["id"] == old_id:
            # the ONLY mutation permitted, and it is additive
            it["superseded_by"] = by_id
    doc["supersessions"].append({"superseded": old_id, "by": by_id, "reason": reason.strip(),
                                 "ts": datetime.now().isoformat(timespec="seconds")})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return doc


def report(month_label, here=None):
    doc = _load(path_for(month_label, here))
    if not doc:
        return {"month": month_label, "exists": False}
    items = doc["items"]
    live = [i for i in items if not i.get("superseded_by")]
    by = lambda k: {v: sum(1 for i in items if i.get(k) == v) for v in sorted(
        {str(i.get(k)) for i in items}) if v != "None"}
    tick = {}
    for i in items:
        for t in i.get("tickers", []):
            tick.setdefault(t, []).append(i["id"])
    tiers = {}
    for i in items:
        tiers.setdefault(i.get("source_tier"), {"items": 0, "actionable": 0})
        tiers[i.get("source_tier")]["items"] += 1
        if i.get("category") in ("thesis_evidence", "new_candidate", "risk_flag"):
            tiers[i.get("source_tier")]["actionable"] += 1
    return {"month": month_label, "exists": True, "total": len(items), "live": len(live),
            "superseded": len(items) - len(live), "by_pass": by("pass"),
            "by_category": by("category"), "by_impact": by("thesis_impact"),
            "tickers_covered": len(tick),
            "source_tier_coverage": tiers,
            "note": "source_tier_coverage answers which tiers actually produced signal — the "
                    "evidence for maintaining the tier list rather than assuming it."}


def _selftest():
    import tempfile
    fails = []

    def ok(l, c):
        print(f"  {'PASS' if c else 'FAIL'}  {l}")
        if not c:
            fails.append(l)

    with tempfile.TemporaryDirectory() as td:
        base = {"date": "2026-09-10", "source_name": "Broker note", "source_tier": 1,
                "tickers": ["COCO"], "funds": [], "category": "thesis_evidence",
                "summary": "Guidance raised for the second time this year.",
                "thesis_impact": "reinforcing", "confidence": 0.7}
        r = add("sep_2026", [base], pass_name="midmonth", here=td)
        ok("midmonth item is added", len(r["added"]) == 1)
        r2 = add("sep_2026", [base], pass_name="midmonth", here=td)
        ok("re-running the same pass does NOT duplicate", r2["added"] == [] and r2["duplicates_skipped"])
        m = dict(base, pass_name=None, summary="Monthly pass: guidance confirmed at Q3.",
                 date="2026-10-01")
        m["pass"] = "monthly_step3"
        r3 = add("sep_2026", [m], here=td)
        ok("the monthly pass EXTENDS rather than replaces",
           len(r3["added"]) == 1 and r3["total"] == 2)

        doc = _load(path_for("sep_2026", td))
        first, second = doc["items"][0]["id"], doc["items"][1]["id"]
        supersede("sep_2026", first, second, "Q3 print confirmed the raise", here=td)
        doc2 = _load(path_for("sep_2026", td))
        ok("supersession is additive — the original text is untouched",
           doc2["items"][0]["summary"] == base["summary"]
           and doc2["items"][0]["superseded_by"] == second)
        ok("the supersession carries its reason", doc2["supersessions"][0]["reason"])

        try:
            supersede("sep_2026", first, second, "", here=td); bad = False
        except ValueError:
            bad = True
        ok("a supersession with no reason is REFUSED", bad)

        try:
            add("sep_2026", [dict(base, source_tier=9)], pass_name="midmonth", here=td)
            bad = False
        except ValueError:
            bad = True
        ok("an unknown source_tier is refused", bad)
        try:
            add("sep_2026", [dict(base, tickers=[], funds=[], category="thesis_evidence")],
                pass_name="midmonth", here=td)
            bad = False
        except ValueError:
            bad = True
        ok("a non-macro item naming nothing is refused (it could never be joined)", bad)
        ok("a macro item MAY name nothing",
           len(add("sep_2026", [dict(base, tickers=[], funds=[], category="macro",
                                     summary="BoE holds; energy risk skewed up.")],
                   pass_name="monthly_step3", here=td)["added"]) == 1)

        rep = report("sep_2026", td)
        ok("report separates live from superseded", rep["live"] == rep["total"] - 1)
        ok("report exposes source-tier coverage", 1 in rep["source_tier_coverage"])

    print("\n" + ("INTELLIGENCE STORE SELFTEST PASS" if not fails
                  else f"INTELLIGENCE STORE SELFTEST FAIL {fails}"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month")
    ap.add_argument("--add", metavar="ITEMS_JSON")
    ap.add_argument("--pass", dest="pass_name", choices=sorted(PASSES))
    ap.add_argument("--supersede"); ap.add_argument("--by"); ap.add_argument("--reason")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.month:
        ap.error("--month required")
    if a.add:
        with open(a.add, encoding="utf-8") as f:
            items = json.load(f)
        r = add(a.month, items if isinstance(items, list) else items.get("items", []),
                pass_name=a.pass_name)
        print(f"INTELLIGENCE_ADD month={a.month} added={len(r['added'])} "
              f"dupes={len(r['duplicates_skipped'])} total={r['total']}")
        return 0
    if a.supersede:
        supersede(a.month, a.supersede, a.by, a.reason or "")
        print(f"superseded {a.supersede} by {a.by}")
        return 0
    if a.report:
        print(json.dumps(report(a.month), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
