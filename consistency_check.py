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
import argparse, ast, builtins, hashlib, json, os, re, sys

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



# ── ISA-0321 (Raj, 12-Aug-2026): THE END-OF-BUILD REGISTER GATE ─────────────────────────────
# Raj: "I need this project to remember without me asking to update the register without fail at
# the end of each build every single time."
#
# ⚑ WHY THIS IS A REFUSAL AND NOT A RULE. R7.7 and R7.3 already say the register must be updated.
# R7.3 is enforced in code (close() raises without a liveness reference) but NOTHING detected a
# build that produced no register activity at all - so intake and end-of-build update sat at the
# weakest point of R14.2's order, 'documented rule' and 'intention', for the one step that makes
# every other step auditable. R14.1 says that is a defect, and it says so INCLUDING when the person
# remembering is me. Evidence from the session that prompted this: the register WAS updated, but
# only because I chose to, and in the same session I twice omitted backfill_studies() and wrote one
# item's proximate cause before verifying it.
#
# THE TEST: if a tracked SOURCE file changed today, at least one register item must have been
# created, updated or resolved today. Because check_all() runs in the monthly pre-run and inside
# tests_jul2026/run_tests.py, a build that skipped the register cannot reach a green battery.
#
# ⚑ GENERATED ARTEFACTS ARE EXCLUDED. Regenerating the views is not a build, and a gate that fired
# on its own outputs would be turned off within a week - the way every waived rule dies.
REGISTER_GATE_EXCLUDE = (
    "ISA_OPEN_ITEMS_REGISTER.md", "ISA_Decision_Register.md", "ISA_Build_Backlog_Ranked.md",
    "ISA_Item_Register.csv", "ISA_Item_Register.xlsx",
)


def _register_gate_sources(root=None):
    """Tracked SOURCE files whose modification implies a build. Generated views excluded."""
    import glob as _glob
    root = root or HERE
    out = []
    for pat in ("*.py", "Run_Context_*.md", "ISA_Engineering_Rules.md", "*.json"):
        for fp in _glob.glob(os.path.join(root, pat)):
            b = os.path.basename(fp)
            if b in REGISTER_GATE_EXCLUDE or b.startswith("~$"):
                continue
            # state stores are WRITTEN BY the register and by every run; they are outputs, not builds
            if b.startswith(("email_", "score_panel", "watchlist_", "vci_prescore",
                             "calibration_", "analytics_data", "portfolio_data", "step9_",
                             "fund_action_stack_", "return_architecture_", "beta_alpha_study_",
                             "holding_period_returns_", "missed_opportunity_", "entry_level_",
                             "shadow_", "moa_", "eps_", "concentration_", "lookthrough_",
                             "trust_discount_", "h7_", "scores_", "run_context_", "transactions_",
                             "xray_data", "anchor_", "target_state", "er_anchor", "regime_",
                             "drawdown_", "position_", "screen_", "delisting_", "learning_",
                             "constituents", "supplementary_", "manifest_", "decision_ledger",
                             "transaction_ledger", "fixture_check", "sleeve_", "source_performance",
                             "theme_", "preferred_", "fund_returns_cache", "fund_holdings",
                             "return_inputs", "action_stack_", "m1_source_score")):
                continue
            out.append(fp)
    return sorted(out)


def pair_register_updated_after_build(root=None, today=None, items=None):
    """ISA-0321. A build that did not touch the register FAILS the battery."""
    import datetime as _dt
    root = root or HERE
    today = today or _dt.date.today().isoformat()
    changed = []
    for fp in _register_gate_sources(root):
        try:
            m = _dt.date.fromtimestamp(os.path.getmtime(fp)).isoformat()
        except OSError:
            continue
        if m == today:
            changed.append(os.path.basename(fp))
    if not changed:
        return []                      # nothing was built today; the gate has nothing to say
    if items is None:
        try:
            sys.path.insert(0, HERE)
            import isa_register as _R
            items = _R.read_all()
        except Exception as e:                                   # noqa: BLE001
            # R4.3: a control fed a null BLOCKS. It does NOT pass because it could not look.
            return [f"A18/ISA-0321: {len(changed)} source file(s) changed today and the register "
                    f"could not be read to check it was updated ({type(e).__name__}: {e}) — this is "
                    f"a FAIL, not a skip"]
    touched = [i["id"] for i in items
               if today in (str(i.get("created_on")), str(i.get("updated_on")),
                            str(i.get("resolved_on")))]
    if touched:
        return []
    return [("A18/ISA-0321: {n} source file(s) were modified today ({eg}) and NO register item was "
             "created, updated or resolved today. R7.7: no work without an item — a same-session fix "
             "is recorded OPEN→CLOSED_FIXED with its verification, never left unrecorded because it "
             "was quick. Create or update the item, then run: python3 -c \"import isa_register as R; "
             "R.backfill_studies()\" && python3 isa_register_render.py --write && python3 "
             "isa_register_export.py --write").format(
                n=len(changed), eg=", ".join(sorted(changed)[:6])
                + (" …" if len(changed) > 6 else ""))]


def pair_anchor_cadence(state: dict, drr=None):
    """A19b (D-2/D-3/D-4, 12-Aug-2026): the two-speed cadence, asserted where it can be read.

    ⚑ WHY THIS IS A PAIR AND NOT A UNIT TEST. `target_state.json` is now written by ONE module and
    read by nine, and the field that gates capital (`required_return_operative_pct`) no longer
    changes every time the deriver runs. The failure this catches is a reader that has not been
    updated for two speeds and therefore treats a HELD operative value as a fresh spot solve — a
    plausible number that is silently six months old. The schema bump makes that fail loudly, and
    this pair is what checks the bump is actually in force (R4.7).
    """
    errs = []
    if drr is None:
        try:
            import derive_required_return as drr                     # noqa: PLW0127
        except Exception as e:                                       # noqa: BLE001
            return [f"A18/A19b: derive_required_return unimportable ({type(e).__name__}: {e}) — "
                    f"the anchor cadence cannot be checked, so this is a FAIL, not a skip (R4.3)"]
    want = int(getattr(drr, "TARGET_STATE_SCHEMA_VERSION", 2))
    got = int(state.get("schema_version", 1))
    if got != want:
        errs.append(f"A18/A19b: target_state.schema_version {got} != {want} — the file predates "
                    f"the D-2 two-speed cadence; rerun derive_required_return.py")
        return errs                                    # the rest of the contract cannot apply yet
    for k in ("required_return_reported_floor_pct", "required_return_reported_operative_pct",
              "operative_effective_from", "operative_derived_at", "operative_next_window",
              "anchor_cadence", "flow_trigger", "valuation_basis"):
        if state.get(k) is None:
            errs.append(f"A18/A19b: `{k}` absent from target_state.json — the two-speed contract "
                        f"is incomplete and a consumer cannot tell reported from operative")
    cad = state.get("anchor_cadence") or {}
    if cad and not cad.get("authority"):
        errs.append("A18/A19b: the stored cadence decision names no AUTHORITY, so a held gate "
                    "cannot say why it is holding")
    # the operative value must still sit inside D1c whichever branch last set it
    try:
        op = float(state["required_return_operative_pct"])
        if not (float(getattr(drr, "OPERATIVE_FLOOR_PCT", 10.0)) <= op
                <= float(getattr(drr, "OPERATIVE_CAP_PCT", 18.0))):
            errs.append(f"A18/A19b: the HELD operative anchor {op} is outside the D1c band — a "
                        f"held value is still a gating value")
    except (KeyError, TypeError, ValueError) as e:
        errs.append(f"A18/A19b: operative anchor unreadable ({e})")
    # ⚑ BREAK-GLASS MUST NOT BE ARMED AND UNAPPLIED. This is the one drift that is not benign.
    try:
        rep = float(state["required_return_reported_operative_pct"])
        op = float(state["required_return_operative_pct"])
        bg = float(getattr(drr, "ANCHOR_BREAK_GLASS_PP", 2.0))
        if abs(rep - op) >= bg:
            errs.append(f"A18/A19b: reported {rep} vs operative {op} = {abs(rep-op):.2f}pp, at or "
                        f"beyond the {bg}pp break-glass, and the operative value has NOT been "
                        f"updated — rerun derive_required_return.py")
    except (KeyError, TypeError, ValueError):
        pass                       # the absence is already reported above; no second complaint
    # ⚑ THE D-4 TRIGGER MUST NOT BE UNEVALUABLE. 'No flow' from a missing ledger is not a finding.
    trig = state.get("flow_trigger") or {}
    if trig.get("status") == "UNKNOWN" or trig.get("blocks") is True:
        errs.append(f"A18/A19b: the D-4 flow trigger could not be evaluated "
                    f"({trig.get('reason')}) — absence of evidence is not evidence of no flow")
    # ⚑ A DEGRADED D-3 BASIS IS REPORTED, NOT AN ERROR. Two months of history is the expected
    # state until 31-Aug-2026 lands; what would be a defect is a degradation that says nothing.
    vb = state.get("valuation_basis") or {}
    if vb.get("degraded") and not vb.get("degraded_reason"):
        errs.append("A18/A19b: the valuation basis is degraded and gives NO reason — a fallback "
                    "that cannot say why it fell back is the FC-A defect")
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


# ISA-0228 (12-Aug-2026). A pattern guard cannot tell USE from MENTION, so the sentence that
# forbids a retired construct reads as an instruction to use it - and the correct prose becomes
# unwritable. These are the NEGATING forms: a line that says the construct does not exist, or
# must never be used, is a prohibition, not an instruction. Deliberately narrow, and guarded by
# a negative control asserting a genuine instruction still FAILS (R5.5).
_NEGATION_RE = re.compile(
    r"\b(?:there\s+(?:is|are)\s+no|do(?:es)?\s+not\s+exist|no\s+longer\s+exists?"
    r"|never\s+use|must\s+not\s+use|do\s+not\s+use|is\s+retired|was\s+retired"
    r"|which\s+does\s+not\s+exist)\b", re.I)


def pair_monthly_retired(ctx_text):
    """No operative line may instruct the run to use a construct that has been retired."""
    errs = []
    for ln in _live_lines(ctx_text):
        if _NEGATION_RE.search(ln):
            continue                      # a prohibition, not an instruction (ISA-0228)
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
    # ISA-0026 (12-Aug-2026): the item register. Every other store here can be rebuilt from a
    # re-run; this one cannot.
    "isa_items", "isa_item.schema", "isa_id_map", "isa_id_highwater",
    "isa_migration_declarations",
)


REGISTER_STORE_FILES = ("isa_items.jsonl", "isa_item.schema.json",
                        "isa_id_map.json", "isa_id_highwater.json")



STANDARD_FILE = "ISA_Engineering_Rules.md"
_RUN_CTX_RE = re.compile(r"Run_Context_[A-Za-z_]+\.md")


def pair_standard_referenced(surfaces=None, exists=os.path.exists):
    """ISA-0027 / O-10. Every run surface must REACH the engineering standard.

    The defect this closes: ISA_Engineering_Rules.md stated it was 'Referenced by
    Run_Context_Monthly_ISA_Review.md, Run_Context_VCI_Task.md,
    Run_Context_ISA_Growth_Stock_Analysis.md, every BuildSpec, and every register item', and a
    grep of all 17 run surfaces on 12-Aug-2026 returned ZERO. A document asserting its own
    wiring is believed because it is plausible and nothing checks it (FC-E).

    Reaching it may be DIRECT or via a named Run_Context that references it - the chain is the
    design (O-10: referenced, never restated), so the check follows the chain rather than
    demanding the string everywhere and inviting it to be pasted into 17 places.
    """
    errs = []
    if surfaces is None:
        try:
            import framework_atlas as _fa
            surfaces = _fa.run_surface_texts()
        except Exception as e:                                    # noqa: BLE001
            return [f"ISA-0027: run-surface enumeration unavailable ({e}) - standard coverage "
                    f"reported as UNKNOWN, never as PASS"]
    if not exists(os.path.join(HERE, STANDARD_FILE)):
        return [f"ISA-0027: {STANDARD_FILE} is not on disk - every surface below references a "
                f"file that does not exist"]
    direct = {lbl for lbl, txt in surfaces.items() if STANDARD_FILE in txt}
    for label, text in sorted(surfaces.items()):
        if label in direct:
            continue
        vias = [v for v in set(_RUN_CTX_RE.findall(text)) if v[:-3] in surfaces or v in surfaces]
        if any(STANDARD_FILE in surfaces.get(v[:-3], surfaces.get(v, "")) for v in vias):
            continue
        errs.append(f"ISA-0027: run surface '{label}' neither references {STANDARD_FILE} nor "
                    f"delegates to a Run_Context that does - the binding standard is not in "
                    f"scope for that run")
    return errs


def pair_run_surface_basis(atlas=None):
    """ISA-0211. Say which copy of each SKILL the guards actually read, and flag drift.

    Skills_to_Edit/*/SKILL.md is a MIRROR; the scheduler executes Claude/Scheduled/*/SKILL.md.
    A guard pointed at the mirror raised four false alarms about a 'Step 12' the live prompt had
    already corrected - and the same construction yields a false PASS the day the mirror is right
    and the executed prompt is wrong.

    ⚑ An unreachable live directory is NOT an error here: from a build sandbox the Windows path
    genuinely does not exist (R2.9). It is reported as a stated limitation, because
    'no drift found' and 'could not look' must never render the same (R2.10).
    """
    if atlas is None:
        try:
            import framework_atlas as atlas
        except Exception as e:                                    # noqa: BLE001
            return [f"ISA-0211: framework_atlas unavailable ({e}) - run-surface basis unknown"]
    drift = atlas.run_surface_mirror_drift()
    return [f"ISA-0211: {d}" for d in drift]


def run_surface_basis_note(atlas=None) -> str:
    """One line for the battery output: what the run-surface controls were computed on."""
    if atlas is None:
        import framework_atlas as atlas
    live = atlas.scheduled_skills_dir()
    wb = atlas.run_surface_texts(with_basis=True)
    n_exec = sum(1 for _, b in wb.values() if b == "executed")
    if live is None:
        return (f"run-surface basis: {n_exec}/{len(wb)} read from the EXECUTED contract; the "
                f"scheduled directory is unreachable from here, so the SKILL surfaces were read "
                f"from the ISA-folder MIRROR and mirror drift COULD NOT BE CHECKED (ISA-0211)")
    return f"run-surface basis: {n_exec}/{len(wb)} EXECUTED, mirror drift checked"

def pair_retrospectives_ingested(intake=None, screens=None):
    """ISA-0229 / ISA-0231. Every screen's findings must have reached the register.

    R7.7 calls intake "automatic, not discretionary" and it had one caller: someone remembering.
    Nine retrospectives existed and none had ever created an item. This is the refusal that
    replaces the intention (R14.2).

    Two halves, because the capture surface is moving: any retrospective FILE still on disk must
    be ingested, and any screen run named in the log must have recorded findings or an EXPLICIT
    no-findings result. A screen that quietly reported nothing fails - "there was nothing" and
    "nobody looked" must never render the same (R2.10).
    """
    if intake is None:
        try:
            import isa_retrospective_intake as intake
        except Exception as e:                                    # noqa: BLE001
            return [f"ISA-0229: isa_retrospective_intake unavailable ({e}) - retrospective "
                    f"coverage reported as UNKNOWN, never as PASS"]
    errs = [f"ISA-0229: {g}" for g in intake.coverage()]
    if screens:
        errs += [f"ISA-0231: {g}" for g in intake.run_coverage(screens)]
    return errs

def pair_rationale_ledger(ledger=None):
    """R12.3 / P2.5. Every capital-gating constant must have a ledger record.

    A missing RECORD and a missing REASON are different failures and are reported apart:
    `coverage()` is "nobody even asked", `gaps()` is "nobody wrote down why". Only the first
    fails the battery - NO_RECORDED_RATIONALE is a permitted and important answer under R12.3,
    and it already auto-raises its own OPEN item. Failing on it would push me to invent
    justifications to turn the battery green, which is the worst outcome available.
    """
    if ledger is None:
        try:
            import isa_rationale_ledger as ledger
        except Exception as e:                                    # noqa: BLE001
            return [f"R12.3: isa_rationale_ledger unavailable ({e}) - rationale coverage "
                    f"reported as UNKNOWN, never as PASS"]
    return [f"R12.3: {c}" for c in ledger.coverage()]

def pair_archive_backlog(reg=None):
    """Ageing policy (Raj, 12-Aug-2026). Reports LOW items past the archive threshold.

    It REPORTS; it never archives. A check that mutates the thing it checks cannot be trusted to
    tell you what it found, and an item disappearing inside a routine battery run is exactly the
    kind of silent state change this framework keeps getting caught by.
    """
    if reg is None:
        import isa_register as reg
    due = reg.archive_candidates()
    if not due:
        return []
    return [f"AGEING: {len(due)} LOW item(s) are past the {reg.ARCHIVE_AFTER_DAYS['LOW']}-day "
            f"archive threshold - run `python3 -c \"import isa_register as R; "
            f"R.archive_aged(dry_run=False)\"` to close them (reversible)"]

def pair_register_store_protected(exists=os.path.exists, never_purge=None, permanent=None):
    """ISA-0026. The canonical item register must exist AND be named in the protection sets.

    BuildSpec s3 said P1 'must handle, not discover' this, and P1 shipped without it: the store
    that records every defect was itself the one store no capture rule protected. The check is
    two-sided on purpose - a file present but unprotected, and a file protected but absent, are
    different defects and neither may pass (R4.3: a control fed nothing BLOCKS, never PASSes).
    """
    errs = []
    if never_purge is None:
        try:
            import capture_archive
            never_purge = capture_archive.NEVER_PURGE
        except Exception as e:                                    # noqa: BLE001
            return [f"ISA-0026: capture_archive.NEVER_PURGE unreadable ({e}) - the protection "
                    f"of the item register cannot be verified, so it is reported as UNKNOWN, "
                    f"never as PASS"]
    permanent = CAPTURE_PERMANENT if permanent is None else permanent
    state = os.path.join(HERE, "Dashboard", "state")
    for fn in REGISTER_STORE_FILES:
        if not exists(os.path.join(state, fn)):
            errs.append(f"ISA-0026: register store file absent: Dashboard/state/{fn}")
        if fn not in never_purge:
            errs.append(f"ISA-0026: {fn} is not in capture_archive.NEVER_PURGE - the one store "
                        f"that cannot be reconstructed by re-running anything")
        if os.path.splitext(fn)[0] not in permanent:
            errs.append(f"ISA-0026: {fn} is not in consistency_check.CAPTURE_PERMANENT")
    return errs


def pair_register_renders_current(check=None):
    """R14.3 / R15.4. The markdown registers are RENDERS; drift is a defect either way.

    If a view differs from what the store produces, either someone hand-edited a generated file
    (and the edit is about to vanish, with the store never learning of it) or the store moved and
    the view was not refreshed. Both are a document that says one thing and is another - which is
    the failure class this whole register exists to catch, applied to the register itself.
    """
    if check is None:
        try:
            import isa_register_render
            check = isa_register_render.check
        except Exception as e:                                    # noqa: BLE001
            return [f"R14.3: isa_register_render unavailable ({e}) - register-view drift cannot "
                    f"be verified, reported as UNKNOWN rather than PASS"]
    try:
        res = check(HERE)
    except Exception as e:                                        # noqa: BLE001
        return [f"R14.3: register render check failed to run: {type(e).__name__}: {e}"]
    drift = res.get("drift", [])
    if drift:
        drift = list(drift) + ["fix: python3 isa_register_render.py --write  "
                               "(the store is right; the views are behind it)"]
    return [f"R14.3: {d}" for d in drift]


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


def pair_er_callsite_manifest():
    """D-24 §1.3 (09-Aug-2026). TWO INDEPENDENT DERIVATIONS OF "WHO COMPUTES E[r]".

    `expected_return`'s docstring named two consumers. On disk there were NINE, and every one
    called `expected_return_for_row(row)` with no context argument — so the moment an anchor-table
    parameter existed, eight of them would have gone on silently running the pre-D-24 behaviour on
    92% of every universe screened, and nothing would have said so.

    The declared set (`scoring_config.ER_CALLSITE_MANIFEST`) and the set the AST actually finds
    must agree. A tenth caller added later fails this until someone decides, in writing, what
    anchor table it receives. Same instrument as pair_orchestrator_parity, pointed at a CONTRACT
    SURFACE rather than a code path.

    Also asserts §6 reachability: the fundamentals evidence route must be ACHIEVABLE at all. Had
    that assertion existed, F4 would have been caught the day the val-hist fetch degraded rather
    than months later.
    """
    out = []
    try:
        sys.path.insert(0, HERE)
        import orchestrator_parity as _op
        m = _op.er_callsite_manifest()
        if m.get("undeclared_callers"):
            out.append(f"E[r] CALL-SITE MANIFEST: {m['undeclared_callers']} import "
                       "`expected_return` and are NOT in scoring_config.ER_CALLSITE_MANIFEST. "
                       "Each must declare whether it receives the screen anchor table, reads the "
                       "persisted one, or explicitly reports er_status='unmeasured' (D-24 §1).")
        if m.get("declared_but_absent"):
            out.append(f"E[r] CALL-SITE MANIFEST: {m['declared_but_absent']} are declared in the "
                       "manifest but no longer import `expected_return` — remove them before the "
                       "list rots into decoration.")
        if m.get("blank_reason"):
            out.append(f"E[r] CALL-SITE MANIFEST: entries with no stated role: {m['blank_reason']}"
                       " — a manifest that can be padded silently is not a control.")
        r = _op.er_reachability()
        if not r.get("ok"):
            out.append(f"E[r] REACHABILITY: {r.get('reason')}. The fundamentals evidence route "
                       "cannot be reached by ANY name — the gate is decorative until this is fixed.")
    except Exception as e:                                     # noqa: BLE001
        out.append(f"E[r] CALL-SITE MANIFEST could not run ({type(e).__name__}: {e}) — "
                   "treat as UNKNOWN, not as PASS.")
    return out


def pair_score_panel_date_format(store=None):
    """D-15 (09-Aug-2026). ONE SPELLING PER DAY IN THE PANEL KEY.

    `score_panel.csv` held the 08-Aug F250-SPI screen twice — 140 rows under `2026-08-08` and the
    same 140 under `20260808` — because the key is (run_date, group, ticker) and two spellings of
    one day are two keys. The idempotent merge could not see them as one screen, every study
    grouping by run_date double-counted it, and the compact rows join nothing in
    `capture_screen_artefacts`, which joins gate_variables on the ISO form.

    The writer now refuses a non-ISO date. This is the STORE-level second derivation: the writer
    can only speak for the rows it wrote, and a panel edited or merged by any other route would
    otherwise drift back silently.
    """
    errs = []
    try:
        import score_panel_logger as _spl
    except Exception as e:                                     # noqa: BLE001
        return [f"D-15: score_panel_logger unimportable ({type(e).__name__}: {e})"]
    path = store or os.path.join(HERE, "score_panel.csv")
    if not os.path.exists(path):
        return errs
    bad = _spl.assert_one_date_format(path)
    if bad:
        errs.append(f"D-15: score_panel.csv carries non-ISO run_date values {bad} — the panel key "
                    f"is (run_date, group, ticker), so each is a duplicate screen under a second "
                    f"spelling of one day")
    try:
        import csv as _csv
        with open(path, encoding="utf-8") as fh:
            seen, dup = set(), set()
            for r in _csv.DictReader(fh):
                k = (r.get("run_date"), r.get("group"), r.get("ticker"))
                (dup.add(k) if k in seen else seen.add(k))
        if dup:
            errs.append(f"D-15: {len(dup)} duplicate (run_date, group, ticker) keys in "
                        f"score_panel.csv, e.g. {sorted(dup)[:3]}")
    except Exception as e:                                     # noqa: BLE001
        errs.append(f"D-15: score_panel.csv unreadable for the duplicate-key check: {e}")
    return errs


def pair_benchmark_registry():
    """ISA-0320 / ISA-0307 (13-Aug-2026). Three things the routine battery now refuses to let past.

    (a) EVERY fund carries a SOURCED `mandate_benchmark`. The comparator sets alpha, which feeds
        M*, the FRS risk-adjusted component and the T4 mandate-breach trigger; R6.1 says a
        decision-grade input has one golden source, and until today it had none at all.
    (b) EVERY comparator series is twin-tested, spike-scanned and basis-break scanned. Choosing
        the right index is worth nothing if the price history for it is defective, and on the
        first run 4 of 27 candidate series were.
    (c) The stored `beta_alpha_study` artefact is FRESH against fund_universe. A map entry added
        after a run used to be invisible to every consumer; the study now stamps the fingerprint
        it was built from, and a mismatch is a stale artefact, not a difference of opinion.
    """
    errs = []
    try:
        sys.path.insert(0, HERE)
        import benchmark_registry as breg
        U = breg.load_universe()
        v = breg.validate_all(U)
    except Exception as e:                                     # noqa: BLE001
        return [f"A20/benchmark_registry could not run: {type(e).__name__}: {e}"]
    for m in v["errors"]:
        errs.append(f"A20/benchmark series: {m}")
    art = os.path.join(HERE, "beta_alpha_study_aug2026.json")
    if not os.path.exists(art):
        return errs + ["A20/beta_alpha_study_aug2026.json is missing"]
    try:
        with open(art, encoding="utf-8") as fh:
            A = json.load(fh)
    except Exception as e:                                     # noqa: BLE001
        return errs + [f"A20/beta_alpha_study artefact unreadable: {e}"]
    cov = A.get("coverage")
    if cov is None:
        errs.append("A20/beta_alpha_study declares no coverage — an artefact must assert its own "
                    "fitness for the use it is put to (R4.10)")
    elif cov.get("not_decomposed"):
        errs.append(f"A20/beta_alpha_study left {cov['not_decomposed']} undecomposed; those "
                    f"holdings carry no measured beta and M* coverage is short by their weight")
    if cov and cov.get("not_decomposed") and not A.get("warnings"):
        errs.append("A20/beta_alpha_study reports an undecomposed fund AND an empty warnings "
                    "list — a reader that cannot produce a result COUNTS it and fails (R4.9)")
    stamped = (A.get("provenance") or {}).get("mandate_fingerprint")
    if not stamped:
        errs.append("A20/beta_alpha_study does not stamp the mandate fingerprint it was built "
                    "from, so staleness against fund_universe cannot be detected (ISA-0307)")
    else:
        src = {s: (U[s]["mandate_benchmark"]["index_name"],
                   U[s]["mandate_benchmark"]["comparator"]["ticker"])
               for s in A.get("funds", {}) if isinstance(U.get(s), dict)
               and "mandate_benchmark" in U[s]}
        now = hashlib.sha256(json.dumps(src, sort_keys=True).encode()).hexdigest()[:12]
        if now != stamped:
            errs.append(f"A20/beta_alpha_study is STALE: built from mandate fingerprint {stamped}, "
                        f"fund_universe today is {now}. Re-run it; do not read it.")
    for c in (A.get("controls") or {}).values():
        if not c.get("pass"):
            errs.append("A20/beta_alpha_study control FAILED — a passive tracker did not reproduce "
                        "beta ~1 / alpha ~ -OCF, so no alpha in the artefact is trustworthy")
            break
    return errs



# ── ISA-0264 / ISA-0336 / ISA-0337 pairs (13-Aug-2026) ──────────────────────────────────

# The screener entry points were NOT in TRACKED_SCRIPTS, which is a large part of why
# ISA-0264 escaped: the one name-resolution check in the battery never read the file that
# broke. Scoped list, stated here, so adding a screener script is a deliberate act.
NAME_RESOLUTION_SCRIPTS = TRACKED_SCRIPTS + (
    "screener_local.py", "screener_core.py", "fv_composite.py", "gate_variables.py",
    "isa_register.py", "isa_register_render.py", "isa_register_export.py",
    "isa_retrospective_intake.py", "build_email.py", "build_excel.py",
)

_STDLIB_MODULES = (
    "os", "sys", "json", "math", "time", "re", "csv", "io", "ast", "abc", "glob", "shutil",
    "random", "socket", "struct", "hashlib", "hmac", "base64", "pickle", "sqlite3", "logging",
    "argparse", "datetime", "pathlib", "subprocess", "itertools", "functools", "collections",
    "statistics", "textwrap", "traceback", "warnings", "importlib", "urllib", "unittest",
    "tempfile", "typing", "copy", "string", "zipfile", "calendar", "decimal", "fractions",
)


def pair_unimported_stdlib_modules(py_texts):
    """A module name READ in a file but never imported or assigned in it.

    ISA-0264, 07-Aug-2026: an edit absorbed the tail of screener_local.py's module-level
    import line, leaving `import argparse` alone and `os`, `json`, `sys`, `math`, `time`,
    `importlib.util`, `datetime` and `urllib.request` bound to nothing. Every weekly screen
    would have died on `NameError: name 'os' is not defined`, and it sat latent for 12 days.

    py_compile cannot see it - the file is syntactically perfect. `pair_undefined_constants`
    could not see it either: that check is scoped to ALL_CAPS by convention, and `os` is
    lowercase. So the closure of ISA-0264 needed a check that did not exist, which is the
    point of R7.3 - without one, "fixed" is a narrative claim.

    Scoped to a declared stdlib list so precision stays high: an unknown lowercase name is
    far more often a local than a missing import, and a noisy check gets ignored.
    """
    errs = []
    for fn, txt in sorted(py_texts.items()):
        try:
            tree = ast.parse(txt, filename=fn)
        except SyntaxError as e:
            errs.append(f"A18/M9: {fn} does not parse ({e})")
            continue
        bound = _assigned_and_imported(tree)
        seen = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in _STDLIB_MODULES and node.id not in bound
                    and node.id not in seen):
                seen.add(node.id)
                errs.append(
                    f"A18/M9: {fn} uses {node.id}.* at line {node.lineno} but never imports or "
                    f"assigns {node.id} - NameError on first execution (ISA-0264 class)")
    return errs


def pair_no_informational_in_register(items=None):
    """No live register item is a finding the registrability gate would now refuse.

    ISA-0336, Raj 13-Aug-2026: concentration warnings, SUMMARY-thinness warnings and
    "worked cleanly" confirmations belong on the retrospective and not on the register.
    Filtering them at intake stops NEW ones arriving. This pair is what stops OLD ones
    surviving and stops a future edit quietly relaxing the gate: if either the rules or the
    store drift, the battery goes red rather than the register slowly refilling.
    """
    errs = []
    try:
        import isa_register as _R
        import isa_retrospective_intake as _RI
    except Exception as e:                                          # noqa: BLE001
        return [f"A18/M10: registrability pair could not run ({e}) - NOT checked this run"]
    rows = items if items is not None else _R.read_all()
    for it in rows:
        if it.get("state") in _R.CLOSED_STATES or it.get("intake_trigger") != "retrospective":
            continue
        # ⚑ SCOPED TO domain == "screener", and the scope is the point. Raj asked about
        # SCREENER retrospectives - Gate 4 sector readings, SUMMARY pool sizes. The first run
        # of this pair flagged ISA-0176, "4 of the 5 VCI watchlist names are quantum -
        # concentration flag live", which is a live CAPITAL risk in a held sleeve, not a
        # statistic about a universe the screen looked at. The word "concentration" means
        # different things in the two places, and a filter that could not tell them apart
        # would have quietly closed a position-risk item. Both intake paths already stamp
        # domain="screener", so this changes nothing about what the gate admits - it stops
        # the gate reaching into domains it was never asked about.
        if it.get("domain") != "screener":
            continue
        title = it["title"].split("] ", 1)[1] if "] " in it["title"] else it["title"]
        v = _RI.registrability(title, "", it.get("criticality", "MEDIUM"))
        if not v["registrable"]:
            errs.append(f"A18/M10: {it['id']} is live but rule {v['rule']} says it is a "
                        f"retrospective observation, not a fix or enhancement: {title[:90]}")
    return errs


def pair_register_fourc_complete(items=None):
    """Every post-cutover item captures all five mandatory Cs.

    ISA-0337, Raj 13-Aug-2026: "it is imperative for every one of the 4Cs to be captured
    going forward for every new item. this is non-negotiable." isa_register refuses such a
    write. This pair is the second gate: it reads the STORE rather than the writer, so an
    item inserted by any route - a migration, a hand-edited line, a future script that
    bypasses write() - is caught. One control at the door, one on the room.
    """
    errs = []
    try:
        import isa_register as _R
    except Exception as e:                                          # noqa: BLE001
        return [f"A18/M11: 4C pair could not run ({e}) - NOT checked this run"]
    rows = items if items is not None else _R.read_all()
    for it in rows:
        if not _R.fourc_binds(it):
            continue
        gaps = _R.fourc_gaps(it)
        if gaps:
            errs.append(f"A18/M11: {it['id']} was created on or after {_R.FOURC_CUTOVER} with "
                        f"{len(gaps)} of 5 Cs not captured ({', '.join(gaps)}) - "
                        "mandatory since 13-Aug-2026 (ISA-0337)")
    return errs


def check_all():
    errs = []
    run_ctx = _read("Run_Context_ISA_Growth_Stock_Analysis.md")
    bem = _read("build_email.py")
    errs += pair_standard_referenced()           # ISA-0027 / O-10 (12-Aug-2026)
    errs += pair_retrospectives_ingested()       # ISA-0229 / ISA-0231 (12-Aug-2026)
    errs += pair_rationale_ledger()              # R12.3 / P2.5 (12-Aug-2026)
    errs += pair_archive_backlog()               # ageing policy (12-Aug-2026)
    errs += pair_run_surface_basis()             # ISA-0211 (12-Aug-2026)
    errs += pair_register_store_protected()      # ISA-0026 (12-Aug-2026)
    errs += pair_register_renders_current()      # R14.3 register-view drift (12-Aug-2026)
    errs += pair_benchmark_registry()             # ISA-0320 / ISA-0307 (13-Aug-2026)
    errs += pair_no_informational_in_register()  # ISA-0336 (13-Aug-2026)
    errs += pair_register_fourc_complete()       # ISA-0337 (13-Aug-2026)
    errs += pair_orchestrator_parity()
    errs += pair_er_callsite_manifest()          # D-24 §1.3 (09-Aug-2026)
    errs += pair_score_panel_date_format()       # D-15 (09-Aug-2026)
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
        errs += pair_anchor_cadence(state)
        errs += pair_register_updated_after_build()
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
        # ROOT CAUSE of ISA-0002, 12-Aug-2026: this guard was built specifically to catch
        # fetch_metrics_local.py and was then pointed at ONE document. The file that still
        # invoked the deleted script was Skills_to_Edit/isa-monthly-prerun/SKILL.md - a
        # scheduled-task prompt that no guard had ever read. The SKILL prompts ARE execution
        # contracts. A verifier whose coverage excludes the surface where the defect lives
        # fails as a false PASS (FC-H).
        _surfaces = {MONTHLY_CTX: mctx}
        try:
            import framework_atlas as _fa
            _surfaces.update(_fa.run_surface_texts())
        except Exception as _e:
            errs.append(f"A18/monthly: run-surface enumeration unavailable ({_e}) - "
                        "scripts referenced by SKILL prompts and other Run_Contexts were NOT "
                        "checked this run. Reported, never silently skipped (R4.9)")
        errs += pair_referenced_scripts_exist(_surfaces)
        errs += pair_monthly_retired("\n".join(_surfaces.values()))
        # Name-resolution across the scripts the two runs depend on.
        errs += pair_undefined_constants({fn: _read(fn) for fn in TRACKED_SCRIPTS
                                          if os.path.exists(os.path.join(HERE, fn))})
        errs += pair_unimported_stdlib_modules(
            {fn: _read(fn) for fn in NAME_RESOLUTION_SCRIPTS
             if os.path.exists(os.path.join(HERE, fn))})      # ISA-0264
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

    # ---- A19b: the D-2 two-speed cadence (12-Aug-2026) --------------------------------------
    _cad_ok = {"schema_version": 2, "required_return_reported_floor_pct": 13.8,
               "required_return_reported_operative_pct": 13.8,
               "required_return_operative_pct": 13.8, "operative_effective_from": "2026-08-12",
               "operative_derived_at": "2026-08-12", "operative_next_window": "2026-09-30",
               "anchor_cadence": {"authority": "HELD_IN_WINDOW"},
               "flow_trigger": {"status": "OK", "fired": False},
               "valuation_basis": {"basis": "spot_fallback", "degraded": True,
                                   "degraded_reason": "2 of 3 observations"}}
    assert not pair_anchor_cadence(_cad_ok), pair_anchor_cadence(_cad_ok)
    # a pre-two-speed file must FAIL rather than read as clean
    assert pair_anchor_cadence({**_cad_ok, "schema_version": 1})
    # break-glass armed and unapplied must FAIL
    assert pair_anchor_cadence({**_cad_ok, "required_return_reported_operative_pct": 16.0})
    # ...and 1.99pp of drift must NOT — the whole point of two speeds is that drift is allowed
    assert not pair_anchor_cadence({**_cad_ok, "required_return_reported_operative_pct": 15.79})

    # ---- ISA-0321: the end-of-build register gate (Raj, 12-Aug-2026) -------------------------
    import tempfile as _tf, datetime as _dt2
    _t = "2026-08-12"
    _stale = [{"id": "ISA-0001", "created_on": "2026-08-01", "updated_on": "2026-08-01",
               "resolved_on": None}]
    _fresh = [{"id": "ISA-0001", "created_on": "2026-08-01", "updated_on": _t, "resolved_on": None}]
    with _tf.TemporaryDirectory() as _td:
        _f = os.path.join(_td, "screener_core.py")
        open(_f, "w").write("# built today\n")
        os.utime(_f, (_dt2.datetime.fromisoformat(_t).timestamp(),) * 2)
        # a build with a STALE register must FAIL
        assert pair_register_updated_after_build(_td, _t, _stale), \
            "a source file changed today with no register activity must FAIL the battery"
        # ...and the SAME build with a touched item must PASS - so the gate is not always-red
        assert not pair_register_updated_after_build(_td, _t, _fresh), \
            "an item updated today must satisfy the gate (negative control)"
        # resolved_on alone satisfies it: closing an item IS register activity
        assert not pair_register_updated_after_build(
            _td, _t, [{"id": "ISA-0001", "created_on": "2026-01-01", "updated_on": "2026-01-01",
                       "resolved_on": _t}]), "a closure must satisfy the gate"
        # a day with NO build must not fire, however stale the register is
        assert not pair_register_updated_after_build(_td, "2026-08-13", _stale), \
            "the gate must be silent on a day when nothing was built"
        # ⚑ a GENERATED view changing must NOT count as a build, or the gate dies of false positives
        for _g in ("ISA_OPEN_ITEMS_REGISTER.md", "ISA_Item_Register.csv"):
            _gp = os.path.join(_td, _g)
            open(_gp, "w").write("generated\n")
            os.utime(_gp, (_dt2.datetime.fromisoformat(_t).timestamp(),) * 2)
        os.remove(_f)
        assert not pair_register_updated_after_build(_td, _t, _stale), \
            "regenerating the views is not a build and must not trip the gate"
    # an unevaluable D-4 trigger must FAIL, never pass as "no flow"
    assert pair_anchor_cadence({**_cad_ok,
                                "flow_trigger": {"status": "UNKNOWN", "blocks": True,
                                                 "reason": "ledger absent"}})
    # a degradation with no reason must FAIL; a degradation WITH one must not
    assert pair_anchor_cadence({**_cad_ok,
                                "valuation_basis": {"basis": "spot_fallback", "degraded": True}})
    # a missing two-speed field must FAIL
    assert pair_anchor_cadence({k: v for k, v in _cad_ok.items() if k != "anchor_cadence"})
    # a held operative value outside the D1c band must FAIL even though nothing re-derived it
    assert pair_anchor_cadence({**_cad_ok, "required_return_operative_pct": 21.0,
                                "required_return_reported_operative_pct": 21.0})

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
    # ISA-0228: a NEGATING line is a prohibition, not an instruction.
    assert not pair_monthly_retired(
        good_m + "\nEXECUTION — the workflow is Steps 1–10. There is no Step 11, 12 or 13.")
    # ...and the exemption must not become a hole: a genuine instruction still FAILS.
    assert pair_monthly_retired(good_m + "\nAt Step 12, source fresh metrics for the candidate."), \
        "ISA-0228 negative control: a real instruction to use a retired step must still FAIL"
    # M5 — an executed stage with no row in the table (the 1.5/6.5/9b.5/9c/9d defect).
    assert pair_monthly_prerun_stages(good_m.replace("| **9d** | x |\n", ""), good_prerun)
    # M6 — heading count vs the list it heads.
    assert pair_monthly_prerun_reads(good_m.replace("(2 reads", "(8 reads"))
    # M7 — THE fetch_metrics_local class: a contract naming a script that is not on disk.
    assert pair_referenced_scripts_exist({"ctx": "run fetch_metrics_local.py now"},
                                         exists=lambda p: False)
    assert not pair_referenced_scripts_exist({"ctx": "run extract_portfolio.py now"},
                                             exists=lambda p: True)
    # M7b — the coverage half of M7: the guard must read EVERY run surface, not one document.
    # This is the assertion that was missing while isa-monthly-prerun/SKILL.md invoked a
    # script deleted on 21-Jun-2026, every month, unchecked.
    assert pair_referenced_scripts_exist(
        {"ctx": "nothing here", "isa-monthly-prerun": "python3 fetch_metrics_local.py --watchlist x"},
        exists=lambda p: False), "M7b: a missing script named by a SKILL prompt must fail"
    assert not pair_referenced_scripts_exist(
        {"isa-monthly-prerun": "~~python3 fetch_metrics_local.py~~ RETIRED 21-Jun-2026"},
        exists=lambda p: False), "M7b: a self-marked retirement note is not a live reference"
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

    # Ageing: a due item must be reported, and a clean register must not be.
    class _Due:
        ARCHIVE_AFTER_DAYS = {"LOW": 90}
        @staticmethod
        def archive_candidates():
            return [({"id": "ISA-0001"}, 120, 90)]
    assert pair_archive_backlog(_Due), "an overdue LOW item must be reported"
    class _Clean:
        ARCHIVE_AFTER_DAYS = {"LOW": 90}
        @staticmethod
        def archive_candidates():
            return []
    assert not pair_archive_backlog(_Clean), "a clean register must not report an archive backlog"

    # R12.3 negative controls: a constant with no ledger record FAILS; a recorded constant with
    # no reason does NOT fail, because inventing reasons to go green is the failure mode.
    class _NoLedger:
        @staticmethod
        def coverage():
            return ["SOURCE_WEIGHTS: declared capital-gating with no ledger record (R12.3)"]
    assert pair_rationale_ledger(_NoLedger), "R12.3: an unrecorded capital constant must FAIL"

    class _SeededLedger:
        @staticmethod
        def coverage():
            return []
    assert not pair_rationale_ledger(_SeededLedger), \
        "R12.3: NO_RECORDED_RATIONALE is a permitted answer and must NOT fail the battery"

    # ISA-0229 negative controls: an un-ingested retrospective and an unrecorded screen run.
    class _FakeIntake:
        @staticmethod
        def coverage():
            return ["20260814_NASDAQ_retrospective.md: never ingested into the register (R7.7)"]
        @staticmethod
        def run_coverage(screens):
            return [f"{x}: recorded neither a finding nor an explicit no-findings result"
                    for x in screens]
    assert pair_retrospectives_ingested(_FakeIntake), \
        "ISA-0229: an un-ingested retrospective must FAIL"
    assert pair_retrospectives_ingested(_FakeIntake, screens=["20260815_SP500"]), \
        "ISA-0231: a screen that recorded nothing must FAIL"

    class _CleanIntake:
        @staticmethod
        def coverage():
            return []
        @staticmethod
        def run_coverage(screens):
            return []
    assert not pair_retrospectives_ingested(_CleanIntake, screens=["20260815_SP500"]), \
        "ISA-0229: a fully recorded screen must pass"

    # ISA-0027 negative controls: a surface with no route to the standard must FAIL,
    # and the delegation chain must be honoured rather than demanding the string everywhere.
    assert pair_standard_referenced(
        {"a_skill": "do the thing", "Run_Context_X": "rules here"}, exists=lambda p: True), \
        "ISA-0027: a surface with no route to the standard must FAIL"
    assert not pair_standard_referenced(
        {"a_skill": "follow Run_Context_X.md", "Run_Context_X": "see ISA_Engineering_Rules.md"},
        exists=lambda p: True), "ISA-0027: delegation via a Run_Context is a valid route"
    assert not pair_standard_referenced(
        {"a_skill": "see ISA_Engineering_Rules.md"}, exists=lambda p: True), \
        "ISA-0027: a direct reference is a valid route"
    assert pair_standard_referenced(
        {"a_skill": "see ISA_Engineering_Rules.md"}, exists=lambda p: False), \
        "ISA-0027: referencing a standard that is not on disk must FAIL"

    # ISA-0211 negative control: reported drift must surface, and an unreachable live
    # directory must NOT be reported as clean.
    class _FakeAtlas:
        @staticmethod
        def run_surface_mirror_drift():
            return ["monthly-isa-portfolio-review: mirror differs from the executed prompt"]
    assert pair_run_surface_basis(_FakeAtlas), "ISA-0211: mirror drift must be reported"

    class _NoLive:
        @staticmethod
        def run_surface_mirror_drift():
            return []
        @staticmethod
        def scheduled_skills_dir():
            return None
        @staticmethod
        def run_surface_texts(with_basis=False):
            return {"x": ("t", "mirror")} if with_basis else {"x": "t"}
    assert "COULD NOT BE CHECKED" in run_surface_basis_note(_NoLive), \
        "ISA-0211: an unreachable live directory must be STATED, never rendered as 'no drift'"

    # ISA-0026 negative controls: a present-but-unprotected file, and a protected-but-absent one.
    assert pair_register_store_protected(exists=lambda p: True, never_purge=(), permanent=()), \
        "ISA-0026: an unprotected register store must FAIL"
    assert pair_register_store_protected(
        exists=lambda p: False,
        never_purge=REGISTER_STORE_FILES,
        permanent=tuple(os.path.splitext(f)[0] for f in REGISTER_STORE_FILES)), \
        "ISA-0026: a protected store file that is ABSENT must FAIL - protection of nothing is not a pass"
    assert not pair_register_store_protected(
        exists=lambda p: True,
        never_purge=REGISTER_STORE_FILES,
        permanent=tuple(os.path.splitext(f)[0] for f in REGISTER_STORE_FILES)), \
        "ISA-0026: a present, protected store must pass"
    # R14.3 negative control: reported drift must surface as an error, not be swallowed.
    assert pair_register_renders_current(check=lambda _h: {"ok": False, "drift": ["X.md: differs"]})
    assert not pair_register_renders_current(check=lambda _h: {"ok": True, "drift": []})

    print("consistency_check SELF-TEST OK (growth pairs + 10 monthly pairs + register pairs)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        sys.exit(0)
    errors = check_all()
    try:
        print(run_surface_basis_note())
    except Exception as _e:                                       # noqa: BLE001
        print(f"run-surface basis: UNKNOWN ({_e})")
    for e in errors:
        print(f"[FAIL] {e}")
    print("ALL PAIRS GREEN" if not errors else f"{len(errors)} MISMATCH(ES)")
    sys.exit(1 if errors else 0)
