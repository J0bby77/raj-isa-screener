#!/usr/bin/env python3
"""
theme_autoclassify.py - ISA-0700 P-B7 (Raj 19-Oct-2026 15:11): classify, automatically and with stored
evidence, every stock the run can hold or fund that has NO membership in the declared theme taxonomy.

Raj 15:06: "I do not think it is an efficient or effective process to rely on me to identify or agree a
stock's theme." Decision 15:11: classification is automatic; doubt resolves TOWARDS membership.

What it does (pre-run Step 8t, after step9_pre is built and before the 6.10 capital engine):
  population = held direct stocks + every ticker in step9_pre (ranked, deployable, forward-ineligible,
               broker-refused) + the engine's dossier universe when present
  unknown    = not in concentration_theme_taxonomy.json (after aliases), and not in the overlay with an
               entry younger than `refresh_days`
  for each unknown (<= max_fetch_per_run, inside fetch_time_budget_s): yfinance Ticker.info -> industry,
  sector, longBusinessSummary -> classify() -> written to the RUNTIME overlay
  Dashboard/state/theme_auto_classifications.json (data, never the signed taxonomy file).

concentration_control.load_taxonomy reads the overlay UNDER the declared taxonomy (declared always wins),
so every consumer - the capital engine's SHADOW cap evaluation, capital_destination, the AI factor line
(Raj 16:02) - sees the same classification.

classify() - deterministic, explainable, declared rules (theme_autoclassify_rules.json):
  * a theme is a membership when the company's own description carries >= 2 distinct phrases of it
    (MEDIUM when >= 3), or 1 phrase AND a MAPPED peer in the same Yahoo industry carries that theme (LOW)
  * no qualifying theme -> NO_MATERIAL_THEME (LOW); one stray phrase is kept as a weak signal only
  * no description fetched -> the name STAYS UNKNOWN (no evidence is not evidence of no theme) and is
    retried next run
Never raises into the run (R4.12).

Usage: python3 theme_autoclassify.py --selftest
       python3 theme_autoclassify.py --tickers AEVA,XYZ        (classify + write the overlay)
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
RULES_FILE = "theme_autoclassify_rules.json"
OVERLAY_REL = os.path.join("Dashboard", "state", "theme_auto_classifications.json")
SCHEMA = "theme_auto_classifications/1"
PROVIDER = "yfinance Ticker.info (Yahoo Finance quoteSummary assetProfile)"


def load_rules(here: str = HERE) -> dict:
    with open(os.path.join(here, RULES_FILE), encoding="utf-8") as f:
        return json.load(f)


def overlay_path(here: str = HERE) -> str:
    return os.path.join(here, OVERLAY_REL)


def load_overlay(here: str = HERE) -> dict:
    try:
        with open(overlay_path(here), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"schema": SCHEMA, "entries": {}}
    except (OSError, ValueError):
        return {"schema": SCHEMA, "entries": {}}


def _hits(text: str, phrases) -> list:
    t = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    out = []
    for p in phrases:
        if re.search(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])", t):
            out.append(p)
    return sorted(set(out))


def peer_industry_themes(doc: dict) -> dict:
    ind = {}
    for t, m in (doc.get("memberships") or {}).items():
        if (m or {}).get("state") == "MAPPED":
            i = (m.get("evidence") or {}).get("industry_at_fetch")
            if i:
                ind.setdefault(i, set()).update(x["theme"] for x in m.get("themes") or [])
    return {k: sorted(v) for k, v in ind.items()}


def classify(info: dict, *, rules: dict, doc: dict, peers=None, now: str = None) -> dict:
    """Pure: one company's Yahoo profile -> a taxonomy-shaped membership (or UNKNOWN when no description)."""
    summ = (info or {}).get("longBusinessSummary") or ""
    industry = (info or {}).get("industry")
    if not summ.strip():
        return {"state": "UNKNOWN", "why": "no business description returned - stays UNKNOWN, retried next run"}
    mr = rules.get("membership_rule") or {}
    nmin, nmed, npeer = (int(mr.get("member_min_distinct_hits", 2)), int(mr.get("medium_confidence_min_distinct_hits", 3)),
                         int(mr.get("peer_industry_plus_hits", 1)))
    peers = peers if peers is not None else peer_industry_themes(doc)
    peer_set = set(peers.get(industry) or [])
    declared = set((doc.get("themes") or {}).keys())
    themes, weak = [], []
    for T, kws in sorted((rules.get("keywords") or {}).items()):
        if T not in declared:
            continue
        h = _hits(summ, kws)
        if len(h) >= nmin:
            themes.append({"theme": T, "confidence": "MEDIUM" if len(h) >= nmed else "LOW",
                           "rationale": "auto: description cites %s" % ", ".join(h), "basis": "DESCRIPTION_PHRASES"})
        elif len(h) >= npeer and T in peer_set:
            themes.append({"theme": T, "confidence": "LOW",
                           "rationale": "auto: description cites %s and a MAPPED %s peer carries the theme "
                                        "(conservative default: doubt counts in)" % (", ".join(h), industry),
                           "basis": "PHRASE_PLUS_INDUSTRY_PEER"})
        elif h:
            weak.append({"theme": T, "phrases": h})
    ev = {"provider": PROVIDER, "fields": ["longBusinessSummary", "industry", "sector"],
          "fetched_at": now or datetime.datetime.utcnow().isoformat() + "Z", "industry_at_fetch": industry,
          "sector_at_fetch": (info or {}).get("sector"),
          "summary_sha256": hashlib.sha256(summ.encode("utf-8")).hexdigest(),
          "rules_version": rules.get("version"), "weak_signals": weak}
    if themes:
        return {"state": "MAPPED", "themes": themes, "evidence": ev, "assessor": "theme_autoclassify (deterministic rules)"}
    return {"state": "NO_MATERIAL_THEME", "themes": [], "confidence": "LOW",
            "rationale": "auto: no theme qualifies on the company's own description%s; the sector cap still applies"
                         % ((" (weak signals: %s)" % ", ".join(w["theme"] for w in weak)) if weak else ""),
            "evidence": ev, "assessor": "theme_autoclassify (deterministic rules)"}


def _yf_info(ticker: str) -> dict:
    import yfinance as yf
    return yf.Ticker(ticker).info or {}


def run(population, *, here: str = HERE, fetch=None, today: datetime.date = None, write: bool = True) -> dict:
    """Classify the unknown members of `population` and persist them to the overlay. Never raises."""
    out = {"schema": SCHEMA, "state": "OK", "n_population": 0, "unknown_before": [], "new": [], "failed": [],
           "skipped_budget": [], "refreshed": []}
    try:
        import concentration_control as cc
        rules = load_rules(here)
        doc = json.load(open(os.path.join(here, cc.THEME_DECLARED_FILE), encoding="utf-8"))
        if cc.validate_theme_doc(doc):
            out.update(state="UNAVAILABLE", why="declared taxonomy invalid - nothing classified (fail closed)")
            return out
        al = {cc._base(k): cc._base(v) for k, v in (doc.get("aliases") or {}).items()}
        declared = {cc._base(k) for k in doc["memberships"]}
        ov = load_overlay(here)
        ents = ov.setdefault("entries", {})
        today = today or datetime.date.today()
        horizon = int(rules.get("refresh_days", 180))
        pop = sorted({cc._base(t) for t in (population or []) if t})
        out["n_population"] = len(pop)
        todo = []
        for t in pop:
            r = al.get(t, t)
            if r in declared or t in declared:
                continue
            e = ents.get(r)
            if e and e.get("classified_on"):
                try:
                    if (today - datetime.date.fromisoformat(e["classified_on"])).days <= horizon:
                        continue
                    out["refreshed"].append(r)
                except ValueError:
                    pass
            todo.append(r)
        out["unknown_before"] = list(todo)
        fetch = fetch or _yf_info
        peers = peer_industry_themes(doc)
        t0 = time.time()
        for i, t in enumerate(todo):
            if i >= int(rules.get("max_fetch_per_run", 15)) or time.time() - t0 > float(rules.get("fetch_time_budget_s", 25)):
                out["skipped_budget"] = todo[i:]
                break
            try:
                info = fetch(t)
            except Exception as e:                                            # noqa: BLE001
                out["failed"].append({"ticker": t, "why": "%s: %s" % (type(e).__name__, str(e)[:120])})
                continue
            m = classify(info, rules=rules, doc=doc, peers=peers)
            if m["state"] == "UNKNOWN":
                out["failed"].append({"ticker": t, "why": m["why"]})
                continue
            m["classified_on"] = today.isoformat()
            ents[t] = m
            out["new"].append({"ticker": t, "state": m["state"],
                               "themes": [(x["theme"], x["confidence"]) for x in m.get("themes") or []],
                               "industry": m["evidence"].get("industry_at_fetch")})
        ov.update(schema=SCHEMA, updated_on=today.isoformat(), rules_version=rules.get("version"),
                  taxonomy_version=doc.get("version"))
        if write and (out["new"] or not os.path.exists(overlay_path(here))):
            os.makedirs(os.path.dirname(overlay_path(here)), exist_ok=True)
            tmp = overlay_path(here) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ov, f, indent=1, ensure_ascii=False)
            os.replace(tmp, overlay_path(here))
        out["n_overlay"] = len(ents)
        if out["failed"] or out["skipped_budget"]:
            out["state"] = "PARTIAL"
    except Exception as e:                                                    # noqa: BLE001
        out.update(state="ERROR", why="%s: %s" % (type(e).__name__, e))
    out["line"] = ("Theme auto-classification (ISA-0700): %d new%s%s"
                   % (len(out["new"]),
                      (" (" + "; ".join("%s %s" % (n["ticker"], ",".join("%s/%s" % x for x in n["themes"]) or "no material theme")
                                         for n in out["new"][:8]) + ")") if out["new"] else "",
                      (" - %d still UNKNOWN (no profile / budget): %s" % (len(out["failed"]) + len(out["skipped_budget"]),
                       ", ".join([f["ticker"] for f in out["failed"]] + out["skipped_budget"])[:200]))
                      if (out["failed"] or out["skipped_budget"]) else ""))
    return out


def _selftest() -> int:
    import tempfile, shutil
    fails = []

    def ok(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)
    rules = load_rules(HERE)
    doc = {"themes": {k: {} for k in rules["keywords"]},
           "memberships": {"PEER": {"state": "MAPPED", "themes": [{"theme": "AI_DATACENTER_CAPEX", "confidence": "HIGH", "rationale": "r"}],
                                    "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Communication Equipment"}}}}
    a = classify({"industry": "Electrical Equipment & Parts",
                  "longBusinessSummary": "Supplies liquid cooling and power distribution for hyperscale data centers running AI workloads."},
                 rules=rules, doc=doc, now="t")
    ok("MUST-FIRE: three AI-datacentre phrases -> MAPPED AI_DATACENTER_CAPEX MEDIUM",
       a["state"] == "MAPPED" and ("AI_DATACENTER_CAPEX", "MEDIUM") in [(x["theme"], x["confidence"]) for x in a["themes"]])
    b = classify({"industry": "Communication Equipment",
                  "longBusinessSummary": "Designs networking routers sold to telecom carriers and data centers."},
                 rules=rules, doc=doc, now="t")
    ok("MUST-FIRE (conservative default): one phrase + a MAPPED industry peer -> member LOW",
       b["state"] == "MAPPED" and b["themes"][0]["theme"] == "AI_DATACENTER_CAPEX" and b["themes"][0]["confidence"] == "LOW")
    c = classify({"industry": "Beverages - Non-Alcoholic",
                  "longBusinessSummary": "Produces coconut water and plant-based beverages sold through retail and travel outlets."},
                 rules=rules, doc=doc, now="t")
    ok("NEGATIVE CONTROL: one stray phrase ('travel'), no peer -> NO_MATERIAL_THEME LOW with the weak signal recorded",
       c["state"] == "NO_MATERIAL_THEME" and c["confidence"] == "LOW"
       and c["evidence"]["weak_signals"] and c["evidence"]["weak_signals"][0]["theme"] == "CONSUMER_TRAVEL_DEMAND")
    q = classify({"industry": "Computer Hardware", "longBusinessSummary": "Builds quantum computers using superconducting qubits."},
                 rules=rules, doc=doc, now="t")
    ok("QBTS-like: quantum computer + qubits -> QUANTUM_COMPUTING (not AI)",
       q["state"] == "MAPPED" and [x["theme"] for x in q["themes"]] == ["QUANTUM_COMPUTING"])
    ok("NEGATIVE CONTROL: no description -> UNKNOWN (no evidence is not evidence of no theme)",
       classify({"industry": "X"}, rules=rules, doc=doc)["state"] == "UNKNOWN")
    ok("word boundary: 'more' does not match a mining phrase; 'gold' does",
       _hits("we sell more gold bars", ["mining", "gold", "ore"]) == ["gold"])
    # run(): only unknowns are fetched, declared names never; a failed fetch stays UNKNOWN; overlay written
    td = tempfile.mkdtemp()
    try:
        import concentration_control as cc
        shutil.copyfile(os.path.join(HERE, RULES_FILE), os.path.join(td, RULES_FILE))
        real = json.load(open(os.path.join(HERE, cc.THEME_DECLARED_FILE), encoding="utf-8"))
        json.dump(real, open(os.path.join(td, cc.THEME_DECLARED_FILE), "w", encoding="utf-8"))
        known = sorted(real["memberships"])[0]
        calls = []

        def fake(t):
            calls.append(t)
            if t == "BADX":
                raise RuntimeError("404")
            return {"industry": "Beverages - Non-Alcoholic", "longBusinessSummary": "Makes soft drinks."}
        r = run([known, "NEWCO", "BADX"], here=td, fetch=fake, today=datetime.date(2026, 10, 31))
        ok("MUST-FIRE run: the unknown name is classified and written; the declared name is never fetched",
           [n["ticker"] for n in r["new"]] == ["NEWCO"] and known not in calls
           and "NEWCO" in load_overlay(td)["entries"])
        ok("NEGATIVE CONTROL run: a failed fetch is reported and stays UNKNOWN (PARTIAL), never NO_MATERIAL_THEME",
           r["state"] == "PARTIAL" and r["failed"][0]["ticker"] == "BADX" and "BADX" not in load_overlay(td)["entries"])
        calls.clear()
        r2 = run(["NEWCO"], here=td, fetch=fake, today=datetime.date(2026, 11, 28))
        ok("idempotent: an entry younger than refresh_days is not re-fetched", not calls and not r2["new"])
        # R5.14 five-point matrix on the refresh horizon (refresh_days, entry classified 2026-10-31)
        hz = int(rules.get("refresh_days", 180))
        d0 = datetime.date(2026, 10, 31)
        calls.clear(); run(["NEWCO"], here=td, fetch=fake, today=d0 + datetime.timedelta(days=hz - 1))
        ok("R5.14 T-1: one day inside the refresh horizon - not re-fetched", not calls)
        calls.clear(); run(["NEWCO"], here=td, fetch=fake, today=d0 + datetime.timedelta(days=hz))
        ok("R5.14 T: on the horizon day - still not re-fetched (<= horizon)", not calls)
        calls.clear(); r_t1 = run(["NEWCO"], here=td, fetch=fake, today=d0 + datetime.timedelta(days=hz + 1))
        ok("R5.14 T+1: one day past the horizon - re-fetched and re-dated", calls == ["NEWCO"] and r_t1["refreshed"] == ["NEWCO"]
           and load_overlay(td)["entries"]["NEWCO"]["classified_on"] == (d0 + datetime.timedelta(days=hz + 1)).isoformat())
        calls.clear(); run(["NEWCO"], here=td, fetch=fake, today=d0 + datetime.timedelta(days=hz + 1))
        ok("R5.14 repeat_same_occurrence: a second run the same day does not fetch again", not calls)
        calls.clear(); run(["NEWCO"], here=td, fetch=fake, today=d0 + datetime.timedelta(days=2 * hz + 2))
        ok("R5.14 next_occurrence: the next horizon crossing re-fetches", calls == ["NEWCO"])
        tax = cc.load_taxonomy(td)
        ok("consumer: load_taxonomy reads the overlay (NEWCO -> NO_MATERIAL_THEME, AUTO provenance)",
           cc._themes(tax, "NEWCO") == [] and tax["theme_provenance"]["NEWCO"]["state"] == "AUTO_NO_MATERIAL_THEME")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("theme_autoclassify selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(1 if _selftest() else 0)
    print(json.dumps(run([t for t in a.tickers.split(",") if t]), indent=1))
