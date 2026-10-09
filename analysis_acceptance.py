#!/usr/bin/env python3
"""
analysis_acceptance.py - ISA-0829 (R13.7, 04-Oct-2026): the ONE home of the ANALYSIS_ACCEPTANCE contract,
its receipts' reader and their currency. discussion_preflight.accept_analysis() WRITES receipts (it needs the
Trusted baseline); everything that READS them - isa_register's BUILD_READY gate, release_gate's
analysis_binding gate, the Rationale Ledger's form clause - imports THIS light module (stdlib only at import).

CLI: python3 discussion_preflight.py --analysis <analysis.json> [--write]   (accept / refuse)
     python3 analysis_acceptance.py --currency <analysis_id> [--fingerprint <sha>]
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
UNKNOWN = "UNKNOWN"


def _today() -> str:
    return datetime.date.today().isoformat()


# ════════════════════════════════════════════════════════════════════════════════════════
# ISA-0829 (R13.7 / R3.11 / R8.4-R8.6 / R12.3 / R19.3, 04-Oct-2026) — ANALYSIS ACCEPTANCE
# ════════════════════════════════════════════════════════════════════════════════════════
# An ORIENTATION receipt proves the speaker KNEW THE FRAMEWORK. It says nothing about whether an
# ANALYSIS is fit to become a recommendation, a BUILD_READY item or a BuildSpec's authority. Until
# 04-Oct-2026 no artefact carried the answers R2.15-R2.18 / R3 / R8.4-R8.6 / R13 / R16 ask for, so no
# check could refuse their absence: ISA-0333 stood BUILD_READY on a 16-Aug corrective action after the
# 28-Sep adjudication replaced its target state, and ISA-0740 on a SHADOW ceiling after the LIVE engine
# began computing the economic maximum. This contract is the machine-readable answer sheet.
#   * every field is either answered or `{"na": "<reason>"}` where N/A is admissible; silence REFUSES;
#   * the receipt binds the exact content (sha256 fingerprint) and the bytes the analysis rests on
#     (anchor digests of the modules/config/capabilities/parameters it names), so it goes STALE by
#     itself when its revalidate_by passes or an intersecting change lands - never by someone noticing;
#   * QUALITY stays JUDGEMENT (§17 honesty): the contract proves the questions were answered and the
#     arithmetic of the population bridge conserves, not that the answers are right.

ANALYSIS_RECEIPT_REL = os.path.join("Dashboard", "state", "analysis_receipts")
ANALYSIS_ACCEPTED, ANALYSIS_REFUSED = "ACCEPTED", "REFUSED"
CONCLUSION_STATES = ("RECOMMEND", "NO_CHANGE", "INSUFFICIENT_EVIDENCE", "REFUSED_FOR_POWER",
                     "ACCUMULATE_DATA_FIRST")
ML_DISPOSITIONS = ("NOT_SUITABLE", "DEFER_ACCUMULATE_DATA", "RESEARCH_CANDIDATE", "BUILD_CANDIDATE")  # R8.6 - one vocabulary
ML_PARADIGMS = ("SUPERVISED", "UNSUPERVISED", "REINFORCEMENT", "OTHER")
HISTORY_STATES = ("AVAILABLE_NOW", "BACKFILLABLE", "ACCUMULATE_ONLY", "NOT_APPLICABLE")
POPULATION_STATES = ("ANALYSED", "EXCLUDED", "UNRESOLVED", "INELIGIBLE")
# field -> "ALWAYS" (must be answered) | "NA_OK" (answered, or {"na": reason})
ANALYSIS_FIELDS = {
    "analysis_id": "ALWAYS", "version": "ALWAYS", "as_of": "ALWAYS", "materiality": "ALWAYS",
    "question": "ALWAYS", "north_star_link": "ALWAYS", "baseline_and_consumer": "ALWAYS",
    "evidence_manifest": "ALWAYS", "pit_basis": "NA_OK", "population_bridge": "NA_OK",
    "method": "ALWAYS", "no_change_baseline": "NA_OK", "alternatives": "NA_OK",
    "assumptions_uncertainties": "ALWAYS", "power_and_limits": "NA_OK",
    "specification_search": "NA_OK", "sensitivity_robust_region": "NA_OK",
    "economic_significance_frictions": "NA_OK", "falsifiers": "ALWAYS",
    "capital_consequence": "NA_OK", "conclusion": "ALWAYS", "revalidation": "ALWAYS",
    "learning": "ALWAYS", "ml_suitability": "ALWAYS", "parameters_in_scope": "ALWAYS",
    "parameterisation": "NA_OK", "unknowns": "ALWAYS",
}
_MIN_NA = 20


def _is_na(v) -> bool:
    return isinstance(v, dict) and set(v) == {"na"}


def _na_ok(v) -> bool:
    return _is_na(v) and isinstance(v["na"], str) and len(v["na"].strip()) >= _MIN_NA


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def analysis_contract_gaps(doc: dict) -> List[str]:
    """Every reason this analysis may NOT become decision-complete. [] = acceptable. Pure."""
    g: List[str] = []
    if not isinstance(doc, dict):
        return ["R13.7: the analysis is not a JSON object"]
    for f, rule in ANALYSIS_FIELDS.items():
        v = doc.get(f)
        if f == "unknowns" or f == "parameters_in_scope":
            if not isinstance(v, list):
                g.append("R13.7: `%s` must be a list (an empty list is an answer; silence is not)" % f)
            continue
        if _is_na(v):
            if rule == "ALWAYS":
                g.append("R13.7: `%s` cannot be N/A - it is mandatory for every material analysis" % f)
            elif not _na_ok(v):
                g.append("R13.7: `%s` is N/A without a reason (>= %d chars) - N/A requires a reason" % (f, _MIN_NA))
            continue
        if _empty(v):
            g.append("R13.7: `%s` is absent - %s" % (f, "answer it or give {\"na\": reason}" if rule == "NA_OK"
                                                       else "mandatory; silence is not N/A"))
    if g:
        pass
    if doc.get("materiality") not in (None, "MATERIAL") and not _is_na(doc.get("materiality")):
        g.append("R13.7: materiality %r - only a MATERIAL analysis is accepted here" % doc.get("materiality"))
    ns = doc.get("north_star_link")
    if isinstance(ns, dict) and not _is_na(ns):
        for k in ("objective", "decision", "capital_link"):
            if _empty(ns.get(k)):
                g.append("R16.1/R16.4: north_star_link.%s is absent - the analysis must join the economic chain "
                         "(objective -> decision -> capital), or say INFRASTRUCTURE explicitly" % k)
    elif ns is not None and not _is_na(ns):
        g.append("R16.4: north_star_link must be {objective, decision, capital_link}")
    em = doc.get("evidence_manifest")
    if isinstance(em, list):
        for i, e in enumerate(em):
            if not isinstance(e, dict) or _empty(e.get("source")) or _empty(e.get("as_of")):
                g.append("R4.2: evidence_manifest[%d] needs source AND as_of" % i)
    alts = doc.get("alternatives")
    if not _is_na(alts) and alts is not None:
        if not isinstance(alts, list) or not all(isinstance(a, dict) and a.get("name") and a.get("assessment") for a in alts):
            g.append("R2.17: alternatives must be [{name, assessment}, ...] or N/A with a reason")
    pb = doc.get("population_bridge")
    if isinstance(pb, dict) and not _is_na(pb):
        exp = pb.get("expected")
        disp = pb.get("dispositions") or {}
        if not isinstance(exp, list) or not exp:
            g.append("R3.11(a): population_bridge.expected must list the expected population's identities")
        else:
            missing = [i for i in exp if i not in disp]
            if missing:
                g.append("R3.11(a): %d expected identit(ies) have NO disposition between expected and analysed: %s - "
                         "an entity cannot vanish from a population (FC-I)" % (len(missing), missing[:8]))
            foreign = [i for i in disp if i not in exp]
            if foreign:
                g.append("R3.11(a): dispositions name identities outside the expected population: %s" % foreign[:8])
            for i, d in disp.items():
                st = (d or {}).get("state") if isinstance(d, dict) else None
                if st not in POPULATION_STATES:
                    g.append("R3.11(a): %s disposition %r not in %s" % (i, st, POPULATION_STATES))
                elif st != "ANALYSED" and _empty((d or {}).get("reason")):
                    g.append("R3.11(a): %s is %s with no reason" % (i, st))
    ss = doc.get("specification_search")
    if isinstance(ss, dict) and not _is_na(ss):
        for k in ("family", "multiple_testing"):
            if _empty(ss.get(k)):
                g.append("R3.11(b): specification_search.%s is absent (the searched family and its multiple-testing "
                         "treatment)" % k)
    es = doc.get("economic_significance_frictions")
    if isinstance(es, dict) and not _is_na(es):
        for k in ("statistical", "economic", "frictions"):
            if _empty(es.get(k)):
                g.append("R3.11(d): economic_significance_frictions.%s is absent" % k)
    con = doc.get("conclusion")
    if isinstance(con, dict):
        if con.get("state") not in CONCLUSION_STATES:
            g.append("R13.7: conclusion.state %r is not one of %s (decision-complete or evidence-based refusal)"
                     % (con.get("state"), CONCLUSION_STATES))
        if _empty(con.get("statement")) or _empty(con.get("uncertainty")):
            g.append("R13.7: conclusion needs statement AND uncertainty")
        if con.get("state") == "RECOMMEND" and not _is_na(alts) and _empty(con.get("why_dominates_alternatives")):
            g.append("R2.17: a RECOMMEND conclusion must say why it dominates the alternatives")
    elif con is not None:
        g.append("R13.7: conclusion must be {state, statement, uncertainty, why_dominates_alternatives}")
    rv = doc.get("revalidation")
    if isinstance(rv, dict):
        rb = str(rv.get("revalidate_by") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rb):
            g.append("R13.7: revalidation.revalidate_by must be an ISO date (analysis currency)")
        elif doc.get("as_of") and rb < str(doc.get("as_of")):
            g.append("R13.7: revalidate_by %s precedes as_of %s" % (rb, doc.get("as_of")))
        if not isinstance(rv.get("triggers"), list) or not rv.get("triggers"):
            g.append("R13.7: revalidation.triggers must name at least one event that invalidates the analysis")
        it = rv.get("intersects")
        if not isinstance(it, dict) or not all(isinstance(it.get(k), list) for k in ("modules", "config", "capabilities", "parameters")):
            g.append("R13.7: revalidation.intersects must declare modules/config/capabilities/parameters lists "
                     "(empty lists are answers) - they are what makes the analysis go STALE mechanically")
    ln = doc.get("learning")
    if isinstance(ln, dict):
        if _empty(ln.get("data_exhaust")):
            g.append("R8.4: learning.data_exhaust is absent")
        hd = ln.get("historical_data")
        if not isinstance(hd, dict) or hd.get("state") not in HISTORY_STATES or _empty(hd.get("reason")):
            g.append("R8.4: learning.historical_data must be {state in %s, reason}" % (HISTORY_STATES,))
        fc = ln.get("future_capture_now")
        if not (isinstance(fc, list) and fc) and not _na_ok(fc):
            g.append("R8.4/R6.5: learning.future_capture_now must list what must be captured now, or N/A with a reason")
    ml = doc.get("ml_suitability")
    if isinstance(ml, dict):
        d = ml.get("disposition")
        if d not in ML_DISPOSITIONS:
            g.append("R8.6: ml_suitability.disposition %r is not one of %s" % (d, ML_DISPOSITIONS))
        if _empty(ml.get("reason")):
            g.append("R8.6: ml_suitability.reason is absent (NOT_SUITABLE is valid only WITH a reason)")
        if d in ML_DISPOSITIONS and d != "NOT_SUITABLE":
            for k in ("paradigm", "family", "target_label", "effective_n", "pit_features", "leakage_risks",
                      "regime_coverage", "benchmark"):
                if _empty(ml.get(k)):
                    g.append("R8.6: ml_suitability claims %s but %s is absent" % (d, k))
            if ml.get("paradigm") not in ML_PARADIGMS:
                g.append("R8.6: ml_suitability.paradigm %r not in %s" % (ml.get("paradigm"), ML_PARADIGMS))
            hd = (ln or {}).get("historical_data") if isinstance(ln, dict) else None
            if not isinstance(hd, dict) or hd.get("state") in (None, "NOT_APPLICABLE"):
                g.append("R8.4/R8.6: ML potential is claimed but there is no historical-data / backfill disposition")
            fc = (ln or {}).get("future_capture_now") if isinstance(ln, dict) else None
            if not (isinstance(fc, list) and fc):
                g.append("R8.4/R8.6: ML potential is claimed but no future data capture is named")
    elif ml is not None:
        g.append("R8.6: ml_suitability must be an object")
    params = doc.get("parameters_in_scope") if isinstance(doc.get("parameters_in_scope"), list) else []
    if params:
        pz = doc.get("parameterisation")
        if not isinstance(pz, dict) or _is_na(pz):
            g.append("R12.3: %d parameter(s) are in scope (%s) and parameterisation is absent/N/A - the "
                     "direct-measure vs static vs periodic vs regime vs adaptive question must be answered"
                     % (len(params), params[:5]))
        else:
            try:
                import isa_rationale_ledger as _irl
                for p in params:
                    for e in _irl.form_contract_errors(p, pz.get(p)):
                        g.append(e)
            except Exception as exc:                                    # noqa: BLE001
                g.append("R12.3: parameter-form vocabulary unreadable (%s)" % exc)
    return g


def analysis_fingerprint(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _anchor_digests(intersects: dict, root: str) -> Dict[str, str]:
    """The bytes the analysis rests on, so currency is MEASURED (R13.7): modules / config files -> sha256 of
    their bytes, capabilities -> sha256 of the registry row, parameters -> sha256 of the file that homes them.
    ⚑ Deliberately LIGHT (no release_gate import): isa_register's BUILD_READY gate reads this, and the
    register writer sits in the screener fallback's import closure (sync_repo_to_github ISA-0498) - an edge
    into release_gate pulled 63 modules and 35 unclassified inputs into that closure (measured 04-Oct-2026).
    A config file is hashed RAW: anchor an analysis on a parameter (its home) rather than on a file that
    carries runtime state, or accept that the runtime write re-opens it."""
    out: Dict[str, str] = {}

    def _sha_file(rel):
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return "ABSENT"
    for m in (intersects or {}).get("modules") or []:
        rel = m if m.endswith(".py") else m + ".py"
        out["src:" + rel] = _sha_file(rel)
    for c in (intersects or {}).get("config") or []:
        out["cfg:" + c] = _sha_file(c)
    try:
        reg = json.load(open(os.path.join(root, "Dashboard", "state", "capability_registry.json"), encoding="utf-8"))
        rows = {c["id"]: c for c in reg.get("capabilities") or []}
    except Exception:                                                   # noqa: BLE001
        rows = {}
    for cid in (intersects or {}).get("capabilities") or []:
        r = rows.get(cid)
        out["cap:" + cid] = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest() if r else "ABSENT"
    try:
        import isa_rationale_ledger as _irl
        homes = {k: v[0] for k, v in _irl.CAPITAL_GATING.items()}
    except Exception:                                                   # noqa: BLE001
        homes = {}
    for p in (intersects or {}).get("parameters") or []:
        h = homes.get(p)
        out["param:" + p] = _sha_file(h) if h else "NOT_CAPITAL_GATING"
    return out


def analysis_receipt_dir(root: str = HERE) -> str:
    return os.path.join(root, ANALYSIS_RECEIPT_REL)


def analysis_receipts(root: str = HERE, analysis_id: Optional[str] = None) -> List[dict]:
    d = analysis_receipt_dir(root)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            r = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except Exception:                                               # noqa: BLE001
            continue
        if analysis_id is None or r.get("analysis_id") == analysis_id:
            out.append(r)
    return out


def analysis_currency(analysis_id: str, fingerprint: Optional[str] = None, root: str = HERE,
                      today: Optional[str] = None) -> dict:
    """CURRENT | STALE | HASH_MISMATCH | ABSENT | REFUSED for the latest receipt of `analysis_id`.
    `today` is injectable (R5.14). STALE names why: expiry, or which anchored bytes changed."""
    recs = analysis_receipts(root, analysis_id)
    if not recs:
        return {"state": "ABSENT", "analysis_id": analysis_id,
                "why": "no ANALYSIS_ACCEPTANCE receipt exists for %s (R13.7)" % analysis_id}
    acc = [r for r in recs if r.get("state") == ANALYSIS_ACCEPTED]
    if fingerprint:
        acc = [r for r in acc if r.get("fingerprint") == fingerprint] or acc
    if not acc:
        return {"state": "REFUSED", "analysis_id": analysis_id, "gaps": recs[-1].get("gaps"),
                "why": "the only receipts for %s are REFUSED" % analysis_id}
    r = sorted(acc, key=lambda x: (x.get("as_of") or "", x.get("id") or ""))[-1]
    if fingerprint and r.get("fingerprint") != fingerprint:
        return {"state": "HASH_MISMATCH", "analysis_id": analysis_id, "receipt": r["id"],
                "why": "R19.3: the bound fingerprint %s is not the accepted analysis %s - the content changed"
                       % (str(fingerprint)[:12], str(r.get("fingerprint"))[:12])}
    t = today or _today()
    if r.get("revalidate_by") and t > str(r["revalidate_by"]):
        return {"state": "STALE", "analysis_id": analysis_id, "receipt": r["id"], "kind": "EXPIRED",
                "why": "R13.7: revalidate_by %s has passed (today %s)" % (r["revalidate_by"], t)}
    now = _anchor_digests(r.get("intersects") or {}, root)
    changed = sorted(k for k in set(now) | set(r.get("anchor_digests") or {})
                     if now.get(k) != (r.get("anchor_digests") or {}).get(k))
    if changed:
        return {"state": "STALE", "analysis_id": analysis_id, "receipt": r["id"], "kind": "INTERSECTING_CHANGE",
                "changed": changed,
                "why": "R13.7: %d anchored input(s) changed since acceptance: %s" % (len(changed), ", ".join(changed[:6]))}
    return {"state": "CURRENT", "analysis_id": analysis_id, "receipt": r["id"], "fingerprint": r.get("fingerprint"),
            "conclusion_state": r.get("conclusion_state"),
            "why": "accepted %s against %s; no anchored input changed; revalidate_by %s"
                   % (r.get("as_of"), r.get("accepted_against_build"), r.get("revalidate_by"))}


def decision_complete(analysis_id: str, fingerprint: Optional[str] = None, root: str = HERE,
                      today: Optional[str] = None) -> dict:
    """R13.7: a material recommendation is DECISION_COMPLETE only on a CURRENT accepted receipt."""
    c = analysis_currency(analysis_id, fingerprint, root, today)
    return dict(c, decision_complete=c["state"] == "CURRENT")




def _selftest() -> int:
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)[:300]) if not cond else ""))
        if not cond:
            fails.append(name)
    good = fixture_analysis()
    ok("ISA-0829 NEGATIVE CONTROL: the complete fixture analysis has no contract gaps",
       analysis_contract_gaps(good) == [], analysis_contract_gaps(good))
    bad = dict(good); bad.pop("north_star_link")
    ok("ISA-0829 MUST-FIRE (handoff case 1): no North-Star link is REFUSED",
       any("north_star_link" in g for g in analysis_contract_gaps(bad)))
    bad = dict(good, no_change_baseline={"na": "no"}, alternatives=None)
    ok("ISA-0829 MUST-FIRE (handoff case 2): NO_CHANGE N/A without a reason and absent alternatives are REFUSED",
       sum(1 for g in analysis_contract_gaps(bad) if "no_change_baseline" in g or "alternatives" in g) >= 2)
    d = tempfile.mkdtemp(prefix="aa_st_")
    os.makedirs(os.path.join(d, ANALYSIS_RECEIPT_REL))
    open(os.path.join(d, "mod_a.py"), "w").write("X = 1\n")
    rec = {"receipt_kind": "ANALYSIS_ACCEPTANCE", "id": "AA-T-1", "analysis_id": "T", "fingerprint": "fp1",
           "state": "ACCEPTED", "as_of": "2026-10-04", "revalidate_by": "2026-12-31", "conclusion_state": "NO_CHANGE",
           "intersects": {"modules": ["mod_a"], "config": [], "capabilities": [], "parameters": []},
           "anchor_digests": _anchor_digests({"modules": ["mod_a"]}, d), "accepted_against_build": "TB-X"}
    json.dump(rec, open(os.path.join(d, ANALYSIS_RECEIPT_REL, "AA-T-1.json"), "w"))
    ok("ISA-0829 NEGATIVE CONTROL: an accepted receipt with unchanged anchors is CURRENT",
       analysis_currency("T", "fp1", d, "2026-10-05")["state"] == "CURRENT")
    ok("ISA-0829 T-1/T/T+1 (R5.14): CURRENT on 2026-12-30 and 2026-12-31, STALE on 2027-01-01 (expiry)",
       analysis_currency("T", "fp1", d, "2026-12-30")["state"] == "CURRENT"
       and analysis_currency("T", "fp1", d, "2026-12-31")["state"] == "CURRENT"
       and analysis_currency("T", "fp1", d, "2027-01-01")["state"] == "STALE")
    ok("R5.14 repeat_same_occurrence: the same reading on the same day gives the same verdict",
       analysis_currency("T", "fp1", d, "2026-12-31") == analysis_currency("T", "fp1", d, "2026-12-31"))
    _r2 = dict(rec, id="AA-T-2", as_of="2027-01-02", revalidate_by="2027-06-30")
    json.dump(_r2, open(os.path.join(d, ANALYSIS_RECEIPT_REL, "AA-T-2.json"), "w"))
    ok("R5.14 next_occurrence: after expiry a re-accepted analysis (new receipt) is CURRENT again",
       analysis_currency("T", "fp1", d, "2027-01-03")["state"] == "CURRENT")
    os.remove(os.path.join(d, ANALYSIS_RECEIPT_REL, "AA-T-2.json"))
    ok("ISA-0829 MUST-FIRE (handoff case 7): a bound fingerprint that is not the accepted one is HASH_MISMATCH",
       analysis_currency("T", "fp-other", d, "2026-10-05")["state"] == "HASH_MISMATCH")
    open(os.path.join(d, "mod_a.py"), "a").write("Y = 2\n")
    _c = analysis_currency("T", "fp1", d, "2026-10-05")
    ok("ISA-0829 MUST-FIRE: an anchored module changing makes the analysis STALE and names it",
       _c["state"] == "STALE" and "src:mod_a.py" in _c.get("changed", []), _c)
    ok("ISA-0829: an analysis with no receipt is ABSENT, never CURRENT",
       analysis_currency("NOPE", None, d)["state"] == "ABSENT")
    print("analysis_acceptance selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def fixture_analysis() -> dict:
    """A complete, MATERIAL analysis used by the negative controls (each case removes one obligation)."""
    return {
        "analysis_id": "FIXTURE", "version": "1", "as_of": "2026-10-04", "materiality": "MATERIAL",
        "question": "should X change", "north_star_link": {"objective": "target by date", "decision": "X",
                                                            "capital_link": "INFRASTRUCTURE: controls capital quality"},
        "baseline_and_consumer": "current X; consumer release_gate",
        "evidence_manifest": [{"source": "file.json", "as_of": "2026-10-04"}],
        "pit_basis": {"na": "no time-series evidence is used; the analysis is structural"},
        "population_bridge": {"expected": ["a", "b"], "dispositions": {"a": {"state": "ANALYSED"},
                                                                      "b": {"state": "EXCLUDED", "reason": "out of scope"}}},
        "method": "structural comparison", "no_change_baseline": "keep X",
        "alternatives": [{"name": "keep", "assessment": "fails case 3"}, {"name": "change", "assessment": "passes"}],
        "assumptions_uncertainties": ["u1"], "power_and_limits": {"na": "no statistical estimate is made in this analysis"},
        "specification_search": {"na": "no specification was searched; one design evaluated against fixed cases"},
        "sensitivity_robust_region": {"na": "no tunable parameter is estimated in this analysis"},
        "economic_significance_frictions": {"na": "no empirical effect size is claimed by this analysis"},
        "falsifiers": ["f1"], "capital_consequence": {"na": "infrastructure; no capital quantity changes"},
        "conclusion": {"state": "RECOMMEND", "statement": "change X", "uncertainty": "low",
                       "why_dominates_alternatives": "only option passing all cases"},
        "revalidation": {"revalidate_by": "2026-12-31", "triggers": ["X changes"],
                         "intersects": {"modules": [], "config": [], "capabilities": [], "parameters": []}},
        "learning": {"data_exhaust": "none", "historical_data": {"state": "NOT_APPLICABLE", "reason": "no series"},
                     "future_capture_now": {"na": "no decision-state data is produced by this change"}},
        "ml_suitability": {"disposition": "NOT_SUITABLE", "reason": "deterministic governance rule; no label"},
        "parameters_in_scope": [], "parameterisation": {"na": "no parameter is set or changed by this analysis"},
        "unknowns": [],
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--currency" in argv:
        aid = argv[argv.index("--currency") + 1]
        fp = argv[argv.index("--fingerprint") + 1] if "--fingerprint" in argv else None
        print(json.dumps(analysis_currency(aid, fp, HERE), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
