#!/usr/bin/env python3
"""
factor_lookthrough.py — Doc B B3: factor look-through concentration metric + soft cap (D15).

ai_complex_effective_weight (% of total ISA) =
    sum(direct stock value x ai_complex class) + sum(fund value x fund_ai_share)
Classification lives in factor_map.json (stocks 0/0.5/1; funds = share estimates — floor,
coverage honesty).

⚑ B08-R02 (October build P-B6, 19-Oct-2026). That sum is a LOWER BOUND, and a JUDGED one: the fund
shares are seeded estimates from top-10 holdings, and an unclassified or unpriced name contributes
nothing to it. A lower bound below a cap does NOT establish the whole book is under the cap — before
P-B6 the line read "OK" with £2,001 of direct stocks unclassified and ~60% of every fund unseen.
So every run now publishes:
    lower_bound_pct   the sum above (unchanged; `ai_complex_effective_weight_pct` keeps this meaning
                      for every existing reader)
    upper_bound_pct   as if every unclassified name and every UNSEEN part of each fund were in the
                      factor (unseen = 1 - the declared top-holdings coverage in
                      fund_holdings_declared.json; a fund with no declaration is wholly unseen)
    verdict           BREACH (lower > cap) | OK (upper <= cap) | NOT_ESTABLISHED (between) |
                      UNAVAILABLE (no total, or an unpriced holding leaves the upper bound unknown)
`breach` keeps its meaning (lower bound > cap). The same for the semis line (`watch_verdict`).

⚑ ENFORCEMENT, STATED (P-B6 finding; WIRED by ISA-0621 / P-B9, 19-Oct-2026). The block is evaluated per plan on
the NET post-trade book inside capital_decision_engine (factor_eval: a plan fails only if its post-trade LOWER bound
exceeds the cap AND it raised the factor's GBP - Raj 19-Oct: a like-for-like AI-capex switch is never blocked), and
Checkpoint-D tick 5 reads that verdict from the receipt. It BINDS only when concentration_control's
concentration_gate is LIVE (one LIVE decision for the concentration family after 1-Nov); until then every line says
SHADOW and binds nothing.

⚑ ONE CLASSIFICATION HOME FOR DIRECT STOCKS (P-B7, Raj 19-Oct-2026 16:02: "COCO is clearly a drinks manufacturer and
QBTS is a quantum computing company ... why are they shown as unclassified?"). factor_map.json is a hand-kept list;
a holding nobody added stayed "unclassified" for months while the evidence-backed theme taxonomy had classified it
(16-Sep). A direct stock with no factor_map entry now takes its class from the ONE theme membership
(concentration_control.theme_memberships: declared taxonomy + conservative inclusions + auto-classification):
member of an AI-complex theme (AI_FACTOR_THEMES) -> 1.0 (conservative: a LOW-confidence membership counts in full);
any other theme or NO_MATERIAL_THEME -> 0.0; no membership -> unclassified (upper bound) as before. Where factor_map
and the taxonomy disagree on a held name, `classification_disagreements` names it - never resolved silently.
factor_map.json keeps what only it supplies: each FUND's estimated AI/semis share.

Usage: python3 factor_lookthrough.py --portfolio portfolio_data_jul_2026.json
       python3 factor_lookthrough.py --selftest
"""
from __future__ import annotations
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import scoring_config as _cfg
except Exception:
    _cfg = None

MAP_PATH = os.path.join(HERE, "factor_map.json")
DECLARED_PATH = os.path.join(HERE, "fund_holdings_declared.json")
ENFORCEMENT = "NET_BOOK_FOLLOWS_CONCENTRATION_GATE"
# themes whose members are 'core AI complex' in factor_map's own definition (semis / AI infra / hyperscale AI)
AI_FACTOR_THEMES = ("AI_DATACENTER_CAPEX", "SEMICONDUCTOR_EQUIPMENT_CAPEX", "MEMORY_STORAGE_PRICING")
ENFORCEMENT_NOTE = "net-book block wired (engine + Checkpoint-D tick 5), SHADOW: binds nothing until the caps go LIVE"


def _enforcement_note():
    """The truthful enforcement sentence for THIS run (ISA-0621): the block's mode is the concentration_gate's."""
    try:
        import concentration_control as _cc
        m = _cc.mode()
    except Exception:                                                     # noqa: BLE001
        m = "UNKNOWN"
    if m == "LIVE":
        return "net-book block wired (engine + Checkpoint-D tick 5), LIVE: a factor-raising plan above the cap is BLOCKED"
    return "net-book block wired (engine + Checkpoint-D tick 5), %s: binds nothing until the caps go LIVE" % m


def _cap():
    return float(getattr(_cfg, "FACTOR_AI_SOFT_CAP_PCT", 30.0)) if _cfg else 30.0


def load_map(path=MAP_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _declared_coverage(path=None):
    """{fund ticker: fraction of the fund whose holdings are declared} — absent = 0 (wholly unseen)."""
    try:
        with open(path or DECLARED_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return {k: max(0.0, min(1.0, float((v or {}).get("coverage_pct") or 0.0) / 100.0))
                for k, v in (d.get("funds") or {}).items()}
    except Exception:                                                     # noqa: BLE001
        return {}


def _theme_layer(themes=None):
    """The ONE theme membership (concentration_control.theme_memberships); {} when unreadable (old behaviour)."""
    if themes is not None:
        return themes
    try:
        import concentration_control as _cc
        return _cc.theme_memberships()
    except Exception:                                                     # noqa: BLE001
        return {}


def _theme_class(ticker, tm):
    """-> (class, themes) from the theme membership, or (None, None) when the name has no membership."""
    th = (tm or {}).get("theme") or {}
    al = (tm or {}).get("aliases") or {}
    t = str(ticker or "").strip().upper()
    ths = th.get(al.get(t, t), th.get(t))
    if ths is None:
        return None, None
    return (1.0 if any(T in AI_FACTOR_THEMES for T in ths) else 0.0), list(ths)


def _val(h):
    v = h.get("value_gbp")
    return v if isinstance(v, (int, float)) else None


def _verdict(lo_pct, hi_pct, cap, unpriced):
    if lo_pct is None:
        return "UNAVAILABLE"
    if lo_pct > cap:
        return "BREACH"
    if unpriced or hi_pct is None:
        return "UNAVAILABLE"
    return "OK" if hi_pct <= cap else "NOT_ESTABLISHED"


def compute(portfolio, fmap, declared=None, themes=None):
    """portfolio = portfolio_data dict; fmap = factor_map dict. Pure (U-B3) apart from reading the
    declared-coverage store when `declared` is not supplied."""
    total = (portfolio.get("summary") or {}).get("total_value_gbp") or 0.0
    stocks_map = fmap.get("stocks", {})
    funds_map = fmap.get("funds", {})
    cov = declared if declared is not None else _declared_coverage()
    tm = _theme_layer(themes)
    contrib, unclassified, unpriced, disagree, from_tax = [], [], [], [], []
    eff = upper_extra = unclassified_gbp = 0.0
    for h in portfolio.get("stocks", []):
        t, v = h.get("ticker"), _val(h)
        if v is None:
            unpriced.append(t)
            continue
        c = stocks_map.get(t)
        tc, tth = _theme_class(t, tm)
        if c is not None and tc is not None and (float(c) > 0) != (tc > 0):
            disagree.append({"ticker": t, "factor_map_class": c, "taxonomy_themes": tth})
        if c is None and tc is not None:
            c = tc
            from_tax.append({"ticker": t, "class": tc, "themes": tth})
        if c is None:
            unclassified.append(t)
            unclassified_gbp += v
            upper_extra += v
            continue
        if c > 0:
            eff += v * c
            contrib.append({"ticker": t, "kind": "stock", "class": c,
                            "basis": ("THEME_TAXONOMY" if any(x["ticker"] == t for x in from_tax) else "FACTOR_MAP"),
                            "value_gbp": round(v, 2), "effective_gbp": round(v * c, 2)})
    for h in portfolio.get("funds", []):
        t, v = h.get("ticker"), _val(h)
        if v is None:
            unpriced.append(t)
            continue
        fm = funds_map.get(t)
        if fm is None:
            unclassified.append(t)
            unclassified_gbp += v
            upper_extra += v
            continue
        share = float(fm.get("fund_ai_share") or 0.0)
        seen = cov.get(t, 0.0)
        upper_extra += v * (min(1.0, share + (1.0 - seen)) - share)
        if share > 0:
            eff += v * share
            contrib.append({"ticker": t, "kind": "fund", "class": share,
                            "value_gbp": round(v, 2), "effective_gbp": round(v * share, 2)})
    pct = round(eff / total * 100.0, 1) if total > 0 else None
    hi = round((eff + upper_extra) / total * 100.0, 1) if (total > 0 and not unpriced) else None
    cap = _cap()
    ENFORCEMENT_NOTE = _enforcement_note()                                # noqa: N806 (ISA-0621: this run's mode)
    verdict = _verdict(pct, hi, cap, unpriced)
    uncl = sorted(set(unclassified))
    tail = ("unclassified £{:,.0f}: {}; ".format(unclassified_gbp, ", ".join(uncl)) if uncl else "")
    if pct is None:
        line = "AI-complex weight: n/a (no portfolio total)"
    elif verdict == "BREACH":
        line = (f"AI-complex effective weight: >={pct}% (judgement floor) vs {cap:.0f}% cap — BREACH "
                f"({ENFORCEMENT_NOTE})")
    elif verdict == "OK":
        line = (f"AI-complex effective weight: {pct}%-{hi}% vs {cap:.0f}% cap — OK even at the upper bound "
                f"({ENFORCEMENT_NOTE})")
    elif verdict == "UNAVAILABLE":
        line = (f"AI-complex effective weight: >={pct}% (judgement floor) vs {cap:.0f}% cap — UNAVAILABLE: "
                f"unpriced holding(s) {', '.join(map(str, unpriced))} leave the upper bound unknown")
    else:
        line = (f"AI-complex effective weight: >={pct}% (judgement floor) vs {cap:.0f}% cap — NOT ESTABLISHED: "
                f"up to {hi}% if the unseen fund holdings and unclassified names were all AI-complex "
                f"({tail}fund shares are seeded estimates from top holdings). {ENFORCEMENT_NOTE}")
    return {
        "ai_complex_effective_weight_pct": pct,
        "lower_bound_pct": pct,
        "upper_bound_pct": hi,
        "lower_bound_gbp": round(eff, 2),
        "upper_bound_gbp": (round(eff + upper_extra, 2) if hi is not None else None),
        "verdict": verdict,
        "verdict_note": line,
        "enforcement": ENFORCEMENT,
        "cap_pct": cap,
        "breach": bool(pct is not None and pct > cap),
        "effective_gbp": round(eff, 2),
        "total_gbp": round(total, 2),
        "contributors": sorted(contrib, key=lambda c: -c["effective_gbp"]),
        "unclassified": uncl,
        "unclassified_gbp": round(unclassified_gbp, 2),
        "unpriced": sorted(set(map(str, unpriced))),
        "classified_from_taxonomy": from_tax,
        "classification_disagreements": disagree,
        "coverage_note": ("fund shares are FLOOR estimates from top-10 holdings (~40-60% of "
                          "assets) + seeded judgment — see factor_map.json _meta; the upper bound "
                          "treats every unseen pound as in the factor (B08-R02)"),
        "semis": compute_semis_lookthrough(portfolio, fmap, declared=cov, themes=tm),
        "email_line": line,
    }



def compute_semis_lookthrough(portfolio, fmap, declared=None, themes=None):
    """H-8 (audit item #8, 26-Jul-26) - semis-complex look-through: funds' semi exposure
    (factor_map funds[t].fund_semis_share - JUDGMENT SEEDS, parsed xray sector data is
    portfolio-level only; refine like fund_ai_share) + direct holdings in
    scoring_config.SEMIS_TICKERS. Report-only; WATCH marker at >= SEMIS_WATCH_PCT.
    Missing share -> fund excluded + '(partial)' marker; never raises.
    B08-R02 (P-B6): the same lower/upper bound + `watch_verdict` as the AI line; a direct stock with
    no factor_map entry and a fund with no semis share count in the upper bound, never as zero."""
    try:
        import scoring_config as _sc
        semis = tuple(getattr(_sc, "SEMIS_TICKERS",
                              ("MU", "AVGO", "NVDA", "AMD", "TSM", "ASML", "SMCI", "ANET", "MRVL")))
        watch = float(getattr(_sc, "SEMIS_WATCH_PCT", 18.0))
    except Exception:
        semis, watch = ("MU", "AVGO", "NVDA", "AMD", "TSM", "ASML", "SMCI", "ANET", "MRVL"), 18.0
    total = ((portfolio.get("summary") or {}).get("total_value_gbp")) or 0.0
    funds_map = (fmap or {}).get("funds", {})
    stocks_map = (fmap or {}).get("stocks", {})
    cov = declared if declared is not None else _declared_coverage()
    tm = _theme_layer(themes)
    funds_eff = direct_eff = upper_extra = unclassified_gbp = 0.0
    partial, unpriced = [], []
    for h in portfolio.get("stocks", []):
        v = _val(h)
        if v is None:
            unpriced.append(h.get("ticker"))
            continue
        if str(h.get("ticker", "")).upper() in semis:
            direct_eff += v
        elif stocks_map and h.get("ticker") not in stocks_map and _theme_class(h.get("ticker"), tm)[0] is None:
            upper_extra += v
            unclassified_gbp += v
    for h in portfolio.get("funds", []):
        v = _val(h)
        if v is None:
            unpriced.append(h.get("ticker"))
            continue
        share = (funds_map.get(h.get("ticker")) or {}).get("fund_semis_share")
        if share is None:
            partial.append(h.get("ticker"))
            upper_extra += v
            unclassified_gbp += v
            continue
        funds_eff += v * float(share)
        upper_extra += v * (min(1.0, float(share) + (1.0 - cov.get(h.get("ticker"), 0.0))) - float(share))
    if total <= 0:
        return {"semis_complex_pct": None, "watch_verdict": "UNAVAILABLE",
                "line": "Semis-complex look-through: n/a (no portfolio total)"}
    f_pct = round(funds_eff / total * 100.0, 1)
    d_pct = round(direct_eff / total * 100.0, 1)
    t_pct = round((funds_eff + direct_eff) / total * 100.0, 1)
    hi = None if unpriced else round((funds_eff + direct_eff + upper_extra) / total * 100.0, 1)
    if t_pct >= watch:
        wv = "WATCH"
    elif hi is None:
        wv = "UNAVAILABLE"
    else:
        wv = "CLEAR" if hi < watch else "NOT_ESTABLISHED"
    marker = (" - ** WATCH (>=%g%%) ** report-only" % watch) if t_pct >= watch else (
        " - WATCH >=%g%%, report-only" % watch)
    suffix = (" (partial: %s unclassified)" % ",".join(sorted(set(partial)))) if partial else ""
    bound = ("" if wv in ("WATCH", "CLEAR") else
             (" - %s: up to %s%% if unseen fund holdings were all semis" % (wv.replace("_", " "), hi)
              if hi is not None else " - upper bound UNAVAILABLE (unpriced holdings)"))
    return {"semis_complex_pct": t_pct, "funds_pct": f_pct, "direct_pct": d_pct,
            "lower_bound_pct": t_pct, "upper_bound_pct": hi,
            "lower_bound_gbp": round(funds_eff + direct_eff, 2),
            "upper_bound_gbp": (round(funds_eff + direct_eff + upper_extra, 2) if hi is not None else None),
            "unclassified_gbp": round(unclassified_gbp, 2),
            "watch": t_pct >= watch, "watch_verdict": wv, "verdict": wv, "partial": sorted(set(partial)),
            "verdict_note": "semis lower bound %.1f%% (judgement floor), upper %s%%" % (t_pct, hi),
            "line": ("Semis-complex look-through: >=%.1f%% (funds %.1f%% + direct %.1f%%; judgement floor)%s%s%s"
                     % (t_pct, f_pct, d_pct, marker, suffix, bound))}


def _selftest():
    fails = []

    def ok(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)
    fmap = {"stocks": {"AAA": 1.0, "BBB": 0.5, "CCC": 0.0}, "funds": {"FND": {"fund_ai_share": 0.4}}}
    port = {"summary": {"total_value_gbp": 1000.0},
            "stocks": [{"ticker": "AAA", "value_gbp": 100.0}, {"ticker": "BBB", "value_gbp": 100.0},
                       {"ticker": "CCC", "value_gbp": 100.0}, {"ticker": "NEW", "value_gbp": 50.0}],
            "funds": [{"ticker": "FND", "value_gbp": 500.0}]}
    r = compute(port, fmap, declared={"FND": 1.0}, themes={})
    # 100*1 + 100*0.5 + 500*0.4 = 350 -> 35% -> breach at 30
    ok("U-B3 lower bound 35% and BREACH at 30", r["ai_complex_effective_weight_pct"] == 35.0 and r["breach"] is True
       and r["verdict"] == "BREACH" and r["unclassified"] == ["NEW"])
    port["funds"][0]["value_gbp"] = 100.0
    r2 = compute(port, fmap, declared={"FND": 1.0}, themes={})   # 100+50+40=190 /1000 = 19%; upper +NEW 50 = 24% -> OK
    ok("U-B3 below the cap at the UPPER bound (fully declared fund) is OK", r2["breach"] is False
       and r2["verdict"] == "OK" and r2["upper_bound_pct"] == 24.0)
    # ⚑ B08-R02 MUST-FIRE — the audit fixture: £10,000 wholly unclassified must not read OK
    p3 = {"summary": {"total_value_gbp": 100000.0},
          "stocks": [{"ticker": "AAA", "value_gbp": 20000.0}, {"ticker": "ZZZ", "value_gbp": 10000.0}], "funds": []}
    r3 = compute(p3, fmap, declared={}, themes={})
    ok("B08-R02 boundary: 20% measured + £10,000 unclassified = 30% even if it were all AI -> OK (upper <= cap)",
       r3["verdict"] == "OK" and r3["upper_bound_pct"] == 30.0 and r3["unclassified_gbp"] == 10000.0)
    p3b = {"summary": {"total_value_gbp": 100000.0},
           "stocks": [{"ticker": "AAA", "value_gbp": 25000.0}, {"ticker": "ZZZ", "value_gbp": 10000.0}], "funds": []}
    r3b = compute(p3b, fmap, declared={}, themes={})
    ok("B08-R02 MUST-FIRE: 25% measured + £10,000 unclassified -> NOT_ESTABLISHED (upper 35%) and the line says so",
       r3b["verdict"] == "NOT_ESTABLISHED" and r3b["upper_bound_pct"] == 35.0 and "NOT ESTABLISHED" in r3b["email_line"]
       and "OK" not in r3b["email_line"].split("—")[1][:6])
    p4 = {"summary": {"total_value_gbp": 100000.0}, "stocks": [{"ticker": "AAA", "value_gbp": 20000.0}],
          "funds": [{"ticker": "FND", "value_gbp": 15000.0}]}
    r4 = compute(p4, fmap, declared={}, themes={})
    ok("B08-R02: an UNSEEN fund (no declared holdings) counts wholly in the upper bound (26% floor, 35% bound)",
       r4["lower_bound_pct"] == 26.0 and r4["upper_bound_pct"] == 35.0 and r4["verdict"] == "NOT_ESTABLISHED")
    r4b = compute(p4, fmap, declared={"FND": 1.0}, themes={})
    ok("B08-R02 NEGATIVE CONTROL: the same fund FULLY declared adds nothing beyond its share -> OK at 26%",
       r4b["upper_bound_pct"] == 26.0 and r4b["verdict"] == "OK")
    p5 = {"summary": {"total_value_gbp": 1000.0}, "stocks": [{"ticker": "AAA", "value_gbp": None}], "funds": []}
    r5 = compute(p5, fmap, declared={}, themes={})
    ok("NEGATIVE CONTROL: an unpriced holding leaves the upper bound UNKNOWN - UNAVAILABLE, never OK",
       r5["verdict"] == "UNAVAILABLE" and r5["upper_bound_pct"] is None and r5["unpriced"] == ["AAA"])
    ok("the line states the enforcement truthfully (ISA-0621: wired, mode stated)", "net-book block wired" in r3b["email_line"])
    # semis
    fm2 = {"stocks": {"MU": 1.0, "ONT": 0.0}, "funds": {"FA": {"fund_semis_share": 0.2}, "FC": {}}}
    s1 = compute_semis_lookthrough({"summary": {"total_value_gbp": 100000.0},
                                    "stocks": [{"ticker": "MU", "value_gbp": 5000.0}, {"ticker": "XNEW", "value_gbp": 3000.0}],
                                    "funds": [{"ticker": "FA", "value_gbp": 20000.0}]}, fm2, declared={"FA": 1.0}, themes={})
    ok("B08-R02 semis: a direct stock with no factor_map entry is in the upper bound, not zero",
       s1["semis_complex_pct"] == 9.0 and s1["upper_bound_pct"] == 12.0 and s1["watch_verdict"] == "CLEAR")
    s2 = compute_semis_lookthrough({"summary": {"total_value_gbp": 100000.0}, "stocks": [],
                                    "funds": [{"ticker": "FC", "value_gbp": 20000.0}]}, fm2, declared={}, themes={})
    ok("B08-R02 semis: a fund with no semis share -> NOT_ESTABLISHED vs the 18% watch",
       s2["watch_verdict"] == "NOT_ESTABLISHED" and s2["partial"] == ["FC"] and "NOT ESTABLISHED" in s2["line"])
    # ⚑ P-B7 (Raj 16:02): the taxonomy classifies what factor_map does not list
    tmx = {"theme": {"COCO": [], "QBTS": ["QUANTUM_COMPUTING"], "NEWAI": ["AI_DATACENTER_CAPEX"], "AAA": ["SHIPPING_RATE_CYCLE"]},
           "aliases": {}}
    p6 = {"summary": {"total_value_gbp": 100000.0},
          "stocks": [{"ticker": "COCO", "value_gbp": 1200.0}, {"ticker": "QBTS", "value_gbp": 800.0},
                     {"ticker": "NEWAI", "value_gbp": 5000.0}, {"ticker": "AAA", "value_gbp": 1000.0},
                     {"ticker": "ZZZ", "value_gbp": 500.0}], "funds": []}
    r6 = compute(p6, fmap, declared={}, themes=tmx)
    ok("MUST-FIRE P-B7: COCO (no theme) and QBTS (quantum) are CLASSIFIED 0 from the taxonomy, not unclassified",
       r6["unclassified"] == ["ZZZ"] and r6["unclassified_gbp"] == 500.0
       and {x["ticker"] for x in r6["classified_from_taxonomy"]} >= {"COCO", "QBTS"})
    ok("MUST-FIRE P-B7: a name in an AI-complex theme and absent from factor_map counts 1.0 (6% = AAA 1% + NEWAI 5%)",
       r6["lower_bound_pct"] == 6.0 and any(c["ticker"] == "NEWAI" and c["basis"] == "THEME_TAXONOMY" for c in r6["contributors"]))
    ok("MUST-FIRE P-B7: factor_map 1.0 vs taxonomy non-AI theme is NAMED as a disagreement (factor_map value kept)",
       [d["ticker"] for d in r6["classification_disagreements"]] == ["AAA"])
    r7 = compute(p6, fmap, declared={}, themes={})
    ok("NEGATIVE CONTROL P-B7: with no theme membership available, COCO/QBTS/NEWAI stay unclassified (old behaviour)",
       set(r7["unclassified"]) == {"COCO", "QBTS", "NEWAI", "ZZZ"} and not r7["classification_disagreements"])
    print("factor_lookthrough selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=None)
    ap.add_argument("--map", default=MAP_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        n = _selftest()
        if not n:
            print("factor_lookthrough SELF-TEST OK (U-B3)")
        sys.exit(1 if n else 0)
    else:
        with open(a.portfolio, encoding="utf-8") as f:
            port = json.load(f)
        print(json.dumps(compute(port, load_map(a.map)), indent=2))
