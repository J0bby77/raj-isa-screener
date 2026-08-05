#!/usr/bin/env python3
"""
capture_screen_artefacts.py — §Q capture, 03-Aug-2026.

THE PROBLEM
-----------
Three point-in-time facts are produced by every weekly screen and then destroyed:

1. **The 139-column `full_data` frame.** Written to the session `outputs/` dir, which
   "clears automatically between sessions" (Run_Context §19 of the growth-screen context).
   The LOSSY 7-sheet workbook is retained; the rich frame is not. Register item M1.
2. **Point-in-time index membership.** Constituent lists are fetched live each run and never
   stored, so "what was in the index that week" is unreconstructable. Index membership changes;
   without it every backward-looking study silently inherits today's membership.
3. **The market regime in force at the formation date.** `regime_resolver` computes it, the
   screen acts on it, nothing stamps it onto the captured rows. Without it every IC is a joint
   statement about the signal AND the regime and cannot be decomposed — which is exactly why
   the ten June/July cohorts are uninterpretable.

None of the three is recoverable after the fact. This module is therefore additive capture
only: it READS what the screen already produced and WRITES it somewhere permanent. It touches
no scoring path, changes no recommendation, and cannot alter a single decision.

DESIGN RULES (inherited from the capture layer)
-----------------------------------------------
* Idempotent field-wise merge on the natural key — never `drop_duplicates(keep="last")`,
  which destroyed 1,246 rich rows on 29-Jul-2026.
* Absent input is a WARN with a stated reason, never a silent pass and never a zero.
* Nothing is deleted from the source; the screen's own cleanup is unchanged.

CLI
---
  python3 capture_screen_artefacts.py --sweep                    # capture whatever is in outputs/
  python3 capture_screen_artefacts.py --src <dir> --run_date ... --group ...
  python3 capture_screen_artefacts.py --selftest
"""
from __future__ import annotations
import argparse, glob, json, os, re, shutil, sys, datetime as _dt

HERE = os.path.dirname(os.path.abspath(__file__))
SCREEN_HISTORY = "screen_history"            # full_data frames, one file per run_date+group
CONSTITUENTS   = "constituents_history.csv"  # PIT index membership
REGIME         = "regime_history.csv"        # regime in force per formation date

FULL_DATA_RE = re.compile(r"(?P<run_date>\d{8})_(?P<group>[A-Za-z0-9\-]+)_full_data\.csv$")

CONSTITUENT_COLS = ["run_date", "group", "ticker", "company", "sector", "in_index", "scored", "gate_code"]
REGIME_COLS = ["run_date", "market_regime", "regime_basis", "drawdown_pct", "tranches_fired",
               "macro_regime", "resolver_state", "captured_at", "source", "stamp_basis"]


# ---------------------------------------------------------------- helpers
def _norm_date(s):
    """20260724 or 2026-07-24 -> 2026-07-24."""
    s = str(s).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _merge_csv(path, new_rows, key_cols, all_cols):
    """Idempotent field-wise merge. Incoming wins per-cell where it has a value; existing
    values are retained wherever the incoming cell is null. Same contract as
    score_panel_logger.log_from_full_data."""
    import pandas as pd
    new = pd.DataFrame(new_rows, columns=all_cols)
    if not len(new):
        return 0, 0
    if os.path.exists(path):
        old = pd.read_csv(path)
        for c in all_cols:
            if c not in old.columns:
                old[c] = None
        old = old[all_cols]
        oi = old.set_index(key_cols)
        ni = new.set_index(key_cols)
        oi = oi[~oi.index.duplicated(keep="last")]
        ni = ni[~ni.index.duplicated(keep="last")]
        merged = ni.combine_first(oi).reset_index()[all_cols]
    else:
        merged = new
    tmp = path + ".tmp"
    merged.to_csv(tmp, index=False)
    os.replace(tmp, path)                     # atomic; a kill mid-write leaves no partial file
    return len(new), len(merged)


# ---------------------------------------------------------------- 1. full_data
def capture_full_data(src_path, dest_root, run_date=None, group=None):
    """Copy the 139-column frame somewhere permanent. Content-identical copy — no reshaping,
    because we do not yet know which of the 139 columns will matter."""
    base = os.path.basename(src_path)
    m = FULL_DATA_RE.search(base)
    if m:
        run_date = run_date or _norm_date(m.group("run_date"))
        group = group or m.group("group")
    if not (run_date and group):
        return {"ok": False, "reason": f"cannot infer run_date/group from {base!r}"}
    outdir = os.path.join(dest_root, SCREEN_HISTORY)
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, f"{run_date}_{group}_full_data.csv")
    if os.path.exists(dest) and os.path.getsize(dest) >= os.path.getsize(src_path):
        return {"ok": True, "action": "already_captured", "dest": dest, "run_date": run_date, "group": group}
    shutil.copy2(src_path, dest)
    return {"ok": True, "action": "captured", "dest": dest, "bytes": os.path.getsize(dest),
            "run_date": run_date, "group": group}


# ---------------------------------------------------------------- 2. PIT constituents
def capture_constituents(full_data_path, dest_root, run_date, group, gate_vars_path=None):
    """Point-in-time index membership: the union of everything the screen SAW that week —
    scored names from full_data, plus gate-rejected names from gate_variables.csv. The union
    is the membership; `scored` and `gate_code` record which side of the gate each fell."""
    import pandas as pd
    rows, seen = [], set()
    try:
        fd = pd.read_csv(full_data_path, usecols=lambda c: c in
                         ("ticker", "company", "sector", "final_status"), low_memory=False)
    except Exception as e:
        return {"ok": False, "reason": f"full_data unreadable: {e}"}
    for _, r in fd.iterrows():
        tk = r.get("ticker")
        if not isinstance(tk, str) or not tk or tk in seen:
            continue
        seen.add(tk)
        rows.append({"run_date": run_date, "group": group, "ticker": tk,
                     "company": r.get("company"), "sector": r.get("sector"),
                     "in_index": True, "scored": True, "gate_code": "pass"})
    if gate_vars_path and os.path.exists(gate_vars_path):
        try:
            gv = pd.read_csv(gate_vars_path, low_memory=False)
            gv = gv[(gv.get("run_date").astype(str) == str(run_date)) &
                    (gv.get("group").astype(str) == str(group))]
            for _, r in gv.iterrows():
                tk = r.get("ticker")
                if not isinstance(tk, str) or not tk or tk in seen:
                    continue
                seen.add(tk)
                rows.append({"run_date": run_date, "group": group, "ticker": tk,
                             "company": r.get("company"), "sector": r.get("sector"),
                             "in_index": True, "scored": False, "gate_code": r.get("gate_code")})
        except Exception as e:
            print(f"  WARN constituents: gate_variables join skipped — {e}", file=sys.stderr)
    n_new, n_tot = _merge_csv(os.path.join(dest_root, CONSTITUENTS), rows,
                              ["run_date", "group", "ticker"], CONSTITUENT_COLS)
    return {"ok": True, "rows_in": n_new, "store_total": n_tot,
            "scored": sum(1 for r in rows if r["scored"]),
            "rejected": sum(1 for r in rows if not r["scored"])}


# ---------------------------------------------------------------- 3. regime stamp
def capture_regime(dest_root, run_date, state_path=None, allow_backfill=False):
    """Stamp the regime in force at the formation date.

    ⚑ POINT-IN-TIME CONTRACT. `drawdown_state.json` holds the CURRENT regime, not a history.
    Stamping a past run_date from it writes today's regime onto a historical row — a value that
    says "the regime on 24-Jul" and IS "the regime today". That is the exact defect class this
    capture layer exists to prevent, so it is refused by default.

    A row is only point-in-time when `stamp_basis == "live"` (run_date == today). Backfill is
    possible with allow_backfill=True and is marked `backfilled_not_pit`; anything reading this
    store MUST filter on stamp_basis before using a regime as evidence.
    """
    import datetime as _d
    today = _d.date.today().isoformat()
    live = (str(run_date) == today)
    if not live and not allow_backfill:
        print(f"  SKIP regime {run_date}: not today ({today}) and allow_backfill=False — "
              f"drawdown_state holds only the CURRENT regime, so this would not be point-in-time",
              file=sys.stderr)
        return {"ok": False, "reason": "would_not_be_point_in_time", "run_date": run_date}
    rec = {c: None for c in REGIME_COLS}
    rec["run_date"] = run_date
    rec["captured_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rec["stamp_basis"] = "live" if live else "backfilled_not_pit"
    sp = state_path or os.path.join(dest_root, "drawdown_state.json")
    try:
        with open(sp) as f:
            st = json.load(f)
        rec["market_regime"] = st.get("regime_state")
        rec["regime_basis"] = json.dumps(st.get("regime_basis")) if st.get("regime_basis") else None
        rec["drawdown_pct"] = st.get("drawdown_pct") or st.get("pct_from_high")
        rec["tranches_fired"] = st.get("tranches_fired")
        rec["source"] = "drawdown_state.regime_state"
    except Exception as e:
        rec["source"] = f"UNAVAILABLE: {e}"
    try:
        sys.path.insert(0, dest_root)
        import regime_resolver as rr
        mr, _basis = rr.read_market_regime(sp)
        if mr and not rec["market_regime"]:
            rec["market_regime"] = mr
            rec["source"] = "regime_resolver.read_market_regime"
    except Exception:
        pass
    if not rec["market_regime"]:
        print("  WARN regime: market_regime UNRESOLVED — stamped as null with reason, "
              "never as a guess", file=sys.stderr)
    n_new, n_tot = _merge_csv(os.path.join(dest_root, REGIME), [rec], ["run_date"], REGIME_COLS)
    return {"ok": True, "market_regime": rec["market_regime"], "stamp_basis": rec["stamp_basis"],
            "rows_in": n_new, "store_total": n_tot}


# ---------------------------------------------------------------- orchestration
def capture_one(full_data_path, dest_root, run_date=None, group=None):
    out = {"src": full_data_path}
    fd = capture_full_data(full_data_path, dest_root, run_date, group)
    out["full_data"] = fd
    if not fd.get("ok"):
        return out
    rd, gp = fd["run_date"], fd["group"]
    out["constituents"] = capture_constituents(
        full_data_path, dest_root, rd, gp, os.path.join(dest_root, "gate_variables.csv"))
    out["regime"] = capture_regime(dest_root, rd)   # PIT-guarded; skips non-today dates
    return out


def sweep(src_dirs, dest_root):
    found = []
    for d in src_dirs:
        if d and os.path.isdir(d):
            found += sorted(glob.glob(os.path.join(d, "*_full_data.csv")))
    if not found:
        print(f"NO_FULL_DATA_FOUND searched={[d for d in src_dirs if d]} — "
              "WARN, not an error: outputs/ clears between sessions by design")
        return []
    return [capture_one(p, dest_root) for p in found]


# ---------------------------------------------------------------- selftest
def _selftest():
    import tempfile, pandas as pd, datetime as _d
    ok = True
    TODAY = _d.date.today()
    D1 = TODAY.strftime("%Y%m%d"); D1N = TODAY.isoformat()
    D2 = (TODAY + _d.timedelta(days=7)).strftime("%Y%m%d"); D2N = (TODAY + _d.timedelta(days=7)).isoformat()
    with tempfile.TemporaryDirectory() as td:
        src, dest = os.path.join(td, "outputs"), os.path.join(td, "ISA")
        os.makedirs(src); os.makedirs(dest)
        pd.DataFrame({"ticker": ["AAA", "BBB"], "company": ["A Co", "B Co"],
                      "sector": ["Technology", "Health Care"],
                      "final_status": ["CANDIDATE_RANKABLE"] * 2,
                      **{f"col{i}": [i, i] for i in range(135)}}
                     ).to_csv(os.path.join(src, f"{D1}_NASDAQ_full_data.csv"), index=False)
        pd.DataFrame({"run_date": [D1N] * 2, "group": ["NASDAQ"] * 2,
                      "ticker": ["ZZZ", "AAA"], "company": ["Z Co", "A Co"],
                      "sector": ["Energy", "Technology"],
                      "gate_code": ["Gate 4", "pass"]}
                     ).to_csv(os.path.join(dest, "gate_variables.csv"), index=False)
        json.dump({"regime_state": "RISK_ON", "drawdown_pct": -1.9, "tranches_fired": 0},
                  open(os.path.join(dest, "drawdown_state.json"), "w"))

        r1 = sweep([src], dest)
        assert r1 and r1[0]["full_data"]["action"] == "captured", "full_data not captured"
        cap = os.path.join(dest, SCREEN_HISTORY, f"{D1N}_NASDAQ_full_data.csv")
        assert os.path.exists(cap), "captured file missing"
        assert len(pd.read_csv(cap).columns) == 139, "column count not preserved"
        c = pd.read_csv(os.path.join(dest, CONSTITUENTS))
        assert len(c) == 3, f"expected 3 constituents (2 scored + 1 rejected), got {len(c)}"
        assert set(c[~c.scored].ticker) == {"ZZZ"}, "gate-rejected name not captured"
        g = pd.read_csv(os.path.join(dest, REGIME))
        assert g.iloc[0].market_regime == "RISK_ON", "regime not stamped"

        r2 = sweep([src], dest)                                   # idempotency
        assert r2[0]["full_data"]["action"] == "already_captured", "re-capture not idempotent"
        assert len(pd.read_csv(os.path.join(dest, CONSTITUENTS))) == 3, "constituents duplicated"
        assert len(pd.read_csv(os.path.join(dest, REGIME))) == 1, "regime duplicated"

        # a later run must ADD, never replace
        pd.DataFrame({"ticker": ["CCC"], "company": ["C Co"], "sector": ["Utilities"],
                      "final_status": ["CANDIDATE_RANKABLE"]}
                     ).to_csv(os.path.join(src, f"{D2}_SP500_full_data.csv"), index=False)
        sweep([src], dest)
        c2 = pd.read_csv(os.path.join(dest, CONSTITUENTS))
        assert len(c2) == 4 and set(c2.run_date) == {D1N, D2N}, "history lost"

        # missing regime source must WARN and stamp null, never guess
        os.remove(os.path.join(dest, "drawdown_state.json"))
        rr = capture_regime(dest, D1N)
        assert rr["market_regime"] is None, "absent regime must be null, not a guess"
        assert rr["stamp_basis"] == "live", "today's stamp must be live"
        pit = capture_regime(dest, "2019-01-01")
        assert pit["ok"] is False and pit["reason"] == "would_not_be_point_in_time", \
            "backfilling a past date from CURRENT state must be refused"
        bf = capture_regime(dest, "2019-01-01", allow_backfill=True)
        assert bf["stamp_basis"] == "backfilled_not_pit", "backfill must be marked not-PIT"
    print("SELFTEST PASS — 11 assertions (capture, 139-col preservation, gate-rejected union, "
          "regime stamp, idempotency ×3, history retention, absent-source null, "
          "PIT refusal, backfill marking)")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", action="append", default=[])
    ap.add_argument("--dest", default=HERE)
    ap.add_argument("--run_date"); ap.add_argument("--group")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    srcs = a.src or [os.environ.get("ISA_OUTPUTS_DIR"), os.path.join(a.dest, "outputs"),
                     "/mnt/user-data/outputs", os.getcwd()]
    res = sweep(srcs, a.dest) if (a.sweep or not a.run_date) else \
        [capture_one(s, a.dest, a.run_date, a.group) for s in srcs if os.path.isfile(s)]
    for r in res:
        fd = r.get("full_data", {})
        print(f"CAPTURED {fd.get('run_date')} {fd.get('group')} -> {fd.get('action')} | "
              f"constituents {r.get('constituents', {}).get('rows_in')} rows "
              f"({r.get('constituents', {}).get('rejected')} gate-rejected) | "
              f"regime {r.get('regime', {}).get('market_regime')}")
    print(f"DONE files={len(res)}")


if __name__ == "__main__":
    main()
