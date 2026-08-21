#!/usr/bin/env python3
"""
fund_returns.py — fund-sleeve return sourcing + the real 12% gate (redesign retro #5, G1/G2).

The monthly review left the fund-sleeve return gate "pending" — est_return_pct=None on every fund,
filled manually from Morningstar each month. This module automates it as far as the data allows:

  SOURCING (hybrid, in priority order, per fund):
    1. fresh CACHE entry (fund_returns_cache.json) — quarterly; the reliable path for OEIC funds that
       have no exchange ticker (Fundsmith etc.), populated once from Morningstar and re-sourced quarterly.
    2. yfinance — for ticker-able funds/ETFs (e.g. *.L): info['threeYearAverageReturn'] if present,
       else a 3yr price-history CAGR proxy (price-only, so dividends understate — flagged in `source`).
    3. PENDING — neither available -> flagged for manual lookup (the old behaviour, for that fund only).

  GATE: value-weighted fund-sleeve return across COVERED funds; PASS/FAIL vs FUND_GATE_PCT only when
        coverage >= FUND_MIN_COVERAGE of fund-sleeve value (else PENDING — never a false PASS/FAIL).

Pure-computation parts (gate, classification, cache) are deterministic and unit-tested; the live
yfinance fetch is best-effort and injectable (fetch_fn) so callers/tests stay deterministic.
Additive: nothing here runs unless a caller invokes it (portfolio_analytics gates it behind a flag).
"""
from __future__ import annotations
import argparse, json, os, datetime

try:
    import scoring_config as _cfg
except Exception:
    _cfg = None
try:
    import action_language as _alang
except Exception:
    _alang = None

SCHEMA_VERSION = "1.1"

# ── ISA-0402 (20-Aug-2026) — THE BASIS IS A FIELD, NOT PROSE ──────────────────────────────────
# ⚑ THE DEFECT. `fund_returns_cache.json` declared in its own header that "Sources are forward
# E[r] decompositions, not trailing" while EIGHT of its twelve rows carried `[trailing 3yr]` in
# their own source strings. The header described the file, the rows described themselves, and they
# disagreed — indefinitely, because a basis recorded in FREE PROSE cannot be asserted. Every other
# decision-grade artefact in this framework carries basis as a first-class field for exactly this
# reason (er_basis, stamp_basis, window_basis, t4_basis, comparator_basis, return_adequacy_basis).
#
# ⚑ AND IT WAS LOAD-BEARING: the trailing rows are the ones feeding the SELL test in
# `classify_fund_action` (ISA-0401), where they are compared to a FORWARD hurdle.
#
# ⚑ THE CLASSIFICATION IS MECHANICAL AND READS ONLY EXPLICIT TAGS. It does not infer a basis from
# narrative — `[index]` and `[NAV-driven]` describe an instrument, not a measurement basis, so
# those rows read UNKNOWN and BLOCK rather than being generously read as forward. Deriving the
# field by judgement would have replaced one hand-set thing with another.
BASIS_FORWARD   = "forward_decomposition"
BASIS_TRAILING  = "trailing"          # + the window, e.g. trailing_3yr
BASIS_UNKNOWN   = "unknown"
BASIS_ENUM = (BASIS_FORWARD, "trailing_1yr", "trailing_3yr", "trailing_5yr",
              "manual_declared", BASIS_UNKNOWN)


def classify_basis(source: str) -> dict:
    """Source prose -> a declared basis. Explicit tags only; silence is UNKNOWN, never forward."""
    import re
    txt = str(source or "")
    m = re.search(r"\[\s*trailing\s*(\d+)\s*yr\s*\]", txt, re.I)
    if m:
        return {"basis": "trailing_%syr" % m.group(1), "basis_evidence": m.group(0),
                "basis_derivation": "explicit tag in the row's own source string"}
    if re.search(r"\b(fwd|forward)\b", txt, re.I):
        return {"basis": BASIS_FORWARD, "basis_evidence": "fwd/forward named in the source",
                "basis_derivation": "explicit tag in the row's own source string"}
    return {"basis": BASIS_UNKNOWN,
            "basis_evidence": None,
            "basis_derivation": ("no explicit basis tag. ⚑ NOT read as forward: a narrative label "
                                 "such as [index] or [NAV-driven] describes the INSTRUMENT, not the "
                                 "measurement basis, and 'unstated' and 'forward' must not produce "
                                 "the same output (R2.10)")}


def basis_is_forward(basis: str) -> bool:
    """The ONE home for 'may this value be compared to a FORWARD hurdle?' (R4.4)."""
    return basis == BASIS_FORWARD


def _g(key, default):
    return getattr(_cfg, key, default)


def _today() -> str:
    return datetime.date.today().isoformat()


def _key(fund: dict) -> str:
    return str(fund.get("ticker") or fund.get("name") or "").upper()


# --- cache -----------------------------------------------------------------
def load_cache(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "returns": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("returns"), dict):
            d.setdefault("schema_version", SCHEMA_VERSION)
            # ⚑ ISA-0402. Stamp the basis on every row AS IT IS READ, derived mechanically from
            # the row's own source string, so an older cache written before the field existed
            # cannot be consumed as though its basis were known (R5.1 — the consumer asserts the
            # semantics again as it reads).
            for _k, _r in (d.get("returns") or {}).items():
                if not isinstance(_r, dict):
                    continue
                if _r.get("basis") not in BASIS_ENUM:
                    _r.update(classify_basis(_r.get("source")))
            return d
    except Exception:
        pass
    return {"schema_version": SCHEMA_VERSION, "returns": {}}


def save_cache(cache: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, default=str)
    os.replace(tmp, path)


def _is_stale(date_str, max_days=None) -> bool:
    max_days = _g("FUND_RETURN_STALE_DAYS", 92) if max_days is None else max_days
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(str(date_str)[:10])).days
        return age > max_days
    except Exception:
        return True


# --- yfinance best-effort fetch -------------------------------------------
def fetch_yf_return(ticker: str):
    """Best-effort 3yr annualised total return (%) for a ticker-able fund/ETF. Returns
    (pct|None, source_str). Never raises."""
    try:
        import yfinance as yf
    except Exception:
        return None, "yfinance_unavailable"
    try:
        tk = yf.Ticker(ticker)
        info = getattr(tk, "info", {}) or {}
        tar = info.get("threeYearAverageReturn")
        if tar is not None:
            return round(float(tar) * 100, 2), "yfinance_3yr_avg_return"
        hist = tk.history(period="3y")
        closes = [float(c) for c in hist["Close"].tolist()] if hist is not None and not hist.empty else []
        closes = [c for c in closes if c and c > 0]
        if len(closes) >= 2:
            yrs = max(1.0, len(closes) / 252.0)
            cagr = (closes[-1] / closes[0]) ** (1.0 / yrs) - 1
            return round(cagr * 100, 2), "yfinance_price_cagr(px-only,div-understated)"
    except Exception:
        pass
    return None, "fetch_failed"


# --- sourcing --------------------------------------------------------------
def source_fund_returns(funds: list, cache_path: str = None, fetch: bool = True,
                        fetch_fn=None) -> dict:
    """Return {KEY: {est_return_pct, source, stale, pending}} for each fund, using
    cache -> yfinance -> pending. fetch_fn(ticker)->(pct,source) overrides the live fetch (tests)."""
    fetch_fn = fetch_fn or fetch_yf_return
    cache = load_cache(cache_path) if cache_path else {"returns": {}}
    out = {}
    for f in funds:
        k = _key(f)
        c = cache.get("returns", {}).get(k)
        if c and c.get("return_pct") is not None and not _is_stale(c.get("date")):
            out[k] = {"est_return_pct": c["return_pct"], "source": c.get("source", "cache"),
                      "stale": False, "pending": False}
            continue
        pct, src = (fetch_fn(f.get("ticker")) if (fetch and f.get("ticker")) else (None, "no_ticker"))
        if pct is not None:
            out[k] = {"est_return_pct": pct, "source": src, "stale": False, "pending": False,
                      "low_confidence": ("price_cagr" in (src or ""))}   # M5: price-only CAGR understates total return
        elif c and c.get("return_pct") is not None:
            # only a STALE cache value available — use it but flag stale for re-sourcing
            out[k] = {"est_return_pct": c["return_pct"], "source": c.get("source", "cache") + "(stale)",
                      "stale": True, "pending": False}
        else:
            out[k] = {"est_return_pct": None, "source": src, "stale": False, "pending": True}
    return out


# --- the 12% gate ----------------------------------------------------------
def compute_fund_gate(funds: list, returns: dict, gate_pct: float = None,
                      min_coverage: float = None) -> dict:
    """Value-weighted fund-sleeve return across COVERED funds + PASS/FAIL vs gate.
    PENDING (no PASS/FAIL) until coverage >= min_coverage of fund-sleeve value."""
    gate_pct = _g("FUND_GATE_PCT", 12.0) if gate_pct is None else gate_pct
    min_coverage = _g("FUND_MIN_COVERAGE", 0.80) if min_coverage is None else min_coverage
    total_val = sum((f.get("value_gbp") or 0) for f in funds)
    covered_val = w_ret = 0.0
    pending = []
    for f in funds:
        r = returns.get(_key(f), {})
        v = f.get("value_gbp") or 0
        # M5: a price-only CAGR understates total return (no dividends) -> exclude it from the PASS/FAIL
        # set (treat as pending) so an understated number can never produce a false FAIL.
        if r.get("est_return_pct") is not None and not r.get("low_confidence"):
            covered_val += v
            w_ret += v * r["est_return_pct"]
        else:
            pending.append(f.get("ticker") or f.get("name"))
    coverage = (covered_val / total_val) if total_val > 0 else 0.0
    weighted_avg = round(w_ret / covered_val, 2) if covered_val > 0 else None
    if coverage < min_coverage or weighted_avg is None:
        result = "PENDING"
    else:
        result = "PASS" if weighted_avg >= gate_pct else "FAIL"
    return {
        "weighted_avg_return": weighted_avg,
        "threshold_pct": gate_pct,
        "result": result,
        "coverage_pct": round(coverage * 100, 1),
        "covered_value_gbp": round(covered_val, 2),
        "pending_funds": pending,
        "status": "computed" if result != "PENDING" else "pending_estimated_returns",
    }


# --- fund actions (G2) -----------------------------------------------------
def classify_fund_action(fund_row: dict, ret_info: dict) -> dict | None:
    """Map a fund's drift + return into an action for the Global Action Stack agenda.
    Returns None for a clean Hold (no action). Uses canonical action language."""
    band_breach = str(fund_row.get("band_breach")) == "Yes"
    actual = fund_row.get("actual_pct")
    target = fund_row.get("target_pct")
    min_ret = fund_row.get("min_return_pct")
    est = (ret_info or {}).get("est_return_pct")
    overweight = (actual is not None and target is not None and actual > target)

    basis = (ret_info or {}).get("basis") or BASIS_UNKNOWN

    action = reason = None
    if band_breach and overweight:
        action, reason = "TRIM", f"overweight {actual:.1f}% vs target {target:.1f}% — rebalance down"
    elif band_breach and not overweight:
        action, reason = "ADD", f"underweight {actual:.1f}% vs target {target:.1f}% — rebalance up"

    # ── ISA-0401 (20-Aug-2026) — THE RETURN TEST MUST COMPARE LIKE WITH LIKE ─────────────────
    # ⚑ THE DEFECT. This branch issued SELL — the strongest verb in the canonical action language
    # ("Exit — disqualifier, thesis-break, or dead money with a funded replacement") — from
    # `est_return_pct < min_return_pct`, where the two sides are DIFFERENT QUANTITIES: a TRAILING
    # 3-year return on the left and a FORWARD structural hurdle on the right. It was LIVE in
    # analytics_data_aug_2026.json on JPM UK Equity Core: "est return 11.0% < min hurdle 12.0%",
    # where the 11.0 is a sentence typed in the July run, self-labelled `[trailing 3yr]`, dated
    # 2026-07-05, and the 12.0 is a hand-set constant last revised 31-May-2026. The register
    # already records the JPM UK reading as a K4 factual_error — this pair had produced a wrong
    # call once before.
    # ⚑ WHAT IS NOT DONE HERE, DELIBERATELY. The test is not re-pointed at ISA-0328's forward
    # structural E[r]: that runs 3.06-7.62% against 12-13% hurdles and would fire on ALL TWELVE
    # funds. Nor is it re-pointed at `return_adequacy_value` — that is the OWNERSHIP measure and
    # changing which funds carry a SELL as the side effect of a basis repair is D-C(ii). The
    # comparison is REFUSED and NAMED; choosing its replacement is a policy decision with its own
    # item.
    return_test = None
    if est is not None and min_ret is not None:
        if basis_is_forward(basis):
            if est < min_ret:
                action, reason = "SELL", (f"est return {est:.1f}% < min hurdle {min_ret:.1f}% "
                                          f"— review for replacement [basis {basis}]")
            return_test = {"state": "APPLIED", "basis": basis,
                           "est_pct": est, "hurdle_pct": min_ret,
                           "below": bool(est < min_ret)}
        else:
            # ⚑ COUNTED, NEVER DROPPED (R4.9). The row is emitted so the refusal is visible.
            return_test = {
                "state": "REFUSED_BASIS_MISMATCH", "basis": basis,
                "est_pct": est, "hurdle_pct": min_ret,
                "would_have_fired": bool(est < min_ret),
                "reason": (f"the estimate's declared basis is `{basis}` and the hurdle "
                           f"`min_expected_return` is a FORWARD structural expectation. A trailing "
                           f"return compared to a forward hurdle is not a comparison, and it may "
                           f"not produce a SELL (ISA-0401, R2.6/R5.1)."),
                "what_would_resolve_it": ("re-source this fund's estimate on a forward "
                                          "decomposition basis, or declare which measure the "
                                          "ownership test should use — see the item")}
            if action == "ADD" and est < min_ret:
                # the old code let SELL override a rebalance-ADD. With the test refused, the ADD
                # stands but the unresolved question travels with it rather than vanishing.
                reason += " ⚑ return test REFUSED (basis mismatch) — not a clean bill of health"

    if action is None and return_test is None:
        return None
    if action is None:
        # a refused return test with no rebalancing action still has to be VISIBLE
        return {"ticker": fund_row.get("ticker"), "name": fund_row.get("name"), "route": "fund",
                "action": None, "canonical_action": None,
                "action_label": "NO ACTION — the return test was refused, see return_test",
                "reason": return_test.get("reason"), "return_test": return_test,
                "est_return_pct": est, "min_return_pct": min_ret, "est_basis": basis,
                "actual_pct": actual, "target_pct": target,
                "source_required": bool((ret_info or {}).get("pending"))}
    canon = _alang.normalize_action(action) if _alang else action
    label = _alang.label_for(action) if _alang else action
    return {
        "ticker": fund_row.get("ticker"), "name": fund_row.get("name"), "route": "fund",
        "action": action, "canonical_action": canon, "action_label": label,
        "reason": reason, "est_return_pct": est, "min_return_pct": min_ret,
        "est_basis": basis, "return_test": return_test,
        "actual_pct": actual, "target_pct": target,
        "source_required": bool((ret_info or {}).get("pending")),
    }


def default_cache_path(inv_dir: str) -> str:
    return os.path.join(inv_dir, "fund_returns_cache.json")


def set_cached_return(path, ticker, return_pct, source="manual", date=None) -> dict:
    """Write a fund's 3yr annualised return into the cache. Source-agnostic — `source` records
    provenance (ajbell / morningstar / hl / manual). This is how the quarterly OEIC refresh lands."""
    cache = load_cache(path)
    entry = {"return_pct": round(float(return_pct), 2), "source": source, "date": date or _today()}
    cache.setdefault("returns", {})[str(ticker).upper()] = entry
    save_cache(cache, path)
    return entry


def selftest(verbose=True) -> int:
    """ISA-0402 / ISA-0401. Every control must FAIL on the real defect it exists to catch (R5.8)."""
    fails = []

    def ck(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    # ── classify_basis reads EXPLICIT TAGS ONLY ──────────────────────────────────────────────
    ck("an explicit [trailing 3yr] tag is classified trailing_3yr",
       classify_basis("Est. return 11.0% (UK core [trailing 3yr])")["basis"] == "trailing_3yr")
    ck("an explicit fwd tag is classified forward_decomposition",
       classify_basis("(AI-supercycle tech earnings growth [Morningstar fwd proxy])")["basis"]
       == BASIS_FORWARD)
    ck("ISA-0402 CONTROL: a NARRATIVE label is NOT read as forward — [index] and [NAV-driven] "
       "describe the instrument, not the basis, so they read `unknown` and BLOCK (R2.10)",
       classify_basis("Japan index, corporate-reform tailwind [index]")["basis"] == BASIS_UNKNOWN
       and classify_basis("High-growth + SpaceX/AI NAV uplift [NAV-driven]")["basis"] == BASIS_UNKNOWN)
    ck("ISA-0402 CONTROL: an EMPTY source is unknown, never forward",
       classify_basis(None)["basis"] == BASIS_UNKNOWN and classify_basis("")["basis"] == BASIS_UNKNOWN)
    ck("basis_is_forward is the ONE home and is true for exactly one value (R4.4)",
       basis_is_forward(BASIS_FORWARD)
       and not any(basis_is_forward(b) for b in BASIS_ENUM if b != BASIS_FORWARD))

    # ── the SELL may not rest on a basis mismatch ────────────────────────────────────────────
    row = {"ticker": "TEST", "name": "T", "band_breach": "No", "actual_pct": 6.0,
           "target_pct": 6.0, "min_return_pct": 12.0}
    fwd = classify_fund_action(row, {"est_return_pct": 11.0, "basis": BASIS_FORWARD})
    ck("a FORWARD estimate below a forward hurdle still produces SELL — the test is not disabled",
       fwd and fwd["action"] == "SELL" and (fwd["return_test"] or {})["state"] == "APPLIED")
    tr = classify_fund_action(row, {"est_return_pct": 11.0, "basis": "trailing_3yr"})
    ck("ISA-0401 CONTROL: the SAME numbers on a TRAILING basis produce NO SELL — a trailing return "
       "compared to a forward hurdle is not a comparison (R2.6)",
       tr and tr["action"] is None
       and tr["return_test"]["state"] == "REFUSED_BASIS_MISMATCH")
    ck("ISA-0401: and the question is NOT dropped — the refusal records that it WOULD have fired "
       "and names what would resolve it (R4.9)",
       tr["return_test"]["would_have_fired"] is True
       and tr["return_test"]["what_would_resolve_it"])
    unk = classify_fund_action(row, {"est_return_pct": 11.0, "basis": BASIS_UNKNOWN})
    ck("ISA-0401 CONTROL: an UNKNOWN basis refuses too — absent is not forward",
       unk and unk["action"] is None
       and unk["return_test"]["state"] == "REFUSED_BASIS_MISMATCH")
    ck("ISA-0401 CONTROL: a missing basis key defaults to UNKNOWN and refuses, it does not default "
       "to forward (R4.3)",
       (classify_fund_action(row, {"est_return_pct": 11.0}) or {}).get("action") is None)

    # ── the rebalancing path is UNTOUCHED by the basis question ──────────────────────────────
    br = {"ticker": "T2", "name": "T2", "band_breach": "Yes", "actual_pct": 9.1,
          "target_pct": 7.0, "min_return_pct": 12.0}
    tb = classify_fund_action(br, {"est_return_pct": 12.0, "basis": "trailing_3yr"})
    ck("a band-breach TRIM is unaffected by the return test being refused — the fix removes a "
       "wrong SELL, it does not loosen rebalancing (D-C(ii))",
       tb and tb["action"] == "TRIM")

    # ── the live cache ───────────────────────────────────────────────────────────────────────
    import os as _os
    p = default_cache_path(_os.path.dirname(_os.path.abspath(__file__)))
    if _os.path.exists(p):
        c = load_cache(p)
        rows = c.get("returns") or {}
        ck("every row in the live cache carries a declared basis in the enum (R4.2)",
           rows and all(r.get("basis") in BASIS_ENUM for r in rows.values()))
        ck("ISA-0402: the header no longer makes a FILE-LEVEL claim contradicted by the rows",
           "CORRECTED" in str(c.get("_comment") or "")
           or "not trailing" not in str(c.get("_comment") or ""))

    print("\nfund_returns selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails else " — 13 assertions green"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(
        description="Fund-return cache (G1) — set / get / list cached 3yr annualised fund returns. "
                    "Source-agnostic: populate from AJ Bell, Morningstar or HL at the quarterly refresh.")
    ap.add_argument("--path", required=True, help="fund_returns_cache.json path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="write a fund's 3yr annualised return to the cache")
    s.add_argument("--ticker", required=True)
    s.add_argument("--return", dest="ret", type=float, required=True, help="3yr annualised return %%")
    s.add_argument("--source", default="manual", help="provenance: ajbell / morningstar / hl / manual")
    s.add_argument("--date", default=None, help="assessment date YYYY-MM-DD (default today)")
    g = sub.add_parser("get", help="show one fund's cached return + staleness")
    g.add_argument("--ticker", required=True)
    sub.add_parser("list", help="list all cached returns + staleness")
    a = ap.parse_args()
    if a.cmd == "set":
        e = set_cached_return(a.path, a.ticker, a.ret, a.source, a.date)
        print(f"SET {a.ticker.upper()} = {e['return_pct']}% (source={e['source']}, date={e['date']})")
    elif a.cmd == "get":
        e = load_cache(a.path).get("returns", {}).get(a.ticker.upper())
        print(json.dumps({**e, "stale": _is_stale(e.get("date"))}, indent=2) if e
              else f"NO_CACHE for {a.ticker.upper()}")
    elif a.cmd == "list":
        rows = sorted(load_cache(a.path).get("returns", {}).items())
        if not rows:
            print("(empty cache)")
        for t, e in rows:
            flag = "STALE" if _is_stale(e.get("date")) else "ok"
            print(f"{t:14} {str(e.get('return_pct')):>7}%  {flag:5}  {e.get('source','')}  {e.get('date','')}")


if __name__ == "__main__":
    main()
