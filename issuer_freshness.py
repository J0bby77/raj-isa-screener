#!/usr/bin/env python3
"""
issuer_freshness.py — ISA-0713: issuer/event evidence freshness and held-stock coverage.

Authority: ISA_Engineering_Rules.md (R2.10, R4.2, R4.3, R4.8, R5.1, R5.5, R14.2, R20.1);
ChatGPT Astra 6 Audit/ISA_Wave2_Consolidated_BuildSpec_19Sep2026.md §5.10, §7.1-7.3, §10,
§13, §14, §15.1. Canonical store: `issuer_reviews_[mmm]_[yyyy].json`.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
The framework treated DISCOVERY as if it were COVERAGE. The mid-month intelligence brief and
the Gmail Investment News corpus find what was published to them; nothing ever asked, per held
name, "what has this issuer said since the underwriting that authorises the capital I am
holding?". So "no matching email" read as "no issuer news" — R2.10's absence-of-evidence error,
applied to every pound of direct-stock capital.

The same root produced the earnings-date gap. A null from one adapter is an UNESTABLISHED FACT,
not an established absence, and the framework had no ladder distinguishing NOT_YET_ANNOUNCED
(a real state of the world) from UNRESOLVED_DATA_FAILURE (a state of the data).

The known instance is QBTS: August underwriting used $16.21 against a last close of $20.76, and
the existing-model asymmetry moved 2.1374x -> 1.6690x — across the 2.0 eligibility gate. The
staleness alone changed the verdict.

⚑ THIS MODULE DECIDES NOTHING ABOUT CAPITAL BY ITSELF. It establishes typed evidence and typed
refusals; the capital-authorisation boundary consumes them. Research output populates structured
evidence; it never issues BUY or SELL (BuildSpec §5.10).

⚑ NO NETWORK. Every retrieval is INJECTED as a list of attempt records. A module that fetches
cannot be tested deterministically and cannot be replayed, and replay is the whole point of a
point-in-time evidence contract.

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["issuer_freshness"] = False` — `coverage_verdict()`
returns DISABLED, which reads as UNKNOWN and never as covered.
"""
from __future__ import annotations

import calendar
import datetime
import json
import os
import re
import tempfile
from typing import Optional

# Execution-ledger instrument (ISA-0699). Best-effort by construction: it is a no-op when
# isa_policy.V2_FLAGS["execution_ledger"] is False and it never raises into the caller. The
# call STAYS IN THE CODE when the flag is off — removing it is what makes it droppable, and a
# producer with no mark site can never be observed, which reads as "did not run" when the truth
# is "not instrumented" (R2.10).
try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"

# ── earnings / event date states (BuildSpec §10) ────────────────────────────────────────
CONFIRMED = "CONFIRMED"
ESTIMATED = "ESTIMATED"
NOT_YET_ANNOUNCED = "NOT_YET_ANNOUNCED"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
UNRESOLVED_DATA_FAILURE = "UNRESOLVED_DATA_FAILURE"
DATE_STATES = (CONFIRMED, ESTIMATED, NOT_YET_ANNOUNCED, SOURCE_CONFLICT,
               UNRESOLVED_DATA_FAILURE)

# ⚑ NOT_YET_ANNOUNCED IS AN ESTABLISHED FACT STATE, NOT A DATA FAILURE. Collapsing the two is
# the defect: one says the issuer has not set a date, the other says we could not find out.
ESTABLISHED_DATE_STATES = (CONFIRMED, ESTIMATED, NOT_YET_ANNOUNCED)

# ── the source-resolution ladder (§5.10). Rung 1 is not the answer; it is the first rung. ──
LADDER = (
    {"rung": 1, "id": "PRIMARY_STRUCTURED_ADAPTER",
     "what": "the approved primary structured adapter (e.g. the provider the pre-run already calls)",
     "quality": "STRUCTURED", "may_confirm": True},
    {"rung": 2, "id": "ALTERNATE_STRUCTURED_CALENDAR",
     "what": "an alternate approved structured calendar/source",
     "quality": "STRUCTURED", "may_confirm": True},
    {"rung": 3, "id": "ISSUER_IR_FILING_EXCHANGE",
     "what": "issuer IR page, official filing or exchange notice, found directly or by targeted search",
     "quality": "PRIMARY", "may_confirm": True},
    {"rung": 4, "id": "CORROBORATING_FINANCIAL_SOURCE",
     "what": "a high-quality independent financial source, where the primary date is not yet published",
     "quality": "SECONDARY", "may_confirm": False},
)
LADDER_BY_ID = {r["id"]: r for r in LADDER}

# R6.x source hierarchy for issuer evidence. A blog, a social post or a generic AI summary
# cannot establish a capital-critical fact without stronger support — it is recorded as
# evidence of a claim, never as the fact.
SOURCE_HIERARCHY = (
    "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
    "HIGH_QUALITY_INDEPENDENT_FINANCIAL_REPORTING",
    "APPROVED_SECONDARY_DATABASE_CORROBORATION_ONLY",
)
NON_ESTABLISHING_SOURCES = ("BLOG", "SOCIAL", "GENERIC_AI_SUMMARY", "NEWSLETTER_DIGEST")

# ── canonical event taxonomy (§5.10). No free-text "material" boolean decides capital. ────
FULL_UNDERWRITING = "FULL_UNDERWRITING"

EVENT_TYPES = {
    "EARNINGS_RESULTS": {
        "definition": "a scheduled periodic results release for a reporting period",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",
                             "HIGH_QUALITY_INDEPENDENT_FINANCIAL_REPORTING"),
        "invalidates": FULL_UNDERWRITING,
        "routes": ("main", "vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "GUIDANCE_CHANGE": {
        "definition": "the issuer changes forward guidance, or withdraws it",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": ("expected_return", "revenue_path", "thesis_state", "evidence_state"),
        "routes": ("main", "vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "FINANCING_OR_CAPITAL_STRUCTURE": {
        "definition": "equity or debt raise, dilution, buyback, refinancing or going-concern change",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": ("share_count", "bottleneck_fv_per_share", "fv_asymmetry",
                        "dilution", "balance_sheet"),
        "routes": ("main", "vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "M_AND_A_OR_MAJOR_ASSET_TRANSACTION": {
        "definition": "acquisition, disposal, merger or a transaction material to the asset base",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": FULL_UNDERWRITING,
        "routes": ("main", "vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "CLINICAL_READOUT": {
        "definition": "a trial or study readout, interim or final, on a named programme",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": FULL_UNDERWRITING,
        "routes": ("vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "REGULATORY_DECISION": {
        "definition": "an approval, rejection, licence, sanction or regulatory ruling naming the issuer",
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": FULL_UNDERWRITING,
        "routes": ("main", "vci", "held"),
        "refresh_required_before_new_capital": True,
    },
    "NAMED_VCI_CATALYST_RESOLUTION": {
        "definition": ("the specific catalyst the current VCI underwriting names has materially "
                       "resolved, either way"),
        "evidence_sources": ("ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY",),
        "invalidates": FULL_UNDERWRITING,
        "routes": ("vci",),
        "refresh_required_before_new_capital": True,
    },
}

# ── the bounded research pack (§5.10). Fixed questions, so coverage is countable. ─────────
RESEARCH_QUESTIONS = (
    "issuer/filing releases since last valid underwriting",
    "guidance change",
    "financing/dilution/capital-structure change",
    "M&A/asset transaction",
    "named thesis/catalyst development",
    "thesis-specific operating metrics named in the current underwriting",
    "evidence contradicting the current thesis",
    "next earnings / named event state",
)

# ── capital dispositions this module may produce (§5.10 refusal semantics) ───────────────
ALLOW = "ALLOW"
BLOCK_NEW_CAPITAL = "BLOCK_NEW_CAPITAL"
EVIDENCE_EXCEPTION = "EVIDENCE_EXCEPTION"
CARRY_EVENT_EXPLICITLY = "CARRY_EVENT_EXPLICITLY"
CAPITAL_DISPOSITIONS = (ALLOW, BLOCK_NEW_CAPITAL, EVIDENCE_EXCEPTION, CARRY_EVENT_EXPLICITLY)


def _flag() -> bool:
    try:
        import isa_policy
        return bool(isa_policy.V2_FLAGS.get("issuer_freshness", True))
    except Exception:                                                   # noqa: BLE001
        return True


def _d(x) -> Optional[datetime.date]:
    if x in (None, ""):
        return None
    if isinstance(x, datetime.date):
        return x
    return datetime.date.fromisoformat(str(x)[:10])


# ═════════════════════════════════════════════════════════════════════════════════════════
# 1. EARNINGS / EVENT DATE — THE SOURCE-RESOLUTION LADDER
# ═════════════════════════════════════════════════════════════════════════════════════════

def attempt(rung_id, *, source, value=None, retrieved_at=None, published_at=None,
            not_announced=False, failed=False, note=None, url=None) -> dict:
    """One rung's ATTEMPT — what was asked, of whom, when, and what came back.

    `value=None` is deliberately ambiguous at this level and is resolved only by the two
    explicit flags: `not_announced` means the source states the issuer has not set a date (a
    fact about the WORLD), `failed` means the source could not be read (a fact about the
    DATA). A null with neither flag is a bare miss — it contributes nothing and cannot, on its
    own, terminate the ladder."""
    if rung_id not in LADDER_BY_ID:
        raise ValueError("unknown ladder rung %r — the ladder is declared, not extended "
                         "ad hoc (R4.8). Known: %s" % (rung_id, ", ".join(LADDER_BY_ID)))
    return {"rung_id": rung_id, "rung": LADDER_BY_ID[rung_id]["rung"],
            "quality": LADDER_BY_ID[rung_id]["quality"],
            "may_confirm": LADDER_BY_ID[rung_id]["may_confirm"],
            "source": source, "url": url,
            "value": (_d(value).isoformat() if value else None),
            "retrieved_at": retrieved_at, "published_at": published_at,
            "not_announced": bool(not_announced), "failed": bool(failed), "note": note}


def resolve_event_date(attempts, *, ticker=None) -> dict:
    """§5.10 — the typed next-event date, from the ladder. PURE: no network, no clock.

    ⚑ A PRIMARY-PROVIDER NULL IS NEVER THE FINAL ANSWER. It is rung 1 returning nothing, and
    the ladder continues. The five terminal states are mutually exclusive and one of them is
    always produced — silence is not an outcome (R2.10)."""
    atts = list(attempts or [])
    if not atts:
        return {"state": UNRESOLVED_DATA_FAILURE, "ticker": ticker, "date": None,
                "resolved_by": None, "attempts": [], "conflict": None,
                "why": ("no source was attempted at all. That is an unestablished fact, not an "
                        "absence of an event (R2.10) — it must not read as 'no upcoming "
                        "earnings'.")}
    dated = [a for a in atts if a.get("value")]
    # A conflict is between sources that are ENTITLED to establish the date. A secondary
    # estimate disagreeing with a confirmed primary is not a conflict; it is a worse source.
    confirmers = [a for a in dated if a.get("may_confirm")]
    distinct_confirmed = sorted({a["value"] for a in confirmers})
    if len(distinct_confirmed) > 1:
        return {"state": SOURCE_CONFLICT, "ticker": ticker, "date": None, "resolved_by": None,
                "attempts": atts, "conflict": distinct_confirmed,
                "why": ("%d sources entitled to establish the date disagree (%s). R6.2: publish "
                        "the disagreement, never blend or silently pick one."
                        % (len(distinct_confirmed), ", ".join(distinct_confirmed)))}
    if distinct_confirmed:
        best = sorted(confirmers, key=lambda a: a["rung"])[0]
        return {"state": CONFIRMED, "ticker": ticker, "date": distinct_confirmed[0],
                "resolved_by": best["rung_id"], "attempts": atts, "conflict": None,
                "why": "confirmed at rung %d (%s)" % (best["rung"], best["source"])}
    if dated:                       # only rung-4-class sources have a date
        best = sorted(dated, key=lambda a: a["rung"])[0]
        return {"state": ESTIMATED, "ticker": ticker, "date": best["value"],
                "resolved_by": best["rung_id"], "attempts": atts, "conflict": None,
                "why": ("only a corroborating source carries a date (rung %d, %s); the issuer "
                        "has not published it, so this is an ESTIMATE and is typed as one"
                        % (best["rung"], best["source"]))}
    # ⚑ The whole point of the item: a stated "not announced" is an ESTABLISHED FACT and
    # outranks a bare miss or a failure. An issuer that has not scheduled its results has told
    # us something true.
    stated = [a for a in atts if a.get("not_announced")]
    if stated:
        best = sorted(stated, key=lambda a: a["rung"])[0]
        return {"state": NOT_YET_ANNOUNCED, "ticker": ticker, "date": None,
                "resolved_by": best["rung_id"], "attempts": atts, "conflict": None,
                "why": ("%s states the issuer has not announced a date. This is an ESTABLISHED "
                        "fact about the world, NOT a data failure, and it does not block."
                        % best["source"])}
    failures = [a for a in atts if a.get("failed")]
    return {"state": UNRESOLVED_DATA_FAILURE, "ticker": ticker, "date": None,
            "resolved_by": None, "attempts": atts, "conflict": None,
            "why": ("the ladder terminated without establishing anything: %d source(s) failed "
                    "and %d returned a bare null. A provider null is an unestablished fact "
                    "(R2.10) — it may not be read as 'no upcoming earnings'."
                    % (len(failures), len(atts) - len(failures) - len(stated)))}


# ═════════════════════════════════════════════════════════════════════════════════════════
# 2. NEXT GUARANTEED REVIEW — FROM THE DECLARED SCHEDULER, NOT A CONSTANT
# ═════════════════════════════════════════════════════════════════════════════════════════
#
# ⚑ THE FIVE-DAY RULE IS EXPLICITLY REJECTED (BuildSpec §5.10, §20). "Earnings within 5 days"
# is a number with no relationship to when this framework can actually act. The question that
# matters is: will a guaranteed review happen before the event? If not, THIS decision must
# carry and underwrite that event exposure before BUY/ADD.
#
# The calendar is the DECLARED one (SCHEDULED_TASKS_SETUP.md), read under the AND/ordinal
# semantics ESTABLISHED by evidence in `schedule_semantics_evidence.py` (verdict H1, negative
# control passed; ⚑ its ISA-0477 label was NEVER ISSUED in the canonical register - ISA-0703
# owns that correction - so the ARTEFACT is cited, not the id): each
# task fires exactly once per month on its ordinal occurrence.
#
# ⚑ THE INTRA-MONTH STOCK REVIEW IS DELIBERATELY ABSENT. It has a Run Context but NO row in
# the scheduled-task table, so it is on-demand. An on-demand review is not a guarantee, and
# counting it would be exactly the optimism this control exists to remove (R4.3).

GUARANTEED_REVIEWS = (
    {"task": "monthly-isa-portfolio-review", "window": (1, 7), "weekday": calendar.SUNDAY,
     "routes": ("main", "held", "vci"),
     "what": "the monthly portfolio review — the whole-book capital decision"},
    {"task": "vci-monthly-value-chain-intelligence", "window": (8, 14), "weekday": calendar.SUNDAY,
     "routes": ("vci",),
     "what": "the VCI run — the asymmetric sleeve's capital decision"},
)

NOT_GUARANTEED = (
    {"surface": "intra-month stock review",
     "why": ("Run_Context_Intramonth_Stock_Review.md exists but the surface has NO row in "
             "SCHEDULED_TASKS_SETUP.md, so it runs on demand. On-demand is not a guarantee.")},
    {"surface": "isa-mid-month-intelligence-brief",
     "why": ("its cron is recorded UNVERIFIED on the declared surface (R4.8), and it is a "
             "DISCOVERY surface that issues no capital decision — see coverage_verdict().")},
)


def _ordinal_dates(year, month, window, weekday):
    lo, hi = window
    last = calendar.monthrange(year, month)[1]
    return [datetime.date(year, month, d) for d in range(lo, min(hi, last) + 1)
            if datetime.date(year, month, d).weekday() == weekday]


def next_guaranteed_review(as_of, *, route="held", horizon_months=3) -> dict:
    """The next date on which a review that can change this route's capital is CERTAIN to run."""
    as_of = _d(as_of)
    cands = []
    for spec in GUARANTEED_REVIEWS:
        if route not in spec["routes"]:
            continue
        y, m = as_of.year, as_of.month
        for _ in range(horizon_months + 1):
            for dt in _ordinal_dates(y, m, spec["window"], spec["weekday"]):
                if dt > as_of:
                    cands.append((dt, spec))
                    break
            else:
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                continue
            break
    if not cands:
        return {"date": None, "task": None, "route": route, "as_of": as_of.isoformat(),
                "why": ("no guaranteed review for route %r within %d months — UNKNOWN, and a "
                        "decision may not assume one will happen (R4.3)"
                        % (route, horizon_months))}
    dt, spec = sorted(cands, key=lambda x: x[0])[0]
    return {"date": dt.isoformat(), "task": spec["task"], "route": route,
            "as_of": as_of.isoformat(), "what": spec["what"],
            "basis": ("declared cron in SCHEDULED_TASKS_SETUP.md, ordinal (AND) semantics "
                      "ESTABLISHED by schedule_semantics_evidence.py (verdict H1; its "
                      "ISA-0477 label was never issued in the register - ISA-0703)"),
            "not_counted": [n["surface"] for n in NOT_GUARANTEED],
            "why": "next certain %s is %s" % (spec["task"], dt.isoformat())}


# ═════════════════════════════════════════════════════════════════════════════════════════
# 3. EVENTS AND SINCE-LAST-UNDERWRITING INVALIDATION
# ═════════════════════════════════════════════════════════════════════════════════════════

def event(event_id, event_type, *, occurred_at=None, expected_at=None, announced_at=None,
          session_tz="UTC", source=None, source_quality=None, url=None, route=None,
          status="OBSERVED", thesis_id=None, prior_event_id=None, next_event_id=None,
          note=None) -> dict:
    """§7.3 — one canonical event row. The TYPE is from the declared taxonomy or it is
    refused: a free-text 'material' boolean may never decide capital (BuildSpec §5.10)."""
    if event_type not in EVENT_TYPES:
        raise ValueError(
            "unknown event type %r. The taxonomy is DECLARED (%s); an event that fits none of "
            "them is a change to the taxonomy, not a string (R4.8/R14.2)."
            % (event_type, ", ".join(sorted(EVENT_TYPES))))
    spec = EVENT_TYPES[event_type]
    return {"event_id": event_id, "event_type": event_type,
            "definition": spec["definition"],
            "announced_at": announced_at, "expected_at": expected_at,
            "occurred_at": occurred_at, "session_tz": session_tz,
            "source": source, "source_quality": source_quality, "url": url,
            "route": route, "status": status,
            "invalidation_scope": spec["invalidates"],
            "refresh_required_before_new_capital": spec["refresh_required_before_new_capital"],
            "thesis_id": thesis_id, "prior_event_id": prior_event_id,
            "next_event_id": next_event_id, "note": note}


def invalidation(events, *, underwriting_cutoff) -> dict:
    """Which events POSTDATE the underwriting that authorises the current capital, and what
    each one invalidates — full underwriting, or named fields only.

    ⚑ The cutoff is the UNDERWRITING date, not the last run date. A run that read nothing new
    does not refresh anything."""
    cutoff = _d(underwriting_cutoff)
    since, fields, full = [], set(), []
    for e in events or []:
        when = _d(e.get("occurred_at")) or _d(e.get("announced_at"))
        if when is None or cutoff is None or when <= cutoff:
            continue
        since.append(e)
        scope = e.get("invalidation_scope")
        if scope == FULL_UNDERWRITING:
            full.append(e["event_id"])
        else:
            fields.update(scope or ())
    if full:
        verdict = "RE_UNDERWRITE_FULLY"
    elif fields:
        verdict = "REFRESH_NAMED_FIELDS"
    else:
        verdict = "SURVIVES_UNCHANGED"
    return {"underwriting_cutoff": (cutoff.isoformat() if cutoff else None),
            "n_events_since": len(since),
            "events_since": [e["event_id"] for e in since],
            "invalidates_full": full, "invalidates_fields": sorted(fields),
            "verdict": verdict,
            "why": {"RE_UNDERWRITE_FULLY":
                    "at least one event since the cutoff invalidates the whole underwriting",
                    "REFRESH_NAMED_FIELDS":
                    "named fields are invalidated; the rest of the underwriting survives",
                    "SURVIVES_UNCHANGED":
                    ("no event postdates the underwriting cutoff. ⚑ This is only a valid "
                     "conclusion where the issuer was actually SEARCHED — see "
                     "coverage_verdict(): 'no matching email' is not 'no issuer news'.")}[verdict]}


# ═════════════════════════════════════════════════════════════════════════════════════════
# 4. THE ISSUER REVIEW RECORD (§7.1) AND THE RESEARCH PACK (§5.10)
# ═════════════════════════════════════════════════════════════════════════════════════════

def research_pack(ticker, answers, *, sources=None) -> dict:
    """The bounded pack: the EIGHT fixed questions, answered or explicitly unanswered.

    Fixed questions make coverage COUNTABLE. `answers` maps question -> {answer, source,
    source_quality, url}. A question with no answer is recorded as unanswered, not dropped.
    ⚑ The pack populates structured evidence. It NEVER issues BUY or SELL."""
    rows, unanswered = [], []
    for q in RESEARCH_QUESTIONS:
        a = (answers or {}).get(q)
        if not a:
            unanswered.append(q)
            rows.append({"question": q, "answered": False, "answer": None,
                         "source": None, "source_quality": None, "url": None})
            continue
        sq = a.get("source_quality")
        rows.append({"question": q, "answered": True, "answer": a.get("answer"),
                     "source": a.get("source"), "source_quality": sq, "url": a.get("url"),
                     "establishing": sq in SOURCE_HIERARCHY,
                     "non_establishing_reason": (
                         None if sq in SOURCE_HIERARCHY else
                         ("%s cannot establish a capital-critical fact without stronger "
                          "support; recorded as a CLAIM, not as the fact" % sq))})
    return {"ticker": ticker, "questions": rows, "n_questions": len(RESEARCH_QUESTIONS),
            "n_answered": len(RESEARCH_QUESTIONS) - len(unanswered),
            "unanswered": unanswered, "sources_declared": list(sources or []),
            "complete": not unanswered,
            "issues_capital_action": False}


def issuer_review(ticker, *, route, instrument_id=None, exchange=None, currency=None,
                  prior_underwriting_id=None, underwriting_cutoff=None,
                  decision_at=None, intended_execution_at=None, retrieved_at=None,
                  events=None, next_event_date=None, next_catalyst=None,
                  pack=None, raw_inputs=None, superseded_inputs=None,
                  conflicts=None, consumers=None, review_id=None) -> dict:
    """§7.1 — one issuer review. The disposition every held direct stock must carry."""
    inv = invalidation(events or [], underwriting_cutoff=underwriting_cutoff)
    ned = next_event_date or {}
    rid = review_id or "IR-%s-%s" % (_d(decision_at or datetime.date.today()).isoformat(),
                                     str(ticker).upper())
    # Survival of the current capital authority: the strictest of the two readings.
    if inv["verdict"] == "RE_UNDERWRITE_FULLY":
        survives = "REQUIRES_FULL_RE_UNDERWRITING"
    elif inv["verdict"] == "REFRESH_NAMED_FIELDS":
        survives = "REQUIRES_PARTIAL_REFRESH"
    elif pack is not None and not pack.get("complete"):
        survives = "UNESTABLISHED"
    elif pack is None:
        survives = "UNESTABLISHED"
    else:
        survives = "SURVIVES_UNCHANGED"
    return {
        "schema_version": SCHEMA_VERSION, "review_id": rid,
        "instrument_id": instrument_id or str(ticker).upper(), "ticker": str(ticker).upper(),
        "exchange": exchange, "currency": currency, "route": route,
        "prior_underwriting_id": prior_underwriting_id,
        "underwriting_cutoff": inv["underwriting_cutoff"],
        "decision_at": decision_at, "intended_execution_at": intended_execution_at,
        "retrieved_at": retrieved_at,
        "source_publication_at": (ned.get("attempts") or [{}])[0].get("published_at"),
        "pit_state": ("POINT_IN_TIME" if retrieved_at and decision_at else "UNSTAMPED"),
        "events": list(events or []), "raw_inputs": list(raw_inputs or []),
        "superseded_inputs": list(superseded_inputs or []),
        "evidence_conflicts": list(conflicts or []),
        "source_coverage": {"pack_complete": bool(pack and pack.get("complete")),
                            "n_answered": (pack or {}).get("n_answered", 0),
                            "unanswered": (pack or {}).get("unanswered", list(RESEARCH_QUESTIONS))},
        "next_event_state": ned.get("state", UNRESOLVED_DATA_FAILURE),
        "next_event_date": ned.get("date"),
        "next_event_resolved_by": ned.get("resolved_by"),
        "next_catalyst": next_catalyst,
        "invalidation": inv,
        "capital_authority_survives": survives,
        "pack": pack,
        "consumers": list(consumers or []),
    }


# ═════════════════════════════════════════════════════════════════════════════════════════
# 5. POPULATION RECONCILIATION — N EXPECTED, N DISPOSITIONED (§7.2)
# ═════════════════════════════════════════════════════════════════════════════════════════

def held_direct_stock_population(portfolio_doc) -> dict:
    """The EXPECTED population, denominated by the BROKER BOOK, never by the review store.

    ⚑ ISA-0580's lesson, applied here: a metric whose denominator is its own numerator's
    source can only ever report 100%. The denominator is `portfolio_data_[mmm]_[yyyy].json`
    stocks[] — what is actually held — so a name nobody reviewed is VISIBLE."""
    rows = (portfolio_doc or {}).get("stocks") or []
    names, values = [], {}
    for s in rows:
        t = str(s.get("ticker") or "").upper()
        if not t:
            continue
        names.append(t)
        values[t] = s.get("value_gbp")
    meta = (portfolio_doc or {}).get("_meta") or {}
    return {"expected": sorted(set(names)), "n_expected": len(set(names)),
            "value_gbp": values,
            "total_gbp": round(sum(v for v in values.values() if isinstance(v, (int, float))), 2),
            "source": meta.get("source_file"), "data_date": meta.get("data_date"),
            "basis": "portfolio_data stocks[] — the broker book (R4.2), not the review store"}


def coverage_verdict(population, reviews, *, discovery_corpus_hits=None) -> dict:
    """§7.2 / §14 — every held direct stock has ONE issuer-specific evidence disposition.

    ⚑ MISSING IS AN ERROR, NOT 'NO NEWS'. And a mid-month brief or an email corpus may never
    certify per-holding coverage: `discovery_corpus_hits` is accepted only so the verdict can
    say IN WRITING that it was not used as proof."""
    if not _flag():
        return {"state": "DISABLED", "covered": False,
                "why": ("isa_policy.V2_FLAGS['issuer_freshness'] is False. DISABLED reads as "
                        "UNKNOWN and never as covered (R4.3).")}
    _fi_mark("issuer_freshness", "coverage_verdict")   # CAP-issuer_evidence_coverage producer
    expected = list((population or {}).get("expected") or [])
    by_ticker = {}
    for r in reviews or []:
        by_ticker.setdefault(str(r.get("ticker") or "").upper(), []).append(r)
    produced = [t for t in expected if by_ticker.get(t)]
    missing = [t for t in expected if not by_ticker.get(t)]
    unestablished = [t for t in produced
                     if any(x.get("capital_authority_survives") == "UNESTABLISHED"
                            for x in by_ticker[t])]
    extra = sorted(set(by_ticker) - set(expected))
    vals = (population or {}).get("value_gbp") or {}
    missing_gbp = round(sum(vals.get(t) or 0 for t in missing), 2)
    return {
        "state": "COMPLETE" if not missing else "INCOMPLETE",
        "covered": not missing,
        "n_expected": len(expected), "n_produced": len(produced),
        "expected": expected, "produced": produced,
        "missing": missing, "missing_gbp": missing_gbp,
        "unestablished": unestablished,
        "not_in_population": extra,
        "discovery_corpus": {
            "hits": list(discovery_corpus_hits or []),
            "counts_as_coverage": False,
            "why": ("the mid-month intelligence brief and the Gmail corpus are a DISCOVERY "
                    "surface: they find what was published to them. They cannot certify that "
                    "an issuer was checked, and 'no matching email' is not 'no issuer news' "
                    "(R2.10). Recorded for provenance only.")},
        "why": ("%d of %d held direct stocks carry an issuer-specific disposition"
                % (len(produced), len(expected))
                + ("" if not missing else
                   "; MISSING %s (GBP %.2f of held direct-stock capital). A missing name is an "
                   "ERROR, not 'no news'." % (", ".join(missing), missing_gbp))),
    }


# ═════════════════════════════════════════════════════════════════════════════════════════
# 6. CAPITAL DISPOSITION — TYPED REFUSALS, DIFFERENT BY ROUTE (§5.10)
# ═════════════════════════════════════════════════════════════════════════════════════════

def capital_disposition(review, *, action, as_of, route=None) -> dict:
    """What this evidence permits. `action` is NEW_CAPITAL (buy/add/top-up) or HOLD.

    Two asymmetries the BuildSpec insists on, and both are deliberate:
      * an unresolved MANDATORY event calendar BLOCKS new capital, but for an ordinary
        existing holding it raises an EVIDENCE EXCEPTION and blocks an unsupported ADD — it
        NEVER manufactures a SELL. Not knowing is not a reason to sell;
      * a known event falling BEFORE the next guaranteed review does not block; it must be
        CARRIED — underwritten explicitly in this decision — because no later run is
        guaranteed to catch it first.
    """
    route = route or review.get("route")
    as_of_d = _d(as_of)
    state = review.get("next_event_state")
    reasons, disposition = [], ALLOW
    new_capital = str(action).upper() in ("NEW_CAPITAL", "BUY", "ADD", "TOP_UP")

    surv = review.get("capital_authority_survives")
    if surv == "REQUIRES_FULL_RE_UNDERWRITING":
        reasons.append("an event since the underwriting cutoff invalidates the whole underwriting")
        disposition = BLOCK_NEW_CAPITAL if new_capital else EVIDENCE_EXCEPTION
    elif surv == "REQUIRES_PARTIAL_REFRESH":
        reasons.append("named underwriting fields are invalidated and not yet refreshed: %s"
                       % ", ".join(review.get("invalidation", {}).get("invalidates_fields") or []))
        disposition = BLOCK_NEW_CAPITAL if new_capital else EVIDENCE_EXCEPTION
    elif surv == "UNESTABLISHED":
        reasons.append("the bounded issuer research pack is incomplete, so nothing about this "
                       "issuer's current state is established")
        disposition = BLOCK_NEW_CAPITAL if new_capital else EVIDENCE_EXCEPTION

    if state in (UNRESOLVED_DATA_FAILURE, SOURCE_CONFLICT):
        reasons.append("next-event date is %s — an unestablished mandatory event calendar" % state)
        if new_capital:
            disposition = BLOCK_NEW_CAPITAL
        elif disposition == ALLOW:
            disposition = EVIDENCE_EXCEPTION
    elif state == NOT_YET_ANNOUNCED:
        reasons.append("the issuer has not announced a date — an ESTABLISHED fact, not a "
                       "failure, and it does not block")

    nxt = next_guaranteed_review(as_of_d, route=route or "held")
    ev_date = _d(review.get("next_event_date"))
    nxt_date = _d(nxt.get("date"))
    event_before_review = bool(ev_date and nxt_date and ev_date <= nxt_date)
    if event_before_review and disposition == ALLOW and new_capital:
        disposition = CARRY_EVENT_EXPLICITLY
        reasons.append("a known event on %s falls on or before the next guaranteed review (%s, "
                       "%s), so THIS decision must carry and underwrite that event exposure "
                       "before BUY/ADD — no later run is guaranteed to see it first"
                       % (ev_date.isoformat(), nxt.get("task"), nxt.get("date")))
    return {"disposition": disposition, "action": str(action).upper(), "route": route,
            "as_of": as_of_d.isoformat(), "ticker": review.get("ticker"),
            "review_id": review.get("review_id"),
            "next_event_state": state, "next_event_date": review.get("next_event_date"),
            "next_guaranteed_review": nxt,
            "event_before_next_guaranteed_review": event_before_review,
            "may_sell_on_this_evidence": False,
            "sell_note": ("this capability NEVER manufactures a SELL. A source failure on an "
                          "existing holding is an evidence exception, not a disposal signal."),
            "reasons": reasons or ["current issuer evidence establishes no impediment"]}


# ═════════════════════════════════════════════════════════════════════════════════════════
# 7. STORE
# ═════════════════════════════════════════════════════════════════════════════════════════

def path_for(month_label, here=None) -> str:
    return os.path.join(here or HERE, "issuer_reviews_%s.json" % month_label)


def new_doc(month_label, *, as_of=None, build_id=None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "month_label": month_label,
            "as_of": as_of or datetime.date.today().isoformat(), "build_id": build_id,
            "reviews": [], "population": None, "coverage": None}


def write(doc, here=None, month_label=None) -> str:
    """Atomic write. REFUSES an incomplete population — the artefact exists to prove coverage,
    and one that records a gap as if it were a result defeats its own purpose."""
    here = here or HERE
    month_label = month_label or doc.get("month_label")
    cov = doc.get("coverage") or {}
    if cov.get("state") == "INCOMPLETE":
        raise ValueError(
            "issuer_freshness: refusing to write %s — coverage is INCOMPLETE (%s). %s"
            % (month_label, ", ".join(cov.get("missing") or []), cov.get("why")))
    path = path_for(month_label, here)
    fd, tmp = tempfile.mkstemp(dir=here, prefix=".issuer_reviews_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def load(month_label, here=None):
    p = path_for(month_label, here)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# ═════════════════════════════════════════════════════════════════════════════════════════
# SELFTEST — the BuildSpec §13 challenge list is the test list
# ═════════════════════════════════════════════════════════════════════════════════════════

def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name
                  + (("  -- " + str(detail)[:220]) if not cond else ""))
        if not cond:
            fails.append(name)

    A = attempt
    # ── §13.1 — provider null must never read as "no upcoming earnings" ────────────────
    r = resolve_event_date([A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", value=None)])
    ok("⚑ MUST-FIRE §13.1: a bare primary-provider null is UNRESOLVED_DATA_FAILURE, NEVER an "
       "established 'no upcoming earnings' — that read is the whole defect",
       r["state"] == UNRESOLVED_DATA_FAILURE and r["date"] is None, r)

    # ── §13.4 — not-yet-announced is a FACT, not a failure ─────────────────────────────
    r = resolve_event_date([A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", value=None),
                            A("ISSUER_IR_FILING_EXCHANGE", source="issuer IR",
                              not_announced=True, retrieved_at="2026-09-20T10:00Z")])
    ok("⚑ MUST-FIRE §13.4: provider null + issuer states no date announced -> "
       "NOT_YET_ANNOUNCED, an ESTABLISHED fact, distinct from a data failure",
       r["state"] == NOT_YET_ANNOUNCED and r["resolved_by"] == "ISSUER_IR_FILING_EXCHANGE", r)
    ok("NOT_YET_ANNOUNCED is classified as ESTABLISHED, so a consumer cannot treat it as a gap",
       NOT_YET_ANNOUNCED in ESTABLISHED_DATE_STATES
       and UNRESOLVED_DATA_FAILURE not in ESTABLISHED_DATE_STATES)

    # ── §15.1 — provider null, issuer confirms -> CONFIRMED ────────────────────────────
    r = resolve_event_date([A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", value=None),
                            A("ISSUER_IR_FILING_EXCHANGE", source="issuer IR",
                              value="2026-10-28", url="https://ir.example/x")])
    ok("MUST-FIRE: the ladder CONTINUES past a null and confirms at rung 3",
       r["state"] == CONFIRMED and r["date"] == "2026-10-28"
       and r["resolved_by"] == "ISSUER_IR_FILING_EXCHANGE", r)

    # ── §13.3 — credible sources disagree ──────────────────────────────────────────────
    r = resolve_event_date([A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", value="2026-10-28"),
                            A("ALTERNATE_STRUCTURED_CALENDAR", source="calendar B",
                              value="2026-11-04")])
    ok("⚑ MUST-FIRE §13.3: two sources ENTITLED to establish the date disagree -> "
       "SOURCE_CONFLICT with both dates published and NO silent choice (R6.2)",
       r["state"] == SOURCE_CONFLICT and r["date"] is None
       and r["conflict"] == ["2026-10-28", "2026-11-04"], r)
    r = resolve_event_date([A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", value="2026-10-28"),
                            A("CORROBORATING_FINANCIAL_SOURCE", source="press", value="2026-11-04")])
    ok("NEGATIVE CONTROL: a SECONDARY source disagreeing with a confirming source is not a "
       "conflict — it is a worse source, and treating it as a conflict would block on noise",
       r["state"] == CONFIRMED and r["date"] == "2026-10-28", r)
    r = resolve_event_date([A("CORROBORATING_FINANCIAL_SOURCE", source="press", value="2026-11-04")])
    ok("a date carried ONLY by a corroborating source is typed ESTIMATED, never CONFIRMED",
       r["state"] == ESTIMATED and r["date"] == "2026-11-04", r)
    ok("NEGATIVE CONTROL: no attempt at all is a FAILURE, not a clean answer — 'nothing was "
       "checked' and 'checked and clear' must not look the same (R2.10)",
       resolve_event_date([])["state"] == UNRESOLVED_DATA_FAILURE)
    try:
        A("MADE_UP_RUNG", source="x")
        ok("NEGATIVE CONTROL: an undeclared ladder rung is REFUSED", False)
    except ValueError:
        ok("NEGATIVE CONTROL: an undeclared ladder rung is REFUSED, not silently accepted", True)

    # ── next guaranteed review: computed, and NOT a five-day constant ──────────────────
    n = next_guaranteed_review("2026-09-20", route="held")
    ok("the next guaranteed HELD review is the 1st Sunday of October (2026-10-04), computed "
       "from the declared cron under the ordinal semantics established by "
       "schedule_semantics_evidence.py",
       n["date"] == "2026-10-04" and n["task"] == "monthly-isa-portfolio-review", n)
    ok("a VCI name's next guaranteed review is the EARLIER of the monthly review and the VCI "
       "run — the route decides the calendar, not a constant",
       next_guaranteed_review("2026-10-05", route="vci")["task"]
       == "vci-monthly-value-chain-intelligence"
       and next_guaranteed_review("2026-10-05", route="vci")["date"] == "2026-10-11",
       next_guaranteed_review("2026-10-05", route="vci"))
    ok("⚑ the intra-month review is EXCLUDED from the guarantee and the exclusion is stated: "
       "it has a Run Context but no scheduled-task row, so it is on-demand",
       any("intra-month" in s["surface"] for s in NOT_GUARANTEED)
       and "intra-month stock review" in n["not_counted"])
    # ⚑ The rejected five-day rule, checked MECHANICALLY rather than by reading the prose
    # that rejects it. Any day-count constant would be the arbitrary rule creeping back in, so
    # the test is structural: no int literal 5 in executable code, no timedelta arithmetic, no
    # module-level constant named like a day count. `resolve_event_date` and
    # `capital_disposition` compare DATES to the SCHEDULER, which is the whole point.
    import ast as _ast
    # The scan covers the CAPABILITY, not its test: a control that scans itself would be
    # tripped by the very literal it is looking for, which is self-reference, not evidence.
    _src = open(os.path.join(HERE, "issuer_freshness.py"), encoding="utf-8").read()
    _src = _src[:_src.index("def _selftest(")]
    _tree = _ast.parse(_src)
    _int5 = [n for n in _ast.walk(_tree)
             if isinstance(n, _ast.Constant) and isinstance(n.value, int)
             and not isinstance(n.value, bool) and n.value == 5]
    _dayconsts = [t.id for n in _tree.body if isinstance(n, _ast.Assign)
                  for t in n.targets if isinstance(t, _ast.Name)
                  and re.search(r"DAY|WINDOW_D|HORIZON_D", t.id)]
    ok("⚑ NEGATIVE CONTROL: the arbitrary five-day earnings rule cannot creep back — no int "
       "literal 5 in executable code, no timedelta arithmetic and no day-count constant. The "
       "question is whether a guaranteed review happens first, not how many days away it is "
       "(BuildSpec §5.10/§20)",
       not _int5 and not _dayconsts and "timedelta" not in _src,
       {"int5": len(_int5), "day_consts": _dayconsts})

    # ── event taxonomy and invalidation ────────────────────────────────────────────────
    try:
        event("E1", "SOMETHING_MATERIAL")
        ok("NEGATIVE CONTROL: a free-text event type is REFUSED", False)
    except ValueError:
        ok("⚑ NEGATIVE CONTROL: a free-text 'material' event type is REFUSED — no unbounded "
           "prose field may silently decide capital", True)
    e_full = event("E-EARN", "EARNINGS_RESULTS", occurred_at="2026-09-10", source="issuer IR")
    e_part = event("E-GUID", "GUIDANCE_CHANGE", occurred_at="2026-09-10", source="issuer IR")
    ok("every declared event type carries its own invalidation scope, evidence requirement and "
       "affected routes — the taxonomy is data, not prose",
       e_full["invalidation_scope"] == FULL_UNDERWRITING
       and "expected_return" in e_part["invalidation_scope"]
       and all(set(v) >= {"definition", "evidence_sources", "invalidates", "routes",
                          "refresh_required_before_new_capital"} for v in EVENT_TYPES.values()))
    inv = invalidation([e_full], underwriting_cutoff="2026-08-09")
    ok("⚑ MUST-FIRE §15.1: an event AFTER the underwriting cutoff fires full re-underwriting",
       inv["verdict"] == "RE_UNDERWRITE_FULLY" and inv["events_since"] == ["E-EARN"], inv)
    ok("a named-field event refreshes those fields and leaves the rest standing — an "
       "all-or-nothing invalidation would make the control unusable and it would be waived",
       invalidation([e_part], underwriting_cutoff="2026-08-09")["verdict"]
       == "REFRESH_NAMED_FIELDS")
    ok("NEGATIVE CONTROL: an event BEFORE the cutoff invalidates nothing — the control must be "
       "able to say 'survives', or it says nothing",
       invalidation([e_full], underwriting_cutoff="2026-09-30")["verdict"] == "SURVIVES_UNCHANGED")


    # ── population coverage — §13.2, the one that exposes every pound of held capital ──
    _pf = {"_meta": {"source_file": "AJ Bell ISA Portfolio 31-Aug-26.xlsx",
                     "data_date": "31-Aug-2026"},
           "stocks": [{"ticker": "MU", "value_gbp": 4952.14},
                      {"ticker": "AVGO", "value_gbp": 4657.66},
                      {"ticker": "ABCL", "value_gbp": 1775.39},
                      {"ticker": "ONT", "value_gbp": 1549.68},
                      {"ticker": "COCO", "value_gbp": 1214.60},
                      {"ticker": "QBTS", "value_gbp": 863.82}]}
    pop = held_direct_stock_population(_pf)
    ok("the EXPECTED population is denominated by the broker book, not by the review store — "
       "ISA-0580's lesson: a denominator taken from the numerator's source always reports 100%",
       pop["n_expected"] == 6 and pop["total_gbp"] == 15013.29
       and "broker book" in pop["basis"], pop)

    def _rev(t, complete=True, verdict="SURVIVES_UNCHANGED"):
        pk = research_pack(t, {q: {"answer": "none", "source": "issuer IR",
                                   "source_quality": "ISSUER_IR_REGULATORY_EXCHANGE_PRIMARY"}
                               for q in RESEARCH_QUESTIONS} if complete else {})
        return issuer_review(t, route="held", underwriting_cutoff="2026-08-09",
                             decision_at="2026-09-20", retrieved_at="2026-09-20T10:00Z",
                             events=[], pack=pk,
                             next_event_date=resolve_event_date(
                                 [A("ISSUER_IR_FILING_EXCHANGE", source="IR",
                                    not_announced=True)]))

    _five = [_rev(t) for t in ("MU", "AVGO", "ABCL", "ONT", "COCO")]
    cov = coverage_verdict(pop, _five, discovery_corpus_hits=["QBTS mentioned in mid-month brief"])
    ok("⚑ MUST-FIRE §13.2 / §15.1: 5 dispositions against 6 held names is INCOMPLETE and names "
       "the missing issuer and its GBP — a missing name is an ERROR, never 'no news'",
       cov["state"] == "INCOMPLETE" and cov["missing"] == ["QBTS"]
       and cov["missing_gbp"] == 863.82, cov)
    ok("⚑ NEGATIVE CONTROL §13.2: a mid-month-brief hit for the MISSING name does NOT close "
       "the gap — the corpus is a discovery surface and cannot certify per-holding coverage",
       cov["state"] == "INCOMPLETE"
       and cov["discovery_corpus"]["counts_as_coverage"] is False
       and cov["discovery_corpus"]["hits"], cov["discovery_corpus"])
    cov6 = coverage_verdict(pop, _five + [_rev("QBTS")])
    ok("POSITIVE CONTROL: N -> N is COMPLETE, so the control can pass as well as block",
       cov6["state"] == "COMPLETE" and cov6["n_produced"] == cov6["n_expected"] == 6, cov6)
    ok("a review for a name that is NOT held is reported rather than silently counted",
       coverage_verdict(pop, _five + [_rev("QBTS"), _rev("NVDA")])["not_in_population"] == ["NVDA"])
    ok("an INCOMPLETE research pack leaves the capital authority UNESTABLISHED — 'we looked at "
       "some of it' is not an established issuer state",
       _rev("MU", complete=False)["capital_authority_survives"] == "UNESTABLISHED")
    ok("the research pack is the EIGHT fixed questions, and an unanswered one is recorded, not "
       "dropped — fixed questions are what make coverage countable",
       research_pack("MU", {})["n_questions"] == 8
       and len(research_pack("MU", {})["unanswered"]) == 8)
    ok("⚑ a newsletter/AI-summary answer is recorded as a CLAIM and flagged non-establishing, "
       "never as the fact (R6 source hierarchy)",
       research_pack("MU", {RESEARCH_QUESTIONS[0]: {"answer": "x", "source": "digest",
                                                    "source_quality": "NEWSLETTER_DIGEST"}}
                     )["questions"][0]["establishing"] is False)
    ok("the research pack declares in DATA that it issues no capital action",
       research_pack("MU", {})["issues_capital_action"] is False)

    # ── capital disposition — the two asymmetries ──────────────────────────────────────
    _conf = resolve_event_date([A("ISSUER_IR_FILING_EXCHANGE", source="IR", value="2026-10-02")])
    _r = issuer_review("COCO", route="held", underwriting_cutoff="2026-08-09",
                       decision_at="2026-09-20", retrieved_at="2026-09-20T10:00Z", events=[],
                       pack=research_pack("COCO", {q: {"answer": "none", "source": "IR",
                                                       "source_quality": SOURCE_HIERARCHY[0]}
                                                   for q in RESEARCH_QUESTIONS}),
                       next_event_date=_conf)
    d = capital_disposition(_r, action="NEW_CAPITAL", as_of="2026-09-20")
    ok("⚑ MUST-FIRE §13.5: a known event on 2026-10-02 falls BEFORE the next guaranteed review "
       "(2026-10-04), so new capital must CARRY and underwrite it explicitly — it is not "
       "silently bought and it is not blocked either",
       d["disposition"] == CARRY_EVENT_EXPLICITLY
       and d["event_before_next_guaranteed_review"] is True, d)
    _late = dict(_r, next_event_date="2026-10-20")
    ok("NEGATIVE CONTROL: the same event AFTER the next guaranteed review needs no special "
       "carry — the guaranteed review will see it first",
       capital_disposition(_late, action="NEW_CAPITAL", as_of="2026-09-20")["disposition"] == ALLOW)

    _fail = issuer_review("MU", route="held", underwriting_cutoff="2026-08-09",
                          decision_at="2026-09-20", retrieved_at="2026-09-20T10:00Z", events=[],
                          pack=research_pack("MU", {q: {"answer": "none", "source": "IR",
                                                        "source_quality": SOURCE_HIERARCHY[0]}
                                                    for q in RESEARCH_QUESTIONS}),
                          next_event_date=resolve_event_date(
                              [A("PRIMARY_STRUCTURED_ADAPTER", source="yfinance", failed=True)]))
    dn = capital_disposition(_fail, action="NEW_CAPITAL", as_of="2026-09-20")
    dh = capital_disposition(_fail, action="HOLD", as_of="2026-09-20")
    ok("⚑ MUST-FIRE: an unresolved mandatory event calendar BLOCKS NEW CAPITAL",
       dn["disposition"] == BLOCK_NEW_CAPITAL, dn)
    ok("⚑ MUST-FIRE: the SAME failure on an existing holding is an EVIDENCE EXCEPTION, and this "
       "capability NEVER manufactures a SELL — not knowing is not a reason to sell",
       dh["disposition"] == EVIDENCE_EXCEPTION
       and dh["may_sell_on_this_evidence"] is False, dh)
    _na = issuer_review("ONT", route="held", underwriting_cutoff="2026-08-09",
                        decision_at="2026-09-20", retrieved_at="2026-09-20T10:00Z", events=[],
                        pack=research_pack("ONT", {q: {"answer": "none", "source": "IR",
                                                       "source_quality": SOURCE_HIERARCHY[0]}
                                                   for q in RESEARCH_QUESTIONS}),
                        next_event_date=resolve_event_date(
                            [A("ISSUER_IR_FILING_EXCHANGE", source="IR", not_announced=True)]))
    ok("⚑ NOT_YET_ANNOUNCED does NOT block new capital — an established fact about the world "
       "must not be treated as a data failure (the two-states defect, at the capital boundary)",
       capital_disposition(_na, action="NEW_CAPITAL", as_of="2026-09-20")["disposition"] == ALLOW)
    _stale = issuer_review("ABCL", route="vci", underwriting_cutoff="2026-08-09",
                           decision_at="2026-09-20", retrieved_at="2026-09-20T10:00Z",
                           events=[event("E-READOUT", "CLINICAL_READOUT",
                                         occurred_at="2026-08-10", source="issuer IR")],
                           pack=research_pack("ABCL", {q: {"answer": "x", "source": "IR",
                                                           "source_quality": SOURCE_HIERARCHY[0]}
                                                       for q in RESEARCH_QUESTIONS}),
                           next_event_date=_conf)
    ok("⚑ MUST-FIRE: a CLINICAL_READOUT after the cutoff blocks new capital until the thesis is "
       "re-underwritten — the ABCL case, mechanised",
       capital_disposition(_stale, action="NEW_CAPITAL", as_of="2026-09-20")["disposition"]
       == BLOCK_NEW_CAPITAL
       and _stale["capital_authority_survives"] == "REQUIRES_FULL_RE_UNDERWRITING")

    # ── store ─────────────────────────────────────────────────────────────────────────
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        doc = new_doc("sep_2026", as_of="2026-09-20", build_id="TB-TEST")
        doc["population"], doc["reviews"] = pop, _five
        doc["coverage"] = cov
        try:
            write(doc, here=td)
            ok("⚑ MUST-FIRE: the store REFUSES to write an INCOMPLETE coverage document", False)
        except ValueError as exc:
            ok("⚑ MUST-FIRE: the store REFUSES to write an INCOMPLETE coverage document — an "
               "artefact that records a gap as if it were a result defeats its own purpose",
               "INCOMPLETE" in str(exc) and "QBTS" in str(exc))
        doc["reviews"], doc["coverage"] = _five + [_rev("QBTS")], cov6
        p = write(doc, here=td)
        ok("POSITIVE CONTROL: a COMPLETE document writes, round-trips and carries its coverage "
           "denominator", os.path.basename(p) == "issuer_reviews_sep_2026.json"
           and load("sep_2026", here=td)["coverage"]["n_expected"] == 6)

    ok("R20.1: a reader can reconstruct input -> calculation -> constraint -> decision from the "
       "review row alone, without reading Python",
       set(_r) >= {"review_id", "ticker", "route", "underwriting_cutoff", "retrieved_at",
                   "next_event_state", "next_event_resolved_by", "invalidation",
                   "capital_authority_survives", "source_coverage", "events"})

    if verbose:
        print("\nissuer_freshness selftest: %d FAIL(s)%s"
              % (len(fails), (": " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


def month_label(d=None) -> str:
    d = _d(d) or datetime.date.today()
    return "%s_%d" % (calendar.month_abbr[d.month].lower(), d.year)


def portfolio_path(month, here=None) -> str:
    return os.path.join(here or HERE, "portfolio_data_%s.json" % month)


def report(month=None, here=None) -> dict:
    """The observability read (R20.1): what the current month's coverage actually is."""
    here = here or HERE
    month = month or month_label()
    doc = load(month, here)
    pf_path = portfolio_path(month, here)
    if not os.path.exists(pf_path):
        return {"month": month, "state": "NO_PORTFOLIO",
                "why": ("%s is absent, so the expected population cannot be established. "
                        "UNKNOWN, not zero (R4.3)." % os.path.basename(pf_path))}
    with open(pf_path, encoding="utf-8") as fh:
        pop = held_direct_stock_population(json.load(fh))
    if doc is None:
        return {"month": month, "state": "NO_REVIEWS", "population": pop,
                "why": ("%s has not been written. The %d held direct stock(s) carrying GBP "
                        "%.2f have NO issuer-specific disposition for this month — that is a "
                        "gap, not a clean result (ISA-0713)."
                        % (os.path.basename(path_for(month, here)), pop["n_expected"],
                           pop["total_gbp"]))}
    cov = coverage_verdict(pop, doc.get("reviews") or [])
    return {"month": month, "state": cov["state"], "population": pop, "coverage": cov,
            "why": cov["why"]}


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    m = None
    for i, a in enumerate(argv):
        if a == "--month" and i + 1 < len(argv):
            m = argv[i + 1]
    if "--next-review" in argv:
        route = "held"
        for i, a in enumerate(argv):
            if a == "--route" and i + 1 < len(argv):
                route = argv[i + 1]
        print(json.dumps(next_guaranteed_review(datetime.date.today(), route=route), indent=2))
        return 0
    print(json.dumps(report(m), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
