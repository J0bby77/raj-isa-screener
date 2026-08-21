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
try:
    import fetch_guard as _fg   # H-5 (26-Jul-26)
except Exception:
    _fg = None


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
    # Stage screener_core.py OFF the OneDrive mount into tmpfs before importing.
    # The mount can serve a stale/TRUNCATED copy right after an edit (OneDrive->mount
    # sync lag) -- importing that directly either crashes on a SyntaxError or, worse,
    # runs partial logic. Reading the full bytes, compile-checking, then exec'ing from
    # /dev/shm removes that failure class for the scoring engine. (Root-cause fix for
    # the 10-Jun-26 truncation incident.)
    src = os.path.join(inv_dir, "screener_core.py")
    with open(src, "r", encoding="utf-8") as fh:
        code = fh.read()
    try:
        compile(code, "screener_core.py", "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"screener_core.py failed to compile at line {e.lineno}: {e.msg}. This is "
            "almost always a truncated/half-synced file on the OneDrive mount. Run HALTED "
            "to avoid executing partial scoring logic; re-run once OneDrive has synced."
        ) from e
    staged = "/dev/shm/screener_core_staged.py"
    with open(staged, "w", encoding="utf-8") as fh:
        fh.write(code)
    spec = importlib.util.spec_from_file_location("screener_core", staged)
    m = importlib.util.module_from_spec(spec)
    # 07-Aug-26 ROOT-CAUSE FIX. The staging above protects against a truncated mount read, but it
    # also made screener_core believe it LIVES in /dev/shm: every sibling path it derives from
    # __file__ (sys.path seed, supplementary_constituents.json, drawdown_state.json, score_panel.csv,
    # the calibration pool store, and the SS-Q capture destination) resolved to tmpfs, which is wiped
    # between bash calls. The code ran, reported success, and wrote its artefacts into a directory
    # that ceased to exist -- including screen_capture_status.json, the receipt that was supposed to
    # make such a failure visible. Point __file__ at the REAL module home before exec: the bytes
    # still come from the staged tmpfs copy (truncation protection intact), but every path the module
    # derives now resolves to the Investment Analysis folder, which is the one thing that was wrong.
    m.__file__ = src
    sys.modules["screener_core"] = m
    spec.loader.exec_module(m)
    if os.path.dirname(os.path.abspath(m.__file__)) != os.path.abspath(inv_dir):
        raise RuntimeError(
            "screener_core.__file__ does not resolve to --inv-dir after staging "
            f"({m.__file__!r} vs {inv_dir!r}); sibling artefacts would be written to tmpfs and lost."
        )
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
    pre-gate is applied at SOURCING by fetch_nasdaq, so it is not re-checked here.

    ⚑ DECLARED DIVERGENCE (07-Aug-2026). This is a SECOND HOME for the Nasdaq gate sequence; the
    first is screener_core.screen_group_nasdaq. It exists because that function owns its own
    fetching and cannot be driven batch-wise. It is registered in ORCHESTRATOR_PARITY_EXEMPT with
    a reason so the parity invariant reports it rather than silently tolerating it.

    Returns (passers_df, exclusions_df, gate_data). `gate_data` maps ticker -> the gate verdict, and
    is REQUIRED by emit_gate_variables: without it every rejected name is recorded with a blank
    verdict, which is the shape of the data that makes a bad gate invisible. `gate_pass` preserves
    True / False / None(unresolved) distinctly — an unresolved name is not a rejected one."""
    import pandas as pd
    passers, excl = [], []
    gate_data = {}

    def _verdict(sym, code, reason, passed):
        gate_data[sym] = {"gate_code": code, "gate_reason": reason, "gate_pass": passed}
    for _, row in cdf.iterrows():
        rowd = row.to_dict(); sym = rowd["ticker"]; info = info_map.get(sym)
        if info is None:
            _verdict(sym, "TECHNICAL_SOURCE_FAILURE", "info fetch failed", None)
            excl.append({**rowd, "gate_code": "TECHNICAL_SOURCE_FAILURE", "gate_reason": "info fetch failed"}); continue
        g1, r1, c1 = core.gate1_pass(info, nasdaq_mode=True)
        if not g1:
            _verdict(sym, c1, r1, False)
            excl.append({**rowd, "gate_code": c1, "gate_reason": r1}); continue
        st = stmt_map.get(sym, {}); inc = st.get("income_stmt"); cf = st.get("cashflow")
        g2, r2, c2, gm = core.gate2_pass(inc, info, nasdaq_mode=True)
        if g2 is None or not g2:
            _verdict(sym, c2, r2, None if g2 is None else False)
            excl.append({**rowd, "gate_code": c2, "gate_reason": r2, "gross_margin": gm}); continue
        g3, r3, c3, fp, av = core.gate3_pass(cf)
        if g3 is None or not g3:
            _verdict(sym, c3, r3, None if g3 is None else False)
            excl.append({**rowd, "gate_code": c3, "gate_reason": r3}); continue
        bucket = core.classify_sector_bucket(info.get("sector", "") or "", info.get("industry", "") or "")
        g4, r4, c4, rc = core.gate4_pass(inc, sector_bucket=bucket)
        if g4 is None or not g4:
            _verdict(sym, c4, r4, None if g4 is None else False)
            excl.append({**rowd, "gate_code": c4, "gate_reason": r4, "rev_cagr_3yr": rc}); continue
        _verdict(sym, "", "", True)
        passers.append(rowd)
    return pd.DataFrame(passers), pd.DataFrame(excl), gate_data


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
            info_map, info_err = (_fg.with_backoff(core.fetch_phase1_info, batch, pg) if _fg else core.fetch_phase1_info(batch, pg))  # H-5
            stmt_map, stmt_err = (_fg.with_backoff(core.fetch_phase2_statements, batch, pg) if _fg else core.fetch_phase2_statements(batch, pg))  # H-5
            if pg == "NASDAQ":
                passers_df, exclusions_df, _gd = screen_batch_nasdaq(core, cdf, info_map, stmt_map)
            else:
                passers_df, exclusions_df, _gd = core.screen_group_standard(cdf, info_map, stmt_map)
            # ── CAPTURE LAYER ITEM 1 / ORCHESTRATOR PARITY (07-Aug-2026) ───────────────────
            # screener_core.run_scheduled has emitted gate variables since 02-Aug; this path —
            # the one that actually runs every week — never did, so PIT constituent membership
            # covered scored names ONLY and every backward-looking study on a weekly frame
            # inherited a survivor-biased universe.
            #
            # ⚑ THE DATE FORMAT IS LOAD-BEARING. capture_screen_artefacts joins gate_variables on
            # the ISO run_date it derives from the frame filename. Passing the compact YYYYMMDD
            # used for filenames writes rows that match NOTHING and reports success — the exact
            # defect class this framework keeps paying for. `a.date` is ISO; use it, not run_date.
            _gv = core.emit_gate_variables(cdf, info_map, stmt_map, _gd, group, a.date)
            state.setdefault("gate_vars", []).append({
                "batch": nxt, "rows_in": _gv.get("rows_in"), "error": _gv.get("error")})
            if _gv.get("error"):
                print(f"WARN GATE_VARS batch {nxt}: {_gv['error']}")
            passers = passers_df["ticker"].tolist() if not passers_df.empty else []
            # SECTION 7b: under the default overlay population the deeper inputs come back in
            # THIS pass, so the overlay stage below no longer re-fetches phases 1, 2 and 3 for a
            # second time. It re-fetched all three because this path's resume state is JSON and
            # DataFrames cannot survive it — the cost was structural, not incidental.
            _deep = passers if core.wants_overlay_inputs() else []
            ph3, _e = (_fg.with_backoff(core.fetch_phase3_scoring, passers, pg, overlay_inputs_for=_deep)
                       if _fg else core.fetch_phase3_scoring(passers, pg, overlay_inputs_for=_deep))  # H-5
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
                    core.apply_overlays_inline(row, t, info, inc, cf, bal, d)
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
        # ── OVERLAY POPULATION (SECTION 7b) ───────────────────────────────────────────────────
        # Default `all_gate_passers`: already applied inline in the batch loop above — no plan, no
        # second fetch, and nothing that can be evaluated in the wrong order.
        #
        # ⚑ THE ORDER BELOW IS LOAD-BEARING, and getting it wrong here is precisely the defect
        # this section replaces. From 07-Aug-2026 to 19-Aug-2026 this path computed the gate from
        # `source_score_for_row` BEFORE `core.apply_cross_sectional_momentum` restamped
        # forward_axis_score — 60% of that score. Every line ran, the run succeeded, and the
        # overlay set simply was not the SUMMARY set: 5 of 8 SUMMARY names on the 08-Aug F250-SPI
        # screen and 5 of 40 on 15-Aug SP500 shipped with no overlays at all. The restamp is
        # therefore invoked HERE, before the population is read, in the rollback path too.
        if core.wants_overlay_inputs():
            state["overlay_plan"] = []
            state["overlay_done"] = {}
            _n_ovl = sum(1 for r in state["scored"] if r.get("overlay_status"))
            print(f"SCORING_DONE overlays_inline={_n_ovl}/{len(state['scored'])} "
                  f"population=all_gate_passers overlay_batches=0")
        else:
            for _r in state["scored"]:
                _r.setdefault("forward_axis_score_bands", _r.get("forward_axis_score"))
                _r.setdefault("score_f_price_mom_blend_bands", _r.get("score_f_price_mom_blend"))
            core.apply_cross_sectional_momentum(state["scored"])
            hs, _pop_basis = core.overlay_population(state["scored"])
            ob = a.overlay_batch
            state["overlay_plan"] = [hs[i:i + ob] for i in range(0, len(hs), ob)]
            state["overlay_done"] = {}
            print(f"SCORING_DONE gated={len(hs)} basis={_pop_basis} "
                  f"overlay_batches={len(state['overlay_plan'])}")
        state["stage"] = "overlay"
        save_state(state, partial)

    if state["stage"] == "overlay":
        oplan = state.get("overlay_plan", []); on = len(oplan)
        onx = next((i for i in range(on) if str(i) not in state.get("overlay_done", {})), None)
        if onx is not None:
            obatch = oplan[onx]
            scored_by_t = {r["ticker"]: r for r in state["scored"]}
            hs_results, _ = core.fetch_phase3_scoring(obatch, pg, overlay_inputs_for=obatch)
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
                core.apply_overlays_inline(row, t, info, inc, cf, bal, d,
                                           mode="all_gate_passers")   # membership already decided
                for _k, _v in list(row.items()):
                    row[_k] = native(_v)
            state["scored"] = list(scored_by_t.values())
            state["overlay_done"][str(onx)] = 1
            save_state(state, partial)
            if on - len(state["overlay_done"]) > 0:
                print(f"OVERLAY {onx+1}/{on} done. Call again."); return
        state["stage"] = "finalize"
        save_state(state, partial)

    scored = [{k: native(v) for k, v in r.items()} for r in state["scored"]]

    # ── ORCHESTRATOR PARITY (07-Aug-2026): CROSS-SECTIONAL MOMENTUM ──────────────────────────
    # WP-M (29-Jul-26) set PRICE_MOM_SCORING="percentile" because the absolute bands SATURATE at
    # both tails — on 24-Jul-26 MU scored 2/2 at +753% and would score 2/2 at +40%. The only code
    # that acts on that setting is core.apply_cross_sectional_momentum, which lived solely in
    # screener_core.run_scheduled. This path never called it, so the declared basis had never once
    # been in force on a weekly screen: on the 07-Aug SP500 frame price_mom_pctl was a declared
    # column with 0 of 312 non-null values, and 126 of 312 names sat on ONE blend value.
    #
    # It MUST run here — after all scoring, before the Source stamp below — because it restamps
    # forward_axis_score, which source_score_components_for_row consumes.
    _basis_declared = str(getattr(getattr(core, "_cfg", None), "PRICE_MOM_SCORING", "bands")).lower()
    for _r in scored:
        _r["forward_axis_score_bands"] = _r.get("forward_axis_score")
        _r["score_f_price_mom_blend_bands"] = _r.get("score_f_price_mom_blend")
    _n_mom = core.apply_cross_sectional_momentum(scored)
    _basis_in_force = "percentile" if (_basis_declared == "percentile" and _n_mom) else "bands"
    for _r in scored:
        _r["scoring_basis"] = _basis_in_force

    # HARD POST-CONDITION. A declared basis that restamps nothing is the failure this whole fix
    # exists to end: the run would succeed, the email would look clean, and the config would be a
    # lie. apply_cross_sectional_momentum swallows its own exceptions by design (a scoring failure
    # must not kill a screen), so the ONLY place this can be caught is here, at the call site.
    # It is not a warning. Below 20 rows the percentile rank is legitimately refused as too thin.
    if _basis_declared == "percentile" and len(scored) >= 20 and not _n_mom:
        print(f"MOMENTUM_BASIS_UNAPPLIED: PRICE_MOM_SCORING='percentile' restamped 0 of "
              f"{len(scored)} rows. The declared basis is NOT in force and the ranking below "
              f"would be built on the retired absolute bands. Refusing to publish.")
        sys.exit(4)

    # DUAL-BASIS DIFF — published, not asserted. The magnitude of a scoring change belongs in the
    # run output, where it can be checked, not in a retrospective written afterwards.
    _d = [abs(_r["forward_axis_score"] - _r["forward_axis_score_bands"]) for _r in scored
          if _r.get("forward_axis_score") is not None and _r.get("forward_axis_score_bands") is not None]
    if _d:
        _moved = sum(1 for _x in _d if _x > 0.05)
        print(f"MOMENTUM_BASIS basis={_basis_in_force} declared={_basis_declared} "
              f"restamped={_n_mom}/{len(scored)} forward_axis_moved={_moved}/{len(_d)} "
              f"mean_abs_delta={sum(_d)/len(_d):.2f} max_abs_delta={max(_d):.2f}")

    # ── Fix Pack P2.1 (18-Jul-26): stamp unified Source anatomy + E[r] on EVERY scored row ──
    # Parity with screener_core's post-overlay stamp (core run flow, "fixpack_stamp" phase).
    # The local batching path previously skipped it, leaving screen_source / implied_upside_fv /
    # expected_return_12_24m EMPTY in full_data.csv — root cause of the 17/18-Jul-26 emails
    # showing E[r] "—" and the display-only consensus gap in the "Upside (FV)" column (D7).
    try:
        import source_score as _ss_fin
        import expected_return as _er_fin
        _regime_fin = None   # B7 shadow (18-Jul-26): parity with screener_core's stamp
        try:
            import json as _dj_fin
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "drawdown_state.json")) as _df_fin:
                _regime_fin = (_dj_fin.load(_df_fin) or {}).get("regime_state")
        except Exception:
            pass
        # ⚑ D-24 §1.1 (09-Aug-2026). THIS is the live screen path, and it has its OWN E[r] call
        # site. The register already records that screener_local is a hand-maintained copy of
        # run_scheduled's orchestration that never calls it, and that this is why
        # PRICE_MOM_SCORING="percentile" never took effect for twelve days. A D-24 built only into
        # screener_core would not have reached the live screen AT ALL. Both paths now call the
        # SAME function — expected_return.stamp_frame — so there is one orchestration to drift
        # from, not two. orchestrator_parity.py asserts it.
        _d24 = _er_fin.stamp_frame(scored, run_date=run_date, group=group, regime=_regime_fin)
        _bt = _d24["built_table"]
        print(f"D24_ANCHOR rows={_bt['rows_in']} fit={_bt['fit_for_anchoring']} "
              f"sectors={len(_bt['median_by_sector'])} excluded={list(_bt['excluded'])} "
              f"as_of={_bt['as_of']}"
              + (f" | BORROWED {_d24['unmeasured'].get('anchor_substituted_from')}"
                 if not _bt["fit_for_anchoring"] else "")
              + f" | {_d24['reachability']['message']} | {_d24['unmeasured']['message']}")
        if _d24["unmeasured"]["verdict"] != "OK":
            print("WARN D-24 §6 " + _d24["unmeasured"]["message"])
        for _srow in scored:
            try:
                _srow.update(_ss_fin.source_score_components_for_row(_srow))
                _srow.update(_ss_fin.door_flags_for_row(_srow, _regime_fin))  # B7 shadow
            except Exception as _se:
                _srow.setdefault("source_input_missing", f"stamp_error:{_se}")
        _sel_fin, _sqa_fin = _ss_fin.select_summary(scored)
        print(f"FIXPACK_STAMP rows={len(scored)} summary_count={_sqa_fin['summary_count']} "
              f"(eligible {_sqa_fin['summary_eligible_count']}, floor {_sqa_fin['summary_floor']:g}, "
              f"cap {_sqa_fin['summary_cap']})"
              + (" SUMMARY_THIN_WARNING" if _sqa_fin.get("summary_thin_warning") else ""))
        # R5.1 — the overlay contract, at the first point where SUMMARY membership is final.
        # Both orchestrators call the SAME check, so "it passed in core" can never again mean
        # something different from "it passed on the path that actually runs every week."
        _ovc = core.overlay_coverage(scored)
        print(f"OVERLAY_COVERAGE {_ovc['verdict']} basis={_ovc['mode']} "
              f"summary={_ovc['summary']} missing={len(_ovc['summary_missing_overlays'])} "
              f"with_overlays={_ovc['with_overlays']}/{_ovc['scored']} "
              f"partial={_ovc['partial_overlays']} unresolved={_ovc['unresolved_by_overlay']} "
              f"| {_ovc['message']}")
    except Exception as _e:
        print(f"WARN fixpack stamping failed (non-fatal, rows keep pre-stamp fields): {_e}")
    passers_df = pd.DataFrame(state["passers"]) if state["passers"] else pd.DataFrame()
    excl_df = pd.DataFrame(state["excluded"]) if state["excluded"] else pd.DataFrame()
    core.save_full_data(scored, a.outputs, run_date, group)
    core.save_gate_results(passers_df, excl_df, a.outputs, run_date, group)
    try:
        # ISA-0375 + ISA-0368: the at-risk denominator must be passed HERE TOO, or this path
        # silently falls back to the retired raw-share basis. Parity of presence is not parity
        # of arguments.
        g4 = core.build_gate4_sector_summary(excl_df, passers_df)
    except Exception:
        g4 = {}
    total = len(state["const"])
    accounted = len(scored) + len(state["excluded"]) + len(state["techfail"])

    # ── ORCHESTRATOR PARITY (07-Aug-2026): run QA ────────────────────────────────────────────
    # run_scheduled has always called save_run_qa; this path never did, which is why build_email
    # pointed at a `{run_date}_{group}_run_qa.csv` that the live path did not produce and why the
    # Excel DIAGNOSTICS tab has been built without it. save_run_qa writes to outputs_dir ONLY
    # (session-temp, never OneDrive) and is the documented input to build_excel.py --run_qa.
    _run_qa = {
        "group": group, "run_date": run_date, "run_date_iso": a.date, "path": "local_primary",
        "constituents": total, "accounted": accounted, "scored": len(scored),
        "excluded": len(state["excluded"]), "technical_failures": len(state["techfail"]),
        "warnings": state.get("warnings"),
        "batches": len(state.get("plan", [])), "overlay_batches": len(state.get("overlay_plan", [])),
        "gate4_sector_summary": g4,
        "gate4_sector_concentration_warning": bool(g4.get("_concentration_warning")),
        "gate_variables": state.get("gate_vars"),
        "scoring_basis": _basis_in_force, "scoring_basis_declared": _basis_declared,
        "momentum_rows_restamped": _n_mom,
    }
    try:
        core.save_run_qa(_run_qa, a.inv_dir, run_date, group,
                         outputs_dir=a.outputs, tech_failures=state["techfail"])
    except Exception as _qe:
        print(f"WARN run_qa save failed (non-fatal): {_qe}")

    json.dump({"status": "done"}, open(partial, "w", encoding="utf-8"))
    print(f"ALL_DONE group={group} constituents={total} scored={len(scored)} "
          f"excluded={len(state['excluded'])} techfail={len(state['techfail'])} "
          f"accounted={accounted} gate4_conc={bool(g4.get('_concentration_warning'))}")


if __name__ == "__main__":
    main()
