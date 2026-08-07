#!/usr/bin/env python3
"""
delisting_registry.py — §Q2 survivorship (05-Aug-2026).

THE PROBLEM, AND WHY IT BIASES EVERY STUDY DOWNWARD
---------------------------------------------------
`calibration_report` concatenates columns and never drops them, so once a ticker is seen it
persists. What it cannot record is WHY a series stopped. A ticker that goes quiet looks
identical whether it was:

    acquired at a 40% premium        ...a WINNER, and the return is realised and knowable
    delisted after a collapse        ...a loser, and the return is roughly -100%
    a transient yfinance failure     ...no information at all

Those are the three most different outcomes in the dataset and the framework stores them the
same way. **Acquired companies are disproportionately winners** — that is what being acquired
usually means — so reading them as failures pulls every measured IC, every decile spread and
every gate evaluation downward by an amount nobody can quantify, because the evidence needed to
quantify it is the evidence that was never kept.

The register ranks this second by irreversibility, behind bid-ask. It is cheap, which is most of
the argument for doing it now.

WHAT IT DOES
------------
Compares the constituent set of each run against the previous run for the same group, and
records every DISAPPEARANCE with:
    last_seen · first_missing · runs_missing · last_price · terminal_return_from_first_seen
    status  ∈ {suspected_gone, confirmed_gone, returned, unknown}
    reason  ∈ {acquired, delisted, renamed, index_removal, fetch_failure, UNKNOWN}

⚑ THE HONEST PART. This module cannot tell an acquisition from a collapse on its own — that
needs a corporate-actions feed the framework does not have. It therefore **never guesses a
reason**. It records `UNKNOWN` and reports it, so a name is a stated open question rather than a
silent zero. A single missing run is `suspected_gone`, not gone: a one-week fetch failure is far
more common than a delisting, and treating the first absence as terminal would manufacture
exactly the false negatives this exists to prevent.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "delisting_registry.json")
CONSTITUENTS = os.path.join(HERE, "constituents_history.csv")
CONFIRM_AFTER_RUNS = 3          # absences before "suspected" becomes "confirmed"

VALID_REASONS = ("acquired", "delisted", "renamed", "index_removal", "fetch_failure", "UNKNOWN")


def _load(path=None):
    try:
        with open(path or STORE, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("names", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(names, path=None):
    with open(path or STORE, "w", encoding="utf-8") as f:
        json.dump({"_meta": {
            "purpose": "Why a ticker's series stopped. An acquired name is a WINNER; a fetch "
                       "failure is no information at all. Storing them identically biases "
                       "every study downward.",
            "reason_vocabulary": list(VALID_REASONS),
            "confirm_after_runs": CONFIRM_AFTER_RUNS,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "reason_policy": "NEVER inferred. A reason is set only by a human or by a corporate-"
                             "actions source. UNKNOWN is the honest default and is reported as "
                             "an open question, not resolved to a plausible guess.",
        }, "names": names}, f, indent=2, sort_keys=True)


def update(constituents_path=None, store_path=None, prices=None):
    """Walk the point-in-time constituent history and record appearances/disappearances."""
    import pandas as pd
    cp = constituents_path or CONSTITUENTS
    if not os.path.exists(cp):
        return {"ok": False, "reason": f"{os.path.basename(cp)} not found — §Q2 needs the PIT "
                                       f"constituent history to know what WAS there"}
    df = pd.read_csv(cp, low_memory=False)
    for col in ("run_date", "group", "ticker"):
        if col not in df.columns:
            return {"ok": False, "reason": f"constituents_history missing column {col!r}"}
    names = _load(store_path)
    new_gone, returned = [], []

    for group, gdf in df.groupby("group"):
        run_dates = sorted(gdf["run_date"].astype(str).unique())
        if len(run_dates) < 2:
            continue                       # a single run cannot show a disappearance
        seen_by_run = {rd: set(gdf[gdf.run_date.astype(str) == rd]["ticker"].astype(str))
                       for rd in run_dates}
        for i in range(1, len(run_dates)):
            prev, cur = run_dates[i - 1], run_dates[i]
            for tk in sorted(seen_by_run[prev] - seen_by_run[cur]):
                key = f"{group}|{tk}"
                rec = names.get(key) or {
                    "group": group, "ticker": tk, "first_seen": run_dates[0],
                    "status": "suspected_gone", "reason": "UNKNOWN",
                    "reason_source": None, "terminal_return_pct": None,
                    "last_price": None, "runs_missing": 0,
                }
                rec["last_seen"] = prev
                rec.setdefault("first_missing", cur)
                rec["runs_missing"] = sum(1 for rd in run_dates
                                          if rd >= rec["first_missing"] and tk not in seen_by_run[rd])
                if rec["status"] in ("suspected_gone", "confirmed_gone"):
                    rec["status"] = ("confirmed_gone" if rec["runs_missing"] >= CONFIRM_AFTER_RUNS
                                     else "suspected_gone")
                if key not in names:
                    new_gone.append(rec)
                names[key] = rec
            # a name that comes back was never gone — say so rather than leaving a stale record
            for tk in sorted(seen_by_run[cur] - seen_by_run[prev]):
                key = f"{group}|{tk}"
                if key in names and names[key]["status"] != "returned":
                    names[key]["status"] = "returned"
                    names[key]["returned_on"] = cur
                    names[key]["reason"] = "fetch_failure"
                    names[key]["reason_source"] = (
                        "self-evident: the ticker reappeared in a later run, so the absence was "
                        "never a delisting")
                    returned.append(names[key])

    if prices:
        for key, rec in names.items():
            p = prices.get(rec["ticker"])
            if p and rec.get("last_price") is None:
                rec["last_price"] = p
    _save(names, store_path)
    live = [r for r in names.values() if r["status"] in ("suspected_gone", "confirmed_gone")]
    return {"ok": True, "tracked": len(names), "gone": len(live),
            "confirmed": sum(1 for r in live if r["status"] == "confirmed_gone"),
            "suspected": sum(1 for r in live if r["status"] == "suspected_gone"),
            "returned": sum(1 for r in names.values() if r["status"] == "returned"),
            "unknown_reason": sum(1 for r in live if r["reason"] == "UNKNOWN"),
            "new_this_run": [r["ticker"] for r in new_gone],
            "reappeared": [r["ticker"] for r in returned]}


def set_reason(group, ticker, reason, source, terminal_return_pct=None, store_path=None):
    """Record WHY a name went. Refuses an unknown vocabulary term and refuses an unsourced
    reason — an unsourced 'acquired' is exactly the plausible-but-unverified value this
    framework keeps being damaged by."""
    if reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of {VALID_REASONS}, got {reason!r}")
    if reason != "UNKNOWN" and not str(source or "").strip():
        raise ValueError("a reason other than UNKNOWN requires a source — an unsourced "
                         "corporate-action claim is not evidence")
    names = _load(store_path)
    key = f"{group}|{ticker}"
    if key not in names:
        raise KeyError(f"{key} is not in the registry — it has not been observed to disappear")
    names[key].update(reason=reason, reason_source=source,
                      reason_set_on=dt.date.today().isoformat())
    if terminal_return_pct is not None:
        names[key]["terminal_return_pct"] = float(terminal_return_pct)
    _save(names, store_path)
    return names[key]


def _selftest():
    import tempfile, pandas as pd
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "c.csv"); sp = os.path.join(td, "s.json")
        rows = []
        for rd, tickers in [("2026-01-01", ["A", "B", "C", "D"]),
                            ("2026-01-08", ["A", "B", "D"]),          # C gone
                            ("2026-01-15", ["A", "B", "D"]),
                            ("2026-01-22", ["A", "B"])]:              # D gone
            for t in tickers:
                rows.append({"run_date": rd, "group": "G", "ticker": t})
        # E disappears once then comes back — the common case, and it must NOT read as gone
        rows += [{"run_date": "2026-01-01", "group": "G", "ticker": "E"},
                 {"run_date": "2026-01-15", "group": "G", "ticker": "E"},
                 {"run_date": "2026-01-22", "group": "G", "ticker": "E"}]
        pd.DataFrame(rows).to_csv(cp, index=False)

        r = update(cp, sp)
        assert r["ok"], r
        names = _load(sp)
        assert "G|C" in names and "G|D" in names, "disappearances not recorded"
        assert names["G|C"]["status"] == "confirmed_gone", names["G|C"]
        assert names["G|C"]["runs_missing"] >= CONFIRM_AFTER_RUNS
        assert names["G|D"]["status"] == "suspected_gone", \
            "one absence must be SUSPECTED, not confirmed — a fetch failure is commoner than a "\
            "delisting"
        assert names["G|E"]["status"] == "returned" and names["G|E"]["reason"] == "fetch_failure"
        assert all(v["reason"] == "UNKNOWN" for k, v in names.items()
                   if v["status"] in ("suspected_gone", "confirmed_gone")), \
            "a reason must NEVER be inferred"
        assert r["unknown_reason"] == 2

        # idempotent
        before = json.dumps(_load(sp), sort_keys=True)
        update(cp, sp)
        assert json.dumps(_load(sp), sort_keys=True) == before, "update() is not idempotent"

        rec = set_reason("G", "C", "acquired", "LSE RNS 2026-01-09, 42% cash premium",
                         terminal_return_pct=42.0, store_path=sp)
        assert rec["reason"] == "acquired" and rec["terminal_return_pct"] == 42.0
        for bad, exc in ((("G", "C", "went_bust", "x"), ValueError),
                         (("G", "C", "acquired", ""), ValueError),
                         (("G", "ZZZ", "acquired", "src"), KeyError)):
            try:
                set_reason(*bad, store_path=sp); raise AssertionError(f"{bad} should have failed")
            except exc:
                pass
        # a set reason must survive a later update
        update(cp, sp)
        assert _load(sp)["G|C"]["reason"] == "acquired", "a recorded reason was overwritten"
        # missing input is a stated reason, not a crash and not an empty pass
        bad = update(os.path.join(td, "nope.csv"), sp)
        assert bad["ok"] is False and "not found" in bad["reason"]
    print("SELFTEST PASS — 15 assertions (disappearance detected, 3-run confirmation threshold, "
          "single absence stays SUSPECTED, reappearance resolves to fetch_failure, reason never "
          "inferred, idempotent, vocabulary enforced, unsourced reason refused, unknown ticker "
          "refused, recorded reason survives re-runs, absent input states its reason)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--set-reason", nargs=4, metavar=("GROUP", "TICKER", "REASON", "SOURCE"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    if a.set_reason:
        print(json.dumps(set_reason(*a.set_reason), indent=2)); sys.exit(0)
    r = update()
    print(f"DELISTING_REGISTRY ok={r.get('ok')} tracked={r.get('tracked')} "
          f"gone={r.get('gone')} (confirmed {r.get('confirmed')}, suspected {r.get('suspected')}) "
          f"returned={r.get('returned')} reason_UNKNOWN={r.get('unknown_reason')}")
    if r.get("reason") and not r.get("ok"):
        print(f"  {r['reason']}")
    if r.get("unknown_reason"):
        print("  ⚑ Names with an UNKNOWN reason are OPEN QUESTIONS, not losses. An acquired "
              "name is a winner; recording it as a failure biases every study downward. "
              "Resolve with: --set-reason GROUP TICKER acquired '<source>'")
