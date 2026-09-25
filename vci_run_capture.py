#!/usr/bin/env python3
"""
vci_run_capture.py — VCI run auditability, Dashboard Spec §7.6B.2-4. 05-Aug-2026.

THE GAP THIS CLOSES
--------------------
Section 3 of the VCI run produces ACS dimension scores, the Part A threshold verdict, and
several explicitly discretionary calls (pre-inflection override, NVIDIA-class bypass, Exception
Track, A12 consensus-stale, estimated-FV manual-confirm holds, ACS7 management-departure cuts) —
all of which currently land ONLY as compressed prose in `project_vci_output_[mmm]_[yyyy].md`.
This is the VCI equivalent of the Step 9 capture (`conviction_capture.py`, §7.6.2): the session
judgement behind the sleeve where Raj's stated confidence gap actually sits is unreconstructable
without it.

WHAT THIS IS (§7.6B.2-4, implemented as written)
--------------------------------------------------
  new_run()        skeleton for `vci_run_[mmm]_[yyyy].json` (schema_version 1).
  add_candidate()  one Section-3-scored candidate, built from the SAME evaluate_candidate()
                   verdict dict the run already produces (vci_deploy_eval) plus the ACS
                   dimension detail and catalyst info the session has to hand — one source of
                   truth, not a re-derivation.
  detect_overrides() §7.6B.3: flags the six discretionary conditions from the assembled
                   candidate data. Where the evidence is a SESSION JUDGEMENT the spec itself
                   requires a human read for (Exception Track's three qualification criteria,
                   an ACS7 cut on management departure), this returns "pending_review" rather
                   than asserting a verdict — it surfaces the question, it does not answer it
                   (build hazard H7: capture observes, it never calibrates).
  add_discard()    §7.6B.4: Part-A-threshold discards, ticker/score/reason/theme/run_date.
  validate()       refuses an incomplete document (missing top-level keys, a candidate missing
                   a schema-required field, a discard missing its reason).
  write()          emits vci_run_[mmm]_[yyyy].json atomically.

CLI:
  python3 vci_run_capture.py --selftest
  python3 vci_run_capture.py --new --month aug_2026 --out /path/to/dir

Stdlib only.
"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1

CANDIDATE_REQUIRED = (
    "ticker", "theme", "layer", "market_cap", "part_a_score", "part_a_threshold_verdict",
    "acs_dimensions", "acs_total", "acs_ex_acs8", "fv_inputs", "bottleneck_fv_per_share",
    "fv_asymmetry", "fv_source", "fv_floor", "deploy_eligible", "require_manual_confirm",
    "vci_source_score", "size_pct", "signal_count", "signals", "catalyst", "decision",
)
DISCARD_REQUIRED = ("ticker", "part_a_score", "primary_failure_reason", "theme", "run_date")
OVERRIDE_TYPES = (
    "pre_inflection_override", "nvidia_class_bypass", "exception_track",
    "a12_consensus_stale", "estimated_fv_manual_confirm", "acs7_management_departure_cut",
)


# ── skeleton ─────────────────────────────────────────────────────────────────────────────

def _vci_capital_authority():
    """R18.5 / ISA-0629 — the VCI run's capital authority, checked when the run starts. An
    unavailable release gate is REFUSED, never AUTHORISED (R4.3)."""
    try:
        import release_gate as _rg
        return _rg.capital_run_authority("vci_run")
    except Exception as e:                                            # noqa: BLE001
        return {"surface": "vci_run", "authority": "REFUSED", "live_state": "UNKNOWN",
                "build_id": None, "why": "release_gate unavailable — %s: %s" % (type(e).__name__, e)}


def new_run(month_label, run_date=None, trusted_build=None):
    """Empty vci_run_[mmm]_[yyyy].json skeleton, stamped with the run's capital authority.

    ⚑ ISA-0629 (16-Sep-2026): the VCI run is a capital-decision run and no VCI surface asked
    whether LIVE matched a signed Trusted Build. `trusted_build` is recorded at init (the
    Run_Context's §7.6B.2 capture init is the first executed step), and `validate()` refuses to
    persist a deploy-eligible candidate whose decision is not an explicit refusal while the
    authority is not AUTHORISED. `trusted_build=` lets a fixture pass a record explicitly."""
    return {
        "schema_version": SCHEMA_VERSION,
        "month_label": month_label,
        "run_date": run_date or datetime.now().strftime("%Y-%m-%d"),
        "trusted_build": trusted_build if trusted_build is not None else _vci_capital_authority(),
        "candidates": [],
        "overrides": [],
        "discards": [],
        "fast_screen_discards": [],   # §7.6B.4: Section 2 / Checkpoint B discards, same shape
    }


# ── candidates (§7.6B.2) ────────────────────────────────────────────────────────────────

def add_candidate(run, *, ticker, theme, layer, market_cap, part_a_score,
                  part_a_threshold_verdict, acs_dimensions, acs_total, acs_ex_acs8,
                  fv_inputs, verdict, signals, catalyst, decision):
    """Append one Section-3 candidate.

    `verdict` is the dict returned by `vci_deploy_eval.evaluate_candidate()` for this candidate
    at the VCI-run price — the single source of truth for bottleneck_fv_per_share, fv_asymmetry,
    fv_source, fv_floor, deploy_eligible, require_manual_confirm, vci_source_score, size_pct.
    Re-deriving these here instead of reading them from `verdict` is exactly the two-sources-of-
    truth class this capture layer exists to prevent.
    """
    entry = {
        "ticker": ticker, "theme": theme, "layer": layer, "market_cap": market_cap,
        "part_a_score": part_a_score, "part_a_threshold_verdict": part_a_threshold_verdict,
        "acs_dimensions": acs_dimensions, "acs_total": acs_total, "acs_ex_acs8": acs_ex_acs8,
        "fv_inputs": fv_inputs,
        "bottleneck_fv_per_share": verdict.get("bottleneck_fv_per_share"),
        "fv_asymmetry": verdict.get("fv_asymmetry"),
        "fv_asymmetry_p25": verdict.get("fv_asymmetry_p25"),
        "fv_source": verdict.get("fv_source"),
        "fv_floor": verdict.get("fv_floor"),
        "deploy_eligible": verdict.get("deploy_eligible"),
        "require_manual_confirm": verdict.get("require_manual_confirm"),
        "vci_source_score": verdict.get("vci_source_score"),
        "size_pct": verdict.get("size_pct"),
        "signal_count": len(signals or []),
        "signals": signals or [],
        "catalyst": catalyst,
        "decision": decision,
    }
    run["candidates"].append(entry)
    for ov in detect_overrides(entry):
        run["overrides"].append(ov)
    return entry


# ── discretionary overrides (§7.6B.3) ───────────────────────────────────────────────────

def detect_overrides(candidate):
    """Flag the six §7.6B.3 conditions from an assembled candidate entry.

    Three are mechanically decidable from data already on the entry (NVIDIA-class ACS/signal
    floor, A12 consensus-stale note text, fv_source=="estimated"). Three require a session read
    the spec itself grounds in judgement (pre-inflection's FCF history, Exception Track's three
    criteria, an ACS7 cut specifically FOR management departure vs some other reason) -- for
    those this returns "pending_review": true so the condition is visible and cannot be silently
    skipped, without this module pretending to have made the call.
    """
    t = candidate["ticker"]
    out = []
    acs_dims = candidate.get("acs_dimensions") or {}

    # NVIDIA-class bypass: ACS >=85, 5-6 signals. Recency ("VCI run within 2 weeks of the
    # monthly review") is a fact about the monthly review's date, not knowable at VCI-run time --
    # flagged eligible here; the monthly pre-run re-price step is where recency is checked.
    acs_total = candidate.get("acs_total")
    sig_n = candidate.get("signal_count", 0)
    if acs_total is not None and acs_total >= 85 and 5 <= sig_n <= 6:
        out.append({"ticker": t, "override_type": "nvidia_class_bypass",
                    "evidence": f"acs_total={acs_total} signal_count={sig_n}",
                    "condition_met": True,
                    "note": "Recency (VCI run within 2 weeks of monthly review) not evaluable "
                            "here -- confirm at the monthly review's Step 9 VCI ticks."})

    # Exception Track: $50-100B market cap band is mechanical; the three qualification criteria
    # (2-3x return ceiling, pre-revenue-recognition, 4/4 bottleneck test) are a Section 2.3
    # session read that this module cannot reconstruct from a verdict dict.
    mc = candidate.get("market_cap")
    if mc is not None and 50e9 <= mc <= 100e9:
        out.append({"ticker": t, "override_type": "exception_track",
                    "evidence": f"market_cap={mc}",
                    "condition_met": "pending_review",
                    "note": "Market-cap band qualifies for Exception Track; confirm all THREE "
                            "Section 2.3 criteria were checked (2-3x ceiling, pre-revenue-"
                            "recognition, 4/4 bottleneck test) before treating as applied."})

    # A12 consensus-stale marker: text-detected from the A12 dimension note.
    a12 = acs_dims.get("A12") or acs_dims.get("a12") or {}
    a12_note = str(a12.get("note", "")) if isinstance(a12, dict) else ""
    if "STALE" in a12_note.upper() or "CONSENSUS LAG" in a12_note.upper():
        out.append({"ticker": t, "override_type": "a12_consensus_stale",
                    "evidence": a12_note, "condition_met": True, "note": ""})

    # Estimated-FV manual-confirm hold: mechanical, straight from the verdict.
    if candidate.get("fv_source") == "estimated":
        out.append({"ticker": t, "override_type": "estimated_fv_manual_confirm",
                    "evidence": f"fv_source=estimated require_manual_confirm="
                                f"{candidate.get('require_manual_confirm')}",
                    "condition_met": True, "note": ""})

    # Pre-inflection override: Part A >=7 (below the 11 standard) with three specific conditions
    # (A5=2, market_cap<$5B, no positive FCF in 3yr) -- A5/FCF history are not on the entry.
    pa = candidate.get("part_a_score")
    if pa is not None and 7 <= pa < 11:
        out.append({"ticker": t, "override_type": "pre_inflection_override",
                    "evidence": f"part_a_score={pa} (below standard 11 threshold)",
                    "condition_met": "pending_review",
                    "note": "Confirm all THREE conditions independently: A5=2 (R&D >15% "
                            "revenue), market_cap <$5B, no positive FCF in any of last 3 FY."})

    # ACS7 management-departure cut: text-detected from the A7/ACS7 dimension note.
    a7 = acs_dims.get("A7") or acs_dims.get("ACS7") or acs_dims.get("a7") or {}
    a7_note = str(a7.get("note", "")) if isinstance(a7, dict) else ""
    if "DEPARTURE" in a7_note.upper():
        out.append({"ticker": t, "override_type": "acs7_management_departure_cut",
                    "evidence": a7_note, "condition_met": "pending_review",
                    "note": "Confirm the ACS7 reduction was applied because of this departure "
                            "specifically, not a coincidental low score for another reason."})

    return out


# ── discards (§7.6B.4) ──────────────────────────────────────────────────────────────────

def add_discard(run, *, ticker, part_a_score, primary_failure_reason, theme, run_date=None,
                fast_screen=False):
    """Section 3 Part-A-threshold discard, or (fast_screen=True) a Section 2 / Checkpoint B
    fast-screen discard -- same shape, §7.6B.4 requires both captured structurally."""
    entry = {"ticker": ticker, "part_a_score": part_a_score,
             "primary_failure_reason": primary_failure_reason, "theme": theme,
             "run_date": run_date or run.get("run_date")}
    run["fast_screen_discards" if fast_screen else "discards"].append(entry)
    return entry


# ── validate / write ────────────────────────────────────────────────────────────────────

def validate(run):
    """Refuse an incomplete document. Returns (ok, errors[])."""
    errs = []
    for k in ("schema_version", "month_label", "run_date", "candidates", "overrides",
             "discards"):
        if k not in run:
            errs.append(f"missing top-level key: {k}")
    for i, c in enumerate(run.get("candidates", [])):
        for k in CANDIDATE_REQUIRED:
            if k not in c:
                errs.append(f"candidate[{i}] ({c.get('ticker', '?')}) missing field: {k}")
    for i, d in enumerate(run.get("discards", []) + run.get("fast_screen_discards", [])):
        for k in DISCARD_REQUIRED:
            # part_a_score=0 is a legitimate value, not a missing one -- check presence/None,
            # not truthiness, or this reintroduces the exact null-vs-missing bug class the
            # capture layer exists to guard against.
            v = d.get(k, "__MISSING__")
            if v == "__MISSING__" or v is None or (isinstance(v, str) and v.strip() == ""):
                errs.append(f"discard[{i}] ({d.get('ticker', '?')}) missing/empty field: {k}")
    for i, o in enumerate(run.get("overrides", [])):
        if o.get("override_type") not in OVERRIDE_TYPES:
            errs.append(f"override[{i}] unknown override_type: {o.get('override_type')}")
    # ⚑ R18.5 / ISA-0629 — no capital decision on an untrusted tree. The authority is read
    #   through release_gate's one reader shape; absence is REFUSED (R4.3). A deploy-eligible
    #   candidate must carry a decision that STARTS with "REFUSED" while it is not AUTHORISED.
    _auth = (run.get("trusted_build") or {}).get("authority")
    if _auth not in ("AUTHORISED", "NOT_ENFORCED"):
        for i, c in enumerate(run.get("candidates", [])):
            if c.get("deploy_eligible") and not str(c.get("decision") or "").upper().startswith("REFUSED"):
                errs.append(
                    f"candidate[{i}] ({c.get('ticker', '?')}) is deploy-eligible with decision "
                    f"{str(c.get('decision'))[:60]!r} but the run's capital authority is {_auth!r} "
                    f"(R18.5: LIVE does not match a signed Trusted Build, or ISA-0696: its suite census "
                    f"is not FRESH_GREEN). Record the decision as 'REFUSED — UNTRUSTED_LIVE_STATE' / "
                    f"'REFUSED — SUITE_CENSUS_<state>' or reconcile LIVE first.")
    return (not errs), errs


def write(run, here=None, month_label=None, *, ledger_path=None, capture_decisions=True,
          held=None):
    """Atomic write of vci_run_[mmm]_[yyyy].json. Refuses to write an invalid document.

    ⚑ ISA-0686 (20-Sep-2026): AT THE MOMENT THE ARTEFACT IS PERSISTED, every scored candidate
    is also written into `decision_ledger.json` through `decision_ledger.log_decision` — the
    same function the growth route reaches via `checkpoint_d.log_top10` (R4.5: two paths call
    one function, or they are one function). Before this, the VCI route wrote its decisions to
    an artefact nothing copied, so the sleeve's first ever deployment (QBTS, 09-Aug-2026)
    reconciled as `bought_outside_framework` — a standing accusation against the operator for
    following the framework's own instruction at the size it specified.

    The ledger write is NOT best-effort. A persisted deploy artefact whose capital-effective
    names are absent from the ledger is precisely the breach, so a capture failure RAISES and
    the boundary contract `decision_ledger.assert_vci_captured` is re-checked afterwards.
    `capture_decisions=False` is the R4.13 rollback and must be stated by the caller.
    """
    here = here or HERE
    month_label = month_label or run.get("month_label")
    ok, errs = validate(run)
    if not ok:
        raise ValueError("vci_run_capture: refusing to write invalid document: " + "; ".join(errs))
    path = os.path.join(here, f"vci_run_{month_label}.json")
    fd, tmp = tempfile.mkstemp(dir=here, prefix=".vci_run_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(run, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if capture_decisions:
        run["decision_capture"] = _capture_to_ledger(run, path, here, ledger_path, held)
        # ── ISA-0716 (§5.9) — the VCI run is one of the THREE lifecycle observers. It runs the
        #    SAME held review as the pre-run (obligations untouched) at the moment the artefact
        #    is persisted, so a resolution first seen by this run is decided by this run, and an
        #    unchanged one revalidates the pre-run's decision rather than minting a second. A
        #    failure is RECORDED on the run document and never swallowed (R4.12).
        try:
            import vci_lifecycle as _lc
            import decision_ledger as _dl_lc
            run["lifecycle_observation"] = _lc.observe(
                "vci_run", root=here, ledger_path=ledger_path or _dl_lc.default_path(here),
                capital_authority=(run.get("trusted_build") or {}).get("authority") or "UNKNOWN")
        except Exception as exc:                                        # noqa: BLE001
            run["lifecycle_observation"] = {"state": "FAILED",
                                            "why": "%s: %s" % (type(exc).__name__, exc)}
    return path


def _capture_to_ledger(run, path, here, ledger_path, held):
    """ISA-0686 — the one capture point. Raises rather than writing a silent half-record."""
    import decision_ledger as _dl
    ledger_path = ledger_path or _dl.default_path(here)
    tb = run.get("trusted_build") or {}
    res = _dl.capture_vci_decisions(
        ledger_path, run, source_path=path,
        date=run.get("run_date"),
        build_id=tb.get("build_id"),
        authority=tb.get("authority") or "UNKNOWN",   # R4.3: unknown is never AUTHORISED
        held=held)
    chk = _dl.assert_vci_captured(ledger_path, run, source_path=path,
                                  authority=tb.get("authority") or "UNKNOWN")
    if not chk["ok"]:
        raise RuntimeError("ISA-0686 VERIFY FAILED after writing %s: %s" % (path, chk["why"]))
    res["contract"] = chk
    res["ledger_path"] = ledger_path
    return res


# ── selftest ─────────────────────────────────────────────────────────────────────────────

CANDIDATE_REQUIRED_FIXTURE = {k: None for k in CANDIDATE_REQUIRED}
CANDIDATE_REQUIRED_FIXTURE.update({"ticker": "ABS"})


def _selftest():
    fails = []

    def ok(label, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(label)

    run = new_run("aug_2026", run_date="2026-08-09")
    ok("VRC1 skeleton has all top-level keys",
       all(k in run for k in ("schema_version", "month_label", "run_date", "candidates",
                              "overrides", "discards", "fast_screen_discards")))

    verdict = {"bottleneck_fv_per_share": 19.6, "fv_asymmetry": 2.4168, "fv_asymmetry_p25": 1.8571,
              "fv_source": "modeled", "fv_floor": 2.0, "deploy_eligible": False,
              "require_manual_confirm": False, "vci_source_score": 51.0, "size_pct": 0.0}
    entry = add_candidate(run, ticker="ABCL", theme="Genomic AI", layer="Layer 3",
                          market_cap=1.2e9, part_a_score=15, part_a_threshold_verdict="PASS",
                          acs_dimensions={"A12": {"score": 0, "note": "CONSENSUS LAG — not a "
                                                  "negative signal"}},
                          acs_total=78, acs_ex_acs8=74,
                          fv_inputs={"latent_tam_usd_bn": 20.0, "capture_share": 0.12,
                                     "steady_margin": 0.35, "exit_multiple": 7.0,
                                     "fully_diluted_shares": 300e6, "asset_structure": "platform"},
                          verdict=verdict, signals=["s1", "s2", "s3", "s4", "s5", "s6"],
                          catalyst={"type": "phase2_readout", "date": "2026-10-28", "days": 80},
                          decision="WATCHLIST — not deploy-eligible (P25 asym below floor)")
    ok("VRC2 candidate carries all schema-required fields",
       all(k in entry for k in CANDIDATE_REQUIRED))
    ok("VRC3 candidate reads verdict fields FROM the verdict dict (single source of truth)",
       entry["fv_asymmetry"] == verdict["fv_asymmetry"]
       and entry["deploy_eligible"] == verdict["deploy_eligible"])
    ok("VRC4 A12 consensus-lag note auto-detected as an override",
       any(o["override_type"] == "a12_consensus_stale" for o in run["overrides"]))
    ok("VRC5 6-signal/ACS78 candidate does NOT wrongly trigger nvidia_class (ACS<85)",
       not any(o["override_type"] == "nvidia_class_bypass" for o in run["overrides"]))

    # NVIDIA-class + exception-track + pending-review cases
    run2 = new_run("aug_2026")
    v2 = dict(verdict, fv_source="estimated", require_manual_confirm=True)
    e2 = add_candidate(run2, ticker="HYP", theme="Test", layer="Layer 1", market_cap=60e9,
                       part_a_score=9, part_a_threshold_verdict="PRE_INFLECTION_OVERRIDE",
                       acs_dimensions={"A7": {"score": 1, "note": "CTO departure Mar-2026"}},
                       acs_total=86, acs_ex_acs8=81,
                       fv_inputs={"latent_tam_usd_bn": 10.0, "capture_share": 0.2,
                                  "steady_margin": 0.3, "exit_multiple": 6.0,
                                  "fully_diluted_shares": 50e6, "asset_structure": "single_asset"},
                       verdict=v2, signals=["s1", "s2", "s3", "s4", "s5"],
                       catalyst={"type": "launch", "date": "2026-11-01", "days": 84},
                       decision="MANUAL CONFIRM — estimated FV")
    types = {o["override_type"] for o in run2["overrides"]}
    ok("VRC6 NVIDIA-class bypass detected (ACS86, 5 signals)",
       "nvidia_class_bypass" in types)
    ok("VRC7 Exception Track flagged pending_review, not asserted true",
       any(o["override_type"] == "exception_track" and o["condition_met"] == "pending_review"
           for o in run2["overrides"]))
    ok("VRC8 estimated FV -> manual-confirm override captured",
       "estimated_fv_manual_confirm" in types)
    ok("VRC9 pre-inflection override flagged pending_review (Part A 9, below 11)",
       any(o["override_type"] == "pre_inflection_override" and o["condition_met"] == "pending_review"
           for o in run2["overrides"]))
    ok("VRC10 ACS7 management-departure cut flagged pending_review (never asserted true)",
       any(o["override_type"] == "acs7_management_departure_cut"
           and o["condition_met"] == "pending_review" for o in run2["overrides"]))

    add_discard(run2, ticker="COLD", part_a_score=6, primary_failure_reason="R&D <15% revenue; "
               "fails both standard and pre-inflection thresholds", theme="Test", run_date="2026-08-09")
    add_discard(run2, ticker="EARLY", part_a_score=0, primary_failure_reason="Universe screen: "
               "below QMS top-12 and no pre-inflection override / watchlist / cascade match",
               theme="Test", run_date="2026-08-09", fast_screen=True)
    ok("VRC11 Section-3 discard and fast-screen discard land in separate lists",
       len(run2["discards"]) == 1 and len(run2["fast_screen_discards"]) == 1)

    v_ok, v_errs = validate(run2)
    ok("VRC12 a complete document validates clean", v_ok, str(v_errs))

    bad = new_run("aug_2026")
    bad["candidates"].append({"ticker": "X"})   # missing everything else
    v_ok2, v_errs2 = validate(bad)
    ok("VRC13 an incomplete candidate FAILS validation (not silently accepted)",
       (not v_ok2) and any("X" in e for e in v_errs2))

    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        path = write(run2, here=td, month_label="aug_2026")
        ok("VRC14 write() produces vci_run_aug_2026.json", os.path.basename(path) ==
           "vci_run_aug_2026.json" and os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            reloaded = json.load(f)
        ok("VRC15 round-trip preserves candidate count", len(reloaded["candidates"]) == 1)
        try:
            write(bad, here=td, month_label="aug_2026")
            ok("VRC16 write() REFUSES an invalid document", False)
        except ValueError:
            ok("VRC16 write() REFUSES an invalid document", True)

    # ── ISA-0629 / R18.5 — a capital decision on an untrusted tree is refused at capture ───────
    ok("VRC17 new_run stamps a capital authority from the release gate (never absent)",
       (new_run("aug_2026").get("trusted_build") or {}).get("authority")
       in ("AUTHORISED", "REFUSED", "NOT_ENFORCED"))
    v_dep = dict(verdict, deploy_eligible=True, size_pct=0.75)
    def _run_with(auth, decision):
        r = new_run("aug_2026", run_date="2026-08-09",
                    trusted_build={"authority": auth, "live_state": "FIXTURE"})
        add_candidate(r, ticker="DEP", theme="T", layer="L1", market_cap=1e9, part_a_score=15,
                      part_a_threshold_verdict="PASS", acs_dimensions={}, acs_total=80,
                      acs_ex_acs8=76, fv_inputs={}, verdict=v_dep, signals=[], catalyst=None,
                      decision=decision)
        return validate(r)
    ok("VRC18 NEGATIVE CONTROL: REFUSED authority + deploy-eligible DEPLOY decision FAILS validation",
       not _run_with("REFUSED", "DEPLOY 0.75% starter")[0])
    ok("VRC18b NEGATIVE CONTROL: an ABSENT authority is treated as REFUSED",
       any("capital authority" in e for e in
           validate(dict(new_run("aug_2026", trusted_build={}),
                         candidates=[dict(CANDIDATE_REQUIRED_FIXTURE, deploy_eligible=True,
                                          decision="DEPLOY")]))[1]))
    ok("VRC19 POSITIVE CONTROL: AUTHORISED + the same DEPLOY decision validates",
       _run_with("AUTHORISED", "DEPLOY 0.75% starter")[0])
    ok("VRC20 POSITIVE CONTROL: REFUSED + an explicit 'REFUSED — UNTRUSTED_LIVE_STATE' decision validates",
       _run_with("REFUSED", "REFUSED — UNTRUSTED_LIVE_STATE")[0])


    # ══ ISA-0686 — the capture point is IN write(), not beside it ═══════════════════
    import decision_ledger as _dl
    with _tf.TemporaryDirectory() as td2:
        _lp = os.path.join(td2, "decision_ledger.json")
        _elig = _run_with("AUTHORISED", "DEPLOY 0.75% starter")[2] \
            if len(_run_with("AUTHORISED", "DEPLOY 0.75% starter")) > 2 else None
        r = new_run("oct_2026", run_date="2026-10-11",
                    trusted_build={"authority": "AUTHORISED", "build_id": "TB-TEST-99"})
        add_candidate(r, ticker="TSTA", theme="T", layer="Layer 3", market_cap=1e9,
                      part_a_score=12, part_a_threshold_verdict="pass",
                      acs_dimensions={"a": 1}, acs_total=80, acs_ex_acs8=75,
                      fv_inputs={"x": 1},
                      verdict={"bottleneck_fv_per_share": 20.0, "fv_asymmetry": 2.6,
                               "fv_asymmetry_p25": 2.4, "fv_source": "modeled",
                               "fv_floor": 2.0, "deploy_eligible": True,
                               "require_manual_confirm": False, "vci_source_score": 60.0,
                               "size_pct": 0.75},
                      signals=["s1"], catalyst="named catalyst",
                      decision="DEPLOY 0.75% starter")
        path = write(r, here=td2, month_label="oct_2026", ledger_path=_lp)
        _cur = _dl.current_decision(_lp, "TSTA", "vci")
        ok("⚑ ISA-0686 MUST-FIRE THROUGH THE REAL WRITE POINT: persisting the VCI run "
           "artefact writes the deploy decision into decision_ledger.json in the same call - "
           "the artefact and the ledger can no longer diverge",
           _cur is not None and _cur["decision"] == "buy", _cur)
        ok("ISA-0686: the captured decision names the BUILD and the INPUT SNAPSHOT of the "
           "artefact it came from (R4.2: a value with no source is not evidence)",
           _cur and _cur["build_id"] == "TB-TEST-99"
           and str(_cur["input_snapshot_id"]).startswith("vci_run_oct_2026.json@"))
        _lo = r.get("lifecycle_observation") or {}
        ok("⚑ ISA-0716 (§5.9): the VCI run is a LIFECYCLE OBSERVER at its own write point - "
           "write() runs vci_lifecycle.observe('vci_run') and publishes the result; in a "
           "scratch tree with no broker book it observes NOTHING and writes nothing (never "
           "another tree's ledger)",
           _lo.get("observed_by") == "vci_run" and _lo.get("state") == "NO_BOOK", _lo)
        ok("ISA-0686: write() publishes its own capture receipt and the boundary contract "
           "verdict on the run document",
           r["decision_capture"]["contract"]["ok"]
           and r["decision_capture"]["ledger_path"] == _lp)
        _n = len(_dl.load_ledger(_lp)["entries"])
        write(r, here=td2, month_label="oct_2026", ledger_path=_lp)
        ok("ISA-0686: re-writing the same run is idempotent in the ledger",
           len(_dl.load_ledger(_lp)["entries"]) == _n)

    with _tf.TemporaryDirectory() as td3:
        _lp3 = os.path.join(td3, "decision_ledger.json")
        r2 = new_run("oct_2026", run_date="2026-10-11",
                     trusted_build={"authority": "AUTHORISED", "build_id": "TB-TEST-99"})
        add_candidate(r2, ticker="TSTB", theme="T", layer="Layer 3", market_cap=1e9,
                      part_a_score=12, part_a_threshold_verdict="pass",
                      acs_dimensions={"a": 1}, acs_total=80, acs_ex_acs8=75,
                      fv_inputs={"x": 1},
                      verdict={"bottleneck_fv_per_share": 20.0, "fv_asymmetry": 2.6,
                               "fv_asymmetry_p25": 2.4, "fv_source": "modeled",
                               "fv_floor": 2.0, "deploy_eligible": True,
                               "require_manual_confirm": False, "vci_source_score": 60.0,
                               "size_pct": 0.75},
                      signals=["s1"], catalyst="c", decision="DEPLOY 0.75% starter")
        write(r2, here=td3, month_label="oct_2026", ledger_path=_lp3,
              capture_decisions=False)
        ok("ISA-0686 ROLLBACK (R4.13): capture_decisions=False restores the previous "
           "behaviour exactly - the artefact is written and the ledger is untouched",
           not os.path.exists(_lp3) and "decision_capture" not in r2)

    ok("⚑ ISA-0686 NEGATIVE CONTROL: the boundary contract still FAILS on a real artefact "
       "whose capital-effective name was never captured - the control is not self-satisfying",
       not _dl.assert_vci_captured(
           os.path.join(_tf.mkdtemp(), "empty_ledger.json"),
           {"ZZZ": {"ticker": "ZZZ", "deploy_eligible": True,
                    "require_manual_confirm": False}})["ok"])

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)}) {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--new", action="store_true")
    ap.add_argument("--month")
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.new:
        if not a.month:
            ap.error("--month required with --new")
        run = new_run(a.month)
        path = write(run, here=a.out, month_label=a.month)
        print(f"VCI_RUN_CAPTURE new skeleton -> {path}")
        return 0
    ap.error("--selftest or --new required")


if __name__ == "__main__":
    sys.exit(main())
