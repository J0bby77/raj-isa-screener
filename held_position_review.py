#!/usr/bin/env python3
"""
held_position_review.py — ISA-0548. THE consumer for the held-position controls.

Authority: ISA-0548 build, 12-Sep-2026. Called by `monthly_isa_prerun` step 6.5.

═══════════════════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════════════════
The ISA-0548 build produced five controls that each answer a question about a HELD position:

    sleeve_risk.ceiling_verdict        is there a size that is both material and
                                       risk-acceptable?            (D27 / ISA-0652 / ISA-0419)
    thesis_state.cap_rung              does judgement lower the earned rung?   (ISA-0466)
    retention.graduation_disposition   keep-and-top-up or sell?    (D30 / ISA-0651)
    position_sizing.refresh_obligations  what has first claim on the next tranche? (ISA-0669)
    position_alerts.min_hold_state     may any of it be acted on?  (ISA-0645)

⚑ EVERY ONE OF THEM WOULD HAVE SHIPPED WITH ZERO CALLERS. That is the single most frequent
defect in this framework's history — `build_symbol_map` (03-Sep), `framework_atlas.check`
(09-Sep), ISA-0498's battery check (11-Sep), `vci_size_pct`, `min_hold_ok`,
`save_fill_obligations`, `thesis_state.apply`. R4.14 is explicit that a calculation with no
intended consumer is NOT LIVE however many assertions it passes. This module is the consumer,
and the pre-run is its orchestrator, so the chain PRODUCED -> EXECUTED -> CONSUMED is real
before any of it is claimed.

⚑ IT DECIDES NOTHING AND TRADES NOTHING. It reads the five verdicts and assembles them per
name. Every refusal is carried out by name; nothing is summarised into a count that hides
which name refused (R4.9).

ROLLBACK (R4.13): `isa_policy.V2_FLAGS["held_position_review"] = False` — `review()` returns
state DISABLED, which reads as UNKNOWN and never as "no position needs attention" (R4.3).
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List, Optional

try:                                                    # pragma: no cover - wiring only (ISA-0699)
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None


HERE = os.path.dirname(os.path.abspath(__file__))


def _flag() -> bool:
    try:
        import isa_policy as pol
        return bool(pol.flag("held_position_review"))
    except Exception:                                                # noqa: BLE001
        return True


def _today() -> str:
    return datetime.date.today().isoformat()


def review(portfolio_path: str, *, root: Optional[str] = None, today: Optional[str] = None,
           store: Optional[dict] = None, dry_run: bool = False) -> dict:
    """Per held direct-stock name: ceiling, judgement cap, disposition, obligations.

    Returns {rows, warnings, summary}. A control that could not be evaluated for a name is
    recorded on that name as UNEVALUATED with its reason — never omitted, because an omitted
    name and a clean name read identically downstream (R2.10)."""
    _fi_mark("held_position_review", "review")   # ISA-0699: execution-ledger observation
    root = root or HERE
    today = today or _today()
    if not _flag():
        return {"state": "DISABLED", "rows": [], "warnings": [
            "Step 6.5: held_position_review is DISABLED by flag — the held-position controls "
            "were NOT evaluated this run. That is UNKNOWN, not a finding that every position "
            "is fine (R4.3)."], "summary": {}}

    warn: List[str] = []
    rows: List[dict] = []
    try:
        with open(portfolio_path, encoding="utf-8") as fh:
            pf = json.load(fh)
    except Exception as exc:                                         # noqa: BLE001
        return {"state": "UNAVAILABLE", "rows": [], "summary": {},
                "warnings": ["Step 6.5 (held_position_review): portfolio_data unreadable "
                             "(%s: %s) — NO held position was reviewed this run."
                             % (type(exc).__name__, exc)]}
    stocks = [x for x in (pf.get("stocks") or []) if x.get("ticker")]
    # ⚑ DATA CURRENCY. portfolio_data is the 31-Aug broker snapshot; a position bought since
    #   is DECLARED in the registries but absent from it, so every weight, every risk share
    #   and therefore every ceiling verdict is computed on a sleeve that is missing money.
    #   Running this live on 12-Sep showed MU REFUSE and AVGO IN_BREACH purely because NTAP
    #   (GBP 5,112.19, bought after the snapshot) was not in the denominator. A verdict
    #   computed on a stale sleeve must SAY SO rather than being read as current (R4.2/R2.6).
    _held_tk = {str(x["ticker"]).upper() for x in stocks}
    nav = (pf.get("summary", {}) or {}).get("total_value_gbp")
    sleeve_gbp = sum(float(x.get("value_gbp") or 0.0) for x in stocks)
    if not stocks or not nav:
        return {"state": "UNAVAILABLE", "rows": [], "summary": {},
                "warnings": ["Step 6.5 (held_position_review): portfolio_data has no stocks or "
                             "no summary.total_value_gbp — NO held position was reviewed."]}

    # ── shared inputs, each loaded once and each degrading to a NAMED gap ────────────────
    weekly = store
    if weekly is None:
        try:
            with open(os.path.join(root, "stock_weekly_returns.json"), encoding="utf-8") as fh:
                weekly = json.load(fh)
        except Exception as exc:                                     # noqa: BLE001
            weekly = None
            warn.append("Step 6.5 (ISA-0652): stock_weekly_returns.json unreadable (%s) — the "
                        "sleeve-risk ceiling is UNEVALUATED for every name, not satisfied."
                        % type(exc).__name__)
    # ⚑ ONE golden source for sigma (R6.1): stock_price_fetch.matrix at the declared
    #   104-week window. Never recomputed locally — a second derivation of volatility would
    #   be a second home for the number every threshold in this build is divided by.
    sigmas = {}
    try:
        import stock_price_fetch as _spf
        _m = _spf.matrix(tickers=[str(x["ticker"]).upper() for x in stocks], weeks=104)
        sigmas = _m.get("sigma_ann") or {}
    except Exception as exc:                                         # noqa: BLE001
        warn.append("Step 6.5 (ISA-0658): sigma could not be read from "
                    "stock_price_fetch.matrix (%s: %s) — every realisation t is UNEVALUATED, "
                    "not zero." % (type(exc).__name__, exc))
    # ⚑ EVIDENCE STATE, from the SAME basis capital_destination uses (R6.1 one golden
    #   source). It is the input `vci_size_pct` expects — an EVIDENCE state, not a ladder
    #   rung. Evidence sets the rung; thesis_state may only lower it afterwards. Passing a
    #   rung here would size on the wrong axis entirely, and position_sizing.rung_for_state
    #   correctly RAISES on it rather than accepting it — which is how this was caught.
    ev_states: Dict[str, str] = {}
    try:
        import evidence_state as _es
        import stock_price_fetch as _spf2
        _chan = _spf2.channel_report([str(x["ticker"]).upper() for x in stocks])
        for _t, _ch in (_chan.get("rows") or {}).items():
            ev_states[str(_t).upper()] = _es.classify(dict(_ch), route="main",
                                                      coverage_ok=True)["state"]
    except Exception as exc:                                         # noqa: BLE001
        warn.append("Step 6.5 (ISA-0646): evidence_state could not be classified (%s: %s) — "
                    "every budget-derived VCI size is UNEVALUATED, not unconstrained."
                    % (type(exc).__name__, exc))
    try:
        import position_alerts as _pa
        mh = _pa.min_hold_state(root=root)
    except Exception as exc:                                         # noqa: BLE001
        mh = {"enforced": False, "positions": {}, "why": "%s: %s" % (type(exc).__name__, exc)}
    if not mh.get("enforced"):
        warn.append("Step 6.5 (ISA-0645): the 182-day min-hold is UNENFORCED this run — %s"
                    % mh.get("why"))
    try:
        import thesis_state as _ts
        th = _ts.load_states(root)
    except Exception as exc:                                         # noqa: BLE001
        th, _ts = {}, None
        warn.append("Step 6.5 (ISA-0466): thesis_state unavailable (%s) — the judgement "
                    "ceiling is UNAPPLIED, not satisfied." % type(exc).__name__)
    try:
        import position_sizing as _ps
        declared = _ps.load_declared_binaries(root)
        min_entry = _ps.min_entry_gbp(float(nav))["min_entry_gbp"]
    except Exception as exc:                                         # noqa: BLE001
        return {"state": "UNAVAILABLE", "rows": [], "summary": {},
                "warnings": warn + ["Step 6.5: position_sizing unavailable (%s: %s)"
                                    % (type(exc).__name__, exc)]}
    _declared_only = sorted({t for t, v in (declared or {}).items()
                             if not str(t).startswith("_")} - _held_tk)
    if _declared_only:
        warn.append(
            "Step 6.5 ⚑ STALE SLEEVE: %s is DECLARED in vci_binary_positions.json but absent "
            "from %s, so it is not in the risk denominator. Every weight, risk share and "
            "ceiling verdict below is computed on a sleeve that is missing that money and is "
            "NOT current. Re-run after the next portfolio extract before acting on any "
            "REFUSE or IN_BREACH verdict."
            % (", ".join(_declared_only), os.path.basename(portfolio_path)))

    # ── D17 obligations: lifecycle first, so the queue's claims are current ──────────────
    try:
        obl = _ps.refresh_obligations({t: {"thesis_state": r.get("state")}
                                       for t, r in (th or {}).items()},
                                      today=today, root=root, dry_run=dry_run)
        warn.extend(obl.get("warnings") or [])
    except Exception as exc:                                         # noqa: BLE001
        obl = {"open": [], "voided_this_run": []}
        warn.append("Step 6.5 (ISA-0669): the fill-obligation lifecycle RAISED (%s: %s) — "
                    "obligations were NOT refreshed; open claims may be stale."
                    % (type(exc).__name__, exc))

    # ── the L1 budget, computed ONCE over the held binaries (ISA-0646) ──────────────────
    budget_available_pct, budget_why = None, "not computed"
    try:
        _bin_rows = []
        for y in stocks:
            _t = str(y["ticker"]).upper()
            _d = declared.get(_t) or {}
            if not _d.get("is_binary"):
                continue
            _bin_rows.append({
                "ticker": _t,
                "size_pct": 100.0 * float(y.get("value_gbp") or 0.0) / float(nav),
                "is_binary": True,
                "catalyst_status": _d.get("catalyst_status"),
                "asset_structure": _d.get("asset_structure"),
                "successor": _d.get("successor"),
                "p_thesis": _d.get("p_thesis"), "L": _d.get("L")})
        _bud = _ps.budget_available_reported(_bin_rows, budget_pct=1.5, max_concurrent=2)
        budget_available_pct = _bud.get("available_pct")
        budget_why = _bud.get("why") or "measured"
    except Exception as _be:                                         # noqa: BLE001
        budget_why = "%s: %s" % (type(_be).__name__, _be)
        warn.append("Step 6.5 (ISA-0646): the held-binary budget RAISED (%s) — every "
                    "budget-derived VCI size is UNEVALUATED, not unconstrained." % budget_why)

    for x in stocks:
        tk = str(x["ticker"]).upper()
        row: dict = {"ticker": tk, "value_gbp": float(x.get("value_gbp") or 0.0),
                     "gain_pct": x.get("gain_pct")}

        # 1) sleeve-risk ceiling (D27)
        if weekly is not None:
            try:
                import sleeve_risk as _sr
                row["ceiling"] = _sr.ceiling_verdict(
                    weekly, pf, tk, sleeve_gbp=sleeve_gbp, min_entry_gbp=min_entry)
            except Exception as exc:                                 # noqa: BLE001
                row["ceiling"] = {"verdict": "UNEVALUATED",
                                  "why": "%s: %s" % (type(exc).__name__, exc)}
                warn.append("Step 6.5 (ISA-0652) %s: the sleeve-risk ceiling could not be "
                            "evaluated — %s: %s. UNEVALUATED, not OK."
                            % (tk, type(exc).__name__, exc))
        else:
            row["ceiling"] = {"verdict": "UNEVALUATED", "why": "no weekly return store"}
        if (row["ceiling"] or {}).get("verdict") == "REFUSE":
            warn.append("Step 6.5 (D27/ISA-0652) %s REFUSE: %s" % (tk, row["ceiling"]["why"]))
        elif (row["ceiling"] or {}).get("verdict") == "IN_BREACH":
            warn.append("Step 6.5 (D27) %s IN BREACH of the sleeve-risk ceiling: %s"
                        % (tk, row["ceiling"]["why"]))

        # 2) judgement ceiling (ISA-0466)
        if _ts is not None:
            try:
                row["thesis"] = _ts.cap_rung(tk, "EARNED_MAX", states=th, root=root)
            except Exception as exc:                                 # noqa: BLE001
                row["thesis"] = {"state": "UNDECLARED",
                                 "why": "%s: %s" % (type(exc).__name__, exc)}
                warn.append("Step 6.5 (ISA-0466) %s: %s" % (tk, exc))

        # 3) graduation disposition (D30/ISA-0651) — only for a resolved binary
        d = declared.get(tk) or {}
        if not d:
            for _k, _v in declared.items():
                if tk in [str(a).upper() for a in (_v.get("aliases") or [])]:
                    d = _v
                    break
        st = d.get("catalyst_status")
        if d.get("is_binary") and st and str(st).startswith("RESOLVED"):
            try:
                import retention as _rt
                succ = d.get("successor") or {}
                mh_row = (mh.get("positions") or {}).get(tk) or {}
                gain = x.get("gain_pct")
                # ── ISA-0658 — the realisation leg, COMPUTED, not assumed ──────────────
                # ⚑ Inside a graduation disposition the leg is by construction PERMISSION
                #   (D31): the keep-or-sell answer was already decided by the forward case,
                #   so the leg is unlocking the 182-day min-hold rather than deciding the
                #   sale. Hard-coding fired=False here — which the first draft of this module
                #   did — silently produced SELL_PENDING_MIN_HOLD on a name whose exemption
                #   was in fact available, i.e. a decided sale reported as blocked.
                _real = {"fired": False, "role": _rt.REALISATION_ROLE_PERMISSION,
                         "why": "not computed"}
                _rt_t = None
                try:
                    _sig = (sigmas or {}).get(tk)
                    _entry = mh_row.get("position_first_entry_date")
                    if _sig and _entry and gain is not None:
                        _d = (datetime.date.fromisoformat(today)
                              - datetime.date.fromisoformat(_entry)).days
                        _rt_t = _rt.realisation_t(float(gain) / 100.0, _sig, _d)
                        _hist = [{"as_of": today, "t": _rt_t.get("t")}]
                        _real = _rt.realisation_trigger(
                            _hist, role=_rt.REALISATION_ROLE_PERMISSION)
                        _real["role"] = _rt.REALISATION_ROLE_PERMISSION
                        row["realisation"] = _rt_t
                        row["mandate_years"] = _rt.mandate_years_banked(float(gain) / 100.0)
                    else:
                        _missing = [n for n, v in (("sigma_ann", _sig),
                                                   ("entry_date", _entry),
                                                   ("gain_pct", gain)) if not v]
                        warn.append("Step 6.5 (ISA-0658) %s: the realisation leg could not be "
                                    "computed — missing %s. UNEVALUATED, which is not a "
                                    "finding that it did not fire (R2.10)."
                                    % (tk, ", ".join(_missing)))
                except Exception as _re:                             # noqa: BLE001
                    warn.append("Step 6.5 (ISA-0658) %s: the realisation leg RAISED (%s: %s) "
                                "— UNEVALUATED." % (tk, type(_re).__name__, _re))
                disp = _rt.graduation_disposition(
                    ticker=tk, catalyst_status=st,
                    forward_case={"priceable": bool(succ.get("priceable")),
                                  "refusal_kind": succ.get("refusal_kind")},
                    in_profit=bool(gain is not None and gain > 0),
                    realisation=_real,
                    giveback={"fired": False},
                    size_gbp=row["value_gbp"], min_entry_gbp=min_entry,
                    rung_gbp=float(nav) * 0.035,
                    risk_ceiling_gbp=(row.get("ceiling") or {}).get("ceiling_gbp"),
                    min_hold_until=mh_row.get("min_hold_until"), today=today)
                row["disposition"] = disp
                warn.append("Step 6.5 (D30/ISA-0651) %s: %s — %s"
                            % (tk, disp.get("state"), (disp.get("why") or "")[:300]))
            except Exception as exc:                                 # noqa: BLE001
                row["disposition"] = {"state": "UNEVALUATED",
                                      "why": "%s: %s" % (type(exc).__name__, exc)}
                warn.append("Step 6.5 (ISA-0651) %s: the graduation disposition RAISED (%s: "
                            "%s) — UNEVALUATED, which is not a decision to hold."
                            % (tk, type(exc).__name__, exc))
        # ── ISA-0646 — vci_size_pct GETS ITS FIRST PRODUCTION CALLER ────────────────
        # `isa_policy` has flagged `vci_budget_sizing: True` since ISA-0356 and the RED
        # baseline recorded it as wired, while the function had ZERO call sites: every VCI
        # size has been computed by hand. It is called here for each held binary, so the
        # budget-derived size is PRODUCED and CONSUMED on the orchestrated path.
        if d.get("is_binary"):
            if budget_available_pct is None:
                row["vci_size"] = {"state": "UNEVALUATED", "why": budget_why}
            else:
                try:
                    _pri = (_ps.adverse_prior(d.get("asset_structure"))
                            if d.get("catalyst_status")
                            == _ps.RESOLVED_POSITIVE_SUCCESSOR_PENDING
                            else {"p_thesis": d.get("p_thesis"), "L": d.get("L")})
                    _ev = ev_states.get(tk)
                    if not _ev:
                        row["vci_size"] = {"state": "UNEVALUATED",
                                           "why": ("no evidence_state for %s, and the ladder "
                                                   "rung is set by evidence — a substituted "
                                                   "state would size on a guess (R4.3)" % tk)}
                    else:
                        _sz = _ps.vci_size_pct(
                            p_thesis=_pri.get("p_thesis"), L=_pri.get("L"),
                            budget_available_pct=budget_available_pct,
                            evidence_state=_ev)
                        _sz["evidence_state"] = _ev
                        _sz["prior_basis"] = _pri.get("basis")
                        # judgement may only LOWER the evidence-earned rung (ISA-0466)
                        _cap = (row.get("thesis") or {})
                        if _cap.get("rung_out") and _cap.get("rung_in"):
                            _sz["thesis_ceiling"] = _cap.get("rung_out")
                            _sz["thesis_state"] = _cap.get("state")
                        row["vci_size"] = _sz
                except Exception as _ve:                             # noqa: BLE001
                    row["vci_size"] = {"state": "REFUSED",
                                       "why": "%s: %s" % (type(_ve).__name__, _ve)}
                    warn.append("Step 6.5 (ISA-0646) %s: the budget-derived VCI size REFUSED "
                                "— %s: %s. No size is emitted; with no percentage ceiling a "
                                "fallback size IS the failure mode (s2)."
                                % (tk, type(_ve).__name__, _ve))
        rows.append(row)

    # ── ISA-0418 — which holdings can be marked against their own case, and which cannot ──
    uw = {"underwritten": [], "not_underwritten": [], "warnings": []}
    try:
        import retention as _rt2
        uw = _rt2.underwriting_state([r["ticker"] for r in rows], root=root)
        warn.extend(uw.get("warnings") or [])
    except Exception as exc:                                         # noqa: BLE001
        warn.append("Step 6.5 (ISA-0418): the underwriting state could not be read (%s: %s) — "
                    "realised_fraction is UNREACHABLE and it is not known for which names."
                    % (type(exc).__name__, exc))

    summary = {
        "as_of": today, "n_held": len(rows),
        "underwritten": uw.get("underwritten"),
        "not_underwritten": uw.get("not_underwritten"),
        "sleeve_gbp": round(sleeve_gbp, 2), "min_entry_gbp": round(min_entry, 2),
        "min_hold_enforced": bool(mh.get("enforced")),
        "min_hold_source": mh.get("source"),
        "obligations_open": obl.get("open"),
        "obligations_voided_this_run": obl.get("voided_this_run"),
        "ceiling_refused": [r["ticker"] for r in rows
                            if (r.get("ceiling") or {}).get("verdict") == "REFUSE"],
        "ceiling_in_breach": [r["ticker"] for r in rows
                              if (r.get("ceiling") or {}).get("verdict") == "IN_BREACH"],
        "below_capital_floor": [r["ticker"] for r in rows
                                if (r.get("ceiling") or {}).get("verdict")
                                == "BELOW_CAPITAL_FLOOR"],
        "dispositions": {r["ticker"]: (r.get("disposition") or {}).get("state")
                         for r in rows if r.get("disposition")},
        "thesis_states": {r["ticker"]: (r.get("thesis") or {}).get("state") for r in rows},
    }
    return {"state": "OK", "rows": rows, "warnings": warn, "summary": summary}


def _selftest(verbose: bool = True) -> int:
    """liveness_ref: held_position_review._selftest"""
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    r = review(os.path.join(HERE, "portfolio_data_sep_2026.json"), dry_run=True)   # ISA-0704: no store write
    ok(r["state"] == "OK", r.get("warnings"))
    ok(r["summary"]["n_held"] >= 6, r["summary"])
    ok(isinstance(r["warnings"], list) and r["warnings"], "a live book must produce findings")
    ok("QBTS" in r["summary"]["ceiling_refused"],
       "⚑ MUST-FIRE (R5.10): QBTS has no size that is both material and risk-acceptable and "
       "the review must say so by name — %s" % r["summary"]["ceiling_refused"])
    ok(r["summary"]["min_hold_enforced"] is True,
       "the 182-day min-hold must be ENFORCED, not silently absent (ISA-0645)")
    ok(all(v for v in r["summary"]["thesis_states"].values()),
       "every held name carries a declared thesis_state or is named as refused (ISA-0466)")

    # ⚑ MUST-FIRE (R5.10) — ISA-0646. On the live book every VCI size is UNEVALUATED
    #   because QBTS cannot be priced and the budget is therefore WITHHELD, which is correct
    #   but proves only the refusal. This fixture proves the OTHER branch: with a priceable
    #   binary the budget resolves and `vci_size_pct` emits a real, budget-derived size. A
    #   control only ever observed refusing is an unproven control (R5.8).
    import position_sizing as _psx
    _b = _psx.budget_available_reported(
        [{"ticker": "FIX", "size_pct": 1.0, "is_binary": True, "catalyst_status": "PENDING",
          "p_thesis": 0.50, "L": 0.35}], budget_pct=1.5, max_concurrent=2)
    ok(_b["available_pct"] is not None and _b["blocks_capital"] is False,
       "a priceable binary must yield a measured budget, not a withheld one: %s" % _b)
    _sz = _psx.vci_size_pct(p_thesis=0.50, L=0.35,
                            budget_available_pct=_b["available_pct"],
                            evidence_state="CONFIRMED")
    ok(_sz["w_vci_pct"] > 0 and _sz["binding_constraint"],
       "vci_size_pct must emit a sized position with a NAMED binding constraint: %s" % _sz)
    ok(_sz["binding_constraint"].startswith("ladder_") or
       _sz["binding_constraint"] in ("expected_loss_budget", "max_stock_position_pct"),
       "the binding constraint must be one of the three declared, never blank: %s"
       % _sz["binding_constraint"])
    try:
        _psx.vci_size_pct(p_thesis=None, L=0.35, budget_available_pct=1.0,
                          evidence_state="CONFIRMED")
        ok(False, "a missing p_thesis must RAISE — reading it as p = 0 is the defect that "
                  "flipped DENY to ADMIT on QBTS (V-1)")
    except Exception:
        ok(True, "")

    # ⚑ NEGATIVE CONTROL (R5.5) — a DRY RUN must not mutate live state. The first version of
    #   this module persisted the obligation store unconditionally, and running the real
    #   pre-run with --dry-run showed underfilled_positions.json with a fresh mtime: a
    #   rehearsal had mutated the thing being rehearsed. A control that cannot be exercised
    #   without side effects cannot be exercised at all (R18.1).
    _st = os.path.join(HERE, "underfilled_positions.json")
    if os.path.exists(_st):
        _m0 = os.path.getmtime(_st)
        review(os.path.join(HERE, "portfolio_data_sep_2026.json"), dry_run=True)
        ok(os.path.getmtime(_st) == _m0,
           "⚑ dry_run=True must NOT write the obligation store")

    # ⚑ NEGATIVE CONTROL (R5.5): an unreadable portfolio must yield UNAVAILABLE with a named
    #   warning, never an empty-and-therefore-clean review.
    r2 = review(os.path.join(HERE, "no_such_portfolio_file.json"))
    ok(r2["state"] == "UNAVAILABLE" and r2["warnings"] and not r2["rows"],
       "an absent portfolio must REFUSE, not report a clean sleeve")
    if verbose:
        print("held_position_review selftest: %d assertions, 0 failed" % n)
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(json.dumps(review(os.path.join(HERE, "portfolio_data_sep_2026.json"))["summary"],
                         indent=1))
