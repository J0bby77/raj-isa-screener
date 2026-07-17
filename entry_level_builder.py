#!/usr/bin/env python3
"""
entry_level_builder.py  --  Pre-run Step 7.25: create / refresh composite entry levels.

Runs AFTER normalise_adapter.py (needs live prices + metrics) and BEFORE
rerank_watchlist.py / step9_pre_builder.py (which use entry_level for tiering).

Scope: main `watchlist` + `candidate_pool` growth/energy names.
EXCLUDED: `vci_watchlist` (own asymmetric framework) and `stock_sleeve`
(already-held positions — entry level is not a tiering input for them).

For every in-scope name it computes up to five disciplined anchors from data
ALREADY present in watchlist_metrics_*.json (+ realised vol / ATR added by
enrich_volatility.py), combines them conservatively, and writes a governed
entry level back to watchlist_tickers.json plus a full audit file.

Anchors
  1 return_hurdle = base_fv / (1 + required_upside)
  2 valuation     = price at own 3yr-average (path-appropriate) multiple
  3 downside_risk = (base_fv + 2*bear) / 3   (enforces >=2:1 upside:downside)
  4 technical     = current_price * (1 - vol_buffer)        [band + sanity]
  5 catalyst      = discount / flag if earnings or binary event is near

base_fv (HYBRID): own-history fair-multiple reconstruction is PRIMARY; analyst
consensus target_mean is used only as a CAP (ceiling) -> base_fv = min(own, analyst).
Lagging analyst targets never inflate the entry level.

Combine: central = min(valid of [return_hurdle, valuation, downside]); technical
is a fallback + sets the band; catalyst applies a final discount.

Provisional gating: every level created here is `provisional` and flagged
`confirm_required` -> step9_pre_builder caps it so it can rank/tier but cannot
present as a deployable "Buy Now" until human confirmation at Step 9/10.
Existing non-null human entry levels are GRANDFATHERED (status approved) unless
a refresh trigger fires.
"""
from __future__ import annotations
import argparse, json, sys, os
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fv_composite as _fvc   # Fix Pack A6 (12-Jul-26): FV composite EXTRACTED to the shared
                              # module — screen + pre-run use ONE implementation. This file
                              # delegates; the maths lives in fv_composite.py only.

TODAY = date.today().isoformat()

# --- classification ---------------------------------------------------------
# required base-case upside hurdle by stock type
REQUIRED_UPSIDE = {
    "quality_compounder": 0.22,
    "normal_growth":      0.30,
    "cyclical":           0.40,
    "healthcare":         0.50,
    "energy":             0.40,
}
# technical volatility buffer by realised-vol profile
VOL_BUFFER = {"low": 0.04, "normal": 0.07, "high": 0.14, "unknown": 0.07}

SEMI_BUCKETS = _fvc.SEMI_BUCKETS                 # moved to fv_composite (A6) — aliases kept
RATIO_LO, RATIO_HI = _fvc.RATIO_LO, _fvc.RATIO_HI
REFRESH_AGE_DAYS = 92                    # ~3 months


def classify_stock_type_detail(t: dict) -> dict:
    """Robust sector_type classification (retro item 6 fix). SINGLE implementation now lives
    in fv_composite.classify_stock_type_detail (Fix Pack A6) — this delegates."""
    return _fvc.classify_stock_type_detail(t)


def classify_stock_type(t: dict) -> str:
    """Back-compat string accessor; full detail via classify_stock_type_detail()."""
    return classify_stock_type_detail(t)["sector_type"]


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:        # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _own_fair_value(t: dict, stock_type: str):
    """
    Reconstruct fair value from the stock's own 3yr-average multiple.
    Returns (fv_own | None, metric_used | None, ratio | None).
    SINGLE implementation now lives in fv_composite.own_fair_value (Fix Pack A6) — delegates.
    """
    return _fvc.own_fair_value(t.get("current_price"),
                               pe_avg=t.get("val_hist_pe_3yr_avg"), pe_cur=t.get("val_hist_current_pe"),
                               pfcf_avg=t.get("val_hist_pfcf_3yr_avg"), pfcf_cur=t.get("val_hist_current_pfcf"),
                               stock_type=stock_type)


def build_entry_for_ticker(t: dict, existing: dict) -> dict:
    """Return an audit record (dict) with all anchors + selected entry + governance."""
    price = _num(t.get("current_price"))
    _st = classify_stock_type_detail(t)
    stock_type = _st["sector_type"]
    req_up = REQUIRED_UPSIDE[stock_type]
    prices = t.get("_prices") or {}

    rec = {
        "ticker": t.get("ticker"),
        "stock_type": stock_type,                       # back-compat
        "sector_type": stock_type,                      # CONTRACTS #5 field (FIXED — no SaaS default)
        "sector_type_inferred": _st["sector_type_inferred"],
        "sector_type_basis": _st["sector_type_basis"],
        "required_upside": req_up,
        "current_price": price,
        "entry_currency": existing.get("entry_currency") or t.get("currency") or "USD",
    }

    if not price:
        rec.update({"entry_level": None, "entry_level_status": "missing_no_price",
                    "entry_level_confidence": "low", "entry_level_provisional": True,
                    "confirm_required": True, "selected_entry_reason": "no current price",
                    "null_after_builder": True,            # CONTRACTS #5 — run status -> PARTIAL
                    "full_size_requires_review": True,     # §I.3 — low confidence != full size
                    "anchors": {}})
        return rec

    # --- anchors ---
    fv_own, fv_metric, fv_ratio = _own_fair_value(t, stock_type)
    analyst = _num(prices.get("target_mean")) or _num(t.get("target_price_mean"))
    num_analysts = _num(t.get("num_analysts")) or 0

    # Detect lagging analysts: price already above their target, OR positive EPS
    # estimate-revision momentum (analysts behind a fast-rising name).
    _up = _num(t.get("est_rev_eps_up_30d")) or 0
    _dn = _num(t.get("est_rev_eps_down_30d")) or 0
    _dir = (t.get("est_rev_direction") or "").lower()
    analysts_lagging = bool(analyst and price and price > analyst) or _dir == "up" or (_up > _dn)

    # base_fv hybrid: own-multiple primary; analyst is a CAP only when analysts are
    # NOT lagging. SINGLE implementation in fv_composite.compose_fv (Fix Pack A6) — delegates.
    # apply_consensus_cap=False preserves this builder's historic base_fv exactly: the
    # consensus sanity-cap is applied downstream at ranking time (rerank / unified source),
    # not to the stored entry-level anchor. (P2 A6-prerun may revisit.)
    _comp = _fvc.compose_fv(price, fv_own=fv_own, fv_metric=fv_metric, analyst_target=analyst,
                            analysts_lagging=analysts_lagging, apply_consensus_cap=False)
    base_fv, fv_basis = _comp["fair_value"], _comp["fv_basis"]

    return_hurdle = round(base_fv / (1 + req_up), 2) if base_fv else None
    valuation     = fv_own if fv_own else None

    bear = _num(prices.get("target_low")) or _num(prices.get("low_52wk"))
    if base_fv and bear and base_fv > bear:
        downside = round((base_fv + 2 * bear) / 3.0, 2)
    else:
        downside = None

    vol_profile = t.get("_vol_profile") or "unknown"
    buffer = VOL_BUFFER.get(vol_profile, 0.07)
    technical = round(price * (1 - buffer), 2)

    # catalyst
    next_e = t.get("next_earnings", "Unknown")
    days_to = None
    catalyst_discount = 0.0
    catalyst_note = "none"
    if next_e and next_e != "Unknown":
        try:
            d = datetime.strptime(next_e[:10], "%Y-%m-%d").date()
            days_to = (d - date.today()).days
            if 0 <= days_to <= 30:
                catalyst_discount = 0.05
                catalyst_note = f"earnings within 30d ({next_e}): -5%"
            elif stock_type == "healthcare" and 0 <= days_to <= 90:
                catalyst_discount = 0.10
                catalyst_note = f"binary event within 90d ({next_e}): -10%"
        except Exception:
            pass

    # --- combine ---
    core = [a for a in (return_hurdle, valuation, downside) if a]
    if core:
        central = min(core)
        if central == downside:
            sel = "downside-risk anchor (most conservative)"
        elif central == return_hurdle:
            sel = "return-hurdle anchor (most conservative)"
        else:
            sel = "valuation anchor (most conservative)"
    else:
        central = technical
        sel = "technical fallback (no fundamental anchor available)"

    entry_level = round(central * (1 - catalyst_discount), 2)
    half = buffer / 2.0
    band_low  = round(entry_level * (1 - half), 2)
    band_high = round(entry_level * (1 + half), 2)

    # --- confidence ---
    conf = 3  # 3=high 2=medium 1=low
    if not fv_own:
        conf -= 1                       # leaned on analyst / no own multiple
    if len(core) <= 2:
        conf -= 1
    if len(core) == 0:
        conf -= 1                       # technical-only
    if vol_profile == "unknown":
        conf -= 1
    if num_analysts and num_analysts < 8:
        conf = min(conf, 2)
    if stock_type == "healthcare" and catalyst_discount >= 0.10:
        conf = min(conf, 1)
    conf = max(1, min(3, conf))
    confidence = {3: "high", 2: "medium", 1: "low"}[conf]

    rec.update({
        "entry_level": entry_level,
        "entry_band_low": band_low,
        "entry_band_high": band_high,
        "entry_level_status": "provisional",
        "entry_level_provisional": True,
        "confirm_required": True,
        "entry_level_method": "pre_run_composite",
        "entry_level_confidence": confidence,
        "null_after_builder": False,
        "full_size_requires_review": (confidence == "low"),   # §I.3 — low conf != full size
        "vol_profile": vol_profile,
        "realised_vol": t.get("_realised_vol"),
        "selected_entry_reason": sel,
        "anchors": {
            "return_hurdle_entry": return_hurdle,
            "valuation_entry": valuation,
            "downside_risk_entry": downside,
            "technical_entry": technical,
            "base_fair_value": round(base_fv, 2) if base_fv else None,
            "fair_value_basis": fv_basis,
            "bear_case_price": round(bear, 2) if bear else None,
            "catalyst_adjustment": catalyst_note,
            "days_to_earnings": days_to,
            "vol_buffer": buffer,
        },
        "entry_level_last_reviewed": TODAY,
        "entry_level_review_trigger": "newly_promoted_null_entry"
            if existing.get("entry_level") is None else "refresh",
    })
    return rec


def _is_stale(existing: dict) -> bool:
    lr = existing.get("entry_level_last_reviewed")
    if not lr:
        return False
    try:
        age = (date.today() - datetime.strptime(lr[:10], "%Y-%m-%d").date()).days
        return age > REFRESH_AGE_DAYS
    except Exception:
        return False


def apply_governance(existing: dict, rec: dict) -> tuple[dict, str]:
    """
    Decide whether to write the computed level or grandfather an existing one.
    Returns (fields_to_write, disposition).
    """
    has_existing = existing.get("entry_level") is not None
    was_provisional = bool(existing.get("entry_level_provisional"))

    # Grandfather an approved (human, non-provisional) level unless stale.
    if has_existing and not was_provisional and not _is_stale(existing):
        return ({
            "entry_level": existing.get("entry_level"),
            "entry_level_status": "approved",
            "entry_level_provisional": False,
            "confirm_required": False,
            "entry_level_last_reviewed": existing.get("entry_level_last_reviewed", TODAY),
        }, "approved_kept")

    if rec.get("entry_level") is None:
        return ({
            "entry_level": None,
            "entry_level_status": "missing_after_builder",
            "entry_level_provisional": True,
            "confirm_required": True,
            "null_after_builder": True,             # CONTRACTS #5 — run status -> PARTIAL
            "full_size_requires_review": True,
        }, "missing_after_builder")

    fields = {
        "entry_level": rec["entry_level"],
        "entry_band_low": rec["entry_band_low"],
        "entry_band_high": rec["entry_band_high"],
        "entry_currency": rec["entry_currency"],
        "entry_level_status": "provisional",
        "entry_level_provisional": True,
        "confirm_required": True,
        "entry_level_method": "pre_run_composite",
        "entry_level_confidence": rec["entry_level_confidence"],
        "entry_level_basis": rec["anchors"],
        "entry_level_last_reviewed": TODAY,
        "entry_level_review_trigger": rec["entry_level_review_trigger"],
    }
    return fields, ("refreshed_stale" if (has_existing and _is_stale(existing))
                    else "provisional_created")


def _refresh_scored(scored_path: str, scored_out: str,
                    entry_map: dict, threshold_pct: float) -> int:
    """
    Propagate freshly-built entry levels into the scored JSON so that
    rerank_watchlist.py (in_window tiebreaker) and the email rows are consistent.
    Updates per-ticker _entry_level / _pct_above_entry / _in_window and the
    matching conviction_ranking rows. Returns count updated.
    """
    with open(scored_path, encoding="utf-8") as f:
        scored = json.load(f)
    n = 0
    tk = scored.get("tickers", {})
    for sym, el in entry_map.items():
        rec = tk.get(sym)
        if rec is None or el is None:
            continue
        cp = _num(rec.get("current_price"))
        rec["_entry_level"] = el
        if cp and el:
            pct = round((cp - el) / el * 100, 2)
            rec["_pct_above_entry"] = pct
            rec["_gap_pct"] = pct
            rec["_in_window"] = pct <= threshold_pct
            rec["_in_window_note"] = "in_range" if rec["_in_window"] else "above_entry"
            n += 1
    for cr in scored.get("conviction_ranking", []):
        sym = cr.get("ticker")
        if sym in entry_map and entry_map[sym] is not None:
            cr["entry_level"] = entry_map[sym]
            rec = tk.get(sym, {})
            if "_in_window" in rec:
                cr["in_window"] = rec["_in_window"]
    with open(scored_out, "w", encoding="utf-8") as f:
        json.dump(scored, f, indent=2)
    return n


def run(metrics_path: str, watchlist_path: str, watchlist_out: str,
        audit_out: str, month_label: str,
        scored_path: str | None = None, scored_out: str | None = None) -> dict:
    with open(metrics_path, encoding="utf-8") as f:
        metrics = json.load(f)
    with open(watchlist_path, encoding="utf-8") as f:
        wt = json.load(f)
    tickers = metrics.get("tickers", {})
    threshold_pct = _num(wt.get("in_window_threshold_pct")) or 10.0
    entry_map: dict[str, float] = {}

    audit, dispo = [], {}
    sections = [("watchlist", wt.get("watchlist", [])),
                ("candidate_pool", wt.get("candidate_pool", []))]

    for sec_name, entries in sections:
        for e in entries:
            sym = e.get("ticker")
            t = tickers.get(sym)
            if not t:
                e.setdefault("entry_level_status", "no_metrics")
                audit.append({"ticker": sym, "section": sec_name,
                              "disposition": "no_metrics", "entry_level": e.get("entry_level")})
                dispo["no_metrics"] = dispo.get("no_metrics", 0) + 1
                continue
            t["ticker"] = sym
            rec = build_entry_for_ticker(t, e)
            fields, disposition = apply_governance(e, rec)
            e.update(fields)
            rec["section"] = sec_name
            rec["disposition"] = disposition
            rec["written_entry_level"] = fields.get("entry_level")
            if disposition == "approved_kept":
                # human-approved level retained; computed band/anchors are reference only
                rec["entry_band_low"] = None
                rec["entry_band_high"] = None
                rec["note"] = "existing human-approved entry retained; composite shown for reference"
            audit.append(rec)
            dispo[disposition] = dispo.get(disposition, 0) + 1
            if fields.get("entry_level") is not None:
                entry_map[sym] = fields["entry_level"]

    # validation: any top-10 watchlist name still null?
    top10_nulls = [e["ticker"] for e in wt.get("watchlist", [])
                   if e.get("entry_level") is None]
    warnings = []
    if top10_nulls:
        warnings.append(f"TOP-10 NULL ENTRY AFTER BUILDER: {top10_nulls}")

    conf_counts = {}
    for a in audit:
        c = a.get("entry_level_confidence")
        if c:
            conf_counts[c] = conf_counts.get(c, 0) + 1

    wt.setdefault("_meta", {})
    wt["_meta"]["last_updated"] = TODAY
    wt["_meta"]["entry_levels_built_by"] = f"entry_level_builder.py ({month_label})"

    audit_doc = {
        "_meta": {
            "month_label": month_label,
            "produced_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "in_scope_sections": ["watchlist", "candidate_pool"],
            "dispositions": dispo,
            "confidence_counts": conf_counts,
            "warnings": warnings,
        },
        "entries": audit,
    }

    # Propagate entry levels into the metrics file so normalise_adapter (and any rerun in
    # rerank_watchlist) computes in_window / pct_above_entry from fresh entry data.
    for _sym, _el in entry_map.items():
        if _sym in tickers:
            tickers[_sym]["_entry_level"] = _el
    try:
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, default=str)
    except Exception as _e:
        print(f"[entry_builder] WARN: could not write entry levels into metrics: {_e}")

    with open(watchlist_out, "w", encoding="utf-8") as f:
        json.dump(wt, f, indent=2)
    with open(audit_out, "w", encoding="utf-8") as f:
        json.dump(audit_doc, f, indent=2)

    if scored_path and scored_out:
        upd = _refresh_scored(scored_path, scored_out, entry_map, threshold_pct)
        print(f"[entry_builder] refreshed scored in_window for {upd} tickers (threshold {threshold_pct}%)")

    print(f"[entry_builder] dispositions={dispo}")
    print(f"[entry_builder] confidence={conf_counts}")
    if warnings:
        for w in warnings:
            print(f"[entry_builder] WARNING: {w}")
    else:
        print("[entry_builder] OK: no top-10 null entry levels after build")
    return audit_doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--watchlist", required=True)
    ap.add_argument("--watchlist-out", required=True)
    ap.add_argument("--audit-out", required=True)
    ap.add_argument("--month-label", required=True)
    ap.add_argument("--scored", default=None, help="optional watchlist_scored JSON to refresh in_window")
    ap.add_argument("--scored-out", default=None, help="output path for refreshed scored JSON")
    args = ap.parse_args()
    for p in (args.metrics, args.watchlist):
        if not os.path.exists(p):
            print(f"ERROR: {p} not found", file=sys.stderr); sys.exit(1)
    run(args.metrics, args.watchlist, args.watchlist_out, args.audit_out,
        args.month_label, args.scored, args.scored_out)


if __name__ == "__main__":
    main()
