#!/usr/bin/env python3
"""
vci_watchlist_store.py - ISA-0796 (10-Oct-2026): the ONE home for the VCI asymmetric watchlist.

WHY. Until 10-Oct-2026 the VCI watchlist lived in an assistant-memory file
(project_isa_vci_watchlist.md in the Cowork auto-memory folder). The 16-Sep memory import moved that
folder off disk: MEASURED 10-Oct, no copy exists anywhere in the ISA tree, MEMORY_BASE resolves to
nothing, and update_vci_watchlist.py - handed a missing path - printed "generating from scratch" and
would have written a watchlist with no excluded list, no re-score calendar and no change log (FC-A).
Framework state that decides which names the VCI review re-scores (vci_review_universe ROLE_WATCH) must
live in the framework's own tree, versioned, with a refusal when it cannot be read.

THE ARTEFACTS.
  Dashboard/state/vci_watchlist.json          CANONICAL STORE (MUTABLE_RUNTIME_STATE, writer vci_run).
  Dashboard/state/vci_watchlist_history.jsonl APPEND-ONLY: one line per write (revision, shas, membership diff).
  project_isa_vci_watchlist.md (beside the code) GENERATED VIEW - rendered from the store on every write;
                                               never hand-edited. Parseable by sync_vci_watchlist (ISA-0792).

THE RULES.
  * One writer path: update_vci_watchlist.py -> apply_update() -> write(). write() re-renders the view.
  * An ACS that was not measured is None with acs_state UNMEASURED and renders "UNMEASURED" - never 0
    (ISA-0667: a manufactured zero is a measurement).
  * load() on an absent/unreadable store RAISES (R4.3): a reader never silently gets an empty watchlist.
  * Max five names. More than five is trimmed by ACS (UNMEASURED last) and the trim is RECORDED in the
    change log and the history line - never silent (FC-I).
Stdlib only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_REL = os.path.join("Dashboard", "state", "vci_watchlist.json")
HISTORY_REL = os.path.join("Dashboard", "state", "vci_watchlist_history.jsonl")
VIEW_NAME = "project_isa_vci_watchlist.md"
SCHEMA_VERSION = "1.0.0"
MAX_NAMES = 5
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
ACS_STATES = ("MEASURED", "UNMEASURED")

try:
    from framework_integrity import _mark as _fi_mark
except Exception:                                                       # noqa: BLE001
    def _fi_mark(*_a, **_k):                                            # noqa: D103
        return None


class StoreError(RuntimeError):
    """The canonical store is absent, unreadable or an update is malformed. Never an empty watchlist."""


def store_path(root: str = HERE) -> str:
    return os.path.join(root, STORE_REL)


def history_path(root: str = HERE) -> str:
    return os.path.join(root, HISTORY_REL)


def view_path(root: str = HERE) -> str:
    return os.path.join(root, VIEW_NAME)


def _sha_doc(doc) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load(root: str = HERE) -> dict:
    """The canonical store. RAISES StoreError when absent or unreadable (R4.3)."""
    p = store_path(root)
    if not os.path.exists(p):
        raise StoreError("ISA-0796: no VCI watchlist store at %s - the watchlist is UNKNOWN, never empty "
                         "(bootstrap once with update_vci_watchlist.py --bootstrap-from)" % p)
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:                                            # noqa: BLE001
        raise StoreError("ISA-0796: VCI watchlist store unreadable (%s: %s)" % (type(exc).__name__, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("watchlist"), list):
        raise StoreError("ISA-0796: VCI watchlist store has no `watchlist` list")
    return doc


def members(root: str = HERE) -> list:
    """Tickers on the watchlist, in rank order. RAISES StoreError (never [] for 'could not read')."""
    _fi_mark("vci_watchlist_store", "members")
    return [r["ticker"] for r in sorted(load(root)["watchlist"], key=lambda r: r.get("rank") or 99)]


# ── validation ────────────────────────────────────────────────────────────────────────────────────────
_ALIASES = {"3yr_pos_pct": "three_yr_pos_pct", "acs_score": "acs", "exch": "exchange",
            "vci_source_score": "source_score", "fv_asymmetry": "asym", "fv_asymmetry_p25": "asym_p25",
            "fv_floor": "floor"}


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise StoreError("a boolean is not a number: %r" % v)
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    if not m:
        return None
    x = float(m.group())
    return int(x) if x.is_integer() and "." not in m.group() else x


def normalise_row(raw: dict) -> dict:
    """One watchlist row, validated. RAISES StoreError on a malformed row."""
    r = {}
    for k, v in (raw or {}).items():
        r[_ALIASES.get(k, k)] = v
    t = str(r.get("ticker") or "").replace("*", "").strip().upper()
    if not TICKER_RE.match(t):
        raise StoreError("ISA-0796: %r is not a ticker - a watchlist row needs one" % r.get("ticker"))
    r["ticker"] = t
    acs = _num(r.get("acs"))
    if acs is not None and not (0 <= acs <= 100):
        raise StoreError("ISA-0796: %s ACS %r outside 0..100" % (t, acs))
    state = r.get("acs_state")
    if acs is None:
        if state not in (None, "UNMEASURED"):
            raise StoreError("ISA-0796: %s has no ACS but acs_state %r - an absent ACS is UNMEASURED" % (t, state))
        r["acs_state"] = "UNMEASURED"                     # ISA-0667: never a manufactured 0
    else:
        r["acs"] = int(round(acs))
        r["acs_state"] = "MEASURED"
    r["acs"] = None if acs is None else r["acs"]
    for k in ("source_score", "asym", "asym_p25", "floor"):
        if k in r:
            r[k] = _num(r[k])
    if r.get("rank") is not None:
        r["rank"] = int(_num(r["rank"]))
    return r


def _trim(rows: list) -> tuple:
    if len(rows) <= MAX_NAMES:
        return rows, []
    ranked = sorted(rows, key=lambda r: (r.get("acs") is None, -(r.get("acs") or 0)))
    keep, drop = ranked[:MAX_NAMES], ranked[MAX_NAMES:]
    keep_t = {r["ticker"] for r in keep}
    return [r for r in rows if r["ticker"] in keep_t], [r["ticker"] for r in drop]


def empty_doc() -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "as_of": None, "run_month": None, "run_note": "",
            "watchlist": [], "nvidia_class_note": "", "rescore_calendar": [], "excluded": [],
            "amba_monitor": [], "change_log": [], "narrative_sections": [], "provenance": {}}


def update_sha(update: dict) -> str:
    return _sha_doc(update)


def is_repeat(prev: dict, update: dict, run_date: str) -> bool:
    """R5.14 repeat_same_occurrence: the SAME update for the SAME run date is already applied - a retry
    of the post-run step must change nothing (no second revision, no duplicate change-log row)."""
    lw = (prev or {}).get("last_write") or {}
    return bool(lw.get("update_sha")) and lw.get("update_sha") == update_sha(update) and \
        (prev or {}).get("as_of") == str(run_date)[:10]


def apply_update(doc: dict, update: dict, run_date: str, writer: str = "vci_run") -> dict:
    """PURE: the next store state from the current one and a VCI run's update JSON (the
    update_vci_watchlist_TEMPLATE.json shape). Sections the update omits are KEPT, never blanked."""
    if not isinstance(update, dict):
        raise StoreError("ISA-0796: the update is not a JSON object")
    if "watchlist" not in update or not isinstance(update["watchlist"], list):
        raise StoreError("ISA-0796: the update has no `watchlist` list ([] is an answer; absence is not)")
    if not isinstance(update.get("change_log_entry"), dict) or not update["change_log_entry"].get("month"):
        raise StoreError("ISA-0796: every write records a change_log_entry with a `month` (R2.13)")
    try:
        datetime.date.fromisoformat(str(run_date)[:10])
    except ValueError:
        raise StoreError("ISA-0796: run_date %r is not YYYY-MM-DD" % run_date)
    rows = [normalise_row(x) for x in update["watchlist"]]
    seen = set()
    for r in rows:
        if r["ticker"] in seen:
            raise StoreError("ISA-0796: %s appears twice in the update" % r["ticker"])
        seen.add(r["ticker"])
    rows, dropped = _trim(rows)
    for i, r in enumerate(sorted(rows, key=lambda r: r.get("rank") or 99), 1):
        r["rank"] = i
    new = json.loads(json.dumps(doc or empty_doc()))
    before = [r["ticker"] for r in new.get("watchlist") or []]
    new["watchlist"] = sorted(rows, key=lambda r: r["rank"])
    for k in ("rescore_calendar", "excluded", "amba_monitor", "narrative_sections"):
        if k in update:
            if not isinstance(update[k], list):
                raise StoreError("ISA-0796: `%s` must be a list" % k)
            new[k] = update[k]
    if "nvidia_class_note" in update:
        new["nvidia_class_note"] = str(update["nvidia_class_note"] or "")
    entry = dict(update["change_log_entry"])
    if dropped:
        entry["notes"] = (str(entry.get("notes") or "") + " [max-5 trim by ACS dropped: %s]" % ", ".join(dropped)).strip()
    old_log = list(new.get("change_log") or [])
    # R5.14: a DIFFERENT update for the SAME run date and month is a same-run CORRECTION - it supersedes
    # that run's row (recorded in last_write), it does not add a second row for one occurrence.
    correction = bool(old_log) and new.get("as_of") == str(run_date)[:10] and \
        str(old_log[0].get("month")) == str(entry.get("month"))
    new["change_log"] = [entry] + (old_log[1:] if correction else old_log)
    after = [r["ticker"] for r in new["watchlist"]]
    new.update(schema_version=SCHEMA_VERSION, revision=int(new.get("revision") or 0) + 1,
               as_of=str(run_date)[:10], run_month=update.get("run_month") or new.get("run_month"),
               run_note=update.get("run_note", new.get("run_note", "")))
    new["last_write"] = {"writer": writer, "run_date": str(run_date)[:10],
                         "added": [t for t in after if t not in before],
                         "removed": [t for t in before if t not in after], "max5_dropped": dropped,
                         "update_sha": update_sha(update),
                         "correction_of_revision": int(doc.get("revision") or 0) if correction else None}
    return new


def _atomic(p: str, text: str) -> None:
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)


def write(root: str, doc: dict, *, writer: str = "vci_run", prev: dict = None) -> dict:
    """Persist the store, append the history line, re-render the view. -> the history line."""
    _fi_mark("vci_watchlist_store", "write")
    doc = dict(doc)
    doc["written_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    _atomic(store_path(root), json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    line = {"revision": doc.get("revision"), "written_at": doc["written_at"], "as_of": doc.get("as_of"),
            "writer": writer, "sha_prev": _sha_doc(prev) if prev else None, "sha_new": _sha_doc(doc),
            "members": [r["ticker"] for r in doc.get("watchlist") or []],
            **{k: (doc.get("last_write") or {}).get(k) for k in ("added", "removed", "max5_dropped")}}
    os.makedirs(os.path.dirname(history_path(root)), exist_ok=True)
    with open(history_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    _atomic(view_path(root), render_markdown(doc))
    return line


# ── the generated view ────────────────────────────────────────────────────────────────────────────────
def _cell(v) -> str:
    if v is None:
        return "—"
    return str(v).replace("|", "/").replace("\n", " ").strip()


def _fmt(x, nd=2):
    return "—" if x is None else (("%." + str(nd) + "f") % x if isinstance(x, float) else str(x))


_LABELS = {"ticker": "Ticker", "next_rescore": "Next Re-Score Trigger", "catalyst": "Catalyst to Watch",
           "reason": "Reason", "company": "Company", "acs_at_removal": "ACS at Removal",
           "nvidia_signals": "NVIDIA Signals", "current_price": "Current Price",
           "re_entry_trigger": "Re-Entry Trigger", "notes": "Notes", "month": "Month", "additions": "Additions",
           "removals": "Removals", "acs_changes": "ACS Changes"}


def _table(rows: list, order: list = None) -> str:
    if not rows:
        return "*None.*"
    cols = list(order or [])
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    cols = [c for c in cols if any(c in r for r in rows)]
    out = ["| " + " | ".join(_LABELS.get(c, c.replace("_", " ").title()) for c in cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def render_markdown(doc: dict) -> str:
    """The view. `lastUpdated:` is the VCI run date (sync_vci_watchlist.source_run_date reads it)."""
    wl = doc.get("watchlist") or []
    L = ["---",
         "name: ISA Asymmetric Watchlist — VCI Pipeline (GENERATED VIEW)",
         "description: GENERATED from Dashboard/state/vci_watchlist.json (ISA-0796) - do not edit; write via update_vci_watchlist.py",
         "lastUpdated: %s" % (doc.get("as_of") or ""),
         "store_revision: %s" % doc.get("revision"),
         "---", "",
         "> **GENERATED VIEW — do not edit.** Canonical store `Investment Analysis/Dashboard/state/vci_watchlist.json` "
         "(revision %s, written %s). Update only through `update_vci_watchlist.py --update <json>` (ISA-0796)."
         % (doc.get("revision"), doc.get("written_at") or "—"), "",
         "## Current Asymmetric Watchlist (%s run — %s%s)" % (doc.get("run_month") or "—", doc.get("as_of") or "—",
                                                            (" — " + doc["run_note"]) if doc.get("run_note") else ""),
         "",
         "| Rank | Ticker | Company | Exch | ACS | Source | asym / P25 | Floor | 3yr Pos | NVIDIA | Deploy? | Last Scored | Status |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in wl:
        acs = "UNMEASURED" if r.get("acs") is None else str(r["acs"])
        asym = "%s / %s" % (_fmt(r.get("asym")), _fmt(r.get("asym_p25")))
        L.append("| " + " | ".join(_cell(x) for x in (
            r.get("rank"), r["ticker"], r.get("company", ""), r.get("exchange", ""), acs,
            _fmt(r.get("source_score"), 1), asym, _fmt(r.get("floor")), r.get("three_yr_pos_pct", ""),
            r.get("nvidia_signals", ""), r.get("deploy", ""), r.get("last_scored", ""), r.get("status", ""))) + " |")
    for i in range(len(wl) + 1, MAX_NAMES + 1):
        L.append("| %d | *(vacant)* | — | — | — | — | — | — | — | — | — | — | — |" % i)
    L += ["", "## Notes on Current Rankings", ""]
    notes = ["**%s (#%s):** %s" % (r["ticker"], r.get("rank"), _cell(r.get("note")).replace("—", "").strip())
             for r in wl if str(r.get("note") or "").strip()]
    L += (notes or ["*No row notes.*"])
    for sec in doc.get("narrative_sections") or []:
        L += ["", "## %s" % sec.get("heading", "Notes"), "", str(sec.get("body_md") or "").rstrip()]
    L += ["", "## NVIDIA-Class Candidates (ACS >=85, >=5 signals)", "",
          doc.get("nvidia_class_note") or "*None recorded.*",
          "", "## Re-Score Calendar", "", _table(doc.get("rescore_calendar") or [], ["ticker", "next_rescore", "catalyst"]),
          "", "## Structurally Excluded (do not score)", "", _table(doc.get("excluded") or [], ["ticker", "reason"]),
          "", "## AMBA Re-Entry Monitor", "", _table(doc.get("amba_monitor") or [], ["ticker"]),
          "", "## Watchlist Change Log", "",
          _table(doc.get("change_log") or [], ["month", "additions", "removals", "acs_changes", "notes"]), ""]
    return "\n".join(L)


# ── one-time bootstrap from a legacy markdown watchlist ──────────────────────────────────────────────
def _norm_head(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def _md_tables_by_heading(md: str) -> dict:
    """{heading_text: {"tables": [[header, rows...]], "text": [non-table lines]}} for every '## ' section."""
    out, cur = {}, None
    for ln in md.splitlines():
        m = re.match(r"^##\s+(.*)$", ln)
        if m:
            cur = m.group(1).strip()
            out[cur] = {"tables": [], "text": []}
            continue
        if cur is None:
            continue
        sec = out[cur]
        if ln.strip().startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if re.match(r"^\|[\s\-\|:]+\|$", ln.strip()):
                continue
            if not sec["tables"] or sec.get("_closed"):
                sec["tables"].append([cells])
                sec["_closed"] = False
            else:
                sec["tables"][-1].append(cells)
        else:
            if sec["tables"]:
                sec["_closed"] = True
            sec["text"].append(ln)
    return out


def _dict_rows(table: list, keymap: dict = None) -> list:
    head = [(keymap or {}).get(_norm_head(h), _norm_head(h)) for h in table[0]]
    return [{head[i]: c for i, c in enumerate(row) if i < len(head)} for row in table[1:]]


def parse_legacy_markdown(md: str) -> dict:
    """Bootstrap a store doc from the legacy memory-file markdown. Every section it does not model is kept
    verbatim as a narrative section, so nothing in the legacy file is lost."""
    doc = empty_doc()
    m = re.search(r"^lastUpdated:\s*(\d{4}-\d{2}-\d{2})", md, re.MULTILINE)
    doc["as_of"] = m.group(1) if m else None
    secs = _md_tables_by_heading(md)
    wl_key = next((k for k in secs if "current asymmetric watchlist" in k.lower()), None)
    if wl_key is None or not secs[wl_key]["tables"]:
        raise StoreError("ISA-0796: no 'Current Asymmetric Watchlist' table in the legacy markdown")
    mm = re.search(r"\(([A-Za-z]+ \d{4}) run", wl_key)
    doc["run_month"] = mm.group(1) if mm else None
    rows = []
    km = {"exch": "exchange", "3yr_pos": "three_yr_pos_pct", "nvidia": "nvidia_signals", "source": "source_score",
          "deploy": "deploy", "acs": "acs"}
    for r in _dict_rows(secs[wl_key]["tables"][0], km):
        t = str(r.get("ticker", "")).replace("*", "").strip()
        if not TICKER_RE.match(t):
            continue                                                    # '(vacant)'
        a = str(r.get("asym_p25", ""))
        parts = [x.strip() for x in a.split("/")]
        row = {"rank": r.get("rank"), "ticker": t, "company": r.get("company", ""), "exchange": r.get("exchange", ""),
               "acs": _num(r.get("acs")), "source_score": _num(r.get("source_score")),
               "asym": _num(parts[0]) if parts else None, "asym_p25": _num(parts[1]) if len(parts) > 1 else None,
               "floor": _num(r.get("floor")), "three_yr_pos_pct": r.get("three_yr_pos_pct", ""),
               "nvidia_signals": r.get("nvidia_signals", ""), "deploy": r.get("deploy", ""),
               "status": r.get("status", ""), "last_scored": doc["as_of"] or ""}
        rows.append(normalise_row(row))
    doc["watchlist"] = rows
    used = {wl_key}
    tail = "\n".join(secs[wl_key]["text"]).strip()
    for k, v in secs.items():
        kl = k.lower()
        if "re-score calendar" in kl and v["tables"]:
            doc["rescore_calendar"] = _dict_rows(v["tables"][0], {"next_re_score_trigger": "next_rescore",
                                                                  "catalyst_to_watch": "catalyst"}); used.add(k)
        elif "structurally excluded" in kl and v["tables"]:
            doc["excluded"] = _dict_rows(v["tables"][0]); used.add(k)
        elif "re-entry monitor" in kl and v["tables"]:
            doc["amba_monitor"] = _dict_rows(v["tables"][0]); used.add(k)
        elif "change log" in kl and v["tables"]:
            doc["change_log"] = _dict_rows(v["tables"][0]); used.add(k)
        elif "nvidia-class" in kl:
            doc["nvidia_class_note"] = "\n".join(v["text"]).strip(); used.add(k)
        elif "how to use this file" in kl:
            used.add(k)                                                 # superseded by the view's own banner
    for k, v in secs.items():
        if k in used:
            continue
        body = []
        for tb in v["tables"]:
            body.append("\n".join("| " + " | ".join(r) + " |" for r in tb))
        body.append("\n".join(v["text"]).strip())
        doc["narrative_sections"].append({"heading": k, "body_md": "\n\n".join(b for b in body if b.strip())})
    if tail:
        doc["narrative_sections"].insert(0, {"heading": "Watchlist commentary (%s run)" % (doc["run_month"] or "last"),
                                             "body_md": tail})
    return doc


# ── selftest ──────────────────────────────────────────────────────────────────────────────────────────
_LEGACY_FIXTURE = """---
name: x
lastUpdated: 2026-09-13
---

## How to Use This File

- old

## HEADLINE (Sep run)

Nothing qualified.

## Current Asymmetric Watchlist (September 2026 run — 13 Sep 2026)

| Rank | Ticker | Company | Exch | Source | ACS | asym / P25 | Floor | 3yr Pos | NVIDIA | Deploy? | Status |
|------|--------|---------|------|--------|-----|-----------|-------|---------|--------|---------|--------|
| 1 | INFQ | Infleqtion | NYSE | 31.5 | **69** ▲1 | 2.93 / 2.25 | 2.00 | 39.0% | 4/6 | No (ACS<75) | Best asymmetry |
| 2 | IONQ | IonQ | NYSE | 24.2 | 73 = | 2.45 / 1.88 | 2.00 | 40.0% | 4/6 | No | Watch |
| 3 | *(vacant)* | — | — | — | — | — | — | — | — | — | — |

**Additions:** none.

## Re-Score Calendar

| Ticker | Next Re-Score Trigger | Catalyst to Watch |
|--------|-----------------------|-------------------|
| INFQ | End-2026 | 30 logical qubits |

## Structurally Excluded (do not score)

| Ticker | Reason |
|--------|--------|
| QUBT | DISCARD |

## AMBA Re-Entry Monitor

| Ticker | ACS at Removal | NVIDIA Signals | Re-Entry Trigger | Notes |
|--------|---------------|---------------|-----------------|-------|
| AMBA | 60 | 5/6 | $50-55 | Removed May |

## Watchlist Change Log

| Month | Additions | Removals | ACS Changes | Notes |
|-------|-----------|----------|-------------|-------|
| Sep-26 | none | QBTS | INFQ 68->69 | first |
"""


def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    fails = []

    def ok(name, cond, detail=""):
        if verbose or not cond:
            print(("  PASS " if cond else "  FAIL ") + name + (("  -- " + str(detail)[:300]) if not cond else ""))
        if not cond:
            fails.append(name)

    root = tempfile.mkdtemp(prefix="vci_store_")
    try:
        try:
            members(root)
            ok("MUST-FIRE: an absent store RAISES (UNKNOWN), never an empty watchlist", False)
        except StoreError as e:
            ok("MUST-FIRE: an absent store RAISES (UNKNOWN), never an empty watchlist", "ISA-0796" in str(e))
        doc = parse_legacy_markdown(_LEGACY_FIXTURE)
        ok("bootstrap: the legacy table yields the two real rows ('(vacant)' is not a row)",
           [r["ticker"] for r in doc["watchlist"]] == ["INFQ", "IONQ"], doc["watchlist"])
        ok("bootstrap: '**69** ▲1' parses to ACS 69 MEASURED; asym/P25 split; as_of from lastUpdated",
           doc["watchlist"][0]["acs"] == 69 and doc["watchlist"][0]["acs_state"] == "MEASURED"
           and doc["watchlist"][0]["asym_p25"] == 2.25 and doc["as_of"] == "2026-09-13", doc["watchlist"][0])
        ok("bootstrap: excluded / re-score / AMBA / change log are carried, and unmodelled sections are kept verbatim",
           doc["excluded"][0]["ticker"] == "QUBT" and doc["rescore_calendar"][0]["next_rescore"] == "End-2026"
           and doc["amba_monitor"][0]["ticker"] == "AMBA" and doc["change_log"][0]["month"] == "Sep-26"
           and any(s["heading"].startswith("HEADLINE") for s in doc["narrative_sections"])
           and any("Additions" in s["body_md"] for s in doc["narrative_sections"]), doc["narrative_sections"])
        doc["revision"] = 1
        write(root, doc, writer="bootstrap")
        ok("MUST-FIRE: after a write, members() reads the store", members(root) == ["INFQ", "IONQ"])
        import sys as _sys
        if HERE not in _sys.path:
            _sys.path.insert(0, HERE)
        try:
            import sync_vci_watchlist as _svw
            parsed = _svw.parse_vci_watchlist_md(view_path(root))
            ok("CONSUMER PARITY: sync_vci_watchlist parses the generated view to the same tickers and ACS, and "
               "reads its run date (ISA-0792 Step-5 source)",
               [(r["ticker"], r["acs_score"]) for r in parsed] == [("INFQ", 69), ("IONQ", 73)]
               and _svw.source_run_date(view_path(root)) == "2026-09-13", parsed)
        except ImportError as exc:
            ok("CONSUMER PARITY: sync_vci_watchlist importable", False, exc)
        upd = {"run_month": "October 2026", "run_note": "v1.13",
               "watchlist": [{"ticker": "INFQ", "acs": 71}, {"ticker": "NEWX", "acs": None, "company": "New"}],
               "change_log_entry": {"month": "Oct-26", "additions": "NEWX", "removals": "IONQ"}}
        prev = load(root)
        nxt = apply_update(prev, upd, "2026-10-11")
        ok("MUST-FIRE (ISA-0667): an unscored row is UNMEASURED with acs None - never a manufactured 0",
           nxt["watchlist"][1]["acs"] is None and nxt["watchlist"][1]["acs_state"] == "UNMEASURED")
        ok("NEGATIVE CONTROL: sections the update omits are KEPT (excluded, re-score, AMBA), change log PREPENDED",
           nxt["excluded"] == prev["excluded"] and nxt["amba_monitor"] == prev["amba_monitor"]
           and nxt["rescore_calendar"] == prev["rescore_calendar"]
           and [c["month"] for c in nxt["change_log"]][:2] == ["Oct-26", "Sep-26"])
        ok("the write records the machine diff (added NEWX, removed IONQ) and bumps the revision",
           nxt["last_write"]["added"] == ["NEWX"] and nxt["last_write"]["removed"] == ["IONQ"] and nxt["revision"] == 2)
        line = write(root, nxt, prev=prev)
        hist = [json.loads(x) for x in open(history_path(root), encoding="utf-8")]
        ok("history is APPEND-ONLY: two lines, the second chained to the first state's sha",
           len(hist) == 2 and hist[1]["sha_prev"] == _sha_doc(prev) and line["revision"] == 2, hist)
        view = open(view_path(root), encoding="utf-8").read()
        ok("view renders UNMEASURED (not 0) and lastUpdated = the run date", "| NEWX | New |" in view
           and "| UNMEASURED |" in view and "lastUpdated: 2026-10-11" in view)
        # ── R5.14 temporal matrix (the store's as_of is the VCI run date the Step-5 recency rule reads) ──
        ok("R5.14 T-1: before the run the store still carries the prior run date (2026-09-13 bootstrap)",
           prev["as_of"] == "2026-09-13")
        ok("R5.14 T: the run's update moves as_of to the run date it was applied for (2026-10-11)",
           nxt["as_of"] == "2026-10-11")
        ok("R5.14 repeat_same_occurrence: the IDENTICAL update for the same run date is a repeat - no new revision",
           is_repeat(load(root), upd, "2026-10-11"))
        _corr = dict(upd, change_log_entry={"month": "Oct-26", "notes": "corrected"})
        _c = apply_update(load(root), _corr, "2026-10-11")
        ok("R5.14 T+1: a corrected update for the SAME run REPLACES that run's change-log row (one row per occurrence)",
           [c["month"] for c in _c["change_log"]][:2] == ["Oct-26", "Sep-26"] and _c["last_write"]["correction_of_revision"] == 2
           and _c["change_log"][0].get("notes") == "corrected", _c["change_log"][:2])
        _n = apply_update(load(root), dict(upd, change_log_entry={"month": "Nov-26"}), "2026-11-08")
        ok("R5.14 next_occurrence: the next run's update PREPENDS a new row and is not a repeat",
           [c["month"] for c in _n["change_log"]][:3] == ["Nov-26", "Oct-26", "Sep-26"]
           and not is_repeat(load(root), dict(upd, change_log_entry={"month": "Nov-26"}), "2026-11-08"))
        six = dict(upd, watchlist=[{"ticker": "A%d" % i, "acs": 60 + i} for i in range(6)] + [{"ticker": "U1"}])
        t = apply_update(nxt, six, "2026-11-08")
        ok("MUST-FIRE: >5 names is trimmed by ACS with UNMEASURED last, and the trim is RECORDED (never silent)",
           [r["ticker"] for r in t["watchlist"]] == ["A1", "A2", "A3", "A4", "A5"]
           and set(t["last_write"]["max5_dropped"]) == {"A0", "U1"} and "max-5 trim" in t["change_log"][0]["notes"],
           t["last_write"])
        for bad, why in (({"watchlist": [{"ticker": "bad ticker"}], "change_log_entry": {"month": "x"}}, "ticker"),
                         ({"change_log_entry": {"month": "x"}}, "no watchlist"),
                         ({"watchlist": []}, "no change log"),
                         ({"watchlist": [{"ticker": "X", "acs": 140}], "change_log_entry": {"month": "x"}}, "acs range"),
                         ({"watchlist": [{"ticker": "X"}, {"ticker": "X"}], "change_log_entry": {"month": "x"}}, "dup")):
            try:
                apply_update(nxt, bad, "2026-10-11")
                ok("NEGATIVE CONTROL: a malformed update is REFUSED (%s)" % why, False)
            except StoreError:
                ok("NEGATIVE CONTROL: a malformed update is REFUSED (%s)" % why, True)
        with open(store_path(root), "w") as fh:
            fh.write("{not json")
        try:
            load(root)
            ok("MUST-FIRE: an unreadable store RAISES", False)
        except StoreError:
            ok("MUST-FIRE: an unreadable store RAISES", True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if verbose:
        print("vci_watchlist_store selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    if "--members" in sys.argv:
        print(json.dumps(members(HERE)))
