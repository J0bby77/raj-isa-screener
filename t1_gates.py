#!/usr/bin/env python3
"""
t1_gates.py — Fix Pack Jul-2026 (Doc A, P2): THE T1 qualification gate set (A2/A3/A4/A15)
+ A5 v3 evidence-based sizing (Raj, 15-Jul-2026 — D18/D19 approved).

T1 = QUALIFICATION, not a rank band (A4). One implementation, three consumers:
  - rerank_watchlist.py    : stamps gate fields + t1_qualified on every eligible name
  - step9_pre_builder.py   : derives the tier from t1_qualified when cfg.T1_QUALIFICATION_MODE
  - intramonth review (A16): Steps E/F call evaluate() — same gate set, no restated numbers

Gates (ALL must pass; documented-cause overrides where D2 allows):
  ns_floor    : normalised_score >= 60 (the existing removal floor — not a new bar)
  stage       : revision_stage not in SUMMARY_STAGE_EXCLUDE (A3/D2 — override: documented runway case)
  er          : expected_return_12_24m >= ER_DEPLOY_FLOOR (A2/D1 — override: named catalyst <90d)
  clean_flags : no disqualifiers, no unresolved reversal, no UNdocumented late-cycle (A15)

A5 v3 — TENURE IS NOT A GATE (supersedes the P2 cycles_seen>=2 rule; change log in spec §3-A5):
a hedge fund sizes on edge/uncertainty, never on discovery date. The screen sighting is the
DISCOVERY event, not the evidence — the evidence is the underlying data, which is itself a time
series (both-window estimate revisions = confirmation over time that already happened). So:
  evidence_confirmed (mechanical, computable at FIRST sighting) =
        er_confidence >= EVIDENCE_ER_CONF_MIN (D18: 0.75)
    AND estimate revisions improving on BOTH windows (30d direction + 90d eps-trend trajectory)
    AND stage in {Igniting, Accelerating, Sustained}
    OR  screen_sightings >= 2 spaced >= EVIDENCE_SIGHTING_GAP_DAYS (D19: 7d) — the alternative
        route for thin-fundamentals names (sightings counted from score_panel.csv, live since A8).
  ⚑ THIS MODULE NO LONGER PUBLISHES A SIZE (ISA-0442, Raj option (a), 26-Aug-2026). It used to
  return `size_mode` full|starter with a `starter_cap_pct` of 1.5% and a docstring claim that
  "full additionally requires Step-10 conviction >= 75". The conviction sentence was never
  implemented in this file — no line here has ever read a conviction score — but the size_mode
  WAS live, and it was a SECOND authority for how big a position is, against V2.1's ladder in
  `position_sizing.target_pct(evidence_state)`. Its 1.5% sat BELOW the ladder's 3.5% STARTER, so
  on the next forward-led buy the smaller of two numbers would have won silently. What this
  module certifies is EVIDENCE; what decides pounds is the ladder, and there is now exactly one
  path from a qualified candidate to a position size.
Evidence NEVER blocks a deploy. cycles_seen / screen_sightings remain LOGGED data —
pre-registered calibration rule (A8 pattern): if first-sighting full-size entries underperform
confirmed-sighting entries at 3m over >=2 quarters of ledger data, tighten EVIDENCE_ER_CONF_MIN.

Invariant 1: evaluate() ALWAYS computes; consumers only let it decide when T1_QUALIFICATION_MODE.
Invariant: no gate blocks on data it didn't see — missing stage / E[r] is NO_DATA (pass + flagged).
Overrides are irreducibly qualitative (invariant 5 residual): the Step-10 case records
entry["t1_gate_overrides"] = {"stage": "<runway cause>", "late_cycle": "<cause>"}; the machine
verifies PRESENCE of the documented cause (checkpoint_d ticks it), the review owns its content.
"""
from __future__ import annotations
import csv, os, sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import scoring_config as _cfg
except Exception:                     # standalone/self-test safety — mirrors sibling modules
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


# ── ISA-0432: the guarded accessor for anchor-derived thresholds ──────────────────────────
# These quantities are DERIVED from the portfolio value and the contribution schedule and move
# whenever either does. A literal default here is a shadow copy that stops tracking on the day it
# is written, so this delegates to isa_policy.derived(), which RAISES rather than substituting one.
def _policy_derived(_name, _cfg_override=None):
    import isa_policy as _pol
    return _pol.derived(_name, cfg=_cfg_override)


NS_FLOOR = 60.0   # existing removal floor (step9 assign_main_tier hard floor) — referenced, not new
LATE_STAGES = ("Maturing", "Rolling over")
EVIDENCE_STAGES = ("Igniting", "Accelerating", "Sustained")


def _num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except Exception:
        return None


def _stage_exclude():
    return tuple(_c("SUMMARY_STAGE_EXCLUDE", ["Maturing", "Rolling over", "Flat/Down", "Marginal"]))


def stage_gate(stage):
    """A3/D2 -> (state, blocked). Unknown/missing stage NEVER blocks (NO_DATA, review-visible)."""
    if not stage:
        return "NO_DATA", False
    if str(stage) in _stage_exclude():
        return "BLOCKED_PENDING_CASE", True
    return "OK", False


def late_cycle_flag(premium_disc_pct, stage):
    """A15: extended multiple + late stage => documented-cause treatment (buy-side symmetry with
    sell Signal 2). val_hist carries a 3yr-avg premium/discount, NOT a percentile series, so the
    spec's 'own 5-yr 90th pct' is proxied by premium >= LATE_CYCLE_PREMIUM_PCT (config, 35% —
    ~90th pct of a typical own-history multiple distribution; replace when a percentile series
    lands; basis logged in the change log per standing build permission)."""
    p = _num(premium_disc_pct)
    if p is None or not stage:
        return False
    return str(stage) in LATE_STAGES and p >= float(_c("LATE_CYCLE_PREMIUM_PCT", 35.0))


def catalyst_within_90d(entry, ref_date=None):
    """D1 override source: named/confirmed catalyst inside CATALYST_MAX_DAYS (default 90).
    Accepted evidence (any): confirmed_catalyst / catalyst_protected (existing wt fields),
    days_to_catalyst (numeric), or a parseable binary-event date (risk_flags or first-class)."""
    if not entry:
        return False
    if entry.get("confirmed_catalyst") or entry.get("catalyst_protected"):
        return True
    maxd = int(_c("CATALYST_MAX_DAYS", 90))
    n = _num(entry.get("days_to_catalyst"))
    if n is not None:
        return 0 <= n <= maxd
    rf = entry.get("risk_flags")
    ev = (rf.get("binary_event_within_90d") if isinstance(rf, dict) else None) \
        or entry.get("binary_event_within_90d")
    if isinstance(ev, str) and len(ev) >= 10:
        try:
            dt = datetime.strptime(ev[:10], "%Y-%m-%d").date()
            ref = ref_date or date.today()
            return 0 <= (dt - ref).days <= maxd
        except Exception:
            return False
    return False


def screen_sightings_from_panel(ticker, panel_path=None, ref_date=None):
    """A5 v3 alternative evidence route: count DISTINCT screen sightings of `ticker` in
    score_panel.csv (A8 — every SUMMARY row of every screen is logged there since P0) within
    EVIDENCE_SIGHTING_WINDOW_DAYS, where consecutive counted sightings are spaced
    >= EVIDENCE_SIGHTING_GAP_DAYS apart. Returns int, or None when the panel is unreadable
    (evidence then rests on the fundamentals route alone — never a crash, never a block)."""
    panel_path = panel_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "score_panel.csv")
    try:
        ref = ref_date or date.today()
        window = int(_c("EVIDENCE_SIGHTING_WINDOW_DAYS", 45))
        gap = int(_c("EVIDENCE_SIGHTING_GAP_DAYS", 7))
        t = str(ticker).strip().upper()
        dates = set()
        with open(panel_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("ticker", "")).strip().upper() != t:
                    continue
                d = str(row.get("run_date", ""))[:10]
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if 0 <= (ref - dt).days <= window:
                    dates.add(dt)
        count, last = 0, None
        for dt in sorted(dates):
            if last is None or (dt - last).days >= gap:
                count += 1
                last = dt
        return count
    except Exception:
        return None


def entry_stability(ticker, panel_path=None, ref_date=None):
    """C-1 fix (WP-3, audit #3, 26-Jul-26) - entry-time Forward-Axis stability check for
    Path A. Reads score_panel.csv sightings within ENTRY_STABILITY_LOOKBACK_DAYS.
    PASS = >= ENTRY_STABILITY_MIN_SIGHTINGS sightings spanning >= MIN_SPAN days, ALL with
    forward_axis_score AND source_score >= ENTRY_STABILITY_FLOOR. FAIL = any sighting in
    window below floor on either leg. UNVERIFIED = insufficient sightings/span (contract:
    starter size 1.5%, existing A5v3 mechanics). Malformed rows skipped; unreadable panel
    -> UNVERIFIED. Never raises, never blocks - sizing/blocking is Step 10's call."""
    panel_path = panel_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "score_panel.csv")
    floor = float(_c("ENTRY_STABILITY_FLOOR", 50.0))
    lookback = int(_c("ENTRY_STABILITY_LOOKBACK_DAYS", 182))
    min_n = int(_c("ENTRY_STABILITY_MIN_SIGHTINGS", 2))
    min_span = int(_c("ENTRY_STABILITY_MIN_SPAN_DAYS", 60))
    ref = ref_date or date.today()
    t = str(ticker).strip().upper()
    sightings = []
    try:
        with open(panel_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("ticker", "")).strip().upper() != t:
                    continue
                try:
                    dt = datetime.strptime(str(row.get("run_date", ""))[:10], "%Y-%m-%d").date()
                    fa = float(row.get("forward_axis_score"))
                    src = float(row.get("source_score"))
                except (TypeError, ValueError):
                    continue
                if 0 <= (ref - dt).days <= lookback:
                    sightings.append((dt, min(fa, src)))
    except OSError:
        return {"verdict": "UNVERIFIED", "sightings": 0, "min_score": None,
                "detail": "score_panel.csv unreadable - starter size per A5v3"}
    if not sightings:
        return {"verdict": "UNVERIFIED", "sightings": 0, "min_score": None,
                "detail": "no sightings in last %dd - starter size per A5v3" % lookback}
    min_score = min(x for _, x in sightings)
    n = len(set(d for d, _ in sightings))
    span = (max(d for d, _ in sightings) - min(d for d, _ in sightings)).days
    if min_score < floor:
        return {"verdict": "FAIL", "sightings": n, "min_score": min_score,
                "detail": ("sighting below floor %g in window (min %g) - Path A full "
                           "deployment BLOCKED; override only via A13 with reason"
                           % (floor, min_score))}
    if n >= min_n and span >= min_span:
        return {"verdict": "PASS", "sightings": n, "min_score": min_score,
                "detail": "%d sightings over %dd, all >= %g on both legs" % (n, span, floor)}
    # 02-Aug-2026 (Aug retrospective item 2): distinguish "this NAME lacks history" from
    # "the PANEL itself is younger than the test's own lookback".
    #
    # score_panel.csv begins 25-Jun-2026, so on the Aug-2026 run the maximum span available to
    # ANY name was 37 days against a 60-day requirement. Every candidate therefore returned a
    # bare UNVERIFIED and every Path A entry was capped at starter size — the right outcome by
    # accident, not by design. Worse, the gate carried NO information: it could not distinguish
    # a genuinely unstable name from a merely young panel, so a PASS was unreachable and a
    # non-PASS meant nothing.
    #
    # UNVERIFIED_PANEL_TOO_YOUNG says so explicitly, and reports the date from which a PASS
    # first becomes attainable. It is still non-PASS and still caps at starter size — the
    # SIZING is unchanged (H7: nothing here adjusts a threshold). What changes is that the
    # reason is now legible instead of silent.
    panel_start = _panel_start_date(panel_path)
    if panel_start is not None:
        panel_span = (ref - panel_start).days
        if panel_span < min_span:
            return {"verdict": "UNVERIFIED_PANEL_TOO_YOUNG", "sightings": n,
                    "min_score": min_score, "panel_start": panel_start.isoformat(),
                    "panel_span_days": panel_span,
                    "pass_attainable_from": (panel_start + timedelta(days=min_span)).isoformat(),
                    "detail": ("score_panel.csv begins %s, so the widest span available to ANY "
                               "name is %dd against the %dd this test requires. No candidate can "
                               "return PASS before %s. This is a PANEL-AGE limit, not evidence "
                               "about %s. Starter size per A5v3 — unchanged."
                               % (panel_start.isoformat(), panel_span, min_span,
                                  (panel_start + timedelta(days=min_span)).isoformat(), t))}
    return {"verdict": "UNVERIFIED", "sightings": n, "min_score": min_score,
            "detail": ("insufficient history (%d sightings, span %dd; need >= %d over >= %dd)"
                       " - starter size per A5v3" % (n, span, min_n, min_span))}


_PANEL_START_CACHE = {}


def _panel_start_date(panel_path):
    """Earliest run_date in the panel. Cached per path — entry_stability is called once per
    candidate and re-reading a 3,000-row CSV for each would be gratuitous."""
    if panel_path in _PANEL_START_CACHE:
        return _PANEL_START_CACHE[panel_path]
    earliest = None
    try:
        with open(panel_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    dt = datetime.strptime(str(row.get("run_date", ""))[:10], "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    continue
                if earliest is None or dt < earliest:
                    earliest = dt
    except OSError:
        earliest = None
    _PANEL_START_CACHE[panel_path] = earliest
    return earliest


def min_hold_until(entry_date):
    """WP-3 rule B (26-Jul-26) - Path A minimum-hold stamp: entry + MIN_HOLD_DAYS. Early
    exit only on MIN_HOLD_EXEMPT grounds (hard_thesis_break/drawdown_mandate/preclearance)."""
    from datetime import timedelta
    if isinstance(entry_date, str):
        entry_date = datetime.strptime(entry_date[:10], "%Y-%m-%d").date()
    return entry_date + timedelta(days=int(_c("MIN_HOLD_DAYS", 182)))


def evidence(entry, scored_row=None, ref_date=None):
    """A5 v3 — evidence-based sizing (D18/D19). MECHANICAL, computable at first sighting;
    NEVER blocks (sizing only). Returns:
      {evidence_confirmed, size_authority, basis: {...}}   -- NO SIZE (ISA-0442)
    Fundamentals route: er_confidence >= EVIDENCE_ER_CONF_MIN AND revisions improving on BOTH
    windows (30d canonical direction AND 90d eps-trend trajectory > 0) AND early/mid stage.
    Sightings route: screen_sightings >= EVIDENCE_SIGHTING_MIN (>=7d apart, from score_panel).
    ⚑ It CERTIFIES; it does not size. `position_sizing.target_pct(evidence_state)` is the one
    home for how big a position is (ISA-0442)."""
    e = entry or {}
    s = scored_row or {}

    def g(k):
        return e.get(k) if e.get(k) is not None else s.get(k)

    er_conf = _num(g("er_confidence"))
    conf_min = float(_c("EVIDENCE_ER_CONF_MIN", 0.75))
    # D-24 §5: an UNMEASURED re-rate makes E[r] a partial number. It may not certify the
    # fundamentals evidence route, and the name is never eligible for `full` size on it.
    er_unmeasured = str(g("er_status") or "") == "unmeasured"
    conf_ok = (er_conf is not None and er_conf >= conf_min) and not er_unmeasured
    direction = str(g("est_rev_direction") or "").lower()
    trend_90d = _num(g("eps_trend_mom_pct"))
    rev_ok = (direction == "improving") and (trend_90d is not None and trend_90d > 0)
    stage = g("revision_stage")
    stage_ok = str(stage) in EVIDENCE_STAGES if stage else False
    fundamentals_ok = conf_ok and rev_ok and stage_ok

    sightings = g("screen_sightings")
    sightings = int(sightings) if _num(sightings) is not None else None
    sightings_ok = sightings is not None and sightings >= int(_c("EVIDENCE_SIGHTING_MIN", 2))

    confirmed = bool(fundamentals_ok or sightings_ok) and not er_unmeasured
    out = {
        "evidence_confirmed": confirmed,
        # ⚑ ISA-0442 — a POINTER, not a size. Anything downstream that needs pounds asks the one
        # home; anything that only needs to know whether the evidence side is satisfied reads
        # `evidence_confirmed`. There is no third answer available from this module.
        "size_authority": ("position_sizing.target_pct(evidence_state) — the V2.1 fixed ladder. "
                           "t1_gates certifies evidence and publishes NO size (ISA-0442)."),
        "basis": {
            "route": ("unconfirmed_er_unmeasured" if er_unmeasured else
                      "fundamentals" if fundamentals_ok else
                      "sightings" if sightings_ok else "unconfirmed"),
            "er_confidence": er_conf, "er_conf_min": conf_min, "conf_ok": conf_ok,
            "er_status": (g("er_status") or None), "er_unmeasured": er_unmeasured,
            "rev_30d_direction": direction or None, "rev_90d_trend_pct": trend_90d,
            "rev_both_windows_ok": rev_ok,
            "stage": stage, "stage_ok": stage_ok,
            "screen_sightings": sightings, "sightings_ok": sightings_ok,
            "note": ("evidence certification only. The position TARGET is the ladder rung for "
                     "this name's evidence_state, from position_sizing — this module has no "
                     "opinion on pounds and no conviction input (ISA-0442)."),
        },
    }
    if not _c("SIZE_AUTHORITY_SINGLE", True):
        # R4.13 ROLLBACK. ⚑ Note what it restores and what it does NOT: the size_mode LABEL comes
        # back, read from the ladder. The 1.5% literal does not come back on any path — it was the
        # defect, not the behaviour.
        try:
            import position_sizing as _ps
            _lad = _ps.ladder()
            out["size_mode"] = "full" if confirmed else "starter"
            out["starter_rung_pct"] = _lad.get("STARTER")
            out["rollback_note"] = ("SIZE_AUTHORITY_SINGLE is False: the legacy label is restored, "
                                    "sourced from the ladder. The stale 1.5%% constant is gone.")
        except Exception as _e:                                       # noqa: BLE001
            out["size_mode_error"] = "%s: %s" % (type(_e).__name__, _e)
    return out


def evaluate(entry, scored_row=None, ref_date=None):
    """Full A4 gate set for one name. entry = watchlist/pool entry (rerank-stamped fields OK);
    scored_row = watchlist_scored tickers[t] dict (fallback field source). Returns the detail
    dict; ['t1_qualified'] is THE qualification verdict (truth table in U-A4).
    A5 v3: persistence/tenure is NOT a gate — ['evidence'] carries the sizing verdict instead;
    cycles_seen / screen_sightings are surfaced as logged calibration data."""
    e = entry or {}
    s = scored_row or {}

    def g(k):
        return e.get(k) if e.get(k) is not None else s.get(k)

    overrides = e.get("t1_gate_overrides") or {}
    ns = _num(g("normalised_score"))
    stage = g("revision_stage")
    er = _num(g("expected_return_12_24m"))
    catalyst = catalyst_within_90d(e, ref_date)
    st_state, st_blocked = stage_gate(stage)
    late = late_cycle_flag(g("val_hist_pe_premium_disc"), stage)
    dq = list(e.get("disqualifier_flags") or s.get("disqualifier_flags") or [])
    reversal = bool(e.get("reversal_unresolved")) or (
        "recent_reversal_vs_12_1m" in (e.get("review_flags") or s.get("review_flags") or []))
    er_floor = _policy_derived("ER_DEPLOY_FLOOR")   # ISA-0432: no literal fallback
    er_status = str(g("er_status") or "")
    er_unmeasured = (er_status == "unmeasured")

    detail = {
        "ns_floor": {"pass": ns is not None and ns >= NS_FLOOR, "value": ns},
        "stage": {"pass": (not st_blocked) or bool(overrides.get("stage")),
                  "state": st_state, "value": stage, "override": overrides.get("stage")},
        # er: missing E[r] = NO_DATA -> pass + flagged (never block on unseen data).
        # D-24 (09-Aug-2026): er_status == "unmeasured" means the RE-RATE term was refused rather
        # than silently scored 0, so the total is a partial figure. Doctrine is unchanged — "no
        # gate blocks on data it didn't see" — so it is NO_DATA (pass + flagged), NOT a pass on a
        # fabricated number. `evidence()` additionally denies it `full` size.
        "er": {"pass": (er is None) or er_unmeasured or (er >= er_floor) or catalyst,
               "state": ("NO_DATA" if er is None else
                         "NO_DATA_UNMEASURED" if er_unmeasured else
                         "OK" if (er >= er_floor or catalyst) else "BELOW_FLOOR"),
               "value": er, "floor": er_floor, "catalyst": catalyst,
               "er_status": er_status, "er_rerate_status": g("er_rerate_status"),
               "partial": er_unmeasured},
        "clean_flags": {"pass": (not dq) and (not reversal) and ((not late) or bool(overrides.get("late_cycle"))),
                        "disqualifiers": dq, "reversal_unresolved": reversal,
                        "late_cycle_flag": late, "override": overrides.get("late_cycle")},
    }
    gates = ("ns_floor", "stage", "er", "clean_flags")
    detail["t1_qualified"] = all(detail[k]["pass"] for k in gates)
    detail["stage_gate"] = st_state
    detail["late_cycle_flag"] = late
    # A5 v3: EVIDENCE certification (never blocks) + logged tenure data for the ledger and
    # calibration. ⚑ No size is produced here — ISA-0442.
    detail["evidence"] = evidence(e, s, ref_date)
    detail["evidence_confirmed"] = detail["evidence"]["evidence_confirmed"]
    detail["size_authority"] = detail["evidence"]["size_authority"]
    if "size_mode" in detail["evidence"]:                 # rollback path only (ISA-0442)
        detail["size_mode"] = detail["evidence"]["size_mode"]
    detail["cycles_seen"] = _num(g("cycles_seen"))
    detail["screen_sightings"] = detail["evidence"]["basis"]["screen_sightings"]
    return detail


def tier_for(entry, scored_row=None, ref_date=None, detail=None):
    """A4 tier derivation (qualification mode): T1 = ALL qualified names (however many);
    T2 = unqualified but above the viability floor (ns >= 60); T3 = rest. Attention order
    WITHIN T1 = source_score desc (deploy tiebreak; Step 10.1 caps cases at 5 by this order)."""
    d = detail or evaluate(entry, scored_row, ref_date)
    if d["t1_qualified"]:
        return "T1"
    ns = _num((entry or {}).get("normalised_score"))
    if ns is None:
        ns = _num((scored_row or {}).get("normalised_score"))
    return "T2" if (ns is not None and ns >= NS_FLOOR) else "T3"


if __name__ == "__main__":
    ref = date(2026, 7, 15)
    base = {"normalised_score": 72, "revision_stage": "Sustained", "expected_return_12_24m": 18.0,
            "disqualifier_flags": [], "review_flags": []}
    # 1. clean qualifier
    assert evaluate(dict(base), ref_date=ref)["t1_qualified"] is True
    # 2. Maturing stage blocks... unless documented runway case
    m = dict(base, revision_stage="Maturing")
    d = evaluate(m, ref_date=ref)
    assert d["stage_gate"] == "BLOCKED_PENDING_CASE" and not d["t1_qualified"]
    d = evaluate(dict(m, t1_gate_overrides={"stage": "runway: cloud regate FY27"}), ref_date=ref)
    assert d["t1_qualified"] is True
    # 3. E[r] below floor blocks; catalyst override unblocks; NO_DATA passes flagged
    lo = dict(base, expected_return_12_24m=9.0)
    assert not evaluate(lo, ref_date=ref)["t1_qualified"]
    assert evaluate(dict(lo, confirmed_catalyst=True), ref_date=ref)["t1_qualified"]
    nd = evaluate(dict(base, expected_return_12_24m=None), ref_date=ref)
    assert nd["er"]["state"] == "NO_DATA" and nd["t1_qualified"]
    # 4. A5 v3: tenure NEVER gates — first-sighting name with confirmed evidence qualifies FULL
    fresh = dict(base, cycles_seen=1, er_confidence=0.9, est_rev_direction="improving",
                 eps_trend_mom_pct=4.2)
    d = evaluate(fresh, ref_date=ref)
    assert d["t1_qualified"] and d["evidence_confirmed"], d["evidence"]
    assert "size_mode" not in d and "starter_cap_pct" not in d["evidence"], (
        "ISA-0442: t1_gates must publish NO size. A size field here is a second authority.")
    assert "position_sizing.target_pct" in d["size_authority"]
    assert d["evidence"]["basis"]["route"] == "fundamentals"
    # 5. thin evidence -> STARTER, still qualified (sizing caps, never blocks)
    thin = dict(base, cycles_seen=1, er_confidence=0.4, est_rev_direction="neutral")
    d = evaluate(thin, ref_date=ref)
    assert d["t1_qualified"] and not d["evidence_confirmed"], d["evidence"]
    # 5b. one window improving is NOT both-window confirmation
    onew = dict(base, er_confidence=0.9, est_rev_direction="improving", eps_trend_mom_pct=-1.0)
    assert evaluate(onew, ref_date=ref)["evidence_confirmed"] is False
    # 6. sightings route: thin fundamentals but 2 spaced screen sightings -> full
    seen2 = dict(thin, screen_sightings=2)
    d = evaluate(seen2, ref_date=ref)
    assert d["evidence_confirmed"] and d["evidence"]["basis"]["route"] == "sightings"
    # 7. reversal + disqualifier + late-cycle still block; late-cycle documented case unblocks
    assert not evaluate(dict(base, review_flags=["recent_reversal_vs_12_1m"]), ref_date=ref)["t1_qualified"]
    assert not evaluate(dict(base, disqualifier_flags=["revision_cut"]), ref_date=ref)["t1_qualified"]
    lc = dict(base, revision_stage="Maturing", val_hist_pe_premium_disc=42.0,
              t1_gate_overrides={"stage": "runway documented"})
    assert not evaluate(lc, ref_date=ref)["t1_qualified"]
    lc["t1_gate_overrides"]["late_cycle"] = "multiple re-based post-divestment"
    assert evaluate(lc, ref_date=ref)["t1_qualified"]
    # 8. tiers — tenure absent from the decision
    assert tier_for(dict(base, cycles_seen=1), ref_date=ref) == "T1"
    assert tier_for(dict(base, cycles_seen=1, normalised_score=55), ref_date=ref) == "T3"
    # 9. scored_row fallback supplies stage + premium + evidence inputs
    d = evaluate({"normalised_score": 75, "expected_return_12_24m": 20},
                 {"revision_stage": "Accelerating", "val_hist_pe_premium_disc": 10,
                  "er_confidence": 1.0, "est_rev_direction": "improving",
                  "eps_trend_mom_pct": 6.0}, ref_date=ref)
    assert d["t1_qualified"] and d["stage_gate"] == "OK" and d["evidence_confirmed"]
    # 10. sightings gap logic (pure csv helper) — synthetic panel
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("run_date,group,ticker\n2026-07-01,NASDAQ,ALAB\n2026-07-03,SP500,ALAB\n"
                "2026-07-10,NASDAQ,ALAB\n2026-05-01,NASDAQ,ALAB\n2026-07-10,NASDAQ,OTHER\n")
        pth = f.name
    n = screen_sightings_from_panel("ALAB", pth, ref_date=date(2026, 7, 15))
    assert n == 2, n   # 01-Jul counts, 03-Jul too close (<7d), 10-Jul counts, May out of window
    os.unlink(pth)
    assert screen_sightings_from_panel("XXX", "/nonexistent.csv") is None
    # D-24 (09-Aug-2026): er_status == "unmeasured" is NO_DATA (pass + flagged), never a pass on
    # a fabricated number — and never eligible for `full` size however high er_confidence reads.
    um = evaluate(dict(base, expected_return_12_24m=4.0, er_status="unmeasured",
                       er_rerate_status="UNMEASURED", er_confidence=1.0,
                       est_rev_direction="improving", eps_trend_mom_pct=6.0), ref_date=ref)
    assert um["er"]["state"] == "NO_DATA_UNMEASURED" and um["er"]["pass"], um["er"]
    assert um["t1_qualified"] and um["evidence_confirmed"] is False, um
    assert um["evidence"]["basis"]["route"] == "unconfirmed_er_unmeasured", um["evidence"]
    # (gate_status_for_screen_row is defined below this self-test block — its D-24 label
    #  assertion lives in tests_jul2026/test_d24_expected_return.py)
    print("t1_gates SELF-TEST OK (A5 v3 — evidence sizing, tenure gate removed; + D-24 er_status)")


def gate_status_for_screen_row(row, get=None):
    """Review item 4 (18-Jul-26) — email 'Actionable?' column: the T1 gates computable from a
    full_data SCREEN row alone (stage / E[r]-vs-floor / late-cycle / clean final_status).
    ns_floor, reversal history and catalyst dates live on WATCHLIST entries, not screen rows —
    excluded by design (the full evaluate() runs at rerank/step9). Returns (label, reasons):
    'PASS' or 'BLOCKED(reason,..)', with ' !conflict' appended when capital_signal_conflict."""
    g = get or (lambda r, k: r.get(k))
    stage = g(row, "revision_stage")
    _st_state, st_blocked = stage_gate(stage)
    er = _num(g(row, "expected_return_12_24m"))
    er_floor = _policy_derived("ER_DEPLOY_FLOOR")   # ISA-0432: no literal fallback
    late = late_cycle_flag(_num(g(row, "val_hist_pe_premium_disc")), stage)
    reasons = []
    if st_blocked:
        reasons.append("stage")
    # D-24: an UNMEASURED re-rate makes E[r] partial — it must not BLOCK on a number it did not
    # fully see, and it must not read as a clean PASS either. It is flagged, exactly like NO_DATA.
    er_unmeasured = str(g(row, "er_status") or "") == "unmeasured"
    if er is not None and er < er_floor and not er_unmeasured:
        reasons.append(f"E[r]<{er_floor:g}")
    if late:
        reasons.append("late-cycle")
    st_final = str(g(row, "final_status") or "").upper()
    if st_final in ("HARD_GATE_FAIL", "MANDATORY_MINIMUM_FAIL",
                    "UNRESOLVED_HARD_GATE_NOT_RANKABLE"):
        reasons.append("gate-fail")
    label = "PASS" if not reasons else "BLOCKED(" + ",".join(reasons) + ")"
    if er_unmeasured:
        label += " ?E[r]unmeasured"
    conflict = str(g(row, "capital_signal_conflict") or "").lower() in ("true", "1", "yes")
    if conflict:
        label += " !conflict"
    return label, reasons
