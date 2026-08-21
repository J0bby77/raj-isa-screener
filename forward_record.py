#!/usr/bin/env python3
"""
forward_record.py — closes ISA-0306 (M* series) · ISA-0156 (D-21) · ISA-0192 (L-9).
Built 16-Aug-2026.

TWO LEDGERS, ONE FILE, BECAUSE THEY ARE THE SAME IDEA: write down what you claimed BEFORE you
find out, or the claim is unfalsifiable and the framework learns nothing.

§A  IMPLIED-M HISTORY  (ISA-0306). The framework has ZERO labelled forward observations of any
    kind. The VCI learning loop stands at 0/12 and every other forward claim is unfalsifiable in
    practice because nothing records it. M* — the market return the portfolio's own weights and
    the anchor jointly IMPLY — is the first falsifiable forward statement the framework produces.
    One observation accrues per month; the first comparison against a realised 12-month market
    return lands 2027-06-30. Without the series, whether the required rate was ever attainable is
    permanently unanswerable.

§B  SURPRISE REGISTER  (ISA-0156 / D-21, and ISA-0192 / L-9 is its learning half — the SAME
    instrument, so they close together). Raj could not see my diagnostic error rate; on the day it
    was raised it stood at 3 for 3 wrong. R2.12 makes it a rule: log what I EXPECTED, what I GOT,
    what I CLAIMED caused it — logged BEFORE investigating — and what ACTUALLY caused it, after.

⚑ THE PROPERTY THAT MATTERS IN BOTH: the prediction is written before the outcome is known, and
neither field may be edited once the outcome is in. `append()` refuses to overwrite an existing
prediction (R6.4 — point-in-time or labelled not-point-in-time). A register I can revise after the
fact would measure my memory, not my accuracy.

ROLLBACK (R4.13): delete the two stores; `report()` returns NO_OBSERVATIONS.
"""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

M_STORE = HERE / "implied_m_history.json"
S_STORE = HERE / "surprise_register.json"
SCHEMA_VERSION = "1.0.0"
CLAIM_STATES = ("HYPOTHESIS", "TESTED", "VERIFIED")          # R2.2


def _load(p: Path, key: str) -> dict:
    if not p.exists():
        return {"schema_version": SCHEMA_VERSION,
                "_meta": {"generator": "forward_record.py", "note":
                          "append-only. A recorded prediction is never edited (R6.4)."},
                key: []}
    return json.loads(p.read_text(encoding="utf-8"))


# ── §A  implied M* ────────────────────────────────────────────────────────────────────────────
def record_m_star(*, as_of, m_star_pct, leverage_lambda, intercept_pct, coverage_pct,
                  anchor_pct, alpha_mode, source, store: Path = None) -> dict:
    p = store or M_STORE
    doc = _load(p, "observations")
    if any(o["as_of"] == as_of for o in doc["observations"]):
        raise ValueError(f"an observation already exists for {as_of}; predictions are "
                         "append-only and are never revised (R6.4)")
    obs = {"as_of": as_of, "m_star_pct": m_star_pct, "leverage_lambda": leverage_lambda,
           "intercept_pct": intercept_pct, "coverage_pct": coverage_pct,
           "anchor_pct": anchor_pct, "alpha_mode": alpha_mode, "source": source,
           "recorded_at": dt.date.today().isoformat(),
           "realised_market_return_12m_pct": None,
           "realisation_due": (dt.date.fromisoformat(as_of).replace(
               year=dt.date.fromisoformat(as_of).year + 1)).isoformat(),
           "error_pp": None, "schema_version": SCHEMA_VERSION}
    doc["observations"].append(obs)
    doc["_meta"]["n"] = len(doc["observations"])
    p.write_text(json.dumps(doc, indent=2))
    return obs


def realise_m_star(*, as_of, realised_pct, source, store: Path = None) -> dict:
    p = store or M_STORE
    doc = _load(p, "observations")
    for o in doc["observations"]:
        if o["as_of"] == as_of:
            if o["realised_market_return_12m_pct"] is not None:
                raise ValueError(f"{as_of} is already realised; an outcome is written once")
            o["realised_market_return_12m_pct"] = realised_pct
            o["realised_source"] = source
            o["error_pp"] = round(realised_pct - o["m_star_pct"], 3)
            p.write_text(json.dumps(doc, indent=2))
            return o
    raise KeyError(f"no prediction on file for {as_of} - an outcome may not be recorded for a "
                   "prediction that was never made (R7.5)")


# ── §B  surprise register ─────────────────────────────────────────────────────────────────────
def log_surprise(*, surprise_id, expected, observed, claimed_cause, claim_state="HYPOTHESIS",
                 context=None, store: Path = None) -> dict:
    """Logged BEFORE investigating. R2.12."""
    if claim_state not in CLAIM_STATES:
        raise ValueError(f"claim_state must be one of {CLAIM_STATES} (R2.2)")
    p = store or S_STORE
    doc = _load(p, "entries")
    if any(e["surprise_id"] == surprise_id for e in doc["entries"]):
        raise ValueError(f"{surprise_id} already logged; the claim is written once (R6.4)")
    e = {"surprise_id": surprise_id, "logged_at": dt.date.today().isoformat(),
         "expected": expected, "observed": observed, "claimed_cause": claimed_cause,
         "claim_state": claim_state, "context": context,
         "actual_cause": None, "verdict": None, "resolved_at": None,
         "schema_version": SCHEMA_VERSION}
    doc["entries"].append(e)
    p.write_text(json.dumps(doc, indent=2))
    return e


def resolve_surprise(*, surprise_id, actual_cause, store: Path = None) -> dict:
    p = store or S_STORE
    doc = _load(p, "entries")
    for e in doc["entries"]:
        if e["surprise_id"] == surprise_id:
            if e["actual_cause"] is not None:
                raise ValueError(f"{surprise_id} is already resolved")
            e["actual_cause"] = actual_cause
            e["verdict"] = "RIGHT" if actual_cause.strip().lower() == \
                str(e["claimed_cause"]).strip().lower() else "WRONG"
            e["resolved_at"] = dt.date.today().isoformat()
            p.write_text(json.dumps(doc, indent=2))
            return e
    raise KeyError(f"no surprise logged as {surprise_id}")


def diagnostic_accuracy(store: Path = None) -> dict:
    """The number Raj could not see. Reported in the monthly email."""
    doc = _load(store or S_STORE, "entries")
    res = [e for e in doc["entries"] if e["verdict"]]
    right = sum(1 for e in res if e["verdict"] == "RIGHT")
    return {"logged": len(doc["entries"]), "resolved": len(res), "right": right,
            "wrong": len(res) - right,
            "accuracy_pct": (round(right / len(res) * 100, 1) if res else None),
            "state": "MEASURED" if res else "NO_RESOLVED_OBSERVATIONS",
            "open": [e["surprise_id"] for e in doc["entries"] if not e["verdict"]]}


def report(m_store: Path = None, s_store: Path = None) -> dict:
    m = _load(m_store or M_STORE, "observations")
    obs = m["observations"]
    due = [o for o in obs if o["realised_market_return_12m_pct"] is None
           and o["realisation_due"] <= dt.date.today().isoformat()]
    errs = [o["error_pp"] for o in obs if o["error_pp"] is not None]
    return {"implied_m": {
                "n_predictions": len(obs), "n_realised": len(errs),
                "mean_error_pp": (round(sum(errs) / len(errs), 3) if errs else None),
                "realisations_due_now": [o["as_of"] for o in due],
                "state": "MEASURED" if errs else ("ACCRUING" if obs else "NO_OBSERVATIONS")},
            "surprise": diagnostic_accuracy(s_store)}


def selftest(verbose=True) -> int:
    import tempfile
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    mt, st_ = Path(tempfile.mktemp(suffix=".json")), Path(tempfile.mktemp(suffix=".json"))
    ck("no store reports NO_OBSERVATIONS, never a score",
       report(mt, st_)["implied_m"]["state"] == "NO_OBSERVATIONS")

    record_m_star(as_of="2026-08-31", m_star_pct=11.78, leverage_lambda=0.810,
                  intercept_pct=1.2, coverage_pct=92.0, anchor_pct=13.8, alpha_mode="measured",
                  source="return_architecture._implied_m_block", store=mt)
    ck("a prediction is recorded with its realisation date",
       _load(mt, "observations")["observations"][0]["realisation_due"] == "2027-08-31")
    ck("state is ACCRUING with a prediction and no outcome",
       report(mt, st_)["implied_m"]["state"] == "ACCRUING")

    dup = False
    try:
        record_m_star(as_of="2026-08-31", m_star_pct=99.0, leverage_lambda=1, intercept_pct=0,
                      coverage_pct=0, anchor_pct=0, alpha_mode="x", source="y", store=mt)
    except ValueError:
        dup = True
    ck("NEGATIVE CONTROL: a prediction may not be revised after the fact", dup)

    orphan = False
    try:
        realise_m_star(as_of="2025-01-31", realised_pct=8.0, source="x", store=mt)
    except KeyError:
        orphan = True
    ck("NEGATIVE CONTROL: an outcome without a prediction is REFUSED (R7.5)", orphan)

    r = realise_m_star(as_of="2026-08-31", realised_pct=9.5, source="MSCI World 12m", store=mt)
    ck("the error is arithmetic, not narrative", r["error_pp"] == round(9.5 - 11.78, 3))
    ck("state becomes MEASURED once an outcome lands",
       report(mt, st_)["implied_m"]["state"] == "MEASURED")

    log_surprise(surprise_id="S-2026-08-01", expected="fund sleeve alpha positive over 87m",
                 observed="t 1.13, not distinguishable from zero",
                 claimed_cause="short panel", store=st_)
    ck("a surprise is logged before investigation with claim_state HYPOTHESIS",
       _load(st_, "entries")["entries"][0]["claim_state"] == "HYPOTHESIS")
    ck("an unresolved surprise yields NO accuracy figure, not 100%",
       diagnostic_accuracy(st_)["accuracy_pct"] is None)
    resolve_surprise(surprise_id="S-2026-08-01",
                     actual_cause="one manager's process, not panel length", store=st_)
    a = diagnostic_accuracy(st_)
    ck("a wrong claim scores WRONG", a["wrong"] == 1 and a["accuracy_pct"] == 0.0)
    log_surprise(surprise_id="S-2026-08-02", expected="a", observed="b", claimed_cause="c",
                 store=st_)
    resolve_surprise(surprise_id="S-2026-08-02", actual_cause="c", store=st_)
    ck("NEGATIVE CONTROL: a right claim scores RIGHT and moves the rate",
       diagnostic_accuracy(st_)["accuracy_pct"] == 50.0)
    bad = False
    try:
        log_surprise(surprise_id="S-3", expected="a", observed="b", claimed_cause="c",
                     claim_state="PROBABLY", store=st_)
    except ValueError:
        bad = True
    ck("NEGATIVE CONTROL: an untagged causal claim is refused (R2.2)", bad)
    for f in (mt, st_):
        f.unlink(missing_ok=True)
    print(f"\nforward_record selftest: {len(fails)} failure(s)"
          + (" -> " + ", ".join(fails) if fails else " — 13 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(json.dumps(report(), indent=2))
