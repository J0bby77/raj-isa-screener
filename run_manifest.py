#!/usr/bin/env python3
"""
run_manifest.py — Capture Layer Item 2 / Dashboard Spec §7.6A. 02-Aug-2026.

THE DEFECT THIS CLOSES
----------------------
Every silent failure in this framework's history shared one signature: something didn't run,
and the log looked fine.

  - MEMORY_BASE pointed at a Windows path inside a Linux sandbox, so Step 5 had NEVER run and
    the trades-log read failed silently (01-Aug-2026).
  - `FETCH_WORKERS` was undefined, so the local metrics fetch raised NameError before touching
    Yahoo — for weeks — while the caller logged it as an expected architectural condition.
  - `verify_stores()` read the key "rows" while `capture()` wrote "observations", so the guard
    built to detect silent-write failure reported 0 and could not tell a healthy store from a
    missing one. Four VCI runs passed through it.
  - `score_panel.csv` stopped growing and the pre-run said WARN.
  - STOXX600 produces a ranking on 23% price coverage and nothing says so.

The common cause is not any of those bugs. It is the DEFAULT: absence is treated as benign.

WHAT THIS CHANGES
-----------------
1. **The default is inverted.** Zero rows out, or null-dominant output, is an ERROR, not a
   WARN. A step must declare a positive result to be considered to have run. `SILENT_OK` is
   not a status this module can produce.
2. **Declared coverage floors, enforced.** Each step carries a floor. Below it the step is
   DEGRADED; far below, or empty, it is ERROR. `gate_emission()` lets a consumer REFUSE to
   emit rather than publish a ranking built on 23% of its inputs.
3. **A permanent per-run record.** `run_manifest_[mmm]_[yyyy].json` — per step: ran / skipped /
   degraded / error, rows in, rows out, coverage %, duration, and the config fingerprint under
   which it ran. This is the file that makes "which steps actually executed, and on what
   coverage?" answerable after the fact instead of never.
4. **Fifteen-second vital signs in the email** via `email_block_html()`.

Per build hazard H7 this module observes and reports. It changes no weight, gate or threshold.

CLI:
  python3 run_manifest.py --selftest
  python3 run_manifest.py --show run_manifest_aug_2026.json

Library:
  from run_manifest import Manifest
  mf = Manifest("aug_2026")
  with mf.step("6", "fetch_watchlist_metrics") as st:
      st.rows_in(len(tickers)); st.rows_out(n); st.coverage(n / len(tickers))
  mf.write()
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1

# Status ladder. Ordered worst-last so max() gives the run-level status.
OK, SKIPPED, ACKNOWLEDGED, DEGRADED, ERROR = "OK", "SKIPPED", "ACKNOWLEDGED", "DEGRADED", "ERROR"
_RANK = {SKIPPED: 0, OK: 1, ACKNOWLEDGED: 2, DEGRADED: 3, ERROR: 4}

# ── acknowledgements: the ONLY way a condition may be less than its true severity ─────────
#
# Without this, the first run after fail-loud lands would ERROR on several PRE-EXISTING
# conditions and block the framework outright. With a naive suppression list, the framework
# rots instead: the list grows, everything is acknowledged, and the alarm means nothing again
# — which is precisely the state fail-loud exists to escape.
#
# So an acknowledgement is a DATED, REGISTERED, EXPIRING contract:
#   * it must name a calibration_registry decision id (someone owns it),
#   * it must state why the run may proceed anyway,
#   * it must carry an expiry date, and ON EXPIRY THE CONDITION REVERTS TO ITS TRUE SEVERITY,
#     with the expiry called out by name.
# An acknowledged condition is still reported, still counted, and still written to the
# manifest. It is downgraded, never hidden.
ACK_FILE = "manifest_acknowledgements.json"


def load_acknowledgements(path=None, today=None):
    import datetime as _dt
    path = path or os.path.join(HERE, ACK_FILE)
    today = today or _dt.date.today()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    return _normalise_acks(raw.get("acknowledgements", []), today=today)


def _normalise_acks(acks, today=None):
    """Apply the same validity rules to hand-passed acks as to the file: an ack needs an
    owner and a valid expiry, or it does not apply. Tests must not be able to construct an
    ack that production would reject."""
    import datetime as _dt
    today = today or _dt.date.today()
    out = []
    for a in acks or []:
        if not a.get("registry_id"):
            continue
        try:
            exp = _dt.date.fromisoformat(str(a.get("expires")))
        except Exception:
            continue
        a = dict(a)
        a["_expired"] = exp < today
        a["_expiry_date"] = exp.isoformat()
        out.append(a)
    return out


def acknowledgement_for(step_id, blob, acks):
    """Public form of the ack lookup, for consumers that gate OUTSIDE a manifest step —
    principally the action-stack emission refusal, which must honour the same registered
    acknowledgement as the step whose coverage it is judging. Otherwise acknowledging a
    condition in one place and blocking on it in another produces a run that cannot proceed
    and cannot say why."""
    return _match_ack({"id": str(step_id), "name": "", "notes": [str(blob)]}, acks or [])


def _match_ack(step_rec, acks):
    for a in acks:
        if str(a.get("step")) != str(step_rec["id"]):
            continue
        pat = a.get("match")
        blob = (step_rec["name"] + " " + " ".join(step_rec.get("notes", []))).lower()
        if pat and pat.lower() not in blob:
            continue
        return a
    return None

# ── declared coverage floors ─────────────────────────────────────────────────────────────
# floor  = below this the step is DEGRADED (it ran, but the output is not trustworthy whole)
# refuse = at or below this a CONSUMER MUST REFUSE TO EMIT (see gate_emission)
#
# The STOXX600 case sets the refuse level: a ranking published on 23% price coverage is not a
# weak ranking, it is a different question answered by accident. 0.60 is the point at which a
# cross-sectional rank order stops being about the universe and starts being about who had
# data. Registered as CAP-4 rather than tuned.
FLOORS = {
    "1":    {"floor": 1.00, "refuse": 0.99, "what": "portfolio extract — every holding or none"},
    "1b":   {"floor": 0.00, "refuse": None, "what": "transaction import — absence is a WARN by policy"},
    "1.5":  {"floor": 0.90, "refuse": None, "what": "ledger reconciliation"},
    "2":    {"floor": 1.00, "refuse": 0.99, "what": "X-Ray extract"},
    "3":    {"floor": 0.95, "refuse": None, "what": "portfolio analytics"},
    "4":    {"floor": 0.90, "refuse": None, "what": "watchlist update"},
    "5":    {"floor": 0.90, "refuse": None, "what": "VCI watchlist sync"},
    "6":    {"floor": 0.90, "refuse": 0.60, "what": "metrics fetch — tickers priced / tickers needed"},
    "6.5":  {"floor": 0.90, "refuse": None, "what": "VCI re-price"},
    "7":    {"floor": 0.95, "refuse": 0.60, "what": "Part A/B scoring"},
    "7.25": {"floor": 0.80, "refuse": None, "what": "entry levels — provisional is allowed"},
    "7.5":  {"floor": 0.95, "refuse": 0.60, "what": "re-rank"},
    "8":    {"floor": 0.95, "refuse": 0.60, "what": "step9_pre build"},
    "9":    {"floor": 0.95, "refuse": None, "what": "email prefill"},
    "cal":  {"floor": 0.00, "refuse": None, "what": "calibration report — pre-gate by design"},
    "9d":   {"floor": 1.00, "refuse": None, "what": "mechanical asserts"},
    "action_stack": {"floor": 0.90, "refuse": 0.60,
                     "what": "action stack — REFUSES to emit below the floor"},
    "screen": {"floor": 0.85, "refuse": 0.60, "what": "weekly screen price coverage"},
}
DEFAULT_FLOOR = {"floor": 0.90, "refuse": 0.60, "what": ""}

# Below this share of non-null cells an output is "null-dominant" and the step ERRORs even if
# its row count looks healthy. This is the shape the FETCH_WORKERS bug produced: rows present,
# values absent.
NULL_DOMINANT_BELOW = 0.50


class EmissionRefused(RuntimeError):
    """Raised by gate_emission when a consumer's own inputs are below its refuse level.

    Deliberately an exception rather than a return value: the STOXX600 defect is that a
    ranking gets published anyway, and a caller that must remember to check a flag will
    eventually forget."""


class _Step:
    def __init__(self, sid, name, manifest):
        self.id, self.name, self._mf = sid, name, manifest
        self.t0 = time.time()
        self.rec = {
            "id": sid, "name": name, "status": None, "rows_in": None, "rows_out": None,
            "coverage": None, "non_null_share": None, "duration_s": None,
            "notes": [], "floor": FLOORS.get(sid, DEFAULT_FLOOR)["floor"],
            "refuse_below": FLOORS.get(sid, DEFAULT_FLOOR)["refuse"],
            "what": FLOORS.get(sid, DEFAULT_FLOOR)["what"],
        }

    # -- fluent recorders ---------------------------------------------------------------
    def rows_in(self, n):
        self.rec["rows_in"] = int(n) if n is not None else None
        return self

    def rows_out(self, n):
        self.rec["rows_out"] = int(n) if n is not None else None
        return self

    def coverage(self, frac):
        self.rec["coverage"] = round(float(frac), 4) if frac is not None else None
        return self

    def non_null_share(self, frac):
        self.rec["non_null_share"] = round(float(frac), 4) if frac is not None else None
        return self

    def note(self, msg):
        self.rec["notes"].append(str(msg)[:400])
        return self

    def skipped(self, why):
        self.rec["status"] = SKIPPED
        return self.note(f"skipped: {why}")

    def failed(self, why):
        self.rec["status"] = ERROR
        return self.note(f"failed: {why}")

    # -- the inverted default ------------------------------------------------------------
    def _classify(self):
        if self.rec["status"] in (SKIPPED, ERROR):
            return self.rec["status"]
        ro, cov, nn = self.rec["rows_out"], self.rec["coverage"], self.rec["non_null_share"]
        floor = self.rec["floor"]

        # (a) Nothing came out. Under the old default this was a WARN and the run continued
        #     reporting success. It is an ERROR.
        if ro is not None and ro == 0:
            self.note("ERROR: zero rows out — a step that produces nothing has not run, "
                      "whatever its exit code said")
            return ERROR
        # (b) Rows present, values absent — the FETCH_WORKERS shape.
        if nn is not None and nn < NULL_DOMINANT_BELOW:
            self.note(f"ERROR: null-dominant output ({nn:.0%} non-null) — rows exist but the "
                      f"values do not")
            return ERROR
        # (c) A step that declared nothing at all cannot be assumed to have worked.
        if ro is None and cov is None:
            self.note("DEGRADED: step declared no rows_out and no coverage — cannot be "
                      "distinguished from a step that silently did nothing")
            return DEGRADED
        if cov is not None and cov < floor:
            self.note(f"DEGRADED: coverage {cov:.0%} below declared floor {floor:.0%}")
            return DEGRADED
        return OK

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.rec["duration_s"] = round(time.time() - self.t0, 2)
        if exc is not None:
            self.rec["status"] = ERROR
            self.note(f"exception: {exc_type.__name__}: {exc}")
        else:
            self.rec["status"] = self._classify()
        self._mf._apply_ack(self.rec)
        self._mf._add(self.rec)
        return False   # never swallow


class Manifest:
    def __init__(self, month_label, script_dir=None, config_fingerprint=None,
                 acknowledgements=None):
        self.month_label = month_label
        self.dir = script_dir or HERE
        self.steps = []
        self.t0 = time.time()
        self.fingerprint = config_fingerprint or _config_fingerprint()
        self.acks = (_normalise_acks(acknowledgements) if acknowledgements is not None
                     else load_acknowledgements(os.path.join(self.dir, ACK_FILE)))

    def _apply_ack(self, rec):
        """Downgrade a KNOWN, REGISTERED, UNEXPIRED condition to ACKNOWLEDGED. Everything
        about it is still recorded — this changes how loudly it is reported, not whether."""
        if rec["status"] not in (ERROR, DEGRADED):
            return
        a = _match_ack(rec, self.acks)
        if not a:
            return
        if a.get("_expired"):
            rec["notes"].append(
                f"ACKNOWLEDGEMENT {a['registry_id']} EXPIRED on {a['_expiry_date']} — this "
                f"condition has reverted to {rec['status']}. It was accepted as a known "
                f"pre-existing issue, not as a permanent exemption.")
            rec["ack_expired"] = a["registry_id"]
            return
        rec["true_status"] = rec["status"]
        rec["status"] = ACKNOWLEDGED
        rec["ack"] = {"registry_id": a["registry_id"], "expires": a["_expiry_date"],
                      "reason": a.get("reason", "")}
        rec["notes"].append(
            f"ACKNOWLEDGED under {a['registry_id']} until {a['_expiry_date']} "
            f"(true status {rec['true_status']}): {a.get('reason','')}")

    def _add(self, rec):
        self.steps.append(rec)

    def step(self, sid, name):
        return _Step(str(sid), name, self)

    def record(self, sid, name, status=None, rows_in=None, rows_out=None, coverage=None,
               non_null_share=None, duration_s=None, notes=None):
        """Non-context-manager form, for wrapping a call site that already has its result."""
        st = _Step(str(sid), name, self)
        st.rows_in(rows_in).rows_out(rows_out).coverage(coverage).non_null_share(non_null_share)
        for n in (notes or []):
            st.note(n)
        st.rec["duration_s"] = duration_s
        st.rec["status"] = status or st._classify()
        self._apply_ack(st.rec)
        self._add(st.rec)
        return st.rec

    # -- run-level roll-up ---------------------------------------------------------------
    @property
    def status(self):
        if not self.steps:
            return ERROR
        return max((s["status"] for s in self.steps), key=lambda s: _RANK.get(s, 3))

    def errors(self):
        return [f"[{s['id']}] {s['name']}: " + "; ".join(s["notes"])
                for s in self.steps if s["status"] == ERROR]

    def degraded(self):
        return [f"[{s['id']}] {s['name']}: " + "; ".join(s["notes"])
                for s in self.steps if s["status"] == DEGRADED]

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "month": self.month_label,
            "produced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_duration_s": round(time.time() - self.t0, 2),
            "run_status": self.status,
            "config_fingerprint": self.fingerprint,
            "counts": {s: sum(1 for x in self.steps if x["status"] == s)
                       for s in (OK, SKIPPED, ACKNOWLEDGED, DEGRADED, ERROR)},
            "acknowledged": [
                {"id": x["id"], "name": x["name"], "true_status": x.get("true_status"),
                 **(x.get("ack") or {})} for x in self.steps if x["status"] == ACKNOWLEDGED],
            "expired_acknowledgements": [
                {"id": x["id"], "name": x["name"], "registry_id": x["ack_expired"]}
                for x in self.steps if x.get("ack_expired")],
            "steps": self.steps,
            "errors": self.errors(),
            "degraded": self.degraded(),
        }

    def write(self, path=None):
        path = path or os.path.join(self.dir, f"run_manifest_{self.month_label}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return path


# ── consumer-side refusal ────────────────────────────────────────────────────────────────

def gate_emission(consumer, coverage, rows_out=None, floors=None, raise_on_refuse=True):
    """Should `consumer` be allowed to emit, given the coverage of its own inputs?

    Returns (allowed: bool, reason: str). With raise_on_refuse=True (the default) a refusal
    raises EmissionRefused, because the failure this exists to prevent is precisely a caller
    publishing anyway.
    """
    spec = (floors or FLOORS).get(consumer, DEFAULT_FLOOR)
    refuse = spec.get("refuse")
    if rows_out is not None and rows_out == 0:
        reason = (f"{consumer}: REFUSING to emit — zero rows. An empty ranking is not a "
                  f"ranking; publishing it would look like 'no opportunities'.")
        if raise_on_refuse:
            raise EmissionRefused(reason)
        return False, reason
    if refuse is not None and coverage is not None and coverage <= refuse:
        reason = (f"{consumer}: REFUSING to emit — input coverage {coverage:.0%} is at or "
                  f"below the refuse level {refuse:.0%}. A cross-sectional ranking built on "
                  f"this much of its universe orders the names that had data, not the names "
                  f"that are best.")
        if raise_on_refuse:
            raise EmissionRefused(reason)
        return False, reason
    floor = spec.get("floor")
    if floor is not None and coverage is not None and coverage < floor:
        return True, (f"{consumer}: emitting DEGRADED — coverage {coverage:.0%} below floor "
                      f"{floor:.0%}. Output is published but must be labelled.")
    return True, ""


# ── config fingerprint ───────────────────────────────────────────────────────────────────

def _config_fingerprint():
    """Reuse calibration_guard's fingerprint so the manifest and the calibration record agree
    on what 'this config' means. Falls back to a marker rather than inventing a hash."""
    try:
        sys.path.insert(0, HERE)
        import calibration_guard as cg
        fp = cg.config_fingerprint()
        return {"hash": fp.get("hash"), "n_params": len(fp.get("params", {}))}
    except Exception as e:
        return {"hash": None, "error": f"unavailable: {e}"}


# ── email block (§7.6A: machine vital signs readable in fifteen seconds) ──────────────────

_COLOUR = {OK: "#16a34a", SKIPPED: "#64748b", ACKNOWLEDGED: "#0891b2",
           DEGRADED: "#d97706", ERROR: "#dc2626"}

# Run_Context "Email HTML Rules" #1: no Unicode above U+007F in the body string. Notes are
# written to JSON where Unicode is fine, so the substitution happens at RENDER time only —
# banning em dashes in the notes themselves would push the constraint into the wrong place.
_ENT = {"\u2014": "&mdash;", "\u2013": "&ndash;", "\u2265": "&ge;", "\u2264": "&le;",
        "\u2212": "&minus;", "\u00a7": "&sect;", "\u2192": "&rarr;", "\u00b7": "&middot;",
        "\u00d7": "x", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."}


def _ascii(text):
    out = []
    for ch in str(text):
        if ord(ch) < 128:
            out.append(ch)
        else:
            out.append(_ENT.get(ch, "?"))
    return "".join(out)


def email_block_html(manifest_dict, max_rows=None, compact=True):
    """Compact inline-styled block for the monthly email.

    Deliberately small: the Aug-2026 report could not be sent at 196KB, ~61% of which was
    mandated inline styles. One table, one row per step, no nested wrappers.
    """
    d = manifest_dict
    rs = d.get("run_status", ERROR)
    c = d.get("counts", {})
    head = (
        f'<div style="font-family:Arial,sans-serif;font-size:13px;color:#e2e8f0;'
        f'border-left:4px solid {_COLOUR.get(rs, "#dc2626")};padding:8px 12px;margin:12px 0;'
        f'background:#0f172a">'
        f'<b style="color:{_COLOUR.get(rs, "#dc2626")}">RUN {rs}</b> &mdash; '
        f'{c.get(OK,0)} ok &middot; {c.get(ACKNOWLEDGED,0)} ack &middot; '
        f'{c.get(DEGRADED,0)} degraded &middot; '
        f'{c.get(ERROR,0)} error &middot; {c.get(SKIPPED,0)} skipped &middot; '
        f'{d.get("total_duration_s","?")}s &middot; '
        f'cfg {(d.get("config_fingerprint") or {}).get("hash") or "n/a"}</div>'
    )
    rows = []
    steps = d.get("steps", [])
    # COMPACT by default. The Aug-2026 report could not be sent at 196KB, ~61% of which was
    # mandated inline styles, so a diagnostics panel that lists 16 healthy steps every month
    # is buying attention at the price of deliverability. Show what needs attention; the full
    # per-step record is in run_manifest_[mmm]_[yyyy].json, which is what it is for.
    if compact:
        steps = [x for x in steps if x["status"] != OK]
    if max_rows:
        steps = steps[:max_rows]
    for s in steps:
        cov = s.get("coverage")
        cov_txt = f"{cov:.0%}" if isinstance(cov, (int, float)) else "&ndash;"
        rows.append(
            f'<tr><td style="padding:2px 6px;color:#94a3b8">{s["id"]}</td>'
            f'<td style="padding:2px 6px;color:#cbd5e1">{s["name"]}</td>'
            f'<td style="padding:2px 6px;color:{_COLOUR.get(s["status"], "#94a3b8")}">'
            f'{s["status"]}</td>'
            f'<td style="padding:2px 6px;color:#94a3b8;text-align:right">'
            f'{s.get("rows_out") if s.get("rows_out") is not None else "&ndash;"}</td>'
            f'<td style="padding:2px 6px;color:#94a3b8;text-align:right">{cov_txt}</td>'
            f'<td style="padding:2px 6px;color:#64748b;text-align:right">'
            f'{s.get("duration_s") or "&ndash;"}s</td></tr>')
    table = ('<table style="border-collapse:collapse;font-family:Arial,sans-serif;'
             'font-size:12px;width:100%">'
             '<tr><td style="padding:2px 6px;color:#64748b">step</td>'
             '<td style="padding:2px 6px;color:#64748b">script</td>'
             '<td style="padding:2px 6px;color:#64748b">status</td>'
             '<td style="padding:2px 6px;color:#64748b;text-align:right">rows</td>'
             '<td style="padding:2px 6px;color:#64748b;text-align:right">cov</td>'
             '<td style="padding:2px 6px;color:#64748b;text-align:right">t</td></tr>'
             + "".join(rows) + '</table>')
    if compact and not rows:
        return _ascii(head + '<div style="font-family:Arial,sans-serif;font-size:12px;'
                             'color:#64748b">All steps OK. Full per-step record: '
                             'run_manifest JSON.</div>')
    errs = ""
    if d.get("errors"):
        errs = ('<div style="font-family:Arial,sans-serif;font-size:12px;color:#fca5a5;'
                'margin-top:6px">' + "<br>".join(d["errors"][:8]) + "</div>")
    return _ascii(head + table + errs)


def email_block_text(manifest_dict):
    d = manifest_dict
    c = d.get("counts", {})
    lines = [f"RUN {d.get('run_status')} — {c.get(OK,0)} ok / {c.get(ACKNOWLEDGED,0)} ack / "
             f"{c.get(DEGRADED,0)} degraded / {c.get(ERROR,0)} error / "
             f"{c.get(SKIPPED,0)} skipped ({d.get('total_duration_s')}s)"]
    for s in d.get("steps", []):
        cov = s.get("coverage")
        lines.append(f"  [{s['id']:>5}] {s['name'][:28]:<28} {s['status']:<9} "
                     f"rows={s.get('rows_out')} cov="
                     f"{(f'{cov:.0%}' if isinstance(cov,(int,float)) else '-')}")
    return "\n".join(lines)


# ── self-test ────────────────────────────────────────────────────────────────────────────

def _selftest():
    import tempfile
    fails = []

    def ok_(label, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(label)

    mf = Manifest("sep_2026", script_dir=tempfile.gettempdir())

    with mf.step("6", "fetch_watchlist_metrics") as st:
        st.rows_in(120).rows_out(118).coverage(118 / 120).non_null_share(0.97)
    ok_("U-RM1 healthy step is OK", mf.steps[-1]["status"] == OK)

    # THE inverted default: zero rows is an ERROR, not a WARN.
    with mf.step("7", "scoring") as st:
        st.rows_in(118).rows_out(0)
    ok_("U-RM2 zero rows out -> ERROR (was WARN)", mf.steps[-1]["status"] == ERROR)

    # THE FETCH_WORKERS shape: rows present, values absent.
    with mf.step("7.5", "rerank") as st:
        st.rows_in(118).rows_out(118).coverage(1.0).non_null_share(0.11)
    ok_("U-RM3 null-dominant output -> ERROR", mf.steps[-1]["status"] == ERROR)

    # A step that declares nothing cannot be assumed to have worked.
    with mf.step("5", "sync_vci_watchlist") as st:
        pass
    ok_("U-RM4 step declaring nothing -> DEGRADED, never OK",
        mf.steps[-1]["status"] == DEGRADED)

    # Coverage below floor is DEGRADED, not silent.
    with mf.step("4", "update_watchlist") as st:
        st.rows_in(100).rows_out(72).coverage(0.72)
    ok_("U-RM5 coverage below floor -> DEGRADED", mf.steps[-1]["status"] == DEGRADED)

    with mf.step("1b", "extract_transactions") as st:
        st.skipped("no transaction export saved this month")
    ok_("U-RM6 explicit skip is SKIPPED, not ERROR", mf.steps[-1]["status"] == SKIPPED)

    # An exception inside a step must be recorded, not swallowed.
    try:
        with mf.step("3", "portfolio_analytics") as st:
            raise ValueError("boom")
    except ValueError:
        pass
    ok_("U-RM7 exception recorded as ERROR and re-raised",
        mf.steps[-1]["status"] == ERROR and "boom" in mf.steps[-1]["notes"][0])

    ok_("U-RM8 run status is the worst step status", mf.status == ERROR, mf.status)
    ok_("U-RM9 errors listed for the email", len(mf.errors()) >= 3)

    # THE STOXX600 CASE: a consumer must refuse to emit on 23% coverage.
    refused = False
    try:
        gate_emission("action_stack", coverage=0.23, rows_out=40)
    except EmissionRefused:
        refused = True
    ok_("U-RM10 action stack REFUSES to emit at 23% coverage", refused)

    allowed, why = gate_emission("action_stack", coverage=0.72, rows_out=40,
                                 raise_on_refuse=False)
    ok_("U-RM11 between refuse and floor: emits but labelled DEGRADED",
        allowed and "DEGRADED" in why)
    allowed2, why2 = gate_emission("action_stack", coverage=0.98, rows_out=40)
    ok_("U-RM12 healthy coverage emits clean", allowed2 and not why2)

    refused0 = False
    try:
        gate_emission("action_stack", coverage=1.0, rows_out=0)
    except EmissionRefused:
        refused0 = True
    ok_("U-RM13 zero-row emission refused (an empty ranking reads as 'no opportunities')",
        refused0)

    # ── acknowledgements ────────────────────────────────────────────────────────────────
    acks = [{"step": "8", "match": "null-dominant", "registry_id": "CAP-5-x",
             "reason": "pre-existing", "expires": "2099-01-01"},
            {"step": "9", "match": "declared no rows", "registry_id": "CAP-6-x",
             "reason": "expired one", "expires": "2000-01-01"}]
    mf2 = Manifest("sep_2026", script_dir=tempfile.gettempdir(), acknowledgements=acks)
    with mf2.step("8", "step9_pre_builder") as st:
        st.rows_out(41).non_null_share(0.15)
    ok_("U-RM19 registered unexpired ack downgrades ERROR to ACKNOWLEDGED",
        mf2.steps[-1]["status"] == ACKNOWLEDGED and mf2.steps[-1]["true_status"] == ERROR)
    ok_("U-RM20 acknowledgement still records the true severity and its owner",
        mf2.steps[-1]["ack"]["registry_id"] == "CAP-5-x")
    with mf2.step("9", "email_prefill") as st:
        pass
    ok_("U-RM21 EXPIRED ack does NOT downgrade — condition reverts",
        mf2.steps[-1]["status"] == DEGRADED and mf2.steps[-1].get("ack_expired") == "CAP-6-x")
    mf3 = Manifest("sep_2026", script_dir=tempfile.gettempdir(),
                   acknowledgements=[{"step": "8", "match": "null-dominant",
                                      "reason": "no owner", "expires": "2099-01-01"}])
    with mf3.step("8", "x") as st:
        st.rows_out(41).non_null_share(0.15)
    ok_("U-RM22 ack without a registry owner does not apply", mf3.steps[-1]["status"] == ERROR)
    mf4 = Manifest("sep_2026", script_dir=tempfile.gettempdir(),
                   acknowledgements=[{"step": "8", "match": "null-dominant",
                                      "registry_id": "X", "reason": "no expiry"}])
    with mf4.step("8", "x") as st:
        st.rows_out(41).non_null_share(0.15)
    ok_("U-RM23 ack without an expiry does not apply", mf4.steps[-1]["status"] == ERROR)

    with tempfile.TemporaryDirectory() as td:
        p = mf.write(os.path.join(td, "m.json"))
        back = json.load(open(p, encoding="utf-8"))
        ok_("U-RM14 manifest round-trips", back["run_status"] == ERROR
            and len(back["steps"]) == len(mf.steps))
        html = email_block_html(back)
        ok_("U-RM15 email block renders, no <style>/<head>, no flex",
            "<style" not in html and "<head" not in html and "flex" not in html)
        ok_("U-RM16 email block is compact (<8KB)", len(html) < 8192, f"{len(html)}B")
        ok_("U-RM17 email block is ASCII-safe (Run_Context email rule 1)",
            all(ord(ch) < 128 for ch in html))
        ok_("U-RM18 text block renders", "RUN ERROR" in email_block_text(back))

    print("SELFTEST PASS" if not fails else f"SELFTEST FAIL ({len(fails)}) {fails}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--show")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.show:
        d = json.load(open(a.show, encoding="utf-8"))
        print(email_block_text(d))
        return 0 if d.get("run_status") != ERROR else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
