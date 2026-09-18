#!/usr/bin/env python3
"""
defect_corpus_backtest.py — R5.13: systemic controls are BACKTESTED against the historical
defect corpus, and the misses are reported as loudly as the catches.

Authority: ISA_Engineering_Rules.md §5 (R5.13, R5.5, R5.8), §10 (K9), §8 (R8.3).
Raised as ISA-0631 under ISA-0623 / ISA-0467.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
R5.13, in the standard's own words: *"If the build claims to prevent a failure class,
map/replay known historical defects in that class and report catches and misses — **one
synthetic fixture cannot justify 'class killed'**."* And: *"the register is labelled validation
data as well as a backlog."*

That sentence is the whole design. Every control in this framework was written after a defect,
and every one of them was tested against the defect that motivated it — which is the weakest
possible test, because the control was shaped around that example. This module asks the harder
question: **of the N historical items in a failure class, how many does the control actually
fire on, and which does it miss?**

═══════════════════════════════════════════════════════════════════════════════════════════
⚑ WHAT "REPLAY" HONESTLY MEANS HERE, AND WHAT IT DOES NOT
═══════════════════════════════════════════════════════════════════════════════════════════
Most historical items cannot be re-executed: the code that carried them was deleted, the data
they ran on has moved on, and re-creating either would be fabrication (R7.5). So this module
does two DIFFERENT things and never blends them (R6.2):

  **SHAPE REPLAY** — for each control, a fixture that reproduces the DEFECT SHAPE is
  constructed and the control must fire on it, and must NOT fire on the corrected shape. This
  is executable and is either green or red. It is the `replays` block.

  **CLASS COVERAGE** — for each failure class in the register, how many historical items it
  holds, and whether any replayed control addresses that class at all. This is a census, not a
  proof, and it is labelled as one. An uncovered class with 38 items in it is the finding.

Reporting the census as if it were the proof would be exactly the substitution this framework
keeps making (a cheap proxy for the expensive property), so the two never appear as one number.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from collections import Counter
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_MODULE = "defect_corpus_backtest"

# Which failure classes each replayed control CLAIMS. A claim here is a commitment the replay
# must honour; an unclaimed class is reported as uncovered rather than quietly omitted.
CONTROL_CLAIMS = {
    "atlas_drift_fails_the_build":      ("FC-B", "FC-E"),
    "unsigned_live_state_blocks":       ("FC-B", "FC-E", "FC-D"),
    "two_computers_for_one_quantity":   ("FC-D", "FC-E"),
    "reachable_but_never_live":         ("FC-E", "FC-K"),
    "prose_is_not_enforcement":         ("FC-E",),
    "unclassified_rule_fails":          ("FC-E", "FC-D"),
    "undispositioned_run_surface":      ("FC-H", "FC-D"),
    "stale_build_ready_authority":      ("FC-B",),
    "vacuous_selftest":                 ("FC-K",),
    "verified_no_change_without_evidence": ("FC-H",),
}


def _today() -> str:
    return datetime.date.today().isoformat()


def _items(root: str = HERE) -> List[dict]:
    p = os.path.join(root, "Dashboard", "state", "isa_items.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


# ────────────────────────────────────────────────────────────────────────────────────────
# SHAPE REPLAY — each control fires on the defect shape and NOT on the corrected shape
# ────────────────────────────────────────────────────────────────────────────────────────

def _replay(name, defect_fires, corrected_fires, evidence, historical) -> dict:
    """A replay is green only when the control fires on the defect AND stays quiet on the fix.

    ⚑ BOTH HALVES ARE REQUIRED. A control that fires on everything catches every historical
    defect and is worthless; a control that fires on nothing is the vacuous pass. The pair is
    the R5.5 negative control applied to a historical shape rather than an invented one."""
    return {"control": name,
            "fires_on_defect_shape": bool(defect_fires),
            "silent_on_corrected_shape": not bool(corrected_fires),
            "state": "GREEN" if (defect_fires and not corrected_fires) else "RED",
            "evidence": evidence,
            "historical_items": list(historical),
            "claims_classes": list(CONTROL_CLAIMS.get(name, ()))}


def replays(root: str = HERE) -> List[dict]:
    out = []
    sys.path.insert(0, root)

    # ── ISA-0467: the Atlas 28 days and 59 modules stale, and no build failed ───────
    try:
        import framework_atlas as fa
        from pathlib import Path as P
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "Dashboard", "state"))
        with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def a():\n    return 1\n")
        old_env = os.environ.get("ISA_REGISTER_STORE")
        os.environ["ISA_REGISTER_STORE"] = os.path.join(d, "Dashboard", "state")
        fa.refresh(P(d))
        clean = fa.check(P(d))[0]
        with open(os.path.join(d, "b.py"), "w", encoding="utf-8") as fh:
            fh.write("def b():\n    return 2\n")
        drifted = fa.check(P(d))[0]
        if old_env is None:
            os.environ.pop("ISA_REGISTER_STORE", None)
        else:
            os.environ["ISA_REGISTER_STORE"] = old_env
        out.append(_replay(
            "atlas_drift_fails_the_build", defect_fires=not drifted, corrected_fires=not clean,
            evidence=("a module added after the manifest was declared makes "
                      "framework_atlas.check() return False; an undrifted tree returns True"),
            historical=["ISA-0467", "ISA-0468"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "atlas_drift_fails_the_build", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0629: LIVE edited between runs with nothing able to notice ─────────────
    try:
        import release_gate as rg
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "Dashboard", "state"))
        os.makedirs(os.path.join(d, "Skills_to_Edit", "t"))
        with open(os.path.join(d, "Skills_to_Edit", "t", "SKILL.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# t\nSee ISA_Engineering_Rules.md.\n")
        with open(os.path.join(d, "ISA_Engineering_Rules.md"), "w", encoding="utf-8") as fh:
            fh.write("# rules\n")
        # ISA-0702: read the ONE home of the config surface - this fixture hand-copied the tuple and
        # went RED (ENVIRONMENT_UNKNOWN on a fixture file) the day the surface gained the taxonomy.
        for cfg in rg.CONFIG_FILES:
            os.makedirs(os.path.dirname(os.path.join(d, cfg)), exist_ok=True)
            with open(os.path.join(d, cfg), "w", encoding="utf-8") as fh:
                fh.write("{}\n" if cfg.endswith(".json") else "# stub\n")
        with open(os.path.join(d, "m.py"), "w", encoding="utf-8") as fh:
            fh.write("def f():\n    return 1\n")
        with open(rg.receipt_path(d), "w", encoding="utf-8") as fh:
            json.dump({"build_id": "TB-REPLAY", "fingerprints": rg.live_fingerprints(d)}, fh)
        clean = rg.verify_live(d)["blocks_capital_run"]
        with open(os.path.join(d, "m.py"), "a", encoding="utf-8") as fh:
            fh.write("def g():\n    return 2\n")
        drifted = rg.verify_live(d)["blocks_capital_run"]
        out.append(_replay(
            "unsigned_live_state_blocks", defect_fires=drifted, corrected_fires=clean,
            evidence=("an unsigned edit to LIVE returns UNTRUSTED_LIVE_STATE with "
                      "blocks_capital_run=True; a signed tree returns TRUSTED and does not "
                      "block (R18.5)"),
            historical=["ISA-0467", "ISA-0002", "ISA-0211"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "unsigned_live_state_blocks", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0454: two functions computing one quantity, only the wrong one live ────
    try:
        import framework_integrity as fi
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "one.py"), "w", encoding="utf-8") as fh:
            fh.write('def a():\n    out = {}\n    out["q_gbp"] = 1 + 1\n    return out\n')
        reg = [{"name": "q_gbp", "computer": "one.a", "units": "GBP",
                "surface": ["x"], "gbp_exposure": 1.0}]
        fi.reset_caches()
        clean = fi.q1_two_computers(d, reg)["state"] == "FAIL"
        with open(os.path.join(d, "two.py"), "w", encoding="utf-8") as fh:
            fh.write('def b(nav):\n    out = {}\n    out["q_gbp"] = nav * 0.035\n    return out\n')
        fi.reset_caches()
        rival = fi.q1_two_computers(d, reg)["state"] == "FAIL"
        out.append(_replay(
            "two_computers_for_one_quantity", defect_fires=rival, corrected_fires=clean,
            evidence=("a second COMPUTER for a registered quantity makes Q1 FAIL; a single "
                      "computer passes. Green only because ISA-0625 restored cache "
                      "invalidation — this replay was RED on the delivered tree"),
            historical=["ISA-0454", "ISA-0442", "ISA-0430"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "two_computers_for_one_quantity", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0454 / ISA-0468: reachable, instrumented, never run from a live caller ─
    try:
        import capability_registry as cr
        on_disk = {"position_sizing.stock_max"}
        cap = {"id": "CAP-r", "name": "stock_max_gbp", "producer": "position_sizing.stock_max",
               "outputs": ["email.s2.capital_router"], "consumers": ["email_prefill"],
               # ISA-0628 (16-Sep-2026): evidence is a TYPED check whose suite is GREEN on the
               # source, never a sentence - the fixture carries the typed form and its suite map.
               "consumption_ref": {"kind": "check", "ref": "position_sizing.stock_max"},
               "must_fire": [{"state": "BUY", "fixture": "f"}],
               # ISA-0699 (17-Sep-2026): a decision ref names the orchestrator it traverses (`via`);
               # the fixture is synthetic, so the AST traversal check is off (check_ast=False).
               "decision_effective_ref": {"kind": "check", "ref": "position_sizing.stock_max",
                                          "via": "position_sizing.stock_max"},
               "decision_states": ["BUY"], "gbp_exposure": 11250.0}
        _suites = {"position_sizing": "GREEN"}
        probe_only = cr.live_state(cap, cr.evidence_chain(cap, on_disk, {}, HERE, suites=_suites,
                                                          check_ast=False))
        live_ok = cr.live_state(cap, cr.evidence_chain(
            cap, on_disk, {"position_sizing.stock_max": 27}, HERE, suites=_suites, check_ast=False))
        out.append(_replay(
            "reachable_but_never_live",
            defect_fires=(probe_only["live"] is False and probe_only["blocked_at"] == "EXECUTED"),
            corrected_fires=(live_ok["live"] is False),
            evidence=("a producer on disk with zero live_run calls is blocked at EXECUTED; with "
                      "live calls and a proven consumer it is LIVE. This is the exact ISA-0454 "
                      "shape, in which every STRUCTURAL instrument read green"),
            historical=["ISA-0454", "ISA-0468", "ISA-0430"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "reachable_but_never_live", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0526 / ISA-0627: a sentence about a rule read as enforcement of it ─────
    try:
        import rule_audit as ra
        prose = ('def render(rows):\n'
                 '    rows.append("This file is a VIEW. Edit the store instead (R9.9).")\n'
                 "    return rows\n")
        real = ('def load(x):\n    if x is None:\n'
                '        raise ValueError("R9.9: refused — a null control never returns PASS")\n'
                "    return x\n")
        out.append(_replay(
            "prose_is_not_enforcement",
            defect_fires=(ra._named_by_enforcement(prose, "R9.9")
                          and not ra._named_by_failing_check(prose, "R9.9")),
            corrected_fires=(not ra._named_by_failing_check(real, "R9.9")),
            evidence=("a renderer's output string reads as enforcement under the loose test and "
                      "NOT under the failing-check test; a raise reads as enforcement under "
                      "both. ISA-0526 was the comment version of this; ISA-0627 the rendered "
                      "version"),
            historical=["ISA-0526", "ISA-0627", "ISA-0446", "ISA-0523"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "prose_is_not_enforcement", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0624: a rule written into the standard and into no §17 row ────────────
    try:
        import rule_audit as ra
        text = open(os.path.join(root, "ISA_Engineering_Rules.md"), encoding="utf-8").read()
        clean = bool(ra.unclassified(text))
        injected = text + "\n\n**R99.1 — a rule in the body and in no row of §17.**\n"
        fires = ra.unclassified(injected) == ["R99.1"]
        out.append(_replay(
            "unclassified_rule_fails", defect_fires=fires, corrected_fires=clean,
            evidence=("a rule added to the body and to no §17 row is reported; the live "
                      "standard reports none. R15.4 item 3 made fireable"),
            historical=["ISA-0624", "ISA-0525", "ISA-0522"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "unclassified_rule_fails", "state": "ENVIRONMENT_UNKNOWN",
                    "evidence": str(exc)[:200]})

    # ── ISA-0002 / ISA-0211: a run surface nobody read, and a mirror standing in ───
    try:
        import release_gate as rg
        good = {s: {"verdict": "VERIFIED_NO_CHANGE", "evidence": "diffed"}
                for s in rg.RUN_SURFACES}
        miss = dict(good); miss.pop("skill_mirror")
        noev = dict(good); noev["run_context"] = {"verdict": "VERIFIED_NO_CHANGE"}
        out.append(_replay(
            "undispositioned_run_surface",
            defect_fires=(rg.run_surface_dispositions(miss)["state"] == "RED"),
            corrected_fires=(rg.run_surface_dispositions(good)["state"] == "RED"),
            evidence=("an unlisted SKILL mirror FAILS; a fully dispositioned set passes. "
                      "ISA-0002's root cause was a guard pointed at one document while the "
                      "defect lived in a SKILL prompt no guard had ever read"),
            historical=["ISA-0002", "ISA-0211", "ISA-0481"]))
        out.append(_replay(
            "verified_no_change_without_evidence",
            defect_fires=(rg.run_surface_dispositions(noev)["unevidenced"] == ["run_context"]),
            corrected_fires=(rg.run_surface_dispositions(good)["state"] == "RED"),
            evidence="a VERIFIED_NO_CHANGE verdict with no evidence FAILS (R14.5)",
            historical=["ISA-0002", "ISA-0513"]))
        items = [{"id": "ISA-X", "state": "OPEN", "build_readiness": "BUILD_READY",
                  "criticality": "CRITICAL", "title": "t",
                  "validated_against_build_id": "TB-OLD"}]
        fresh = [dict(items[0], validated_against_build_id="TB-NEW")]
        out.append(_replay(
            "stale_build_ready_authority",
            defect_fires=(rg.item_currency(root, "TB-NEW", items)["state"] == "RED"),
            corrected_fires=(rg.item_currency(root, "TB-NEW", fresh)["state"] == "RED"),
            evidence=("a BUILD_READY item validated against an older build is stale authority. "
                      "ISA-0467 itself stood BUILD_READY for twelve days while six builds "
                      "shipped most of its corrective action"),
            historical=["ISA-0467", "ISA-0454", "ISA-0430"]))
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "hollow.py"), "w", encoding="utf-8") as fh:
            fh.write("def _selftest():\n    return 0\n")
        with open(os.path.join(d, "real.py"), "w", encoding="utf-8") as fh:
            fh.write('def _selftest():\n    ok("NEGATIVE CONTROL: must fail", True)\n    return 0\n')
        cen = rg.negative_control_census(["hollow", "real"], d)
        out.append(_replay(
            "vacuous_selftest",
            defect_fires=any(r["module"] == "hollow" and r["state"] == "RED"
                             for r in cen["rows"]),
            corrected_fires=any(r["module"] == "real" and r["state"] == "RED"
                                for r in cen["rows"]),
            evidence=("a selftest with no assertions and no labelled negative control FAILS the "
                      "census; one carrying a control passes"),
            historical=["ISA-0348", "ISA-0513", "ISA-0625"]))
    except Exception as exc:                                            # noqa: BLE001
        out.append({"control": "run_surface_and_currency_replays",
                    "state": "ENVIRONMENT_UNKNOWN", "evidence": str(exc)[:200]})
    return out


# ────────────────────────────────────────────────────────────────────────────────────────
# CLASS COVERAGE — a census, labelled a census
# ────────────────────────────────────────────────────────────────────────────────────────

def class_coverage(root: str = HERE, reps: Optional[List[dict]] = None) -> dict:
    reps = reps if reps is not None else replays(root)
    covered = set()
    for r in reps:
        if r.get("state") == "GREEN":
            covered |= set(r.get("claims_classes") or ())
    items = _items(root)
    counts = Counter(i.get("failure_class") for i in items if i.get("failure_class"))
    untagged = sum(1 for i in items if not i.get("failure_class"))
    rows = []
    for cls, n in counts.most_common():
        rows.append({"failure_class": cls, "n_historical_items": n,
                     "addressed_by_a_green_replay": cls in covered,
                     "controls": sorted(c for c, cl in CONTROL_CLAIMS.items()
                                        if cls in cl
                                        and any(r["control"] == c and r.get("state") == "GREEN"
                                                for r in reps))})
    n_cov = sum(r["n_historical_items"] for r in rows if r["addressed_by_a_green_replay"])
    n_all = sum(r["n_historical_items"] for r in rows)
    return {
        "basis": ("⚑ THIS IS A CENSUS, NOT A PROOF. It says which failure CLASSES a green "
                  "replay addresses and how many historical items sit in each. It does NOT say "
                  "those items would have been caught — most cannot be re-executed, and "
                  "re-creating their code or data would be fabrication (R7.5). The executable "
                  "evidence is the `replays` block; these two are never blended (R6.2)."),
        "n_items_in_register": len(items),
        "n_untagged_failure_class": untagged,
        "untagged_note": ("%d of %d items carry no failure class, so this census speaks for "
                          "%.0f%% of the register at most (R3.2: report effective N, not "
                          "nominal N)" % (untagged, len(items),
                                          100.0 * n_all / max(1, len(items)))),
        "n_classified": n_all,
        "n_in_addressed_classes": n_cov,
        "pct_of_classified": round(100.0 * n_cov / n_all, 1) if n_all else None,
        "rows": rows,
        "uncovered_classes": [r["failure_class"] for r in rows
                              if not r["addressed_by_a_green_replay"]],
    }


def report(root: str = HERE) -> dict:
    reps = replays(root)
    cov = class_coverage(root, reps)
    red = [r for r in reps if r.get("state") == "RED"]
    env = [r for r in reps if r.get("state") == "ENVIRONMENT_UNKNOWN"]
    return {
        "as_of": _today(),
        "state": "PASS" if not red and not env else ("FAIL" if red else "ENVIRONMENT_UNKNOWN"),
        "n_replays": len(reps), "n_green": len(reps) - len(red) - len(env),
        "n_red": len(red), "n_environment_unknown": len(env),
        "replays": reps,
        "class_coverage": cov,
        "misses": {
            "uncovered_classes": cov["uncovered_classes"],
            "why_it_matters": ("R5.13 requires misses to be reported as loudly as catches. The "
                               "classes above have historical items and NO green replay in this "
                               "build — naming them is what stops 'ten green replays' being "
                               "read as 'the corpus is covered'."),
        },
        "r5_13": ("one synthetic fixture cannot justify 'class killed' — so each replay asserts "
                  "BOTH that the control fires on the defect shape AND that it stays silent on "
                  "the corrected shape, and the class census is published beside it as a "
                  "separate, weaker measurement"),
    }


_ASSERTS = [0]


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        _ASSERTS[0] += 1
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:240]) if not cond else ""))
        if not cond:
            fails.append(name)

    rep = report(HERE)
    ok("every declared replay produced a verdict rather than being skipped",
       len(rep["replays"]) >= 9, len(rep["replays"]))
    ok("⚑ a replay is GREEN only when the control fires on the defect AND stays silent on the "
       "fix - a control that fires on everything would 'catch' the whole corpus and be useless",
       all(("state" not in r) or r["state"] != "GREEN"
           or (r["fires_on_defect_shape"] and r["silent_on_corrected_shape"])
           for r in rep["replays"]))
    ok("no replay is RED", rep["n_red"] == 0,
       [r["control"] for r in rep["replays"] if r.get("state") == "RED"])

    # ⚑ THE CONTROL ON THE BACKTEST ITSELF. A backtest that cannot report a miss is a
    #   press release. Force a class with items and no green replay and assert it surfaces.
    cov = class_coverage(HERE, [{"control": "x", "state": "GREEN", "claims_classes": ["FC-E"]}])
    ok("⚑ NEGATIVE CONTROL ON THE BACKTEST: with only one control claiming one class, every "
       "other class with historical items is reported UNCOVERED - the misses are visible, not "
       "implied (R5.13, R2.10)",
       "FC-B" in cov["uncovered_classes"] and "FC-E" not in cov["uncovered_classes"],
       cov["uncovered_classes"])
    ok("...and the census states its own effective N rather than quoting a flattering "
       "percentage of the whole register (R3.2)",
       cov["n_untagged_failure_class"] > 0 and "effective N" in cov["untagged_note"])
    ok("the census is labelled a census and the executable evidence is kept separate (R6.2)",
       "NOT A PROOF" in cov["basis"] and "replays" in rep)

    if verbose:
        print("\ndefect_corpus_backtest selftest: %d assertion(s), %d FAIL(s)%s"
              % (_ASSERTS[0], len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    r = report()
    if "--json" in argv:
        print(json.dumps(r, indent=2))
        return 0 if r["state"] == "PASS" else 1
    print("defect corpus backtest %s — %d replay(s): %d green, %d red, %d environment-unknown"
          % (r["as_of"], r["n_replays"], r["n_green"], r["n_red"], r["n_environment_unknown"]))
    for rep in r["replays"]:
        print("  %-8s %-38s %s" % (rep.get("state", "?"), rep["control"],
                                   ", ".join(rep.get("historical_items") or [])))
    c = r["class_coverage"]
    print("\nclass coverage (CENSUS, not proof): %d of %d classified items sit in a class some "
          "green replay addresses (%.1f%%); %d of %d items carry no class at all"
          % (c["n_in_addressed_classes"], c["n_classified"], c["pct_of_classified"] or 0.0,
             c["n_untagged_failure_class"], c["n_items_in_register"]))
    for row in c["rows"]:
        print("  %-6s n=%-4d %s" % (row["failure_class"], row["n_historical_items"],
                                    "addressed" if row["addressed_by_a_green_replay"]
                                    else "⚑ NO GREEN REPLAY"))
    return 0 if r["state"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
