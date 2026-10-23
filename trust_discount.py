#!/usr/bin/env python3
"""
trust_discount.py — closed-end discount / premium. Tier-1 item 3, built 06-Aug-2026.

⚑ THE FACT THE FRAMEWORK COULD NOT EXPRESS
Scottish Mortgage is a closed-end INVESTMENT TRUST. Its shares trade at a price set by the
market, which is not the value of what it owns. On 05-Aug-2026 Raj's chart showed price 13.83
against NAV 15.16 — an **~8.8% discount**, roughly **£640 of underlying assets** attached to a
£6,626 holding that the framework had no field for.

That absence is not cosmetic; it corrupts two things at once.

**1. It corrupts the measurement.** `fund_performance` pulls SMT's price from yfinance and treats
it exactly like an OEIC's NAV. For a trust:

        price return  =  NAV return  +  change in the discount

so a widening discount depresses the measured return with the manager having done nothing, and a
narrowing one flatters it. This is the likeliest cause of the one residual the golden source
never resolved: at the X-Ray's OWN strike date the two sources agree to 2dp on JPM UK (11.48),
RLGES (16.53), VUAG (13.98) and Vanguard Japan (10.22) — and disagree on SMT alone, by 2.48pp.
Four exact agreements and one exception in the only closed-end holding is not a coincidence.
(The same shape appears at ten years: this framework computes 15.97% from price, Morningstar
publishes 16.7%.)

**2. It corrupts the decision.** Selling a trust at a discount CRYSTALLISES that discount — you
hand over £7,266 of assets and receive £6,626. Buying at one does the reverse. "Every pound of
capital deployed must earn its place" cannot be assessed on a holding whose market value and
asset value differ by 8.8% while the framework can only see one of them.

⚑ WHAT THIS DELIBERATELY DOES NOT DO
It does not score the discount. The right question is not "is 8.8% a discount" but "is 8.8% WIDE
or NARROW for this trust" — and that needs a discount history nobody has been keeping. There is
no NAV feed in the sandbox and no retrievable NAV series, so a z-score would have to be invented.
The clock starts here instead: observations are DECLARED with a date and a source, the history
accumulates monthly, and the statistical question is answerable once N permits. Capture now,
analyse later — the same discipline the register applies to every other measurement it lacks.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from isa_metric import Metric, Missing, is_present, as_dict     # noqa: E402

SCHEMA_VERSION = 1
OBS_PATH = os.path.join(HERE, "trust_nav_observations.json")
# ⚑ A DISCOUNT IS A PROPERTY OF ONE MOMENT. price/NAV taken from two different dates is not a
# discount, it is a discount plus whatever the price did in between — a value that says "today"
# and IS a mixture, which is the failure mode this register is a list of. So the MATCHED pair
# inside a single observation is always preferred, and an unmatched comparison is allowed only
# across a few days and is labelled as approximate.
MAX_PRICE_NAV_GAP_DAYS = 3
MIN_OBS_FOR_STATS = 12           # below this, no percentile or z-score is published


def load_observations(path=OBS_PATH):
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "observations": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_observations(store, path=OBS_PATH):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=1, default=str)
    return path


def record_observation(store, sedol, as_of, nav, source, price=None,
                       as_of_confidence="stated", note=""):
    """Append a dated NAV observation. Content-keyed so re-running a month cannot inflate the
    history — the same defect `intelligence_store` was built to avoid."""
    key = f"{sedol}|{as_of}|{nav}"
    if any(o.get("_key") == key for o in store["observations"]):
        return store, False
    store["observations"].append({
        "_key": key, "sedol": sedol, "as_of": str(as_of)[:10], "nav": float(nav),
        "price": (float(price) if price is not None else None), "source": source,
        "as_of_confidence": as_of_confidence, "note": note,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    store["observations"].sort(key=lambda o: (o["sedol"], o["as_of"]))
    return store, True


def is_closed_end(u):
    """A trust is DECLARED closed-end in fund_universe, never inferred from its name or its
    ticker. 'Ord' in a name and a .L suffix are both true of things that are not trusts."""
    return (u or {}).get("structure") == "closed_end"


def discount(sedol, universe, price, price_date, store=None, as_of=None):
    """-> dict with a Metric|Missing discount. Never returns 0.0 for 'unknown'."""
    u = (universe or {}).get(sedol) or {}
    src_name = f"trust_nav_observations:{sedol}"
    out = {"sedol": sedol, "name": u.get("name"), "structure": u.get("structure"),
           "closed_end": is_closed_end(u), "price": price,
           "price_date": price_date.isoformat() if hasattr(price_date, "isoformat")
                         else price_date}
    if not is_closed_end(u):
        out["discount_pct"] = as_dict(Missing(
            "not a closed-end vehicle — an open-ended fund deals AT net asset value, so a "
            "discount does not exist for it", out["price_date"], "fund_universe"))
        out["applicable"] = False
        return out
    out["applicable"] = True
    store = store if store is not None else load_observations()
    obs = [o for o in store.get("observations", []) if o["sedol"] == sedol]
    # ⚑ POINT-IN-TIME (ISA-0127, 18-Oct-2026): a BUILD as of a date uses only observations on or before it.
    #   With one observation the rule was invisible; with an autonomous daily source a build 'as of 31-Jul'
    #   would otherwise read October's NAV. Production is unchanged (build's as_of = the run date). A direct
    #   call without as_of keeps the documented matched-pair preference across a broker-date gap (C3/C4).
    if as_of is not None:
        _ao = (as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of))[:10]
        obs = [o for o in obs if str(o.get("as_of"))[:10] <= _ao]
    obs.sort(key=lambda o: o["as_of"])
    if not obs:
        out["discount_pct"] = as_dict(Missing(
            "no NAV observation on record. There is no NAV feed for UK investment trusts in this "
            "pipeline; the value must be DECLARED from the manager's or the AIC's published "
            "figure. Absent it, the discount is unknown — it is NOT zero, and a trust's price "
            "return must not be read as its NAV return.",
            out["price_date"], src_name))
        out["what_would_resolve_it"] = (
            f"one entry in trust_nav_observations.json: sedol {sedol}, as_of, nav, source")
        return out
    latest = obs[-1]
    out["nav"] = latest["nav"]
    out["nav_as_of"] = latest["as_of"]
    out["nav_source"] = latest["source"]
    out["nav_as_of_confidence"] = latest.get("as_of_confidence")
    obs_price = latest.get("price")

    def _gap(a, b):
        try:
            aa = a if hasattr(a, "isoformat") else dt.date.fromisoformat(str(a)[:10])
            return (aa - dt.date.fromisoformat(str(b)[:10])).days
        except Exception:
            return None

    if obs_price:
        # matched pair — the only exact form
        d = (obs_price / latest["nav"] - 1.0) * 100.0
        out["basis"] = "matched_pair"
        out["discount_pct"] = as_dict(Metric(
            d, latest["as_of"], src_name, unit="%",
            note=f"price {obs_price:.4f} and NAV {latest['nav']:.4f}, BOTH as at "
                 f"{latest['as_of']} — a single-moment observation"))
        out["price_used"] = obs_price
        g = _gap(price_date, latest["as_of"])
        out["broker_price_gap_days"] = g
        if price and g is not None and abs(g) > 0:
            out["note_broker_price"] = (
                f"the broker valuation prices this holding at {price:.4f} on {out['price_date']}, "
                f"{abs(g)}d from the observation. The discount above is NOT recomputed against it "
                f"— price/NAV across two dates is not a discount.")
    else:
        g = _gap(price_date, latest["as_of"])
        out["nav_staleness_days"] = g
        if not price:
            out["discount_pct"] = as_dict(Missing("no market price supplied", out["price_date"],
                                                  src_name))
            return out
        if g is None or abs(g) > MAX_PRICE_NAV_GAP_DAYS:
            out["discount_pct"] = as_dict(Missing(
                f"the NAV on record ({latest['as_of']}) and the price ({out['price_date']}) are "
                f"{'an unknown number of' if g is None else abs(g)} days apart, beyond the "
                f"{MAX_PRICE_NAV_GAP_DAYS}d limit. Their ratio would be a discount plus the "
                f"price move in between, reported as though it were a discount.",
                out["price_date"], src_name))
            out["stale_nav"] = latest
            out["what_would_resolve_it"] = (
                f"a NAV observation dated within {MAX_PRICE_NAV_GAP_DAYS}d of the valuation "
                f"date, ideally recorded WITH its matching price")
            return out
        d = (price / latest["nav"] - 1.0) * 100.0
        out["basis"] = "approximate_unmatched"
        out["price_used"] = price
        out["discount_pct"] = as_dict(Metric(
            d, latest["as_of"], src_name, unit="%", confidence=0.8,
            note=f"price {price:.4f} @{out['price_date']} vs NAV {latest['nav']:.4f} "
                 f"@{latest['as_of']} — {abs(g)}d apart, approximate"))
    out["direction"] = "discount" if d < 0 else ("premium" if d > 0 else "par")
    out["nav_value_gbp_per_unit_gap"] = round(latest["nav"] - out["price_used"], 4)

    # ── history: reported, never scored until N permits ────────────────────────────────
    hist = [(o["as_of"], (o["price"] / o["nav"] - 1.0) * 100.0)
            for o in obs if o.get("price") and o.get("nav")]
    out["history"] = [{"as_of": a, "discount_pct": round(v, 2)} for a, v in hist]
    if len(hist) >= MIN_OBS_FOR_STATS:
        vals = sorted(v for _, v in hist)
        rank = sum(1 for v in vals if v < d) / len(vals) * 100.0
        out["history_percentile"] = round(rank, 1)
        out["history_n"] = len(hist)
    else:
        out["history_percentile"] = None
        out["history_n"] = len(hist)
        out["history_note"] = (
            f"{len(hist)} observation(s) on record, {MIN_OBS_FOR_STATS} needed before a "
            f"percentile is published. The question that matters is not whether "
            f"{abs(d):.1f}% is a discount but whether it is WIDE or NARROW for this trust, and "
            f"that is not answerable yet. A z-score computed on {len(hist)} points would be a "
            f"number, not evidence.")
    return out


def crystallisation_cost(disc, value_gbp):
    """What selling at the prevailing discount actually costs, in pounds.

    This is the whole practical point. A retain-vs-redeploy comparison that ignores the discount
    is comparing the price of the asset with the value of the alternative."""
    dd = disc.get("discount_pct") or {}
    if not dd.get("present") or not value_gbp:
        return {"applicable": bool(disc.get("applicable")), "cost_gbp": None,
                "reason": dd.get("reason", "no discount measured")}
    pct = dd["value"]
    underlying = value_gbp / (1.0 + pct / 100.0)
    basis = disc.get("basis")
    return {
        "applicable": True, "market_value_gbp": round(value_gbp, 2),
        "underlying_nav_value_gbp": round(underlying, 2),
        "cost_gbp": round(underlying - value_gbp, 2),
        "discount_pct": round(pct, 2), "discount_basis": basis,
        "statement": (
            f"selling at a {abs(pct):.1f}% {'discount' if pct < 0 else 'premium'} "
            f"{'FOREGOES' if pct < 0 else 'CAPTURES'} £{abs(underlying - value_gbp):,.0f} of "
            f"underlying asset value: £{value_gbp:,.0f} received against £{underlying:,.0f} of "
            f"assets. Whether that is a reason to wait depends on whether the discount is wide "
            f"or narrow for this trust — which needs a discount history this framework has only "
            f"just started keeping."),
        "not_a_verdict": ("a discount is NOT by itself a reason to hold. It is a real cost of "
                          "acting that the retain-vs-redeploy comparison must carry on the same "
                          "line as the dealing cost and the FX charge."),
    }


def measurement_caveat(sedol, universe):
    """What a price-derived return actually means for a closed-end holding."""
    if not is_closed_end((universe or {}).get(sedol)):
        return None
    return {
        "sedol": sedol, "return_basis": "market price (yfinance)",
        "caveat": ("for a closed-end trust, price return = NAV return + change in the discount. "
                   "Every trailing return this framework publishes for this holding therefore "
                   "mixes the manager's result with a change in market sentiment about the "
                   "trust, and the two are not separable without a NAV series."),
        "observed_consequence": ("at the X-Ray's own strike date the golden source agrees to 2dp "
                                 "with the X-Ray on JPM UK (11.48), RLGES (16.53), VUAG (13.98) "
                                 "and Vanguard Japan (10.22) and disagrees on Scottish Mortgage "
                                 "alone by 2.48pp — four exact agreements and one exception, in "
                                 "the only closed-end holding."),
    }


# ── OBSERVATION CADENCE (06-Aug-2026) ────────────────────────────────────────────────────
# The discount machinery works and the series has ONE point. Twelve are needed before −8.77%
# can be called wide or narrow, and at one observation a month that is August 2027 — but only
# if an observation is actually taken every month. "Someone must remember" is not a mechanism;
# it is the same failure as §Q capture being prose in step 16c-2 of 19.
#
# ⚑ AND IT CANNOT BE AUTOMATED, WHICH IS THE POINT. yfinance serves the trust's PRICE; the NAV
# is published by the manager and the AIC, and neither is in any feed this framework has. So
# the mechanism is not a fetch — it is a REFUSAL TO BE SILENT: every run either finds a fresh
# observation or emits a request naming the fund, the exact fields, the source to read them
# from, and how far behind the clock now is.
OBS_MAX_AGE_DAYS = 45            # a monthly cadence with slack for a late run
OBS_TARGET = MIN_OBS_FOR_STATS


def capture_status(store=None, universe=None, as_of=None):
    """Per closed-end holding: is this month's observation on record, and if not, what exactly
    is needed. Emitted every run so the clock is visible rather than assumed to be running."""
    import fund_performance as fp
    universe = universe if universe is not None else fp.load_universe()
    store = store if store is not None else load_observations()
    today = as_of or dt.date.today()
    out = []
    for sedol, u in universe.items():
        if str(sedol).startswith("_") or not is_closed_end(u):
            continue
        obs = sorted((o for o in store.get("observations", []) if o["sedol"] == sedol),
                     key=lambda o: o["as_of"])
        latest = obs[-1]["as_of"] if obs else None
        age = (today - dt.date.fromisoformat(str(latest)[:10])).days if latest else None
        # ⚑ A NEGATIVE AGE IS NOT A FRESH OBSERVATION. It means the observation is dated AFTER
        # the run date — which is either a typo in the recorded date or an observation being
        # stamped forward, and both are point-in-time violations of exactly the kind the Q5
        # regime guard exists to stop. Reported, never silently treated as "very fresh".
        future_dated = age is not None and age < 0
        due = latest is None or (age is not None and age > OBS_MAX_AGE_DAYS)
        remaining = max(0, OBS_TARGET - len(obs))
        # projected completion at one observation per month, from the latest on record
        proj = None
        if remaining and latest:
            m = dt.date.fromisoformat(str(latest)[:10])
            y, mo = divmod((m.year * 12 + m.month - 1) + remaining, 12)
            proj = dt.date(y, mo + 1, 1).isoformat()
        out.append({
            "sedol": sedol, "name": u.get("name"),
            "observations": len(obs), "target": OBS_TARGET, "remaining": remaining,
            "latest_observation": latest, "age_days": age,
            "capture_due": bool(due),
            "future_dated": bool(future_dated),
            "future_dated_note": ((
                f"⚑ the latest observation is dated {latest}, AFTER the run date "
                f"{today.isoformat()}. Either the recorded date is wrong or an observation was "
                f"stamped forward; a point-in-time series cannot contain the future.")
                if future_dated else None),
            "projected_complete": proj,
            "nav_source": u.get("nav_source") or (
                "NOT DECLARED — add `nav_source` to fund_universe.json so this request names a "
                "page rather than a task"),
            "request": ((
                f"RECORD A NAV OBSERVATION for {u.get('name')} ({sedol}). Read the published "
                f"NAV and the market price AS A MATCHED PAIR from ONE source on ONE date — a "
                f"discount computed from two sources on two dates is not a discount, it is two "
                f"unrelated numbers. Then:\n"
                f"    python3 trust_discount.py --record {sedol} <YYYY-MM-DD> <NAV> \"<source>\"\n"
                + (f"Last observation {latest} ({age} days ago). " if latest else
                   "NO observation on record at all. ")
                + f"{remaining} more needed before a percentile or z-score can be published"
                + (f"; on a monthly cadence that is {proj}." if proj else ".")
            ) if due else None),
        })
    return out


# ── ISA-0127 (18-Oct-2026): AUTONOMOUS CAPTURE FROM THE MANAGER'S OWN RNS ──────────────────────
# The 06-Aug note above said the NAV "cannot be automated" - true of the feeds this framework had. Scottish
# Mortgage publishes a daily regulatory 'Net Asset Value(s)' RNS (Cum/Ex, Par/Fair, pence), reachable on its
# Investegate company page. The AIC convention is Cum Fair. Each observation is a MATCHED PAIR: the RNS
# NAV as at date D with the trust's unadjusted market close on the SAME date D (yfinance auto_adjust=False -
# an adjusted close is rescaled at every dividend and is not the price anyone traded at).
RNS_LIST_URL = "https://www.investegate.co.uk/company/{epic}?page={page}"
RNS_NAV_FIELD = "Cum Fair NAV"
RNS_CAPTURE_MIN_AGE_DAYS = 7     # the pre-run records a new observation when the newest is older than this


class RnsLayoutError(RuntimeError):
    """The Investegate page no longer has the shape this parser was written against."""


def parse_rns_list(html_text):
    """[(release_date, url, title)] from an Investegate company page. RAISES on layout change."""
    import re
    if "announcement-table" not in html_text:
        raise RnsLayoutError("no announcement-table on the Investegate company page")
    rows = re.findall(r'<tr>\s*<td>(\d{1,2} \w{3} \d{4})</td>.*?announcement-link" href="([^"]+)">([^<]+)</a>',
                      html_text, re.S)
    if not rows:
        raise RnsLayoutError("announcement-table has no parseable rows")
    out = []
    for d, url, title in rows:
        out.append((dt.datetime.strptime(d, "%d %b %Y").date(), url, title.strip()))
    return out


def parse_rns_nav(html_text):
    """{as_of, cum_par_p, cum_fair_p, ex_par_p, ex_fair_p} from a 'Net Asset Value(s)' announcement.
    The as-at date is the one printed beneath the trust's name in the body (the release is the next
    business day). RAISES when any field or the date is missing."""
    import re, html as _h
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text, flags=re.S)
    txt = re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", txt)))
    m = re.search(r"Investment Trust PLC \(SMT\) (\d{1,2} \w+ \d{4})", txt) or \
        re.search(r"\(\w{2,5}\) (\d{1,2} \w+ \d{4}) Legal Entity Identifier", txt)
    if not m:
        raise RnsLayoutError("NAV announcement carries no as-at date")
    out = {"as_of": dt.datetime.strptime(m.group(1), "%d %B %Y").date()}
    for key, label in (("cum_par_p", "Cum Par NAV"), ("cum_fair_p", "Cum Fair NAV"),
                       ("ex_par_p", "Ex Par NAV"), ("ex_fair_p", "Ex Fair NAV")):
        mm = re.search(re.escape(label) + r" ([0-9][0-9,]*\.?[0-9]*)p", txt)
        if not mm:
            raise RnsLayoutError("NAV announcement has no '%s' figure" % label)
        out[key] = float(mm.group(1).replace(",", ""))
    return out


def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ISA research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _yf_unadjusted_closes(symbol, start, end):
    """{date: close} unadjusted (auto_adjust=False), or {} on any failure."""
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                                      auto_adjust=False)["Close"]
        return {(i.date() if hasattr(i, "date") else dt.date.fromisoformat(str(i)[:10])): float(v)
                for i, v in h.items() if v == v and v > 0}
    except Exception:                                                   # noqa: BLE001
        return {}


def rns_nav_targets(sedol, u):
    """The declared RNS route for a closed-end fund (`rns_nav: {epic, field}` in fund_universe), or None."""
    r = (u or {}).get("rns_nav") or {}
    return r if r.get("epic") else None


def capture_rns(as_of, universe=None, store_path=None, *, get=None, closes=None, months=0, budget_s=15.0):
    """Record matched NAV/price observations from the trust's own RNS for every closed-end fund declaring
    `rns_nav`. months=0 (the pre-run): the newest announcement, if the newest observation on record is older
    than RNS_CAPTURE_MIN_AGE_DAYS. months=N (one-off backfill): one observation per month-end for the last N
    months. Content-keyed: re-running adds nothing. Returns a typed per-fund report; never raises for a
    source failure (the capture_status request then stays visible)."""
    import time as _t
    import fund_performance as fp
    t0 = _t.time()
    get = get or _http_get
    closes = closes or _yf_unadjusted_closes
    universe = universe if universe is not None else fp.load_universe()
    funds = universe.get("funds", universe)
    path = store_path or OBS_PATH
    store = load_observations(path)
    rep = {}
    for sedol, u in funds.items():
        if str(sedol).startswith("_") or not is_closed_end(u):
            continue
        rt = rns_nav_targets(sedol, u)
        if not rt:
            rep[sedol] = {"state": "NOT_DECLARED", "why": "no rns_nav route declared in fund_universe"}
            continue
        obs = sorted((o for o in store.get("observations", []) if o["sedol"] == sedol), key=lambda o: o["as_of"])
        newest = dt.date.fromisoformat(obs[-1]["as_of"]) if obs else None
        if months == 0 and newest and (as_of - newest).days <= RNS_CAPTURE_MIN_AGE_DAYS:
            rep[sedol] = {"state": "CURRENT", "newest": newest.isoformat(), "why": "observation within %d days" % RNS_CAPTURE_MIN_AGE_DAYS}
            continue
        try:
            rows, page = [], 1
            oldest_needed = (as_of.replace(day=1) - dt.timedelta(days=31 * months)) if months else as_of
            while page <= (months + 2 if months else 1):
                lst = parse_rns_list(get(RNS_LIST_URL.format(epic=rt["epic"], page=page)))
                rows += [r for r in lst if "net-asset-value" in r[1].lower() or "net asset value" in r[2].lower()]
                if lst[-1][0] < oldest_needed or _t.time() - t0 > budget_s:
                    break
                page += 1
            rows = [r for r in rows if r[0] <= as_of]
            if not rows:
                rep[sedol] = {"state": "SOURCE_FAILED", "why": "no NAV announcement on the RNS list"}
                continue
            if months:
                # one per month: the FIRST release of the following month carries the month-end NAV
                by_month = {}
                for d, url, title in sorted(rows, key=lambda r: r[0]):
                    by_month.setdefault((d.year, d.month), (d, url))
                picks = [by_month[k] for k in sorted(by_month)][-(months + 1):]
            else:
                picks = [max(rows, key=lambda r: r[0])[:2]]
            navs = []
            for d, url in picks:
                if _t.time() - t0 > budget_s:
                    break
                navs.append(dict(parse_rns_nav(get(url)), url=url, released=d))
            if not navs:
                rep[sedol] = {"state": "SOURCE_FAILED", "why": "budget spent before any announcement was read"}
                continue
            lo, hi = min(n["as_of"] for n in navs), max(n["as_of"] for n in navs)
            px = closes(u.get("yf_symbol"), lo - dt.timedelta(days=5), hi)
            scale = 100.0 if u.get("price_unit") == "GBp" else 1.0
            added, unmatched = [], []
            for n in sorted(navs, key=lambda n: n["as_of"]):
                field = rt.get("field") or RNS_NAV_FIELD
                key = {"Cum Fair NAV": "cum_fair_p", "Cum Par NAV": "cum_par_p", "Ex Fair NAV": "ex_fair_p",
                       "Ex Par NAV": "ex_par_p"}[field]
                nav = round(n[key] / 100.0, 6)
                price = px.get(n["as_of"])
                if price is None:
                    unmatched.append(n["as_of"].isoformat())
                    continue
                store, ok = record_observation(
                    store, sedol, n["as_of"].isoformat(), nav,
                    "Investegate RNS 'Net Asset Value(s)' released %s (%s, %s) + %s unadjusted close same date (yfinance)"
                    % (n["released"].isoformat(), n["url"], field, u.get("yf_symbol")),
                    price=round(price / scale, 6), as_of_confidence="stated",
                    note="ISA-0127 autonomous capture; Cum Par %.2fp / Cum Fair %.2fp / Ex Par %.2fp / Ex Fair %.2fp"
                         % (n["cum_par_p"], n["cum_fair_p"], n["ex_par_p"], n["ex_fair_p"]))
                if ok:
                    added.append(n["as_of"].isoformat())
            rep[sedol] = {"state": "RECORDED" if added else ("UNMATCHED_PRICE" if unmatched else "ALREADY_PRESENT"),
                          "added": added, "unmatched_price_dates": unmatched,
                          "why": "%d NAV announcement(s) read" % len(navs)}
        except Exception as e:                                          # noqa: BLE001
            rep[sedol] = {"state": "SOURCE_FAILED", "why": "%s: %s" % (type(e).__name__, str(e)[:200])}
    if any(r.get("added") for r in rep.values()):
        save_observations(store, path)
    return {"as_of": as_of.isoformat(), "funds": rep, "secs": round(_t.time() - t0, 1)}


def _selftest():
    """ISA-0127 offline selftest: RNS list/NAV parsers, matched-pair capture, dedupe, failure typing."""
    import tempfile, shutil
    fails = []

    def ok(name, cond, detail=None):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
        if not cond:
            fails.append(name)
    row = ('<tr>\n<td>%s</td>\n<td>11:27 AM</td><td>RNS</td><td>\n<a class="announcement-link" href="%s">%s</a>'
           '\n</td></tr>')
    lst = ('<div class="announcement-table"><table>' + row % ("16 Oct 2026", "https://x/net-asset-value-s-/2", "Net Asset Value(s)")
           + row % ("16 Oct 2026", "https://x/transaction-in-own-shares/3", "Transaction in Own Shares")
           + row % ("01 Oct 2026", "https://x/net-asset-value-s-/1", "Net Asset Value(s)") + "</table></div>")
    got = parse_rns_list(lst)
    ok("RNS list parser reads date, url, title", got[0] == (dt.date(2026, 10, 16), "https://x/net-asset-value-s-/2", "Net Asset Value(s)"), got[:1])
    try:
        parse_rns_list("<html>blocked</html>")
        ok("MUST-FIRE: a list page without the announcement table RAISES", False)
    except RnsLayoutError:
        ok("MUST-FIRE: a list page without the announcement table RAISES", True)
    body = ("<p>Scottish Mortgage Investment Trust PLC (SMT) 15 October 2026 Legal Entity Identifier : X</p>"
            "<p>Cum Par NAV 1714.96p</p><p>Cum Fair NAV 1,753.50p</p><p>Ex Par NAV 1712.46p</p><p>Ex Fair NAV 1751.00p</p>")
    n = parse_rns_nav(body)
    ok("RNS NAV parser reads the as-at date (not the release date) and all four figures",
       n == {"as_of": dt.date(2026, 10, 15), "cum_par_p": 1714.96, "cum_fair_p": 1753.5, "ex_par_p": 1712.46, "ex_fair_p": 1751.0}, n)
    try:
        parse_rns_nav(body.replace("Cum Fair NAV 1,753.50p", ""))
        ok("MUST-FIRE: an announcement missing the Cum Fair figure RAISES (never a guessed NAV)", False)
    except RnsLayoutError:
        ok("MUST-FIRE: an announcement missing the Cum Fair figure RAISES (never a guessed NAV)", True)
    tmp = tempfile.mkdtemp(prefix="td_st_")
    try:
        sp = os.path.join(tmp, "obs.json")
        U = {"funds": {"SMT": {"name": "SMT", "structure": "closed_end", "yf_symbol": "SMT.L", "price_unit": "GBp",
                               "rns_nav": {"epic": "SMT", "field": "Cum Fair NAV"}},
                       "OE": {"name": "Open", "structure": "oeic"}}}
        pages = {"https://www.investegate.co.uk/company/SMT?page=1": lst, "https://x/net-asset-value-s-/2": body}
        r = capture_rns(dt.date(2026, 10, 18), U, sp, get=lambda url: pages[url],
                        closes=lambda sym, a, b: {dt.date(2026, 10, 15): 1545.0})
        o = load_observations(sp)["observations"]
        ok("MUST-FIRE: the newest RNS NAV is recorded as a MATCHED pair (NAV GBP, same-date unadjusted price GBP)",
           r["funds"]["SMT"]["state"] == "RECORDED" and len(o) == 1 and o[0]["as_of"] == "2026-10-15"
           and abs(o[0]["nav"] - 17.535) < 1e-9 and abs(o[0]["price"] - 15.45) < 1e-9, (r, o))
        r2 = capture_rns(dt.date(2026, 10, 30), U, sp, get=lambda url: pages[url],
                         closes=lambda sym, a, b: {dt.date(2026, 10, 15): 1545.0})
        ok("R5.14 repeat: re-reading the same announcement adds nothing (content-keyed)",
           r2["funds"]["SMT"]["state"] == "ALREADY_PRESENT" and len(load_observations(sp)["observations"]) == 1, r2)
        r3 = capture_rns(dt.date(2026, 10, 19), U, sp, get=lambda url: (_ for _ in ()).throw(AssertionError("fetched")))
        ok("R5.14 T: an observation within 7 days is CURRENT and nothing is fetched", r3["funds"]["SMT"]["state"] == "CURRENT", r3)
        r4 = capture_rns(dt.date(2026, 11, 30), U, os.path.join(tmp, "o2.json"), get=lambda url: pages[url],
                         closes=lambda sym, a, b: {})
        ok("NEGATIVE CONTROL: no same-date price -> UNMATCHED_PRICE, nothing recorded (a discount across two dates is not one)",
           r4["funds"]["SMT"]["state"] == "UNMATCHED_PRICE" and not os.path.exists(os.path.join(tmp, "o2.json")), r4)
        r5 = capture_rns(dt.date(2026, 11, 30), U, os.path.join(tmp, "o3.json"), get=lambda url: "<html>blocked</html>")
        ok("MUST-FIRE: a blocked source is SOURCE_FAILED (typed), never a silent pass", r5["funds"]["SMT"]["state"] == "SOURCE_FAILED", r5)
        ok("NEGATIVE CONTROL: an open-ended fund is never captured", "OE" not in r5["funds"])
        _st = {"observations": [{"sedol": "SMT", "as_of": "2026-07-31", "nav": 15.0, "price": 13.8, "source": "t"},
                                {"sedol": "SMT", "as_of": "2026-10-15", "nav": 17.5, "price": 15.9, "source": "t"}]}
        _dj = discount("SMT", U["funds"], 13.8, dt.date(2026, 7, 31), _st, as_of=dt.date(2026, 7, 31))
        ok("MUST-FIRE (PIT): a build as of 31-Jul never reads the 15-Oct observation", _dj.get("nav_as_of") == "2026-07-31", _dj.get("nav_as_of"))
        _dk = discount("SMT", U["funds"], 15.9, dt.date(2026, 10, 18), _st, as_of=dt.date(2026, 10, 18))
        ok("NEGATIVE CONTROL (PIT): at the run date the newest observation is used", _dk.get("nav_as_of") == "2026-10-15", _dk.get("nav_as_of"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("trust_discount selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def build(portfolio=None, universe=None, out_path=None, as_of=None):
    import fund_performance as fp
    universe = universe if universe is not None else fp.load_universe()
    store = load_observations()
    res = {"schema_version": SCHEMA_VERSION,
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "as_of": (as_of.isoformat() if as_of else None),
           "closed_end_holdings": [], "open_ended_skipped": [], "caveats": [],
           "observations_on_record": len(store.get("observations", []))}
    holdings = {h.get("ticker"): h for h in ((portfolio or {}).get("funds") or [])}
    for sedol, u in universe.items():
        if str(sedol).startswith("_"):
            continue
        if not is_closed_end(u):
            res["open_ended_skipped"].append(sedol)
            continue
        h = holdings.get(sedol) or {}
        d = discount(sedol, universe, h.get("price"), as_of, store, as_of=as_of)
        d["value_gbp"] = h.get("value_gbp")
        d["crystallisation"] = crystallisation_cost(d, h.get("value_gbp"))
        res["closed_end_holdings"].append(d)
        c = measurement_caveat(sedol, universe)
        if c:
            res["caveats"].append(c)
    res["capture_status"] = capture_status(store, universe, as_of)
    res["summary"] = {
        "captures_due": [c["sedol"] for c in res["capture_status"] if c["capture_due"]],
        "n_closed_end": len(res["closed_end_holdings"]),
        "measured": sum(1 for d in res["closed_end_holdings"]
                        if (d.get("discount_pct") or {}).get("present")),
        "unmeasured": [d["sedol"] for d in res["closed_end_holdings"]
                       if not (d.get("discount_pct") or {}).get("present")]}
    if out_path:
        json.dump(res, open(out_path, "w", encoding="utf-8"), indent=1, default=str)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=os.path.join(HERE, "portfolio_data_aug_2026.json"))
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--record", nargs=4, metavar=("SEDOL", "AS_OF", "NAV", "SOURCE"),
                    help="append a declared NAV observation")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--capture-rns", type=int, default=None, metavar="MONTHS",
                    help="ISA-0127: record matched RNS NAV observations (0 = newest; N = one per month-end for N months)")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(1 if _selftest() else 0)
    if a.capture_rns is not None:
        print(json.dumps(capture_rns(dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today(),
                                     months=a.capture_rns, budget_s=150.0), indent=1, default=str))
        sys.exit(0)
    if a.record:
        st = load_observations()
        st, added = record_observation(st, a.record[0], a.record[1], float(a.record[2]),
                                       a.record[3])
        save_observations(st)
        print(("recorded" if added else "already present") + f": {a.record}")
        sys.exit(0)
    pf = json.load(open(a.portfolio, encoding="utf-8")) if os.path.exists(a.portfolio) else {}
    d = dt.date.fromisoformat(a.as_of) if a.as_of else None
    print(json.dumps(build(pf, None, a.out, d), indent=1, default=str))
