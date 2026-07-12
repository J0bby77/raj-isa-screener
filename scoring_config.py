#!/usr/bin/env python3
"""
scoring_config.py — SINGLE SOURCE OF TRUTH for ISA path-scorer thresholds.

WHY THIS EXISTS
  The pre-run must use ONLY the scorers used in the 3 paths (growth / energy / VCI),
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

# Energy valuation parity (redesign Part 2 §F) — bring energy Part B into line with growth v27:
# growth-ADJUST EV/EBITDA + Forward P/E (vs raw multiples) and DROP the stale 52-week-position metric
# (Part B max 16 -> 14 when on). DEFAULT False = current energy scoring (Sunday-run-safe); activate + test.
ENERGY_VALUATION_PARITY  = True
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
FORWARD_ELIG_PART_A_FLOOR_ENERGY  = 14    # energy: 14 = energy "Strong"/Part-B-kick-in line on /20 (NOT the Acceptable
                                          # floor of 8); energy is a curated ~28-name list + Part B only computes for
                                          # Part A>=14, so 14 is its natural fully-scorable floor (conscious asymmetry).

# Source Score weights (redesign Part 3 §13) — the FORWARD-LED ranking composite in rerank_watchlist.
# F dominant, quality de-emphasised, cheapness earns no separate credit. PROVISIONAL — calibrate in
# shadow. Used ONLY when FORWARD_AXIS_IN_RANKING=True (else rerank runs its legacy deployment composite).
# Jul-26 Part 1: THE single Source-Score weight dict (used by source_score.compute_source_score,
# inherited by build_excel / build_email / rerank_watchlist / screener_core overlay). forward 0.60 /
# revisions 0.15 / deployability 0.10 / quality 0.05 / analyst 0.10 (higher-end, per Raj's call).
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
VCI_REQUIRED_ANNUAL_RETURN = 0.14      # Raj's stock hurdle (RESOLVED 6-Jul); ADJUST yearly to portfolio needs
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
VCI_BINARY_MAX_CONCURRENT     = 3     # loosened secondary guard; budget is primary
# E5 — liquidity-aware eligibility & sizing. FLIPPED LIVE 6-Jul-2026 (Raj). Min-ADV gate armed;
# inert until adv_usd is supplied to evaluate_candidate (None -> gate skipped), so it bites only once
# ADV data flows. Rollback: 0.
VCI_MIN_ADV_USD    = 1_000_000         # LIVE — below -> manual (rollback: 0)
VCI_MAX_PCT_ADV    = 0.10             # position value <= 10% of ADV
VCI_MAX_SPREAD_BPS = 100
# E7 — asymmetry-compression cause split
VCI_FV_EROSION_THRESHOLD = 0.15       # FV revised down >15% run-over-run = thesis erosion (not harvest)
VCI_RANK_MODE                 = "advisory"          # §11.6
VCI_BINARY_MAX_CONCURRENT     = 2                    # §9.4
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
# & est-rev not deteriorating & Part B>=14). When True: top-N by a forward-led SCREEN Source Score (no
# deployability/entry data at screen time), multi-door eligibility (viability Part A>=14 + Part B>=14 +
# not deteriorating). Activate together with the gate relaxations + two-pass fetch.
SUMMARY_COUNT_BASED    = True
SUMMARY_TARGET_COUNT   = 30

# ===========================================================================
# ENERGY  (energy_screener.py — Part A /20 + Part B /16 = Total /36)
# ===========================================================================
ENERGY_PART_A_MAX        = 20
ENERGY_PART_B_MAX        = 16
ENERGY_TOTAL_MAX         = 36

ENERGY_PART_A_STRONG     = 14
ENERGY_PART_A_ACCEPTABLE = 8
ENERGY_PART_B_STRONG     = 11
ENERGY_PART_B_WATCH      = 6
ENERGY_HIGH_SCORE        = 28     # ~78% of 36

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
ENERGY_CONVICTION_BRACKETS = _brackets(ENERGY_TOTAL_MAX)   # 33 / 30 / 25 on /36

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
# DEFAULT False = current carry-forward behaviour (4