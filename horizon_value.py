#!/usr/bin/env python3
"""
horizon_value.py — ISA-0744 (24-Sep-2026). THE 12-month horizon-value E[r], in SHADOW.

Authority: Forecast & Underwriting Integrity BuildSpec v2 (24-Sep-2026) §5.1, §10.1-10.5, Raj's
session-6 instruction. Consumed by NOTHING that moves capital until Raj promotes it (§16.4).

    E[r] = (terminal_shareholder_wealth / approval_price) ** (1 / h) - 1,   h = 1 year
    W_T  = P_T + horizon cash dividends

P/E route (§10.2):   P_T = EPS1 * (P0 / EPS0) * (1 + r)       FY1 EPS ONCE; no FY2; no buyback.
EV/EBITDA route (§10.3):
    NTM_REV  = w0 * REV(0y) + (1 - w0) * REV(+1y)             w0 = share of FY0 left in the horizon
    MARGIN   = TTM_EBITDA / TTM_REV                           production base (TTM, 4 quarters)
    M0       = (P0 * SHARES + ND) / TTM_EBITDA                SAME EBITDA definition as the base
    EV_T     = NTM_REV * MARGIN * M0 * (1 + r)
    P_T      = (EV_T - ND_BASE) / SHARES_BASE                 share count enters ONCE (ISA-0745)

r is the APPROVED bounded re-rate already stamped on the row by expected_return (not
recalibrated here). The cross-check route is evaluated at the same r, so it tests the
earnings/EBITDA leg with the valuation view held constant; its disagreement on hurdle pass/fail
is a REVIEW trigger, never averaged (§5.2).

Sensitivities (§10.4) — never production authority: margin at lambda=0.38 partial reversion to the
reference median, at the reference median, at the completed-FY high and low; net debt moved by its
last completed-FY change; diluted shares moved by their completed-FY CAGR. Any sensitivity that
flips the hurdle -> REVIEW_REQUIRED. A missing MANDATORY margin sensitivity (fewer than 3 completed
FY margins) -> REVIEW_REQUIRED. Net-debt / share sensitivities run where supported (§10.4).

Typed states (§10.1): VALID_MECHANICAL, REVIEW_REQUIRED, MISSING_REQUIRED_INPUT, STALE,
CONFLICTED, NOT_DEFENSIBLY_QUANTIFIABLE, METHOD_CHANGED. Missing is never zero; an ABSENT dividend
gives an ex-dividend LOWER BOUND and is recorded as unknown, not as zero (§7.2). P/FCF keeps its
bounded refusal (§7.4). No FX is forecast: a price/financial currency mismatch is CONFLICTED; the
pence/pound family is a unit conversion, detected against the provider's own forward P/E.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
HV_METHOD_VERSION = "HV-1.2"   # 1.1 (ISA-0747/0197): period normalisation + three-currency basis
                               # 1.2 (ISA-0197 FX closure 24-Sep): ECB SDMX FX authority (ecb_fx), exp(+-sigma)
                               #     1y AND 3y sensitivities, prospective route record; yfinance FX retired;
                               #     ESTIMATE currency basis (GMAB DKK revenue consensus vs USD reporting)
LAMBDA_SENSITIVITY = 0.38                       # literature prior - SENSITIVITY ONLY (§5.2)
MIN_COMPLETED_FY_MARGINS = 3                    # §10.4
PE_FIELDS = {"fwd_pe", "trailing_pe", "val_hist_current_pe", "current_pe"}
EV_FIELDS = {"ev_ebitda"}
PFCF_FIELDS = {"price_fcf"}
# ISA-0744/0197 (24-Sep-2026): quotation SUB-UNITS are a deterministic unit, never FX.
SUBUNIT_MAJOR = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ILA": ("ILS", 100.0), "ZAc": ("ZAR", 100.0)}
BASE_CCY = "GBP"                                # the ISA portfolio/base currency

VALID = "VALID_MECHANICAL"
REVIEW = "REVIEW_REQUIRED"
MISSING = "MISSING_REQUIRED_INPUT"
STALE = "STALE"
CONFLICTED = "CONFLICTED"
NDQ = "NOT_DEFENSIBLY_QUANTIFIABLE"
METHOD_CHANGED = "METHOD_CHANGED"
STATES = (VALID, REVIEW, MISSING, STALE, CONFLICTED, NDQ, METHOD_CHANGED)


try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                                            # noqa: D103
        return None


def method_id() -> str:
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return "horizon_value@" + hashlib.sha256(fh.read()).hexdigest()[:12]
    except Exception:                                                   # noqa: BLE001
        return "horizon_value@UNREADABLE"


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _date(x):
    if x is None:
        return None
    if isinstance(x, datetime.date):
        return x if not isinstance(x, datetime.datetime) else x.date()
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _hurdle(hurdle_pct):
    if hurdle_pct is not None:
        return float(hurdle_pct)
    import scoring_config as _cfg
    return float(_cfg.ER_DEPLOY_FLOOR)


def _price(rec):
    info = rec.get("info") or {}
    return _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))


def currency_basis(rec, fx=None):
    """ISA-0744 / ISA-0197 — THREE distinct quantities: the issuer's REPORTING currency, the
    security's QUOTE currency and unit, and the ISA BASE currency (GBP).
    -> {"state", "quote", "quote_major", "unit_div", "reporting", "rate_quote_to_rep", "why"}.
    The quote unit is normalised first (GBp -> GBP / 100). A quote currency that differs from the
    reporting currency is NOT a conflict: valuation runs in the reporting currency and the quoted
    price is converted with the SUPPLIED as-of spot (fx["<QUOTE><REP>"] = units of REP per 1 QUOTE).
    Only a missing rate is refused (CONFLICTED / FX_UNAVAILABLE). No FX is forecast."""
    info = rec.get("info") or {}
    pc = info.get("currency")
    if not pc:
        return {"state": MISSING, "why": "no quote currency"}
    qmaj, div = SUBUNIT_MAJOR.get(pc, (pc, 1.0))
    rep = info.get("financialCurrency") or qmaj
    out = {"quote": pc, "quote_major": qmaj, "unit_div": div, "reporting": rep}
    if rep == qmaj:
        return dict(out, state=VALID, rate_quote_to_rep=1.0, fx_source="IDENTITY")
    key = "%s%s" % (qmaj, rep)
    rate = _num((fx or {}).get(key))
    if rate is None:
        inv = _num((fx or {}).get("%s%s" % (rep, qmaj)))
        rate = (1.0 / inv) if inv else None
    if rate is None or rate <= 0:
        return dict(out, state=CONFLICTED, why="FX_UNAVAILABLE: no as-of spot %s for a %s-quoted, %s-reporting "
                                               "issuer - refused, never assumed 1.0" % (key, qmaj, rep))
    return dict(out, state=VALID, rate_quote_to_rep=rate, fx_source="SPOT %s" % key)


def _dividend_major(rec, cb, p0_major):
    """Dividend per share in the QUOTE-MAJOR currency. A sub-unit quote is disambiguated against the
    provider's own dividendYield (a unit, not an estimate); undecidable -> treated as ABSENT."""
    d = rec.get("dividend") or {}
    st = d.get("state")
    if st == "ZERO":
        return 0.0, "ZERO"
    if st != "PRESENT":
        return None, (st or "ABSENT")
    v = _num(d.get("value"))
    if cb.get("unit_div", 1.0) == 1.0:
        return v, "PRESENT"
    y = _num(d.get("yield_raw"))
    if y and p0_major:
        c_major, c_minor = v, v / cb["unit_div"]
        best = min((c_major, c_minor), key=lambda c: abs(math.log(max(c / p0_major * 100.0, 1e-12) / y)))
        return best, "PRESENT"
    return None, "UNIT_UNDECIDABLE"


def _fiscal(rec, as_of):
    """-> (w0, fy0_end, why). w0 = share of the 12-month horizon inside the current fiscal year."""
    info = rec.get("info") or {}
    nfy = _num(info.get("nextFiscalYearEnd"))
    if nfy is None:
        return None, None, "no nextFiscalYearEnd"
    fy0_end = datetime.datetime.utcfromtimestamp(nfy).date()
    days = (fy0_end - as_of).days
    if days <= 0:
        return None, fy0_end, "fiscal rollover: provider FY0 ended %s before as_of %s" % (fy0_end, as_of)
    return min(days / 365.0, 1.0), fy0_end, None


def _dividend(rec):
    """Legacy helper kept for callers that do not need the unit (SHADOW v1 fixtures)."""
    d = rec.get("dividend") or {}
    st = d.get("state")
    if st == "PRESENT":
        return _num(d.get("value")), "PRESENT"
    if st == "ZERO":
        return 0.0, "ZERO"
    return None, (st or "ABSENT")


def _wealth(p_t, p0, div):
    w = p_t + (div or 0.0)
    return round(100.0 * (w / p0 - 1.0), 2)


def estimate_basis(rec, table, fx=None):
    """ISA-0197 (found 24-Sep live census): consensus tables carry their OWN currency, which can
    differ from the reporting currency (GMAB: revenue consensus DKK, financials USD). Estimates
    are converted into REPORTING currency with the supplied as-of ECB spot - the same treatment as
    the quote->reporting conversion; never forecast. Unstated -> assumed reporting (recorded).
    Mixed FY0/FY1 currencies, or a missing rate -> CONFLICTED. -> dict(state, factor, ...)."""
    info = rec.get("info") or {}
    pc = info.get("currency")
    qmaj = SUBUNIT_MAJOR.get(pc, (pc, 1.0))[0] if pc else None
    repc = info.get("financialCurrency") or qmaj
    est = rec.get(table) or {}
    ccys = {(est.get(k) or {}).get("currency") for k in ("0y", "+1y")} - {None}
    out = {"table": table, "estimate_currency": None, "reporting": repc}
    if not ccys:
        return dict(out, state=VALID, factor=1.0, basis="UNSTATED_ASSUMED_REPORTING")
    if len(ccys) > 1:
        return dict(out, state=CONFLICTED, factor=None,
                    why="ESTIMATE_CURRENCY_MISMATCH: %s FY0/FY1 in different currencies %s" % (table, sorted(ccys)))
    ec = ccys.pop()
    out["estimate_currency"] = ec
    if ec == repc:
        return dict(out, state=VALID, factor=1.0, basis="SAME_AS_REPORTING")
    rate = _num((fx or {}).get(ec + repc))
    if rate is None or rate <= 0:
        inv = _num((fx or {}).get(repc + ec))
        rate = (1.0 / inv) if inv and inv > 0 else None
    if rate is None:
        return dict(out, state=CONFLICTED, factor=None,
                    why="ESTIMATE_CURRENCY_MISMATCH / FX_UNAVAILABLE: %s in %s, reporting %s, no as-of spot %s%s"
                        % (table, ec, repc, ec, repc))
    return dict(out, state=VALID, factor=rate, basis="SPOT %s%s (ECB as-of)" % (ec, repc))


def gbp_return(r_local_pct, r_fx_pct=0.0):
    """ISA-0197: 1 + R_GBP = (1 + R_local) x (1 + R_FX->GBP) - multiplicative, never added."""
    return round(100.0 * ((1.0 + r_local_pct / 100.0) * (1.0 + r_fx_pct / 100.0) - 1.0), 2)


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def pe_route(rec, r, as_of, *, p0=None, fx=None):
    est = rec.get("eps_estimate") or {}
    eps0 = _num((est.get("0y") or {}).get("avg"))
    eps1 = _num((est.get("+1y") or {}).get("avg"))
    p0 = p0 or _price(rec)
    out = {"route": "PE", "inputs": {"P0_quote": p0, "EPS0": eps0, "EPS1": eps1, "r": r}}
    if p0 is None or p0 <= 0:
        return dict(out, state=MISSING, why="no approval price")
    if eps0 is None or eps1 is None:
        return dict(out, state=MISSING, why="FY0/FY1 consensus EPS missing")
    if eps0 <= 0 or eps1 <= 0:
        return dict(out, state=NDQ, why="EPS0/EPS1 not positive - P/E route unavailable (§10.2)")
    if r is None:
        return dict(out, state=MISSING, why="approved re-rate UNMEASURED")
    w0, fy0_end, why = _fiscal(rec, as_of)
    if why:
        return dict(out, state=CONFLICTED if "rollover" in why else MISSING, why=why)
    eb = estimate_basis(rec, "eps_estimate", fx)
    out["inputs"]["estimate_basis"] = eb
    if eb["state"] != VALID:
        return dict(out, state=eb["state"], why=eb.get("why"))
    eps0, eps1 = eps0 * eb["factor"], eps1 * eb["factor"]
    cb = currency_basis(rec, fx)
    if cb["state"] != VALID:
        return dict(out, state=cb["state"], why=cb.get("why"), currency=cb)
    p0_major = p0 / cb["unit_div"]
    p0_rep = p0_major * cb["rate_quote_to_rep"]                  # valuation in REPORTING currency
    pe0 = p0_rep / eps0
    p_t_rep = eps1 * pe0 * (1.0 + r)                             # FY1 EPS ONCE - never grown again
    p_t_major = p_t_rep / cb["rate_quote_to_rep"]                # spot-flat back to the quote currency
    div, dstate = _dividend_major(rec, cb, p0_major)
    return dict(out, state=VALID, er_pct=_wealth(p_t_major, p0_major, div), P_T=round(p_t_major, 4),
                PE0=round(pe0, 3), currency=cb, fy0_end=str(fy0_end), dividend_state=dstate,
                dividend_lower_bound=(dstate not in ("PRESENT", "ZERO")))


# ── ISA-0747 — the reporting-period normalisation contract ─────────────────────────────────
def ttm_from_ytd(fy, ytd_cur, ytd_prior):
    """FLOW measure over the trailing 12 months from a completed FY plus the current interim/YTD
    less the corresponding prior-year interim/YTD. Pure arithmetic on already-validated inputs
    (e.g. Clarkson revenue 631.4 + 413.5 - 297.8 = 747.1)."""
    if None in (fy, ytd_cur, ytd_prior):
        return None
    return fy + ytd_cur - ytd_prior


def _d(x):
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def flow_ttm(rec, rows=("Total Revenue", "EBITDA")):
    """ONE trailing-12-month observation of the flow `rows`, all on the SAME period and source.
    Order: (1) the provider's DIRECT TTM, validated - its end date must not precede the latest
    completed FY (else STALE / PROVIDER_LAG, typed separately from cadence) and, when four
    consecutive quarters end on the same date, the divergence from their sum is RECORDED;
    a quarterly table FRESHER than the provider TTM wins (provider lag); (2) the sum of four
    consecutive same-definition quarters; (3) FY + current YTD - prior-year YTD where an interim
    table with matching period lengths exists; else MISSING. Never mixes sources across rows."""
    ttm = rec.get("income_stmt_ttm") or {}
    q = rec.get("income_stmt_quarterly") or {}
    a = rec.get("income_stmt_annual") or {}
    qd = sorted((dd for dd, c in q.items() if all(_num((c or {}).get(r)) is not None for r in rows)), reverse=True)
    q4 = qd[:4]
    q4_ok = len(q4) == 4 and all(70 <= (_d(q4[i]) - _d(q4[i + 1])).days <= 112 for i in range(3))
    sum4 = {r: sum(_num(q[dd][r]) for dd in q4) for r in rows} if q4_ok else None
    fy_end = max((dd for dd, c in a.items() if all(_num((c or {}).get(r)) is not None for r in rows)), default=None)
    tcols = sorted((dd for dd, c in ttm.items() if all(_num((c or {}).get(r)) is not None for r in rows)), reverse=True)
    if tcols:
        end = tcols[0]
        vals = {r: _num(ttm[end][r]) for r in rows}
        if q4_ok and q4[0] > end:
            return {"state": VALID, "source": "SUM_4Q", "end": q4[0], "values": sum4,
                    "why": "provider TTM (%s) lags the quarterly table (%s) - PROVIDER_LAG" % (end, q4[0])}
        if fy_end and end < fy_end:
            return {"state": STALE, "source": "PROVIDER_TTM", "end": end, "values": vals,
                    "why": "PROVIDER_LAG: provider TTM %s precedes the latest completed FY %s" % (end, fy_end)}
        div = ({r: (round(100.0 * (vals[r] / sum4[r] - 1.0), 3) if sum4[r] else None) for r in rows}
               if q4_ok and q4[0] == end else None)
        return {"state": VALID, "source": "PROVIDER_TTM", "end": end, "values": vals,
                "divergence_vs_4q_pct": div}
    if q4_ok:
        return {"state": VALID, "source": "SUM_4Q", "end": q4[0], "values": sum4}
    it = rec.get("income_stmt_interim") or {}
    if it and fy_end:
        cur = max(it)
        prior = [dd for dd in it if abs((_d(cur) - _d(dd)).days - 365) <= 7]
        if prior and _d(cur) > _d(fy_end) and it[cur].get("period_months") == it[prior[0]].get("period_months"):
            vals = {r: ttm_from_ytd(_num(a[fy_end].get(r)), _num(it[cur].get(r)), _num(it[prior[0]].get(r)))
                    for r in rows}
            if all(v is not None for v in vals.values()):
                return {"state": VALID, "source": "FY_PLUS_YTD", "end": cur, "values": vals}
    return {"state": MISSING, "source": None,
            "why": "no provider TTM, no 4 consecutive quarters and no reconcilable FY + YTD - prior YTD"}


def latest_diluted_shares(rec):
    """STOCK measure: the latest valid point-in-time diluted share observation (no TTM formula)."""
    obs = []
    for src, key in (("TTM", "income_stmt_ttm"), ("QUARTERLY", "income_stmt_quarterly"),
                     ("ANNUAL", "income_stmt_annual")):
        for dd, c in (rec.get(key) or {}).items():
            v = _num((c or {}).get("Diluted Average Shares"))
            if v and v > 0:
                obs.append((dd, src, v))
    if not obs:
        return None
    dd, src, v = max(obs, key=lambda t: (t[0], {"TTM": 2, "QUARTERLY": 1, "ANNUAL": 0}[t[1]]))
    return {"value": v, "as_of": dd, "source": src}


def _stmt_rows(tbl, row):
    """[(date, value)] newest first for one statement row."""
    out = []
    for d, col in sorted((tbl or {}).items(), reverse=True):
        v = _num((col or {}).get(row))
        if v is not None:
            out.append((d, v))
    return out


def ev_components(rec, as_of, *, p0=None, fx=None):
    info = rec.get("info") or {}
    a = rec.get("income_stmt_annual") or {}
    bs = rec.get("balance_sheet_annual") or {}
    p0 = p0 or _price(rec)
    c = {"P0_quote": p0}
    ft = flow_ttm(rec)
    c["ttm"] = {k: ft.get(k) for k in ("source", "end", "divergence_vs_4q_pct", "why")}
    if ft["state"] != VALID:
        return c, ft["state"], ft.get("why")
    ttm_r, ttm_e = ft["values"]["Total Revenue"], ft["values"]["EBITDA"]
    c.update(TTM_EBITDA=ttm_e, TTM_REV=ttm_r)
    if ttm_r <= 0 or ttm_e <= 0:
        return c, NDQ, "TTM EBITDA/revenue not positive - EV/EBITDA multiple not meaningful (§10.3)"
    est = rec.get("revenue_estimate") or {}
    r0, r1 = _num((est.get("0y") or {}).get("avg")), _num((est.get("+1y") or {}).get("avg"))
    if r0 is None or r1 is None or r0 <= 0 or r1 <= 0:
        return c, MISSING, ("FY0/FY1 revenue estimates missing or non-positive (%s, %s) - a broken source "
                            "field, NTM revenue cannot be formed" % (r0, r1))
    w0, fy0_end, why = _fiscal(rec, as_of)
    if why:
        return c, (CONFLICTED if "rollover" in why else MISSING), why
    eb = estimate_basis(rec, "revenue_estimate", fx)
    c["estimate_basis"] = eb
    if eb["state"] != VALID:
        return c, eb["state"], eb.get("why")
    r0, r1 = r0 * eb["factor"], r1 * eb["factor"]
    c.update(NTM_REV=w0 * r0 + (1.0 - w0) * r1, w0=round(w0, 4), fy0_end=str(fy0_end))
    td, tc = _num(info.get("totalDebt")), _num(info.get("totalCash"))
    if td is None or tc is None:
        return c, MISSING, "current net debt (totalDebt/totalCash) missing"
    c["ND"] = td - tc                                           # reporting currency, MRQ (stock)
    sh = latest_diluted_shares(rec)
    if not sh:
        return c, MISSING, "no diluted share observation (basic count is a different quantity)"
    c["SHARES"], c["shares_asof"], c["shares_source"] = sh["value"], sh["as_of"], sh["source"]
    cb = currency_basis(rec, fx)
    c["currency"] = cb
    if cb["state"] != VALID:
        return c, cb["state"], cb.get("why")
    c["p0_major"] = p0 / cb["unit_div"]
    c["p0_rep"] = c["p0_major"] * cb["rate_quote_to_rep"]
    ev0 = c["p0_rep"] * c["SHARES"] + c["ND"]
    c["M0"] = ev0 / ttm_e
    if c["M0"] <= 0:
        return c, NDQ, "current EV/EBITDA not positive"
    ae, ar = dict(_stmt_rows(a, "EBITDA")), dict(_stmt_rows(a, "Total Revenue"))
    c["fy_margins"] = [round(ae[d] / ar[d], 5) for d in sorted(set(ae) & set(ar), reverse=True) if ar[d] > 0]
    nd_hist = _stmt_rows(bs, "Net Debt")
    if len(nd_hist) < 2:
        td_h, ch_h = dict(_stmt_rows(bs, "Total Debt")), dict(_stmt_rows(bs, "Cash And Cash Equivalents"))
        nd_hist = [(d, td_h[d] - ch_h[d]) for d in sorted(set(td_h) & set(ch_h), reverse=True)]
    c["nd_fy_change"] = (nd_hist[0][1] - nd_hist[1][1]) if len(nd_hist) >= 2 else None
    ash = _stmt_rows(a, "Diluted Average Shares")
    c["shares_cagr"] = ((ash[0][1] / ash[-1][1]) ** (1.0 / (len(ash) - 1)) - 1.0
                        if len(ash) >= 2 and ash[-1][1] > 0 else None)
    return c, VALID, None


def ev_value(c, r, *, margin=None, nd=None, shares=None):
    """-> (terminal price in the QUOTE-MAJOR currency, state, why). Valued in the reporting
    currency and translated back at spot-flat."""
    m = c["TTM_EBITDA"] / c["TTM_REV"] if margin is None else margin
    ebitda_t = c["NTM_REV"] * m
    if ebitda_t <= 0:
        return None, NDQ, "terminal EBITDA not positive"
    eq_t = ebitda_t * c["M0"] * (1.0 + r) - (c["ND"] if nd is None else nd)
    if eq_t <= 0:
        return None, NDQ, "terminal equity not positive - no negative share-price artefact (§10.3)"
    p_t_rep = eq_t / (c["SHARES"] if shares is None else shares)
    return p_t_rep / c["currency"]["rate_quote_to_rep"], VALID, None


def ev_route(rec, r, as_of, *, p0=None, hurdle_pct=None, fx=None):
    c, st, why = ev_components(rec, as_of, p0=p0, fx=fx)
    out = {"route": "EV", "inputs": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in c.items()},
           "r": r}
    if st != VALID:
        return dict(out, state=st, why=why)
    if r is None:
        return dict(out, state=MISSING, why="approved re-rate UNMEASURED")
    div, dstate = _dividend_major(rec, c["currency"], c["p0_major"])
    p_t, st, why = ev_value(c, r)
    if st != VALID:
        return dict(out, state=st, why=why)
    base = _wealth(p_t, c["p0_major"], div)
    h = _hurdle(hurdle_pct)
    sens, mandatory_missing = [], []
    tm = c["TTM_EBITDA"] / c["TTM_REV"]
    fy = c.get("fy_margins") or []
    if len(fy) >= MIN_COMPLETED_FY_MARGINS:
        import statistics as _st
        ref = _st.median([tm] + fy)
        for name, m in (("margin_lambda_0.38", tm - LAMBDA_SENSITIVITY * (tm - ref)),
                        ("margin_reference_median", ref), ("margin_fy_high", max(fy)),
                        ("margin_fy_low", min(fy))):
            pt, s2, w2 = ev_value(c, r, margin=m)
            sens.append({"name": name, "margin": round(m, 5),
                         "er_pct": (_wealth(pt, c["p0_major"], div) if pt is not None else None), "state": s2})
    else:
        mandatory_missing.append("margin sensitivity: %d completed FY margins < %d"
                                 % (len(fy), MIN_COMPLETED_FY_MARGINS))
    if c.get("nd_fy_change") is not None:
        pt, s2, _ = ev_value(c, r, nd=c["ND"] + c["nd_fy_change"])
        sens.append({"name": "net_debt_fy_trend", "er_pct": (_wealth(pt, c["p0_major"], div) if pt else None), "state": s2})
    if c.get("shares_cagr") is not None:
        pt, s2, _ = ev_value(c, r, shares=c["SHARES"] * (1.0 + c["shares_cagr"]))
        sens.append({"name": "diluted_shares_fy_cagr", "er_pct": (_wealth(pt, c["p0_major"], div) if pt else None), "state": s2})
    for s_ in sens:
        s_["flips_hurdle"] = (s_["er_pct"] is None) or ((s_["er_pct"] >= h) != (base >= h))
    return dict(out, state=VALID, er_pct=base, P_T=round(p_t, 4), sensitivities=sens,
                mandatory_sensitivity_missing=mandatory_missing, dividend_state=dstate,
                dividend_lower_bound=(dstate not in ("PRESENT", "ZERO")))


# ── the case ────────────────────────────────────────────────────────────────────────────────
STRESS_POLICIES = ("GATE", "INFORMATIONAL")
STRESS_SENSITIVITIES = ("margin_lambda_0.38", "margin_reference_median", "margin_fy_high", "margin_fy_low",
                        "net_debt_fy_trend", "diluted_shares_fy_cagr")


def case(rec, *, as_of, hurdle_pct=None, event_state=None, max_age_days=None, fx=None, fx_vol=None,
         stress_policy="GATE"):
    """ONE canonical horizon-value case for a PIT record. Never raises.
    `fx`: {"<QUOTE><REP>": spot} as-of ECB rates; `fx_vol`: {"<QUOTE_MAJOR>": {"1y": sigma, "3y": sigma}}
    annualised realised sigma of the QUOTE->GBP rate (ecb_fx) for the informational FX sensitivity. `stress_policy` (SHADOW comparison only):
    GATE (current, default) - a stress sensitivity that flips the hurdle is a REVIEW trigger;
    INFORMATIONAL - the central estimate alone decides economic eligibility and stress results are
    recorded as downside information. Cross-route disagreement, missing mandatory sensitivity and
    data refusals trigger review under BOTH policies."""
    _fi_mark("horizon_value", "case")
    as_of = _date(as_of)
    sc = rec.get("scored") or {}
    field = sc.get("er_multiple_field")
    rr = _num(sc.get("er_rerate"))
    r = None if rr is None else rr / 100.0
    h = _hurdle(hurdle_pct)
    out = {"ticker": rec.get("ticker"), "method_id": method_id(), "method_version": HV_METHOD_VERSION,
           "as_of": str(as_of), "hurdle_pct": h, "declared_field": field, "stress_policy": stress_policy,
           "pit_fingerprint": rec.get("fingerprint"), "captured_at": rec.get("captured_at"),
           "additive_er_pct": _num(sc.get("expected_return_12_24m")),
           "additive_method_id": sc.get("er_method_id"), "triggers": [], "stress_flags": []}
    try:
        if stress_policy not in STRESS_POLICIES:
            raise ValueError("unknown stress_policy %r" % stress_policy)
        if max_age_days is None:
            import scoring_config as _cfg
            max_age_days = int(getattr(_cfg, "C1_SNAPSHOT_MAX_AGE_DAYS", 1))
        cap = _date(rec.get("captured_at"))
        if event_state and event_state.get("material"):
            out.update(state=STALE, why="material event supersedes the case: %s" % event_state.get("ref"))
            return _finish(out)
        if cap is None or (as_of - cap).days > max_age_days or (as_of - cap).days < 0:
            out.update(state=STALE, why="PIT capture %s is not fresh for as_of %s" % (cap, as_of))
            return _finish(out)
        if field in PFCF_FIELDS:
            out.update(state=NDQ, route="PFCF",
                       why="P/FCF route has no authorised horizon-value method - bounded refusal, "
                           "never the retired additive logic (§7.4)")
            return _finish(out)
        if field in PE_FIELDS:
            auth, cross = pe_route(rec, r, as_of, fx=fx), ev_route(rec, r, as_of, hurdle_pct=h, fx=fx)
        elif field in EV_FIELDS:
            auth, cross = ev_route(rec, r, as_of, hurdle_pct=h, fx=fx), pe_route(rec, r, as_of, fx=fx)
        else:
            out.update(state=MISSING, why="no declared valuation route on the stamped row (%r)" % field)
            return _finish(out)
        out.update(route=auth["route"], authoritative=auth, cross_check=cross)
        out["route_record"] = route_record(field, auth, cross, h)
        if auth["state"] != VALID:
            out.update(state=auth["state"], why=auth.get("why"))
            return _finish(out)
        er_local = auth["er_pct"]
        er = gbp_return(er_local, 0.0)               # central: spot-flat FX (R_FX = 0)
        cb = auth.get("currency") or auth["inputs"].get("currency") or {}
        out.update(er_pct=er, er_local_pct=er_local, passes=er >= h,
                   currency={k: cb.get(k) for k in ("quote", "quote_major", "unit_div", "reporting",
                                                    "rate_quote_to_rep", "fx_source")},
                   fx_basis="SPOT_FLAT central; 1 + R_GBP = (1 + R_local)(1 + R_FX->GBP)")
        qmaj = cb.get("quote_major")
        out["fx_sensitivity"] = fx_sensitivity(er_local, h, qmaj, fx_vol)
        trig = out["triggers"]
        for s_ in auth.get("sensitivities") or []:
            if s_["flips_hurdle"]:
                tag = "SENSITIVITY_FLIP:%s(%s)" % (s_["name"], s_["er_pct"])
                (out["stress_flags"] if stress_policy == "INFORMATIONAL" else trig).append(tag)
        for m in auth.get("mandatory_sensitivity_missing") or []:
            trig.append("MANDATORY_SENSITIVITY_MISSING:%s" % m)
        if cross.get("state") == VALID and (cross["er_pct"] >= h) != (er_local >= h):
            trig.append("CROSS_ROUTE_DISAGREEMENT:%s %s vs %s %s"
                        % (auth["route"], er_local, cross["route"], cross["er_pct"]))
        if auth.get("dividend_lower_bound") and er < h:
            trig.append("DIVIDEND_ABSENT_LOWER_BOUND_FAILS")
        out["state"] = REVIEW if trig else VALID
        return _finish(out)
    except Exception as exc:                                            # noqa: BLE001
        out.update(state=CONFLICTED, why="case raised %s: %s" % (type(exc).__name__, exc))
        return _finish(out)


def _finish(out):
    out["case_id"] = "HV-" + hashlib.sha256(json.dumps(
        {k: out.get(k) for k in ("ticker", "method_id", "pit_fingerprint", "as_of", "hurdle_pct",
                                 "declared_field", "stress_policy")}, sort_keys=True, default=str).encode()).hexdigest()[:16]
    out["capital_authority"] = False                # SHADOW (§16.4)
    if isinstance(out.get("route_record"), dict):
        out["route_record"]["review_state"] = out.get("state")
        out["route_record"]["case_id"] = out["case_id"]
    return out


def route_record(field, auth, cross, h):
    """ISA-0744 prospective route evidence (persisted on every case, SHADOW). The DECLARED route
    decides; the other is a cross-check. Never switched to whichever passes, never averaged, no %
    disagreement threshold - a hurdle-SIGN disagreement is what triggers REVIEW. The continuous
    magnitude is recorded so the realised outcome can later be linked to it (case_id)."""
    a_ok, c_ok = auth.get("state") == VALID, cross.get("state") == VALID
    a_er, c_er = (auth.get("er_pct") if a_ok else None), (cross.get("er_pct") if c_ok else None)
    return {"declared_field": field, "declared_route": auth.get("route"),
            "rationale": "the er_multiple_field stamped on the scoring row declares the route; not optimised",
            "declared_admissibility": auth.get("state"), "declared_why": None if a_ok else auth.get("why"),
            "cross_route": cross.get("route"), "cross_admissibility": cross.get("state"),
            "cross_why": None if c_ok else cross.get("why"),
            "base_er_local_pct": a_er, "cross_er_local_pct": c_er,
            "base_passes": None if a_er is None else a_er >= h,
            "cross_passes": None if c_er is None else c_er >= h,
            "route_disagreement_pp": None if (a_er is None or c_er is None) else round(c_er - a_er, 2),
            "hurdle_sign_disagreement": None if (a_er is None or c_er is None) else ((a_er >= h) != (c_er >= h)),
            "review_state": None, "realised_outcome": None,
            "outcome_link": "case_id - filled by outcome capture at horizon; never back-filled from later data"}


def fx_sensitivity(er_local, h, qmaj, fx_vol):
    """ISA-0197: one-sigma FX sensitivities from ECB realised volatility, 1y and 3y SEPARATELY
    (never blended). favourable = exp(+s) - 1, adverse = exp(-s) - 1, applied multiplicatively:
    1 + R_GBP = (1 + R_local)(1 + R_FX). INFORMATIONAL - no probability, no hurdle, no forecast."""
    raw = 0.0 if qmaj == BASE_CCY else (fx_vol or {}).get(qmaj)
    if not isinstance(raw, dict):
        raw = {"1y": raw, "3y": raw} if isinstance(raw, (int, float)) and qmaj == BASE_CCY else {"1y": raw, "3y": None}
    out = {"basis": "ECB realised weekly-log-return sigma (ddof=1 x sqrt 52); favourable exp(+s)-1, adverse "
                    "exp(-s)-1; multiplicative; 1y and 3y not blended",
           "quote_major": qmaj, "windows": {}, "flips_hurdle_adverse": False,
           "forward_implied": "UNAVAILABLE - no reliable forward points/rate inputs in the PIT capture",
           "authority": "INFORMATIONAL ONLY - never an eligibility rule (Raj 24-Sep-2026)"}
    for w in ("1y", "3y"):
        sig = _num(raw.get(w))
        if sig is None or sig < 0:
            out["windows"][w] = {"sigma_annual": None, "state": "FX_VOL_UNAVAILABLE",
                                 "why": "no ECB %s volatility for %s->GBP" % (w, qmaj)}
            continue
        fav, adv = math.exp(sig) - 1.0, math.exp(-sig) - 1.0
        e_fav, e_adv = gbp_return(er_local, 100.0 * fav), gbp_return(er_local, 100.0 * adv)
        central = gbp_return(er_local, 0.0)
        out["windows"][w] = {"sigma_annual": sig, "favourable_fx_return_pct": round(100.0 * fav, 3),
                             "adverse_fx_return_pct": round(100.0 * adv, 3),
                             "favourable_er_pct": e_fav, "adverse_er_pct": e_adv,
                             "flips_hurdle_adverse": (e_adv >= h) != (central >= h),
                             "flips_hurdle_favourable": (e_fav >= h) != (central >= h)}
        out["flips_hurdle_adverse"] = out["flips_hurdle_adverse"] or out["windows"][w]["flips_hurdle_adverse"]
    return out


def fx_currencies(records):
    """Census of the ISO currencies the month's records need: every quote major and reporting
    currency (GBp -> GBP first). The ECB producer is asked for exactly these."""
    out = set()
    for rec in records:
        info = rec.get("info") or {}
        pc = info.get("currency")
        if not pc:
            continue
        qmaj, _ = SUBUNIT_MAJOR.get(pc, (pc, 1.0))
        out.add(qmaj)
        out.add(info.get("financialCurrency") or qmaj)
        for t in ("eps_estimate", "revenue_estimate"):
            for k in ("0y", "+1y"):
                ec = ((rec.get(t) or {}).get(k) or {}).get("currency")
                if ec:
                    out.add(ec)
    return sorted(out | {BASE_CCY})


def fx_from_ecb(art, records, as_of, *, source=None):
    """The ONLY FX input to a horizon-value case: the month's ECB PIT artefact (ecb_fx.produce).
    PIT: the artefact must not post-date as_of and its observation must be <= as_of and not stale.
    Anything else -> no rates -> every cross-currency case is CONFLICTED / FX_UNAVAILABLE. Never 1.0,
    never yfinance, never another provider (the yfinance FX path was retired in HV-1.2)."""
    import ecb_fx as _ef
    a = _date(as_of)
    prov = {"provider": "ECB", "source": source, "state": (art or {}).get("state", "ABSENT"),
            "fingerprint": (art or {}).get("fingerprint"), "acquisition_route": (art or {}).get("acquisition_route"),
            "method_version": (art or {}).get("method_version"), "requested_as_of": (art or {}).get("requested_as_of"),
            "fx_observation_date": ((art or {}).get("asof") or {}).get("fx_observation_date"),
            "lag_days": ((art or {}).get("asof") or {}).get("lag_days"), "case_as_of": str(a)}
    if not art or art.get("state") != "OK":
        prov["why"] = "ECB FX artefact %s - FX_UNAVAILABLE for cross-currency names" % prov["state"]
        return {"fx": {}, "fx_vol": {}, "provenance": prov}
    obs_d, req_d = _date(prov["fx_observation_date"]), _date(prov["requested_as_of"])
    if req_d is None or obs_d is None or req_d > a or obs_d > a:
        prov.update(state="PIT_VIOLATION", why="FX artefact as_of %s / observation %s post-dates the case as_of %s"
                                                % (req_d, obs_d, a))
        return {"fx": {}, "fx_vol": {}, "provenance": prov}
    if (a - obs_d).days > _ef.MAX_ASOF_LAG_DAYS:
        prov.update(state="FX_STALE", why="ECB observation %s is %d days before %s" % (obs_d, (a - obs_d).days, a))
        return {"fx": {}, "fx_vol": {}, "provenance": prov}
    pairs, quotes = set(), set()
    for rec in records:
        info = rec.get("info") or {}
        pc = info.get("currency")
        if not pc:
            continue
        qmaj, _ = SUBUNIT_MAJOR.get(pc, (pc, 1.0))
        rep_ = info.get("financialCurrency") or qmaj
        if rep_ != qmaj:
            pairs.add(qmaj + rep_)
        for t in ("eps_estimate", "revenue_estimate"):
            for k in ("0y", "+1y"):
                ec = ((rec.get(t) or {}).get(k) or {}).get("currency")
                if ec and ec != rep_:
                    pairs.add(ec + rep_)
        if qmaj != BASE_CCY:
            quotes.add(qmaj)
    fx = _ef.pair_rates(art, sorted(pairs))
    vol = {q: _ef.vol_for(art, q) for q in sorted(quotes)}
    prov.update(pairs_needed=sorted(pairs), pairs_missing=sorted(pairs - set(fx)),
                vol_missing=sorted(q for q, v in vol.items() if v.get("1y") is None or v.get("3y") is None),
                unsupported=sorted(c for c, r in (art.get("currencies") or {}).items()
                                   if r.get("state") == getattr(_ef, "S_UNSUPPORTED", "UNSUPPORTED_CURRENCY")))
    return {"fx": fx, "fx_vol": vol, "provenance": prov}


def compare(cases):
    """SHADOW comparison against the repaired additive E[r] stamped on the same PIT record."""
    by = {}
    for c in cases:
        by[c["state"]] = by.get(c["state"], 0) + 1
    both = [c for c in cases if c.get("er_pct") is not None and c.get("additive_er_pct") is not None]
    d = sorted(c["er_pct"] - c["additive_er_pct"] for c in both)
    q = (lambda p: round(d[int(p * (len(d) - 1))], 1)) if d else (lambda p: None)
    flips = [{"ticker": c["ticker"], "additive": c["additive_er_pct"], "hv": c["er_pct"], "state": c["state"]}
             for c in both if (c["additive_er_pct"] >= c["hurdle_pct"]) != (c["er_pct"] >= c["hurdle_pct"])]
    trig = {}
    for c in cases:
        for t in c.get("triggers") or []:
            k = t.split(":")[0]
            trig[k] = trig.get(k, 0) + 1
    add_pass = [c for c in cases if (c.get("additive_er_pct") or -1e9) >= c["hurdle_pct"]]
    hv_ok = [c for c in add_pass if c["state"] == VALID and c.get("passes")]
    return {"n": len(cases), "by_state": by, "n_both_quantified": len(both),
            "delta_pp": {"p10": q(.1), "p25": q(.25), "median": q(.5), "p75": q(.75), "p90": q(.9)},
            "hurdle_flips": flips, "review_triggers": trig,
            "additive_passes": len(add_pass), "additive_passes_still_valid_pass_under_hv": len(hv_ok),
            "comparator": "repaired additive E[r] (post ISA-0720/0721/0745) stamped on the same PIT record",
            "label": "METHOD_CHANGED - not a like-for-like delta of one method (§7.3)"}


def attach_provider_ttm(records, *, budget_s=90.0, fetch=None):
    """ISA-0747 — acquire the provider's DIRECT TTM income statement for records that lack it, AFTER
    every capital-relevant fetch has finished (Step 8h), so a provider throttle here can never degrade
    the scoring fetch. Fail-soft, time-bounded; each attached table carries its own fetch time."""
    import time as _t
    t0, n, errs = _t.time(), 0, {}
    for rec in records:
        if rec.get("income_stmt_ttm") or rec.get("state") not in (None, "CAPTURED"):
            continue
        if _t.time() - t0 > budget_s:
            errs["_budget"] = "stopped after %.0fs - remaining names keep MISSING/other sources" % budget_s
            break
        try:
            if fetch is not None:
                df = fetch(rec["ticker"])
            else:
                import yfinance as yf
                df = yf.Ticker(rec["ticker"]).ttm_income_stmt
            import pit_capture as _pc
            tbl = _pc._stmt(df, _pc.INC_ROWS, 1)
            if tbl:
                rec["income_stmt_ttm"] = tbl
                rec["income_stmt_ttm_fetched_at"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
                n += 1
        except Exception as exc:                                        # noqa: BLE001
            errs[rec.get("ticker")] = "%s: %s" % (type(exc).__name__, str(exc)[:80])
    return {"attached": n, "errors": dict(list(errs.items())[:20]), "secs": round(_t.time() - t0, 1)}


def shadow_month(month_label, *, root=None, as_of=None, hurdle_pct=None, records=None, write=True,
                 fx_bundle=None, stress_policy="GATE", fetch_ttm=False):
    """Pre-run Step 8h. Reads pit_capture_[month].jsonl (the ISA-0722 golden record), builds one
    case per captured name, writes horizon_value_[month].json. SHADOW. Never raises."""
    _fi_mark("horizon_value", "shadow_month")
    root = root or HERE
    try:
        import isa_policy as _pol
        if not _pol.flag("horizon_value_shadow"):
            return {"state": "DISABLED", "why": "isa_policy flag horizon_value_shadow is False (R4.13)"}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FAILED", "why": "flag unreadable (%s) - UNKNOWN, never run by default" % exc}
    try:
        if records is None:
            p = os.path.join(root, "pit_capture_%s.jsonl" % month_label)
            if not os.path.exists(p):
                return {"state": "NO_INPUT", "why": "%s absent - no PIT vintage this month" % os.path.basename(p)}
            latest = {}
            for line in open(p, encoding="utf-8"):
                try:
                    rr = json.loads(line)
                except ValueError:
                    continue
                if rr.get("state") == "CAPTURED":
                    latest[rr["ticker"]] = rr               # last vintage wins (append-only file)
            records = list(latest.values())
        as_of = as_of or datetime.date.today().isoformat()
        ttm_note = attach_provider_ttm(records) if fetch_ttm else {"attached": 0, "skipped": True}
        # ⚑ OFFLINE BY DEFAULT: Step 8h runs inside the orchestrator. FX comes ONLY from the ECB PIT
        #   artefact fx_pit_[month].json written by Step 6f (ecb_fx.produce). Absent / failed / stale /
        #   post-dated -> every cross-currency case is typed FX_UNAVAILABLE - never 1.0, never yfinance.
        if fx_bundle is None:
            _fp = os.path.join(root, "fx_pit_%s.json" % month_label)
            try:
                _art = json.load(open(_fp, encoding="utf-8")) if os.path.exists(_fp) else None
            except ValueError:
                _art = {"state": "UNREADABLE"}
            fxb = fx_from_ecb(_art, records, as_of, source=os.path.basename(_fp))
        else:
            fxb = fx_bundle
        cases = [case(r, as_of=as_of, hurdle_pct=hurdle_pct, fx=fxb.get("fx"), fx_vol=fxb.get("fx_vol"),
                      stress_policy=stress_policy) for r in records]
        cmp_ = compare(cases)
        doc = {"_meta": {"month": month_label, "as_of": as_of, "method_id": method_id(),
                         "method_version": HV_METHOD_VERSION, "mode": "SHADOW", "capital_authority": False,
                         "fx_inputs": fxb, "stress_policy": stress_policy, "provider_ttm": ttm_note,
                         "produced_at": datetime.datetime.now().isoformat(timespec="seconds")},
               "comparison": cmp_, "cases": cases}
        if write:
            with open(os.path.join(root, "horizon_value_%s.json" % month_label), "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1, default=str)
        return {"state": "OK", "mode": "SHADOW", "n_cases": len(cases), "by_state": cmp_["by_state"],
                "hurdle_flips": len(cmp_["hurdle_flips"]), "delta_pp": cmp_["delta_pp"],
                "review_triggers": cmp_["review_triggers"], "method_id": doc["_meta"]["method_id"]}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": "FAILED", "why": "%s: %s" % (type(exc).__name__, exc)}


# ─────────────────────────────────────────────────────────── selftest ─────────────
def _fx(**kw):
    """A fully specified synthetic PIT record (P0 100, EPS 5 -> 6, revenue 1000 -> 1200, 20% TTM
    margin, net debt 100, 10 diluted shares, dividend 2.0, FY0 ends 31-Dec-2026)."""
    ep = lambda d: (datetime.datetime.fromisoformat(d) - datetime.datetime(1970, 1, 1)).total_seconds()
    q = {d: {"EBITDA": 50.0, "Total Revenue": 250.0, "Diluted Average Shares": 10.0}
         for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30")}
    a = {d: {"EBITDA": 200.0, "Total Revenue": 1000.0, "Diluted Average Shares": 10.0}
         for d in ("2025-12-31", "2024-12-31", "2023-12-31")}
    r = {"ticker": "FX", "captured_at": "2026-09-24T08:00:00", "fingerprint": "fp",
         "info": {"currency": "USD", "financialCurrency": "USD", "currentPrice": 100.0,
                  "nextFiscalYearEnd": ep("2026-12-31"), "totalDebt": 200.0, "totalCash": 100.0,
                  "forwardPE": 100.0 / 6.0},
         "eps_estimate": {"0y": {"avg": 5.0}, "+1y": {"avg": 6.0}},
         "revenue_estimate": {"0y": {"avg": 1000.0}, "+1y": {"avg": 1200.0}},
         "income_stmt_quarterly": q, "income_stmt_annual": a,
         "balance_sheet_annual": {"2025-12-31": {"Net Debt": 100.0}, "2024-12-31": {"Net Debt": 100.0}},
         "dividend": {"state": "PRESENT", "value": 2.0},
         "scored": {"er_multiple_field": "fwd_pe", "er_rerate": 0.0, "expected_return_12_24m": 22.0,
                    "er_method_id": "expected_return@x"}}
    for k, v in kw.items():
        r[k] = v
    return r


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:200]) if not cond else ""))
        if not cond:
            fails.append(name)
    A, H = "2026-09-24", 15.7
    C = lambda rec, **k: case(rec, as_of=A, hurdle_pct=H, **k)
    base = C(_fx())
    ok("F1 POSITIVE: clean P/E case is VALID_MECHANICAL at 22.0% (6 x 20 + 2 dividend)",
       base["state"] == VALID and base["er_pct"] == 22.0 and base["passes"] and base["route"] == "PE", base)
    ok("F1 every case is SHADOW: capital_authority False, typed case_id", base["capital_authority"] is False
       and base["case_id"].startswith("HV-") and C(_fx())["case_id"] == base["case_id"])
    ok("F3 MUST-FIRE: FY1 EPS used ONCE - never grown again into FY2 (would be 46.0)",
       base["er_pct"] != 46.0 and base["authoritative"]["P_T"] == 120.0)
    info_roll = dict(_fx()["info"], nextFiscalYearEnd=(datetime.datetime(2026, 8, 31) - datetime.datetime(1970, 1, 1)).total_seconds())
    ok("F2 MUST-FIRE: provider FY0 already ended (fiscal rollover) -> CONFLICTED, not silently re-labelled",
       C(_fx(info=info_roll))["state"] == CONFLICTED)
    zero = C(_fx(dividend={"state": "ZERO", "value": 0.0}))
    absent = C(_fx(dividend={"state": "ABSENT", "value": None}))
    ok("F4 ZERO dividend is a measured 0 (20.0%, VALID); ABSENT is an ex-dividend LOWER BOUND, never recorded as 0",
       zero["er_pct"] == 20.0 and zero["state"] == VALID and absent["er_pct"] == 20.0
       and absent["authoritative"]["dividend_lower_bound"] is True and zero["authoritative"]["dividend_lower_bound"] is False)
    low = dict(_fx()["eps_estimate"], **{"+1y": {"avg": 5.5}})
    rv = {"0y": {"avg": 1000.0}, "+1y": {"avg": 1100.0}}          # both routes fail ex-dividend
    ok("F4 MUST-FIRE: an ABSENT-dividend lower bound that FAILS the hurdle -> REVIEW_REQUIRED (unknown, not zero)",
       C(_fx(eps_estimate=low, revenue_estimate=rv, dividend={"state": "ABSENT"}))["state"] == REVIEW
       and C(_fx(eps_estimate=low, revenue_estimate=rv, dividend={"state": "ZERO", "value": 0.0}))["state"] == VALID)
    ers = {sc: C(_fx(scored=dict(_fx()["scored"], share_count_change=sc)))["er_pct"] for sc in (-0.03, 0.0, 0.04)}
    ok("F5 MUST-FIRE (ISA-0745): buyback / neutral / issuance give the SAME P/E E[r] - no share-count yield",
       len(set(ers.values())) == 1, ers)
    evd = dict(_fx()["scored"], er_multiple_field="ev_ebitda")
    ev = C(_fx(scored=evd))
    ok("F6 POSITIVE: clean EV/EBITDA case (same-definition TTM margin and M0) is VALID at 18.09% (w0 = 98/365)",
       ev["state"] == VALID and ev["route"] == "EV" and abs(ev["er_pct"] - 18.09) < 0.02, ev.get("er_pct"))
    hist = dict(_fx()["income_stmt_annual"])
    hist["2023-12-31"] = dict(hist["2023-12-31"], **{"Diluted Average Shares": 5.0})
    ev_sh = C(_fx(scored=evd, income_stmt_annual=hist))
    ok("F5 (EV) share count enters ONCE via the terminal denominator: share HISTORY moves only a sensitivity",
       ev_sh["er_pct"] == ev["er_pct"])
    ok("F10 MUST-FIRE: a dilution sensitivity that flips the hurdle -> REVIEW_REQUIRED",
       ev_sh["state"] == REVIEW and any("diluted_shares" in t for t in ev_sh["triggers"]), ev_sh["triggers"])
    ok("F7 MUST-FIRE: missing quarterly TTM EBITDA -> MISSING_REQUIRED_INPUT (no annual fallback)",
       C(_fx(scored=evd, income_stmt_quarterly={}))["state"] == MISSING)
    ok("F17 MUST-FIRE: a zero revenue estimate is a broken source field -> MISSING (not a valuation verdict)",
       C(_fx(scored=evd, revenue_estimate={"0y": {"avg": 0.0}, "+1y": {"avg": 0.0}}))["state"] == MISSING)
    ok("F7 MUST-FIRE: missing FY1 revenue estimate (no NTM revenue) -> MISSING_REQUIRED_INPUT",
       C(_fx(scored=evd, revenue_estimate={"0y": {"avg": 1000.0}}))["state"] == MISSING)
    lowm = {d: {"EBITDA": m * 1000.0, "Total Revenue": 1000.0, "Diluted Average Shares": 10.0}
            for d, m in (("2025-12-31", 0.12), ("2024-12-31", 0.13), ("2023-12-31", 0.14))}
    m8 = C(_fx(scored=evd, income_stmt_annual=lowm))
    ok("F8 MUST-FIRE: a margin sensitivity (FY low / median / lambda) that flips the hurdle -> REVIEW_REQUIRED",
       m8["state"] == REVIEW and any("margin" in t for t in m8["triggers"]) and m8["er_pct"] == ev["er_pct"], m8["triggers"])
    ok("F8 NEGATIVE CONTROL: lambda=0.38 is a sensitivity only - the base E[r] is the TTM-margin value",
       m8["er_pct"] == ev["er_pct"] and ev["state"] == VALID)
    two = {"2025-12-31": _fx()["income_stmt_annual"]["2025-12-31"], "2024-12-31": _fx()["income_stmt_annual"]["2024-12-31"]}
    ok("F8 MUST-FIRE: fewer than 3 completed FY margins (mandatory sensitivity missing) -> REVIEW_REQUIRED",
       C(_fx(scored=evd, income_stmt_annual=two))["state"] == REVIEW)
    nd9 = C(_fx(scored=evd, balance_sheet_annual={"2025-12-31": {"Net Debt": 100.0}, "2024-12-31": {"Net Debt": -400.0}}))
    ok("F9 MUST-FIRE: a net-debt sensitivity that flips the hurdle -> REVIEW_REQUIRED; base net debt frozen",
       nd9["state"] == REVIEW and any("net_debt" in t for t in nd9["triggers"]) and nd9["er_pct"] == ev["er_pct"], nd9["triggers"])
    dis = C(_fx(revenue_estimate={"0y": {"avg": 1000.0}, "+1y": {"avg": 1100.0}}))
    ok("F11 MUST-FIRE: P/E passes and EV fails -> REVIEW_REQUIRED (never averaged, no 10% threshold)",
       dis["state"] == REVIEW and any("CROSS_ROUTE" in t for t in dis["triggers"]) and dis["er_pct"] == 22.0, dis["triggers"])
    ok("F11 NEGATIVE CONTROL: routes that agree on pass/fail do not trigger review", not base["triggers"])
    ok("F12 MUST-FIRE: a material event supersedes the case -> STALE",
       C(_fx(), event_state={"material": True, "ref": "profit warning"})["state"] == STALE
       and C(_fx(), event_state={"material": False})["state"] == VALID)
    ok("F12 MUST-FIRE: a PIT capture older than the run freshness contract -> STALE",
       C(_fx(captured_at="2026-09-20T08:00:00"))["state"] == STALE)
    pf = C(_fx(scored=dict(_fx()["scored"], er_multiple_field="price_fcf")))
    ok("F16 MUST-FIRE: P/FCF keeps its bounded refusal - NOT_DEFENSIBLY_QUANTIFIABLE, no E[r], no additive fallback",
       pf["state"] == NDQ and pf.get("er_pct") is None and pf["route"] == "PFCF")
    bad = dict(_fx()["eps_estimate"], **{"0y": {"avg": "abc"}})
    ok("F17 MUST-FIRE: a broken source field (non-numeric EPS) -> MISSING_REQUIRED_INPUT at the boundary",
       C(_fx(eps_estimate=bad))["state"] == MISSING)
    ok("F17 MUST-FIRE: negative EPS -> P/E route NOT_DEFENSIBLY_QUANTIFIABLE (no fallback quantity)",
       C(_fx(eps_estimate={"0y": {"avg": -1.0}, "+1y": {"avg": 2.0}}))["state"] == NDQ)
    ok("F17 MUST-FIRE: price/financial currency mismatch -> CONFLICTED (no FX forecast)",
       C(_fx(info=dict(_fx()["info"], currency="USD", financialCurrency="EUR")))["state"] == CONFLICTED)
    gbp = dict(_fx()["info"], currency="GBp", financialCurrency="GBP", currentPrice=10000.0)
    g = C(_fx(info=gbp, dividend={"state": "PRESENT", "value": 2.0, "yield_raw": 2.0}))
    ok("F17 POSITIVE: GBp/GBP is a deterministic unit (/100), and the dividend unit is resolved "
       "against the provider yield (2.0 GBP on 100.00 GBP = 2%) -> 22.0%, VALID",
       g["state"] == VALID and g["er_pct"] == 22.0 and g["currency"]["unit_div"] == 100.0, (g["state"], g.get("er_pct")))
    gu = C(_fx(info=gbp, dividend={"state": "PRESENT", "value": 2.0}))
    ok("F17 NEGATIVE CONTROL: a sub-unit dividend with no yield to resolve its unit is a LOWER BOUND, never guessed",
       gu["er_pct"] == 20.0 and gu["authoritative"]["dividend_state"] == "UNIT_UNDECIDABLE")
    eur = dict(_fx()["info"], currency="USD", financialCurrency="EUR")
    ok("ISA-0197 MUST-FIRE: USD-quoted / EUR-reporting WITHOUT an as-of spot -> CONFLICTED FX_UNAVAILABLE (never 1.0)",
       C(_fx(info=eur))["state"] == CONFLICTED and "FX_UNAVAILABLE" in (C(_fx(info=eur)).get("why") or ""))
    k = 0.9                                       # EUR per USD
    def _scaled(rate_key, rate):
        r = _fx(info=dict(eur, totalDebt=200.0 * k, totalCash=100.0 * k),
                eps_estimate={"0y": {"avg": 5.0 * k}, "+1y": {"avg": 6.0 * k}},
                revenue_estimate={"0y": {"avg": 1000.0 * k}, "+1y": {"avg": 1200.0 * k}},
                income_stmt_quarterly={d: {kk: (v * k if kk != "Diluted Average Shares" else v) for kk, v in c.items()}
                                       for d, c in _fx()["income_stmt_quarterly"].items()},
                scored=evd)
        return case(r, as_of=A, hurdle_pct=H, fx={rate_key: rate})
    xr = _scaled("USDEUR", k)
    ok("ISA-0197 POSITIVE: a quote-vs-reporting currency difference is NOT a conflict - EV valued in EUR from a "
       "USD quote reproduces the same-currency case (18.09%)", xr["state"] == VALID and abs(xr["er_pct"] - ev["er_pct"]) < 0.02,
       (xr["state"], xr.get("er_pct")))
    ok("ISA-0197 MUST-FIRE: the rate DIRECTION matters - the inverted rate gives a different EV result",
       abs(_scaled("USDEUR", 1.0 / k)["er_pct"] - 17.82) < 0.02)    # net debt re-weighted vs market value
    ok("ISA-0197: 1 + R_GBP = (1 + R_local)(1 + R_FX) - multiplicative (10% local, +5% FX = 15.5%, not 15%)",
       gbp_return(10.0, 5.0) == 15.5)
    fv = C(_fx(), fx_vol={"USD": {"1y": 0.5, "3y": 0.05}})
    w1, w3 = fv["fx_sensitivity"]["windows"]["1y"], fv["fx_sensitivity"]["windows"]["3y"]
    ok("ISA-0197: the FX sensitivity is computed and INFORMATIONAL - even a hurdle-flipping 1-sigma FX move "
       "never changes the state", fv["state"] == VALID and fv["fx_sensitivity"]["flips_hurdle_adverse"] is True
       and fv["er_pct"] == 22.0)
    ok("ISA-0197 MUST-FIRE: sensitivities are exp(+-s)-1 applied multiplicatively, 1y and 3y SEPARATE",
       w1["adverse_er_pct"] == gbp_return(22.0, 100.0 * (math.exp(-0.5) - 1.0))
       and w1["favourable_er_pct"] == gbp_return(22.0, 100.0 * (math.exp(0.5) - 1.0))
       and w3["sigma_annual"] == 0.05 and w3["flips_hurdle_adverse"] is False and w1["flips_hurdle_adverse"] is True)
    ok("ISA-0197: a GBP-quoted name has zero FX exposure (sigma 0 in both windows, no flip)",
       all(fx_sensitivity(22.0, H, "GBP", {})["windows"][w]["sigma_annual"] == 0.0 for w in ("1y", "3y")))
    ok("ISA-0197 MUST-FIRE: a missing ECB volatility is typed FX_VOL_UNAVAILABLE per window - never zero",
       fx_sensitivity(22.0, H, "USD", {})["windows"]["3y"]["state"] == "FX_VOL_UNAVAILABLE")
    # ── ECB bundle (the only FX authority) ─────────────────────────────────────────────────
    _art = {"state": "OK", "requested_as_of": "2026-09-24", "fingerprint": "f", "acquisition_route": "ECB_SDMX_REST_API",
            "asof": {"fx_observation_date": "2026-09-24", "lag_days": 0},
            "raw_eur_rates": {"USD": {"2026-09-24": 1.2}, "GBP": {"2026-09-24": 0.84}},
            "currencies": {"USD": {"state": "OK", "vol_1y": {"state": "OK", "sigma_ann": 0.06},
                                   "vol_3y": {"state": "OK", "sigma_ann": 0.07}},
                           "EUR": {"state": "OK", "vol_1y": {"state": "OK", "sigma_ann": 0.03},
                                   "vol_3y": {"state": "OK", "sigma_ann": 0.04}}}}
    eur_rec = _fx(info=dict(_fx()["info"], currency="USD", financialCurrency="EUR"))
    bx = fx_from_ecb(_art, [eur_rec], A)
    ok("ECB bundle: USD-quoted/EUR-reporting gets USDEUR = R_EUR/R_USD = 1/1.2 and USD 1y/3y vols",
       abs(bx["fx"]["USDEUR"] - 1.0 / 1.2) < 1e-12 and bx["fx_vol"]["USD"] == {"1y": 0.06, "3y": 0.07}
       and bx["provenance"]["pairs_missing"] == [], bx)
    ok("ECB bundle MUST-FIRE: an artefact dated AFTER the case as_of is a PIT_VIOLATION -> no rates",
       fx_from_ecb(dict(_art, requested_as_of="2026-09-25"), [eur_rec], A)["provenance"]["state"] == "PIT_VIOLATION")
    ok("ECB bundle MUST-FIRE: a stale ECB observation -> FX_STALE -> no rates -> case CONFLICTED FX_UNAVAILABLE",
       fx_from_ecb(dict(_art, asof={"fx_observation_date": "2026-09-01", "lag_days": 23}), [eur_rec], A)["fx"] == {}
       and C(eur_rec, fx={})["state"] == CONFLICTED)
    ok("ECB bundle MUST-FIRE: absent artefact -> FX_UNAVAILABLE (never 1.0, never another provider)",
       fx_from_ecb(None, [eur_rec], A)["fx"] == {} and "FX_UNAVAILABLE" in fx_from_ecb(None, [eur_rec], A)["provenance"]["why"])
    ok("ISA-0197: the yfinance FX path is retired (no fx_inputs / _yf_close_history in HV-1.2)",
       "fx_inputs" not in globals() and "_yf_close_history" not in globals())
    ok("census: GBp normalised to GBP; quote and reporting currencies both requested",
       fx_currencies([eur_rec, _fx(info=dict(_fx()["info"], currency="GBp", financialCurrency="GBP"))]) == ["EUR", "GBP", "USD"])
    # ── estimate currency basis (GMAB) ─────────────────────────────────────────────────────
    dkk = {"0y": {"avg": 1000.0 / 0.15, "currency": "DKK"}, "+1y": {"avg": 1200.0 / 0.15, "currency": "DKK"}}
    gm = _fx(scored=evd, revenue_estimate=dkk)
    ok("ISA-0197 MUST-FIRE (GMAB): DKK revenue consensus vs USD reporting WITHOUT a rate -> EV CONFLICTED "
       "ESTIMATE_CURRENCY_MISMATCH (never used raw)", C(gm)["state"] == CONFLICTED
       and "ESTIMATE_CURRENCY_MISMATCH" in (C(gm).get("why") or ""), C(gm).get("why"))
    gmv = C(gm, fx={"DKKUSD": 0.15})
    ok("ISA-0197 (GMAB): with the ECB DKKUSD spot the DKK consensus converts to reporting - identical to the "
       "all-USD case", gmv["state"] == ev["state"] and gmv["er_pct"] == ev["er_pct"], (gmv.get("er_pct"), ev.get("er_pct")))
    mix = {"0y": {"avg": 1000.0, "currency": "USD"}, "+1y": {"avg": 1200.0, "currency": "DKK"}}
    ok("ISA-0197 MUST-FIRE: FY0/FY1 consensus in different currencies -> CONFLICTED",
       C(_fx(scored=evd, revenue_estimate=mix), fx={"DKKUSD": 0.15})["state"] == CONFLICTED)
    ok("ISA-0197: consensus stated in the reporting currency is untouched (factor 1, SAME_AS_REPORTING)",
       estimate_basis(_fx(eps_estimate={"0y": {"avg": 5.0, "currency": "USD"}, "+1y": {"avg": 6.0, "currency": "USD"}}),
                      "eps_estimate")["basis"] == "SAME_AS_REPORTING")
    ok("census/bundle: estimate currencies are requested and their EST->REP pair is priced",
       "DKK" in fx_currencies([gm]) and "DKKUSD" in fx_from_ecb(dict(_art, raw_eur_rates=dict(_art["raw_eur_rates"],
       DKK={"2026-09-24": 7.46})), [gm], A)["fx"])
    # ── prospective route record (ISA-0744) ───────────────────────────────────────────────
    rr0 = base["route_record"]
    ok("ISA-0744 route record persisted: declared route, admissibility, base/cross E[r], hurdle outcomes, "
       "continuous disagreement, review state, outcome link",
       rr0["declared_route"] == "PE" and rr0["declared_admissibility"] == VALID and rr0["base_er_local_pct"] == 22.0
       and rr0["review_state"] == VALID and rr0["case_id"] == base["case_id"] and rr0["realised_outcome"] is None
       and "route_disagreement_pp" in rr0 and rr0["hurdle_sign_disagreement"] in (True, False, None), rr0)
    # ── ISA-0747 reporting-period normalisation contract ─────────────────────────────────────
    ok("ISA-0747: FY + YTD - prior YTD (Clarkson 631.4 + 413.5 - 297.8 = 747.1)",
       abs(ttm_from_ytd(631.4, 413.5, 297.8) - 747.1) < 1e-9 and ttm_from_ytd(1.0, None, 1.0) is None)
    semi = _fx(scored=evd, income_stmt_quarterly={},
               income_stmt_ttm={"2026-06-30": {"EBITDA": 200.0, "Total Revenue": 1000.0, "Diluted Average Shares": 10.0}})
    sv = C(semi)
    ok("ISA-0747 MUST-FIRE: a half-yearly reporter with NO quarters uses the validated provider TTM -> VALID (was MISSING)",
       sv["state"] == VALID and sv["authoritative"]["inputs"]["ttm"]["source"] == "PROVIDER_TTM", sv.get("why"))
    lag = C(_fx(scored=evd, income_stmt_quarterly={},
                income_stmt_ttm={"2025-06-30": {"EBITDA": 200.0, "Total Revenue": 1000.0}}))
    ok("ISA-0747 MUST-FIRE: a provider TTM that PRECEDES the latest completed FY is STALE / PROVIDER_LAG, "
       "typed separately from cadence", lag["state"] == STALE and "PROVIDER_LAG" in lag.get("why", ""))
    fq = C(_fx(scored=evd, income_stmt_ttm={"2026-03-31": {"EBITDA": 190.0, "Total Revenue": 980.0}}))
    ok("ISA-0747: a quarterly table FRESHER than the provider TTM wins (sum of 4Q) - provider lag recorded",
       fq["authoritative"]["inputs"]["ttm"]["source"] == "SUM_4Q" and "PROVIDER_LAG" in fq["authoritative"]["inputs"]["ttm"]["why"])
    it = _fx(scored=evd, income_stmt_quarterly={},
             income_stmt_interim={"2026-06-30": {"period_months": 6, "EBITDA": 110.0, "Total Revenue": 550.0},
                                  "2025-06-30": {"period_months": 6, "EBITDA": 100.0, "Total Revenue": 500.0}})
    fi = flow_ttm(it)
    ok("ISA-0747: no provider TTM and no quarters -> FY + current H1 - prior H1 on matching period lengths",
       fi["source"] == "FY_PLUS_YTD" and fi["values"] == {"Total Revenue": 1050.0, "EBITDA": 210.0}, fi)
    bad = dict(it["income_stmt_interim"], **{"2025-06-30": {"period_months": 3, "EBITDA": 50.0, "Total Revenue": 250.0}})
    ok("ISA-0747 MUST-FIRE: interim periods of different length never combine -> MISSING",
       flow_ttm(_fx(income_stmt_quarterly={}, income_stmt_interim=bad))["state"] == MISSING)
    ok("ISA-0747: diluted shares are a STOCK measure - the latest valid observation, never a TTM formula",
       latest_diluted_shares(_fx(income_stmt_ttm={"2026-06-30": {"Diluted Average Shares": 11.0}}))["value"] == 11.0)
    # ── stress-policy comparison (SHADOW only; production default GATE unchanged) ────────────
    m8i = C(_fx(scored=evd, income_stmt_annual=lowm), stress_policy="INFORMATIONAL")
    ok("STRESS POLICY: under INFORMATIONAL the FY-low flip is recorded as downside information and the "
       "central estimate decides (VALID); under GATE it is REVIEW_REQUIRED",
       m8i["state"] == VALID and any("margin" in f for f in m8i["stress_flags"]) and m8["state"] == REVIEW)
    ok("STRESS POLICY NEGATIVE CONTROL: cross-route disagreement is NOT a stress result - still REVIEW under INFORMATIONAL",
       C(_fx(revenue_estimate={"0y": {"avg": 1000.0}, "+1y": {"avg": 1100.0}}), stress_policy="INFORMATIONAL")["state"] == REVIEW)
    ok("MUST-FIRE: an UNMEASURED approved re-rate -> MISSING_REQUIRED_INPUT (never a zero re-rate)",
       C(_fx(scored=dict(_fx()["scored"], er_rerate=None)))["state"] == MISSING)
    ok("ISA-0744: a hurdle-SIGN route disagreement is recorded with its continuous magnitude and REVIEW",
       dis["state"] == REVIEW and dis["route_record"]["hurdle_sign_disagreement"] is True
       and dis["route_record"]["route_disagreement_pp"] is not None and dis["route_record"]["review_state"] == REVIEW,
       dis.get("route_record"))
    cmp_ = compare([base, dis, ev])
    ok("F18 golden comparison is LABELLED METHOD_CHANGED and names its repaired-additive comparator",
       "METHOD_CHANGED" in cmp_["label"] and "0745" in cmp_["comparator"] and cmp_["n"] == 3)
    try:
        import isa_policy as _pol
        sv = _pol.V2_FLAGS.get("horizon_value_shadow")
        _pol.V2_FLAGS["horizon_value_shadow"] = False
        ok("R4.13: flag False -> DISABLED (UNKNOWN), never a case", shadow_month("x", records=[_fx()], write=False, fetch_ttm=False)["state"] == "DISABLED")
        _pol.V2_FLAGS["horizon_value_shadow"] = True
        ok("NO_INPUT when the month has no PIT vintage", shadow_month("nomonth", root=os.path.dirname(HERE))["state"] == "NO_INPUT")
        r1 = shadow_month("x", records=[_fx()], write=False, as_of=A, hurdle_pct=H, fetch_ttm=False,
                          fx_bundle={"fx": {}, "fx_vol": {}})
        _r = [_fx(income_stmt_quarterly={}, scored=evd)]
        _note = attach_provider_ttm(_r, fetch=lambda t: __import__("pandas").DataFrame(
            [[200.0], [1000.0]], index=["EBITDA", "Total Revenue"], columns=__import__("pandas").to_datetime(["2026-06-30"])))
        ok("ISA-0747: Step 8h attaches the provider TTM to a record that lacks it (fail-soft, bounded)",
           _note["attached"] == 1 and C(_r[0])["state"] == VALID)
        ok("ISA-0747 NEGATIVE CONTROL: a failing provider call attaches nothing and never raises",
           attach_provider_ttm([_fx(income_stmt_quarterly={})], fetch=lambda t: 1 / 0)["attached"] == 0)
        ok("POSITIVE: shadow_month over one record -> OK with one VALID case", r1["state"] == "OK" and r1["by_state"] == {VALID: 1}, r1)
    finally:
        _pol.V2_FLAGS["horizon_value_shadow"] = sv if sv is not None else True
    print("horizon_value selftest: %d FAIL(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(shadow_month(sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime("%b_%Y").lower()), default=str, indent=1))
