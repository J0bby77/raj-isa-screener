#!/usr/bin/env python3
"""
decision_ledger.py — the ISA decision ledger (redesign Part 1 §5 / CONTRACTS #7, H1/H3).

Logs EVERY monthly-review decision — buys, trims, sells, top-ups, holds, AND passes
(the road not taken is the signal) — capturing the scores / gates / flags AT decision
time, then appends monthly mark-to-market so per-signal information coefficient can be
measured at quarterly calibration. Append-only JSON; safe to import and call from the
review step. NOT yet wired into any scheduled run — pure additive new module.

Contract (CONTRACTS #7), per entry:
  date, ticker, route, decision{buy,trim,sell,top_up,PASS,hold},
  scores_at_decision{source_score,F,Q,V,native_score},
  gates_at_decision[], flags_at_decision[],
  thesis, catalyst|null, expected_review_date,
  mtm:[{date, price, return_pct, thesis_status}]
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, datetime

# Contract decision vocabulary. PASS is upper-case (the "road not taken"); the rest lower.
DECISIONS = {"buy", "trim", "sell", "top_up", "PASS", "hold"}
SCHEMA_VERSION = "1.1"    # ISA-0686 (20-Sep-2026): canonical decision identity


def _today() -> str:
    return datetime.date.today().isoformat()


def _norm_decision(decision: str) -> str:
    """Map any casing to the contract form (PASS upper, others lower). Raises on unknown."""
    d = str(decision).strip()
    norm = "PASS" if d.lower() == "pass" else d.lower()
    if norm not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}, got {decision!r}")
    return norm


def load_ledger(path: str) -> dict:
    """Return the ledger dict {schema_version, entries:[...]} — tolerant of a missing or
    legacy (bare-list) file so a first run never crashes."""
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    if isinstance(d, dict) and isinstance(d.get("entries"), list):
        d.setdefault("schema_version", SCHEMA_VERSION)
        return d
    if isinstance(d, list):                       # legacy bare list
        return {"schema_version": SCHEMA_VERSION, "entries": d}
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def save_ledger(ledger: dict, path: str) -> None:
    """Atomic write (tmp + replace) so a crash mid-write never corrupts the ledger."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, default=str)
    os.replace(tmp, path)


def entry_id(date: str, ticker: str, decision: str) -> str:
    return f"{date}::{(ticker or '').upper()}::{decision}"


# ═════════════════════════════════════════════════════════════════════════════════════════
# ISA-0686 — CANONICAL VCI DECISION CAPTURE
#
# The VCI deploy run wrote its deploy decisions to `vci_deploy_[mmm]_[yyyy].json` and NOTHING
# copied them into this ledger, so the sleeve's FIRST EVER new deployment (QBTS, 09-Aug-2026,
# bought at the size the framework specified the next day) reconciled as a trade with no
# recommendation behind it and was logged `bought_outside_framework` — a standing accusation
# against the operator for following the framework's own written instruction.
#
# R4.5: two paths call one function, or they are one function. The VCI route now reaches this
# ledger through `log_decision` — the same function the growth route uses via
# `checkpoint_d.log_top10` — and `assert_vci_captured` is the boundary contract (R5.1): a
# deploy artefact carrying a capital-effective name with no ledger entry is a breach, and it is
# checkable RETROSPECTIVELY over every month already on disk.
#
# ⚑ NEGATIVE CONTROL, and it is the whole point: this must not become a blanket amnesty. A
# genuinely unrecommended trade — a holding with no decision behind it — must STILL reconcile
# as bought_outside_framework. Capture records what the framework actually decided; it never
# back-fills a decision to make an execution look authorised.
# ═════════════════════════════════════════════════════════════════════════════════════════

VCI_ROUTE = "vci"


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def decision_id(date, ticker, route, decision, input_snapshot_id=None) -> str:
    """A stable, collision-resistant name for one decision.

    Includes the ROUTE (two routes may decide the same name on the same day) and the INPUT
    SNAPSHOT (the same route re-deciding on refreshed inputs is a different decision, which is
    what makes supersession meaningful rather than a silent overwrite)."""
    basis = "|".join([str(date), str(ticker).upper(), str(route), str(decision),
                      str(input_snapshot_id or "")])
    return "DEC-%s-%s-%s-%s" % (date, str(route).upper(), str(ticker).upper(), _sha12(basis))


def snapshot_id(path: str) -> str:
    """`<basename>@<sha256[:12]>` — names the exact artefact a decision was computed from.

    A decision whose inputs cannot be named cannot be shown to be current, which is the whole
    QBTS stale-price failure ($16.21 underwritten against a $20.76 last close)."""
    try:
        with open(path, "rb") as fh:
            return "%s@%s" % (os.path.basename(path),
                              hashlib.sha256(fh.read()).hexdigest()[:12])
    except Exception:                                                   # noqa: BLE001
        return "%s@UNREADABLE" % os.path.basename(str(path))


def normalise_vci_artefact(doc) -> list:
    """Return the scorecard rows from a VCI deploy artefact, whatever shape it is on disk.

    Three shapes exist in the delivered tree and all three are real production output:
      * dict keyed by ticker      (`vci_deploy_aug_2026.json`)
      * bare list of scorecards   (`vci_deploy_sep_2026.json`)
      * a single scorecard dict   (`vci_deploy_qbts_sep_2026.json`)
    Guessing between them is exactly the silent-miss class this item exists to close, so the
    shape is DECIDED on the presence of a `ticker` key, never on the month or the filename."""
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict) and r.get("ticker")]
    if isinstance(doc, dict):
        if doc.get("ticker"):                       # one scorecard
            return [doc]
        if isinstance(doc.get("candidates"), list):  # a vci_run_capture document
            return [r for r in doc["candidates"] if isinstance(r, dict) and r.get("ticker")]
        rows = []
        for k, v in doc.items():
            if isinstance(v, dict) and (v.get("ticker") or isinstance(k, str)):
                r = dict(v)
                r.setdefault("ticker", k)
                rows.append(r)
        return [r for r in rows if r.get("ticker")]
    return []


def vci_decision_for(row: dict, *, held=None, authority: str = "AUTHORISED") -> dict:
    """The recommendation a VCI scorecard row implies — mechanically, from declared fields.

    No prose is read and nothing is inferred from an email. `eligibility_reasons` travel with
    the decision so the ledger explains the refusal as well as the buy (R20.1)."""
    held = {str(t).upper() for t in (held or [])}
    t = str(row.get("ticker") or "").upper()
    eligible = bool(row.get("deploy_eligible"))
    manual = bool(row.get("require_manual_confirm"))
    flags, reasons = [], list(row.get("eligibility_reasons") or [])
    if str(authority).upper() != "AUTHORISED":
        # R18.5 / ISA-0629: an unsigned LIVE state may screen and score, but may not issue a
        # deploy decision. The evidence is captured; the capital action is NOT.
        flags.append("REFUSED_CAPITAL_AUTHORITY_%s" % str(authority).upper())
        return {"decision": "PASS", "flags": flags,
                "reasons": reasons + ["capital authority %s — no deploy decision issued"
                                      % authority]}
    if not eligible:
        return {"decision": "PASS", "flags": flags, "reasons": reasons or ["not deploy-eligible"]}
    if manual:
        flags.append("MANUAL_CONFIRM_REQUIRED")
        return {"decision": "hold", "flags": flags,
                "reasons": reasons + ["deploy-eligible but requires manual confirm — a point "
                                      "estimate or a crosscheck breach never auto-deploys"]}
    return {"decision": ("top_up" if t in held else "buy"), "flags": flags,
            "reasons": reasons or ["deploy-eligible"]}


def _gates_from_row(row: dict) -> list:
    """The gate readings that decided this row, quoted from the row itself (R4.2)."""
    g = []
    for k in ("acs", "acs_total", "fv_asymmetry", "fv_asymmetry_p25", "fv_floor", "floor_source",
              "fv_source", "p_thesis", "L", "expected_loss_pct_isa", "signal_count",
              "catalyst_type", "catalyst_domain", "price", "size_pct"):
        if k in row:
            g.append("%s=%s" % (k, row.get(k)))
    return g


def capture_vci_decisions(ledger_path, doc, *, source_path=None, date=None, build_id=None,
                          authority="AUTHORISED", held=None, event_review_id=None,
                          config_id=None, thesis="", dry_run=False,
                          retrospective=False, supersede_same_month=True) -> dict:
    """ISA-0686 — write every scored VCI name into the ledger through `log_decision`.

    Called at the moment the VCI artefact is persisted (`vci_run_capture.write`), so the
    artefact and the ledger cannot diverge. Idempotent: `log_decision(dedupe=True)` means a
    re-run of the same month writes nothing new."""
    rows = normalise_vci_artefact(doc)
    snap = snapshot_id(source_path) if source_path else None
    date = date or _today()
    out = {"source": source_path, "input_snapshot_id": snap, "date": date,
           "n_rows": len(rows), "captured": [], "skipped": [], "dry_run": bool(dry_run)}
    for row in rows:
        t = str(row.get("ticker") or "").upper()
        if not t:
            out["skipped"].append({"ticker": None, "why": "row carries no ticker"})
            continue
        v = vci_decision_for(row, held=held, authority=authority)
        # ── BuildSpec §5.9 — "changed inputs create a superseding decision with attributable
        # delta". A later workflow re-pricing the same name in the same month (the monthly
        # pre-run re-prices every VCI name at the Saturday live price) must not leave two
        # live decisions standing: the newer one supersedes, explicitly, on both rows. An
        # UNCHANGED input snapshot is the other half of the rule — it dedupes to the same
        # decision and supersedes nothing.
        prior = None
        if supersede_same_month:
            cur = current_decision(ledger_path, t, VCI_ROUTE)
            if (cur and str(cur.get("date") or "")[:7] == str(date)[:7]
                    and cur.get("input_snapshot_id") != snap):
                prior = cur.get("decision_id") or cur.get("_id")
        if dry_run:
            out["captured"].append({"ticker": t, "decision": v["decision"],
                                    "supersedes": prior,
                                    "decision_id": decision_id(date, t, VCI_ROUTE,
                                                               v["decision"], snap)})
            continue
        e = log_decision(
            ledger_path, t, VCI_ROUTE, v["decision"],
            # The deploy artefact calls it `acs`; the vci_run_capture document calls the
            # same number `acs_total`. One quantity, two field names on two real shapes —
            # read both rather than publishing a None that looks like an unscored name.
            scores={"source_score": row.get("vci_source_score"),
                    "native_score": row.get("acs", row.get("acs_total"))},
            gates=_gates_from_row(row),
            flags=(["ISA0686_VCI_CAPTURE"] + v["flags"]
                   + (["RETROSPECTIVE_CAPTURE_ISA0686",
                       "the decision itself is dated %s and is NOT back-dated in substance; "
                       "only the CAPTURE is retrospective" % date,
                       "captured from %s" % os.path.basename(str(source_path or "artefact")),
                       "execution is NOT asserted by this row — reconciliation is separate"]
                      if retrospective else [])),
            thesis=thesis or "", catalyst=(row.get("catalyst_type") or row.get("catalyst")),
            date=date, build_id=build_id, input_snapshot_id=snap,
            event_review_id=event_review_id, config_id=config_id,
            size_pct=row.get("size_pct"), reasons=v["reasons"],
            supersedes_decision_id=prior)
        out["captured"].append({"ticker": t, "decision": e["decision"],
                                "supersedes": prior,
                                "decision_id": e.get("decision_id")})
    return out


def capital_effective_names(doc, *, authority="AUTHORISED") -> list:
    """The names in a VCI artefact that carry a CAPITAL-EFFECTIVE recommendation.

    These are the rows that must exist in the ledger. A PASS is logged too (the road not
    taken), but its absence is not a capital breach — so the contract below is tight enough to
    fire on the failure that actually happened and loose enough not to cry on backlog."""
    out = []
    for row in normalise_vci_artefact(doc):
        v = vci_decision_for(row, authority=authority)
        if v["decision"] in BUY_LIKE or v["decision"] in SELL_LIKE:
            out.append(str(row.get("ticker")).upper())
    return sorted(set(out))


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def artefact_month(source_path) -> str | None:
    """`YYYY-MM` for a `*_[mmm]_[yyyy].json` artefact, else None.

    ⚑ The month matters. A ticker-presence-only contract passes a name whose decision was
    captured in a DIFFERENT month — ABCL's July buy would have absolved the August run that
    never recorded anything. "Somewhere in the ledger" is not "this run's decision"."""
    name = os.path.basename(str(source_path or ""))
    m = re.search(r"_(%s)_(\d{4})\.json$" % "|".join(_MONTHS), name, re.I)
    if not m:
        return None
    return "%s-%02d" % (m.group(2), _MONTHS.index(m.group(1).lower()) + 1)


def assert_vci_captured(ledger_path, doc, *, source_path=None, authority="AUTHORISED",
                        month=None) -> dict:
    """R5.1 boundary contract — every capital-effective name in a VCI artefact has a ledger
    decision on the VCI route IN THAT ARTEFACT'S MONTH. Retrospectively checkable over every
    month already on disk.

    When the month cannot be established from the filename the check falls back to ticker
    presence and SAYS SO, rather than silently applying the weaker rule (R2.10)."""
    need = capital_effective_names(doc, authority=authority)
    month = month or artefact_month(source_path)
    led = load_ledger(ledger_path)
    rows = [e for e in led.get("entries", []) if (e.get("route") or "").lower() == VCI_ROUTE]
    if month:
        have = {str(e.get("ticker") or "").upper()
                for e in rows if str(e.get("date") or "").startswith(month)}
        basis = "vci route, month %s" % month
    else:
        have = {str(e.get("ticker") or "").upper() for e in rows}
        basis = ("vci route, ANY month — the artefact filename carries no [mmm]_[yyyy], so the "
                 "weaker ticker-presence rule was applied")
    missing = [t for t in need if t not in have]
    return {"source": source_path, "month": month, "basis": basis,
            "n_capital_effective": len(need),
            "capital_effective": need, "missing": missing, "ok": not missing,
            "why": ("every capital-effective VCI name has a ledger decision (%s)" % basis
                    if not missing else
                    "ISA-0686 BREACH: %d deploy-eligible name(s) in %s have NO decision-ledger "
                    "entry on the %s (%s) — every execution of these reconciles as "
                    "bought_outside_framework"
                    % (len(missing), os.path.basename(str(source_path or "artefact")), basis,
                       ", ".join(missing)))}


def current_decision(ledger_path, ticker, route=None):
    """The latest NON-SUPERSEDED decision for a name — §5.2: a stale or superseded receipt
    cannot authorise. Returns None when nothing current exists."""
    t = str(ticker).upper()
    cands = [e for e in load_ledger(ledger_path).get("entries", [])
             if str(e.get("ticker") or "").upper() == t
             and (route is None or (e.get("route") or "").lower() == str(route).lower())
             and not e.get("superseded_by")]
    return sorted(cands, key=lambda e: str(e.get("date") or ""))[-1] if cands else None


def current_decisions(ledger_path, ticker, routes=None) -> list:
    """ISA-0716/0717 — every NON-SUPERSEDED decision on `ticker` (optionally within `routes`),
    oldest first. A name can carry several un-superseded rows on different routes or from
    months before supersession existed; the lifecycle engine supersedes all of them at a
    graduation, and execution reads the validity windows they define."""
    t = str(ticker).upper()
    rs = None if routes is None else {str(r).lower() for r in routes}
    cands = [e for e in load_ledger(ledger_path).get("entries", [])
             if str(e.get("ticker") or "").upper() == t
             and (rs is None or (e.get("route") or "").lower() in rs)
             and not e.get("superseded_by")]
    return sorted(cands, key=lambda e: str(e.get("date") or ""))


def record_observation(ledger_path, decision_id, *, observed_by, date=None, note=None) -> dict:
    """ISA-0716 §5.9 — a later workflow with UNCHANGED admissible inputs revalidates the SAME
    decision rather than writing a second one. The observation is appended to that decision."""
    ledger = load_ledger(ledger_path)
    for e in ledger["entries"]:
        if e.get("decision_id") == decision_id:
            e.setdefault("observations", []).append(
                {"date": date or _today(), "observed_by": observed_by,
                 "action": "REVALIDATED_UNCHANGED_INPUTS", "note": note})
            save_ledger(ledger, ledger_path)
            return e
    raise KeyError("record_observation: no decision %s in the ledger" % decision_id)


def log_decision(path, ticker, route, decision, scores=None, gates=None, flags=None,
                 thesis="", catalyst=None, expected_review_date=None, date=None,
                 dedupe=True, *, build_id=None, input_snapshot_id=None,
                 event_review_id=None, config_id=None, supersedes_decision_id=None,
                 size_pct=None, target_gbp=None, reasons=None, lifecycle=None,
                 also_supersedes=None) -> dict:
    """Append one decision entry and persist. Idempotent per (date, ticker, decision)
    when dedupe=True (re-running a review in the same month won't duplicate). Returns
    the entry dict (existing one if it was a dedupe hit)."""
    norm = _norm_decision(decision)
    date = date or _today()
    ledger = load_ledger(path)
    eid = entry_id(date, ticker, norm)
    if dedupe:
        for e in ledger["entries"]:
            if e.get("_id") == eid:
                return e
    s = scores or {}
    entry = {
        "_id": eid,
        # ── ISA-0686 (20-Sep-2026) — canonical decision identity ────────────────────────
        # A decision that cannot be named cannot be superseded, cited by a report, joined to
        # an execution, or refused when stale. `_id` was (date, ticker, decision) only: two
        # routes deciding the same name on the same day collide, and nothing recorded WHICH
        # build, WHICH input snapshot or WHICH event review produced it. These fields are
        # additive and default to None, so every existing caller is unchanged (R4.7).
        "decision_id": decision_id(date, ticker, route, norm, input_snapshot_id),
        "supersedes_decision_id": supersedes_decision_id,
        "superseded_by": None,
        "build_id": build_id,
        "input_snapshot_id": input_snapshot_id,
        "event_review_id": event_review_id,
        "config_id": config_id,
        "size_pct": size_pct,
        "target_gbp": target_gbp,
        "reasons": list(reasons or []),
        "date": date,
        "ticker": ticker,
        "route": route,
        "decision": norm,
        # The ledger records a RECOMMENDATION, never an assumed trade. Execution is confirmed
        # retrospectively from the broker ISA PDF/Excel the FOLLOWING month (reconcile_executions).
        "execution_status": "recommended",
        "executed_confirmed_date": None,
        "scores_at_decision": {
            "source_score": s.get("source_score"),
            "F": s.get("F"),
            "Q": s.get("Q"),
            "V": s.get("V"),
            "native_score": s.get("native_score"),
        },
        "gates_at_decision": list(gates or []),
        "flags_at_decision": list(flags or []),
        "thesis": thesis or "",
        "catalyst": catalyst,
        "expected_review_date": expected_review_date,
        "mtm": [],
    }
    # ISA-0716 (23-Sep-2026): the post-event lifecycle record travels ON the canonical decision,
    # so the route evaluated, the route chosen, the rejected alternatives and the executability
    # are readable from the ledger alone (R20.1). Absent for every other caller (R4.7: additive).
    if lifecycle is not None:
        entry["lifecycle"] = lifecycle
    # ⚑ ISA-0722 (23-Sep-2026) — THE DECISION BINDS ITS UNDERWRITING CASE AS IT IS WRITTEN (R4.11).
    #   A positive capital decision (buy / top_up) carries the id of the decision-month case in
    #   underwriting_cases.jsonl beside this ledger, plus an E[r] snapshot, so the ORIGINAL E[r] of
    #   a position can never again be lost to a column overwritten next month. No case -> the
    #   absence is RECORDED (flag + None), never filled; the decision is not refused for it.
    if norm in ("buy", "top_up"):
        try:
            import underwriting as _uw
            _c = _uw.case_for_decision(ticker, date,
                                       root=os.path.dirname(os.path.abspath(path)))
            entry["underwriting_case_id"] = (_c or {}).get("case_id")
            entry["er_at_decision"] = ({k: ((_c or {}).get("er") or {}).get(k)
                                        for k in ("state", "value_pct", "horizon_months",
                                                  "method_id")} if _c else None)
            if not _c:
                entry["flags_at_decision"].append("NO_UNDERWRITING_CASE_AT_DECISION")
        except Exception as _uwe:                                       # noqa: BLE001
            entry["underwriting_case_id"] = None
            entry["underwriting_case_error"] = "%s: %s" % (type(_uwe).__name__, _uwe)
            entry["flags_at_decision"].append("UNDERWRITING_CASE_LOOKUP_FAILED")
    # ISA-0686: supersession is recorded on BOTH rows, so a reader that finds the old
    # decision learns immediately that it no longer authorises anything (§5.2: a stale or
    # superseded receipt cannot authorise).
    _sup = [x for x in ([supersedes_decision_id] + list(also_supersedes or [])) if x]
    if _sup:
        for e in ledger["entries"]:
            if e.get("decision_id") in _sup or e.get("_id") in _sup:
                if not e.get("superseded_by"):
                    e["superseded_by"] = entry["decision_id"]
                    e["superseded_on"] = date
    ledger["entries"].append(entry)
    ledger["schema_version"] = SCHEMA_VERSION
    save_ledger(ledger, path)
    return entry


def append_mtm(path, ticker, price, return_pct=None, thesis_status=None,
               date=None, decision=None) -> dict | None:
    """Append a monthly mark-to-market point to the latest matching entry for `ticker`
    (optionally filtered to a specific decision). Returns the updated entry, or None if
    no matching entry exists yet."""
    date = date or _today()
    ledger = load_ledger(path)
    cand = [e for e in ledger["entries"]
            if (e.get("ticker") or "").upper() == (ticker or "").upper()
            and (decision is None or e.get("decision") == decision)]
    if not cand:
        return None
    entry = sorted(cand, key=lambda e: e.get("date", ""))[-1]
    entry.setdefault("mtm", []).append({
        "date": date,
        "price": price,
        "return_pct": return_pct,
        "thesis_status": thesis_status,
    })
    save_ledger(ledger, path)
    return entry


BUY_LIKE = {"buy", "top_up", "etf_tactical"}   # B4/P3 (18-Jul-26): Category-8 action vocab
SELL_LIKE = {"sell", "trim"}


def _as_qty_map(holdings):
    """Accept {ticker: quantity} OR a bare [ticker] list (presence only -> quantity None)."""
    if isinstance(holdings, dict):
        return {str(k).strip().upper(): v for k, v in holdings.items()}
    return {str(t).strip().upper(): None for t in (holdings or [])}


def reconcile_executions(path, current_holdings, prior_holdings=None, date=None) -> dict:
    """Confirm — from BROKER TRUTH (next month's actual holdings) — which recommendations were taken.
    The system NEVER assumes execution. Pass `current_holdings` as {ticker: quantity} (or a bare ticker
    list = presence only). `prior_holdings` ({ticker: quantity}) lets top_up/trim be confirmed by a
    QUANTITY change; without it (or without quantities) top_up/trim are left `execution_unconfirmed`
    because presence alone cannot prove a size change. Reconciles entries still `recommended`:
      buy    : executed if the ticker is now held, else not_executed
      sell   : executed if the ticker is now NOT held, else not_executed
      top_up : executed if current qty > prior qty (needs both quantities), else not_executed / unconfirmed
      trim   : executed if current qty < prior qty (needs both quantities), else not_executed / unconfirmed
      PASS/hold : no_action_expected
    Returns counts by new status."""
    date = date or _today()
    cur = _as_qty_map(current_holdings)
    prior = _as_qty_map(prior_holdings) if prior_holdings is not None else None
    ledger = load_ledger(path)
    counts = {"confirmed_executed": 0, "not_executed": 0,
              "execution_unconfirmed": 0, "no_action_expected": 0}
    for e in ledger["entries"]:
        if e.get("execution_status") != "recommended":
            continue
        d = e.get("decision")
        t = str(e.get("ticker") or "").strip().upper()
        held_now = t in cur
        if d == "buy":
            status = "confirmed_executed" if held_now else "not_executed"
        elif d == "sell":
            status = "confirmed_executed" if not held_now else "not_executed"
        elif d in ("top_up", "trim"):
            cq = cur.get(t)
            pq = prior.get(t) if prior is not None else None
            if prior is None or cq is None or pq is None:
                status = "execution_unconfirmed"   # presence alone cannot prove a size change
            elif d == "top_up":
                status = "confirmed_executed" if cq > pq else "not_executed"
            else:  # trim
                status = "confirmed_executed" if cq < pq else "not_executed"
        else:  # PASS / hold — nothing to execute
            status = "no_action_expected"
        e["execution_status"] = status
        if status == "confirmed_executed":
            e["executed_confirmed_date"] = date
        counts[status] = counts.get(status, 0) + 1
    save_ledger(ledger, path)
    return counts


def default_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "decision_ledger.json")


def summary(path: str) -> dict:
    """Counts by decision + total entries — for a quick run-context / email line."""
    ledger = load_ledger(path)
    by = {}
    for e in ledger["entries"]:
        by[e.get("decision")] = by.get(e.get("decision"), 0) + 1
    return {"total": len(ledger["entries"]), "by_decision": by,
            "schema_version": ledger.get("schema_version")}


def main():
    ap = argparse.ArgumentParser(description="ISA decision ledger (log / mark-to-market / summary).")
    ap.add_argument("--path", required=True, help="decision_ledger.json path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="log a decision")
    lg.add_argument("--ticker", required=True)
    lg.add_argument("--route", default="growth")
    lg.add_argument("--decision", required=True, help="buy|trim|sell|top_up|PASS|hold")
    lg.add_argument("--thesis", default="")
    lg.add_argument("--catalyst", default=None)
    lg.add_argument("--review-date", default=None)

    mt = sub.add_parser("mtm", help="append a mark-to-market point")
    mt.add_argument("--ticker", required=True)
    mt.add_argument("--price", type=float, required=True)
    mt.add_argument("--return-pct", type=float, default=None)
    mt.add_argument("--thesis-status", default=None)

    sub.add_parser("summary", help="print ledger summary")

    # ISA-0686 — capture a VCI artefact's decisions. The SUPPORTED, repeatable route for the
    # retrospective repair as well as for a live run, because a one-off script is a thing
    # somebody has to remember (R14.1).
    cv = sub.add_parser("capture-vci", help="ISA-0686: capture a VCI deploy/run artefact")
    cv.add_argument("--artefact", required=True)
    cv.add_argument("--date", required=True, help="the DECISION date (the run date), YYYY-MM-DD")
    cv.add_argument("--held", default="", help="comma-separated tickers held at the run date")
    cv.add_argument("--build-id", default=None)
    cv.add_argument("--authority", default="AUTHORISED")
    cv.add_argument("--thesis", default="")
    cv.add_argument("--retrospective", action="store_true",
                    help="stamp RETROSPECTIVE_CAPTURE_ISA0686 provenance on every row")
    cv.add_argument("--dry-run", action="store_true")

    ck = sub.add_parser("check-vci", help="ISA-0686: boundary contract over one artefact")
    ck.add_argument("--artefact", required=True)

    a = ap.parse_args()
    if a.cmd == "log":
        e = log_decision(a.path, a.ticker, a.route, a.decision, thesis=a.thesis,
                         catalyst=a.catalyst, expected_review_date=a.review_date)
        print(f"LOGGED {e['_id']}")
    elif a.cmd == "mtm":
        e = append_mtm(a.path, a.ticker, a.price, return_pct=a.return_pct,
                       thesis_status=a.thesis_status)
        print(f"MTM_APPENDED {e['_id']}" if e else f"NO_ENTRY for {a.ticker}")
    elif a.cmd == "summary":
        print(json.dumps(summary(a.path), indent=2))
    elif a.cmd == "capture-vci":
        with open(a.artefact, encoding="utf-8") as fh:
            doc = json.load(fh)
        held = [t for t in re.split(r"[,\s]+", a.held or "") if t]
        res = capture_vci_decisions(
            a.path, doc, source_path=a.artefact, date=a.date, build_id=a.build_id,
            authority=a.authority, held=held,
            thesis=(a.thesis or ""), dry_run=a.dry_run,
            retrospective=bool(a.retrospective))
        print(json.dumps(res, indent=2))
        chk = assert_vci_captured(a.path, doc, source_path=a.artefact, authority=a.authority)
        print("CONTRACT " + ("OK" if chk["ok"] else "BREACH") + ": " + chk["why"])
        return 0 if (a.dry_run or chk["ok"]) else 1
    elif a.cmd == "check-vci":
        with open(a.artefact, encoding="utf-8") as fh:
            doc = json.load(fh)
        chk = assert_vci_captured(a.path, doc, source_path=a.artefact)
        print(json.dumps(chk, indent=2))
        return 0 if chk["ok"] else 1


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Transaction-truth reconciliation  (extract_transactions.py, 26-Jul-2026)
# ---------------------------------------------------------------------------
# reconcile_executions() above infers execution from month-to-month HOLDINGS
# deltas. That can confirm presence, but it cannot see the date, the fill price,
# the dealing cost, or a top-up/trim without quantities on both sides. When a
# monthly AJ Bell transaction export is available, this function reconciles from
# the actual dealing record instead, and falls back to the holdings-delta logic
# per-entry for anything the transactions do not cover. Purely additive: the
# original function is untouched and remains the fallback path.

def _txn_window(transactions, ticker, start_date, end_date=None):
    """Trades on `ticker` in (start_date, end_date]. Recommendations are made at
    a review; only trades AFTER that review can be that recommendation."""
    t = (ticker or "").strip().upper()
    out = []
    for x in transactions or []:
        if str(x.get("ticker") or "").strip().upper() != t:
            continue
        if x.get("type") not in ("buy", "sell"):
            continue
        d = str(x.get("date") or "")
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        out.append(x)
    return sorted(out, key=lambda x: x.get("date") or "")


def _stamp_execution(entry, txn):
    """Record what actually happened, not merely that something happened."""
    entry["executed_date"] = txn.get("date")
    entry["executed_quantity"] = txn.get("quantity")
    entry["executed_price"] = txn.get("price")
    entry["executed_amount_gbp"] = txn.get("amount_gbp")
    entry["executed_reference"] = txn.get("reference")      # ISA-0701: execution identity (idempotency)
    # ISA-0722 §6.2: an execution-point E[r] is a SEPARATE record and is not computed today.
    #   Recorded as NOT_COMPUTED - never synthesised, never written over the decision's E[r].
    entry.setdefault("execution_er", {"state": "NOT_COMPUTED",
                                      "why": "no execution-time re-underwriting exists (BuildSpec §6.2)"})
    entry["dealing_cost_gbp"] = txn.get("cost_gbp")
    entry["dealing_cost_pct"] = txn.get("cost_pct")
    entry["execution_source"] = "transaction_record"
    limit = entry.get("limit_price") or entry.get("execution", {}).get("limit_price")
    fill = txn.get("price")
    if limit and fill:
        try:
            entry["slippage_vs_limit_pct"] = round((fill / float(limit) - 1) * 100, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            entry["slippage_vs_limit_pct"] = None


def reconcile_executions_from_transactions(path, transactions, current_holdings,
                                           prior_holdings=None, date=None, persist=True):
    """Confirm recommendations against the ACTUAL dealing record (broker truth).

    `transactions`: list of dicts as produced by
    extract_transactions.run()["executed_trades"] — date, ticker, type
    ('buy'/'sell'), quantity, price, amount_gbp, cost_gbp, cost_pct.

    Per still-`recommended` entry:
      * a matching trade after the recommendation date  -> confirmed_executed,
        stamped with date / quantity / fill price / dealing cost / slippage
      * no matching trade, but transactions cover the period -> not_executed
      * transactions unavailable for that ticker         -> fall back to the
        holdings-delta inference (same semantics as reconcile_executions)

    Also returns `off_framework`: trades with no corresponding ledger
    recommendation, i.e. action taken outside the framework. That is the input
    the A13 override log needs, and holdings-diffing cannot produce it reliably.

    Returns {counts, confirmed, off_framework, fallback_used}."""
    date = date or _today()
    cur = _as_qty_map(current_holdings)
    prior = _as_qty_map(prior_holdings) if prior_holdings is not None else None
    ledger = load_ledger(path)
    txns = list(transactions or [])
    have_txns = bool(txns)
    # ISA-0684 (R4.3/V-1, R2.10). `have_txns` says an export EXISTS; it does not
    # say the export COVERS the recommendation. The September NTAP buy was
    # labelled "declined" because the newest export ended in August -- absence of
    # evidence was rendered as evidence of refusal. The coverage window makes the
    # difference visible so the two can never again produce the same output.
    _txn_dates = sorted(str(x.get("date") or "") for x in txns if x.get("date"))
    txn_from = _txn_dates[0] if _txn_dates else None
    txn_to = _txn_dates[-1] if _txn_dates else None

    counts = {"confirmed_executed": 0, "not_executed": 0,
              "execution_unconfirmed": 0, "no_action_expected": 0,
              "superseded_unexecuted": 0}
    confirmed, fallback_used = [], []
    matched_uids = set()

    # ── ISA-0717 (23-Sep-2026) — EXECUTION REVALIDATES AGAINST THE DECISION IN FORCE ────────
    # A decision authorises a trade only inside its VALIDITY WINDOW: from its own date up to
    # (not including) the next decision on the same name on ANY route, or its supersession,
    # whichever is first. Windows on one name are therefore disjoint, so ONE trade can link at
    # most ONE decision, and a stale proposal (ABCL's 09-Aug top_up behind a 13-Sep PASS) can
    # never be confirmed by a later trade — that is the proposal-becomes-authority class
    # ISA-0701 closed for obligations, closed here for execution itself.
    _all = list(enumerate(ledger["entries"]))
    _by_tk = {}
    for _i, _e in _all:
        _by_tk.setdefault(str(_e.get("ticker") or "").strip().upper(), []).append((_i, _e))
    _did_date = {(_e.get("decision_id") or _e.get("_id")): str(_e.get("date") or "")
                 for _i, _e in _all}

    def _window_end(idx, ent):
        tk = str(ent.get("ticker") or "").strip().upper()
        d0 = str(ent.get("date") or "")
        nxt = [str(e2.get("date") or "") for i2, e2 in _by_tk.get(tk, [])
               if i2 != idx and (str(e2.get("date") or "") > d0
                                 or (str(e2.get("date") or "") == d0 and i2 > idx))]
        ends = sorted(nxt)[:1]
        if ent.get("superseded_by"):
            ends.append(str(ent.get("superseded_on") or _did_date.get(ent["superseded_by"]) or d0))
        return min(ends) if ends else None

    for _idx, e in _all:
        if e.get("execution_status") != "recommended":
            continue
        # Historic entries were written with mixed casing ("BUY", "HOLD"), so
        # normalise before matching -- a case-sensitive compare would route a
        # HOLD into the trade path and mark it not_executed.
        d_raw = e.get("decision")
        d = str(d_raw).strip().lower() if d_raw is not None else None
        t = str(e.get("ticker") or "").strip().upper()
        rec_date = str(e.get("date") or "")

        if d in (None, "none", "", "pass", "hold"):
            e["execution_status"] = "no_action_expected"
            counts["no_action_expected"] += 1
            continue

        buy_like = {x.lower() for x in BUY_LIKE}
        sell_like = {x.lower() for x in SELL_LIKE}
        want = "buy" if d in buy_like else ("sell" if d in sell_like else None)
        if want is None:
            # Unknown decision verb: do not guess an execution for it.
            e["execution_status"] = "no_action_expected"
            counts["no_action_expected"] += 1
            continue
        _end = _window_end(_idx, e)
        e["validity_end"] = _end
        cand = _txn_window(txns, t, rec_date, date) if (have_txns and want) else []
        cand = [x for x in cand if x.get("type") == want
                and (_end is None or str(x.get("date") or "") < _end)
                and (x.get("date"), t, x.get("type"), x.get("reference")) not in matched_uids]

        if cand:
            _stamp_execution(e, cand[0])
            e["execution_status"] = "confirmed_executed"
            e["executed_confirmed_date"] = date
            counts["confirmed_executed"] += 1
            confirmed.append({"ticker": t, "decision": d,
                              "decision_id": e.get("decision_id") or e.get("_id"),
                              "route": e.get("route"),
                              "executed_date": cand[0].get("date"),
                              "price": cand[0].get("price"),
                              "amount_gbp": cand[0].get("amount_gbp")})
            for x in cand[:1]:
                matched_uids.add((x.get("date"), t, x.get("type"),
                                  x.get("reference")))
            continue

        if _end is not None and _end <= str(date):
            # ISA-0717: the window CLOSED (a later decision or a supersession) with no trade in
            # it. The decision can no longer be executed; it is not "declined" (Raj did not
            # refuse it — the framework replaced it), so it must not feed the override log.
            e["execution_status"] = "superseded_unexecuted"
            counts["superseded_unexecuted"] += 1
            continue

        if have_txns:
            # ISA-0684. No matching trade. That means "declined" ONLY if the
            # export actually covers the window in which the trade could have
            # happened -- [rec_date, run date]. Three cases, three outputs:
            #   export reaches the run date  -> evidence complete  -> not_executed
            #   export ends after rec_date   -> blind tail         -> unconfirmed
            #   export ends before rec_date  -> no evidence at all -> unconfirmed
            if txn_to is not None and txn_to >= str(date):
                e["execution_status"] = "not_executed"
                e.pop("execution_unconfirmed_reason", None)
                counts["not_executed"] += 1
                continue
            if txn_to is None or txn_to < rec_date:
                _why = ("no transaction evidence covers this recommendation: "
                        "export covers %s..%s, recommended %s"
                        % (txn_from, txn_to, rec_date))
            else:
                _why = ("transaction evidence is blind after %s: recommended %s, "
                        "reconciled to %s, export covers %s..%s"
                        % (txn_to, rec_date, date, txn_from, txn_to))
            e["execution_status"] = "execution_unconfirmed"
            e["execution_unconfirmed_reason"] = _why
            counts["execution_unconfirmed"] += 1
            continue

        # No transaction data at all: legacy holdings-delta inference.
        fallback_used.append(t)
        held_now = t in cur
        if d in buy_like and d != "top_up":
            status = "confirmed_executed" if held_now else "not_executed"
        elif d == "sell":
            status = "confirmed_executed" if not held_now else "not_executed"
        elif d in ("top_up", "trim"):
            cq = cur.get(t)
            pq = prior.get(t) if prior is not None else None
            if prior is None or cq is None or pq is None:
                status = "execution_unconfirmed"
            elif d == "top_up":
                status = "confirmed_executed" if cq > pq else "not_executed"
            else:
                status = "confirmed_executed" if cq < pq else "not_executed"
        else:
            status = "no_action_expected"
        e["execution_status"] = status
        if status == "confirmed_executed":
            e["executed_confirmed_date"] = date
            e["execution_source"] = "holdings_delta"
        counts[status] = counts.get(status, 0) + 1

    # Trades the framework never recommended — action taken outside the process.
    off_framework = []
    for x in txns:
        key = (x.get("date"), str(x.get("ticker") or "").upper(),
               x.get("type"), x.get("reference"))
        if key in matched_uids:
            continue
        off_framework.append({
            "date": x.get("date"), "ticker": x.get("ticker"),
            "type": x.get("type"), "quantity": x.get("quantity"),
            "price": x.get("price"), "amount_gbp": x.get("amount_gbp"),
            "note": ("executed with no matching ledger recommendation IN FORCE at the trade "
                     "date (ISA-0717: a superseded or later-replaced decision cannot claim it)"),
            "current_decision_id": None,
        })

    # ISA-0704: explicit persistence contract - a rehearsal computes the same reconciliation, saves nothing.
    if persist:
        save_ledger(ledger, path)
    return {"counts": counts, "confirmed": confirmed, "persisted": bool(persist),
            "off_framework": off_framework,
            "fallback_used": sorted(set(fallback_used)),
            # ISA-0684: the coverage window travels with the verdict, so every
            # downstream reader can tell a refusal from a rejection (R2.10).
            "txn_coverage": {"from": txn_from, "to": txn_to,
                             "reconciled_to": str(date),
                             "complete": bool(txn_to is not None
                                              and txn_to >= str(date))},
            "source": "transactions" if have_txns else "holdings_delta"}


def load_transactions(transactions_json_path):
    """Read executed_trades out of transactions_data_[mmm_yyyy].json.
    Returns [] when the file is absent — a missing export degrades, never fails."""
    if not transactions_json_path or not os.path.exists(transactions_json_path):
        return []
    try:
        with open(transactions_json_path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("executed_trades", []) or []
    except Exception:
        return []


def _selftest():
    """ISA-0684 — the execution verdict is CONDITIONED on evidence, not merely produced.

    R5.5: every test ships a negative control. The control here is load-bearing and
    labelled: without it this suite would pass just as well against a module that had
    simply BANNED the not_executed verdict, which would replace a false accusation with
    a blind spot. The negative control is what proves the verdict still fires when the
    export genuinely covers the window.
    """
    import tempfile as _tf
    ok = [0]

    def ck(label, cond):
        assert cond, "decision_ledger._selftest FAIL: " + label
        ok[0] += 1

    _d = _tf.mkdtemp()
    _n = [0]

    def _mk(entries):
        _n[0] += 1
        p = os.path.join(_d, "l%d.json" % _n[0])
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"entries": entries}, fh)
        return p

    def _e(t, dec, date):
        return {"_id": "%s::%s::%s" % (date, t, dec), "date": date, "ticker": t,
                "route": "growth", "decision": dec,
                "execution_status": "recommended", "executed_confirmed_date": None}

    _txn = [{"date": "2026-08-10", "ticker": "QBTS", "type": "buy", "quantity": 68.0,
             "price": 15.24, "amount_gbp": 1049.56, "reference": "R1"},
            {"date": "2026-07-02", "ticker": "MU", "type": "buy", "quantity": 10.0,
             "price": 100.0, "amount_gbp": 800.0, "reference": "R2"}]

    # MUST FIRE — the live NTAP shape: recommended 06-Sep, export ends 10-Aug.
    p = _mk([_e("NTAP", "buy", "2026-09-06")])
    r = reconcile_executions_from_transactions(
        p, _txn, {"QBTS": 68.0}, prior_holdings={"QBTS": 68.0}, date="2026-09-12")
    with open(p, encoding="utf-8") as fh:
        got = json.load(fh)["entries"][0]
    ck("MUST-FIRE: a recommendation the export cannot see is execution_unconfirmed",
       got["execution_status"] == "execution_unconfirmed")
    ck("MUST-FIRE: a reason is published, not merely a status",
       "no transaction evidence covers" in (got.get("execution_unconfirmed_reason") or ""))
    ck("MUST-FIRE: the reason names both the coverage window and the recommendation date",
       "2026-08-10" in got["execution_unconfirmed_reason"]
       and "2026-09-06" in got["execution_unconfirmed_reason"])
    ck("MUST-FIRE: nothing is counted as declined", r["counts"]["not_executed"] == 0)
    ck("MUST-FIRE: coverage is published as incomplete",
       r["txn_coverage"]["complete"] is False)

    # NEGATIVE CONTROL (labelled, load-bearing) — full coverage, no matching trade.
    # The verdict MUST still fire. Delete this and the module could ban not_executed
    # outright and still go green, which is the failure this control exists to catch.
    p = _mk([_e("AVGO", "buy", "2026-07-01")])
    r = reconcile_executions_from_transactions(
        p, _txn, {"QBTS": 68.0}, prior_holdings={"QBTS": 68.0}, date="2026-08-10")
    with open(p, encoding="utf-8") as fh:
        got = json.load(fh)["entries"][0]
    ck("NEGATIVE CONTROL: export reaches the run date -> not_executed STILL fires",
       got["execution_status"] == "not_executed")
    ck("NEGATIVE CONTROL: no stale unconfirmed reason is left on the entry",
       got.get("execution_unconfirmed_reason") is None)
    ck("NEGATIVE CONTROL: coverage reported complete",
       r["txn_coverage"]["complete"] is True)

    # Blind tail — recommended inside coverage, reconciled beyond it.
    p = _mk([_e("COCO", "buy", "2026-08-01")])
    reconcile_executions_from_transactions(
        p, _txn, {"QBTS": 68.0}, prior_holdings={"QBTS": 68.0}, date="2026-09-12")
    with open(p, encoding="utf-8") as fh:
        got = json.load(fh)["entries"][0]
    ck("BLIND TAIL: unconfirmed, with the blind window named",
       got["execution_status"] == "execution_unconfirmed"
       and "blind after 2026-08-10" in got["execution_unconfirmed_reason"])

    # A real execution still confirms — the fix breaks nothing.
    p = _mk([_e("QBTS", "buy", "2026-08-05")])
    reconcile_executions_from_transactions(
        p, _txn, {"QBTS": 68.0}, prior_holdings={}, date="2026-08-10")
    with open(p, encoding="utf-8") as fh:
        got = json.load(fh)["entries"][0]
    ck("a matching trade still confirms", got["execution_status"] == "confirmed_executed")

    # NEGATIVE CONTROL — no export at all: the legacy holdings-delta path is untouched
    # and reports no coverage rather than an empty window that reads as complete.
    p = _mk([_e("MU", "buy", "2026-08-05")])
    r = reconcile_executions_from_transactions(
        p, [], {"MU": 10.0}, prior_holdings={}, date="2026-08-10")
    with open(p, encoding="utf-8") as fh:
        got = json.load(fh)["entries"][0]
    ck("NEGATIVE CONTROL: no export -> holdings-delta path unchanged",
       got["execution_status"] == "confirmed_executed" and r["source"] == "holdings_delta")
    ck("NEGATIVE CONTROL: no export -> coverage reports no evidence, not a clean window",
       r["txn_coverage"]["to"] is None and r["txn_coverage"]["complete"] is False)


    # ══ ISA-0686 — canonical VCI decision capture ════════════════════════════════════
    _lp = _mk([])
    _art = {"QBTS": {"ticker": "QBTS", "acs": 76, "deploy_eligible": True,
                     "require_manual_confirm": False, "size_pct": 0.75,
                     "eligibility_reasons": ["eligible"], "vci_source_score": 56.7},
             "RGTI": {"ticker": "RGTI", "acs": 61, "deploy_eligible": False,
                      "require_manual_confirm": False, "eligibility_reasons": ["acs below floor"]},
             "IONQ": {"ticker": "IONQ", "acs": 80, "deploy_eligible": True,
                      "require_manual_confirm": True, "eligibility_reasons": ["scalar FV"]}}
    ck("ISA-0686: the three real artefact shapes all normalise (dict-by-ticker, bare list, "
       "one scorecard) - guessing between them is the silent-miss class itself",
       len(normalise_vci_artefact(_art)) == 3
       and len(normalise_vci_artefact(list(_art.values()))) == 3
       and len(normalise_vci_artefact(_art["QBTS"])) == 1)
    ck("ISA-0686: only the deploy-eligible auto-deployable name is CAPITAL-EFFECTIVE - a "
       "manual-confirm name is held, not bought, and a refusal is a PASS",
       capital_effective_names(_art) == ["QBTS"])

    _before = assert_vci_captured(_lp, _art, source_path="vci_deploy_test.json")
    ck("⚑ ISA-0686 MUST-FIRE: a deploy artefact with a capital-effective name and an EMPTY "
       "ledger is a BREACH - this is exactly the QBTS 09-Aug-2026 failure",
       (not _before["ok"]) and _before["missing"] == ["QBTS"])

    _res = capture_vci_decisions(_lp, _art, source_path=__file__, date="2026-08-09",
                                 build_id="TB-TEST-01", authority="AUTHORISED")
    _after = assert_vci_captured(_lp, _art, source_path="vci_deploy_test.json")
    ck("ISA-0686 POSITIVE CONTROL: after capture the boundary contract is satisfied",
       _after["ok"] and len(_res["captured"]) == 3)
    _q = current_decision(_lp, "QBTS", "vci")
    ck("ISA-0686: the captured decision carries route, build id, input snapshot and a "
       "decision id - a decision that cannot be named cannot be superseded or cited",
       _q["decision"] == "buy" and _q["route"] == "vci" and _q["build_id"] == "TB-TEST-01"
       and _q["input_snapshot_id"] and str(_q["decision_id"]).startswith("DEC-2026-08-09-VCI-QBTS-"))
    ck("ISA-0686: a manual-confirm name is captured as HOLD, never auto-deployed",
       current_decision(_lp, "IONQ", "vci")["decision"] == "hold")
    ck("ISA-0686: the road not taken is captured too - a refused name is a PASS with its "
       "reason, not silence",
       current_decision(_lp, "RGTI", "vci")["decision"] == "PASS"
       and current_decision(_lp, "RGTI", "vci")["reasons"])

    _n_before = len(load_ledger(_lp)["entries"])
    capture_vci_decisions(_lp, _art, source_path=__file__, date="2026-08-09",
                          build_id="TB-TEST-01", authority="AUTHORISED")
    ck("ISA-0686: capture is IDEMPOTENT - re-running the same month writes nothing new",
       len(load_ledger(_lp)["entries"]) == _n_before)

    # R18.5 — an unsigned LIVE state may screen and score; it may NOT issue a deploy decision
    _lp2 = _mk([])
    capture_vci_decisions(_lp2, _art, source_path=__file__, date="2026-08-09",
                          authority="REFUSED")
    _q2 = current_decision(_lp2, "QBTS", "vci")
    ck("⚑ ISA-0686/R18.5 NEGATIVE CONTROL: under a REFUSED capital authority the same "
       "deploy-eligible name is captured as PASS carrying the refusal - the evidence is kept, "
       "the capital action is NOT manufactured",
       _q2["decision"] == "PASS"
       and any("REFUSED_CAPITAL_AUTHORITY" in f for f in _q2["flags_at_decision"]))
    ck("ISA-0686/R18.5: and nothing is capital-effective under a refused authority, so the "
       "contract cannot demand a decision the gate forbids",
       capital_effective_names(_art, authority="REFUSED") == [])

    # supersession — §5.2: a stale or superseded receipt cannot authorise
    _lp3 = _mk([])
    _a = log_decision(_lp3, "ABCL", "vci", "buy", date="2026-07-10",
                      input_snapshot_id="run_jul@aaa")
    _b = log_decision(_lp3, "ABCL", "vci", "sell", date="2026-09-10",
                      input_snapshot_id="run_sep@bbb",
                      supersedes_decision_id=_a["decision_id"])
    _led = load_ledger(_lp3)["entries"]
    ck("ISA-0686: supersession is stamped on BOTH rows",
       _led[0]["superseded_by"] == _b["decision_id"]
       and _b["supersedes_decision_id"] == _a["decision_id"])
    ck("⚑ ISA-0686 MUST-FIRE: current_decision returns the SUCCESSOR, never the superseded "
       "row - a stale decision must not be able to authorise anything",
       current_decision(_lp3, "ABCL", "vci")["decision_id"] == _b["decision_id"])
    ck("ISA-0686: the same name decided by two ROUTES on one day gets two distinct ids - the "
       "old (date, ticker, decision) key collided",
       decision_id("2026-08-09", "QBTS", "vci", "buy")
       != decision_id("2026-08-09", "QBTS", "growth", "buy"))
    ck("ISA-0686: re-deciding on a DIFFERENT input snapshot is a different decision, which is "
       "what makes supersession meaningful rather than a silent overwrite",
       decision_id("2026-08-09", "QBTS", "vci", "buy", "a@1")
       != decision_id("2026-08-09", "QBTS", "vci", "buy", "b@2"))
    # §5.9 — a re-priced name in the same month supersedes, it does not stand beside
    _lp5 = _mk([])
    _row1 = {"QBTS": {"ticker": "QBTS", "acs": 76, "deploy_eligible": False,
                      "require_manual_confirm": True, "eligibility_reasons": ["scalar FV"]}}
    _row2 = {"QBTS": {"ticker": "QBTS", "acs": 76, "deploy_eligible": True,
                      "require_manual_confirm": False, "size_pct": 1.0,
                      "eligibility_reasons": ["eligible"]}}
    capture_vci_decisions(_lp5, _row1, source_path=_mk([]), date="2026-09-13")
    _r2 = capture_vci_decisions(_lp5, _row2, source_path=_mk([]), date="2026-09-13",
                                held=["QBTS"])
    _cur5 = current_decision(_lp5, "QBTS", "vci")
    ck("⚑ ISA-0686/§5.9 MUST-FIRE: re-deciding the same name in the same month on CHANGED "
       "inputs supersedes the earlier decision - two live decisions for one name is the "
       "competing-authority defect Wave 2 exists to remove",
       _cur5["decision"] == "top_up" and _cur5["supersedes_decision_id"]
       and len([e for e in load_ledger(_lp5)["entries"] if not e.get("superseded_by")]) == 1)
    _n5 = len(load_ledger(_lp5)["entries"])
    capture_vci_decisions(_lp5, _row2, source_path=_r2["source"], date="2026-09-13",
                          held=["QBTS"])
    ck("ISA-0686 NEGATIVE CONTROL: an UNCHANGED input snapshot supersedes nothing and writes "
       "nothing - supersession must mean 'the inputs moved', not 'the function ran again'",
       len(load_ledger(_lp5)["entries"]) == _n5)

    ck("ISA-0686: the artefact month is read from the filename, not guessed",
       artefact_month("vci_deploy_aug_2026.json") == "2026-08"
       and artefact_month("vci_run_jan_2027.json") == "2027-01"
       and artefact_month("decision_ledger.json") is None)
    _lp4 = _mk([])
    log_decision(_lp4, "ABCL", "vci", "buy", date="2026-07-10")
    _aug = assert_vci_captured(_lp4, {"ABCL": {"ticker": "ABCL", "deploy_eligible": True,
                                               "require_manual_confirm": False}},
                               source_path="vci_deploy_aug_2026.json")
    ck("⚑ ISA-0686 MUST-FIRE: a JULY decision does NOT absolve the AUGUST run - 'somewhere in "
       "the ledger' is not 'this run's decision', and the weaker rule would have hidden two "
       "real uncaptured August decisions",
       (not _aug["ok"]) and _aug["missing"] == ["ABCL"] and _aug["month"] == "2026-08")
    log_decision(_lp4, "ABCL", "vci", "top_up", date="2026-08-09")
    ck("ISA-0686 POSITIVE CONTROL: the same check passes once the August decision exists",
       assert_vci_captured(_lp4, {"ABCL": {"ticker": "ABCL", "deploy_eligible": True,
                                           "require_manual_confirm": False}},
                           source_path="vci_deploy_aug_2026.json")["ok"])
    _nomonth = assert_vci_captured(_lp4, {"ABCL": {"ticker": "ABCL", "deploy_eligible": True,
                                                   "require_manual_confirm": False}},
                                   source_path="vci_deploy.json")
    ck("ISA-0686 R2.10: when the month cannot be established the weaker rule is applied and "
       "the receipt SAYS SO, rather than passing silently on a different basis",
       "ANY month" in _nomonth["basis"])
    ck("⚑ ISA-0686 NEGATIVE CONTROL: capture is NOT an amnesty - a name the framework never "
       "decided has NO current decision, so its execution still reconciles as "
       "bought_outside_framework",
       current_decision(_lp, "NEVERDECIDED", "vci") is None)

    # ── ISA-0717 (23-Sep-2026) — execution revalidates against the decision IN FORCE ──────
    _lp7 = os.path.join(_tf.mkdtemp(), "l7.json")
    log_decision(_lp7, "ABCL", "vci", "top_up", date="2026-08-09")   # the real stale shape
    log_decision(_lp7, "ABCL", "vci", "PASS", date="2026-09-13")
    log_decision(_lp7, "COCO", "growth", "buy", date="2026-08-02")
    _tx7 = [{"date": "2026-10-06", "ticker": "ABCL", "type": "buy", "quantity": 10,
             "price": 9.0, "amount_gbp": 70.0, "reference": "R-ABCL"},
            {"date": "2026-08-03", "ticker": "COCO", "type": "buy", "quantity": 5,
             "price": 70.0, "amount_gbp": 3000.0, "reference": "R-COCO"}]
    _r7 = reconcile_executions_from_transactions(_lp7, _tx7, {"ABCL": 10, "COCO": 5},
                                                 date="2026-10-10", persist=True)
    _e7 = {(e["ticker"], e["decision"]): e for e in load_ledger(_lp7)["entries"]}
    ck("⚑ ISA-0717 MUST-FIRE: a STALE top_up (09-Aug) behind a later PASS (13-Sep) is NOT "
       "confirmed by an October buy - its validity window closed on 13-Sep",
       _e7[("ABCL", "top_up")]["execution_status"] == "superseded_unexecuted"
       and _e7[("ABCL", "top_up")]["validity_end"] == "2026-09-13")
    ck("⚑ ISA-0717 MUST-FIRE: that October buy reconciles OFF-FRAMEWORK - no decision in force "
       "authorised it",
       any(x["ticker"] == "ABCL" and x.get("current_decision_id") is None
           for x in _r7["off_framework"]))
    ck("ISA-0717 POSITIVE CONTROL: a decision inside its window IS confirmed, and the confirmed "
       "row NAMES the decision it links",
       _e7[("COCO", "buy")]["execution_status"] == "confirmed_executed"
       and any(c.get("decision_id") == _e7[("COCO", "buy")]["decision_id"]
               for c in _r7["confirmed"]))
    _lp8 = os.path.join(_tf.mkdtemp(), "l8.json")
    log_decision(_lp8, "DUP", "growth", "buy", date="2026-09-01")
    log_decision(_lp8, "DUP", "vci", "buy", date="2026-09-02")
    _r8 = reconcile_executions_from_transactions(
        _lp8, [{"date": "2026-09-05", "ticker": "DUP", "type": "buy", "quantity": 1,
                "price": 1.0, "amount_gbp": 1.0, "reference": "R-DUP"}],
        {"DUP": 1}, date="2026-09-06", persist=False)
    ck("⚑ ISA-0717 MUST-FIRE: ONE trade links at most ONE decision - the one in force (the later "
       "vci buy), never both",
       _r8["counts"]["confirmed_executed"] == 1
       and [c["route"] for c in _r8["confirmed"]] == ["vci"])

    print("decision_ledger._selftest: %d assertion(s) passed (ISA-0684 + ISA-0686 + ISA-0717)" % ok[0])
    return ok[0]
