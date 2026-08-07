#!/usr/bin/env python3
"""
consistency_check.py — Fix Pack A18 v1 (12-Jul-2026). Prose↔config invariant checker.

The June desync class (prose contract says one thing, code/config another) is caught
mechanically: N known invariant pairs asserted at every pre-run; any mismatch goes to
run_context errors[] (run continues; the review session sees the warning per the existing
ERROR protocol). Seeded with the pairs the Fable5 review + Fix Pack build touched; GROW BY
ONE PAIR per future desync — never fix a desync without adding its check here.

Usage:
  python3 consistency_check.py             # run all pairs, print PASS/FAIL, exit 1 on any FAIL
  python3 consistency_check.py --selftest  # U-A18: seeded pairs pass, mutated pair fails
Library:
  from consistency_check import check_all
  errs = check_all()          # [] when green; strings for run_context["errors"]

Stdlib only.
"""
import argparse, ast, builtins, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8", errors="ignore") as f:
        return f.read()


# ── pair implementations (take text so the self-test can feed mutations) ────────────────

# Markers that make a line HISTORICAL rather than operative. A correction note must be able to
# quote the rule it retired without tripping the very pair that enforces the retirement, so any
# line carrying one of these is skipped by every prose pair. Extended 31-Jul-2026 from the
# original SUPERSEDED-only rule when the monthly pairs landed (they annotate in place).
HISTORICAL_MARKERS = ("SUPERSEDED", "CORRECTED", "RETIRED", "REMOVED")


def _live_lines(text):
    """Lines that are OPERATIVE prose — historical/changelog lines that explicitly mark
    themselves (SUPERSEDED / CORRECTED / RETIRED / REMOVED, any case) may legitimately quote
    retired rules and are skipped."""
    out = []
    for ln in text.splitlines():
        up = ln.upper()
        if any(m in up for m in HISTORICAL_MARKERS):
            continue
        out.append(ln)
    return out


def pair_stale_partb(run_ctx_text):
    """A7b: the stale 'Part B >= 14' SUMMARY gate must not reappear in LIVE Run_Context_Growth
    prose; the constant NAME must be referenced instead (invariant 2: config over prose)."""
    errs = []
    for ln in _live_lines(run_ctx_text):
        if re.search(r"Part\s*B\s*(?:≥|>=)\s*14", ln):
            errs.append("A18/A7b: stale 'Part B >= 14' gate text present in Run_Context_Growth")
            break
    if "SUMMARY_PART_B_FLOOR" not in run_ctx_text:
        errs.append("A18/A7b: Run_Context_Growth no longer references SUMMARY_PART_B_FLOOR")
    return errs


def pair_summary_floor_prose(run_ctx_text):
    """A1: SUMMARY selection prose must reference the constants, not restate numbers."""
    errs = []
    for const in ("SUMMARY_SOURCE_FLOOR", "SUMMARY_MAX_COUNT"):
        if const not in run_ctx_text:
            errs.append(f"A18/A1: Run_Context_Growth does not reference {const}")
    for ln in _live_lines(run_ctx_text):
        if re.search(r"top[\s-]*30\b", ln, re.I):
            errs.append("A18/A1: stale 'top 30' selection text present in Run_Context_Growth")
            break
    return errs


def pair_top10_columns(run_ctx_text, build_email_text):
    """Email contract: 'exactly these N' in Run_Context == len(header_cols) in build_email."""
    m = re.search(r"exactly these (\d+)", run_ctx_text)
    if not m:
        return ["A18/email: Run_Context_Growth top-10 'exactly these N' clause missing"]
    want = int(m.group(1))
    mb = re.search(r"header_cols = \[(.*?)^\s*\]", build_email_text, re.S | re.M)
    # Fix 13-Jul-26: non-greedy stop at first "]" swallowed the "E[r]" column (12 vs 13);
    # now match the closing bracket on its own line.
    if not mb:
        return ["A18/email: build_email.py header_cols block not found"]
    got = len(re.findall(r'"[^"]+"', mb.group(1)))
    if got != want:
        return [f"A18/email: top-10 column count desync — Run_Context says {want}, build_email has {got}"]
    return []


def pair_email_sections(run_ctx_text, build_email_text):
    """'Email — N Mandatory Sections' in Run_Context == '# Section n —' markers in build_email."""
    m = re.search(r"Email — (\d+) Mandatory Sections", run_ctx_text)
    if not m:
        return ["A18/email: Run_Context_Growth mandatory-sections heading missing"]
    want = int(m.group(1))
    got = len(set(re.findall(r"# Section (\d+) —", build_email_text)))
    if got != want:
        return [f"A18/email: section count desync — Run_Context says {want}, build_email emits {got}"]
    return []


def pair_retired_constants(py_texts):
    """A1/A7: retired constants must have NO live consumer ({filename: text})."""
    errs = []
    for fn, txt in py_texts.items():
        if "SUMMARY_TARGET_COUNT" in txt:
            errs.append(f"A18/A1: retired SUMMARY_TARGET_COUNT still referenced in {fn}")
    return errs


def pair_anchor(state: dict, required_return_mid=None):
    """A19: anchor file coherent + fresh; loader actually consumed it (not the fallback)."""
    errs = []
    try:
        if str(state.get("derived_at", "")) < str(state.get("schedule_updated_at", "")):
            errs.append("A18/A19: contribution_schedule changed AFTER last derivation — rerun derive_required_return.py")
        if state.get("guardrail_state") not in ("OK", "TARGET_ATTAINABILITY_REVIEW", "GLIDEPATH_REVIEW"):
            errs.append(f"A18/A19: unexpected guardrail_state {state.get('guardrail_state')!r} (FALLBACK = loader failed)")
        op = float(state["required_return_operative_pct"])
        if not (10.0 <= op <= 18.0):
            errs.append(f"A18/A19: operative anchor {op} outside D1c band 10.0–18.0")
        if required_return_mid is not None and abs(float(required_return_mid) - op) > 1e-9:
            errs.append(f"A18/A19: scoring_config.REQUIRED_RETURN_MID {required_return_mid} != anchor operative {op}")
    except (KeyError, TypeError, ValueError) as e:
        errs.append(f"A18/A19: target_state.json malformed ({e})")
    return errs


def pair_max_scale(cfg):
    """/50-/54 handling: extended max must stay consistent with base (+4 conditional Part B)."""
    errs = []
    if getattr(cfg, "GROWTH_TOTAL_MAX", None) != 50 or getattr(cfg, "GROWTH_TOTAL_MAX_EXTENDED", None) != 54:
        errs.append("A18: GROWTH_TOTAL_MAX/_EXTENDED no longer 50/54 — update every '/50-/54' consumer + this pair")
    if getattr(cfg, "GROWTH_PART_B_MAX_EXTENDED", 0) - getattr(cfg, "GROWTH_PART_B_MAX", 0) != \
       getattr(cfg, "GROWTH_TOTAL_MAX_EXTENDED", 0) - getattr(cfg, "GROWTH_TOTAL_MAX", 0):
        errs.append("A18: extended Part-B delta != extended Total delta (conditional-metric drift)")
    return errs


# ── MONTHLY REVIEW pairs (added 31-Jul-2026) ─────────────────────────────────────────────
# Until this date every pair above read Run_Context_ISA_Growth_Stock_Analysis.md ONLY, so the
# monthly review's execution guide had ZERO mechanical coverage — the checker reported ALL PAIRS
# GREEN while the monthly prose carried a miscounted action-category list, a self-contradicting
# T1 definition, two retired price-vs-entry output tables, five live Path C instructions, and a
# pre-run recipe naming a script that did not exist. Same standing rule as above: GROW BY ONE
# PAIR per future desync.

MONTHLY_CTX = "Run_Context_Monthly_ISA_Review.md"

# Scripts the pre-run and monthly review actually execute. Kept in step with the SCRIPTS dict
# in monthly_isa_prerun.py and the pre-run task's maintenance rule. Name-resolution (M8) runs
# over these; a file listed here but absent is skipped by check_all (M7 covers absence).
TRACKED_SCRIPTS = (
    "monthly_isa_prerun.py", "fetch_watchlist_metrics.py", "normalise_adapter.py",
    "rerank_watchlist.py", "step9_pre_builder.py", "email_prefill.py", "extract_portfolio.py",
    "extract_transactions.py", "extract_xray.py", "portfolio_analytics.py", "update_watchlist.py",
    "build_monthly_isa_email.py", "entry_level_builder.py", "sync_vci_watchlist.py",
    "decision_ledger.py", "checkpoint_d.py", "compliance.py", "etf_tactical.py",
    "position_alerts.py", "calibration_report.py", "fund_returns.py", "drawdown_monitor.py",
    "consistency_check.py", "scoring_config.py",
    # 01-Aug-26: isa_env_guard.py added — it is imported by fetch_watchlist_metrics.py and
    # owns the tmpfs guard + the H-4 secrets loader, so a stale copy on the Composio
    # fallback path would silently change where temp files and API keys come from.
    "isa_env_guard.py",
    # 02-Aug-26: capture_archive.py added — Capture Layer Item 0. It owns the archive/purge
    # split at post-run cleanup, so a stale or absent copy silently reverts the framework to
    # deleting step9_pre and the decision record stops accruing again.
    "capture_archive.py",
    "regime_resolver.py",
)
# Kept identical (by design, checked at self-test time is not automated — see the pre-run
# task's MAINTENANCE RULE list) to the set of scripts whose edits must be pushed to GitHub.
# If you add a script to one list, add it to the other.


def pair_monthly_action_categories(ctx_text):
    """Step 8's stated category COUNT must equal the number of categories it enumerates.
    The 31-Jul-26 defect: 'rank all 7 action categories' above a list of 8. Category 8 is
    explicitly 'unforced', so a miscount drops it silently and it is never ranked."""
    m = re.search(r"assess and rank all (\d+) action categories", ctx_text)
    if not m:
        return ["A18/M1: Step 8 'assess and rank all N action categories' clause missing"]
    want = int(m.group(1))
    body = ctx_text.split("## Step 8 — Build and Rank the Monthly Action Set", 1)
    if len(body) < 2:
        return ["A18/M1: Step 8 heading not found"]
    seg = body[1].split("**Category 2 — top-up criteria", 1)[0]
    got = len(re.findall(r"^(\d+)\.\s+\S", seg, re.M))
    if got != want:
        return [f"A18/M1: Step 8 category count desync — prose says {want}, list enumerates {got}"]
    # every other restatement of the count must agree
    for n in re.findall(r"main (\d+)-category ranking", ctx_text):
        if int(n) != want:
            return [f"A18/M1: 'main {n}-category ranking' contradicts the stated {want}"]
    for n in re.findall(r"ranked list of all (\d+) action categories", ctx_text):
        if int(n) != want:
            return [f"A18/M1: 'ranked list of all {n}' contradicts the stated {want}"]
    for n in re.findall(r"all (\d+) categories ranked", ctx_text):
        if int(n) != want:
            return [f"A18/M1: Checkpoint-D 'all {n} categories ranked' contradicts the stated {want}"]
    return []


def pair_monthly_email_sections(ctx_text, builder_text):
    """'Email Structure — N Mandatory Sections' == len(SECTION_ORDER) in the monthly builder."""
    m = re.search(r"Email Structure — (\d+) Mandatory Sections", ctx_text)
    if not m:
        return ["A18/M2: monthly 'Email Structure — N Mandatory Sections' heading missing"]
    want = int(m.group(1))
    mb = re.search(r"SECTION_ORDER = \[(.*?)^\]", builder_text, re.S | re.M)
    if not mb:
        return ["A18/M2: build_monthly_isa_email.py SECTION_ORDER block not found"]
    got = len(re.findall(r'"[^"]+"', mb.group(1)))
    if got != want:
        return [f"A18/M2: monthly section count desync — Run_Context says {want}, "
                f"build_monthly_isa_email has {got}"]
    return []


def pair_monthly_t1_mode(ctx_text, t1_qualification_mode):
    """T1 semantics in prose must match T1_QUALIFICATION_MODE. Under qualification mode the
    rank-band reading ('T1 = top ~5') is retired and must not appear in operative prose — it
    silently truncates the Checkpoint-D tick-1 comparative set, which needs ALL T1 names."""
    errs = []
    live = _live_lines(ctx_text)
    band = [ln for ln in live if re.search(r"T1\s*=\s*top\s*~?\s*\d", ln)]
    if t1_qualification_mode and band:
        errs.append("A18/M3: T1_QUALIFICATION_MODE=True but operative prose still defines "
                    "T1 as a rank band ('T1 = top N') — caps T1 and truncates Checkpoint-D tick 1")
    if not t1_qualification_mode and "T1 = QUALIFICATION" in "\n".join(live):
        errs.append("A18/M3: T1_QUALIFICATION_MODE=False (rollback) but operative prose asserts "
                    "T1 = QUALIFICATION")
    return errs


# Constructs retired by a dated decision. Key = regex, value = why it must not be operative.
MONTHLY_RETIRED = {
    r"\bPath C\b":                 "Path C retired 26-Jul-2026 (energy scores on Path A)",
    r"energy scorecard":           "no energy scorecard exists after the Path C retirement",
    r"out of 36|\[X/36\]":         "the /36 energy total was retired with Path C",
    r"entry_level\s*×\s*1\.20":    "price-window tiering removed Jul-2026",
    r"Price vs Entry":             "entry distance is display-only; use fv_asymmetry vs floor",
    r"\bStep 1[123]\b":            "the monthly workflow is Steps 1-10; Steps 11-13 do not exist",
    r"fetch_metrics_local":        "script deleted 21-Jun-2026",
    r"Composio-transferred":       "metrics fetch is local-primary; no out-of-band transfer",
}


def pair_monthly_retired(ctx_text):
    """No operative line may instruct the run to use a construct that has been retired."""
    errs = []
    for ln in _live_lines(ctx_text):
        for pat, why in MONTHLY_RETIRED.items():
            if re.search(pat, ln):
                errs.append(f"A18/M4: retired construct /{pat}/ in operative monthly prose "
                            f"({why}) — line: {ln.strip()[:110]}")
                break
    return errs


# Files that ARE the decision record. No operative monthly prose may instruct their deletion.
# Kept in step with capture_archive.ARCHIVE_SET / NEVER_PURGE — if you add a permanent store
# there, add its stem here.
CAPTURE_PERMANENT = (
    "step9_pre", "step9_conviction", "run_manifest", "action_stack", "entry_level_audit",
    "shadow_ledger", "gate_variables", "transaction_ledger", "decision_ledger", "score_panel",
    # 02-Aug-2026: §7.7 intelligence store and §7.2 MOA are decision record too. The
    # intelligence store is append-only and the MOA file is the ONLY instrument pointed at
    # rejected names — losing either at cleanup would be silent and unrecoverable.
    "intelligence", "missed_opportunity",
)


def pair_monthly_capture_retention(ctx_text, exists=os.path.exists):
    """M9 (02-Aug-2026, Capture Layer Item 0 / Dashboard Spec 7.6.1).

    The defect: post-run cleanup item 8 said 'Delete ... step9_pre_[mmm_yyyy].json'. That file
    is the only record of the Step 9A inputs, so every month the framework destroyed the
    reasoning behind its own most consequential decision and acceptance criterion #19 could
    never be met. Prose was the ONLY thing holding the rule, which is why it survived so long.

    Three assertions, because reverting any one of them re-opens the hole:
      (a) no operative line instructs deletion of a permanent capture store;
      (b) the cleanup contract names capture_archive.py (the mechanical archiver), and it exists;
      (c) the archive destination is stated, so 'archive it' cannot degrade to 'leave it lying
          around until something else tidies up'.
    """
    errs = []
    for ln in _live_lines(ctx_text):
        if not re.search(r"\b(delete|remove|purge|rm -f)\b", ln, re.I):
            continue
        for stem in CAPTURE_PERMANENT:
            # 'never deleted' / 'is NOT deleted' are the correct instruction, not a violation.
            if re.search(rf"\b(delete|remove|purge)\b[^.]*\b{stem}", ln, re.I) and \
               not re.search(rf"\b(never|not|no)\b[^.]{{0,40}}\b(delete|deleted|remove|removed|purge|purged)\b", ln, re.I):
                errs.append(f"A18/M9: operative monthly prose instructs deletion of the permanent "
                            f"capture store '{stem}' — line: {ln.strip()[:110]}")
                break
    if "capture_archive.py" not in ctx_text:
        errs.append("A18/M9: post-run cleanup does not invoke capture_archive.py — the "
                    "archive/purge split is prose-only again and will silently regress")
    elif not exists(os.path.join(HERE, "capture_archive.py")):
        errs.append("A18/M9: Run_Context invokes capture_archive.py but it is not on disk")
    if "archive/decision_capture" not in ctx_text:
        errs.append("A18/M9: cleanup contract does not state the archive destination "
                    "(archive/decision_capture)")
    return errs


def pair_monthly_lean_email(ctx_text, builder_text):
    """H11 (05-Aug-2026). The email format has desynced between `build_*_email.py` and the
    Run_Context prose before — that is a recorded, named defect in this project. `--lean` is
    now load-bearing (September cannot be delivered without it), so it gets a pair on day one
    rather than after it drifts.

    Four assertions, because dropping any one re-opens a distinct hole:
      (a) the builder actually implements --lean;
      (b) the monthly contract instructs it — a capability nobody invokes is not a fix;
      (c) the never-omit guarantee exists in code, so 'lean' can never mean 'no decisions';
      (d) the full report is still written, so lean is a delivery choice and not a data loss.
    """
    errs = []
    if '"--lean"' not in builder_text:
        errs.append("A18/H11: build_monthly_isa_email.py does not implement --lean, but the "
                    "August send failed on size and September will too")
    if "--lean" not in ctx_text:
        errs.append("A18/H11: the monthly Run_Context email command does not pass --lean — "
                    "the capability exists but the contract does not invoke it")
    if "LEAN_NEVER_OMIT" not in builder_text:
        errs.append("A18/H11: no LEAN_NEVER_OMIT guarantee — lean triage could omit the "
                    "Decision Summary, i.e. the only part that is acted on")
    if "_full.html" not in ctx_text and "full-output" not in builder_text:
        errs.append("A18/H11: the complete report destination is not stated — an omitted "
                    "section would have nowhere to be read in full")
    return errs


def pair_screen_capture_coverage(dest_root=None, exists=os.path.exists):
    """§Q/M1 (05-Aug-2026). Two INDEPENDENT derivations of "a screen ran for (run_date, group)"
    must agree:

        (a) score_panel.csv          — written by score_panel_logger from the scored frame
        (b) screen_history/*.csv     — written by capture_screen_artefacts from the same frame

    They are produced by different modules from the same source, so a disagreement means one of
    them silently did not happen. Before 05-Aug-2026 the capture step was PROSE ONLY (Run_Context
    16c-2, the 17th of 19 steps, and absent from step 16b's own degradation ladder) — so the
    failure mode was not hypothetical, it was the default under session pressure. Capture is now
    a side-effect of save_full_data(); this pair is what proves that claim every month rather
    than assuming it.

    A missing capture is PERMANENT — outputs/ clears between sessions — so this is reported as an
    error, not a warning. Nothing is asserted before CAPTURE_ENFORCED_FROM: those frames are the
    closed, confirmed-irrecoverable M1 wound, and re-reporting them forever would train the
    reader to ignore this check.

    The regime side is reported SEPARATELY and as a warning-grade error string: a row that exists
    but is not `stamp_basis == "live"` is still useful for everything except regime-conditional
    analysis, and is upgradeable by re-running capture after drawdown_monitor.py.
    """
    errs = []
    root = dest_root or HERE
    try:
        sys.path.insert(0, root)
        import capture_screen_artefacts as _csa
    except Exception as e:
        return [f"A18/\u00a7Q: capture_screen_artefacts not importable ({e}) \u2014 the weekly "
                f"139/151-column frame, PIT constituents and PIT regime are being destroyed"]
    if not exists(os.path.join(root, "score_panel.csv")):
        return []                      # nothing to reconcile against yet; not a defect
    try:
        v = _csa.verify(root)
    except Exception as e:
        return [f"A18/\u00a7Q: capture verification failed to run ({e})"]
    for miss in v.get("missing", []):
        errs.append(f"A18/\u00a7Q: score_panel records a screen for {miss} but "
                    f"screen_history holds no frame for it \u2014 that week's full-width frame is "
                    f"PERMANENTLY LOST unless outputs/ still holds it this session")
    for np_ in v.get("not_pit", []):
        rd, _, basis = np_.partition(":")
        errs.append(f"A18/\u00a7Q: regime row for {rd} is stamp_basis={basis}, not 'live' \u2014 "
                    f"not admissible as point-in-time evidence; re-run "
                    f"capture_screen_artefacts.py after drawdown_monitor.py to upgrade it")
    return errs


def pair_monthly_two_regimes(ctx_text, exists=os.path.exists):
    """M10 (02-Aug-2026). The framework carries TWO four-state regime variables:

        macro_regime   Expansion|Slowdown|Contraction|Recovery  — Step 4 judgement, ECONOMY,
                       forward-looking, governs the Step 6.3 fund-band tilt.
        market_regime  RISK_ON|LATE_CYCLE|RISK_OFF|RECOVERY     — drawdown_monitor, PRICE,
                       lagging by construction, governs B1 tranches, B7 doors, B4 Category 8.

    They measure different things, so they cannot contradict each other — but both are called
    "regime", both are four-state, and both contain a state spelled Recovery/RECOVERY. That
    collision is what invites a cross-wire, and a cross-wire here would let a macro OPINION
    fire a drawdown deployment tranche.

    Asserts the operative prose keeps them distinguishable and names the resolver.
    """
    errs = []
    if "regime_resolver.py" not in ctx_text:
        errs.append("A18/M10: the monthly contract never names regime_resolver.py — the two "
                    "regime variables have no stated precedence and can be cross-wired")
    elif not exists(os.path.join(HERE, "regime_resolver.py")):
        errs.append("A18/M10: Run_Context names regime_resolver.py but it is not on disk")
    for term in ("macro_regime", "market_regime"):
        if term not in ctx_text:
            errs.append(f"A18/M10: '{term}' never appears in the monthly contract — the two "
                        f"regimes are not namespaced and 'regime' remains ambiguous")
    # The precedence rule that stops a judgement moving capital.
    if "market_regime" in ctx_text and not re.search(
            r"B1[^.\n]{0,80}market_regime|market_regime[^.\n]{0,80}B1", ctx_text):
        errs.append("A18/M10: the B1 drawdown ladder is not explicitly bound to market_regime. "
                    "A macro judgement must never be able to fire a deployment tranche.")
    return errs


def pair_monthly_return_architecture(ctx_text, prefill_text, ra_text):
    """A18 (golden-source steps 7+9 / build item #1): the Section A/B/C prose contract.

    Four things that must agree and, until 06-Aug-2026, did not:
      1. Run_Context must say Section C is READ from Step 6.08, not computed by hand. The old
         prose said "Claude computes after Section A is complete" while the artefact shipped
         `total_return: null` — a documented instruction to do arithmetic nobody ever did.
      2. `email_prefill` must not carry a "[Claude fills]" placeholder for Section C or the
         fund expected-return column.
      3. Run_Context must state that fund returns are DATED and name both sources.
      4. The operative basis constant must be one the module actually recognises.
    """
    errs = []
    if "Step 6.08" not in ctx_text or "read it; do not compute it" not in ctx_text.lower():
        errs.append("A18/6.08: Run_Context must state that Section C is READ from Step 6.08, "
                    "not computed by hand — the old prose told Claude to compute a number the "
                    "pre-run had already been shipping as null")
    # ⚑ Test the STRING LITERALS, not the file text. The first version grepped and fired on
    # `email_prefill`'s own comment recording what the placeholder USED to be — a check that
    # cannot tell a mention from a use is a check that gets deleted rather than fixed.
    try:
        import ast as _ast
        _lits = [n.value for n in _ast.walk(_ast.parse(prefill_text))
                 if isinstance(n, _ast.Constant) and isinstance(n.value, str)]
    except SyntaxError:
        _lits = []
        errs.append("A18/6.08: email_prefill.py does not parse")
    for bad in ("[Claude fills after Section A complete",):
        if any(bad in l for l in _lits):
            errs.append(f"A18/6.08: email_prefill still EMITS the placeholder {bad!r} — "
                        f"Section C is mechanical now")
    if "_perf_cell(" not in prefill_text:
        errs.append("A18/step7: email_prefill must render fund returns through _perf_cell "
                    "(dated, two named sources) — a bare figure cannot be reconciled")
    if "carries its OWN `as_of`" not in ctx_text:
        errs.append("A18/step7: Run_Context §8 compliance must require a dated return per fund")
    m = re.search(r'ER_BASIS_OPERATIVE\s*=\s*"([a-z_]+)"', ra_text)
    bases = re.search(r'ER_BASES\s*=\s*\(([^)]*)\)', ra_text)
    if not m or not bases:
        errs.append("A18/6.08: return_architecture must declare ER_BASIS_OPERATIVE and ER_BASES")
    elif m.group(1) not in bases.group(1):
        errs.append(f"A18/6.08: operative basis {m.group(1)!r} is not one of ER_BASES — a basis "
                    f"the module does not recognise would silently produce no sections")
    return errs


def pair_monthly_prerun_stages(ctx_text, prerun_text):
    """Every stage the orchestrator PRINTS must have a row in the Run_Context stage table.
    The 31-Jul-26 defect: Steps 1.5, 6.5, 9b.5, 9c and 9d all executed undocumented — 9d alone
    runs ten sub-checks that can raise blocking-visible errors the review must act on."""
    stages = set()
    for raw in re.findall(r'print\(f?"\\n\[([0-9][0-9a-z.]*)(?:/9)?\]', prerun_text):
        stages.add(raw)
    if not stages:
        return ["A18/M5: could not parse any stage headers from monthly_isa_prerun.py"]
    table = ctx_text.split("**Pre-run scripts (Investment Analysis folder):**", 1)
    if len(table) < 2:
        return ["A18/M5: Run_Context pre-run script table not found"]
    # ⚑ FIXED 05-Aug-2026. This read a fixed 6,000-CHARACTER window after the marker. Adding one
    # long stage row (1b-2, the cash-statement stage) pushed stages 9c, 9d and 8c beyond the
    # window, and the pair reported three DOCUMENTED stages as undocumented. A verifier whose
    # coverage silently shrinks as the document grows is worse than no verifier, because it
    # fails in the direction of a false alarm today and a false PASS tomorrow — a row moved just
    # inside the window by an unrelated edit would be "checked" without being read.
    # It now walks the actual markdown table: every contiguous run of table lines after the
    # marker, stopping only at the next heading.
    rest = table[1]
    rows, started = [], False
    for ln in rest.splitlines():
        st = ln.strip()
        if st.startswith("|"):
            rows.append(st); started = True
        elif st.startswith("#") and started:
            break                       # next section — the table is over
    documented = set(re.findall(r"^\|\s*\*{0,2}([0-9][0-9a-z.]*)\*{0,2}\s*\|",
                                "\n".join(rows), re.M))
    missing = sorted(stages - documented, key=lambda s: (len(s), s))
    if missing:
        return [f"A18/M5: pre-run stages executed but NOT in the Run_Context stage table: "
                f"{missing} (documented: {sorted(documented)})"]
    return []


def pair_monthly_prerun_reads(ctx_text):
    """'Pre-Run Files (N reads ...)' must equal the number of entries actually listed."""
    m = re.search(r"## Pre-Run Files \((\d+) reads", ctx_text)
    if not m:
        return ["A18/M6: '## Pre-Run Files (N reads' heading missing"]
    want = int(m.group(1))
    seg = ctx_text.split(m.group(0), 1)[1].split("**Conditional reads", 1)[0]
    got = len(re.findall(r"^(\d+)\.\s+\S", seg, re.M))
    if got != want:
        return [f"A18/M6: pre-run read count desync — heading says {want}, list has {got}"]
    return []


def pair_referenced_scripts_exist(texts, exists=os.path.exists):
    """Every *.py referenced by an execution contract must be present on disk.
    THE pair that would have caught fetch_metrics_local.py: the pre-run recipe invoked it every
    month after it was deleted, and the orchestrator mis-reported the resulting failure as
    benign. {label: text} in, one error per missing script."""
    errs = []
    for label, txt in texts.items():
        # Operative lines only — a retirement note naming the script it retired is not a
        # live reference (caught immediately on first run: the struck-through energy_screener.py
        # line in the monthly Run_Context).
        live = "\n".join(_live_lines(txt))
        for name in sorted(set(re.findall(r"\b([a-z_][a-z0-9_]*\.py)\b", live))):
            # Test scripts legitimately live in tests_jul2026/, and the regex deliberately
            # captures the bare filename so a path prefix in the prose does not defeat the
            # check. Searching both roots keeps the invariant honest without weakening it:
            # a script that exists NOWHERE is still an error. (Extended 05-Aug-2026 when the
            # pair correctly flagged tests_jul2026/test_email_lean.py on its first run.)
            if not any(exists(os.path.join(HERE, d, name)) for d in ("", "tests_jul2026")):
                errs.append(f"A18/M7: {label} references {name}, which does not exist in the "
                            f"Investment Analysis folder or tests_jul2026/")
    return errs


def _assigned_and_imported(tree):
    """Every name bound anywhere in a module (any scope) plus every imported name."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.ExceptHandler,)) and node.name:
            bound.add(node.name)
    return bound


def pair_undefined_constants(py_texts):
    """ALL_CAPS names READ but never bound or imported anywhere in the file.

    This is the FETCH_WORKERS class (31-Jul-2026): fetch_watchlist_metrics.py referenced an
    undefined FETCH_WORKERS at its ThreadPoolExecutor, so every local metrics fetch raised
    NameError before touching Yahoo — for weeks — while the caller logged it as an expected
    architectural condition. py_compile cannot see it; only a name-resolution check can.
    Scoped to ALL_CAPS (module-constant convention) to keep precision high: dotted access
    (sc.FOO) is an Attribute, not a Name, so it is correctly ignored.
    """
    errs = []
    for fn, txt in sorted(py_texts.items()):
        try:
            tree = ast.parse(txt, filename=fn)
        except SyntaxError as e:
            errs.append(f"A18/M8: {fn} does not parse ({e})")
            continue
        bound = _assigned_and_imported(tree)
        seen = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", node.id)
                    and node.id not in bound
                    and node.id not in dir(builtins)
                    and node.id not in seen):
                seen.add(node.id)
                errs.append(f"A18/M8: {fn} reads {node.id} (line {node.lineno}) but it is never "
                            f"assigned or imported in that file — NameError at runtime")
    return errs


# ── driver ───────────────────────────────────────────────────────────────────────────────

def pair_orchestrator_parity():
    """07-Aug-2026. TWO INDEPENDENT DERIVATIONS OF "WHAT THE WEEKLY SCREEN DOES".

    The reference orchestrator (`screener_core.run_scheduled`) and the live one
    (`screener_local.main`) must agree on which capabilities run. They diverged for nine days
    without anything noticing, and the cost was that PRICE_MOM_SCORING="percentile" — set on
    29-Jul-2026 — had never once been in force on a weekly screen.

    Lives in the ROUTINE battery deliberately: a suite that is not in the routine battery is not
    a test (register D3).
    """
    out = []
    try:
        sys.path.insert(0, HERE)
        import orchestrator_parity as _op
        res = _op.check_all()
        cap, cfgr = res["capability_parity"], res["config_reachability"]
        if cap.get("undeclared_missing"):
            out.append("ORCHESTRATOR PARITY: capabilities in screener_core.run_scheduled that the "
                       "LIVE path (screener_local) never calls, and that are not declared in "
                       f"orchestrator_parity.EXEMPT: {cap['undeclared_missing']}. Each is a "
                       "capability the framework believes it has and does not.")
        if cap.get("exempt_with_blank_reason"):
            out.append(f"ORCHESTRATOR PARITY: exemptions with no stated reason: "
                       f"{cap['exempt_with_blank_reason']} — an exemption is a declaration, not a "
                       "suppression.")
        if cap.get("stale_exemptions"):
            out.append(f"ORCHESTRATOR PARITY (advisory): exemptions for capabilities no longer "
                       f"required: {cap['stale_exemptions']} — remove them before the list rots.")
        if cap.get("reason") and not cap.get("undeclared_missing"):
            out.append(f"ORCHESTRATOR PARITY: {cap['reason']}")
        for st in cfgr.get("stranded", []):
            out.append(f"CONFIG REACHABILITY: {st['constant']} is declared in scoring_config and "
                       f"read ONLY by {st['read_only_by']}, which the live entry point cannot "
                       "reach. It is declared operative and is not.")
    except Exception as e:                                     # noqa: BLE001
        # Loud, never silent: an unrunnable parity check is indistinguishable from a passing one
        # unless it says so.
        out.append(f"ORCHESTRATOR PARITY could not run ({type(e).__name__}: {e}) — "
                   "treat as UNKNOWN, not as PASS.")
    return out


def check_all():
    errs = []
    run_ctx = _read("Run_Context_ISA_Growth_Stock_Analysis.md")
    bem = _read("build_email.py")
    errs += pair_orchestrator_parity()
    errs += pair_stale_partb(run_ctx)
    errs += pair_summary_floor_prose(run_ctx)
    errs += pair_top10_columns(run_ctx, bem)
    errs += pair_email_sections(run_ctx, bem)
    try:
        errs += pair_monthly_return_architecture(
            _read("Run_Context_Monthly_ISA_Review.md"), _read("email_prefill.py"),
            _read("return_architecture.py"))
    except Exception as _e:                                    # noqa: BLE001
        errs.append(f"A18/6.08 pair could not run: {type(_e).__name__}: {_e}")
    errs += pair_retired_constants({fn: _read(fn) for fn in
                                    ("build_excel.py", "build_email.py", "update_watchlist.py",
                                     "screener_core.py", "rerank_watchlist.py", "scoring_config.py")})
    try:
        with open(os.path.join(HERE, "target_state.json"), encoding="utf-8") as f:
            state = json.load(f)
        sys.path.insert(0, HERE)
        import scoring_config as cfg
        errs += pair_anchor(state, getattr(cfg, "REQUIRED_RETURN_MID", None))
        errs += pair_max_scale(cfg)
    except Exception as e:
        errs.append(f"A18/A19: anchor/config check failed to run ({e})")
        cfg = None

    # ---- MONTHLY REVIEW pairs (31-Jul-2026) --------------------------------------------
    try:
        mctx   = _read(MONTHLY_CTX)
        mbuild = _read("build_monthly_isa_email.py")
        prerun = _read("monthly_isa_prerun.py")
        errs += pair_monthly_action_categories(mctx)
        errs += pair_monthly_email_sections(mctx, mbuild)
        errs += pair_monthly_retired(mctx)
        errs += pair_monthly_prerun_stages(mctx, prerun)
        errs += pair_monthly_prerun_reads(mctx)
        errs += pair_monthly_capture_retention(mctx)
        errs += pair_monthly_two_regimes(mctx)
        errs += pair_monthly_lean_email(mctx, mbuild)
        errs += pair_screen_capture_coverage()
        if cfg is not None:
            errs += pair_monthly_t1_mode(mctx, bool(getattr(cfg, "T1_QUALIFICATION_MODE", False)))
        # Contracts that name executables: a missing script is a silent monthly failure.
        errs += pair_referenced_scripts_exist({MONTHLY_CTX: mctx})
        # Name-resolution across the scripts the two runs depend on.
        errs += pair_undefined_constants({fn: _read(fn) for fn in TRACKED_SCRIPTS
                                          if os.path.exists(os.path.join(HERE, fn))})
    except Exception as e:
        errs.append(f"A18/monthly: monthly pair set failed to run ({e})")
    return errs


def _selftest():
    # seeded-good fixtures pass
    good_ctx = ("references SUMMARY_PART_B_FLOOR and SUMMARY_SOURCE_FLOOR and SUMMARY_MAX_COUNT. "
                "Email — 7 Mandatory Sections ... exactly these 13 ...")
    good_bem = ('header_cols = [\n"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"\n]\n'
                + "".join(f"# Section {i} — x\n" for i in range(1, 8)))
    assert not pair_stale_partb(good_ctx)
    assert not pair_summary_floor_prose(good_ctx)
    assert not pair_top10_columns(good_ctx, good_bem)
    assert not pair_email_sections(good_ctx, good_bem)
    # mutations fail
    assert pair_stale_partb(good_ctx + " Part B >= 14 ")
    assert pair_summary_floor_prose(good_ctx + " select the top 30 ")
    assert pair_top10_columns(good_ctx.replace("13", "12"), good_bem)
    assert pair_email_sections(good_ctx.replace("7 Mandatory", "8 Mandatory"), good_bem)
    assert pair_retired_constants({"x.py": "n = SUMMARY_TARGET_COUNT"})
    ok_state = {"derived_at": "2026-07-12", "schedule_updated_at": "2026-07-12",
                "guardrail_state": "OK", "required_return_operative_pct": 13.9}
    assert not pair_anchor(ok_state, 13.9)
    assert pair_anchor({**ok_state, "schedule_updated_at": "2026-08-01"})
    assert pair_anchor({**ok_state, "guardrail_state": "FALLBACK"})
    assert pair_anchor(ok_state, 14.0)

    # ---- build item #1: Section A/B/C prose contract ---------------------------------------
    _gctx = ("Step 6.08 ... **Read it; do not compute it.** ... carries its OWN `as_of` ...")
    _gpre = "x = _perf_cell(a, b, c, d)\n# mention only: [Claude fills after Section A complete]"
    _gra = 'ER_BASIS_OPERATIVE = "declared_prior"\nER_BASES = ("declared_prior", "realised")'
    assert not pair_monthly_return_architecture(_gctx, _gpre, _gra)
    # each mutation is the real-world defect it was written for
    assert pair_monthly_return_architecture(_gctx.replace("Step 6.08", "Step 6"), _gpre, _gra)
    # a MENTION in a comment must NOT fire; an emitted LITERAL must
    assert not pair_monthly_return_architecture(_gctx, _gpre, _gra)
    assert pair_monthly_return_architecture(
        _gctx, _gpre + "\ny = '[Claude fills after Section A complete]'", _gra)
    assert pair_monthly_return_architecture(_gctx, "no perf cell here", _gra)
    assert pair_monthly_return_architecture(_gctx.replace("carries its OWN `as_of`", "x"), _gpre, _gra)
    assert pair_monthly_return_architecture(
        _gctx, _gpre, _gra.replace('"declared_prior"', '"typo_basis"', 1))

    # ---- MONTHLY pairs (31-Jul-2026): each must be GREEN on a good fixture and FIRE on the
    # exact mutation that caused the real-world defect it was written for. --------------------
    good_m = (
        "## Pre-Run Script Infrastructure\n"
        "**Pre-run scripts (Investment Analysis folder):**\n"
        "| Step | Script |\n"
        "| 1 | `extract_portfolio.py` |\n"
        "| **1b** | x |\n| **1.5** | x |\n| 2 | x |\n| 3 | x |\n| **4** | x |\n| **5** | x |\n"
        "| 6 | x |\n| **6.5** | x |\n| 7 | x |\n| **7.25** | x |\n| **7.5** | x |\n"
        "| **8** | x |\n| 9 | x |\n| **9b.5** | x |\n| **9c** | x |\n| **9d** | x |\n"
        "\n## Pre-Run Files (2 reads — nothing else)\n1. one\n2. two\n"
        "**Conditional reads (only when the step explicitly requires them):**\n"
        "## Step 8 — Build and Rank the Monthly Action Set\n"
        "Explicitly assess and rank all 3 action categories:\n"
        "1. a\n2. b\n3. c\n"
        "**Category 2 — top-up criteria:** x\n"
        "After completing the main 3-category ranking above, x\n"
        "Step 8 produces a ranked list of all 3 action categories, x\n"
        "action set decision (all 3 categories ranked)\n"
        "## Email Structure — 2 Mandatory Sections (exact order)\n"
        "T1 = QUALIFICATION, not a rank band.\n"
    )
    good_build = 'SECTION_ORDER = [\n    "s1_a",\n    "s2_b",\n]\n'
    good_prerun = ('print(f"\\n[1/9] a")\nprint("\\n[1b] b")\nprint("\\n[1.5] c")\n'
                   'print(f"\\n[2/9] d")\nprint(f"\\n[3/9] e")\nprint(f"\\n[4/9] f")\n'
                   'print(f"\\n[5/9] g")\nprint(f"\\n[6/9] h")\nprint(f"\\n[6.5] i")\n'
                   'print(f"\\n[7/9] j")\nprint(f"\\n[7.25] k")\nprint(f"\\n[7.5] l")\n'
                   'print(f"\\n[8/9] m")\nprint(f"\\n[9/9] n")\nprint("\\n[9b.5] o")\n'
                   'print(f"\\n[9c] p")\nprint("\\n[9d] q")\n')

    assert not pair_monthly_action_categories(good_m), pair_monthly_action_categories(good_m)
    assert not pair_monthly_email_sections(good_m, good_build)
    assert not pair_monthly_retired(good_m)
    assert not pair_monthly_prerun_stages(good_m, good_prerun), \
        pair_monthly_prerun_stages(good_m, good_prerun)
    assert not pair_monthly_prerun_reads(good_m)
    assert not pair_monthly_t1_mode(good_m, True)

    # M10 — two regimes. Good fixture names both, the resolver, and binds B1 to market_regime.
    good_reg = (good_m + "\nRegimes: macro_regime (Step 4) vs market_regime (mechanical). "
                "Resolve via regime_resolver.py. The B1 drawdown ladder reads market_regime "
                "ONLY.\n")
    assert not pair_monthly_two_regimes(good_reg, exists=lambda p: True), \
        pair_monthly_two_regimes(good_reg, exists=lambda p: True)
    assert pair_monthly_two_regimes(good_reg.replace("macro_regime", "regime"),
                                    exists=lambda p: True)
    assert pair_monthly_two_regimes(good_reg.replace("regime_resolver.py", "x.py"),
                                    exists=lambda p: True)
    assert pair_monthly_two_regimes(good_reg.replace(
        "The B1 drawdown ladder reads market_regime ONLY.", ""), exists=lambda p: True)

    # M9 — capture retention. Good fixture: archives via capture_archive.py, names the target.
    good_cap = (good_m + "\n8. Post-run cleanup: run `python3 capture_archive.py --month "
                "[mmm_yyyy] --purge`. step9_pre_[mmm_yyyy].json is NOT deleted; it is archived "
                "into archive/decision_capture/. Delete watchlist_metrics_[mmm_yyyy].json only.\n")
    assert not pair_monthly_capture_retention(good_cap, exists=lambda p: True), \
        pair_monthly_capture_retention(good_cap, exists=lambda p: True)
    # THE real-world mutation: cleanup reverts to deleting the Step 9A inputs.
    bad_cap = good_cap.replace("step9_pre_[mmm_yyyy].json is NOT deleted; it is archived "
                               "into archive/decision_capture/.",
                               "Delete step9_pre_[mmm_yyyy].json after the email sends.")
    assert pair_monthly_capture_retention(bad_cap, exists=lambda p: True)
    # ...and dropping the mechanical archiver, leaving prose alone to hold the rule.
    assert pair_monthly_capture_retention(good_cap.replace("capture_archive.py", "tidy_up.py"),
                                          exists=lambda p: True)
    # ...and naming it while it is absent from disk.
    assert pair_monthly_capture_retention(good_cap, exists=lambda p: False)

    # M1 — the live defect: "rank all 7" above a list of 8 (Category 8 silently dropped).
    assert pair_monthly_action_categories(good_m.replace("rank all 3", "rank all 7"))
    # ...and every restatement of the count must agree with the header.
    assert pair_monthly_action_categories(good_m.replace("main 3-category", "main 7-category"))
    assert pair_monthly_action_categories(good_m.replace("all 3 categories ranked",
                                                         "all 7 categories ranked"))
    # M2 — section count vs the builder's SECTION_ORDER.
    assert pair_monthly_email_sections(good_m.replace("— 2 Mandatory", "— 11 Mandatory"), good_build)
    # M3 — rank-band prose surviving under qualification mode (truncates Checkpoint-D tick 1).
    assert pair_monthly_t1_mode(good_m + "\nTiers are rank bands (T1 = top ~5), not price bands.",
                                True)
    assert not pair_monthly_t1_mode(good_m + "\nCORRECTED: was 'T1 = top ~5'.", True)  # marker skips
    assert pair_monthly_t1_mode(good_m, False)   # qualification prose under the rollback flag
    # M4 — every retired construct, one mutation each.
    for frag in ("Use the Path C energy scorecard", "state it out of 36",
                 "price vs entry_level × 1.20 boundary", "column: Price vs Entry",
                 "write the trades log at Step 12", "run fetch_metrics_local.py",
                 "using Composio-transferred metrics"):
        assert pair_monthly_retired(good_m + "\n" + frag), f"M4 missed: {frag}"
    # ...but a line that marks itself historical is allowed to quote them.
    assert not pair_monthly_retired(good_m + "\nPath C RETIRED 26-Jul-2026 — do not use.")
    # M5 — an executed stage with no row in the table (the 1.5/6.5/9b.5/9c/9d defect).
    assert pair_monthly_prerun_stages(good_m.replace("| **9d** | x |\n", ""), good_prerun)
    # M6 — heading count vs the list it heads.
    assert pair_monthly_prerun_reads(good_m.replace("(2 reads", "(8 reads"))
    # M7 — THE fetch_metrics_local class: a contract naming a script that is not on disk.
    assert pair_referenced_scripts_exist({"ctx": "run fetch_metrics_local.py now"},
                                         exists=lambda p: False)
    assert not pair_referenced_scripts_exist({"ctx": "run extract_portfolio.py now"},
                                             exists=lambda p: True)
    # M8 — THE FETCH_WORKERS class: an ALL_CAPS constant read but never bound.
    assert pair_undefined_constants(
        {"f.py": "def go(x):\n    return ThreadPoolExecutor(max_workers=FETCH_WORKERS)\n"})
    assert not pair_undefined_constants(
        {"f.py": "FETCH_WORKERS = 12\ndef go(x):\n    return f(max_workers=FETCH_WORKERS)\n"})
    assert not pair_undefined_constants(          # imported constants are bound
        {"f.py": "from scoring_config import MIN_HOLD_DAYS\ndef go():\n    return MIN_HOLD_DAYS\n"})
    assert not pair_undefined_constants(          # dotted access is an Attribute, not a Name
        {"f.py": "import scoring_config as sc\ndef go():\n    return sc.MIN_HOLD_DAYS\n"})
    assert not pair_undefined_constants(          # builtins are not undefined
        {"f.py": "def go():\n    raise NotImplementedError\n"})

    print("consistency_check SELF-TEST OK (growth pairs + 10 monthly pairs)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        sys.exit(0)
    errors = check_all()
    for e in errors:
        print(f"[FAIL] {e}")
    print("ALL PAIRS GREEN" if not errors else f"{len(errors)} MISMATCH(ES)")
    sys.exit(1 if errors else 0)
