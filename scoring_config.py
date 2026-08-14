#!/usr/bin/env python3
"""
scoring_config.py — SINGLE SOURCE OF TRUTH for ISA path-scorer thresholds.

WHY THIS EXISTS
  The pre-run must use ONLY the scorers used in the 2 live paths (growth / VCI; Path C energy
  which are continually enhanced. To stop display/threshold constants drifting out of
  sync (the the former pre-run formatter "/54" divergence), the canonical thresholds live HERE and are
  imported by: screener_core.py, energy_screener.py, and the pre-run formatter
  (normalise_adapter.py). No script may hold its own private copy of these numbers.

  Lightweight by design: NO heavy imports (no yfinance/pandas) so it is safe to import
  anywhere, including the pre-run formatter step.

KEEP IN SYNC: the *_MAX values are facts about each scorer's computation
  (growth Part B = 11 metrics x2 = 22; total 50). If a scorer adds/removes a scored
  metric, update the matching _MAX here in the SAME change.
"""

# ===========================================================================
# GROWTH  (screener_core.py — v27: Part A /28 + Part B /22 = Total /50)
# ===========================================================================
GROWTH_PART_A_MAX        = 28
GROWTH_PART_B_MAX        = 22     # v27 (was 26 pre-v27)
GROWTH_TOTAL_MAX         = 50     # v27 BASE (Part A 28 + Part B 22)
# EXTENDED max — PERMITTED for semiconductor_hardware/equipment stocks that earn the
# book-to-bill + backlog/EV conditional metrics (Part B 26 -> Total 54). The per-stock
# max comes from the scored data `total_max`/`part_b_max` fields (set by screener_core);
# scripts default to the BASE when those fields are absent.
GROWTH_PART_B_MAX_EXTENDED = 26
GROWTH_TOTAL_MAX_EXTENDED  = 54

GROWTH_PART_A_STRONG     = 22     # "Strong" Part A
GROWTH_PART_A_ACCEPTABLE = 14
GROWTH_PART_B_STRONG     = 16     # v27-recalibrated: 16/22 ~= 73% (was 19/26 ~= 73%)
GROWTH_PART_B_ACCEPTABLE = 11     # ~50%

# SUMMARY-tab inclusion rule (v27). NOTE: count-based top ~25-30 selection (Source
# Score) supersedes the fixed Total cut in the redesign — kept here for the legacy rule.
GROWTH_SUMMARY_PART_B_MIN = 14
GROWTH_SUMMARY_TOTAL_MIN  = 43

# Analyst-disparity trigger (high combined score). 37/50 ~= 74% (was 40/54).
GROWTH_HIGH_SCORE         = 37

# ── Gate relaxation (redesign Part 3 §8) — GM is a SECTOR-SEGMENTED SCORE, not a hard gate ──
# The Gate-2 hard GM gate excluded low-GM non-software winners (UNH 18.8%, industrials ~40%)
# BEFORE scoring. Relaxing it lets them survive; GROSS_MARGIN_SCORE_THRESHOLDS (sector-segmented:
# SaaS strong>=70%/accept>=55% vs default 30%/20%) differentiates quality in Part A instead.
# DEFAULT False = current behaviour (Friday-safe). Set True to ACTIVATE — relaxing ~doubles the
# scored set (Part 3 §11), so switch on together with the two-pass fetch / after assessing the
# SUMMARY + high-score-overlay fetch growth.
RELAX_GM_GATE      = True
GM_VIABILITY_FLOOR = 0.0     # when relaxed, gate ONLY genuinely broken businesses (negative gross margin)

# Gate 3 (FCF) soften — negative FCF from strategic capex is OK if operations generate cash (OCF>0).
# ORCL-type (FCF-negative on data-centre capex, OCF strongly positive) survives; genuine cash-burners
# (OCF<=0) still gated. DEFAULT False (Friday-safe); activate with the two-pass fetch.
RELAX_FCF_GATE     = True

# Gate 4 (revenue CAGR) forward-inclusive — low-trailing-growth turnarounds (UNH +2%) survive if not
# declining; forward growth / estimate momentum is scored downstream by the forward axis. DEFAULT False.
RELAX_CAGR_GATE        = True
GATE4_RELAXED_CAGR_MIN = 0.0   # when relaxed, pass if 3yr CAGR >= this (i.e. revenue not shrinking)

# H7 debug fix (redesign §8 / partner to H2). The Part A ROIC + FCF-positive-years HARD gates run inside
# the scorer and stamp final_status=HARD_GATE_FAIL (not rankable) — independent of the GENERATION-gate
# relaxations above — so ORCL-type strategic-capex negative-FCF and low-ROIC turnarounds are dropped from
# the CANDIDATES ranking BEFORE the forward selection can admit them. When RELAX_PARTA_HARDGATES is on,
# these two hard gates become quality FLAGS (low_roic / low_fcf_positive_years) carried into scoring — the
# name stays CANDIDATE_RANKABLE and scores 0 on those metrics (so it is penalised in the rank, not deleted).
# Net Debt/EBITDA > 3 (MANDATORY_MINIMUM_FAIL) is UNCHANGED — genuine leverage risk, not a quality gate.
# DEFAULT off; activate in S5 together with the generation-gate relaxations + FORWARD_ELIGIBILITY.
RELAX_PARTA_HARDGATES  = True

# H8 — serviceable-leverage carve-out for the Net Debt/EBITDA > 3 MANDATORY minimum (redesign §8; real
# data: ORCL scored Part A=14 but MANDATORY_MINIMUM_FAIL on ND/EBITDA>3 — its leverage funds data-centre
# capex + M&A and is well-covered, i.e. "good leverage"). When RELAX_ND_MANDATORY is on, ND/EBITDA>3 becomes
# a FLAG (high_leverage_serviceable), NOT a hard fail, IF the debt is comfortably serviced (net cash OR
# interest coverage >= ND_SERVICEABLE_INT_COV). Genuinely distressed over-leverage (weak coverage) still
# fails. Leverage is still scored (score_nd_ebitda) so it's penalised in the rank, not deleted. DEFAULT off
# (activate in S5 with the other relaxations). ND_SERVICEABLE_INT_COV provisional — calibrate in shadow.
RELAX_ND_MANDATORY     = True
ND_SERVICEABLE_INT_COV = 4.0

# ── Forward axis (redesign Part 3 §13) — forward signals combined into a 0-100 Forward score (F) ──
# F is SEPARATE from Part A (quality) / Part B (valuation). Computed additively + carried in the
# scored data (for shadow analysis); the Source Score (rerank) combines F + quality + valuation.
# Thresholds are PROVISIONAL — calibrate in shadow (Part 3 §10: losers print +3-5% eps_trend, so the
# strong band sits above the noise). Sub-scores are 0/1/2.
EPS_TREND_MOM_THRESHOLDS = (8.0, 2.0)   # +1y consensus EPS, now vs 90d ago, % : strong>=8, acceptable>=2
REV_EST_FWD_THRESHOLDS   = (15.0, 5.0)  # forward revenue growth % : strong>=15, acceptable>=5
PRICE_MOM_THRESHOLDS     = (30.0, 0.0)  # 12-1m price return % : strong>=30, acceptable>=0 (Jun-26 backtest; old 3m bands were (10,0))
# Revision-journey stage (PEAD/revision drift decays late): classify WHERE a rising +1y estimate sits in
# its upgrade cycle from the eps_trend trajectory. Igniting/Accelerating=runway 2, Sustained=1, Maturing/
# Rolling-over=0. Carried as a field + review timing context; added to F only when this flag is on (post-shadow).
REVISION_RUNWAY_IN_F     = True

# Energy valuation parity — REMOVED 26-Jul-2026 with Path C (see ISA_PathC_Energy_Assessment_Jul2026.md
# and ISA_PathC_Energy_Calibration_Study_Jul2026.md). The flag's own change deleted the 52-week-position
# metric, which the calibration study then found to be the single strongest signal in the energy universe
# (IC +0.262, 5/5 stability). Energy names are now screened by Path A, whose forward axis already carries
# price momentum and whose Source Score weights quality at 0.05 — the weighting the energy data supports.
FORWARD_AXIS_IN_RANKING  = True        # when True, Source Score (rerank) ranks on F — activate after shadow

# H2/H3 debug fix (redesign §7.5 / §13.1). When FORWARD_ELIGIBILITY is on, selection gates on a
# VIABILITY floor (Part A >= GROWTH_PART_A_STRONG, path-aware) + forward eligibility (eps_trend positive
# OR confirmed catalyst) instead of the fixed ns>=70 quality-TOTAL gate — so forward-confirmed lower-total
# names (UNH/ORCL/RR.L) are admitted and ranked by Source Score rather than pre-filtered out; and held
# positions are scored on the SAME Source Score as candidates. DEFAULT off (activate in S5 alongside
# FORWARD_AXIS_IN_RANKING + SUMMARY_COUNT_BASED so the whole forward-led path is consistent).
FORWARD_ELIGIBILITY      = True
# Forward-eligibility VIABILITY floor on Part A — DISTINCT from GROWTH_PART_A_STRONG (22, the "Strong
# Growth" classification used elsewhere, left unchanged). Raj: 21 = a clean 75% of /28. Energy kept at
# 14 (its Strong line is only 70% of /20, so a 75% floor would exceed Strong — revisit in shadow).
FORWARD_ELIG_PART_A_FLOOR         = 10    # growth VIABILITY floor = bottom of "Acceptable" (/28). Was 22->21;
                                          # lowered to 14 (redesign §8 viability-not-quality) so forward-confirmed
                                          # reversals (e.g. ORCL scored Part A=14) are NOT pre-excluded — the
                                          # Source Score + count-cap do the selection. 22 stays the "Strong" label.
# FORWARD_ELIG_PART_A_FLOOR_ENERGY — REMOVED 26-Jul-2026 with Path C. It was dead code in any case:
# rerank_watchlist eliminated energy-pipeline names one branch earlier on `ns is None`, so the
# energy-aware floor could never be reached (ISA_PathC_Energy_Assessment_Jul2026.md §3.2).

# Source Score weights (redesign Part 3 §13) — the FORWARD-LED ranking composite in rerank_watchlist.
# F dominant, quality de-emphasised, cheapness earns no separate credit. PROVISIONAL — calibrate in
# shadow. Used ONLY when FORWARD_AXIS_IN_RANKING=True (else rerank runs its legacy deployment composite).
# Jul-26 Part 1: THE single Source-Score weight dict (used by source_score.compute_source_score,
# inherited by build_excel / build_email / rerank_watchlist / screener_core overlay). forward 0.60 /
# revisions 0.15 / deployability 0.10 / quality 0.05 / analyst 0.10 (higher-end, per Raj's call).
# FROZEN 12-Jul-2026 (Fix Pack A8 / decision D5, Raj-approved) - SOURCE_WEIGHTS and
# FORWARD_AXIS_BUCKET_WEIGHTS may change ONLY via the pre-registered calibration rule below.
# *** OVERRIDE 29-Jul-2026 (Raj, WP-M): FORWARD_AXIS_BUCKET_WEIGHTS changed OUTSIDE the
# *** pre-registered rule. Justification: the rule cannot fire before ~late Sep 2026 (it needs
# *** CALIBRATION_MIN_MATURED_3M matured 3m observations and score_panel.csv only begins
# *** 25-Jun-2026), while the live setting contradicted the Jun-26 study that introduced it.
# *** This is a REVERSION TO PRE-REGISTERED GUIDANCE, not a new judgment recalibration.
# *** SOURCE_WEIGHTS itself is UNCHANGED and remains frozen.
# *** Evidence: ISA_Momentum_Diagnostic_PCTY_MU_Jul2026.md (§3, §7).
# No judgment recalibrations. These weights are UNVALIDATED PRIORS until score_panel matures.
# PRE-REGISTERED RULE (evaluate quarterly once >= 200 matured 3m observations exist in
# score_panel.csv, via calibration_report.py):
#   (1) if IC_3m(forward_axis_score) < 0.03 -> forward 0.60->0.40; +0.10 revisions,
#       +0.05 quality, +0.05 deployability.
#   (2) if IC_3m(revisions_score) >= 2x IC_3m(score_f_price_mom) -> FORWARD_AXIS_BUCKET_WEIGHTS
#       price 0.70->0.40, margin 0.30->0.60 (estimate signals already live in revisions_score).
#   (3) any change: calibration changelog entry + one shadow cycle before live.
# Gate for the pre-registered rule above, read by calibration_report.py (no magic numbers).
CALIBRATION_MIN_MATURED_3M = 200

SOURCE_WEIGHTS = {"forward": 0.60, "revisions": 0.15, "deployability": 0.10, "quality": 0.05, "analyst": 0.10}

# --- VCI forward-led (Jul-2026) — VCI_Forward_Led_Framework_Implementation_Jul2026.md -------
VCI_FV_ASYMMETRY_MIN_PLATFORM = 2.0     # §9.1 tiered floor — platform / multi-shot names
VCI_FV_ASYMMETRY_MIN_SINGLE   = 2.5     # §9.1 tiered floor — single-asset / true-cliff names
VCI_DEPLOY_THRESHOLD          = 75      # ACS quality floor (78 Exception Track handled by caller)
VCI_MGMT_PENALTY              = 5.0     # F1
VCI_STARTER_SIZE_PCT          = 1.0     # §9.2 full starter at ACS>=80
VCI_STARTER_SIZE_PCT_MID      = 0.75    # §9.2 ACS 75-79
VCI_HIGH_ACS                  = 80      # §9.2 "high" threshold for full 1.0%
VCI_EXCEPTIONAL_SIZE_PCT      = 1.5     # §9.2 cap
VCI_SOURCE_WEIGHTS = {"asymmetry": 0.30, "quality": 0.15, "catalyst": 0.25, "signals": 0.15, "revisions": 0.15}  # v2 (E8/E6): quality 0.30->0.15, revisions added; advisory/uncalibrated
# ============================================================================================
# VCI v2 ENHANCEMENT PACK (Jul-2026) — VCI_Framework_Enhancements_Implementation_Jul2026.md
# All default to FWDVCI-equivalent behaviour until each flag is flipped at the P6 calibration step.
# ============================================================================================
# E1 — probability-weighted floor (p·L), horizon-aware hurdle
VCI_FLOOR_MODE             = "derived" # FLIPPED LIVE 6-Jul-2026 (Raj) — probability-weighted floor active; rollback: "fixed"
# VCI_REQUIRED_ANNUAL_RETURN: P3 (18-Jul-26) — now DERIVED from the A19 anchor below (was 0.14 hardcoded)
VCI_FLOOR_MAX              = 4.0       # applied_floor = clamp(max(A_min, fixed tier), fixed, 4.0)
# p_thesis / L priors live in vci_base_rates.json (authoritative, sourced); these are inert fallbacks:
VCI_P_THESIS_PRIORS        = {"platform/_default": 0.50, "single_asset/_default": 0.35}
VCI_L_PRIORS               = {"platform": 0.35, "single_asset": 0.60}
# E2 — bottleneck-FV hardening. FLIPPED LIVE 6-Jul-2026 (Raj). Structured §10.2 fv_inputs flow from
# vci_fv_inputs.json (loaded by the VCI run + sync); a name WITHOUT structured inputs correctly
# falls to manual-confirm (intended discipline). Eligibility now uses the conservative P25 asymmetry
# (quadrature CI, ~23% haircut). Rollback: VCI_FV_REQUIRE_STRUCTURED=False, VCI_ASYM_ELIG_PCTILE="p50".
VCI_FV_REQUIRE_STRUCTURED  = True     # LIVE (rollback: False)
VCI_FV_CROSSCHECK_MAXDEV   = 0.40
VCI_FV_CI_DELTAS           = {"capture_share": 0.30, "exit_multiple": 0.25}   # per-input 1-SIGMA fractional uncertainty
VCI_FV_CI_Z                = 0.6745   # z-score for the P25/P75 percentile (combined in quadrature; softer than the old both-worst-case rule)
VCI_ASYM_ELIG_PCTILE       = "p25"    # LIVE — conservative P25 eligibility (rollback: "p50")
# E4 — sleeve-level binary risk budget (replaces the count cap as primary control)
VCI_SLEEVE_BINARY_RISK_BUDGET = 1.5   # % ISA expected-loss across open+proposed binaries (None disables)
VCI_BINARY_CORR_RIDER         = 1.5   # shared catalyst-domain risk inflation
# VCI_BINARY_MAX_CONCURRENT: E4's original design loosened this to 3 ("budget is primary, count cap
# secondary"). Superseded 31-Jul-2026 by the prose/code reconciliation (Run_Context_VCI_Task.md §9.4,
# Run_Context_Monthly_ISA_Review.md Checkpoint D), which re-tightened it to 2 for an unrelated reason
# (Step 8 Sub-decision B policy). That later, more specific value is authoritative; single assignment
# now lives below with §9.4 so there is one source of truth, not two silently disagreeing ones.
# E5 — liquidity-aware eligibility & sizing. FLIPPED LIVE 6-Jul-2026 (Raj). Min-ADV gate armed;
# inert until adv_usd is supplied to evaluate_candidate (None -> gate skipped), so it bites only once
# ADV data flows. Rollback: 0.
VCI_MIN_ADV_USD    = 1_000_000         # LIVE — below -> manual (rollback: 0)
VCI_MAX_PCT_ADV    = 0.10             # position value <= 10% of ADV
VCI_MAX_SPREAD_BPS = 100
# E7 — asymmetry-compression cause split
VCI_FV_EROSION_THRESHOLD = 0.15       # FV revised down >15% run-over-run = thesis erosion (not harvest)
VCI_RANK_MODE                 = "advisory"          # §11.6
VCI_BINARY_MAX_CONCURRENT     = 2                    # §9.4 — authoritative (31-Jul-2026 reconciliation; see comment above)
VCI_BINARY_CORRELATION_RIDER  = True                 # §9.4
VCI_ENTRY_LEVEL_DISPLAY_ONLY  = True                 # §8 rollback flag
import os as _os  # stdlib-only; keeps the "no heavy imports" guarantee
# §13 — the learning module writes vci_calibration_state.json beside the scripts; vci_source_score
# .load_weights() reads it and switches to calibrated weights ONLY once calibration_gate_passed.
# Until the file exists the getattr-default (None) path is inert, so this is safe to set now.
VCI_CALIBRATION_STATE_PATH    = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                              "vci_calibration_state.json")
VCI_LEARNING_STORE_PATH       = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                              "vci_learning_store.json")
VCI_CALIBRATION_CHANGELOG_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                              "vci_calibration_changelog.json")

# SUMMARY tab selection (forward-led). Legacy fixed-total v27 rule retired (source_score.summary_eligible
# & est-rev not deteriorating & Part B>=14). When True: floor-based selection via
# source_score.select_summary (Fix Pack A1 — fixed top-30 RETIRED 12-Jul-26; count-backfill admitted
# weak names in thin tapes and certified different quality per universe).
SUMMARY_COUNT_BASED    = True

# ============================================================================================
# FIX PACK Jul-2026 (Doc A: ISA_ChangeSpec_FixPack_Implementation_Jul2026.md) — D1-D8 approved
# 12-Jul-2026. P2 gate flags ship False (shadow-before-blocking, invariant 1) — flip at 1-Aug.
# ============================================================================================
SUMMARY_MAX_COUNT   = 40      # A1/D4 (replaces the retired fixed-30 count — floor selects, cap only truncates)
SUMMARY_MIN_WARN    = 10      # A1/D4 — SUMMARY_THIN_WARNING to RUN_QA/retro/email below this
UNIFIED_SOURCE      = True    # A6 — ONE Source Score, screen = deploy. P3 (18-Jul-26): the
                              #      legacy proxy path is DELETED; flag is INERT (P1 assert only)
SOURCE_UPSIDE_CAP   = 0.60    # A6 — upside normalisation cap in the deployability term
                              #      (was rerank_watchlist.UPSIDE_CAP; one home now)
CONSENSUS_UPSIDE_CAP_MULT = 1.15  # A6 — composite FV <= consensus target x this (was getattr-only)
ER_RERATE_CAP       = 0.10    # A2 — per-year multiple-drift clamp in expected_return.py
# ⚑ ADDED 06-Aug-2026. The C1 build record and `expected_return`'s own docstring both state the
# change is "reversible via ER_RERATE_MODE='legacy'" — and the constant did not exist here.
# `expected_return` read it with a getattr default, so the code worked and the ESCAPE HATCH DID
# NOT: setting it anywhere would have had no effect, and nothing checked. A documented rollback
# that is not wired is the same class of defect as a stored value that says one thing and is
# another. `test_session_02aug2026` has been failing on exactly this assertion.
ER_RERATE_MODE      = "regime_aware"   # "regime_aware" (C1, live) | "legacy" (raw monotonic)
ER_RERATE_NEUTRAL_BAND = 0.05          # C1 — deciles 2-7 measured -2 to -3%: scored ZERO, not
                                       #      given a confident penalty
ER_RERATE_REGIME_DAMPING = {           # C1 — DE-RATE side only; a cheap name is never damped
    "RISK_ON": 0.25, "LATE_CYCLE": 0.50, "RISK_OFF": 1.00, "RECOVERY": 1.00,
}                                      # an UNKNOWN regime is UNDAMPED — conservative, not a guess
# ── D-24 (09-Aug-2026) sector-declared multiple selection ─────────────────────────
# THE DEFECT: the E[r] row adapter resolved `current_multiple` from `trailing_pe` first (8.7% of
# SP500 rows) and the anchor from `val_hist_pe_anchor` (8.3%). A missing term contributed 0, so on
# 92% of every universe screened E[r] silently ASSERTED that the multiple would not change, and the
# fundamentals evidence route was unreachable (confidence ceiling 0.70 vs EVIDENCE_ER_CONF_MIN 0.75).
#
# DECLARED mapping, not fitted. One home. Values are yfinance `sector` strings. No parameter below
# is estimated from the data, so this adds no degrees of freedom.
ER_MULTIPLE_BY_SECTOR = {
    "Technology":             "fwd_pe",
    "Healthcare":             "fwd_pe",
    "Consumer Cyclical":      "fwd_pe",
    "Consumer Defensive":     "fwd_pe",
    "Communication Services": "fwd_pe",
    "Financial Services":     "fwd_pe",     # P/B would be better; not populated. Declared limitation.
    "Industrials":            "ev_ebitda",  # capital-intensive
    "Utilities":              "ev_ebitda",
    "Energy":                 "ev_ebitda",
    "Basic Materials":        "ev_ebitda",
}
ER_MULTIPLE_DEFAULT      = "fwd_pe"
ER_MULTIPLE_FALLBACK     = "price_fcf"   # used only if the sector's chosen multiple is absent
ER_ANCHOR_MIN_SECTOR_N   = 5             # below this, no sector median is formed
ER_ANCHOR_MIN_ROWS       = 30            # ⚑ below this the WHOLE TABLE is not fit to anchor with.
                                         # Found live 09-Aug-2026: a 6-ticker ad-hoc screener_local
                                         # run built a table with ZERO sector medians and would
                                         # have become the `latest` pointer the monthly pre-run
                                         # reads — §3.3's trap re-entering through the back door.
                                         # An unfit table is still PERSISTED (it is a record of
                                         # what ran) but never becomes the pointer, and never
                                         # anchors anything.
ER_ANCHOR_AGREE_BAND     = 0.25          # |xs/own - 1| above this = DIVERGENCE, published
ER_ANCHOR_MODE           = "cross_sectional_primary"  # | "own_history_primary" | "own_history_only"
                                         # ROLLBACK (§11): "own_history_only" restores pre-D-24
                                         # behaviour EXACTLY. T8 asserts the 152/312 pass count.
ER_XS_CONF_WEIGHT        = 0.20          # re-rate confidence credit from a cross-sectional anchor
ER_OWN_CONF_WEIGHT       = 0.30          # ...from an own-history anchor (unchanged)
# Sanity band per multiple family, applied BEFORE a median is taken. Count/publish what it removes.
ER_MULTIPLE_SANITY = {"fwd_pe": (0.0, 200.0), "trailing_pe": (0.0, 200.0),
                      "val_hist_current_pe": (0.0, 200.0), "current_pe": (0.0, 200.0),
                      "price_fcf": (0.0, 200.0), "ev_ebitda": (0.0, 60.0)}
# §6 — share of rows refusing to measure the re-rate. Above these, something upstream has broken.
ER_UNMEASURED_WARN_SHARE = 0.10
ER_UNMEASURED_FAIL_SHARE = 0.25
ER_ANCHOR_STORE   = "er_anchor_store.json"     # persisted beside the scripts (like score_panel.csv)
ER_LEARNING_STORE = "er_anchor_learning.csv"   # L-10 — per-row anchor evidence; ships with Stage 1

# ⛑ §1.3 THE MANIFEST. `expected_return`'s docstring claimed TWO consumers; there are NINE live
# call sites, and every one called expected_return_for_row(row) with no context argument — so an
# anchor-table parameter added to one of them leaves the other eight silently running the old,
# defective behaviour. test_d24_expected_return.py asserts that the set of modules importing
# `expected_return` EQUALS this manifest: a tenth caller added later fails the battery until it is
# registered. This is orchestrator_parity.py applied to a CONTRACT SURFACE rather than a code path.
ER_CALLSITE_MANIFEST = {
    "screener_core.py":           "screen — BUILDS the anchor table and passes it",
    "screener_local.py":          "screen (LIVE path) — builds it too; the copy of run_scheduled that never calls it",
    "rerank_watchlist.py":        "pre-run — READS persisted; must not recompute",
    "fetch_watchlist_metrics.py": "pre-run — READS persisted; must not recompute",
    "step9_pre_builder.py":       "pre-run fallback compute — READS persisted",
    "build_email.py":             "monthly review email — READS persisted, else er_status=unmeasured",
    "build_excel.py":             "screen workbook — READS persisted",
    "return_architecture.py":     "Section A/B/C — READS persisted, else er_status=unmeasured",
    # Not a computer of E[r], but a declared importer nonetheless:
    "orchestrator_parity.py":     "OBSERVER — runs the §6 reachability assertion; computes nothing",
    "test_session_02aug2026.py":  "root-level session test (C1 re-rate shape) — fixtures only",
}
# ⚑ THREE MODULES THE SPEC LISTED THAT DO **NOT** IMPORT `expected_return`, verified by AST
# 09-Aug-2026 — and the manifest records the truth, not the spec:
#   • t1_gates.py — CONSUMES the stamped fields (expected_return_12_24m, er_confidence, er_status)
#     and never computes E[r]. That is the correct arrangement: one implementation, one consumer
#     boundary. It is listed in the D-24 spec's §1.4 files-touched table because its er_status
#     handling changed, not because it calls the module.
#   • Dashboard/server/cache.py and Dashboard/server/decision_table.py — RENDER
#     `expected_return_12_24m` off the frame. Render-only was the spec's open question at row 9
#     of §1; CONFIRMED at build time. If a future change adds the import, the manifest check
#     below fails until someone decides what anchor table the dashboard should use.

ER_GATE_ACTIVE      = True    # A2 — E[r] T1-deploy gate; FLIPPED LIVE 13-Jul-26 (P2) — consumed 1-Aug pre-run
STAGE_GATE_ACTIVE   = True    # A3 — stage gate; FLIPPED LIVE 13-Jul-26 (P2)
T1_QUALIFICATION_MODE = True  # A4 — T1 = QUALIFICATION; FLIPPED LIVE 13-Jul-26 (P2); False = legacy rank-band rollback
# ── A5 v3 (Raj 15-Jul-26, D18/D19 APPROVED): TENURE GATE REMOVED — sizing by conviction ×
# evidence. Tenure/discovery-date carries no information about the company; the evidence is the
# underlying data (both-window revisions = confirmation-over-time that already happened). Sizing
# NEVER blocks a deploy; it caps it. Full size additionally requires Step-10 conviction >= 75.
EVIDENCE_ER_CONF_MIN = 0.75   # D18 — er_confidence floor for the fundamentals evidence route
EVIDENCE_SIGHTING_MIN = 2     # A5v3 — alternative route: distinct screen sightings...
EVIDENCE_SIGHTING_GAP_DAYS = 7      # D19 — ...spaced at least this far apart...
EVIDENCE_SIGHTING_WINDOW_DAYS = 45  # ...within this lookback (source: score_panel.csv, A8)
STARTER_SIZE_CAP_PCT = 1.5    # D19 — cap for thin-evidence entries; scale-up trigger recorded
                              #      at entry. PRE-REGISTERED calibration rule (A8 pattern):
                              #      if first-sighting FULL entries underperform confirmed
                              #      entries at 3m over >=2 quarters of ledger data, raise
                              #      EVIDENCE_ER_CONF_MIN — never judgment-recalibrated.
PERSISTENCE_MIN_CYCLES = 2    # A5 — RETIRED AS A GATE (v3, 15-Jul-26); cycles_seen is still
                              #      STAMPED by update_watchlist as ledger/calibration data
LATE_CYCLE_MULT_PCTILE = 90   # A15 — extended-multiple buy-guard percentile (spec basis)
LATE_CYCLE_PREMIUM_PCT = 35.0 # A15 — OPERATIVE proxy: val_hist premium-vs-own-3yr-avg (%) that
                              #      stands in for the 90th-pct multiple until a percentile
                              #      series exists (t1_gates.late_cycle_flag documents the basis)
CATALYST_MAX_DAYS   = 90      # A2/A5/D3 — named-catalyst override window (days)
# A11/D8 + A19: Section-A verdict bands are ANCHOR OFFSETS, not hardcodes (invariant 6).
# At the current 13.9 derivation these evaluate to pass=13.0 / inconclusive=11.0 (D8's numbers);
# they move when the anchor moves. Mapping (A12): fund gate = required return minus the margin
# the stock sleeve is expected to contribute (documented in project_isa_target_weights.md §1).
FUND_GATE_PASS_OFFSET_PP        = -0.9
FUND_GATE_INCONCLUSIVE_OFFSET_PP = -2.9
SLEEVE_PROBATION_PP = 5.0     # A14/D6 — sleeve-vs-VUAG probation threshold
# ── A19 central required-return anchor (Raj 12-Jul: "everything needs to start being anchored
# to this"). ONE derived hurdle in target_state.json (derive_required_return.py; re-derived each
# April pre-run + on any contribution-schedule change). Loaded at import with a HARD FALLBACK +
# loud warning — a missing/corrupt anchor file must never stop a screen, but must never be silent.
def _load_target_state():
    import json as _json
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "target_state.json")
    try:
        with open(_p, encoding="utf-8") as _f:
            return _json.load(_f)
    except Exception as _e:
        import sys as _sys
        print(f"WARNING scoring_config: target_state.json unreadable ({_e}) — using FROZEN "
              f"12-Jul-2026 fallback anchor 13.9/18.7 (A19). Fix the anchor file.", file=_sys.stderr)
        return {"required_return_floor_pct": 13.9, "required_return_stretch_pct": 18.7,
                "required_return_operative_pct": 13.9, "guardrail_state": "FALLBACK"}

TARGET_STATE        = _load_target_state()
REQUIRED_RETURN_MID = float(TARGET_STATE["required_return_operative_pct"])   # 13.9 at current derivation
VCI_REQUIRED_ANNUAL_RETURN = round(REQUIRED_RETURN_MID / 100.0, 4)  # P3/A19 (18-Jul-26): E1 hurdle h
                              # reads THE anchor — VCI/PathA symmetric (invariant 6; was 0.14 hardcoded)
ER_FRICTION_BUFFER  = 2.0     # A2/D1 — pp over the A19 anchor (friction + FX + estimation)
ER_DEPLOY_FLOOR     = REQUIRED_RETURN_MID + ER_FRICTION_BUFFER   # A2/D1 DERIVED (≈15.9 today) —
                              # consumed only when ER_GATE_ACTIVE flips True at P2; never hardcode.
FUND_GATE_BANDS     = {"pass": round(REQUIRED_RETURN_MID + FUND_GATE_PASS_OFFSET_PP, 1),
                       "inconclusive": round(REQUIRED_RETURN_MID + FUND_GATE_INCONCLUSIVE_OFFSET_PP, 1)}
                              # A11/D8/A19 — DERIVED bands (13.0/11.0 at today's anchor)
FUND_GATE_PCT       = round(REQUIRED_RETURN_MID - 1.9, 1)   # legacy 12.0 line, now anchor-derived
                              # (fund_returns.compute_fund_gate default; D8 bands govern the verdict)

# ── Doc B (New Capabilities) P2 constants — B1/B2/B3/B4/B7 ─────────────────────────────────
DRAWDOWN_PROTOCOL_ACTIVE = True          # B1 — rollback: False (state file retained)
DRAWDOWN_TRANCHES   = [10, 20, 30]       # B1/D10 — % below 252d high; 1/(remaining) of reserve each
DRAWDOWN_LOOKBACK   = 252                # B1/D9 — trailing-high window (VUAG GBP daily close)
DRAWDOWN_BUFFER_GBP = 500.0              # B1/D11 — cash buffer excluded from the reserve
MMF_SWEEP_MIN_GBP   = 1500.0             # B2/D14 — idle cash >= this AND no committed action
MMF_SWEEP_IDLE_DAYS = 10                 #          within 10 trading days -> mechanical SWEEP line
CASH_EQUIVALENT_TICKERS = ["CSH2.L", "ROYAL LONDON SHORT TERM MONEY MARKET"]  # B2 RESOLVED (Raj 18-Jul-26: money-market fund).
# CSH2.L = Amundi Smart Overnight Return GBP UCITS ETF (LSE ticker -> classify_holding matches
# mechanically; OCF ~0.07%, AUM >>500m, on AJ Bell, intraday, preclearance-exempt UCITS).
# Name entry covers the OEIC alternative (Royal London STMM Y Acc) if ever held/exported by name.
# Swap = edit this list only; sweep rule (D14) reads _ceq[0] as the instrument.
                                         #      (spec: GBP MMF, OCF<=0.15%, AUM>=£500m, on AJ Bell).
                                         #      Rollback: empty list. Counts as CASH everywhere.
FACTOR_AI_SOFT_CAP_PCT = 30.0            # B3/D15 — AI-complex effective look-through soft cap
FACTOR_CAP_ENFORCE  = True               # B3 — breach blocks factor-raising BUYs (Checkpoint-D);
                                         #      rollback: False = report-only
ETF_TACTICAL_MAX_POS_PCT   = 5.0         # B4/D16 — per-ETF cap (% ISA)
ETF_TACTICAL_MAX_TOTAL_PCT = 10.0        # B4/D16 — total tactical cap (% ISA)
ETF_TACTICAL_MIN_HOLD_MONTHS = 3         # B4/D16 — anti-churn
# — Review Pack 18-Jul-26 (Fable5_Email_Excel_Review_18Jul2026.md items 4/7/8 + B7 shadow) —
CAPITAL_SIGNAL_CONFLICT_PP = 25.0  # item 8: |E[r] %pa - annualised FV-implied %pa| above -> conflict
CONFLICT_ER_CONF_CAP = 0.5         # conflict caps er_confidence (below A5 v3's 0.75 full-size bar)
REGIME_OPEN_DOORS = {              # B7(2): doors open per regime (momentum never closes)
    "RISK_ON": ["momentum"], "LATE_CYCLE": ["momentum"],
    "RISK_OFF": ["momentum", "quality"], "RECOVERY": ["momentum", "inflection"],
}
DOOR_QUALITY_PART_A_MIN = 20       # B7 quality-stability door (Doc B spec)
DOOR_QUALITY_ND_EBITDA_MAX = 1.5
DOOR_QUALITY_FCF_YEARS_MIN = 4   # WP-D 29-Jul-26: was 5 — UNREACHABLE. fcf_positive_years is
#   computed over a 5-year window whose observed MAXIMUM across the whole 423-name NASDAQ universe
#   is 4 (264 names at 4, 78 at 3, ZERO at 5). The door could never open on this criterion alone.
DOOR_QUALITY_DIV_PAYOUT_FCF_MAX = 0.8  # dividend covered; no dividend (None) = covered
DOOR_INFLECTION_PART_A_MIN = 16    # B7 inflection door
DOOR_INFLECTION_OFF_HIGH_MIN_PCT = 25.0
# B7 shadow proxies (documented): beta<1 criterion WAIVED (beta not in full_data);
# inflection revisions second-derivative proxied by est_rev_direction == improving.
# — B4 Category-8 tactical UCITS-ETF layer (P3 hook 18-Jul-26; Doc B §B4, D16 caps) —
ETF_TACTICAL_MAX_POSITION_PCT = 5.0   # per position, % of TOTAL ISA (D17)
ETF_TACTICAL_MAX_TOTAL_PCT    = 10.0  # all Category-8 positions combined
ETF_TACTICAL_MIN_HOLD_MONTHS  = 3     # anti-churn
REGIME_B4_MENU = {                    # B7(3): regime -> permitted tactical tilts (selection=JUDGMENT;
    "RISK_ON": [],                    #   RISK_ON: none without documented cause)
    "LATE_CYCLE": ["min_vol", "quality"],
    "RISK_OFF": ["hold_existing"],
    "RECOVERY": ["equal_weight", "value"],
}
# — B6 glidepath trigger constants (18-Jul-26; design note: glidepath_design.md) —
GLIDEPATH_AGE_TRIGGER = 56
GLIDEPATH_VALUE_TRIGGER_GBP = 700_000
RAJ_DOB_YM = (1977, 12)         # CONFIRMED by Raj 18-Jul-26 (born Dec-1977) — month-precise age for B6
REGIME_DOORS_ACTIVE = False              # B7 — doors admit for real at P3 (Sep screens); shadow first
REGIME_RULES = {                         # B7(1) — pure decision table: (vs-200dma, dd-band, 63d slope)
    ("above", 0, "+"): "RISK_ON",    ("above", 0, "-"): "LATE_CYCLE",
    ("above", 1, "+"): "RECOVERY",   ("above", 1, "-"): "LATE_CYCLE",
    ("above", 2, "+"): "RECOVERY",   ("above", 2, "-"): "RISK_OFF",
    ("below", 0, "+"): "LATE_CYCLE", ("below", 0, "-"): "RISK_OFF",
    ("below", 1, "+"): "RECOVERY",   ("below", 1, "-"): "RISK_OFF",
    ("below", 2, "+"): "RECOVERY",   ("below", 2, "-"): "RISK_OFF",
}                                        # dd bands: 0 = >-5%, 1 = -5..-15%, 2 = <=-15%

# ===========================================================================
# ENERGY (Path C) — RETIRED 26-Jul-2026. Raj decision: absorb energy names into Path A.
# The scorecard had no measurable predictive power (Part A IC +0.029 pooled, -0.005 on a
# 2025 holdout, bands inverted) and all three of its differentiated rules were contradicted
# by 627 point-in-time observations across 210 names in 8 regions. Energy/utilities names are
# now screened by the weekly index runs on Path A; pre-scale binaries route to Path B.
# Evidence: ISA_PathC_Energy_Calibration_Study_Jul2026.md
# ===========================================================================

# ===========================================================================
# PRELIMINARY conviction brackets — DISPLAY ONLY (Claude refines to /100 at Step 9).
# Expressed as FRACTIONS of each path's TOTAL_MAX so they auto-scale if the max changes.
# ===========================================================================
CONVICTION_FRACTIONS = [
    (0.92, "High Conviction",   "high",   "[Claude: refine to /100 at Step 9]"),
    (0.82, "Medium Conviction", "medium", "[Claude: refine to /100 at Step 9]"),
    (0.70, "Watch but Wait",    "low",    "[Claude: refine to /100 at Step 9]"),
    (0.00, "No Action",         "low",    "[Claude: refine to /100 at Step 9]"),
]

def _brackets(total_max: int):
    """Build [(threshold, label, level, note), ...] from fractions of total_max."""
    return [(round(f * total_max), lbl, lvl, note) for f, lbl, lvl, note in CONVICTION_FRACTIONS]

def conviction_brackets(total_max):
    """Preliminary-bracket list for any per-stock max (50 base / 54 semi-hardware)."""
    return _brackets(total_max)

GROWTH_CONVICTION_BRACKETS = _brackets(GROWTH_TOTAL_MAX)   # 46 / 41 / 35 on /50

# ===========================================================================
# FUND SLEEVE — return sourcing + 12% gate (redesign retro #5 G1/G2). The fund-sleeve weighted-average
# return gate was left as "pending" each month (est_return_pct=None) for manual Morningstar lookup.
# When FUND_RETURN_SOURCING is on, fund_returns.py sources returns (yfinance for ticker-able funds +
# a quarterly cache for OEICs), computes the REAL value-weighted sleeve return + PASS/FAIL vs FUND_GATE_PCT,
# and emits fund actions for the agenda. DEFAULT off (additive; analytics unchanged until activated).
# ===========================================================================
FUND_RETURN_SOURCING   = True
FUND_GATE_PCT          = 12.0
FUND_RETURN_STALE_DAYS = 92      # cached fund return older than this -> stale, re-source (quarterly)
FUND_MIN_COVERAGE      = 0.80    # need >= this fraction of fund-sleeve value covered to PASS/FAIL (else pending)

# ===========================================================================
# ACTION STACK (redesign Part3 §13.3-13.6 / CONTRACTS #6) — the Global Action Stack.
# rerank computes the Source Score; the action stack turns scores into ONE ranked agenda of
# BUY / STARTER / TOP_UP / TRIM / SELL across candidates AND held positions, applying disqualifier
# CAPS before ranking, an Action Priority Score (APS) that makes different actions comparable, and a
# reallocation (replacement) test linking a sell to the buy it would fund. HOLD/WATCH are context
# (not in the stack). DEFAULT off — emits an additive action_stack_[mmm].json; pre-run unchanged
# until activated (flip True or pass rerank --action-stack).
# ===========================================================================
BUILD_ACTION_STACK     = True
APS_FRESH_CAPITAL_BAR  = 65.0   # Source Score >= bar -> eligible for fresh capital (BUY / TOP-UP)
APS_HOLD_FLOOR         = 50.0   # held name Source < floor -> TRIM / SELL-review
APS_TOPUP_PENALTY      = 12.0   # TOP-UP APS = Source - penalty (§13.5 — prevents averaging down)
APS_MANDATORY_SELL     = 95.0   # disqualifier/thesis-break SELL -> high fixed APS (capital protection first)
APS_TOP_N              = 10     # stack = top N by APS + ALL mandatory (tier M) actions
REPLACEMENT_RETURN_PP  = 10.0   # replacement test: +10pp Source (return proxy)
REPLACEMENT_BUYABILITY = 15.0   # replacement test: +15pp upside-to-FV (buyability)
APS_REALLOC_BONUS      = 10.0   # opportunity-cost bonus added to a TRIM's APS when replacement test passes

# ===========================================================================
# FLUID CANDIDATE POOL — decay/turnover (redesign Part3 §4 Layer4 / §13; CONTRACTS candidate-pool).
# The pool + watchlist must turn over month-to-month, DRIVEN by the scheduled screen outputs:
# stocks flow IN when freshly screened and OUT when they stop appearing — nothing may squat a slot.
# When FLUID_POOL_DECAY is True, watchlist/pool entries carry first_seen / last_confirmed / decay_state,
# a name is RE-CONFIRMED whenever it reappears in this cycle's screens (decay reset), and a name absent
# from the screens AGES OUT after POOL_AGEOUT_MONTHS without re-confirmation (time-based — replaces the
# old score_history-LENGTH staleness, which could freeze a <3-history name on a stale score forever).
# Decay (not instant drop) protects Regime-3 reversal names that legitimately skip one month's screen.
# DEFAULT False = current carry-forward behaviour (4-Jul pre-run byte-for-byte unchanged); flip True
# (or pass --fluid-pool) to ACTIVATE the fluid pool.
# ===========================================================================
# COMMENT CORRECTION 12-Jul-2026 (Fix Pack A7a; BEHAVIOUR UNCHANGED): the decay/age-out
# semantics in the block comment above are OBSOLETE - superseded by the 04-Jul-2026
# purge-on-absence doctrine (membership = current cycle screens + held ONLY; absent names
# PURGED, never decayed). FLUID_POOL_DECAY=True now does ONE thing (update_watchlist.py
# Phase 7 ~L1029-1034): carries each name's first_seen forward across months. That memory
# is the substrate for the A5 persistence rule (cycles_seen >= 2 for T1-deploy). DO NOT set
# False - it would erase first_seen continuity. POOL_AGEOUT_MONTHS / POOL_DECAY_PENALTY are
# RETIRED (no live code path uses them for membership or ranking); retained only so legacy
# getattr callers never see a missing attribute. Do not consume in new code.
FLUID_POOL_DECAY      = True     # LIVE - first_seen carry ONLY (see correction above)
POOL_AGEOUT_MONTHS    = 3        # RETIRED 12-Jul-26 - do not consume
POOL_DECAY_PENALTY    = 5.0      # RETIRED 12-Jul-26 - do not consume

# ===========================================================================
# VCI  (vci_acs_scorer.py — ACS /100). Deployment thresholds
# Analyst rating buckets treated as a positive ("strong") signal. Centralised here so
# normalise_adapter (_cfg.STRONG_RATINGS) and fetch_watchlist_metrics share one source of
# truth — previously only defined locally in those modules, which left _cfg.STRONG_RATINGS
# undefined and broke the rerank membership-refresh under the activated forward path (S5).
STRONG_RATINGS = {"strongbuy", "strong buy", "buy"}

# S5 go-live: VCI F1-F4 final-layer gates ON (was getattr-default False)
VCI_FINAL_LAYER_GATES = True

# ── Forward Axis re-weighting (Jun-26) ─────────────────────────────────────────
# REVISION_RUNWAY_CAP: cap journey-stage runway at 1 unless est-rev direction is "Improving".
REVISION_RUNWAY_CAP        = True
# FORWARD_AXIS_BUCKETED: weight the forward axis by independent dimension (estimates / margin /
# price) instead of equal-per-signal, so the 4 correlated estimate-revision signals can't swamp
# price momentum. False => legacy equal-per-signal (kept only for backtest comparison).
FORWARD_AXIS_BUCKETED      = True
# Bucket weights. Equal (1/1/1) => price ~= 1/3 of the axis (above each individual analyst signal,
# but not dominant). To test price as a smaller timing overlay, lower "price" (e.g. 0.7).
# ---------------------------------------------------------------------------------------------
# WP-M (29-Jul-2026) — MOMENTUM REBALANCE. RAJ OVERRIDE of the 12-Jul freeze, evidence:
#   ISA_Momentum_Diagnostic_PCTY_MU_Jul2026.md + ISA_Momentum_Horizon_Study_Jul2026.md
# WHY: the Jul-26 Part 2 split set price=0.70, which made trailing price momentum 0.60*0.70 = 42%
# of the Source Score. That directly contradicted the pre-registered instruction in
# Forward_Axis_Backtest_Findings_Jun2026.md: "KEEP bucketed forward axis at equal 1/3
# (estimates / margin / price). Do not raise price above 1/3." Restoring the 1/3 split and putting
# the estimate signals back into the axis. Momentum now = 0.60 * 0.3333 = 20% of Source Score.
# ROLLBACK: set FORWARD_AXIS_BUCKET_WEIGHTS back to {"margin":0.30,"price":0.70} and
# PRICE_MOM_BLEND to {"long":1.0,"short":0.0} and PRICE_MOM_SCORING="bands".
# ---------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------
# WP-M7 (29-Jul-2026) — ESTIMATES WEIGHT DECIDED. Raj: "decide the estimates weight".
# Evidence: ISA_Estimates_Weight_Decision_Jul2026.md (1,176-row / 5-screener panel, permutation
# influence study). Registry entry WPM-7.
#
# THE DEFECT. The axis "estimates" bucket and `revisions_score` are not merely correlated, they are
# the SAME NUMBER: both are mean(score_f_eps_trend, score_f_rev_est, score_b_est_rev,
# revision_runway)/2. Verified identical on 1176/1176 rows. Under thirds the estimate signals
# therefore carried 0.60*(1/3) + 0.15 = 35% of the Source Score, against margin 20% and price 20%.
# Nobody chose 35% — it is the arithmetic of implementing the Jun-26 "equal thirds" instruction on
# top of the Jul-26 Part 2 split that had already pulled estimates OUT into their own 15% term.
#
# WHY NOT SIMPLY REMOVE THE DUPLICATE. Setting estimates=0 forces margin+price to 0.5/0.5, i.e.
# momentum back to 30% of Source — reversing the WP-M reweight delivered the same day on a
# 43k-stock-month panel. SOURCE_WEIGHTS is frozen, so `revisions`=0.15 cannot absorb it either.
# The duplication cannot be removed; it can only be SIZED. Constraints that bind:
#     price effective = 20%  (WP-M decision, evidence-backed, locked)
#     SOURCE_WEIGHTS frozen  =>  estimates_eff + margin_eff = 55% exactly
# Every point taken off estimates MUST go to margin. There is no third destination.
#
# THE DECISION: estimates_eff = margin_eff = 27.5%.  Solve 60e+15 = 60m subject to e+m = 2/3:
#     e = 5/24, m = 11/24, p = 8/24 (= 1/3).
# This is the correct translation of the Jun-26 "equal 1/3 estimates/margin/price" instruction into
# the post-split architecture: equal EFFECTIVE weight across the three forward dimensions, subject
# to the momentum lock — not equal RAW axis shares, which is what produced the 35%.
#
# WHY EQUAL AND NOT A VIEW. Neither estimates nor margin has a validated IC and neither CAN be
# validated before score_panel matures (~Sep-26 at the earliest; the bucket weights themselves are
# not reconstructable at all — see calibration_registry WPM-1). Equal weight is the minimum-judgment
# allocation under genuine ignorance. The measured alternatives were all worse on one of the two
# influence metrics: at 35/20 estimates permutation changes 4.05 of the top 5 (margin 1.95, price
# 2.40) — estimates dominates selection; at 20/35 margin's mean |rank shift| hits 52 vs 20/26 —
# margin dominates the cross-section. 27.5/27.5 is inside the balanced zone on both.
#
# MATERIALITY (measured, not asserted): SUMMARY pool 46 -> 48; the deployable TOP 5 is UNCHANGED in
# NASDAQ, SP500, STOXX600 and F250SPI, and swaps ranks 4-5 in MIDCAP400 only (FTI out, WTS in).
# This is a correctness fix to a weight nobody chose, not a change to what gets deployed.
#
# IT DOES NOT DEMOTE MU, and was not chosen to. MU holds rank 2 in NASDAQ and SP500 at every
# setting tested from 35% down to 20%. MU's problem is ROIC 13.6% vs WACC 16.0% — destroying value
# — and quality carries 5% in the FROZEN SOURCE_WEIGHTS. The quality weight is the MU lever; the
# estimates weight is not. Do not re-litigate this one via the axis.
#
# NEW DEFECT SURFACED, NOT FIXED HERE (registry WPM-8): score_f_margin_traj is missing for 20.7% of
# the universe and 27% of the selected pool. wsum renormalisation silently reallocates margin's
# weight to price+estimates for exactly those names, and the distortion SCALES with margin's weight
# (1.50x at thirds -> 1.85x here). Margin-missing names already score lower (41.6 vs 45.2 mean
# source). Deliberately left alone: one lever, one cycle. Do not raise margin further until fixed.
#
# ROLLBACK: FORWARD_AXIS_BUCKET_WEIGHTS = {"estimates": 1/3, "margin": 1/3, "price": 1/3}
# (WP-M as delivered 29-Jul); the pre-WP-M setting was {"margin": 0.30, "price": 0.70}.
# ---------------------------------------------------------------------------------------------
FORWARD_AXIS_BUCKET_WEIGHTS = {"estimates": 5/24, "margin": 11/24, "price": 1/3}

# Price-momentum windows. LONG = 12-1m (252-day window ending ~21 trading days ago) — the Jun-26
# backtest signal. SHORT = trailing 1 month (21d, no skip) — the window the LONG signal discards.
# Horizon study (29-Jul-26, 217 Nasdaq growth names, 44 monthly formation dates, rank-IC -> fwd 1m):
#     12-1m -0.005 (t -0.19) | 12m +0.000 | 6-1m -0.006 | 3m +0.012 | 1m +0.036 (t 1.74, 64% hit) | 5d -0.003
# 1m is the strongest horizon at the 1-month decision cadence and is exactly what SKIP removed.
# 5d was tested and REJECTED (t -0.16, microstructure noise) — do not add it.
# CAUTION: t=1.74 is below significance and contradicts the classic short-term-reversal literature;
# this universe is a survivorship-biased growth screen. Blend is deliberately modest and logged.
PRICE_MOM_LOOKBACK         = 252
PRICE_MOM_SKIP             = 21
PRICE_MOM_SHORT_LOOKBACK   = 21     # trailing 1 month
PRICE_MOM_SHORT_SKIP       = 0      # no skip — this IS the recent-month signal
PRICE_MOM_BLEND            = {"long": 0.50, "short": 0.50}
# CORRECTED 29-Jul-26 after the FULL cross-universe panel (978 names / 5 screeners / 45 formation
# dates / ~43k stock-months). The earlier 0.35/0.65 lean-short was fitted to NASDAQ ONLY and does
# NOT generalise: pooled 5y rank-IC -> fwd 1m is 12-1m +0.0254 vs 1m +0.0207 (i.e. the LONG window
# is marginally better pooled), and in STOXX600 12-1m (+0.0328) beats 1m (+0.0259) outright.
# What DOES generalise is the BLEND: highest t-stat of the three in the pooled panel on both
# windows (5y +0.0252 t 1.32; 4y +0.0430 t 2.06 — the only t>2 in the study). Blending beats
# picking a horizon. 50/50 is therefore the evidence-supported setting; do not lean either way
# without a pooled result that survives both windows.
# PER-UNIVERSE CAVEAT: F250SPI shows NO momentum edge (5y 12-1m -0.0077; 12-1m -> fwd 3m -0.0341
# t -1.82, 29% hit). Flagged for a per-universe weight review — see calibration_registry WPM-5.

# Momentum scoring mode. "percentile" = cross-sectional rank within the run (fixes the saturation
# that scored MU 2/2 at +753% AND at +40%, and PCTY 0/2 at -44.3% AND at -42.1% while improving).
# "bands" = legacy absolute PRICE_MOM_THRESHOLDS. Percentile needs the whole cross-section, so it is
# applied by screener_core.apply_cross_sectional_momentum() after per-ticker scoring.
PRICE_MOM_SCORING          = "percentile"
PRICE_MOM_PCTL_CUTS        = (0.667, 0.333)   # >=P67 -> 2 ; >=P33 -> 1 ; else 0

# Momentum state (long vs short disagreement) — the second-derivative read that was missing.
# PX_-prefixed so they can never be confused with `revision_stage` (ANALYST ESTIMATES), which has
# its own "Rolling over" value feeding SUMMARY_STAGE_EXCLUDE. Different measurements entirely.
# PX_RECOVERING    : long <= 0, short > 0   (PCTY 24-Jul: 12-1m -42.9%, 1m +19.5%)
# PX_ADVANCING     : long  > 0, short > 0
# PX_DETERIORATING : long  > 0, short <= 0  (MU   24-Jul: 12-1m +758.9%, 1m -12.2%)
# PX_DECLINING     : long <= 0, short <= 0
MOMENTUM_STATE_IN_AXIS     = False   # label is DIAGNOSTIC + routing only; it must not re-score
# Deployment-timing gate (WP-M2). Reconciliation study 29-Jul-26 showed BOTH momentum horizons have
# ~zero rank-IC against FORWARD 3-MONTH returns on 5y data (12-1m -0.000 t -0.00 on the June panel),
# while 1m retains weak FORWARD 1-MONTH power. Momentum is therefore an ENTRY-TIMING signal, not a
# SELECTION signal. SHADOW THIS CYCLE: recorded on every row, gates nothing. Flip to True after one
# clean cycle to block deployment into FALLING / ROLLING_OVER names.
MOMENTUM_TIMING_GATE_ACTIVE = False
MOMENTUM_TIMING_BLOCK_STATES = ("PX_DECLINING", "PX_DETERIORATING")
# EVIDENCE 29-Jul-26 (964 names / 5 screeners / 44 dates): gating SUMMARY on these states is NOT
# supported — excluding them earns +0.119pp/month (t +0.74, i.e. zero) while cutting candidate
# breadth 41%. The state label is a coarse SIGN split and discards the tail information the
# continuous percentile score keeps. It is therefore DIAGNOSTIC + a deployment BLOCK tag only,
# and must never gate SUMMARY membership. Mean fwd 1m by state: PX_ADVANCING +1.86 / PX_RECOVERING
# +1.45 / PX_DETERIORATING +1.56 / PX_DECLINING +1.44 / universe +1.57.
MOMENTUM_STATE_GATES_SUMMARY = False

# SUMMARY forward-runway gate (Jul-26): exclude estimate-cycle stages with no forward runway from the
# SUMMARY candidate pool (the pre-run deployment funnel). Igniting/Accelerating/Sustained/Early-unconfirmed
# (runway>=1) qualify; Maturing/Rolling over/Flat-Down/Marginal (runway 0/None) are excluded. Stage=None
# (missing estimate data) is NOT excluded (ranks low via forward axis). Price still RANKS the eligible names.
SUMMARY_STAGE_EXCLUDE = ["Maturing", "Rolling over", "Flat/Down", "Marginal"]

# SUMMARY source-score floor (Jul-26): a SUMMARY/candidate name must clear this Source Score to be a
# genuine capital opportunity (the count-based top-N won't backfill with weak names). Excludes the
# low-source tail (e.g. ADBE ~48). Screen-source scale (0.75 fwd / 0.05 quality / 0.20 valuation).
SUMMARY_SOURCE_FLOOR = 70.0

# ===========================================================================
# JUL-26 FORWARD-LED CALIBRATION (implementation plan ISA_Forward_Calibration_..._Jul2026.md)
# Authoritative parameter set (§0.5). SOURCE_WEIGHTS + FORWARD_AXIS_BUCKET_WEIGHTS + SUMMARY_SOURCE_FLOOR
# are set inline above; the remaining structural constants live here.
# ===========================================================================
# Part 4 — relax the SUMMARY/candidate Part B hard gate from 14 -> 10 (balance-sheet risk is still
# protected by the separate ND/EBITDA MANDATORY_MINIMUM_FAIL gate). Used by source_score.summary_eligible.
SUMMARY_PART_B_FLOOR      = 10

# Part 3 — deployability entry-weight rework (backtested: penalising a stock for having run is backwards).
# Gentler, floored decay so extended momentum winners keep deployability.
DEPLOY_ENTRY_DECAY        = 0.50      # was 0.25 (steeper)
DEPLOY_ENTRY_FLOOR        = 0.50      # entry-weight floor (was ~0)

# Part 7 — held-stock upgrade / replacement test. A middling HOLD (floor<=source<bar) is reclassified
# TRIM (sell-to-upgrade) when the best eligible candidate's Source beats it by >= this margin.
UPGRADE_DELTA             = 15

# Part 8 — sleeve sector / theme concentration caps (netted against fund look-through) + diversification.
SLEEVE_SECTOR_CAP_ISA     = 0.12      # max one GICS sector across direct stocks (share of ISA)
SLEEVE_THEME_CAP          = 0.50      # max one theme as share of the sleeve
DIVERSIFY_OVERRIDE_DELTA  = 10        # source margin a 3rd same-sector name must beat the best other-sector name by


# --- WP-C position alerts (29-Jul-2026): between-run early warning thresholds -----------------
# Consumed by position_alerts.py, run by the weekly EPS snapshot task over data it already fetches.
# DETECTION ONLY -- these never trigger a trade. An alert is context for the NEXT SCHEDULED review
# (see position_alerts.py docstring / C-1). Tune from observed false-positive rate, not intuition.
ALERT_EPS_WOW_DROP_PCT    = -3.0   # +1y consensus EPS fall week-on-week
ALERT_EPS_TRAJ_DROP_PCT   = -6.0   # cumulative +1y EPS fall across the trajectory window
ALERT_TARGET_CUT_PCT      = -8.0   # mean analyst target cut across the window
ALERT_CONSEC_DOWN_WEEKS   = 2      # consecutive down weeks == direction change
ALERT_TRAJECTORY_WEEKS    = 6      # trajectory lookback (weekly observations)

# --- COMPLIANCE REGIME (Raj 29-Jul-2026: Citi redundancy) -----------------------------------
# THE single switch governing employer personal-account-dealing (PAD) rules. Flip to "CITI_PT"
# to restore Citi-equivalent behaviour the day Raj joins another bank -- one line, no other edit.
# NEVER test this constant directly in a caller: import compliance and use its predicates (H-7).
#
# WARNING -- TWO DIFFERENT MIN-HOLDS, DO NOT CONFLATE:
#   * MIN_HOLD_DAYS = 182  -> FRAMEWORK anti-churn rule (C-1 fix, 22-Jul-26). REGIME-INDEPENDENT.
#                             Still fully in force. Pausing it would re-open the -GBP1,097 churn
#                             pattern the audit identified as the framework's costliest defect.
#   * compliance.min_hold_days() -> REGULATORY hold (Citi PT 30d -> 0d while paused).
COMPLIANCE_REGIME = "NONE"                      # "CITI_PT" | "NONE"
COMPLIANCE_REGIME_EFFECTIVE_FROM = "2026-07-29"
COMPLIANCE_REGIME_NOTE = (
    "Citi redundancy confirmed by Raj 29-Jul-2026. Preclearance, 2-day approval validity, "
    "30-day regulatory hold and the narrow/broad instrument test are PAUSED, not retired. "
    "Restore by setting COMPLIANCE_REGIME='CITI_PT' if Raj joins another bank; positions opened "
    "while paused carry a restoration-risk marker (compliance.restoration_risk)."
)

# --- C-1 fix (audit item #3, WP-3, 26-Jul-26): entry-time stability + minimum-hold ---
# Additive config, NOT a scoring-weight change - H-1 freeze untouched.
ENTRY_STABILITY_LOOKBACK_DAYS = 182
ENTRY_STABILITY_FLOOR = 50.0     # reuse exit-floor level: check mirrors the rule policing the position
ENTRY_STABILITY_MIN_SIGHTINGS = 2
ENTRY_STABILITY_MIN_SPAN_DAYS = 60
MIN_HOLD_DAYS = 182
MIN_HOLD_EXEMPT = ("hard_thesis_break", "drawdown_mandate", "preclearance")

# --- H-6 (audit item #7, 26-Jul-26): known-store manifest - one authoritative list,
#     versioned with the code that creates stores; consumed by vci_learning.orphan_check ---
KNOWN_STORE_PATTERNS = (
    r"(action_stack|analytics_data|email_data|portfolio_data|run_context|scores|step9_pre"
    r"|watchlist_metrics|watchlist_scored|xray_data|vci_email_data|vci_prescore_cache"
    r"|vci_prescore|entry_level_audit)_[a-z]{3}_\d{4}\.json",
    r"[A-Za-z0-9_]+_TEMPLATE\.json",
    r"(decision_ledger|drawdown_state|eps_snapshot_resume"
    r"|eps_trend_snapshots|factor_map|fund_returns_cache|sleeve_counterfactual"
    r"|source_performance_log|target_state|target_weights|theme_opportunity"
    r"|vci_base_rates|vci_fv_inputs|vci_learning_store|vci_calibration_state"
    r"|watchlist_tickers"
    # 29-Jul-26: two PRE-EXISTING unregistered stores (both live inputs, both were showing as
    # ORPHAN-SUSPECT) plus the three WP-B/WP-C stores.
    # transaction_ledger = Step 1b dealing record, PERSISTENT - never delete it.
    # supplementary_constituents = screener universe supplement.
    r"|transaction_ledger|supplementary_constituents"
    r"|position_alerts|calibration_state|score_panel_backfill_manifest|learning_growth_state"
    r")\.json",
)

# --- H-8 (audit item #8, 26-Jul-26): semis-complex look-through ---
SEMIS_TICKERS = ("MU", "AVGO", "NVDA", "AMD", "TSM", "ASML", "SMCI", "ANET", "MRVL")
SEMIS_WATCH_PCT = 18.0   # report-only WATCH marker; hard cap is a Raj decision (Oct run)


# M3 listing policy (05-Aug-2026, Raj): exclude non-common instruments (preferred
# depositary shares, tangible equity units, notes) from the rankable universe, and rank
# ONE line per issuer — the most liquid. Set False to revert to the pre-05-Aug behaviour
# in which GOOGM, SMCIP and NOVTU were all independently rankable.
LISTING_POLICY_ACTIVE = True
