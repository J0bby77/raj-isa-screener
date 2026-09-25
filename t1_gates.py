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
    """RETIRED (ISA-0616/0619, Raj 24-Sep-2026). C-1 is CURRENT_ADMISSIBILITY on the latest valid
    current snapshot - see `current_admissibility`. The 182-day / >=2-sightings / >=60-day
    persistence rule is NOT capital authority and must not be resurrected under this name; the
    superseded implementation is kept, uncalled, as `_entry_stability_superseded` for audit only."""
    return {"verdict": "RETIRED_ISA0616", "sightings": None, "min_score": None,
            "detail": ("C-1 is t1_gates.current_admissibility (one latest valid current snapshot); "
                       "historical persistence is not capital authority (ISA-0616/0619)")}


def _entry_stability_superseded(ticker, panel_path=None, ref_date=None):
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


# ── ISA-0616 / ISA-0619 (Raj 24-Sep-2026): C-1 = CURRENT_ADMISSIBILITY / ENTRY_INTEGRITY ─────────
C1_ADMISSIBLE = "ADMISSIBLE"
C1_STATES = ("ADMISSIBLE", "BELOW_FLOOR", "NO_SNAPSHOT", "INCOMPLETE", "STALE", "INVALID_PIT",
             "INCONSISTENT", "INCOMPARABLE_LEGACY_DEFINITION", "INCOMPARABLE_UNKNOWN_DEFINITION",
             "UNDECLARED_CURRENT_DEFINITION", "EVENT_INVALIDATED")
C1_BASIS = ("CURRENT_SNAPSHOT (ISA-0616/0619, Raj 24-Sep-2026): ONE latest valid current sighting - "
            "PIT-valid, complete, fresh, produced under the declared CURRENT scoring definition, "
            "internally consistent, both Forward-Axis and Source >= ENTRY_STABILITY_FLOOR. No "
            "persistence, sighting-count or duration requirement; no STARTER cap from this verdict. "
            "Governs POSITIVE NEW EXPOSURE only (new positions and top-ups); it never creates a SELL.")


def _as_date(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def c1_lookup(verdicts, ticker, aliases=None):
    """ISA-0616: THE one way a consumer reads a name's canonical C-1 verdict from the step9_pre map.

    Exact key first; then the caller-supplied DECLARED broker->Yahoo alias map in either
    direction - never a guessed suffix. Returns (verdict_dict_or_None, key_used). None map ->
    (None, None): the caller states C1_UNAVAILABLE. Present map without the name -> (None, None)
    with the caller stating C1_VERDICT_ABSENT. Both fail closed for new capital only."""
    if verdicts is None or not ticker:
        return None, None
    if ticker in verdicts:
        return verdicts[ticker], ticker
    # ⚑ The DECLARED alias map is supplied by the caller (stock_candidates passes
    #   broker_dealability's), so t1_gates does not pull stock_price_fetch into the weekly-screen
    #   fallback's import closure (A18/ISA-0498). None -> exact key only (fail-closed).
    aliases = aliases or {}
    fwd = aliases.get(ticker)
    if fwd and fwd in verdicts:
        return verdicts[fwd], fwd
    for broker, yahoo in aliases.items():
        if yahoo == ticker and broker in verdicts:
            return verdicts[broker], broker
    return None, None


def current_admissibility(row, ref_date=None, *, event_state=None, current=None):
    """THE C-1 verdict from the latest current snapshot (a scored row of THIS run).

    Reads only the snapshot: the scores it carries, its score-definition stamp and its snapshot
    time. History is never consulted, so no persistence duration can re-enter by this route.
    `event_state`: optional {"material": bool, "ref": ...} from issuer/event review; when not
    supplied the verdict says so (the downstream issuer-freshness control still binds)."""
    r = row or {}
    ref = ref_date or date.today()
    if isinstance(ref, str):
        ref = _as_date(ref) or date.today()
    out = {"basis": C1_BASIS, "evaluated_at": ref.isoformat(), "floor": float(_c("ENTRY_STABILITY_FLOOR", 50.0)),
           "snapshot_as_of": r.get("snapshot_as_of"),
           "score_definition_hash": r.get("score_definition_hash"),
           "score_definition_id": r.get("score_definition_id"),
           "event_state": ("NOT_SUPPLIED - issuer_freshness binds downstream" if event_state is None
                           else event_state)}

    def done(verdict, why, **kw):
        out.update(kw)
        out.update(verdict=verdict, admissible=(verdict == C1_ADMISSIBLE), why=why)
        return out

    if not r:
        return done("NO_SNAPSHOT", "no current scored row for this name in this run")
    try:
        import score_definition as _sd
        ds = _sd.row_state(r, current=current)
    except Exception as exc:                                            # noqa: BLE001
        return done("UNDECLARED_CURRENT_DEFINITION", "score_definition unavailable (%s)" % exc)
    if ds["state"] != _sd.COMPARABLE:
        return done(ds["state"] if ds["state"] in C1_STATES else "INCOMPARABLE_UNKNOWN_DEFINITION",
                    "the snapshot was not produced under the declared current scoring definition "
                    "(%s)" % ds.get("basis", ds.get("why")), definition_state=ds)
    as_of = _as_date(r.get("snapshot_as_of"))
    if as_of is None:
        return done("STALE", "snapshot carries no as_of - freshness cannot be established")
    age = (ref - as_of).days
    out["age_days"] = age
    if age < 0:
        return done("INVALID_PIT", "snapshot is dated after the evaluation date")
    if age > int(_c("C1_SNAPSHOT_MAX_AGE_DAYS", 1)):
        return done("STALE", "snapshot is %d day(s) old; C-1 admits only this run's own snapshot" % age)
    fa = _num(r.get("forward_axis_score"))
    try:                                        # the ONE Source Score definition, from the snapshot
        import source_score as _ss
        src = _num(_ss.source_score_for_row(r))
    except Exception:                                                   # noqa: BLE001
        src = None
    rev = r.get("revisions_score")
    missing = [k for k, v in (("forward_axis_score", fa), ("source_score", src)) if v is None]
    if rev is None or (isinstance(rev, float) and rev != rev) or str(rev).strip() in ("", "nan", "None"):
        missing.append("revisions_score")
    out.update(forward_axis_score=fa, source_score=src)
    if missing:
        return done("INCOMPLETE", "required snapshot field(s) absent: %s" % ", ".join(missing))
    if not (0.0 <= fa <= 100.0 and 0.0 <= src <= 100.0):
        return done("INCONSISTENT", "score outside 0..100 (forward %s, source %s)" % (fa, src))
    if isinstance(event_state, dict) and event_state.get("material"):
        return done("EVENT_INVALIDATED", "a material issuer event invalidates the snapshot (%s)"
                    % event_state.get("ref"))
    if min(fa, src) < out["floor"]:
        return done("BELOW_FLOOR", "current snapshot below floor %g (forward %.1f, source %.1f)"
                    % (out["floor"], fa, src))
    return done(C1_ADMISSIBLE, "current snapshot valid: forward %.1f, source %.1f >= %g under %s"
                % (fa, src, out["floor"], r.get("score_definition_id")))


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
    exit only on `scoring_config.MIN_HOLD_EXEMPT` grounds. ⚑ The grounds are NOT restated
    here (ISA-0647): this docstring listed three of them while the published set carried four,
    and a prose copy of a capital-gating constant is a third home that nothing can diff."""
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
    # ⚑ ISA-0721 (23-Sep-2026) — ADMISSION, NOT SCREENING. This gate feeds t1_qualified ->
    #   stock_candidates -> position_sizing, i.e. POSITIVE NEW SIZE. "No gate blocks on data it
    #   didn't see" is right for a screen and wrong for an admission: absence of an expected
    #   return became permission. A missing or partial E[r] now BLOCKS positive size; it never
    #   triggers a sale (this gate governs additions only), and a catalyst override needs a
    #   VALID E[r] to override.
    er_state = str(g("er_state") or "")
    if not er_state:
        er_state = ("MISSING_REQUIRED_INPUT" if er is None else
                    "PARTIAL_UNMEASURED_RERATE" if er_unmeasured else "VALID_LEGACY_UNTYPED")
    er_valid = (er is not None and not er_unmeasured
                and er_state in ("VALID_MECHANICAL", "VALID_MECHANICAL_PROXY",
                                 "VALID_STRUCTURED_JUDGEMENT", "VALID_LEGACY_UNTYPED"))

    detail = {
        "ns_floor": {"pass": ns is not None and ns >= NS_FLOOR, "value": ns},
        "stage": {"pass": (not st_blocked) or bool(overrides.get("stage")),
                  "state": st_state, "value": stage, "override": overrides.get("stage")},
        # er (ISA-0721): only a VALID E[r] can admit. Missing -> BLOCKED_MISSING_ER; a partial
        # figure (re-rate refused) -> BLOCKED_PARTIAL_ER. Superseded doctrine, retained and marked
        # (R2.13): D-24 read both as NO_DATA and PASSED them ("never block on unseen data").
        "er": {"pass": er_valid and ((er >= er_floor) or catalyst),
               "state": ("BLOCKED_MISSING_ER" if er is None else
                         "BLOCKED_PARTIAL_ER" if not er_valid else
                         "OK" if (er >= er_floor or catalyst) else "BELOW_FLOOR"),
               "value": er, "floor": er_floor, "catalyst": catalyst,
               "er_status": er_status, "er_state": er_state,
               "er_rerate_status": g("er_rerate_status"),
               "partial": er_unmeasured},
        "clean_flags": {"pass": (not dq) and (not reversal) and ((not late) or bool(overrides.get("late_cycle"))),
                        "disqualifiers": dq, "reversal_unresolved": reversal,
                        "late_cycle_flag": late, "override": overrides.get("late_cycle")},
    }
    # ⚑ ISA-0616 (24-Sep-2026): C-1 current admissibility from THIS run's snapshot (the scored
    #   row). A qualification gate for POSITIVE NEW EXPOSURE only - it never creates a sale.
    _snap = dict(s) if s else {}
    detail["c1"] = current_admissibility(_snap, ref_date)
    detail["c1"]["pass"] = bool(detail["c1"]["admissible"])
    gates = ("ns_floor", "stage", "er", "clean_flags", "c1")
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


def _selftest():
    ref = date(2026, 7, 15)
    # ISA-0616: every positive case needs THIS run's valid snapshot (the scored row). SNAP is a
    # synthetic current snapshot under the declared CURRENT scoring definition, dated `ref`.
    import score_definition as _sd
    _cur = _sd.current_identity()
    SNAP = {"forward_axis_score": 90.0, "revisions_score": 90.0, "part_a_score": 28,
            "score_definition_hash": _cur["hash"], "score_definition_id": _cur.get("id"),
            "score_definition_basis": "STAMPED_AT_SCORING", "snapshot_as_of": ref.isoformat()}
    _evaluate = globals()["evaluate"]

    def evaluate(e, scored_row=None, ref_date=None):                   # noqa: F811
        return _evaluate(e, {**SNAP, **(scored_row or {})}, ref_date)
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
    # ⚑ ISA-0721 MUST-FIRE: a MISSING E[r] no longer admits (was: NO_DATA -> pass).
    assert nd["er"]["state"] == "BLOCKED_MISSING_ER" and not nd["t1_qualified"], nd["er"]
    # ⚑ ISA-0721 MUST-FIRE: a catalyst cannot override an E[r] that does not exist.
    ndc = evaluate(dict(base, expected_return_12_24m=None, confirmed_catalyst=True), ref_date=ref)
    assert not ndc["t1_qualified"], ndc["er"]
    # ISA-0721 NEGATIVE CONTROL: a typed VALID E[r] above the floor still qualifies.
    assert evaluate(dict(base, er_state="VALID_MECHANICAL"), ref_date=ref)["t1_qualified"], \
        "ISA-0721 NEGATIVE CONTROL: a typed VALID E[r] above the floor must still qualify"
    # ISA-0721 MUST-FIRE: a typed MISSING_REQUIRED_INPUT blocks even if a stale scalar lingers.
    assert not evaluate(dict(base, er_state="MISSING_REQUIRED_INPUT"), ref_date=ref)["t1_qualified"], \
        "ISA-0721 MUST-FIRE: a typed MISSING_REQUIRED_INPUT must not admit on a stale scalar"
    assert not nd["t1_qualified"], "ISA-0721 negative control: a missing E[r] must fail the T1 gate"
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
    assert tier_for(dict(base, cycles_seen=1), SNAP, ref_date=ref) == "T1"
    assert tier_for(dict(base, cycles_seen=1, normalised_score=55), SNAP, ref_date=ref) == "T3"
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
    # ⚑ ISA-0721 MUST-FIRE: a PARTIAL E[r] (re-rate refused) no longer admits (was: pass).
    assert um["er"]["state"] == "BLOCKED_PARTIAL_ER" and not um["er"]["pass"], um["er"]
    assert not um["t1_qualified"] and um["evidence_confirmed"] is False, um
    assert um["evidence"]["basis"]["route"] == "unconfirmed_er_unmeasured", um["evidence"]
    # ── ISA-0616/0619 C-1 CURRENT ADMISSIBILITY ──────────────────────────────────────────────
    ca = current_admissibility
    ok_ = ca(SNAP, ref)
    assert ok_["verdict"] == "ADMISSIBLE" and ok_["admissible"], ok_
    assert _evaluate(dict(base), SNAP, ref)["t1_qualified"] is True     # POSITIVE: one sighting passes
    # MUST-FIRE: no snapshot / stale / future / incomplete / legacy / unknown / undeclared / event
    assert ca({}, ref)["verdict"] == "NO_SNAPSHOT"
    assert not _evaluate(dict(base), None, ref)["t1_qualified"], "no snapshot must not admit"
    assert ca(dict(SNAP, snapshot_as_of="2026-07-12"), ref)["verdict"] == "STALE"
    assert ca(dict(SNAP, snapshot_as_of=None), ref)["verdict"] == "STALE"
    assert ca(dict(SNAP, snapshot_as_of="2026-07-16"), ref)["verdict"] == "INVALID_PIT"
    assert ca(dict(SNAP, revisions_score=None), ref)["verdict"] == "INCOMPLETE"
    assert ca(dict(SNAP, forward_axis_score=None), ref)["verdict"] == "INCOMPLETE"
    assert ca(dict(SNAP, score_definition_hash="0" * 16), ref)["verdict"] == "INCOMPARABLE_LEGACY_DEFINITION"
    assert ca(dict(SNAP, score_definition_basis="HISTORICAL_WRITE_UNSTAMPED",
                   score_definition_hash=None), ref)["verdict"] == "INCOMPARABLE_UNKNOWN_DEFINITION"
    assert ca(SNAP, ref, current={"hash": _cur["hash"], "declared": False})["verdict"] == \
        "UNDECLARED_CURRENT_DEFINITION"
    assert ca(SNAP, ref, event_state={"material": True, "ref": "profit warning"})["verdict"] == "EVENT_INVALIDATED"
    assert ca(dict(SNAP, forward_axis_score=40.0), ref)["verdict"] == "BELOW_FLOOR"
    assert ca(dict(SNAP, forward_axis_score=140.0), ref)["verdict"] == "INCONSISTENT"
    # NEGATIVE CONTROLS: a measured-zero revisions score is present (not missing); the midnight
    # tolerance admits yesterday's run snapshot; a non-material event does not block
    assert ca(dict(SNAP, revisions_score=0.0, forward_axis_score=95.0), ref)["verdict"] in ("ADMISSIBLE", "BELOW_FLOOR")
    assert ca(dict(SNAP, snapshot_as_of="2026-07-14"), ref)["verdict"] == "ADMISSIBLE"
    assert ca(SNAP, ref, event_state={"material": False})["verdict"] == "ADMISSIBLE"
    # NO PERSISTENCE: the verdict has no history input at all - a panel full of FAIL sightings is
    # irrelevant, and the retired persistence rule cannot answer
    import inspect as _insp
    assert "panel" not in str(_insp.signature(current_admissibility)), "C-1 must not read history"
    assert entry_stability("ANY")["verdict"] == "RETIRED_ISA0616"
    # ISA-0616 c1_lookup: exact key; declared alias both directions; no map / no alias -> None
    _m = {"ONT.L": {"verdict": "ADMISSIBLE"}, "AVGO": {"verdict": "STALE"}}
    assert c1_lookup(_m, "AVGO") == ({"verdict": "STALE"}, "AVGO")
    assert c1_lookup(_m, "ONT", aliases={"ONT": "ONT.L"})[1] == "ONT.L"
    assert c1_lookup({"ONT": {"verdict": "X"}}, "ONT.L", aliases={"ONT": "ONT.L"})[1] == "ONT"
    assert c1_lookup(_m, "ONT") == (None, None), "no declared alias -> never a guessed suffix"
    assert c1_lookup(None, "AVGO") == (None, None)
    # (gate_status_for_screen_row is defined below this self-test block — its D-24 label
    #  assertion lives in tests_jul2026/test_d24_expected_return.py)
    print("t1_gates SELF-TEST OK (A5 v3 — evidence sizing, tenure gate removed; + D-24 er_status; + ISA-0721 admission)")
    return 0


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
    # ISA-0721: missing / partial E[r] cannot admit; the screen label says so rather than PASS.
    if er is None:
        reasons.append("E[r] missing")
    elif er_unmeasured:
        reasons.append("E[r] partial")
    elif er < er_floor:
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


if __name__ == "__main__":
    _selftest()
