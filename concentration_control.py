#!/usr/bin/env python3
"""
concentration_control.py — ISA-0465: direct-stock SECTOR / THEME hard concentration backstops.

Authority: Raj decision 16-Sep-2026; ISA_BuildSpec_ISA-0465_Concentration_Control_16Sep2026.md.
Classification INTERIM_STEP_TO_TARGET. ONE HOME for the rule; position_sizing.allocate consults it
through a hook, capital_destination runs it in SHADOW (V2_FLAGS["concentration_gate"]).

THE POLICY (DECLARED backstops, not calibrated — changed only by explicit recalibration):
  · sector: post-trade direct-stock exposure to one canonical sector <= SECTOR_CAP_NAV x total ISA NAV
  · theme : post-trade exposure to one canonical theme <= THEME_CAP_SLEEVE x post-trade direct-stock sleeve
  · funds / look-through are NOT in either cap (separate whole-portfolio risk architecture)
  · composed INDEPENDENTLY with ISA-0600 (beta, in step9_pre_builder) — passing one never waives another
  · equality passes; greater-than fails for new or increasing exposure
  · missing classification for a positive-capital candidate -> UNKNOWN_CLASSIFICATION -> refuse
  · existing breach: no forced sale; a worsening addition is refused; a risk-reducing trade is allowed
  · residual headroom below the minimum meaningful entry never opens a sub-scale NEW position
  · no score-based override (DIVERSIFY_OVERRIDE_DELTA is retired)

⚑ TAXONOMY HONESTY (R19.3 amendment A1, 16-Sep-2026).
  SECTOR — golden-source contract VERIFIED: one provider, yfinance `Ticker.info["sector"]`, fetched by
  screener_core.fetch_phase1_info and written to screen_history/*_full_data.csv. Yahoo Finance's
  eleven-sector taxonomy — NOT GICS, never labelled GICS. Precedence: (1) newest screener file with a
  non-blank sector for the exact ticker; (2) `sector_captures` in the declared taxonomy file (same
  provider); (3) UNKNOWN. A sector that differs across screener dates is published as SECTOR_CHANGED.
  THEME — ISA-0700 (Raj option A, multi-label): the declared, versioned concentration_theme_taxonomy.json
  is the ONLY theme source. Each stock has zero or more material theme memberships; explicit
  NO_MATERIAL_THEME passes the theme cap; a ticker absent from the file is UNKNOWN and refuses new
  capital. A stock counts in full toward EVERY theme it belongs to. No sector/industry proxy; the VCI
  screener's themes are NOT imported. A held UNKNOWN name is counted WORST CASE in every theme/sector.
  CAP_CONSTRAINED_ENTRY — Raj decision A1.3: a NEW entry limited by concentration headroom is admitted
  only if the placed amount >= the 2.8% NAV minimum meaningful entry; any later fill is conditional on
  fresh headroom and continued eligibility (position_sizing releases blocked capital immediately).

ROLLBACK (R4.13): isa_policy.V2_FLAGS["concentration_gate"] = "OFF".
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import re
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

try:                                                    # pragma: no cover - wiring only
    from framework_integrity import _mark as _fi_mark
except Exception:                                       # noqa: BLE001  pragma: no cover
    def _fi_mark(*_a, **_k):                            # noqa: D103
        return None

SECTOR_CAP_NAV = 0.12          # RAJ 16-Sep-2026 — DECLARED backstop, revalidate_by 2026-12-31
THEME_CAP_SLEEVE = 0.50        # RAJ 16-Sep-2026 — DECLARED backstop, revalidate_by 2026-12-31
SECTOR_TAXONOMY = "YAHOO_FINANCE_SECTOR (yfinance Ticker.info['sector']; NOT GICS)"
SECTOR_SOURCE_CONTRACT = {
    "provider": "yfinance Ticker.info['sector'] (Yahoo Finance)",
    "producer": "screener_core.fetch_phase1_info -> score_part_a -> screen_history/*_full_data.csv",
    "taxonomy": "Yahoo Finance 11-sector taxonomy — NOT GICS",
    "precedence": ["newest screen_history *_full_data.csv with non-blank sector (exact ticker)",
                   "concentration_theme_taxonomy.json sector_captures (same provider)",
                   "UNKNOWN -> refuses new capital"],
    "blank": "UNKNOWN", "as_of": "screener file date, or capture fetched_at",
    "conflict": "different non-blank sectors across screener dates -> newest wins, SECTOR_CHANGED published",
    # ISA-0465 pre-LIVE close-out, 16-Sep-2026 (read-only 146-name census, yfinance 1.5.1):
    "empirical_census": ("_candidate_evidence/ISA-0465_16Sep2026/classification_census_16Sep2026.json - "
                         "sector/sectorKey/sectorDisp/industry/industryKey/industryDisp 146/146 populated "
                         "(US 95/95, non-US 51/51, held 6/6), 0 fetch failures, 9 sectors / 55 industries, "
                         "0 industry->sector conflicts, no peer-group field returned. `sector` stays the ONLY "
                         "binding classification; industry/keys are NOT a capital authority (no industry cap)."),
}
THEME_DECLARED_FILE = "concentration_theme_taxonomy.json"
THEME_SCHEMA_VERSION = "2.0"
THEME_STATES = ("MAPPED", "NO_MATERIAL_THEME")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
MODES = ("OFF", "SHADOW", "LIVE")
VERDICTS = ("PASS", "REFUSE_SECTOR", "REFUSE_THEME", "UNKNOWN_CLASSIFICATION",
            "RISK_REDUCING_EXCEPTION", "CAP_CONSTRAINED_ENTRY", "CAPPED_TO_HEADROOM")
TOL = 1e-9


class ConcentrationRefused(RuntimeError):
    pass


def mode() -> str:
    try:
        import isa_policy as _p
        m = _p.V2_FLAGS.get("concentration_gate", "OFF")
    except Exception:                                                   # noqa: BLE001
        m = "OFF"
    if m is True:
        m = "LIVE"
    if m in (False, None):
        m = "OFF"
    if m not in MODES:
        raise ConcentrationRefused("V2_FLAGS['concentration_gate'] = %r is not one of %s"
                                   % (m, list(MODES)))
    return m


def _base(t: str) -> str:
    return (t or "").strip().upper()


def validate_theme_doc(d: dict) -> List[str]:
    """Structural contract of the declared taxonomy. Any error -> the file is NOT used (all UNKNOWN)."""
    errs = []
    if not isinstance(d, dict):
        return ["taxonomy is not an object"]
    if d.get("schema_version") != THEME_SCHEMA_VERSION:
        errs.append("schema_version %r != %r" % (d.get("schema_version"), THEME_SCHEMA_VERSION))
    for k in ("version", "as_of", "assessor", "materiality_criterion", "themes", "memberships"):
        if not d.get(k):
            errs.append("missing %s" % k)
    themes = d.get("themes") or {}
    for t, m in (d.get("memberships") or {}).items():
        st = (m or {}).get("state")
        if st not in THEME_STATES:
            errs.append("%s: state %r not in %s" % (t, st, THEME_STATES))
            continue
        ev = m.get("evidence") or {}
        if not ev.get("provider") or not ev.get("fetched_at"):
            errs.append("%s: membership evidence lacks provider/fetched_at" % t)
        ths = m.get("themes")
        if st == "NO_MATERIAL_THEME":
            if ths:
                errs.append("%s: NO_MATERIAL_THEME carries themes" % t)
            if not m.get("rationale"):
                errs.append("%s: NO_MATERIAL_THEME without rationale" % t)
        else:
            if not ths:
                errs.append("%s: MAPPED with no themes" % t)
            seen = set()
            for x in ths or []:
                if x.get("theme") not in themes:
                    errs.append("%s: undeclared theme %r" % (t, x.get("theme")))
                if x.get("confidence") not in CONFIDENCE:
                    errs.append("%s: confidence %r" % (t, x.get("confidence")))
                if not x.get("rationale"):
                    errs.append("%s: theme %s without rationale" % (t, x.get("theme")))
                if x.get("theme") in seen:
                    errs.append("%s: duplicate theme %s" % (t, x.get("theme")))
                seen.add(x.get("theme"))
    return errs


def load_taxonomy(here: str = HERE) -> dict:
    """-> {sector: {TICKER: {value, source, as_of}}, theme: {TICKER: [theme ids]}, ...}.

    `theme[T]` is a LIST (possibly empty = NO_MATERIAL_THEME); a missing key is UNKNOWN."""
    sectors: Dict[str, dict] = {}
    history: Dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.join(here, "screen_history", "*_full_data.csv"))):
        as_of = os.path.basename(f)[:10]
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    t = _base(r.get("ticker") or r.get("symbol") or "")
                    s = (r.get("sector") or "").strip()
                    if t and s:
                        history.setdefault(t, {})[as_of] = s
                        sectors[t] = {"value": s, "source": os.path.basename(f), "as_of": as_of,
                                      "precedence": 1}
        except OSError:
            continue
    changed = {t: h for t, h in history.items() if len(set(h.values())) > 1}
    themes: Dict[str, list] = {}
    theme_prov: Dict[str, dict] = {}
    aliases: Dict[str, str] = {}
    meta = {"file": THEME_DECLARED_FILE, "state": "ABSENT", "errors": []}
    decl = os.path.join(here, THEME_DECLARED_FILE)
    if os.path.exists(decl):
        raw = open(decl, "rb").read()
        meta["sha256"] = hashlib.sha256(raw).hexdigest()
        try:
            d = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            d, meta["errors"] = None, ["unparseable: %s" % e]
        errs = validate_theme_doc(d) if d is not None else meta["errors"]
        meta.update(errors=errs[:50], n_errors=len(errs))
        if d is not None and not errs:
            meta.update(state="OK", version=d.get("version"), as_of=d.get("as_of"),
                        status=d.get("status"), assessor=d.get("assessor"))
            aliases = {_base(k): _base(v) for k, v in (d.get("aliases") or {}).items()}
            for t, m in d["memberships"].items():
                themes[_base(t)] = [x["theme"] for x in m.get("themes") or []]
                theme_prov[_base(t)] = {"state": m["state"],
                                        "confidence": ([x["confidence"] for x in m.get("themes")]
                                                       or [m.get("confidence")])}
            for t, v in ((d.get("sector_captures") or {}).get("values") or {}).items():
                if _base(t) not in sectors and v.get("sector"):
                    sectors[_base(t)] = {"value": v["sector"], "source": THEME_DECLARED_FILE,
                                         "as_of": v.get("fetched_at"), "precedence": 2}
        elif d is not None:
            meta["state"] = "INVALID"
    return {"sector": sectors, "theme": themes, "theme_provenance": theme_prov, "aliases": aliases,
            "sector_taxonomy": SECTOR_TAXONOMY, "sector_source_contract": SECTOR_SOURCE_CONTRACT,
            "sector_changed": changed,
            "theme_taxonomy": ("ISA-0700 multi-label v%s" % meta.get("version")
                               if meta["state"] == "OK" else "UNAVAILABLE (%s)" % meta["state"]),
            "declared_file": meta}


def _resolve(tax: dict, ticker: str) -> str:
    t = _base(ticker)
    return (tax.get("aliases") or {}).get(t, t)


def _sector(tax: dict, ticker: str) -> Optional[str]:
    t = _resolve(tax, ticker)
    rec = (tax.get("sector") or {}).get(t) or (tax.get("sector") or {}).get(_base(ticker))
    return rec["value"] if rec else None


def _themes(tax: dict, ticker: str) -> Optional[list]:
    """None = UNKNOWN; [] = NO_MATERIAL_THEME; [ids] = memberships."""
    th = tax.get("theme") or {}
    t = _resolve(tax, ticker)
    if t in th:
        return list(th[t])
    if _base(ticker) in th:
        return list(th[_base(ticker)])
    return None


def coverage(tickers, tax: dict) -> dict:
    """Coverage of a population (published every run; the pre-run coverage gate reads this)."""
    ts = sorted({_base(t) for t in tickers if t})
    sec_unknown = [t for t in ts if not _sector(tax, t)]
    th_unknown = [t for t in ts if _themes(tax, t) is None]
    nmt = [t for t in ts if _themes(tax, t) == []]
    n = len(ts)
    return {"n": n, "sector_known": n - len(sec_unknown), "sector_unknown": sec_unknown,
            "theme_mapped": n - len(th_unknown) - len(nmt), "theme_no_material": len(nmt),
            "theme_unknown": th_unknown,
            "sector_coverage_pct": round(100.0 * (n - len(sec_unknown)) / n, 1) if n else None,
            "theme_coverage_pct": round(100.0 * (n - len(th_unknown)) / n, 1) if n else None,
            "state": ("COMPLETE" if not sec_unknown and not th_unknown else "PARTIAL"),
            "sector_changed": sorted(t for t in ts if _resolve(tax, t) in (tax.get("sector_changed") or {})),
            "taxonomy": (tax.get("declared_file") or {}).get("version"),
            "taxonomy_state": (tax.get("declared_file") or {}).get("state")}


def gate(book: Dict[str, float], ticker: str, gbp: float, *, nav_gbp: float, tax: dict,
         is_new: bool, min_entry_gbp: float, partial_entry_allowed: Optional[bool] = None) -> dict:
    """ONE candidate against the prospective book (held + same-run admissions).

    `book` {ticker: GBP}. Sector exposure is % of NAV; each theme's exposure is % of the POST-TRADE
    direct-stock sleeve. `partial_entry_allowed` is RETIRED for this gate (Raj A1.3 decides capped
    entry by the minimum meaningful entry alone) and accepted only for call compatibility."""
    _fi_mark("concentration_control", "gate")
    gbp = float(gbp)
    sec, thm = _sector(tax, ticker), _themes(tax, ticker)
    out = {"ticker": ticker, "requested_gbp": round(gbp, 2), "sector": sec,
           "themes": thm, "theme_state": ("UNKNOWN" if thm is None else
                                          ("NO_MATERIAL_THEME" if thm == [] else "MAPPED")),
           "sector_cap_pct_nav": SECTOR_CAP_NAV * 100, "theme_cap_pct_sleeve": THEME_CAP_SLEEVE * 100,
           "is_new": bool(is_new)}
    if gbp <= 0:
        out.update(verdict="RISK_REDUCING_EXCEPTION", allowed_gbp=gbp,
                   why="a trim/sale never worsens concentration")
        return out
    if not sec or thm is None:
        out.update(verdict="UNKNOWN_CLASSIFICATION", allowed_gbp=0.0,
                   why=("missing %s classification — no positive capital (R4.3)"
                        % " and ".join(x for x, v in (("sector", sec), ("theme", thm is not None))
                                       if not v)))
        return out
    c = THEME_CAP_SLEEVE
    sleeve0 = sum(max(v, 0.0) for v in book.values())
    unk_sec = sum(v for t, v in book.items() if not _sector(tax, t))
    unk_thm = sum(v for t, v in book.items() if _themes(tax, t) is None)
    sec0 = sum(v for t, v in book.items() if _sector(tax, t) == sec)
    sec_cap = SECTOR_CAP_NAV * float(nav_gbp)
    sec_room = sec_cap - (sec0 + unk_sec)
    sec_room_known = sec_cap - sec0
    per_theme = {}
    for T in thm:
        t0 = sum(v for t, v in book.items() if T in (_themes(tax, t) or []))
        # (t0 + unk + x) <= c (sleeve0 + x)  ->  x <= (c*sleeve0 - t0 - unk) / (1 - c)
        per_theme[T] = {"pre_gbp": round(t0, 2),
                        "pre_pct_sleeve": round(100 * t0 / sleeve0, 3) if sleeve0 else None,
                        "headroom_gbp": round((c * sleeve0 - (t0 + unk_thm)) / (1.0 - c), 2),
                        "_room": (c * sleeve0 - (t0 + unk_thm)) / (1.0 - c),
                        "_room_known": (c * sleeve0 - t0) / (1.0 - c)}
    if per_theme:
        bind_t = min(per_theme, key=lambda k: per_theme[k]["_room"])
        thm_room, thm_room_known = per_theme[bind_t]["_room"], min(v["_room_known"] for v in per_theme.values())
    else:
        bind_t, thm_room, thm_room_known = None, float("inf"), float("inf")
    out.update(pre_sector_pct_nav=round(100 * sec0 / nav_gbp, 3) if nav_gbp else None,
               unclassified_held_gbp={"sector": round(unk_sec, 2), "theme": round(unk_thm, 2)},
               sector_headroom_gbp=round(sec_room, 2),
               theme_headroom_gbp=(None if bind_t is None else round(thm_room, 2)),
               binding_theme=bind_t)
    room = min(sec_room, thm_room)
    binding = "sector" if sec_room <= thm_room else "theme"
    room_known = min(sec_room_known, thm_room_known)
    refuse = "REFUSE_SECTOR" if binding == "sector" else "REFUSE_THEME"
    if gbp <= room + TOL:
        allowed, verdict = gbp, "PASS"
    elif gbp <= room_known + TOL and (unk_sec > 0 or unk_thm > 0):
        allowed, verdict = 0.0, "UNKNOWN_CLASSIFICATION"
    elif room < 0.01:              # no investable headroom: never a zero-pound capped admission
        allowed, verdict = 0.0, refuse
    elif is_new:
        if room + TOL >= float(min_entry_gbp):
            allowed, verdict = room, "CAP_CONSTRAINED_ENTRY"
        else:                      # a NEW position is never sub-scale
            allowed, verdict = 0.0, refuse
    else:
        allowed, verdict = room, "CAPPED_TO_HEADROOM"
    allowed = max(allowed, 0.0)
    sl1 = sleeve0 + allowed
    for T, v in per_theme.items():
        v["post_pct_sleeve"] = round(100 * (v["pre_gbp"] + allowed) / sl1, 3) if sl1 else None
        v.pop("_room"), v.pop("_room_known")
    out.update(verdict=verdict, allowed_gbp=round(allowed, 2), binding=binding,
               post_sector_pct_nav=round(100 * (sec0 + allowed) / nav_gbp, 3) if nav_gbp else None,
               per_theme=per_theme,
               post_theme_pct_sleeve=(max((v["post_pct_sleeve"] or 0.0) for v in per_theme.values())
                                      if per_theme else None),
               why=("equality passes; greater-than fails; a concentration-limited NEW entry opens only "
                    "at >= the minimum meaningful entry (CAP_CONSTRAINED_ENTRY); unclassified held "
                    "value counted worst-case; multi-label names count in every theme"))
    return out


def exposures(book: Dict[str, float], *, nav_gbp: float, tax: dict) -> dict:
    sec, th = {}, {}
    for t, v in book.items():
        s = _sector(tax, t) or "UNKNOWN"
        sec[s] = sec.get(s, 0.0) + v
        ts = _themes(tax, t)
        for T in (["UNKNOWN"] if ts is None else (ts or ["NO_MATERIAL_THEME"])):
            th[T] = th.get(T, 0.0) + v
    sl = sum(book.values())
    return {"sleeve_gbp": round(sl, 2), "sector_gbp": sec, "theme_gbp": th,
            "sector_pct_nav": {k: round(100 * v / nav_gbp, 3) for k, v in sec.items()} if nav_gbp else {},
            "theme_pct_sleeve": {k: round(100 * v / sl, 3) for k, v in th.items()} if sl else {}}


def book_invariant(book: Dict[str, float], *, nav_gbp: float, tax: dict,
                   pre_book: Dict[str, float]) -> dict:
    """Final-allocation invariant: no sector/theme whose exposure INCREASED is above its cap."""
    e0, e1 = exposures(pre_book, nav_gbp=nav_gbp, tax=tax), exposures(book, nav_gbp=nav_gbp, tax=tax)
    viol = []
    sl1 = e1["sleeve_gbp"]
    skip = ("UNKNOWN", "NO_MATERIAL_THEME")
    for cl, v in e1["sector_gbp"].items():
        if cl not in skip and v > e0["sector_gbp"].get(cl, 0.0) + 0.01 and v > SECTOR_CAP_NAV * nav_gbp + 0.01:
            viol.append({"kind": "sector", "class": cl, "pct_nav": round(100 * v / nav_gbp, 3)})
    for cl, v in e1["theme_gbp"].items():
        if cl not in skip and v > e0["theme_gbp"].get(cl, 0.0) + 0.01 and sl1 and v > THEME_CAP_SLEEVE * sl1 + 0.01:
            viol.append({"kind": "theme", "class": cl, "pct_sleeve": round(100 * v / sl1, 3)})
    return {"state": "PASS" if not viol else "FAIL", "violations": viol,
            "sector_exposure_pct_nav": e1["sector_pct_nav"],
            "theme_exposure_pct_sleeve": e1["theme_pct_sleeve"],
            "pre_sector_exposure_pct_nav": e0["sector_pct_nav"],
            "pre_theme_exposure_pct_sleeve": e0["theme_pct_sleeve"]}


# ── ISA-0700 pre-LIVE close-out: risk-weighted taxonomy EXCEPTION REVIEW + LIVE readiness ─────────
# Raj 16-Sep-2026: do not approve 146 rows wholesale; adjudicate a risk-weighted exception set and keep
# the review status explicit until it is adjudicated. Blanket approval is never inferred.
# ISA-0700 (16-Sep-2026, adjudication package §3.3): presented as the MATERIALITY REVIEW SET, not
# "exceptions" (a conservative review cohort, not detected errors); the old combined selector
# MEDIUM_CONFIDENCE_CAPITAL_RELEVANT is split into its two real grounds.
EXCEPTION_REASONS = ("HELD", "LOW_CONFIDENCE_MAPPED", "LOW_CONFIDENCE_NO_MATERIAL_THEME",
                     "PLAUSIBLE_FALSE_NEGATIVE_NMT", "MEDIUM_CAPITAL_RELEVANT",
                     "MEDIUM_NEAR_BINDING_THEME")
ADJUDICATION_DECISIONS = ("CONFIRMED", "AMENDED")
# Review-SELECTION parameter only (never gates capital): a theme whose current sleeve share is at least
# this fraction of THEME_CAP_SLEEVE is "close to binding", so its MEDIUM memberships are reviewed.
NEAR_BINDING_FRACTION_OF_CAP = 0.80
REVIEW_FILE = "concentration_taxonomy_exception_review.json"


def membership_sha256(m: dict) -> str:
    return hashlib.sha256(json.dumps(m, sort_keys=True).encode("utf-8")).hexdigest()


def exception_review(doc: dict, *, held: Dict[str, float], capital_relevant) -> dict:
    """-> the exception set Raj adjudicates. `held` {ticker: GBP}; `capital_relevant` = tickers the
    router could fund this run (qualifying uses). An adjudication counts only while the membership it
    judged is byte-identical (membership_sha256) — an amended row must be re-adjudicated."""
    errs = validate_theme_doc(doc)
    if errs:
        return {"state": "UNAVAILABLE", "why": "taxonomy invalid: %s" % errs[:5], "rows": []}
    al = {_base(k): _base(v) for k, v in (doc.get("aliases") or {}).items()}
    mem = {_base(k): v for k, v in doc["memberships"].items()}
    res = lambda t: al.get(_base(t), _base(t))                           # noqa: E731
    held_r = {}
    for t, v in (held or {}).items():
        held_r[res(t)] = held_r.get(res(t), 0.0) + float(v or 0.0)
    cap_r = {res(t) for t in (capital_relevant or [])}
    sleeve = sum(max(v, 0.0) for v in held_r.values())
    theme_share = {}
    for t, v in held_r.items():
        for x in (mem.get(t) or {}).get("themes") or []:
            theme_share[x["theme"]] = theme_share.get(x["theme"], 0.0) + v
    near = sorted(T for T, v in theme_share.items()
                  if sleeve and v / sleeve >= NEAR_BINDING_FRACTION_OF_CAP * THEME_CAP_SLEEVE)
    ind_mapped = {}
    for t, m in mem.items():
        if m["state"] == "MAPPED":
            ind = (m.get("evidence") or {}).get("industry_at_fetch")
            if ind:
                ind_mapped.setdefault(ind, set()).update(x["theme"] for x in m["themes"])
    adj = ((doc.get("review") or {}).get("adjudications") or {})
    rows, missing_held = [], sorted(t for t in held_r if t not in mem)
    for t, m in sorted(mem.items()):
        reasons, ths = [], m.get("themes") or []
        if t in held_r:
            reasons.append("HELD")
        if any(x["confidence"] == "LOW" for x in ths):
            reasons.append("LOW_CONFIDENCE_MAPPED")
        ind = (m.get("evidence") or {}).get("industry_at_fetch")
        if m["state"] == "NO_MATERIAL_THEME":
            if m.get("confidence") == "LOW":
                reasons.append("LOW_CONFIDENCE_NO_MATERIAL_THEME")
            if ind and ind in ind_mapped:
                reasons.append("PLAUSIBLE_FALSE_NEGATIVE_NMT")
        if any(x["confidence"] == "MEDIUM" for x in ths) and (t in cap_r or t in held_r):
            reasons.append("MEDIUM_CAPITAL_RELEVANT")
        if any(x["confidence"] == "MEDIUM" and x["theme"] in near for x in ths):
            reasons.append("MEDIUM_NEAR_BINDING_THEME")
        if not reasons:
            continue
        a = adj.get(t) or {}
        sha = membership_sha256(m)
        if a.get("decision") in ADJUDICATION_DECISIONS and a.get("by") and a.get("on") \
                and a.get("membership_sha256") == sha:
            status = "ADJUDICATED_%s" % a["decision"]
        elif a:
            status = "ADJUDICATION_STALE_OR_INCOMPLETE"
        else:
            status = "PENDING_RAJ"
        rows.append({"ticker": t, "state": m["state"], "industry_at_fetch": ind,
                     "themes": [{"theme": x["theme"], "confidence": x["confidence"],
                                 "rationale": x.get("rationale")} for x in ths],
                     "nmt_confidence": m.get("confidence") if m["state"] == "NO_MATERIAL_THEME" else None,
                     "nmt_rationale": m.get("rationale") if m["state"] == "NO_MATERIAL_THEME" else None,
                     "peer_industry_themes": (sorted(ind_mapped.get(ind, ()))
                                              if "PLAUSIBLE_FALSE_NEGATIVE_NMT" in reasons else None),
                     "capital_relevant": t in cap_r, "held_gbp": round(held_r.get(t, 0.0), 2) or None,
                     "reasons": reasons, "membership_sha256": sha, "review_status": status})
    pending = [r["ticker"] for r in rows if not r["review_status"].startswith("ADJUDICATED_")]
    by = {k: sum(1 for r in rows if k in r["reasons"]) for k in EXCEPTION_REASONS}
    state = ("UNKNOWN_HELD_UNMAPPED" if missing_held else
             ("ADJUDICATED" if not pending else "EXCEPTION_REVIEW_PENDING"))
    return {"presentation_name": "materiality review set", "state": state, "taxonomy_version": doc.get("version"), "taxonomy_status": doc.get("status"),
            "n_population": len(mem), "n_exceptions": len(rows), "n_pending": len(pending),
            "n_not_in_exception_set": len(mem) - len(rows), "by_reason": by,
            "held_not_in_taxonomy": missing_held, "near_binding_themes": near,
            "held_theme_share_pct_sleeve": {T: round(100 * v / sleeve, 2) for T, v in sorted(theme_share.items())}
            if sleeve else {},
            "rows": rows,
            "selection_basis": ("Raj 16-Sep-2026 risk-weighted exception review: all HELD; all LOW-confidence "
                                "memberships; LOW-confidence NO_MATERIAL_THEME; NO_MATERIAL_THEME sharing a Yahoo "
                                "industry with a MAPPED name (plausible false negative - heuristic, labelled); "
                                "MEDIUM memberships only where capital-relevant (qualifying use / held) or in a "
                                "theme >= %.0f%% of its cap. Rows outside the set are NOT thereby approved: the "
                                "taxonomy stays UNREVIEWED_BY_RAJ; this set is what LIVE readiness requires."
                                % (100 * NEAR_BINDING_FRACTION_OF_CAP))}


def real_shadow_evidence(here: str = HERE) -> dict:
    """A REAL pre-run SHADOW record (never DRYRUN/scenario/fixture): run_context_<mmm>_<yyyy>.json whose
    summary.capital_destination.concentration carries mode SHADOW and an available impact."""
    found = []
    for f in sorted(glob.glob(os.path.join(here, "run_context_*_*.json"))):
        b = os.path.basename(f)
        if not re.match(r"^run_context_[a-z]{3}_\d{4}\.json$", b):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        c = (((d.get("summary") or {}).get("capital_destination") or {}).get("concentration") or {})
        imp = c.get("impact") or {}
        if c.get("mode") == "SHADOW" and imp and imp.get("state") != "UNAVAILABLE":
            found.append(b)
    return {"state": "PRESENT" if found else "ABSENT", "files": found}


def live_readiness(here: str = HERE, *, review: Optional[dict] = None,
                   coverage_state: Optional[str] = None) -> dict:
    """Mechanical pre-LIVE blockers. Raj's LIVE decision is separate and is the flag itself; this only
    says whether flipping it would be defensible. LIVE with blockers is REFUSED by capital_destination."""
    blockers = []
    tax = load_taxonomy(here)
    if (tax.get("declared_file") or {}).get("state") != "OK":
        blockers.append("THEME_TAXONOMY_NOT_OK (%s)" % (tax.get("declared_file") or {}).get("state"))
    if review is None:
        rv = {"state": "UNKNOWN"}
        p = os.path.join(here, REVIEW_FILE)
        if os.path.exists(p):
            try:
                rv = json.load(open(p, encoding="utf-8"))
            except ValueError:
                rv = {"state": "UNREADABLE"}
        review = rv
    if review.get("state") != "ADJUDICATED":
        blockers.append("TAXONOMY_EXCEPTION_REVIEW_%s (%s pending)"
                        % (review.get("state"), review.get("n_pending", "?")))
    ev = real_shadow_evidence(here)
    if ev["state"] != "PRESENT":
        blockers.append("NO_REAL_PRERUN_SHADOW_EVIDENCE (first due 03-Oct-2026 pre-run)")
    if coverage_state is not None and coverage_state != "COMPLETE":
        blockers.append("CLASSIFICATION_COVERAGE_%s" % coverage_state)
    return {"state": "READY" if not blockers else "NOT_READY", "blockers": blockers,
            "shadow_evidence": ev, "review_state": review.get("state"),
            "basis": "ISA-0465/ISA-0700 pre-LIVE requirements (16-Sep-2026); a READY state is not a LIVE decision"}


def _cli_population(here: str = HERE):
    """(held {ticker: GBP}, capital_relevant [tickers], sources) from the newest real artefacts."""
    held, cap, src = {}, [], {}
    pf = sorted(glob.glob(os.path.join(here, "portfolio_data_*_*.json")), key=os.path.getmtime)
    if pf:
        d = json.load(open(pf[-1], encoding="utf-8"))
        for h in d.get("stocks") or []:
            if h.get("ticker"):
                held[h["ticker"]] = held.get(h["ticker"], 0.0) + float(h.get("value_gbp") or 0.0)
        src["held"] = os.path.basename(pf[-1])
    cd = [f for f in sorted(glob.glob(os.path.join(here, "capital_destination_*_*.json")), key=os.path.getmtime)
          if re.match(r"^capital_destination_[a-z]{3}_\d{4}\.json$", os.path.basename(f))]
    if cd:
        d = json.load(open(cd[-1], encoding="utf-8"))
        cap = [u.get("ticker") for u in (((d.get("sleeve_split") or {}).get("demand_pull") or {})
                                         .get("qualifying_uses") or []) if u.get("ticker")]
        src["capital_relevant"] = os.path.basename(cd[-1])
    return held, cap, src


# ── selftest: BuildSpec §14 fixtures + R19.3 amendment A1 ────────────────────────────────
def _selftest(verbose: bool = True) -> int:
    import tempfile
    fails = []

    def ok(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    NAV = 100_000.0
    ME = 2_800.0
    S = lambda v: {"value": v}                                           # noqa: E731
    tax = {"sector": {"A": S("Tech"), "B": S("Tech"), "C": S("Tech"), "H": S("Health"),
                      "X": S("Tech"), "U": S("Tech"), "N": S("Staples"), "M": S("Ind")},
           "theme": {"A": ["T1"], "B": ["T2"], "C": ["T3"], "H": ["T4"], "X": ["T5"], "N": [],
                     "M": ["T1", "T2"]}}
    g = gate({"A": 4500.0}, "B", 4500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("2 x NORMAL same sector = 9% -> PASS", g["verdict"] == "PASS" and g["post_sector_pct_nav"] == 9.0)
    g = gate({"A": 4500.0, "B": 4500.0}, "C", 3500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("MUST-FIRE A1.3: + STARTER would be 12.5% -> CAP_CONSTRAINED_ENTRY at the GBP 3,000 headroom "
       "(>= 2,800 minimum entry), exactly 12.0% post-trade, never 12.5%",
       g["verdict"] == "CAP_CONSTRAINED_ENTRY" and g["allowed_gbp"] == 3000.0
       and g["post_sector_pct_nav"] == 12.0)
    g2 = gate({"A": 4500.0, "B": 4500.0}, "C", 3500.0, nav_gbp=NAV, tax=tax, is_new=True,
              min_entry_gbp=ME, partial_entry_allowed=False)
    ok("partial_entry_allowed is retired for this gate: identical verdict", g2["verdict"] == g["verdict"])
    g = gate({"A": 4500.0, "B": 5200.0}, "C", 3500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("MUST-FIRE: headroom 2,300 < 2,800 minimum entry -> REFUSE_SECTOR, no dust position",
       g["verdict"] == "REFUSE_SECTOR" and g["allowed_gbp"] == 0.0)
    g = gate({"A": 4500.0, "B": 4500.0, "N": 20000.0}, "A", 3500.0, nav_gbp=NAV, tax=tax, is_new=False, min_entry_gbp=ME)
    ok("held increase limited by headroom -> CAPPED_TO_HEADROOM (not an entry)",
       g["verdict"] == "CAPPED_TO_HEADROOM" and g["allowed_gbp"] == 3000.0)
    g = gate({"A": 6500.0}, "B", 5500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("EARNED_MAX 6.5% + HIGH 5.5% = 12% exactly -> PASS (equality passes)", g["verdict"] == "PASS")
    g = gate({"A": 6500.0, "B": 5500.0, "H": 20000.0}, "A", 500.0, nav_gbp=NAV, tax=tax, is_new=False, min_entry_gbp=ME)
    ok("MUST-FIRE: further same-sector increase above 12% -> REFUSE_SECTOR", g["verdict"] == "REFUSE_SECTOR")
    g = gate({"A": 14000.0}, "A", -2000.0, nav_gbp=NAV, tax=tax, is_new=False, min_entry_gbp=ME)
    ok("existing breach: a risk-reducing trim is allowed (no forced sale)", g["verdict"] == "RISK_REDUCING_EXCEPTION")
    g = gate({"A": 14000.0}, "B", 3000.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("existing breach: a worsening addition is refused", g["verdict"] == "REFUSE_SECTOR")
    tax2 = {"sector": {"P": S("S1"), "Q": S("S2")}, "theme": {"P": ["TH"], "Q": ["OTHER"]}}
    g = gate({"P": 5000.0, "Q": 10000.0}, "P", 5000.0, nav_gbp=1_000_000.0, tax=tax2, is_new=False, min_entry_gbp=ME)
    ok("theme exactly 50% of post-trade sleeve -> PASS", g["verdict"] == "PASS" and g["post_theme_pct_sleeve"] == 50.0)
    g = gate({"P": 5000.0, "Q": 10000.0}, "P", 5001.0, nav_gbp=1_000_000.0, tax=tax2, is_new=False, min_entry_gbp=ME)
    ok("MUST-FIRE: theme above 50% -> capped, never above the cap",
       g["verdict"] in ("CAPPED_TO_HEADROOM", "REFUSE_THEME") and g["post_theme_pct_sleeve"] <= 50.0 + 1e-6)
    g = gate({"P": 3000.0}, "P", 3000.0, nav_gbp=1_000_000.0, tax=tax2, is_new=False, min_entry_gbp=ME)
    ok("A1.2 no small-sleeve exemption: a one-theme sleeve adding to that theme is refused (100% > 50%)",
       g["verdict"] == "REFUSE_THEME" and g["allowed_gbp"] == 0.0)
    g = gate({"A": 4500.0}, "NOCLASS", 3500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("NEGATIVE CONTROL: unmapped name -> UNKNOWN_CLASSIFICATION, no positive capital",
       g["verdict"] == "UNKNOWN_CLASSIFICATION" and g["allowed_gbp"] == 0.0)
    tax3 = {"sector": {"A": S("Tech"), "Z": S("Tech")}, "theme": {"A": ["T1"]}}
    g = gate({"A": 1000.0}, "Z", 3000.0, nav_gbp=NAV, tax=tax3, is_new=True, min_entry_gbp=ME)
    ok("NEGATIVE CONTROL: sector known but theme UNMAPPED -> UNKNOWN (never proxied from sector)",
       g["verdict"] == "UNKNOWN_CLASSIFICATION" and g["theme_state"] == "UNKNOWN")
    g = gate({"A": 10000.0}, "N", 5000.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("NO_MATERIAL_THEME passes the theme cap on a sleeve dominated by one theme",
       g["verdict"] == "PASS" and g["theme_state"] == "NO_MATERIAL_THEME")
    g = gate({"B": 5000.0, "N": 5000.0}, "M", 3000.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("MUST-FIRE multi-label: M in [T1,T2]; T2 already 50% of sleeve -> REFUSE_THEME bound by T2 although T1 is empty",
       g["verdict"] == "REFUSE_THEME" and g["binding_theme"] == "T2")
    g = gate({"A": 2000.0, "N": 8000.0}, "M", 3000.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("multi-label positive control: both themes within cap -> PASS with per-theme arithmetic",
       g["verdict"] == "PASS" and set(g["per_theme"]) == {"T1", "T2"})
    e = exposures({"M": 3000.0, "A": 1000.0}, nav_gbp=NAV, tax=tax)
    ok("multi-label counts IN FULL toward every membership (T1 = 4,000; T2 = 3,000)",
       round(e["theme_gbp"]["T1"]) == 4000 and round(e["theme_gbp"]["T2"]) == 3000)
    g = gate({"A": 4500.0, "U": 3000.0}, "B", 4000.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("an unclassified-theme HELD name counts worst-case -> UNKNOWN, never PASS on lost headroom",
       g["verdict"] in ("UNKNOWN_CLASSIFICATION", "PASS")
       and (g["verdict"] != "PASS" or g["theme_headroom_gbp"] >= 4000.0))
    g = gate({"A": 6500.0, "B": 5499.999, "H": 20000.0}, "A", 800.0, nav_gbp=NAV, tax=tax,
             is_new=False, min_entry_gbp=ME)
    ok("NEGATIVE CONTROL: sub-penny headroom is REFUSE, never a GBP 0.00 capped admission",
       g["verdict"] == "REFUSE_SECTOR" and g["allowed_gbp"] == 0.0)
    g1 = gate({"A": 4500.0}, "B", 4500.0, nav_gbp=NAV, tax=tax, is_new=True, min_entry_gbp=ME)
    ok("independence from ISA-0600: the gate reads no beta", g1["verdict"] == "PASS" and "beta" not in g1)
    inv = book_invariant({"A": 6500.0, "B": 6000.0}, nav_gbp=NAV, tax=tax, pre_book={"A": 6500.0})
    ok("NEGATIVE CONTROL: final-book invariant FAILS when an addition leaves a sector above 12%", inv["state"] == "FAIL")
    inv = book_invariant({"A": 14000.0}, nav_gbp=NAV, tax=tax, pre_book={"A": 14000.0})
    ok("POSITIVE CONTROL: an unchanged pre-existing breach does not fail the invariant", inv["state"] == "PASS")
    inv = book_invariant({"B": 5000.0, "N": 1000.0, "M": 3000.0}, nav_gbp=1e7, tax=tax,
                         pre_book={"B": 5000.0, "N": 1000.0})
    ok("NEGATIVE CONTROL: invariant counts multi-label (T2 = 8,000 / 9,000 > 50%) -> FAIL",
       inv["state"] == "FAIL" and any(v["class"] == "T2" for v in inv["violations"]))
    # ── declared-file contract + sector precedence, on a scratch directory ──
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "screen_history"))
        def w(name, rows):
            with open(os.path.join(td, "screen_history", name), "w", newline="") as fh:
                cw = csv.writer(fh); cw.writerow(["ticker", "sector"]); cw.writerows(rows)
        w("2026-08-01_X_full_data.csv", [["AAA", "Technology"], ["BBB", "Industrials"]])
        w("2026-09-01_X_full_data.csv", [["AAA", "Communication Services"], ["BBB", ""]])
        good = {"schema_version": "2.0", "version": "9.9.9", "as_of": "2026-09-16", "assessor": "t",
                "materiality_criterion": "m", "themes": {"T1": {"name": "t"}},
                "aliases": {"CCC": "CCC.L"},
                "memberships": {"AAA": {"state": "MAPPED", "themes": [{"theme": "T1", "confidence": "HIGH", "rationale": "r"}],
                                        "evidence": {"provider": "p", "fetched_at": "x"}},
                                "CCC.L": {"state": "NO_MATERIAL_THEME", "themes": [], "rationale": "r",
                                          "evidence": {"provider": "p", "fetched_at": "x"}}},
                "sector_captures": {"values": {"CCC.L": {"sector": "Healthcare", "fetched_at": "x"},
                                               "AAA": {"sector": "Energy", "fetched_at": "x"}}}}
        json.dump(good, open(os.path.join(td, THEME_DECLARED_FILE), "w"))
        tx = load_taxonomy(td)
        ok("sector precedence: newest non-blank screener value wins (AAA -> Communication Services)",
           _sector(tx, "AAA") == "Communication Services")
        ok("sector precedence: a BLANK newer row does not erase an older non-blank value (BBB)",
           _sector(tx, "BBB") == "Industrials")
        ok("sector precedence: capture used only where no screener value exists (CCC via alias)",
           _sector(tx, "CCC") == "Healthcare" and tx["sector"]["AAA"]["precedence"] == 1)
        ok("SECTOR_CHANGED published for AAA", "AAA" in tx["sector_changed"])
        ok("declared file OK: version/sha published; alias resolves NO_MATERIAL_THEME",
           tx["declared_file"]["state"] == "OK" and tx["declared_file"].get("sha256")
           and _themes(tx, "CCC") == [] and _themes(tx, "AAA") == ["T1"])
        cov = coverage(["AAA", "BBB", "CCC"], tx)
        ok("coverage: BBB theme UNKNOWN reported, not hidden", cov["theme_unknown"] == ["BBB"] and cov["state"] == "PARTIAL")
        bad = json.loads(json.dumps(good))
        bad["memberships"]["AAA"]["themes"][0]["theme"] = "NOT_DECLARED"
        bad["memberships"]["CCC.L"]["themes"] = [{"theme": "T1", "confidence": "HIGH", "rationale": "r"}]
        json.dump(bad, open(os.path.join(td, THEME_DECLARED_FILE), "w"))
        tb = load_taxonomy(td)
        ok("NEGATIVE CONTROL: invalid declared taxonomy is NOT used -> every theme UNKNOWN (fail-closed)",
           tb["declared_file"]["state"] == "INVALID" and _themes(tb, "AAA") is None
           and tb["declared_file"]["n_errors"] >= 2)
        del bad["memberships"]["AAA"]["evidence"]
        ok("NEGATIVE CONTROL: membership without provenance is a contract error",
           any("evidence" in x for x in validate_theme_doc(bad)))
    # ── ISA-0700 pre-LIVE: exception review + readiness ──
    rd = {"schema_version": "2.0", "version": "1.0.0", "as_of": "x", "assessor": "t", "materiality_criterion": "m",
          "themes": {"T1": {"name": "t"}, "T2": {"name": "u"}},
          "aliases": {"HH": "HH.L"},
          "memberships": {
              "HH.L": {"state": "MAPPED", "themes": [{"theme": "T1", "confidence": "HIGH", "rationale": "r"}],
                       "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Semis"}},
              "LO": {"state": "MAPPED", "themes": [{"theme": "T2", "confidence": "LOW", "rationale": "r"}],
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Other"}},
              "NF": {"state": "NO_MATERIAL_THEME", "themes": [], "confidence": "MEDIUM", "rationale": "r",
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Semis"}},
              "NL": {"state": "NO_MATERIAL_THEME", "themes": [], "confidence": "LOW", "rationale": "r",
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Food"}},
              "MC": {"state": "MAPPED", "themes": [{"theme": "T2", "confidence": "MEDIUM", "rationale": "r"}],
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Other2"}},
              "MN": {"state": "MAPPED", "themes": [{"theme": "T1", "confidence": "MEDIUM", "rationale": "r"}],
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Other3"}},
              "MX": {"state": "MAPPED", "themes": [{"theme": "T2", "confidence": "MEDIUM", "rationale": "r"}],
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Other4"}},
              "OK": {"state": "NO_MATERIAL_THEME", "themes": [], "confidence": "MEDIUM", "rationale": "r",
                     "evidence": {"provider": "p", "fetched_at": "x", "industry_at_fetch": "Food"}}}}
    rv = exception_review(rd, held={"HH": 1000.0}, capital_relevant=["MC"])
    got = {r["ticker"]: set(r["reasons"]) for r in rv["rows"]}
    ok("MUST-FIRE review: held (via alias), LOW mapped, LOW NMT, industry-peer NMT, capital-relevant MEDIUM, "
       "near-binding MEDIUM all selected",
       got.get("HH.L") == {"HELD"} and got.get("LO") == {"LOW_CONFIDENCE_MAPPED"}
       and "PLAUSIBLE_FALSE_NEGATIVE_NMT" in got.get("NF", set())
       and got.get("NL") == {"LOW_CONFIDENCE_NO_MATERIAL_THEME"}
       and got.get("MC") == {"MEDIUM_CAPITAL_RELEVANT"}
       and got.get("MN") == {"MEDIUM_NEAR_BINDING_THEME"})
    ok("NEGATIVE CONTROL review: MEDIUM in a non-binding theme and not capital-relevant (MX), and a plain "
       "MEDIUM NMT (OK), are NOT exceptions - and are not thereby approved (state PENDING)",
       "MX" not in got and "OK" not in got and rv["state"] == "EXCEPTION_REVIEW_PENDING"
       and rv["n_not_in_exception_set"] == 2)
    rd2 = json.loads(json.dumps(rd))
    rd2["review"] = {"adjudications": {r["ticker"]: {"decision": "CONFIRMED", "by": "Raj", "on": "2026-10-01",
                                                     "membership_sha256": r["membership_sha256"]}
                                       for r in rv["rows"]}}
    ok("POSITIVE CONTROL: every exception adjudicated against its exact membership -> ADJUDICATED",
       exception_review(rd2, held={"HH": 1000.0}, capital_relevant=["MC"])["state"] == "ADJUDICATED")
    rd2["memberships"]["LO"]["themes"][0]["confidence"] = "MEDIUM"
    r3 = exception_review(rd2, held={"HH": 1000.0}, capital_relevant=["MC", "LO"])
    ok("NEGATIVE CONTROL: amending an adjudicated membership invalidates its adjudication (sha) -> PENDING",
       r3["state"] == "EXCEPTION_REVIEW_PENDING"
       and any(x["ticker"] == "LO" and x["review_status"] == "ADJUDICATION_STALE_OR_INCOMPLETE" for x in r3["rows"]))
    ok("NEGATIVE CONTROL: a held name absent from the taxonomy blocks the review state",
       exception_review(rd, held={"ZZ": 5.0}, capital_relevant=[])["state"] == "UNKNOWN_HELD_UNMAPPED")
    with tempfile.TemporaryDirectory() as td2:
        json.dump(rd, open(os.path.join(td2, THEME_DECLARED_FILE), "w"))
        lr = live_readiness(td2)
        ok("MUST-FIRE readiness: no review file + no real SHADOW run -> NOT_READY with both blockers",
           lr["state"] == "NOT_READY" and len(lr["blockers"]) == 2)
        cdoc = {"summary": {"capital_destination": {"concentration": {"mode": "SHADOW", "impact": {"state": "OK"}}}}}
        json.dump(cdoc, open(os.path.join(td2, "run_context_oct_2026.DRYRUN.json"), "w"))
        ok("NEGATIVE CONTROL: a DRYRUN run_context is not real SHADOW evidence",
           real_shadow_evidence(td2)["state"] == "ABSENT")
        json.dump(cdoc, open(os.path.join(td2, "run_context_oct_2026.json"), "w"))
        lr = live_readiness(td2, review={"state": "ADJUDICATED"}, coverage_state="COMPLETE")
        ok("POSITIVE CONTROL readiness: valid taxonomy + ADJUDICATED + real SHADOW + COMPLETE coverage -> READY",
           lr["state"] == "READY")
        ok("NEGATIVE CONTROL readiness: PARTIAL coverage blocks",
           live_readiness(td2, review={"state": "ADJUDICATED"}, coverage_state="PARTIAL")["state"] == "NOT_READY")
    real = os.path.join(HERE, THEME_DECLARED_FILE)
    if os.path.exists(real):
        ok("the DECLARED taxonomy on disk passes its own contract",
           not validate_theme_doc(json.load(open(real, encoding="utf-8"))))
    if verbose:
        print("concentration_control selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--exception-review", action="store_true",
                    help="write %s from the declared taxonomy + newest portfolio/capital_destination" % REVIEW_FILE)
    ap.add_argument("--live-readiness", action="store_true")
    a = ap.parse_args(argv)
    if a.exception_review:
        held, cap, src = _cli_population()
        doc = json.load(open(os.path.join(HERE, THEME_DECLARED_FILE), encoding="utf-8"))
        rv = exception_review(doc, held=held, capital_relevant=cap)
        rv["population_sources"] = src
        rv["taxonomy_sha256"] = hashlib.sha256(open(os.path.join(HERE, THEME_DECLARED_FILE), "rb").read()).hexdigest()
        with open(os.path.join(HERE, REVIEW_FILE), "w", encoding="utf-8") as fh:
            json.dump(rv, fh, indent=1, sort_keys=True)
        print(json.dumps({k: rv[k] for k in ("state", "n_population", "n_exceptions", "n_pending", "by_reason",
                                              "near_binding_themes", "held_not_in_taxonomy")}, indent=1))
        return 0
    if a.live_readiness:
        print(json.dumps(live_readiness(), indent=1))
        return 0
    return _selftest()


if __name__ == "__main__":
    import sys
    sys.exit(main())
