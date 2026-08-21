#!/usr/bin/env python3
"""L-4 (ISA-0187) — THE INDEPENDENT-DERIVATION DISAGREEMENT LOG.

R8.3: "where independent derivations disagree, log the disagreement. WHERE DISAGREEMENTS CLUSTER
IS ITSELF DATA ABOUT WHERE THE FRAMEWORK IS WEAK."

R5.2 is the framework's most productive rule — two independent derivations must agree, with a
stated tolerance — and it is the only defence that has caught things unprompted. But each
agreement check has lived alone: `spot_vs_store` publishes its divergence, `consistency_check`
publishes its mismatches, `fund_pair_test` publishes its own. Nothing puts them on one axis, so
the SECOND-ORDER signal is invisible: three disagreements in one quantity is a different message
from three disagreements spread across three subsystems, and only the first says "the weakness is
here".

WHAT IT IS NOT
--------------
Not a register (R7.1: `isa_items.jsonl` is the one register). A disagreement is an OBSERVATION,
recorded whether or not it is a defect — most are within tolerance and are evidence the check
works. Only an out-of-tolerance disagreement with no explanation becomes a register item, and
that is the caller's decision, not this module's.

Not a second home for any tolerance. The tolerance is passed IN by the check that owns it (R4.4).

CLI:
  python3 disagreement_log.py --seed        # the four cases the 09-Aug session found
  python3 disagreement_log.py --clusters    # where they concentrate — the L-4 output
  python3 disagreement_log.py --report
  python3 disagreement_log.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOG_FILE = HERE / "disagreement_log.json"
LOG_VERSION = "1.0.0"

# A disagreement is UNEXPLAINED until someone writes down why. These are the permitted
# resolutions; anything else is a typo wearing a verdict (R4.8).
RESOLUTIONS = ("UNEXPLAINED", "ROUNDING", "DIFFERENT_WINDOW", "DIFFERENT_BASIS",
               "STALE_ONE_SIDE", "DEFECT_RAISED", "ACCEPTED_BY_RAJ")


def _load() -> dict:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    return {"schema_version": LOG_VERSION, "entries": []}


def _save(d: dict) -> None:
    LOG_FILE.write_text(json.dumps(d, indent=1, sort_keys=True, default=str), encoding="utf-8")


def _key(quantity, derivation_a, derivation_b, subject) -> str:
    """Content-derived id, so the same disagreement logged twice is ONE entry (R7.6).

    Deliberately excludes the VALUES: the same two derivations of the same quantity disagreeing
    again next month is the same disagreement with a new observation, not a new finding. That is
    what makes `observations` a persistence signal rather than a duplicate count.
    """
    raw = "|".join(str(x) for x in (quantity, sorted([derivation_a, derivation_b]), subject))
    return "DL-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def record(*, quantity, subject, derivation_a, value_a, derivation_b, value_b,
           tolerance, tolerance_basis, domain, resolution="UNEXPLAINED", note=None,
           as_of=None, register_item=None, save=True) -> dict:
    """Log one comparison. Called by whichever check owns the tolerance.

    ⚑ AGREEMENTS ARE LOGGED TOO. A log that only holds failures cannot tell you whether a
    subsystem is quiet because it is healthy or because its check stopped running (FC-E), and it
    cannot produce a disagreement RATE. `agree` is derived here, never passed in.
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution {resolution!r} not in {RESOLUTIONS} (R4.8)")
    if tolerance is None or not tolerance_basis:
        raise ValueError(
            "a comparison without a STATED tolerance and its basis is not a test — R5.2 requires "
            "two derivations to agree 'with a stated tolerance', and an unstated one is chosen "
            "after seeing the answer")
    num = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (value_a, value_b))
    delta = (float(value_a) - float(value_b)) if num else None
    agree = (abs(delta) <= float(tolerance)) if num else (value_a == value_b)
    entry = {
        "id": _key(quantity, derivation_a, derivation_b, subject),
        "quantity": quantity, "subject": subject, "domain": domain,
        "derivation_a": derivation_a, "value_a": value_a,
        "derivation_b": derivation_b, "value_b": value_b,
        "delta": round(delta, 10) if delta is not None else None,
        "tolerance": tolerance, "tolerance_basis": tolerance_basis,
        "agree": bool(agree),
        "resolution": resolution if not agree else "N/A_AGREES",
        "note": note,
        "register_item": register_item,
        "first_seen": (as_of or dt.date.today().isoformat()),
        "last_seen": (as_of or dt.date.today().isoformat()),
        "observations": 1,
    }
    d = _load()
    by_id = {e["id"]: e for e in d["entries"]}
    if entry["id"] in by_id:
        prev = by_id[entry["id"]]
        entry["first_seen"] = prev["first_seen"]
        entry["observations"] = int(prev.get("observations", 1)) + 1
        # a resolution recorded by a human is never overwritten by a later automatic log
        if prev.get("resolution") not in (None, "UNEXPLAINED", "N/A_AGREES"):
            entry["resolution"] = prev["resolution"]
            entry["note"] = prev.get("note") or entry["note"]
        by_id[entry["id"]] = entry
        d["entries"] = list(by_id.values())
    else:
        d["entries"].append(entry)
    if save:
        _save(d)
    return entry


def clusters(entries=None) -> dict:
    """THE L-4 OUTPUT. Not the list — the concentration.

    Two cuts, because they answer different questions: by DOMAIN says which subsystem is weak;
    by QUANTITY says which single number the framework cannot derive twice the same way.
    """
    entries = entries if entries is not None else _load()["entries"]
    dis = [e for e in entries if not e["agree"]]
    by_domain, by_quantity = {}, {}
    for e in dis:
        by_domain.setdefault(e["domain"], []).append(e["id"])
        by_quantity.setdefault(e["quantity"], []).append(e["id"])
    unexplained = [e for e in dis if e["resolution"] == "UNEXPLAINED"]
    persistent = [e for e in dis if int(e.get("observations", 1)) >= 3]
    return {
        "n_comparisons": len(entries),
        "n_disagreements": len(dis),
        "disagreement_rate": (round(len(dis) / len(entries), 4) if entries else None),
        "by_domain": {k: {"n": len(v), "ids": sorted(v)}
                      for k, v in sorted(by_domain.items(), key=lambda t: -len(t[1]))},
        "by_quantity": {k: {"n": len(v), "ids": sorted(v)}
                        for k, v in sorted(by_quantity.items(), key=lambda t: -len(t[1]))},
        "unexplained": sorted(e["id"] for e in unexplained),
        "persistent_3plus_observations": sorted(e["id"] for e in persistent),
        "read_this_first": (
            "A domain with several disagreements is where two parts of the framework hold "
            "different pictures of the same thing, and that is a better place to spend a session "
            "than any single item in it. An entry seen three times or more has survived two "
            "chances to be explained."),
    }


def check() -> list:
    """Battery surface: unexplained disagreements, loudest first."""
    out = []
    for e in _load()["entries"]:
        if e["agree"] or e["resolution"] != "UNEXPLAINED":
            continue
        out.append(f"UNEXPLAINED DISAGREEMENT {e['id']} [{e['domain']}] {e['quantity']} "
                   f"on {e['subject']}: {e['derivation_a']}={e['value_a']} vs "
                   f"{e['derivation_b']}={e['value_b']} (delta {e['delta']}, tolerance "
                   f"{e['tolerance']} — {e['tolerance_basis']}); seen {e['observations']}x "
                   f"since {e['first_seen']} (R5.2/R8.3)")
    return sorted(out)


# ------------------------------------------------------------------ the four known cases

def seed(save=True) -> dict:
    """The four disagreements the 09-Aug-2026 session found, recorded as the log's first content.

    R7.5 — nothing is invented: each is quoted from the register item that already records it,
    and where a side's value was never written down, it is logged as the string it actually is
    rather than a number nobody measured.
    """
    out = []
    out.append(record(
        quantity="portfolio_valuation_gbp", subject="31-Jul-2026 portfolio value",
        domain="analysis",
        derivation_a="target_state.portfolio_value_gbp (hand-written at each derivation)",
        value_a=139738.00,
        derivation_b="anchor_valuation_history.json (derived from portfolio_data _meta.data_date)",
        value_b=139738.39,
        tolerance=0.005, tolerance_basis=(
            "the anchor renders to 1 decimal place, so anything under half a pence of the "
            "rendered unit is invisible downstream; this is the tolerance the RENDER implies, "
            "not one chosen to make the test pass"),
        resolution="DIFFERENT_BASIS", register_item="ISA-0312",
        note=("GBP0.39. Registered for its CLASS, not its size: a stored value that says one "
              "thing and IS another, invisible because both numbers are plausible. Published "
              "every run by valuation_basis.spot_vs_store."),
        as_of="2026-08-12", save=False))
    out.append(record(
        quantity="fund_bucket_minimum", subject="B1/B2/B3 bucket minima",
        domain="funds",
        derivation_a="bucket minimum as implemented in code", value_a="code",
        derivation_b="bucket minimum as stated in the allocation policy", value_b="policy",
        tolerance=0, tolerance_basis=(
            "an exact-match comparison: a band is either the same number in both homes or it is "
            "two numbers (R4.4). Zero is the only defensible tolerance for a rule's identity"),
        resolution="UNEXPLAINED", register_item="ISA-0166",
        note=("O-11: the bands were set against min_expected_return per bucket — the field D-8 "
              "retired. Neither side's current value was written down at the time the "
              "disagreement was observed, so both are logged as the labels they are, not as "
              "numbers nobody measured (R2.10)."),
        as_of="2026-08-09", save=False))
    out.append(record(
        quantity="as_of_date", subject="anchor as_of vs portfolio as_of",
        domain="analysis",
        derivation_a="required-return anchor as_of", value_a="anchor cadence date",
        derivation_b="portfolio_data _meta.data_date", value_b="broker file date",
        tolerance=0, tolerance_basis=(
            "two dates describing the same valuation must be the same date; any difference puts "
            "a stale valuation inside a live anchor (R4.2)"),
        resolution="DIFFERENT_BASIS", register_item="ISA-0143",
        note=("D-7: anchor quarterly, switches semi-annual, M monthly — three unsynchronised "
              "clocks. The disagreement is structural, not a bug, which is exactly why it needs "
              "logging: a structural disagreement nobody records becomes a surprise later."),
        as_of="2026-08-09", save=False))
    out.append(record(
        quantity="beta", subject="Ranmore Global Equity Institutional",
        domain="funds",
        derivation_a="beta on the 38-month share-class window", value_a=0.351,
        derivation_b="beta on the 194-month fund-level window", value_b=0.769,
        tolerance=0.10, tolerance_basis=(
            "fund_learning.CONTROL_BETA_TOL — the same 0.10 the standing index-tracker control "
            "uses, because a beta difference that would fail a tracker control is material "
            "wherever it appears"),
        resolution="DIFFERENT_WINDOW", register_item="ISA-0151",
        note=("D-16. The two betas are both correct and describe different windows; the defect is "
              "that only one was ever quoted. 0.418 apart — four times the tolerance."),
        as_of="2026-08-09", save=False))
    d = _load()
    by_id = {e["id"]: e for e in d["entries"]}
    for e in out:
        if e["id"] not in by_id:
            d["entries"].append(e)
    if save:
        _save(d)
    return {"seeded": len(out), "total": len(d["entries"])}


def report() -> str:
    d = _load()
    c = clusters(d["entries"])
    L = [f"INDEPENDENT-DERIVATION DISAGREEMENT LOG  ({c['n_comparisons']} comparisons, "
         f"{c['n_disagreements']} disagreements)", ""]
    L.append("BY DOMAIN — which subsystem holds two pictures of one thing:")
    for k, v in c["by_domain"].items():
        L.append(f"   {k:<12} {v['n']}")
    L += ["", "BY QUANTITY — which number cannot be derived twice the same way:"]
    for k, v in c["by_quantity"].items():
        L.append(f"   {k:<28} {v['n']}")
    L += ["", f"UNEXPLAINED: {len(c['unexplained'])}  |  "
              f"SEEN 3+ TIMES: {len(c['persistent_3plus_observations'])}", ""]
    for e in sorted(d["entries"], key=lambda x: (x["agree"], x["domain"])):
        mark = "ok  " if e["agree"] else "DIS "
        L.append(f"{mark}{e['id']} [{e['domain']}] {e['quantity']} — {e['subject']}")
        L.append(f"     a: {e['derivation_a']} = {e['value_a']}")
        L.append(f"     b: {e['derivation_b']} = {e['value_b']}")
        L.append(f"     tol {e['tolerance']} ({e['tolerance_basis'][:70]}...)  "
                 f"-> {e['resolution']}  {e.get('register_item') or ''}")
    return "\n".join(L)


def selftest(verbose=True) -> int:
    global LOG_FILE
    import shutil, tempfile
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    tmp = Path(tempfile.mkdtemp(prefix="isa_dislog_"))
    saved = LOG_FILE
    LOG_FILE = tmp / "disagreement_log.json"
    try:
        e = record(quantity="q", subject="s", domain="funds",
                   derivation_a="a", value_a=1.00, derivation_b="b", value_b=1.001,
                   tolerance=0.01, tolerance_basis="declared for the test")
        ok(e["agree"], "1.000 vs 1.001 inside a 0.01 tolerance must AGREE")
        ok(e["resolution"] == "N/A_AGREES", "an agreement carries no resolution to explain")

        e2 = record(quantity="q", subject="s2", domain="funds",
                    derivation_a="a", value_a=1.0, derivation_b="b", value_b=2.0,
                    tolerance=0.01, tolerance_basis="declared for the test")
        ok(not e2["agree"] and e2["resolution"] == "UNEXPLAINED",
           "a disagreement defaults to UNEXPLAINED — silence is not an explanation")

        raised = False
        try:
            record(quantity="q", subject="s3", domain="funds", derivation_a="a", value_a=1,
                   derivation_b="b", value_b=2, tolerance=None, tolerance_basis="")
        except ValueError as ex:
            raised = "stated tolerance" in str(ex)
        ok(raised, "a comparison with NO stated tolerance must RAISE — R5.2 (this is the whole "
                   "point: an unstated tolerance is chosen after seeing the answer)")

        raised = False
        try:
            record(quantity="q", subject="s4", domain="funds", derivation_a="a", value_a=1,
                   derivation_b="b", value_b=2, tolerance=0.1, tolerance_basis="t",
                   resolution="probably fine")
        except ValueError:
            raised = True
        ok(raised, "a free-text resolution must be refused (R4.8)")

        # idempotence + persistence counting
        before = len(_load()["entries"])
        again = record(quantity="q", subject="s2", domain="funds",
                       derivation_a="a", value_a=1.0, derivation_b="b", value_b=2.5,
                       tolerance=0.01, tolerance_basis="declared for the test")
        ok(len(_load()["entries"]) == before,
           "the same two derivations of the same quantity must be ONE entry, not two (R7.6)")
        ok(again["observations"] == 2,
           "a repeat must increment observations — that count IS the persistence signal")
        ok(again["value_b"] == 2.5, "the latest observation's values must be kept")

        # a human resolution survives a later automatic log
        record(quantity="q", subject="s2", domain="funds", derivation_a="a", value_a=1.0,
               derivation_b="b", value_b=2.5, tolerance=0.01, tolerance_basis="t",
               resolution="ACCEPTED_BY_RAJ", note="Raj's call")
        after = record(quantity="q", subject="s2", domain="funds", derivation_a="a", value_a=1.0,
                       derivation_b="b", value_b=2.6, tolerance=0.01, tolerance_basis="t")
        ok(after["resolution"] == "ACCEPTED_BY_RAJ",
           "an automatic re-log must NOT overwrite a recorded human resolution")

        c = clusters()
        ok(c["n_comparisons"] == 2 and c["n_disagreements"] == 1,
           f"clusters must count agreements AND disagreements separately, got {c}")
        ok(c["by_domain"]["funds"]["n"] == 1, "the cluster cut must count by domain")
        ok(c["disagreement_rate"] == 0.5, "a rate needs the agreements in the denominator")
        ok(not check(), "an ACCEPTED disagreement must not appear as unexplained")

        # non-numeric comparison
        e5 = record(quantity="basis", subject="x", domain="process", derivation_a="code",
                    value_a="policy_a", derivation_b="doc", value_b="policy_b",
                    tolerance=0, tolerance_basis="exact match: a rule's identity")
        ok(not e5["agree"] and e5["delta"] is None,
           "two non-numeric values must compare by equality with delta None, never a coerced 0")
        ok(len(check()) == 1, "an unexplained disagreement must reach the battery surface")

        # seeding is idempotent
        LOG_FILE = tmp / "seed_only.json"
        s1 = seed()
        s2 = seed()
        ok(s1["total"] == 4 and s2["total"] == 4,
           f"seeding twice must not duplicate, got {s1} then {s2}")
        cl = clusters()
        ok(cl["n_disagreements"] == 4,
           "all four seeded cases are disagreements, by construction of how they were found")
        ok(cl["by_domain"]["funds"]["n"] == 2 and cl["by_domain"]["analysis"]["n"] == 2,
           f"the cluster cut must split the four across their domains, got {cl['by_domain']}")
    finally:
        LOG_FILE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    if verbose:
        print(f"disagreement_log selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="L-4 independent-derivation disagreement log")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--clusters", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.seed:
        print(json.dumps(seed(), indent=1))
        return 0
    if a.clusters:
        print(json.dumps(clusters(), indent=1))
        return 0
    if a.check:
        v = check()
        print("\n".join(v) if v else "disagreement log: no unexplained disagreement")
        return 1 if v else 0
    if a.report:
        print(report())
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
