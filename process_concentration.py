#!/usr/bin/env python3
"""
process_concentration.py — ISA-0329.  Built 19-Aug-2026.

⚑ THE FINDING. The fund sleeve is 85.1% of the ISA and its entire measured edge comes from ONE
MANAGER running ONE QUANTITATIVE PROCESS: Artemis SmartGARP, held twice (European and UK), 18.00%
of the ISA, and ISA-0329 measured 38% of the sleeve's alpha vs MSCI World coming from it. Nothing
measured that. Concentration was measured only by `concentration_clusters.py`, on the CORRELATION
OF OUTPUTS — and that module puts the two SmartGARP funds in DIFFERENT clusters, because output
correlation cannot see a shared process. Two funds can share a manager, a research team, a factor
model and a single point of failure while their monthly returns correlate at 0.6.

⚑ THE TWO READINGS ARE PUBLISHED SIDE BY SIDE AND NEVER BLENDED (R6.2). They answer different
questions:
    concentration_clusters  "how much of my RISK rides on one factor?"      — output side
    process_concentration   "how much of my MONEY rides on one PROCESS?"    — source side
This study is the evidence that they disagree about which exposure is largest, so averaging them
would destroy the only information the pair carries.

⚑ IT SETS NO LIMIT. Same discipline as `concentration_clusters` (Raj, 06-Aug-2026: "build the
measurement first and set the number against two runs of real data") and the same reason
`strategic_allocation` declares no default cap: an invented limit would make an unmeasured
concentration look governed. `process_concentration_policy.json` is read if it exists and every
process reads UNCAPPED until Raj declares one.

⚑ UNDECLARED IS NOT "OTHER" (R2.10). Eight of twelve funds have no declared process. Their weight
is reported as UNDECLARED COVERAGE, never merged into a residual bucket and never treated as
twelve separate processes — "I could not measure it" and "it is diversified" must not produce the
same output. The published concentration figures describe the DECLARED portion and say so.

ROLLBACK (R4.13): `ENABLED = False` -> build() returns DISABLED and emits nothing.

CLI:  python3 process_concentration.py [--report] [--selftest]
"""
from __future__ import annotations
import datetime as dt, json, math, os, sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

ENABLED = True
SCHEMA_VERSION = "1.0.0"
POLICY_FILE = "process_concentration_policy.json"

# A process at or above this share of the fund sleeve is one somebody should have decided.
# REPORTING threshold, not a cap: nothing here blocks a trade.
MATERIALITY_PCT_OF_SLEEVE = 15.0

# Below this declared coverage the headline concentration figures are not admissible as a reading
# of the sleeve — they describe a minority of it. Reported as INSUFFICIENT_COVERAGE, with the
# measured part still published so the number is visible and the gap is nameable.
MIN_DECLARED_COVERAGE_PCT = 60.0


class InputError(RuntimeError):
    """Raised when two artefacts cannot be validly joined. Never downgraded to a warning."""


def _today():
    return dt.date.today().isoformat()


def _policy():
    p = HERE / POLICY_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _load(name):
    """Absolute paths pass through. ⚑ The ISA folder is a mount that permits OVERWRITE but not
    DELETE, so a selftest fixture written beside the scripts can never be cleaned up — it has to
    go to a real temp directory, and that means this loader has to accept one."""
    p = Path(name)
    return json.loads((p if p.is_absolute() else HERE / name).read_text(encoding="utf-8"))


# ── the join, and its contract ────────────────────────────────────────────────────────────────
def holdings(portfolio, universe):
    """Join the portfolio's fund lines to the declared universe. R5.1 at the boundary.

    ⚑ A held fund that is absent from the universe is an ERROR, not a zero. It would silently
    shrink every denominator below and make the sleeve look more diversified than it is.
    """
    uni = universe.get("funds") or {}
    out, missing = [], []
    for f in portfolio.get("funds") or []:
        k = f.get("ticker")
        u = uni.get(k)
        if u is None:
            missing.append(k)
            continue
        out.append({
            "key": k, "name": f.get("name"), "value_gbp": f.get("value_gbp"),
            "weight_pct_of_isa": f.get("weight_pct"),
            "manager_key": u.get("manager_key"), "manager_key_basis": u.get("manager_key_basis"),
            "process_key": u.get("process_key"), "process_family": u.get("process_family"),
            "process_key_basis": u.get("process_key_basis"), "bucket": u.get("bucket"),
        })
    if missing:
        raise InputError(
            "held fund(s) %s are not in fund_universe.json. Every concentration denominator below "
            "would be computed over a sleeve that is missing them, and the answer would look "
            "exactly like a good one." % missing)
    return out


def _hhi_neff(weights):
    """Inverse Herfindahl on a weight vector, normalised to sum 1. 1 = everything in one bucket."""
    tot = sum(weights)
    if tot <= 0:
        return None
    w = [x / tot for x in weights]
    h = sum(x * x for x in w)
    return None if h <= 0 else round(1.0 / h, 3)


def _dimension(rows, field, sleeve_gbp, isa_gbp, caps, family_of=None):
    declared = [r for r in rows if r.get(field)]
    undeclared = [r for r in rows if not r.get(field)]
    groups = {}
    for r in declared:
        g = groups.setdefault(r[field], {"key": r[field], "members": [], "value_gbp": 0.0})
        g["members"].append(r["key"])
        g["value_gbp"] += float(r["value_gbp"] or 0.0)
    out = []
    for g in groups.values():
        cap = (caps or {}).get(g["key"])
        pct_sleeve = 100.0 * g["value_gbp"] / sleeve_gbp if sleeve_gbp else None
        material = pct_sleeve is not None and pct_sleeve >= MATERIALITY_PCT_OF_SLEEVE
        out.append({
            "key": g["key"], "n_funds": len(g["members"]), "members": sorted(g["members"]),
            "family": (family_of or {}).get(g["key"]),
            "value_gbp": round(g["value_gbp"], 2),
            "pct_of_isa": round(100.0 * g["value_gbp"] / isa_gbp, 2) if isa_gbp else None,
            "pct_of_fund_sleeve": round(pct_sleeve, 2) if pct_sleeve is not None else None,
            "material": material,
            "cap_pct_of_sleeve": cap,
            "cap_state": ("UNCAPPED" if cap is None else
                          ("BREACH" if pct_sleeve is not None and pct_sleeve > cap
                           else "WITHIN_CAP")),
        })
    out.sort(key=lambda r: -(r["pct_of_fund_sleeve"] or 0))
    dec_gbp = sum(float(r["value_gbp"] or 0.0) for r in declared)
    cov = 100.0 * dec_gbp / sleeve_gbp if sleeve_gbp else 0.0
    return {
        "dimension": field,
        "groups": out,
        "n_groups": len(out),
        "largest": (out[0]["key"] if out else None),
        "largest_pct_of_fund_sleeve": (out[0]["pct_of_fund_sleeve"] if out else None),
        "n_eff_declared": _hhi_neff([r["value_gbp"] for r in out]),
        "declared_coverage_pct_of_sleeve": round(cov, 2),
        "coverage_state": ("OK" if cov >= MIN_DECLARED_COVERAGE_PCT else "INSUFFICIENT_COVERAGE"),
        # ⚑ NEVER "other". These are named, and their concentration is UNKNOWN, not zero.
        "undeclared": [{"key": r["key"], "name": r["name"],
                        "pct_of_fund_sleeve": (round(100.0 * float(r["value_gbp"]) / sleeve_gbp, 2)
                                               if sleeve_gbp else None)} for r in undeclared],
        "undeclared_pct_of_sleeve": round(100.0 - cov, 2),
        "material_groups": [r["key"] for r in out if r["material"]],
        "breaches": [r["key"] for r in out if r["cap_state"] == "BREACH"],
        "n_eff_caveat": ("computed over the DECLARED portion only (%.1f%% of the sleeve). It is an "
                         "UPPER bound on process diversification if the undeclared funds share a "
                         "process with a declared one, and a lower bound if they are all distinct "
                         "— which of those is true is exactly what is undeclared." % cov),
    }


def output_side_reading():
    """The OTHER reading, quoted not recomputed. R6.2 — published beside, never blended."""
    try:
        c = _load("concentration_aug_2026.json")
    except Exception as e:                                        # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": "%s: %s" % (type(e).__name__, e)}
    eb = c.get("effective_bets") or {}
    cl = c.get("clusters") or []
    return {
        "state": "QUOTED",
        "source": "concentration_clusters.py -> concentration_aug_2026.json",
        "as_of": c.get("run_date"),
        "n_eff_by_cluster_weight": eb.get("by_cluster_weight"),
        "n_eff_by_risk_principal_portfolios": eb.get("by_risk_principal_portfolios"),
        "pc1_share_of_variance_pct": eb.get("pc1_share_of_variance_pct"),
        "largest_cluster_pct_of_isa": c.get("largest_cluster_pct_of_isa"),
        "clusters_containing_smartgarp": [sorted(k.get("members") or []) for k in cl
                                          if any(m in ("B2PLJD7", "B2PLJM6")
                                                 for m in (k.get("members") or []))],
    }


def build(out_path=None, as_of=None, portfolio_file="portfolio_data_aug_2026.json",
          universe_file="fund_universe.json"):
    if not ENABLED:
        return {"state": "DISABLED", "reason": "process_concentration.ENABLED is False (R4.13)"}
    as_of = as_of or _today()
    pf, uni, pol = _load(portfolio_file), _load(universe_file), _policy()
    rows = holdings(pf, uni)
    summ = pf.get("summary") or {}
    sleeve = float(summ.get("fund_sleeve_value_gbp") or 0.0)
    isa = float(summ.get("total_value_gbp") or 0.0)

    # R5.2 — two independent derivations of the same quantity must agree.
    joined = sum(float(r["value_gbp"] or 0.0) for r in rows)
    if sleeve and abs(joined - sleeve) > max(1.0, 0.005 * sleeve):
        raise InputError(
            "the joined fund lines total GBP %.2f but the portfolio summary states a fund sleeve of "
            "GBP %.2f. Every share below would be computed against a denominator that does not "
            "describe the rows in the numerator." % (joined, sleeve))

    caps = pol.get("caps") or {}
    fam = {r["process_key"]: r.get("process_family") for r in rows if r.get("process_key")}
    dims = {
        "process": _dimension(rows, "process_key", sleeve, isa, caps.get("process"), fam),
        "process_family": _dimension(rows, "process_family", sleeve, isa,
                                     caps.get("process_family")),
        "manager": _dimension(rows, "manager_key", sleeve, isa, caps.get("manager")),
    }
    out_side = output_side_reading()
    src = dims["process"]
    doc = {
        "_meta": {"module": "process_concentration.py", "schema_version": SCHEMA_VERSION,
                  "as_of": as_of, "built": "2026-08-19", "enabled": ENABLED,
                  "closes": ["ISA-0329"]},
        "inputs": {"portfolio": portfolio_file, "portfolio_as_of": (pf.get("_meta") or {}).get("data_date"),
                   "universe": universe_file, "universe_verified_at": uni.get("verified_at"),
                   "fund_sleeve_gbp": round(sleeve, 2), "isa_total_gbp": round(isa, 2),
                   "n_funds": len(rows)},
        "source_side": dims,
        "output_side": out_side,
        "sets_no_limit": ("this module MEASURES. It sets no cap and blocks nothing. A limit on "
                          "process is Raj's to declare in process_concentration_policy.json, and "
                          "until he does every process reads UNCAPPED rather than carrying an "
                          "invented number that would make it look governed."),
        "policy_present": bool(pol),
        "readings_disagree": None,
        "state": "OK",
    }
    # ⚑ THE POINT OF THE STUDY, computed rather than asserted in prose.
    smart = [g for g in src["groups"] if g["key"] == "artemis_smartgarp"]
    split = [c for c in (out_side.get("clusters_containing_smartgarp") or []) if c]
    doc["readings_disagree"] = {
        "claim": ("the source-side and output-side readings do not see the same largest exposure, "
                  "which is why R6.2 forbids blending them"),
        "largest_process_pct_of_sleeve": src.get("largest_pct_of_fund_sleeve"),
        "largest_process": src.get("largest"),
        "smartgarp_pct_of_isa": (smart[0]["pct_of_isa"] if smart else None),
        "smartgarp_pct_of_fund_sleeve": (smart[0]["pct_of_fund_sleeve"] if smart else None),
        "smartgarp_in_n_output_clusters": len(split),
        "verdict": ("SPLIT — the two funds that share a manager, a research team and one "
                    "quantitative process sit in %d DIFFERENT output-correlation clusters, so the "
                    "output-side reading cannot see this exposure at all" % len(split))
                   if len(split) > 1 else
                   ("CO-LOCATED — the output-side reading happens to group them this month; that is "
                    "a fact about returns, not about the process, and it can reverse next month"),
    }
    out = Path(out_path or HERE / f"process_concentration_{as_of[:7].replace('-', '_')}.json")
    out.write_text(json.dumps(doc, indent=2))
    doc["_written"] = str(out)
    return doc


def report(doc=None) -> str:
    d = doc or build()
    if d.get("state") != "OK":
        return "process_concentration: %s" % d.get("state")
    L = ["PROCESS & MANAGER CONCENTRATION (ISA-0329) — source side",
         "fund sleeve GBP %.0f of GBP %.0f ISA" % (d["inputs"]["fund_sleeve_gbp"],
                                                   d["inputs"]["isa_total_gbp"]), ""]
    for dim in ("process", "manager"):
        s = d["source_side"][dim]
        L.append("%-14s coverage %.1f%% of sleeve [%s]   N_eff(declared) %s"
                 % (dim.upper(), s["declared_coverage_pct_of_sleeve"], s["coverage_state"],
                    s["n_eff_declared"]))
        for g in s["groups"]:
            L.append("    %-32s %2d fund(s) %6.2f%% of sleeve %6.2f%% of ISA  [%s]"
                     % (g["key"][:32], g["n_funds"], g["pct_of_fund_sleeve"], g["pct_of_isa"],
                        g["cap_state"] + (" · MATERIAL" if g["material"] else "")))
        if s["undeclared"]:
            L.append("    UNDECLARED (named, never merged into 'other'): %s  = %.2f%% of sleeve"
                     % (", ".join(u["key"] for u in s["undeclared"]), s["undeclared_pct_of_sleeve"]))
        L.append("")
    o = d["output_side"]
    L += ["OUTPUT SIDE (quoted from concentration_clusters, %s) — published beside, never blended"
          % o.get("as_of"),
          "    N_eff by cluster weight %s | by risk (Meucci) %s | PC1 %.1f%% of variance"
          % (o.get("n_eff_by_cluster_weight"), o.get("n_eff_by_risk_principal_portfolios"),
             o.get("pc1_share_of_variance_pct") or 0.0), ""]
    r = d["readings_disagree"]
    L += ["  * %s" % r["verdict"],
          "  * SmartGARP is %.2f%% of the ISA and %.2f%% of the fund sleeve on ONE process and ONE "
          "manager." % (r["smartgarp_pct_of_isa"] or 0, r["smartgarp_pct_of_fund_sleeve"] or 0)]
    return "\n".join(L)


def selftest(verbose=True) -> int:
    import copy, tempfile
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)
        if verbose:
            print(("  ok   " if c else "  FAIL ") + n)

    d = build(out_path=tempfile.mktemp(suffix=".json"))
    ck("build returns OK", d["state"] == "OK")
    ck("all twelve held funds joined to the declared universe", d["inputs"]["n_funds"] == 12)

    p = d["source_side"]["process"]
    m = d["source_side"]["manager"]
    ck("ISA-0329 REPRODUCED: the largest declared process is Artemis SmartGARP",
       p["largest"] == "artemis_smartgarp")
    ck("... held TWICE, so it is a process exposure and not a fund position",
       p["groups"][0]["n_funds"] == 2 and sorted(p["groups"][0]["members"]) == ["B2PLJD7", "B2PLJM6"])
    ck("... and it is MATERIAL against the declared reporting threshold",
       p["groups"][0]["material"] is True)
    ck("the manager dimension covers the WHOLE sleeve — a manager is readable from the legal name "
       "and a process is not, so the two coverages must differ",
       m["declared_coverage_pct_of_sleeve"] == 100.0
       and p["declared_coverage_pct_of_sleeve"] < 100.0)
    ck("R2.10: the eight funds with no declared process are NAMED, not merged into an 'other' "
       "bucket that would read as diversification",
       len(p["undeclared"]) == 8 and all(u["key"] and u["pct_of_fund_sleeve"] for u in p["undeclared"]))
    ck("the N_eff figure carries its own coverage caveat rather than standing alone",
       "DECLARED portion only" in p["n_eff_caveat"])
    ck("with no policy on disk every process reads UNCAPPED — never an invented limit",
       all(g["cap_pct_of_sleeve"] is None and g["cap_state"] == "UNCAPPED" for g in p["groups"]))

    # ⚑ THE STUDY'S OWN CLAIM, ASSERTED
    rd = d["readings_disagree"]
    ck("THE DISAGREEMENT IS REAL: the two SmartGARP funds sit in DIFFERENT output-correlation "
       "clusters, so the output-side reading cannot see this exposure",
       rd["smartgarp_in_n_output_clusters"] > 1 and rd["verdict"].startswith("SPLIT"))
    ck("both readings are published, and neither is blended into the other",
       d["output_side"]["state"] == "QUOTED"
       and d["source_side"]["process"]["n_eff_declared"] is not None)

    # ── negative controls ─────────────────────────────────────────────────────────────────────
    pf = _load("portfolio_data_aug_2026.json")
    uni = _load("fund_universe.json")

    bad = copy.deepcopy(uni)
    bad["funds"].pop("SMT")
    try:
        holdings(pf, bad)
        ck("NEGATIVE CONTROL: a held fund missing from the universe RAISES (it would silently "
           "shrink every denominator)", False)
    except InputError:
        ck("NEGATIVE CONTROL: a held fund missing from the universe RAISES (it would silently "
           "shrink every denominator)", True)

    one = copy.deepcopy(uni)
    for k in one["funds"]:
        one["funds"][k]["process_key"] = "SINGLE"
    tmp = Path(tempfile.mkdtemp()) / "_pc_test_universe.json"
    tmp.write_text(json.dumps(one))
    try:
        z = build(out_path=tempfile.mktemp(suffix=".json"), universe_file=str(tmp))
        ck("NEGATIVE CONTROL: a sleeve on ONE process reads N_eff 1.0 and 100% coverage",
           z["source_side"]["process"]["n_eff_declared"] == 1.0
           and z["source_side"]["process"]["declared_coverage_pct_of_sleeve"] == 100.0)
        none_ = copy.deepcopy(uni)
        for k in none_["funds"]:
            none_["funds"][k]["process_key"] = None
        tmp.write_text(json.dumps(none_))
        z2 = build(out_path=tempfile.mktemp(suffix=".json"), universe_file=str(tmp))
        ck("NEGATIVE CONTROL: a sleeve with NO declared process reports INSUFFICIENT_COVERAGE and "
           "N_eff None — it does not report perfect diversification across twelve unknowns",
           z2["source_side"]["process"]["coverage_state"] == "INSUFFICIENT_COVERAGE"
           and z2["source_side"]["process"]["n_eff_declared"] is None)
        cap = copy.deepcopy(uni)
        tmp.write_text(json.dumps(cap))
        # ⚑ the cap control passes the policy in DIRECTLY rather than writing one to disk: a
        # policy file dropped beside the scripts could not be removed afterwards on this mount,
        # and a leftover selftest fixture that silently becomes the live policy is a worse defect
        # than the one being controlled for.
        rows_ = holdings(_load("portfolio_data_aug_2026.json"), _load("fund_universe.json"))
        z3 = _dimension(rows_, "process_key",
                        float((_load("portfolio_data_aug_2026.json")["summary"]
                               ["fund_sleeve_value_gbp"])),
                        float((_load("portfolio_data_aug_2026.json")["summary"]
                               ["total_value_gbp"])),
                        {"artemis_smartgarp": 10.0})
        ck("NEGATIVE CONTROL: a declared 10% cap turns the SmartGARP exposure into a BREACH — the "
           "cap machinery works, it is simply unpopulated",
           "artemis_smartgarp" in z3["breaches"])
    finally:
        tmp.unlink(missing_ok=True)
        try:
            tmp.parent.rmdir()
        except OSError:
            pass

    global ENABLED
    ENABLED = False
    ck("rollback constant disables the module", build()["state"] == "DISABLED")
    ENABLED = True

    print("\nprocess_concentration selftest: %d failure(s)%s"
          % (len(fails), (" -> " + ", ".join(fails)) if fails else " — 16 assertions green"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(report() if "--report" in sys.argv else json.dumps(build(), indent=2))
