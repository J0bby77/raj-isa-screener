#!/usr/bin/env python3
"""
screener_local.py - LOCAL, resumable, batched growth-stock screener runner.
PRIMARY path for ISA Growth Stock tasks: wraps screener_core.py and drives it in
<=45s resumable batches on local bash. Composio/GitHub = FALLBACK only.
Guardrails: preflight (yfinance import + /dev/shm headroom + Yahoo reach) ->
FALLBACK_TO_COMPOSIO exit 3; constituent fetch failure/empty -> fallback exit 3;
writes only to --outputs (mount) + /dev/shm, never /; single overwrite-only resume
cache shrunk to stub; canonical screener_core savers for schema parity.
Composio constituent hybrid-fallback: Composio runs --dump-constituents PATH, local
runs --constituents PATH (so metrics+scoring stay local even when the constituent
source is IP-blocked e.g. iShares MidCap400/SPI or slow e.g. STOXX600/FTSE250).
Call repeatedly until ALL_DONE.
"""
import argparse, json, os, sys, math, time, importlib.util, datetime, urllib.request


def preflight(shm, min_shm_mb=80):
    reasons = []
    try:
        import yfinance  # noqa: F401
    except Exception as e:
        reasons.append(f"yfinance import failed ({e})")
    try:
        d = shm if os.path.isdir(shm) else "/dev/shm"
        st = os.statvfs(d)
        free_mb = st.f_bavail * st.f_frsize / 1e6
        if free_mb < min_shm_mb:
            reasons.append(f"/dev/shm low ({free_mb:.0f}MB < {min_shm_mb}MB)")
    except Exception as e:
        reasons.append(f"statvfs failed ({e})")
    try:
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
            headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=8).read(64)
    except Exception as e:
        reasons.append(f"Yahoo unreachable ({e})")
    return reasons


def load_core(inv_dir):
    p = os.path.join(inv_dir, "screener_core.py")
    spec = importlib.util.spec_from_file_location("screener_core", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["screener_core"] = m
    spec.loader.exec_module(m)
    return m


def native(v):
    try:
        import numpy as np
        if isinstance(v, np.integer):  return int(v)
        if isinstance(v, np.floating):
            f = float(v); return None if math.isnan(f) else f
        if isinstance(v, np.bool_):    return bool(v)
    except Exception:
        pass
    if isinstance(v, float) and math.isnan(v): return None
    return v


def clean_rows(rows):
    return [{k: native(val) for k, val in r.items()} for r in rows]


def native_dict(d):
    return {k: native(v) for k, v in (d or {}).items()}


def save_state(state, path):
    tmp = "/dev/shm/_screener_state.json"
    json.dump(state, open(tmp, "w", encoding="utf-8"), default=str)
    if os.path.dirname(tmp) == os.path.dirname(path):
        os.replace(tmp, path)
    else:
        json.dump(state, open(path, "w", encoding="utf-8"), default=str)


def screen_batch_nasdaq(core, cdf, info_map, stmt_map):
    """Apply Nasdaq-modified gates (Gate1 sector nasdaq_mode, Gate2 GM 40%, Gate3 FCF, Gate4 RevCAGR)
    per ticker for one batch — mirrors screener_core.screen_group_nasdaq Phase1+2 logic. MktCap>=$2bn
    pre-gate is applied at SOURCING by fetch_nasdaq, so it is not re-checked here."""
    import pandas as pd
    passers, excl = [], []
    for _, row in cdf.iterrows():
        rowd = row.to_dict(); sym = rowd["ticker"]; info = info_map.get(sym)
        if info is None:
            excl.append({**rowd, "gate_code": "TECHNICAL_SOURCE_FAILURE", "gate_reason": "info fetch failed"}); continue
        g1, r1, c1 = core.gate1_pass(info, nasdaq_mode=True)
        if not g1:
            excl.append({**rowd, "gate_code": c1, "gate_reason": r1}); continue
        st = stmt_map.get(sym, {}); inc = st.get("income_stmt"); cf = st.get("cashflow")
        g2, r2, c2, gm = core.gate2_pass(inc, info, nasdaq_mode=True)
        if g2 is None or not g2:
            excl.append({**rowd, "gate_code": c2, "gate_reason": r2, "gross_margin": gm}); continue
        g3, r3, c3, fp, av = core.gate3_pass(cf)
        if g3 is None or not g3:
            excl.append({**rowd, "gate_code": c3, "gate_reason": r3}); continue
        bucket = core.classify_sector_bucket(info.get("sector", "") or "", info.get("industry", "") or "")
        g4, r4, c4, rc = core.gate4_pass(inc, sector_bucket=bucket)
        if g4 is None or not g4:
            excl.append({**rowd, "gate_code": c4, "gate_reason": r4, "rev_cagr_3yr": rc}); continue
        passers.append(rowd)
    return pd.DataFrame(passers), pd.DataFrame(excl)


def main():
    ap = argparse.ArgumentParser(description="Local resumable growth screener runner.")
    ap.add_argument("--group")
    ap.add_argument("--tickers", nargs="+")
    ap.add_argument("--date", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--inv-dir", dest="inv_dir", required=True)
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--overlay-batch", type=int, default=8)
    ap.add_argument("--shm", default="/dev/shm/pylibs")
    ap.add_argument("--partial", default=None)
    ap.add_argument("--max-fail-rate", type=float, default=0.5)
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--constituents", default=None)
    ap.add_argument("--dump-constituents", default=None)
    a = ap.parse_args()

    if a.shm and os.path.isdir(a.shm):
        sys.path.insert(0, a.shm)

    # HARD disk guardrail: the HOME fs (/sessions) is tiny (~12 MB) and holds pip's DEFAULT temp dir + the
    # yfinance cache (the May-2026 fill surface). Force ALL temp + the yfinance cache onto tmpfs /dev/shm, in
    # code, independent of whatever the SKILL exported. Nothing this runner does can land on the tight fs.
    for _d in ("/dev/shm/piptmp", "/dev/shm/yf_cache"):
        try: os.makedirs(_d, exist_ok=True)
        except Exception: pass
    os.environ["TMPDIR"] = "/dev/shm/piptmp"
    try:
        import tempfile as _tf; _tf.tempdir = "/dev/shm/piptmp"
    except Exception: pass
    try:
        import yfinance as _yf; _yf.set_tz_cache_location("/dev/shm/yf_cache")
    except Exception: pass

    if not a.skip_preflight:
        pf = preflight(a.shm)
        if pf:
            print("FALLBACK_TO_COMPOSIO: " + "; ".join(pf)); sys.exit(3)

    core = load_core(a.inv_dir)
    import pandas as pd
    import logging; logging.disable(logging.WARNING)

    run_date = a.date.replace("-", "")
    group = a.group or "ADHOC"
    par_group = a.group if a.group in core.BATCH_PARAMS else "OTHER"
    os.makedirs(a.outputs, exist_ok=True)
    try:
        _ost = os.statvfs(a.outputs); _ofree = _ost.f_bavail * _ost.f_frsize / 1e6
        if _ofree < 100:
            print(f"FALLBACK_TO_COMPOSIO: outputs dir on a critically-low fs ({_ofree:.0f}MB free) — point --outputs at the OneDrive mount"); sys.exit(3)
    except Exception:
        pass
    partial = a.partial or os.path.join(a.outputs, f"{run_date}_{group}_screener.partial.json")

    state = None
    if os.path.exists(partial):
        try:
            prev = json.load(open(partial, encoding="utf-8"))
            if isinstance(prev, dict) and prev.get("group") == group \
               and prev.get("run_date") == run_date and "plan" in prev:
                state = prev
        except Exception:
            state = None

    if state is None:
        if a.constituents:
            const = clean_rows(json.load(open(a.constituents, encoding="utf-8")))
            warnings = ["constituents_provided_external"]
        elif a.tickers:
            const = [{"ticker": t, "company": t, "sector": "", "industry": "",
                      "index": group} for t in a.tickers]
            warnings = []
        else:
            if not a.group:
                ap.error("--group, --tickers or --constituents required")
            try:
                cdf, warnings = core.fetch_constituents(a.group)
            except Exception as e:
                print(f"FALLBACK_TO_COMPOSIO: constituent fetch raised for {group} ({e})"); sys.exit(3)
            const = clean_rows(cdf.to_dict("records"))
            if (not const) or (warnings and "CONSTITUENT_SOURCE_FAILURE" in str(warnings)):
                print(f"FALLBACK_TO_COMPOSIO: constituent fetch failed/empty for {group} ({warnings})"); sys.exit(3)
        if a.dump_constituents:
            json.dump(const, open(a.dump_constituents, "w", encoding="utf-8"), default=str)
            print(f"CONSTITUENTS_DUMPED group={group} n={len(const)} -> {a.dump_constituents}"); return
        tickers = [r["ticker"] for r in const]
        bs = a.batch_size
        plan = [tickers[i:i + bs] for i in range(0, len(tickers), bs)]
        state = {"group": group, "run_date": run_date, "par_group": par_group,
                 "const": const, "warnings": warnings, "plan": plan,
                 "stage": "score", "done": {}, "scored": [], "passers": [],
                 "excluded": [], "techfail": []}
        save_state(state, partial)
        print(f"PLAN_BUILT group={group} constituents={len(tickers)} batches={len(plan)}")
        if not a.tickers:
            print(f"NOT_DONE - {len(plan)} batch(es) left. Call again."); return

    pg = state.get("par_group", par_group)

    if state["stage"] == "score":
        plan = state["plan"]; n = len(plan)
        nxt = next((i for i in range(n) if str(i) not in state["done"]), None)
        if nxt is not None:
            batch = plan[nxt]; bset = set(batch)
            cdf = pd.DataFrame([r for r in state["const"] if r["ticker"] in bset])
            info_map, info_err = core.fetch_phase1_info(batch, pg)
            stmt_map, stmt_err = core.fetch_phase2_statements(batch, pg)
            if pg == "NASDAQ":
                passers_df, exclusions_df = screen_batch_nasdaq(core, cdf, info_map, stmt_map)
            else:
                passers_df, exclusions_df, _gd = core.screen_group_standard(cdf, info_map, stmt_map)
            passers = passers_df["ticker"].tolist() if not passers_df.empty else []
            ph3, _e = core.fetch_phase3_scoring(passers, pg)
            scored, techfail = [], []
            for t in passers:
                d = ph3.get(t)
                if d is None:
                    techfail.append({"ticker": t, "reason": "phase3_fetch_failed"}); continue
                try:
                    info = {**(info_map.get(t) or {}), **d}
                    inc = stmt_map.get(t, {}).get("income_stmt")
                    cf = stmt_map.get(t, {}).get("cashflow")
                    bal = stmt_map.get(t, {}).get("balance_sheet")
                    _iq = d.get("quarterly_income_stmt")
                    inc_q = _iq if (_iq is not None and not (hasattr(_iq, "empty") and _iq.empty)) \
                        else stmt_map.get(t, {}).get("income_stmt_quarterly")
                    row = core._score_ticker(t, info, inc, cf, bal, inc_q, cdf)
                    scored.append(row)
                except Exception as e:
                    techfail.append({"ticker": t, "reason": f"scoring_exception:{e}"})
            state["scored"].extend(clean_rows(scored))
            state["passers"].extend(clean_rows(passers_df.to_dict("records")) if not passers_df.empty else [])
            state["excluded"].extend(clean_rows(exclusions_df.to_dict("records")) if not exclusions_df.empty else [])
            state["techfail"].extend(techfail)
            state["done"][str(nxt)] = {"batch": len(batch), "info_err": len(info_err)}
            save_state(state, partial)
            done = len(state["done"]); remaining = n - done
            tb = sum(d["batch"] for d in state["done"].values())
            te = sum(d["info_err"] for d in state["done"].values())
            fr = te / max(tb, 1)
            print(f"BATCH {nxt+1}/{n} scored+={len(scored)} excl+={0 if exclusions_df.empty else len(exclusions_df)} cum_failrate={fr:.0%}")
            if fr > a.max_fail_rate and done >= 2:
                print(f"FALLBACK_TO_COMPOSIO: cumulative fetch failure {fr:.0%} > {a.max_fail_rate:.0%}"); sys.exit(3)
            if remaining > 0:
                print(f"NOT_DONE - {remaining} score batch(es) left. Call again."); return
        hs = [r["ticker"] for r in state["scored"]
              if (r.get("part_a_score") or 0) + (r.get("part_b_score") or 0) > core.OVERLAY_SCORE_TRIGGER]
        ob = a.overlay_batch
        state["overlay_plan"] = [hs[i:i + ob] for i in range(0, len(hs), ob)]
        state["overlay_done"] = {}
        state["stage"] = "overlay"
        save_state(state, partial)
        print(f"SCORING_DONE high_score={len(hs)} overlay_batches={len(state['overlay_plan'])}")

    if state["stage"] == "overlay":
        oplan = state.get("overlay_plan", []); on = len(oplan)
        onx = next((i for i in range(on) if str(i) not in state.get("overlay_done", {})), None)
        if onx is not None:
            obatch = oplan[onx]
            scored_by_t = {r["ticker"]: r for r in state["scored"]}
            hs_results, _ = core.fetch_phase3_scoring(obatch, pg, high_score_tickers=obatch)
            info_map, _ = core.fetch_phase1_info(obatch, pg)
            stmt_map, _ = core.fetch_phase2_statements(obatch, pg)
            for t in obatch:
                row = scored_by_t.get(t)
                if row is None:
                    continue
                d = hs_results.get(t) or {}
                info = {**(info_map.get(t) or {}), **d}
                inc = stmt_map.get(t, {}).get("income_stmt")
                cf = stmt_map.get(t, {}).get("cashflow")
                bal = stmt_map.get(t, {}).get("balance_sheet")
                pa_out = {k: v for k, v in row.items() if (k.startswith("score_") or k == "roic")}
                try:
                    geo = core.geography_group(t)
                    ovl = core.run_overlays(t, info, inc, cf, bal, d, pa_out, geo)
                    row.update(native_dict(ovl))
                except Exception as e:
                    row["overlay_status"] = f"error:{e}"
            state["scored"] = list(scored_by_t.values())
            state["overlay_done"][str(onx)] = 1
            save_state(state, partial)
            if on - len(state["overlay_done"]) > 0:
                print(f"OVERLAY {onx+1}/{on} done. Call again."); return
        state["stage"] = "finalize"
        save_state(state, partial)

    scored = [{k: native(v) for k, v in r.items()} for r in state["scored"]]
    passers_df = pd.DataFrame(state["passers"]) if state["passers"] else pd.DataFrame()
    excl_df = pd.DataFrame(state["excluded"]) if state["excluded"] else pd.DataFrame()
    core.save_full_data(scored, a.outputs, run_date, group)
    core.save_gate_results(passers_df, excl_df, a.outputs, run_date, group)
    try:
        g4 = core.build_gate4_sector_summary(excl_df)
    except Exception:
        g4 = {}
    total = len(state["const"])
    accounted = len(scored) + len(state["excluded"]) + len(state["techfail"])
    json.dump({"status": "done"}, open(partial, "w", encoding="utf-8"))
    print(f"ALL_DONE group={group} constituents={total} scored={len(scored)} "
          f"excluded={len(state['excluded'])} techfail={len(state['techfail'])} "
          f"accounted={accounted} gate4_conc={bool(g4.get('_concentration_warning'))}")


if __name__ == "__main__":
    main()
