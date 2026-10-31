#!/usr/bin/env python3
"""
extract_cash_statement.py — BuildSpec Part 2 (built 05-Aug-2026).

WHY THIS EXISTS, STATED PRECISELY
---------------------------------
The ISA allowance was reported ~£3,750 used / ~£16,250 remaining. The truth is **£8,750 used /
£11,250 remaining** — wrong by £5,000, because a `Faster Payment In` of £5,000 on 06-Apr-2026
was in no framework input.

The root cause is structural, not arithmetic, and worth stating so it is not re-introduced:
`parse_contributions()` reads AJ Bell's **Transaction History**, which is a DEALING record —
Purchase, Sale, Transfer In, Equalisation, Fund Class Conversion. It contains **no cash
deposits at all**. Contributions live only in the **Cash Statement**, a different export. So the
allowance was being derived from a document that structurally cannot contain the answer, and the
framework had no way to notice, because "no deposit rows found" and "no deposits made" produce
the same number.

(A second, independent defect was fixed alongside: the A22 glob `ISA Transaction History*.xlsx`
matched 1 of Raj's 3 transaction files. Both had to be wrong for the £5,000 to disappear
silently, which is the usual shape of these things.)

⚑ THE TRAP — `* BALANCE B/F *`
The first row is the prior tax year's closing cash carried forward, **£3,469.64 in the Receipt
column**. It is indistinguishable from a contribution to any naive sum, and counting it would
overstate the allowance by £3,469.64. It is a POSITION, not a FLOW. `I12` is a dedicated
negative test for exactly this.

FX — SETTLED BY PRIMARY SOURCE
The file contains literal `FX Charge (0.50%)` rows. The framework carried **0.75%**. The COCO
purchase executed at £1,354.99 against a £1,384.75 estimate, and the difference is precisely the
overstatement. `FX_RATE_PCT` here is the single home for that number.

CONTRACT
--------
* Missing file  -> WARN and `reconciled: False`. Never an ERROR, never a guessed figure.
* Every category is decided by a tested rule over the free-text Description, because the file
  has no type column. An unmatched description is `UNCLASSIFIED` — never silently dropped, and
  never bucketed into the nearest plausible category.
* Tax year is derived from the ROW DATES, never the filename. The A22 glob failure is the
  reason that is spelled out rather than assumed.
"""
from __future__ import annotations
import argparse, datetime as dt, glob, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ISA_ALLOWANCE_GBP = 20000.0

# ---------------------------------------------------------------------------
# THE single home for cash-side portfolio constants (R4.4). This module already owns
# FX_RATE_FRACTION on the same principle: the module that extracts the truth owns the
# constant, and every consumer imports it. Consolidated 12-Aug-2026 after the Framework
# Atlas found ISA_ALLOWANCE_GBP in 2 homes, CASH_BUFFER_MIN in 2, and the monthly standing
# order in THREE homes under TWO different names (STANDING_ORDER and STANDING_ORDER_MONTHLY).
#
# ⚑ VALUES ARE UNCHANGED BY THAT CONSOLIDATION. A refactor must never move a number.
# ---------------------------------------------------------------------------
CASH_BUFFER_MIN = 150.0
CASH_BUFFER_MAX = 200.0

# ⚑ OPEN QUESTION FOR RAJ (register ISA-0011): user_raj_profile records the £1,250 monthly
# standing order as PAUSED from Jul-2026 (job security). All three former homes carried
# 1250.0 as if live. The VALUE IS LEFT AS IT WAS so this consolidation changes no behaviour;
# STANDING_ORDER_ACTIVE makes the assumption explicit and switchable in ONE place instead of
# being implicit in three. It feeds contribution schedules, the required-return anchor and
# allowance projection - so if the pause is real, the anchor is derived on a schedule that
# is not happening.
# ⚑⚑ ISA-0851 (11-Oct-2026, B01-R01): these names are NO LONGER A SECOND HOME. They are DERIVED, at
#   import, from the one schedule reader (contribution_schedule -> target_state.contribution_schedule), so
#   the 6-Apr-2027 restart (or any schedule edit) reaches every consumer with no hand edit, and a missing
#   schedule reads UNKNOWN -> not active (no assumed contribution). They are kept as names only for the
#   existing importers (extract_portfolio, email_prefill, portfolio_analytics). An expected contribution
#   is never cash: extract_portfolio no longer infers a credit from them (ISA-0851).
try:
    import contribution_schedule as _cs851
    _SO_STATE = _cs851.state()
except Exception as _cs_exc:                                             # noqa: BLE001
    _SO_STATE = {"state": "UNKNOWN", "monthly_gbp": None, "paused_since": None, "restart_monthly_gbp": None,
                 "why": "contribution_schedule unavailable (%s)" % _cs_exc}
STANDING_ORDER_ACTIVE = _SO_STATE.get("state") == "ACTIVE"
STANDING_ORDER = float((_SO_STATE.get("monthly_gbp") if STANDING_ORDER_ACTIVE else _SO_STATE.get("restart_monthly_gbp")) or 0.0)
STANDING_ORDER_PAUSED_FROM = _SO_STATE.get("paused_since")      # None when active or UNKNOWN
STANDING_ORDER_STATE = _SO_STATE.get("state")
SO_CLEAR_WORKING_DAYS = 3             # clears within ~3 working days of the 1st

# ⚑ SINGLE HOME for the AJ Bell FX charge. Evidenced by two `FX Charge (0.50%)` rows in the
# 2026-27 cash statement and by the COCO fill. Anything that models a USD trade cost reads THIS.
FX_RATE_PCT = 0.50
FX_RATE_FRACTION = FX_RATE_PCT / 100.0  # evidenced - see FX_RATE_SOURCE below and the block at 79-81
FX_RATE_SOURCE = ("AJ Bell cash statement 2026-27, literal 'FX Charge (0.50%)' rows; "
                  "corroborated by COCO 03-Aug-2026 executing at GBP1,354.99 vs a "
                  "GBP1,384.75 estimate built on 0.75%")

OPENING_BALANCE_RE = re.compile(r"^\s*\*+\s*balance\s*b/?f\s*\*+", re.I)

# ⚑ ISA-0642 (19-Oct-2026). TRANSFER_IN — cash moved INTO this ISA from another account, by
# Raj's decision: "TFER FROM BB8HNPI" GBP 1,477.83 (01-May-2025) is the transfer of value from the
# adviser platform (AJ Bell Investcentre, BB8HNPI) when Raj stopped using a financial adviser
# (13-Oct-2026), and "Residual Funds from BB8HNPI" GBP 0.74 (24-Jun-2026) "would have been a
# rounding transfer in" (19-Oct-2026). Neither is an allowance subscription, and neither is
# investment growth: it is an EXTERNAL FLOW. The rule is mechanical — the text after "from" must
# be ONE account-like token (letters AND digits, 6-10 characters) — so "Residual Funds from
# Artemis SmartGARP" (a fund, not an account) is not caught by it and stays a SELL.
TRANSFER_IN_RE = re.compile(r"^\s*(?:tfer|transfer|residual\s+funds)\s+from\s+(\S+)\s*$", re.I)


def _is_account_transfer_in(d):
    m = TRANSFER_IN_RE.match(str(d or ""))
    if not m:
        return False
    tok = m.group(1).upper()
    return (bool(re.fullmatch(r"[A-Z0-9]{6,10}", tok)) and any(c.isdigit() for c in tok)
            and any(c.isalpha() for c in tok))


# Order matters: the first rule that matches wins, so the specific precede the general.
CLASSIFIERS = [
    ("OPENING_BALANCE", lambda d: bool(OPENING_BALANCE_RE.match(d))),
    ("TRANSFER_IN",     _is_account_transfer_in),
    ("CONTRIBUTION",    lambda d: any(k in d.lower() for k in
                                      ("faster payment in", "direct debit payment",
                                       "debit card payment", "bank transfer in"))),
    ("FX_FEE",          lambda d: "fx charge" in d.lower()),
    ("PLATFORM_FEE",    lambda d: "account charge" in d.lower()),
    ("DIVIDEND",        lambda d: d.lower().lstrip().startswith("dividend")),
    ("INTEREST",        lambda d: "gross interest" in d.lower()),
    ("REBATE",          lambda d: "cash rebate" in d.lower()),
    ("BUY",             lambda d: d.lower().lstrip().startswith("purchase")),
    ("SELL",            lambda d: d.lower().lstrip().startswith("sale")
                                  or "residual funds from" in d.lower()),
    ("TRANSFER",        lambda d: "transfer" in d.lower()),
]
FLOW_CATEGORIES = {"CONTRIBUTION"}          # what counts against the ISA allowance
# ⚑ ISA-0642: what is an EXTERNAL flow (money that is neither growth nor a position carried
# forward). A superset of FLOW_CATEGORIES: a TRANSFER_IN is a flow for return measurement and the
# anchor's flow ledger, but it is NOT a subscription and never touches the allowance.
EXTERNAL_FLOW_CATEGORIES = {"CONTRIBUTION", "TRANSFER_IN"}


def classify(description):
    d = str(description or "")
    if not d.strip():
        return "UNCLASSIFIED"
    for name, test in CLASSIFIERS:
        try:
            if test(d):
                return name
        except Exception:
            continue
    return "UNCLASSIFIED"


def tax_year_start(ref):
    yr = ref.year if (ref.month, ref.day) >= (4, 6) else ref.year - 1
    return dt.date(yr, 4, 6)


def find_files(folder):
    """`Cash Statement*.xlsx`, case-insensitively, Office lock files excluded. The tax year is
    NOT taken from the filename — see the module docstring."""
    out = {}
    for pat in ("Cash Statement*.xlsx", "cash statement*.xlsx", "*ash*tatement*.xlsx"):
        for fp in glob.glob(os.path.join(folder, pat)):
            if not os.path.basename(fp).startswith("~$"):
                out[os.path.realpath(fp)] = fp
    return sorted(out.values())


def _as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _num(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0.0
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _category_totals(rows):
    totals = {}
    for r in rows:
        totals.setdefault(r["category"], {"n": 0, "receipts": 0.0, "payments": 0.0})
        t = totals[r["category"]]
        t["n"] += 1; t["receipts"] += r["receipt_gbp"]; t["payments"] += r["payment_gbp"]
    for t in totals.values():
        t["receipts"] = round(t["receipts"], 2)
        t["payments"] = round(t["payments"], 2)
        t["net"] = round(t["receipts"] - abs(t["payments"]), 2)
    return totals


def _flow_recon(rows):
    """opening + SUM(receipts - payments) vs the stated closing balance, over ONE tax year's rows."""
    opening = next((r for r in rows if r["category"] == "OPENING_BALANCE"), None)
    ob = opening["receipt_gbp"] if opening else None
    close = rows[-1]["balance_gbp"] if rows else None
    if not rows or ob is None:
        return ob, close, None
    flow = sum(r["receipt_gbp"] - abs(r["payment_gbp"]) for r in rows
               if r["category"] != "OPENING_BALANCE")
    implied = round(ob + flow, 2)
    return ob, close, {"ok": abs(implied - (close or 0)) <= 0.05, "implied": implied, "stated": close,
                       "delta": round(implied - (close or 0), 2)}


def _external_flow_rows(rows):
    return [{"date": r["date"], "description": r["description"], "category": r["category"],
             "amount_gbp": round(r["receipt_gbp"] - abs(r["payment_gbp"]), 2),
             "reference": r["reference"], "source_file": r["source_file"]}
            for r in rows if r["category"] in EXTERNAL_FLOW_CATEGORIES]


def parse(folder=None, files=None, as_of=None):
    """⚑ ISA-0642 (19-Oct-2026): ONE CURRENT-TAX-YEAR VIEW.

    Every consumer of this function — the allowance, the I11-I15 invariants, the opening and
    closing balance, category_totals (fees, FX), the cash rate (return_architecture), deployable
    cash and the account-identity check (extract_portfolio / monthly_isa_prerun), the anchor flow
    ledger (derive_required_return) and the I10 ledger join — reads `rows`, and `rows` holds ONLY
    the rows of the tax year containing `as_of` (decided from ROW DATES, never filenames). Rows of
    earlier tax years are RETURN-FLOW EVIDENCE: they are published under `prior_tax_years` and
    reach nothing else (Raj 13-Oct-2026). Before this, saving the 2025/26 export beside the
    2026/27 one silently made the opening balance GBP 0.00 (the 2025/26 B/F), spread fees over two
    years, fed 2025/26 deposits into the anchor flow ledger and failed I14 on a 2025 row.
    """
    import openpyxl
    folder = folder or os.path.dirname(HERE)
    files = files if files is not None else find_files(folder)
    as_of = as_of or dt.date.today()
    res = {"as_of": as_of.isoformat(), "source_files": [], "files_on_disk": [os.path.basename(f) for f in files],
           "fx_rate_pct": FX_RATE_PCT, "fx_rate_source": FX_RATE_SOURCE,
           "rows": [], "reconciled": False, "invariants": {}, "warnings": [], "prior_tax_years": []}
    if not files:
        res["warnings"].append("no Cash Statement*.xlsx found — allowance stays UNRECONCILED. "
                               "WARN, never ERROR: an absent file is a missing input, not a "
                               "failed computation.")
        return res

    rows = []
    for fp in files:
        wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        # ⚑ ISA-0900 (Raj 26-Oct-2026: "I have provided the cash record required"): the statement COVERS every posting
        #   up to the moment it was exported, not merely to its last dated row - a quiet week after the last posting is
        #   covered, not unknown. The export stamp (workbook created) is recorded per file; consumers take
        #   max(last dated row, export date) as the coverage end. An absent stamp leaves the last row as the end.
        try:
            _cr = getattr(wb.properties, "created", None)
            res.setdefault("file_exports", {})[os.path.basename(fp)] = _cr.isoformat() if _cr else None
        except Exception:                                                   # noqa: BLE001
            res.setdefault("file_exports", {})[os.path.basename(fp)] = None
        frows = []
        for ws in wb.worksheets:
            hdr = None
            for raw in ws.iter_rows(values_only=True):
                if hdr is None:
                    hdr = [str(c or "").strip().lower() for c in raw]
                    continue
                r = dict(zip(hdr, raw))
                d = _as_date(r.get("date"))
                desc = str(r.get("description") or "").strip()
                if d is None and not OPENING_BALANCE_RE.match(desc):
                    continue
                frows.append({
                    "date": d.isoformat() if d else None,
                    "description": desc,
                    "reference": str(r.get("reference") or "").strip(),
                    "settlement_date": (_as_date(r.get("settlement date")).isoformat()
                                        if _as_date(r.get("settlement date")) else None),
                    "receipt_gbp": _num(r.get("receipt (gbp)")),
                    "payment_gbp": _num(r.get("payment (gbp)")),
                    "balance_gbp": _num(r.get("balance (gbp)")),
                    "category": classify(desc),
                    "source_file": os.path.basename(fp),
                })
            break                                     # single-sheet export by design
        # ⚑ ISA-0642 fix-on-finding (19-Oct-2026): AJ Bell exports NEWEST FIRST, and same-day rows
        # often all carry reference "-". The old sort key (date, reference) therefore put same-day
        # rows in an arbitrary order and `closing = rows[-1]` read the wrong row: on 02-Apr-2026 it
        # took GBP 3,469.30 (the direct debit) instead of GBP 3,469.64 (the later rebate), and the
        # 06-Apr B/F sorted AFTER that day's deposit. The export's own sequence is the truth: its
        # direction is read from the dates (first vs last dated row), and rows are ordered by
        # (date, position in time) — never by a reference string.
        _dated = [x["date"] for x in frows if x["date"]]
        _newest_first = bool(_dated) and _dated[0] > _dated[-1]
        n = len(frows)
        for i, x in enumerate(frows):
            x["_seq"] = (n - 1 - i) if _newest_first else i
        rows.extend(frows)
    rows.sort(key=lambda x: (x["date"] or "0000-00-00", x["source_file"], x["_seq"]))
    for x in rows:
        x["file_order"] = x.pop("_seq")

    ty_start = tax_year_start(as_of)
    res["tax_year_start"] = ty_start.isoformat()

    # ── the tax year of every row, from its DATE; an undated B/F row takes its file's year ──
    def _row_ty(r):
        return tax_year_start(dt.date.fromisoformat(r["date"])) if r["date"] else None
    file_ty = {}
    for r in rows:
        t = _row_ty(r)
        if t is not None:
            file_ty.setdefault(r["source_file"], set()).add(t)
    cur_files = sorted(f for f, tys in file_ty.items() if ty_start in tys)
    def _is_current(r):
        t = _row_ty(r)
        return (t == ty_start) if t is not None else (r["source_file"] in cur_files)
    cur = [r for r in rows if _is_current(r)]
    prior = [r for r in rows if not _is_current(r)]
    res["rows"] = cur
    res["source_files"] = cur_files

    # ── prior tax years: RETURN-FLOW EVIDENCE only ─────────────────────────────────────────
    by_ty = {}
    for r in prior:
        t = _row_ty(r)
        if t is None:
            tys = sorted(file_ty.get(r["source_file"]) or [])
            t = tys[0] if tys else None
        if t is None or t > ty_start:
            continue                                  # a row AFTER the current tax year is not evidence of the past
        by_ty.setdefault(t, []).append(r)
    for t in sorted(by_ty):
        ys = by_ty[t]
        ob, close, recon = _flow_recon(ys)
        fl = _external_flow_rows(ys)
        unc = [r for r in ys if r["category"] == "UNCLASSIFIED"]
        res["prior_tax_years"].append({
            "tax_year_start": t.isoformat(), "role": "RETURN_FLOW_EVIDENCE_ONLY",
            "source_files": sorted({r["source_file"] for r in ys}), "n_rows": len(ys),
            "first_date": next((r["date"] for r in ys if r["date"]), None),
            "last_date": next((r["date"] for r in reversed(ys) if r["date"]), None),
            "opening_balance_gbp": ob, "closing_balance_gbp": close, "flows_reconcile": recon,
            "external_flows": fl, "external_flow_total_gbp": round(sum(x["amount_gbp"] for x in fl), 2),
            "category_totals": _category_totals(ys),
            "unclassified": [{"date": r["date"], "description": r["description"][:60]} for r in unc],
            "rows": ys})
        if unc:
            res["warnings"].append("prior tax year from %s carries %d UNCLASSIFIED row(s) %s — reported as "
                                   "evidence only; it does not touch the current-year allowance"
                                   % (t.isoformat(), len(unc), [r["description"][:40] for r in unc[:3]]))
    res["evidence_note"] = ("prior-tax-year rows are RETURN-FLOW EVIDENCE only (Raj 13-Oct-2026, ISA-0642): never in "
                            "the allowance, the invariants, category_totals, the cash rate, deployable cash, the "
                            "account-identity check or the anchor flow ledger")

    if not cur_files:
        res["warnings"].append(
            "no Cash Statement on disk covers the CURRENT tax year from %s (files on disk: %s) — the allowance "
            "stays UNRECONCILED until Raj saves 'Cash Statement Current Tax Year %d-%d.xlsx'. A prior-year "
            "file is evidence of past flows, never of this year's allowance."
            % (ty_start.isoformat(), res["files_on_disk"], ty_start.year, ty_start.year + 1))
        return res

    res["category_totals"] = _category_totals(cur)
    contrib_rows = [r for r in cur if r["category"] == "CONTRIBUTION"]
    used = round(sum(r["receipt_gbp"] for r in contrib_rows), 2)
    res["allowance"] = {
        "tax_year_start": ty_start.isoformat(),
        "used_gbp": used,
        "remaining_gbp": round(ISA_ALLOWANCE_GBP - used, 2),
        "annual_gbp": ISA_ALLOWANCE_GBP,
        "contribution_rows": contrib_rows,
        "source": "AJ Bell Cash Statement (CONTRIBUTION rows only; the opening balance "
                  "carried forward is a POSITION and is excluded — see I12; a TRANSFER_IN is an "
                  "external flow, not a subscription — see EXTERNAL_FLOW_CATEGORIES)",
    }
    fl_cur = _external_flow_rows(cur)
    res["external_flows"] = {"rows": fl_cur, "total_gbp": round(sum(x["amount_gbp"] for x in fl_cur), 2),
                             "categories": sorted(EXTERNAL_FLOW_CATEGORIES)}
    ob, close, recon = _flow_recon(cur)
    res["opening_balance_gbp"] = ob
    res["closing_balance_gbp"] = close

    # continuity across the year boundary (informational: a break means a missing export period)
    if res["prior_tax_years"] and ob is not None:
        last = res["prior_tax_years"][-1]
        pc = last.get("closing_balance_gbp")
        res["cross_year_continuity"] = {
            "ok": pc is not None and abs(pc - ob) <= 0.05, "prior_closing_gbp": pc, "current_opening_gbp": ob,
            "prior_tax_year_start": last["tax_year_start"],
            "note": "the prior year's closing balance must equal this year's B/F; informational, never gates"}
        if not res["cross_year_continuity"]["ok"]:
            res["warnings"].append("cross-year continuity break: prior closing GBP%s vs current B/F GBP%s"
                                   % (pc, ob))

    # ── invariants (CURRENT tax year only) ──────────────────────────────────────────────
    inv = res["invariants"]
    inv["I11_contribution_within_allowance"] = {
        "ok": 0 <= used <= ISA_ALLOWANCE_GBP, "value": used}
    inv["I12_opening_balance_not_a_contribution"] = {
        "ok": all(r["category"] != "CONTRIBUTION" for r in cur
                  if OPENING_BALANCE_RE.match(r["description"])),
        "opening_balance_gbp": res["opening_balance_gbp"],
        "note": "the * BALANCE B/F * row is a position carried forward; counting it would "
                "overstate the allowance by its own value"}
    if recon is not None:
        inv["I13_flows_reconcile_to_closing_balance"] = dict(recon, note=(
            "opening + SUM(receipts - payments) must equal the stated closing balance; "
            "a break means a row was mis-signed or dropped"))
    unclassified = [r for r in cur if r["category"] == "UNCLASSIFIED"]
    inv["I14_every_row_classified"] = {
        "ok": not unclassified, "n": len(unclassified),
        "examples": [r["description"][:60] for r in unclassified[:5]],
        "note": "an unmatched description is reported, never bucketed into the nearest "
                "plausible category (scope: the CURRENT tax year - ISA-0642)"}
    inv["I15_one_file_per_current_tax_year"] = {
        "ok": len(cur_files) == 1, "files": cur_files,
        "note": ("two exports covering the same tax year would count every deposit twice; the "
                 "allowance is REFUSED rather than de-duplicated by guesswork (ISA-0642)")}
    res["reconciled"] = all(v.get("ok") for v in inv.values())
    if not res["reconciled"]:
        res["warnings"].append("one or more invariants failed — allowance is NOT reconciled")
    return res


def reconcile_with_ledger(res, ledger_path=None):
    """I10 — every BUY/SELL row in the cash statement must appear in `transaction_ledger.json`.

    Two genuinely independent derivations of the same trades: the broker's CASH view and the
    broker's DEALING view, exported separately and parsed by different code. Standard rule 4.

    Joined on the **broker reference** (e.g. `44626CGD9RL`), which both documents carry, with
    date+amount as a secondary key. An earlier draft joined on date+amount alone and reported
    25 of 25 unmatched — which was my own key-guessing, not 25 missing trades. Worth recording:
    a reconciliation that reports everything as broken is far more likely to be a broken
    reconciliation, and must be verified before it is believed.
    """
    lp = ledger_path or os.path.join(HERE, "transaction_ledger.json")
    if not os.path.exists(lp):
        return {"ok": None, "reason": "transaction_ledger.json not found — check SKIPPED, "
                                      "explicitly not passed"}
    try:
        led = json.load(open(lp, encoding="utf-8"))
    except Exception as e:
        return {"ok": None, "reason": f"ledger unreadable: {e}"}
    entries = (led.get("entries") or led.get("transactions") or led.get("rows") or []) \
        if isinstance(led, dict) else led
    by_ref, by_date_amt = {}, set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        ref = str(e.get("reference") or "").strip()
        amt = e.get("amount_gbp")
        try:
            amt = round(abs(float(amt)), 2)
        except (TypeError, ValueError):
            amt = None
        if ref:
            by_ref[ref] = amt
        if amt is not None:
            by_date_amt.add((str(e.get("date") or "")[:10], amt))

    # The ledger is exported periodically; the cash statement is live. Trades newer than the
    # ledger's horizon are NOT missing — they are simply ahead of it, and reporting them as
    # breaks every month would train the reader to ignore this invariant entirely.
    ledger_max = max((str(e.get("date") or "")[:10] for e in entries
                      if isinstance(e, dict) and e.get("date")), default="")
    unmatched, amount_breaks, pending = [], [], []
    checked = 0
    for r in res.get("rows", []):
        if r["category"] not in ("BUY", "SELL"):
            continue
        checked += 1
        amt = round(abs(r["receipt_gbp"] or r["payment_gbp"]), 2)
        ref = r["reference"]
        if ref and ref in by_ref:
            if by_ref[ref] is not None and abs(by_ref[ref] - amt) > 0.01:
                amount_breaks.append({"reference": ref, "cash_statement_gbp": amt,
                                      "ledger_gbp": by_ref[ref],
                                      "description": r["description"][:50]})
            continue
        if (r["date"], amt) in by_date_amt:
            continue
        rec = {"date": r["date"], "amount_gbp": amt, "reference": ref,
               "description": r["description"][:60]}
        (pending if (ledger_max and r["date"] and r["date"] > ledger_max)
         else unmatched).append(rec)
    return {"ok": (not unmatched) and (not amount_breaks), "checked": checked,
            "ledger_entries": len(entries), "ledger_horizon": ledger_max,
            "unmatched": unmatched, "amount_breaks": amount_breaks,
            "pending_ledger_update": pending,
            "note": "joined on broker reference, falling back to date+amount; an amount break "
                    "on a MATCHED reference is the serious case — the same trade recorded twice "
                    "with two different figures"}


def _selftest():
    ok = True
    # classification — every rule, including the ones that must NOT fire
    cases = [
        ("* BALANCE B/F *", "OPENING_BALANCE"),
        ("*BALANCE B/F*", "OPENING_BALANCE"),
        ("Faster Payment In", "CONTRIBUTION"),
        ("Direct debit payment", "CONTRIBUTION"),
        ("Debit Card Payment", "CONTRIBUTION"),
        ("FX Charge (0.50%)", "FX_FEE"),
        ("Account charge for funds - Jun 2026", "PLATFORM_FEE"),
        ("Account charge for shares - Jul 2026", "PLATFORM_FEE"),
        ("Dividend 498   SCOTTISH MORTGAGE", "DIVIDEND"),
        ("DIVIDEND 7   MICRON TECHNOLOGY", "DIVIDEND"),
        ("Gross interest to 30/06/26", "INTEREST"),
        ("Fund Cash Rebate May26", "REBATE"),
        ("Purchase 28 The Vita Coco Co Inc", "BUY"),
        ("Sale 40 PAYLOCITY HLDG COR COM", "SELL"),
        ("Residual Funds from BB8HNPI", "TRANSFER_IN"),      # Raj 19-Oct-2026: rounding transfer in
        ("TFER FROM BB8HNPI", "TRANSFER_IN"),                # Raj 13-Oct-2026: adviser-platform transfer
        ("Residual Funds from Artemis SmartGARP", "SELL"),   # a FUND, not an account: must NOT be a transfer
        ("Transfer from ISA cash", "TRANSFER"),              # two tokens: not an account transfer-in
        ("Bank transfer in", "CONTRIBUTION"),
        ("Equalisation Acc Units", "UNCLASSIFIED"),
        ("", "UNCLASSIFIED"),
    ]
    bad = [(d, classify(d), e) for d, e in cases if classify(d) != e]
    assert not bad, f"misclassified: {bad}"

    # ⚑ THE TRAP, as a first-class negative test: the opening balance must never be a
    # contribution however it is spelled, and the allowance must exclude it.
    for spelling in ("* BALANCE B/F *", "*  Balance b/f  *", "*BALANCE BF*"):
        assert classify(spelling) == "OPENING_BALANCE", f"B/F spelling missed: {spelling!r}"
    assert classify("Faster Payment In") in FLOW_CATEGORIES

    # end-to-end over a synthetic sheet with the trap present
    import tempfile
    try:
        import openpyxl
    except ImportError:
        print("SELFTEST PARTIAL — openpyxl unavailable; classification asserted, parse skipped")
        return ok
    with tempfile.TemporaryDirectory() as td:
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "cashstatements"
        ws.append(["Date", "Description", "Reference", "Settlement date",
                   "Receipt (GBP)", "Payment (GBP)", "Balance (GBP)"])
        ws.append([dt.datetime(2026, 4, 6), "* BALANCE B/F *", "-", "-", 3469.64, 0, 3469.64])
        ws.append([dt.datetime(2026, 4, 6), "Faster Payment In", "R1", "-", 5000, 0, 8469.64])
        ws.append([dt.datetime(2026, 5, 2), "Direct debit payment", "R2", "-", 1250, 0, 9719.64])
        ws.append([dt.datetime(2026, 6, 2), "Direct debit payment", "R3", "-", 1250, 0, 10969.64])
        ws.append([dt.datetime(2026, 7, 2), "Direct debit payment", "R4", "-", 1250, 0, 12219.64])
        ws.append([dt.datetime(2026, 7, 23), "FX Charge (0.50%)", "R5", "-", 0, 0.01, 12219.63])
        ws.append([dt.datetime(2026, 8, 3), "Purchase 28 Vita Coco", "R6",
                   dt.datetime(2026, 8, 4), 0, 1354.99, 10864.64])
        p = os.path.join(td, "Cash Statement Current Tax Year 2026-2027.xlsx")
        wb.save(p)
        r = parse(folder=td, as_of=dt.date(2026, 8, 5))
        a = r["allowance"]
        assert a["used_gbp"] == 8750.0, f"allowance wrong: {a['used_gbp']} (trap not excluded?)"
        assert a["remaining_gbp"] == 11250.0, a["remaining_gbp"]
        assert r["opening_balance_gbp"] == 3469.64
        assert r["invariants"]["I12_opening_balance_not_a_contribution"]["ok"]
        assert r["invariants"]["I11_contribution_within_allowance"]["ok"]
        assert r["invariants"]["I14_every_row_classified"]["ok"], \
            r["invariants"]["I14_every_row_classified"]
        assert r["reconciled"] is True, r["warnings"]

        # NEGATIVE CONTROL: if the trap were counted, the figure would be 12,219.64 — assert
        # the difference is exactly the opening balance, so the test proves WHY, not just WHAT.
        naive = round(sum(x["receipt_gbp"] for x in r["rows"]
                          if x["category"] in ("CONTRIBUTION", "OPENING_BALANCE")), 2)
        assert round(naive - a["used_gbp"], 2) == 3469.64, (naive, a["used_gbp"])

        # a missing file must WARN, never raise, and never state a figure
        empty = parse(folder=os.path.join(td, "nope"), as_of=dt.date(2026, 8, 5))
        assert empty["reconciled"] is False and empty["warnings"], empty
        assert "allowance" not in empty, "an absent file must not produce an allowance figure"

        # ── ISA-0642: a PRIOR-YEAR export beside the current one ─────────────────────────
        wp = openpyxl.Workbook(); wsp = wp.active
        wsp.append(["Date", "Description", "Reference", "Settlement date",
                    "Receipt (GBP)", "Payment (GBP)", "Balance (GBP)"])
        wsp.append([dt.datetime(2025, 4, 6), "* BALANCE B/F *", "-", "-", 0, 0, 0])
        wsp.append([dt.datetime(2025, 5, 1), "TFER FROM BB8HNPI", "P1", "-", 1477.83, 0, 1477.83])
        wsp.append([dt.datetime(2025, 6, 1), "Some Unknown Row", "P2", "-", 0, 0, 1477.83])
        wsp.append([dt.datetime(2026, 1, 30), "Faster Payment In", "P3", "-", 11500, 0, 12977.83])
        wsp.append([dt.datetime(2026, 2, 2), "Purchase 1 Fund", "P4", "-", 0, 9508.19, 3469.64])
        wp.save(os.path.join(td, "Cash Statement Previous Tax Year 2025 - 2026.xlsx"))
        r2 = parse(folder=td, as_of=dt.date(2026, 8, 5))
        assert r2["source_files"] == ["Cash Statement Current Tax Year 2026-2027.xlsx"], r2["source_files"]
        assert r2["reconciled"] is True, (r2["warnings"], r2["invariants"])
        assert r2["allowance"]["used_gbp"] == 8750.0, r2["allowance"]["used_gbp"]
        assert r2["opening_balance_gbp"] == 3469.64, "the CURRENT year's B/F, never the prior file's"
        assert all(x["date"] >= "2026-04-06" for x in r2["rows"]), "a prior-year row reached `rows`"
        assert r2["category_totals"] == r["category_totals"], "fees/FX must not span two tax years"
        assert r2["invariants"]["I14_every_row_classified"]["ok"], "a PRIOR-year unknown row must not fail I14"
        py = r2["prior_tax_years"]
        assert len(py) == 1 and py[0]["tax_year_start"] == "2025-04-06" and py[0]["role"] == "RETURN_FLOW_EVIDENCE_ONLY"
        assert py[0]["external_flow_total_gbp"] == 12977.83, py[0]["external_flow_total_gbp"]
        assert [x["category"] for x in py[0]["external_flows"]] == ["TRANSFER_IN", "CONTRIBUTION"]
        assert py[0]["unclassified"] and any("prior tax year" in w for w in r2["warnings"]), "evidence gap must be REPORTED"
        assert r2["cross_year_continuity"]["ok"] is True, r2["cross_year_continuity"]
        assert r2["external_flows"]["total_gbp"] == 8750.0
        # MUST-FIRE (order): a NEWEST-FIRST export with two same-day '-' rows closes on the LATER row
        wo = openpyxl.Workbook(); wso = wo.active
        wso.append(["Date", "Description", "Reference", "Settlement date",
                    "Receipt (GBP)", "Payment (GBP)", "Balance (GBP)"])
        wso.append([dt.datetime(2026, 4, 9), "Fund Cash Rebate Feb", "-", "-", 0.34, 0, 1250.34])
        wso.append([dt.datetime(2026, 4, 9), "Direct debit payment", "-", "-", 1250, 0, 1250.0])
        wso.append([dt.datetime(2026, 4, 6), "* BALANCE B/F *", "-", "-", 0, 0, 0])
        td3 = os.path.join(td, "newest_first"); os.makedirs(td3)
        wo.save(os.path.join(td3, "Cash Statement Current Tax Year 2026-2027.xlsx"))
        r6 = parse(folder=td3, as_of=dt.date(2026, 8, 5))
        assert r6["closing_balance_gbp"] == 1250.34, r6["closing_balance_gbp"]
        assert r6["rows"][0]["category"] == "OPENING_BALANCE"
        assert r6["invariants"]["I13_flows_reconcile_to_closing_balance"]["ok"], r6["invariants"]
        # NEGATIVE CONTROL: the SAME unknown row in the CURRENT year still fails I14
        ws.append([dt.datetime(2026, 8, 4), "Some Unknown Row", "R7", "-", 0, 0, 10864.64])
        wb.save(p)
        r3 = parse(folder=td, as_of=dt.date(2026, 8, 5))
        assert r3["reconciled"] is False and not r3["invariants"]["I14_every_row_classified"]["ok"]
        # MUST-FIRE: two exports covering the current tax year -> I15 refuses (no double count)
        wb.save(os.path.join(td, "Cash Statement Current Tax Year 2026-2027 (1).xlsx"))
        r4 = parse(folder=td, as_of=dt.date(2026, 8, 5))
        assert r4["invariants"]["I15_one_file_per_current_tax_year"]["ok"] is False and r4["reconciled"] is False
        # only a prior-year file, as_of in the NEXT tax year: no allowance figure, a precise warning
        td2 = os.path.join(td, "only_prior"); os.makedirs(td2)
        wp.save(os.path.join(td2, "Cash Statement Previous Tax Year 2025 - 2026.xlsx"))
        r5 = parse(folder=td2, as_of=dt.date(2026, 5, 1))
        assert "allowance" not in r5 and r5["reconciled"] is False and r5["rows"] == []
        assert any("CURRENT tax year" in w for w in r5["warnings"]), r5["warnings"]
        assert r5["prior_tax_years"] and r5["prior_tax_years"][0]["external_flow_total_gbp"] == 12977.83
    print("SELFTEST PASS — 22 classification rules (incl. TRANSFER_IN x2 and 2 that must NOT be "
          "transfers), 3 B/F spellings, allowance = 8,750 / 11,250, opening balance excluded, "
          "I11/I12/I14 green, negative control proving the trap is worth exactly 3,469.64, "
          "absent file warns without stating a figure; ISA-0642: prior-year file is evidence only "
          "(current B/F, fees one year, prior unknown row reported not failing, flows 12,977.83, "
          "continuity), newest-first same-day order closes on the later row, current-year unknown row still fails I14, two current files -> I15 refuses, "
          "prior-only -> no allowance + precise warning)")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=os.path.dirname(HERE))
    ap.add_argument("--out"); ap.add_argument("--ledger-check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    r = parse(folder=a.folder)
    if a.ledger_check:
        r["ledger_reconciliation"] = reconcile_with_ledger(r)
    if a.out:
        json.dump(r, open(a.out, "w"), indent=2, default=str)
    al = r.get("allowance")
    print(f"CASH_STATEMENT rows={len(r['rows'])} files={r['source_files']}")
    if al:
        print(f"  ISA allowance used GBP{al['used_gbp']:,.2f} / remaining "
              f"GBP{al['remaining_gbp']:,.2f}  (tax year from {al['tax_year_start']})")
        for c in al["contribution_rows"]:
            print(f"    {c['date']}  {c['description'][:34]:36s} GBP{c['receipt_gbp']:,.2f}")
    print(f"  opening balance GBP{r.get('opening_balance_gbp')} (EXCLUDED from allowance) | "
          f"closing GBP{r.get('closing_balance_gbp')}")
    print(f"  FX rate {FX_RATE_PCT}%")
    for k, v in r["invariants"].items():
        print(f"  [{'PASS' if v.get('ok') else 'FAIL'}] {k}"
              + (f"  {v}" if not v.get("ok") else ""))
    for w in r["warnings"]:
        print(f"  WARN {w}")
    if r.get("ledger_reconciliation"):
        lr = r["ledger_reconciliation"]
        print(f"  [I10] ledger reconciliation ok={lr.get('ok')} "
              f"checked={lr.get('checked')} unmatched={len(lr.get('unmatched') or [])} "
              f"pending_ledger_update={len(lr.get('pending_ledger_update') or [])} "
              f"(ledger horizon {lr.get('ledger_horizon')})")
        for x in (lr.get("pending_ledger_update") or []):
            print(f"      PENDING {x['date']} GBP{x['amount_gbp']:,.2f} {x['description'][:44]}")
        for x in (lr.get("unmatched") or []):
            print(f"      UNMATCHED {x['date']} GBP{x['amount_gbp']:,.2f} {x['description'][:44]}")
    sys.exit(0 if r["reconciled"] else 1)
