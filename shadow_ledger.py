#!/usr/bin/env python3
"""
shadow_ledger.py — Capture Layer Item 4. 02-Aug-2026.

WHAT THIS IS
------------
Each month's action stack is FROZEN immutably at run time. Three sizing policies are then
computed over the SAME frozen stack, so the only thing that differs between them is the sizing
logic — not the names, not the timing, not hindsight.

  Book A  equal-weight top-N          null hypothesis
  Book B  confidence-scaled           tests: does conviction carry information?  (A vs B)
  Book C  max-size top-1/top-2        tests: what does concentration cost or earn? (A vs C)

Marked monthly against cash (the MMF Raj actually sweeps to), VUAG, IWMO and an equal-weight
benchmark of the stack itself.

REPORTING DISCIPLINE (build order, explicit)
--------------------------------------------
Report **top-3 excess, hit rate, dispersion and drawdown — NOT IC.** IC over 4-6 names a month
is noise wearing a decimal point; it invites a precision the sample cannot support. The
questions here are "did the top of the list beat the alternatives" and "how often", which small
samples can actually answer.

**Do not add a fourth or fifth sizing policy.** The books hold the same names and are ~90%
correlated. More policies means the winner is chosen by which names worked, not which logic was
right — you would be running a horse race between three descriptions of the same horse.

WHAT IT RECORDS PER RECOMMENDATION
-----------------------------------
timestamp, rank, conviction, size under each policy, entry level and **whether it was actually
reachable**, exit trigger, realised outcome, and **what Raj actually did** (behavioural
divergence). The last one matters most: a framework that is right while its owner does
something else has a different problem from one that is wrong.

HONESTY CONSTRAINTS
-------------------
* Returns are computed in GBP. A GBP investor's return on a USD name is not its USD price
  change, and a book of mixed-currency names marked in local currency would silently attribute
  FX to skill. `fx_applied` is recorded per name so this can never be assumed.
* A cohort is IMMUTABLE. Re-freezing a month with different content is REFUSED, never merged —
  a shadow book you can edit after the fact measures nothing.
* No policy here sizes a real trade, and nothing in this module feeds a score, gate or ranking
  (build hazard H7).

CLI:
  python3 shadow_ledger.py --freeze aug_2026
  python3 shadow_ledger.py --mark --asof 2026-08-01
  python3 shadow_ledger.py --report
  python3 shadow_ledger.py --selftest
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, statistics, sys
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "shadow_ledger.json")
PRICE_CACHE = os.path.join(HERE, "shadow_prices.json")
SCHEMA_VERSION = 1

# Book A's N. Four is not arbitrary: it is the cohort size the July-2026 assessment already
# used ("top-4 BUYs mean -2.65% to 29-Jul"), so the backfill is comparable to the number Raj
# has already seen. Registered as SHADOW-1 rather than tuned.
TOP_N = 4

# Book C's concentration. "max-size top-1/top-2" -> the maximum single-name weight goes to
# rank 1 and the remainder to rank 2. 0.60/0.40 rather than 1.0/0.0 because a one-name book
# is a coin flip, not a policy, and would make A vs C a test of one stock per month.
BOOK_C_WEIGHTS = (0.60, 0.40)

BENCHMARKS = {
    "cash":  "CSH2.L",   # the MMF Raj actually sweeps to — the true opportunity cost of holding
    "vuag":  "VUAG.L",
    "iwmo":  "IWMO.L",
}

# currency -> yfinance FX pair quoting UNITS OF THAT CURRENCY PER GBP
FX_PAIRS = {
    "USD": "GBPUSD=X", "EUR": "GBPEUR=X", "CHF": "GBPCHF=X", "SEK": "GBPSEK=X",
    "NOK": "GBPNOK=X", "DKK": "GBPDKK=X", "CAD": "GBPCAD=X", "JPY": "GBPJPY=X",
    "AUD": "GBPAUD=X",
    # Warsaw was absent from BOTH the suffix table and this map, so BFT.WA - a live
    # watchlist name - resolved to USD and would have been marked in the paper books at the
    # dollar rate. Same defect class as the entry_currency default: an unlisted venue
    # silently inheriting the fallback.
    "PLN": "GBPPLN=X", "HUF": "GBPHUF=X", "CZK": "GBPCZK=X",
}
GBP_LIKE = {"GBP", "GBp", "GBX"}


class ImmutableCohortError(RuntimeError):
    """Raised when a re-freeze would change an already-frozen month."""


# ── freezing ─────────────────────────────────────────────────────────────────────────────

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _stack_hash(rows):
    payload = json.dumps([{k: r.get(k) for k in ("ticker", "action", "rank", "source_score")}
                          for r in rows], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _find(here, *names):
    for n in names:
        for p in (os.path.join(here, n),
                  os.path.join(here, "archive", "decision_capture", n)):
            if os.path.exists(p):
                return p
    return None


def build_cohort(month_label, here=None, action_stack=None, conviction=None,
                 watchlist=None, ledger=None, run_date=None):
    """Assemble one month's frozen cohort from the run's own outputs."""
    here = here or HERE
    if action_stack is None:
        p = _find(here, f"action_stack_{month_label}.json")
        action_stack = _load(p, {}) if p else {}
    if conviction is None:
        p = _find(here, f"step9_conviction_{month_label}.json")
        conviction = _load(p, {}) if p else {}
    if watchlist is None:
        watchlist = _load(os.path.join(here, "watchlist_tickers.json"), {})
    if ledger is None:
        ledger = _load(os.path.join(here, "decision_ledger.json"), {})

    stack = action_stack.get("stack") or (action_stack if isinstance(action_stack, list) else [])
    run_date = run_date or action_stack.get("run_date") or _month_first(month_label)

    conv_by = {n.get("ticker"): n for n in (conviction.get("names") or [])}
    entry_by = {}
    for e in (watchlist.get("watchlist") or []) + (watchlist.get("candidate_pool") or []):
        if e.get("ticker"):
            entry_by[e["ticker"]] = e
    led_by = {}
    for e in (ledger.get("entries") or []):
        if e.get("ticker"):
            led_by.setdefault(e["ticker"], []).append(e)

    # Only BUY-side recommendations form the books. A SELL is not a sizing decision and
    # including it would let a correct exit flatter a book that never deployed capital.
    # 02-Aug-2026: START/STARTER was MISSING from this set. `action_language.CANONICAL_ACTIONS`
    # is ["BUY","START","ADD","TRIM","SELL","HOLD","WATCH"] — START is a deployment decision
    # ("open a partial position now"), and omitting it silently dropped 9 of 10 recommendations
    # from the Aug-2026 cohort the moment the gate fix shifted the stack from BUY-heavy to
    # STARTER-heavy. A book that quietly excludes most of the month's recommendations measures
    # nothing. TOP_UP is retained as the legacy spelling of ADD.
    _DEPLOY_ACTIONS = {"BUY", "START", "STARTER", "ADD", "TOP_UP"}
    buys = [r for r in stack
            if str(r.get("canonical_action") or r.get("action") or "").upper()
            in _DEPLOY_ACTIONS]
    buys.sort(key=lambda r: (r.get("rank") if r.get("rank") is not None else 999))

    recs = []
    for i, r in enumerate(buys, 1):
        tk = r.get("ticker")
        c = conv_by.get(tk) or {}
        w = entry_by.get(tk) or {}
        led = sorted(led_by.get(tk, []), key=lambda e: e.get("date", ""))
        latest = led[-1] if led else {}
        recs.append({
            "ticker": tk,
            "rank": i,
            "stack_rank": r.get("rank"),
            "action": r.get("canonical_action") or r.get("action"),
            "route": r.get("route"),
            "source_score": r.get("source_score"),
            "aps": r.get("aps"),
            "conviction_total": c.get("conviction_total"),
            "classification": c.get("classification"),
            "entry_level": w.get("entry_level"),
            "entry_currency": w.get("entry_currency") or w.get("currency"),
            "exit_trigger": (w.get("thesis_break") or r.get("cap")
                             or "not recorded at freeze time"),
            # BEHAVIOURAL DIVERGENCE — the field the whole exercise turns on.
            "raj_action": latest.get("execution_status") or "unknown",
            "raj_decision": latest.get("decision"),
            "divergence": None,       # filled at mark time once we know the outcome
        })

    weights = compute_weights(recs)
    for rec in recs:
        rec["size"] = {b: weights[b].get(rec["ticker"], 0.0) for b in ("A", "B", "C")}

    return {
        "month": month_label,
        "run_date": str(run_date),
        "frozen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stack_hash": _stack_hash(buys),
        "n_stack": len(stack),
        "n_buys": len(buys),
        "recommendations": recs,
        "policy_note": {
            "A": f"equal weight over the top {TOP_N} by stack rank",
            "B": "weight proportional to conviction_total (source_score where conviction was "
                 "not captured), normalised over the same top-N",
            "C": f"max-size concentration: {BOOK_C_WEIGHTS[0]:.0%} rank 1 / "
                 f"{BOOK_C_WEIGHTS[1]:.0%} rank 2",
        },
    }


def compute_weights(recs):
    """The three sizing policies. Same names, same order, different sizes — nothing else."""
    top = recs[:TOP_N]
    A, B, C = {}, {}, {}
    if not top:
        return {"A": A, "B": B, "C": C}

    # Book A — equal weight. The null.
    for r in top:
        A[r["ticker"]] = round(1.0 / len(top), 6)

    # Book B — confidence-scaled. conviction_total where the session recorded it, else the
    # source score. Falling back is stated per name via `_b_basis` rather than silently
    # substituted: a book claiming to test conviction while actually testing source score
    # would answer a different question and say nothing about it.
    raw = {}
    for r in top:
        v = r.get("conviction_total")
        basis = "conviction_total"
        if v is None:
            v = r.get("source_score")
            basis = "source_score_fallback"
        if v is None:
            v = 0.0
            basis = "none"
        raw[r["ticker"]] = max(float(v), 0.0)
        r["_b_basis"] = basis
    tot = sum(raw.values())
    for r in top:
        B[r["ticker"]] = (raw[r["ticker"]] / tot) if tot > 0 else (1.0 / len(top))
    # Absorb rounding into the last weight so the book sums to exactly 1.0. A book that sums
    # to 0.999999 quietly reports a return 0.0001% low every month forever.
    _ks = [r["ticker"] for r in top]
    B[_ks[-1]] += 1.0 - sum(B.values())
    for k in B:
        B[k] = round(B[k], 9)

    # Book C — concentration.
    for i, r in enumerate(top[:len(BOOK_C_WEIGHTS)]):
        C[r["ticker"]] = BOOK_C_WEIGHTS[i]
    if len(top) == 1:
        C[top[0]["ticker"]] = 1.0
    csum = sum(C.values())
    if csum and abs(csum - 1.0) > 1e-9:
        for k in C:
            C[k] = round(C[k] / csum, 6)
    return {"A": A, "B": B, "C": C}


def freeze(month_label, here=None, store=None, force=False, **kw):
    """Write a cohort immutably. Refuses to change an existing one."""
    store = store or (os.path.join(here, "shadow_ledger.json") if here else STORE)
    doc = _load(store, {"schema_version": SCHEMA_VERSION, "cohorts": {}, "marks": {}})
    doc.setdefault("cohorts", {})
    cohort = build_cohort(month_label, here=here, **kw)
    prev = doc["cohorts"].get(month_label)
    if prev and not force:
        if prev.get("stack_hash") != cohort["stack_hash"]:
            raise ImmutableCohortError(
                f"shadow_ledger: {month_label} is already frozen with stack_hash "
                f"{prev.get('stack_hash')}; the new stack hashes {cohort['stack_hash']}. "
                f"A cohort you can rewrite after the fact measures nothing. If the original "
                f"freeze was genuinely wrong, archive it under a new month key by hand.")
        return prev, False
    doc["cohorts"][month_label] = cohort
    _save(store, doc)
    return cohort, True


def _month_first(month_label):
    months = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
    try:
        mmm, yyyy = month_label.lower().split("_")
        return f"{int(yyyy):04d}-{months[mmm]:02d}-01"
    except Exception:
        return date.today().isoformat()


# ── prices (resumable, per the local yfinance recipe) ─────────────────────────────────────

def fetch_prices(tickers, start, end, cache=None, batch=12):
    """Daily close+low per ticker between start and end, cached on disk.

    Resumable by design: the 45-second bash ceiling means a full fetch may not finish in one
    call, and a non-resumable fetch would simply never complete.
    """
    import yfinance as yf
    cache = cache or PRICE_CACHE
    store = _load(cache, {})
    key = lambda t: f"{t}|{start}|{end}"                                    # noqa: E731
    todo = [t for t in dict.fromkeys(tickers) if key(t) not in store]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            df = yf.download(chunk, start=start, end=end, progress=False,
                             auto_adjust=True, group_by="ticker", threads=True)
        except Exception as e:
            for t in chunk:
                store[key(t)] = {"error": str(e)[:120]}
            continue
        for t in chunk:
            try:
                sub = df[t] if len(chunk) > 1 else df
                closes = sub["Close"].dropna()
                lows = sub["Low"].dropna() if "Low" in sub else closes
                if closes.empty:
                    store[key(t)] = {"error": "no data"}
                    continue
                store[key(t)] = {
                    "first": float(closes.iloc[0]), "last": float(closes.iloc[-1]),
                    "first_date": str(closes.index[0].date()),
                    "last_date": str(closes.index[-1].date()),
                    "min_low": float(lows.min()),
                    "path": [float(x) for x in closes.tolist()],
                }
            except Exception as e:
                store[key(t)] = {"error": str(e)[:120]}
        _save(cache, store)
    _save(cache, store)
    return {t: store.get(key(t), {"error": "not fetched"}) for t in tickers}


def _max_drawdown(path):
    if not path or len(path) < 2:
        return None
    peak, mdd = path[0], 0.0
    for p in path:
        peak = max(peak, p)
        if peak > 0:
            mdd = min(mdd, p / peak - 1.0)
    return round(mdd, 4)


_CCY_CONFLICTS = []   # stored-vs-derived currency disagreements, surfaced not swallowed


def _currency_of(ticker, explicit=None):
    """Resolve the quote currency for FX conversion.

    02-Aug-2026 FIX — PRECEDENCE INVERTED. This used to return `explicit` unconditionally,
    so a stored `entry_currency` overrode the suffix table below. That stored field was
    wrong for 25 of 52 names (defaulted to "USD" and never corrected), which meant every
    non-USD recommendation in the paper books was converted to GBP at the DOLLAR rate:
    .L names as USD instead of GBp, .HE/.AS/.MC as USD instead of EUR, and so on. The
    shadow ledger is the instrument that measures whether the framework's recommendations
    work, so this corrupted the learning signal itself.

    The suffix table is deterministic and derived from the listing venue, so it is now
    authoritative wherever it resolves. `explicit` is used only for tickers the table
    cannot classify (bare US lines), and a conflict is reported rather than silently taken.
    """
    t = str(ticker).upper()
    _derived = None
    if t.endswith(".L"):
        _derived = "GBp"
    else:
        for _suf, _ccy in ((".MI", "EUR"), (".AS", "EUR"), (".DE", "EUR"), (".PA", "EUR"),
                           (".BR", "EUR"), (".HE", "EUR"), (".MC", "EUR"), (".LS", "EUR"),
                           (".ST", "SEK"), (".OL", "NOK"), (".CO", "DKK"), (".SW", "CHF"),
                           (".WA", "PLN"), (".VI", "EUR"), (".IR", "EUR"),
                           (".LS", "EUR"), (".BD", "HUF"), (".PR", "CZK"),
                           (".TO", "CAD"), (".T", "JPY"), (".AX", "AUD")):
            if t.endswith(_suf):
                _derived = _ccy
                break
    if _derived:
        if explicit and explicit != _derived:
            _CCY_CONFLICTS.append({"ticker": t, "stored": explicit, "used": _derived})
        return _derived
    if explicit:
        return explicit
    if t.endswith(".L"):
        return "GBp"
    for suf, ccy in ((".MI", "EUR"), (".AS", "EUR"), (".DE", "EUR"), (".PA", "EUR"),
                     (".BR", "EUR"), (".HE", "EUR"), (".MC", "EUR"), (".LS", "EUR"),
                     (".ST", "SEK"), (".OL", "NOK"), (".CO", "DKK"), (".SW", "CHF"),
                     (".TO", "CAD"), (".T", "JPY"), (".AX", "AUD")):
        if t.endswith(suf):
            return ccy
    return "USD"


# ── marking ──────────────────────────────────────────────────────────────────────────────

def mark(asof=None, here=None, store=None, cache=None, fetch=True):
    """Mark every frozen cohort to `asof`, in GBP, and compute the three book returns."""
    here = here or HERE
    store = store or os.path.join(here, "shadow_ledger.json")
    cache = cache or os.path.join(here, "shadow_prices.json")
    asof = asof or date.today().isoformat()
    doc = _load(store, {})
    cohorts = doc.get("cohorts") or {}
    if not cohorts:
        return {"error": "no cohorts frozen"}

    # every ticker + benchmarks + FX pairs, per cohort window
    marks = {}
    for month, coh in sorted(cohorts.items()):
        start = coh["run_date"][:10]
        names = [r["ticker"] for r in coh["recommendations"]]
        ccys = {_currency_of(r["ticker"], r.get("entry_currency")) for r in coh["recommendations"]}
        fx_needed = [FX_PAIRS[c] for c in ccys if c in FX_PAIRS]
        want = names + list(BENCHMARKS.values()) + fx_needed
        px = (fetch_prices(want, start, asof, cache=cache) if fetch
              else {t: _load(cache, {}).get(f"{t}|{start}|{asof}", {"error": "not fetched"})
                    for t in want})

        def gbp_return(tk, ccy):
            d = px.get(tk) or {}
            if "error" in d or not d.get("first"):
                return None, False, d.get("error", "missing")
            local = d["last"] / d["first"] - 1.0
            if ccy in GBP_LIKE:
                return local, True, None
            pair = FX_PAIRS.get(ccy)
            f = px.get(pair) or {}
            if not pair or "error" in f or not f.get("first"):
                # Return the LOCAL number but flag it. Silently passing a local-currency
                # return off as a GBP one would attribute FX to skill.
                return local, False, f"FX {pair or ccy} unavailable — LOCAL currency return"
            # pair quotes units of ccy per GBP, so a rising pair is a WEAKER foreign currency
            return (1 + local) * (f["first"] / f["last"]) - 1.0, True, None

        per_name = []
        for r in coh["recommendations"]:
            ccy = _currency_of(r["ticker"], r.get("entry_currency"))
            ret, fx_ok, note = gbp_return(r["ticker"], ccy)
            d = px.get(r["ticker"]) or {}
            entry = r.get("entry_level")
            reachable = None
            if entry is not None and d.get("min_low") is not None:
                try:
                    reachable = bool(float(d["min_low"]) <= float(entry))
                except Exception:
                    reachable = None
            per_name.append({
                "ticker": r["ticker"], "rank": r["rank"], "currency": ccy,
                "return_gbp": None if ret is None else round(ret, 4),
                "fx_applied": fx_ok, "note": note,
                "price_first": d.get("first"), "price_last": d.get("last"),
                "min_low": d.get("min_low"),
                "entry_level": entry,
                "entry_reachable": reachable,
                "max_drawdown": _max_drawdown(d.get("path")),
                "conviction_total": r.get("conviction_total"),
                "size": r.get("size"),
                "raj_action": r.get("raj_action"),
                # BEHAVIOURAL DIVERGENCE: the framework recommended it; did Raj act?
                "divergence": ("not_executed" if r.get("raj_action") in
                               ("recommended", "no_action_expected", "unknown", None)
                               else "executed"),
            })

        books = {}
        for b in ("A", "B", "C"):
            tot, wsum, missing = 0.0, 0.0, []
            for n in per_name:
                w = (n["size"] or {}).get(b, 0.0)
                if not w:
                    continue
                if n["return_gbp"] is None:
                    missing.append(n["ticker"])
                    continue
                tot += w * n["return_gbp"]
                wsum += w
            books[b] = {
                "return": round(tot / wsum, 4) if wsum else None,
                "weight_covered": round(wsum, 4),
                "missing_prices": missing,
            }

        bench = {}
        for label, tk in BENCHMARKS.items():
            r, fx_ok, note = gbp_return(tk, _currency_of(tk))
            bench[label] = {"ticker": tk, "return": None if r is None else round(r, 4),
                            "fx_applied": fx_ok, "note": note}
        rets = [n["return_gbp"] for n in per_name if n["return_gbp"] is not None]
        bench["equal_weight_stack"] = {
            "ticker": "(all BUYs, equal weight)",
            "return": round(sum(rets) / len(rets), 4) if rets else None,
            "fx_applied": all(n["fx_applied"] for n in per_name) if per_name else None,
        }

        top3 = [n["return_gbp"] for n in sorted(per_name, key=lambda x: x["rank"])[:3]
                if n["return_gbp"] is not None]
        top3_mean = (sum(top3) / len(top3)) if top3 else None
        ew = bench["equal_weight_stack"]["return"]

        marks[month] = {
            "asof": asof,
            "frozen_run_date": coh["run_date"],
            "stack_hash": coh["stack_hash"],
            "per_name": per_name,
            "books": books,
            "benchmarks": bench,
            # THE REPORTED STATISTICS — top-3 excess, hit rate, dispersion, drawdown. NOT IC.
            "statistics": {
                "top3_mean_return": None if top3_mean is None else round(top3_mean, 4),
                "top3_excess_vs_equal_weight":
                    None if (top3_mean is None or ew is None) else round(top3_mean - ew, 4),
                "top3_excess_vs_vuag":
                    None if (top3_mean is None or bench["vuag"]["return"] is None)
                    else round(top3_mean - bench["vuag"]["return"], 4),
                "hit_rate_vs_cash":
                    None if not rets or bench["cash"]["return"] is None
                    else round(sum(1 for r in rets if r > bench["cash"]["return"]) / len(rets), 3),
                "hit_rate_positive":
                    None if not rets else round(sum(1 for r in rets if r > 0) / len(rets), 3),
                "dispersion_stdev":
                    None if len(rets) < 2 else round(statistics.pstdev(rets), 4),
                "worst_name_drawdown":
                    min([n["max_drawdown"] for n in per_name
                         if n["max_drawdown"] is not None] or [0]) or None,
                "n_priced": len(rets), "n_recommended": len(per_name),
                "n_executed_by_raj": sum(1 for n in per_name if n["divergence"] == "executed"),
                "note": "top-3 excess / hit rate / dispersion / drawdown by design. IC is NOT "
                        "reported: over 4-6 names a month it is noise with a decimal point.",
            },
        }

    doc["marks"] = marks
    doc["marked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save(store, doc)
    return marks


def report(store=None, here=None):
    store = store or (os.path.join(here, "shadow_ledger.json") if here else STORE)
    doc = _load(store, {})
    marks = doc.get("marks") or {}
    lines = []
    if not marks:
        return "shadow_ledger: no marks yet — run --freeze then --mark."
    lines.append(f"SHADOW LEDGER — marked {doc.get('marked_at')}")
    lines.append("Books: A equal-weight top-%d | B confidence-scaled | C concentrated %s"
                 % (TOP_N, "/".join(f"{w:.0%}" for w in BOOK_C_WEIGHTS)))
    for month, m in sorted(marks.items()):
        s, b = m["statistics"], m["books"]
        lines.append("")
        lines.append(f"[{month}]  frozen {m['frozen_run_date']}  ->  {m['asof']}   "
                     f"({s['n_priced']}/{s['n_recommended']} priced, "
                     f"{s['n_executed_by_raj']} executed by Raj)")
        for k in ("A", "B", "C"):
            r = b[k]["return"]
            lines.append(f"   Book {k}: {('%+.2f%%' % (r*100)) if r is not None else '   n/a'}"
                         f"   (weight covered {b[k]['weight_covered']:.0%})")
        for label in ("cash", "vuag", "iwmo", "equal_weight_stack"):
            v = m["benchmarks"][label]["return"]
            lines.append(f"   {label:<20} {('%+.2f%%' % (v*100)) if v is not None else '  n/a'}")
        lines.append(f"   top-3 excess vs EW: "
                     f"{('%+.2f%%' % (s['top3_excess_vs_equal_weight']*100)) if s['top3_excess_vs_equal_weight'] is not None else 'n/a'}"
                     f"   vs VUAG: "
                     f"{('%+.2f%%' % (s['top3_excess_vs_vuag']*100)) if s['top3_excess_vs_vuag'] is not None else 'n/a'}")
        lines.append(f"   hit rate (>0): {s['hit_rate_positive']}   "
                     f"(>cash): {s['hit_rate_vs_cash']}   "
                     f"dispersion: {s['dispersion_stdev']}   "
                     f"worst drawdown: {s['worst_name_drawdown']}")
        unreached = [n["ticker"] for n in m["per_name"] if n["entry_reachable"] is False]
        if unreached:
            lines.append(f"   entry never reachable: {unreached}")
        nofx = [n["ticker"] for n in m["per_name"] if not n["fx_applied"]]
        if nofx:
            lines.append(f"   LOCAL-CURRENCY (FX not applied, do not read as GBP): {nofx}")
    lines.append("")
    lines.append("A vs B answers: does confidence carry information?   "
                 "A vs C answers: what does concentration cost or earn?")
    lines.append("Two cohorts is not evidence. It is the clock starting.")
    return "\n".join(lines)


# ── self-test ────────────────────────────────────────────────────────────────────────────

def _selftest():
    import tempfile
    fails = []

    def ok(label, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(label)

    stack = {"run_date": "2026-08-01", "stack": [
        {"ticker": "SELLME", "action": "SELL", "rank": 1, "source_score": 32.6},
        {"ticker": "AAA", "action": "BUY", "rank": 2, "source_score": 70.8},
        {"ticker": "BBB", "action": "BUY", "rank": 3, "source_score": 70.4},
        {"ticker": "CCC", "action": "BUY", "rank": 4, "source_score": 59.1},
        {"ticker": "DDD", "action": "BUY", "rank": 5, "source_score": 52.2},
        {"ticker": "EEE", "action": "BUY", "rank": 6, "source_score": 50.0},
    ]}
    conv = {"names": [{"ticker": "AAA", "conviction_total": 48, "classification": "High"},
                      {"ticker": "BBB", "conviction_total": 40, "classification": "Medium"},
                      {"ticker": "CCC", "conviction_total": 36, "classification": "Medium"},
                      {"ticker": "DDD", "conviction_total": 32, "classification": "Watch"}]}
    wl = {"watchlist": [{"ticker": "AAA", "entry_level": 10.0, "entry_currency": "USD"}]}
    led = {"entries": [{"ticker": "AAA", "date": "2026-08-02", "decision": "BUY",
                        "execution_status": "confirmed_executed"},
                       {"ticker": "BBB", "date": "2026-08-02", "decision": "BUY",
                        "execution_status": "recommended"}]}

    c = build_cohort("aug_2026", action_stack=stack, conviction=conv, watchlist=wl, ledger=led)
    ok("U-SL1 SELLs excluded — a book measures deployment, not exits", c["n_buys"] == 5)
    ok("U-SL2 ranks re-based over BUYs only",
       [r["ticker"] for r in c["recommendations"][:2]] == ["AAA", "BBB"])

    wsum = {b: round(sum(r["size"][b] for r in c["recommendations"]), 6) for b in "ABC"}
    ok("U-SL3 every book sums to 1.0", all(abs(v - 1.0) < 1e-6 for v in wsum.values()), str(wsum))
    ok("U-SL4 Book A is equal-weight over the top N",
       all(abs(r["size"]["A"] - 0.25) < 1e-6 for r in c["recommendations"][:TOP_N]))
    ok("U-SL5 Book A holds exactly N names",
       sum(1 for r in c["recommendations"] if r["size"]["A"] > 0) == TOP_N)
    ok("U-SL6 Book B is monotone in conviction",
       c["recommendations"][0]["size"]["B"] > c["recommendations"][3]["size"]["B"])
    ok("U-SL7 Book B used conviction, not source score",
       c["recommendations"][0]["_b_basis"] == "conviction_total")
    ok("U-SL8 Book C concentrates into the top two",
       abs(c["recommendations"][0]["size"]["C"] - 0.60) < 1e-6
       and abs(c["recommendations"][2]["size"]["C"]) < 1e-9)
    ok("U-SL9 the SAME names across all three books",
       {r["ticker"] for r in c["recommendations"] if r["size"]["A"]}
       >= {r["ticker"] for r in c["recommendations"] if r["size"]["C"]})

    # Book B must SAY when it fell back — otherwise it silently tests source score instead.
    c2 = build_cohort("aug_2026", action_stack=stack, conviction={"names": []},
                      watchlist=wl, ledger=led)
    ok("U-SL10 Book B declares a source_score fallback",
       all(r["_b_basis"] == "source_score_fallback" for r in c2["recommendations"][:TOP_N]))

    ok("U-SL11 behavioural divergence captured from the ledger",
       c["recommendations"][0]["raj_action"] == "confirmed_executed"
       and c["recommendations"][1]["raj_action"] == "recommended")
    ok("U-SL12 entry level carried for reachability", c["recommendations"][0]["entry_level"] == 10.0)

    # immutability
    with tempfile.TemporaryDirectory() as td:
        st = os.path.join(td, "sl.json")
        _, w1 = freeze("aug_2026", store=st, action_stack=stack, conviction=conv,
                       watchlist=wl, ledger=led)
        ok("U-SL13 first freeze writes", w1)
        _, w2 = freeze("aug_2026", store=st, action_stack=stack, conviction=conv,
                       watchlist=wl, ledger=led)
        ok("U-SL14 identical re-freeze is a no-op", not w2)
        changed = json.loads(json.dumps(stack))
        changed["stack"][1]["ticker"] = "ZZZ"
        raised = False
        try:
            freeze("aug_2026", store=st, action_stack=changed, conviction=conv,
                   watchlist=wl, ledger=led)
        except ImmutableCohortError:
            raised = True
        ok("U-SL15 a DIFFERENT re-freeze is REFUSED (a rewritable book measures nothing)",
           raised)
        back = _load(st, {})
        ok("U-SL15b original cohort untouched",
           back["cohorts"]["aug_2026"]["recommendations"][0]["ticker"] == "AAA")

    ok("U-SL16 exactly three books — no fourth policy",
       set(compute_weights(c["recommendations"]).keys()) == {"A", "B", "C"})

    # FX: currency inference
    ok("U-SL17 currency inferred from suffix",
       _currency_of("VUAG.L") == "GBp" and _currency_of("RACE.MI") == "EUR"
       and _currency_of("AUPH") == "USD" and _currency_of("LOOMIS.ST") == "SEK")

    ok("U-SL18 drawdown computed from the price path",
       _max_drawdown([100, 120, 60, 90]) == -0.5, str(_max_drawdown([100, 120, 60, 90])))
    ok("U-SL18b flat path has no drawdown", _max_drawdown([100, 100, 100]) == 0.0)

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)}) {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", help="month_label, e.g. aug_2026")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--asof")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.freeze:
        c, written = freeze(a.freeze)
        print(f"SHADOW_FREEZE month={a.freeze} buys={c['n_buys']} hash={c['stack_hash']} "
              f"{'WRITTEN' if written else 'ALREADY FROZEN (identical)'}")
    if a.mark:
        m = mark(asof=a.asof, fetch=not a.no_fetch)
        if isinstance(m, dict) and m.get("error"):
            print("ERROR: " + m["error"])
            return 1
        print(f"SHADOW_MARK cohorts={len(m)} asof={a.asof or date.today().isoformat()}")
    if a.report:
        print(report())
    if not (a.freeze or a.mark or a.report):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
