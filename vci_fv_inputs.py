"""
vci_fv_inputs.py — ISA-0771: ONE canonical writer for VCI structured fair-value (§10.2) inputs.

Authority: ChatGPT Astra 6 Audit/ISA_BuildSpec_VCI_Input_Population_Integrity_27Sep2026.md §10 (VCI-A),
validated against TB-2026-09-27-03. Binding standard: ISA_Engineering_Rules.md (R14.1, R4.4, R7.5, R2.13).

WHY. The structured inputs a modelled FV needs lived in `vci_fv_inputs.json`, a sidecar maintained BY
HAND. The Aug-2026 run told itself to append QBTS's inputs and it never happened, so the one name the sleeve
had bought fell silently to manual-confirm for a month - a refusal indistinguishable from a liquidity flag,
depending on someone remembering (R14.1). Nothing linked a deploy verdict to the input record it used.

DESIGN (BuildSpec §10 "prefer append-only canonical records with a generated current projection"):
  vci_fv_input_records.jsonl   APPEND-ONLY journal - one record per capture, each with a stable
                               `fv_input_id` (content-derived), schema version, security, valuation
                               as-of, method id, raw inputs, sources, PIT status, capture basis, author,
                               capture route and supersession link. Never edited, never deleted.
  vci_fv_inputs.json           the GENERATED current projection (latest record per ticker, same shape
                               existing consumers already read, plus `fv_input_id`). Hand edits are
                               detected (`projection_drift`) - regenerate, never hand-edit.
  evaluate_candidate()         resolves `fv_input_id` (explicitly, or by exact content match); a modelled
                               FV with no lineage-bearing record is the named refusal
                               MISSING_STRUCTURED_FV_INPUT (vci_deploy_eval).
Historical hand-entered values are migrated ONCE with capture_basis HAND_ENTERED_SIDECAR and pit_status
BACKFILLED_FROM_SIDECAR - their true basis; they never masquerade as contemporaneous captures (R7.5).
Mutable decision data, not source: the journal/projection are DATA; the run receipt records the content
actually consumed (fv_input_id), so capturing an input never forces a new Trusted build.

CLI: python3 vci_fv_inputs.py record --ticker QBTS --as-of 2026-10-11 --author "VCI run 11-Oct" \\
         --json '{"latent_tam_usd_bn":60,"capture_share":0.1,"steady_margin":0.35,"exit_multiple":8,
                  "fully_diluted_shares":372600000,"fx_to_local":1.0}' \\
         [--asset-structure platform --catalyst-type X --catalyst-domain Y --source "10-Q Q2-26"]
     python3 vci_fv_inputs.py show [--ticker T] | check | backfill | --selftest
Learning paradigm: NONE (lineage capture). Irreversible capture: every record is kept for calibration.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "1.0.0"
JOURNAL = "vci_fv_input_records.jsonl"
PROJECTION = "vci_fv_inputs.json"
METHOD_ID = "bottleneck_fv.s10_2.v2"
REQUIRED_INPUTS = ("latent_tam_usd_bn", "capture_share", "steady_margin", "exit_multiple",
                   "fully_diluted_shares", "fx_to_local")
CAPTURED_LIVE, BACKFILLED = "CAPTURED_LIVE", "BACKFILLED_FROM_SIDECAR"
LEGACY_KEYS = ("win_case_fv_usd", "price_asof", "asymmetry_point", "asymmetry_p25", "status", "note",
               "fv_rederived")
PRESENT, MISSING = "PRESENT", "MISSING_STRUCTURED_FV_INPUT"


class FVInputError(ValueError):
    """A structured-input contract breach. Raised at the writer, never stored (R4.7)."""


def _p(root, name):
    return os.path.join(root or HERE, name)


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _canon_inputs(fv_inputs: dict) -> dict:
    return {k: float(fv_inputs[k]) for k in REQUIRED_INPUTS}


def validate_inputs(fv_inputs) -> list:
    """[] when the §10.2 dict is complete and admissible."""
    if not isinstance(fv_inputs, dict):
        return ["fv_inputs must be a dict"]
    errs = []
    for k in REQUIRED_INPUTS:
        v = fv_inputs.get(k)
        try:
            f = float(v)
        except (TypeError, ValueError):
            errs.append("%s missing/non-numeric (%r)" % (k, v))
            continue
        if f <= 0:
            errs.append("%s must be > 0 (%r)" % (k, v))
        if k in ("capture_share", "steady_margin") and f > 1:
            errs.append("%s is a fraction and must be <= 1 (%r)" % (k, v))
    return errs


def make_id(ticker: str, fv_inputs: dict, valuation_as_of, method_id: str = METHOD_ID) -> str:
    body = json.dumps({"ticker": str(ticker).upper(), "fv_inputs": _canon_inputs(fv_inputs),
                       "valuation_as_of": valuation_as_of, "method_id": method_id}, sort_keys=True)
    return "FVI-%s-%s" % (str(ticker).upper(), hashlib.sha256(body.encode("utf-8")).hexdigest()[:12])


def read_journal(root: str = None) -> list:
    p = _p(root, JOURNAL)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"kind": "CORRUPT_LINE", "line": i})
    return out


def _append(rec: dict, root: str = None) -> None:
    with open(_p(root, JOURNAL), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")


def current_records(journal: list) -> dict:
    """ticker -> latest record (journal order is capture order)."""
    cur = {}
    for r in journal:
        if r.get("kind") == "FV_INPUT":
            cur[r["ticker"]] = r
    return cur


def render_projection(journal: list, meta: dict = None) -> dict:
    """THE projection: generated, never hand-maintained. Same per-ticker shape the existing readers use."""
    doc = {"_meta": dict(meta or {}, generated_by="vci_fv_inputs.render_projection (ISA-0771)",
                         journal=JOURNAL, schema_version=SCHEMA_VERSION,
                         rule="GENERATED - do not hand-edit; write through vci_fv_inputs.record()")}
    for t, r in sorted(current_records(journal).items()):
        e = {"fv_input_id": r["fv_input_id"], "fv_inputs": r["fv_inputs"],
             "asset_structure": r.get("asset_structure"), "catalyst_type": r.get("catalyst_type"),
             "catalyst_domain": r.get("catalyst_domain"), "valuation_as_of": r.get("valuation_as_of"),
             "capture_basis": r.get("capture_basis"), "pit_status": r.get("pit_status")}
        e.update({k: v for k, v in (r.get("legacy_fields") or {}).items() if k not in e})
        doc[t] = e
    return doc


def _write_projection(journal: list, root: str = None) -> dict:
    p = _p(root, PROJECTION)
    meta = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                meta = {k: v for k, v in (json.load(fh).get("_meta") or {}).items()
                        if k not in ("generated_by", "journal", "schema_version", "rule")}
        except Exception:                                               # noqa: BLE001
            meta = {}
    doc = render_projection(journal, meta)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)
    return doc


def record(ticker: str, fv_inputs: dict, *, valuation_as_of: str, author: str,
           capture_route: str = "vci_fv_inputs.record", method_id: str = METHOD_ID,
           asset_structure=None, catalyst_type=None, catalyst_domain=None, source_ids=None,
           pit_status: str = CAPTURED_LIVE, capture_basis: str = "CAPTURED_AT_DECISION",
           legacy_fields: dict = None, note: str = None, root: str = None) -> dict:
    """THE writer. Validates, appends one immutable record, regenerates the projection. Idempotent on
    identical content (same id is not re-appended). RAISES on an incomplete record."""
    t = str(ticker or "").upper().strip()
    if not t:
        raise FVInputError("ticker required")
    errs = validate_inputs(fv_inputs)
    if errs:
        raise FVInputError("%s: %s" % (t, "; ".join(errs)))
    if not valuation_as_of:
        raise FVInputError("%s: valuation_as_of required (when was this valuation formed?)" % t)
    if not author:
        raise FVInputError("%s: author required" % t)
    j = read_journal(root)
    fid = make_id(t, fv_inputs, valuation_as_of, method_id)
    cur = current_records(j).get(t)
    if cur and cur.get("fv_input_id") == fid:
        return cur
    rec = {"kind": "FV_INPUT", "schema_version": SCHEMA_VERSION, "fv_input_id": fid, "ticker": t,
           "fv_inputs": _canon_inputs(fv_inputs), "valuation_as_of": valuation_as_of,
           "method_id": method_id, "asset_structure": asset_structure, "catalyst_type": catalyst_type,
           "catalyst_domain": catalyst_domain, "source_ids": list(source_ids or []),
           "pit_status": pit_status, "capture_basis": capture_basis, "author": author,
           "capture_route": capture_route, "recorded_at": _now_iso(),
           "supersedes": cur.get("fv_input_id") if cur else None, "note": note,
           "legacy_fields": dict(legacy_fields or {})}
    _append(rec, root)
    j.append(rec)
    _write_projection(j, root)
    return rec


def get(fv_input_id: str, root: str = None):
    for r in read_journal(root):
        if r.get("kind") == "FV_INPUT" and r.get("fv_input_id") == fv_input_id:
            return r
    return None


def lookup_id(ticker: str, fv_inputs: dict, root: str = None):
    """The fv_input_id of a journal record for `ticker` whose inputs equal `fv_inputs` exactly
    (numerically), preferring the current one. None when the dict has no lineage."""
    if validate_inputs(fv_inputs):
        return None
    t = str(ticker or "").upper().split(".")[0] if ticker else ""
    want = _canon_inputs(fv_inputs)
    hits = [r for r in read_journal(root) if r.get("kind") == "FV_INPUT"
            and r.get("ticker") in (str(ticker or "").upper(), t) and r.get("fv_inputs") == want]
    return hits[-1]["fv_input_id"] if hits else None


def resolve(ticker, fv_inputs=None, fv_input_id=None, root: str = None) -> dict:
    """-> {"state": PRESENT|MISSING_STRUCTURED_FV_INPUT, "fv_input_id", "fv_inputs", "why"}.
    An explicit id is AUTHORITATIVE (its record's inputs are what is consumed); a supplied dict must
    match it. A dict with no journal record has no lineage and is refused by name."""
    try:
        if fv_input_id:
            r = get(fv_input_id, root)
            if r is None:
                return {"state": MISSING, "fv_input_id": None, "fv_inputs": None,
                        "why": "fv_input_id %s not in %s" % (fv_input_id, JOURNAL)}
            if fv_inputs is not None and not validate_inputs(fv_inputs) and _canon_inputs(fv_inputs) != r["fv_inputs"]:
                return {"state": MISSING, "fv_input_id": None, "fv_inputs": None,
                        "why": "supplied fv_inputs CONFLICT with record %s" % fv_input_id}
            return {"state": PRESENT, "fv_input_id": fv_input_id, "fv_inputs": r["fv_inputs"],
                    "why": "explicit id"}
        if fv_inputs:
            fid = lookup_id(ticker, fv_inputs, root)
            if fid:
                return {"state": PRESENT, "fv_input_id": fid, "fv_inputs": _canon_inputs(fv_inputs),
                        "why": "content match"}
            return {"state": MISSING, "fv_input_id": None, "fv_inputs": fv_inputs,
                    "why": "fv_inputs supplied with no journal record (unlineaged hand dict) - record "
                           "them through vci_fv_inputs.record() first"}
        return {"state": MISSING, "fv_input_id": None, "fv_inputs": None, "why": "no structured inputs"}
    except Exception as exc:                                            # noqa: BLE001
        return {"state": MISSING, "fv_input_id": None, "fv_inputs": None,
                "why": "lineage store unreadable (%s: %s)" % (type(exc).__name__, exc)}


def backfill(root: str = None, today: str = None) -> dict:
    """ONE-TIME migration of the hand-maintained sidecar into the journal, labelled with its TRUE basis.
    Idempotent: a ticker already journalled is skipped. Never rewrites history as contemporaneous."""
    p = _p(root, PROJECTION)
    with open(p, encoding="utf-8") as fh:
        side = json.load(fh)
    j = read_journal(root)
    have = set(current_records(j))
    meta = side.get("_meta") or {}
    done, skipped = [], []
    for t, e in side.items():
        if t.startswith("_") or not isinstance(e, dict) or t.upper() in have:
            skipped.append(t)
            continue
        asof = e.get("fv_rederived") or (str(meta.get("prices_asof") or "")[:10] or None) or "UNKNOWN"
        rec = {"kind": "FV_INPUT", "schema_version": SCHEMA_VERSION,
               "fv_input_id": make_id(t, e["fv_inputs"], asof), "ticker": t.upper(),
               "fv_inputs": _canon_inputs(e["fv_inputs"]), "valuation_as_of": asof,
               "method_id": METHOD_ID, "asset_structure": e.get("asset_structure"),
               "catalyst_type": e.get("catalyst_type"), "catalyst_domain": e.get("catalyst_domain"),
               "source_ids": ["vci_fv_inputs.json sidecar (hand-entered)"],
               "pit_status": BACKFILLED, "capture_basis": "HAND_ENTERED_SIDECAR",
               "author": "UNKNOWN - hand-entered sidecar (migrated by ISA-0771, %s)" % (today or _now_iso()[:10]),
               "capture_route": "vci_fv_inputs.backfill", "recorded_at": _now_iso(), "supersedes": None,
               "note": "valuation_as_of from the sidecar's own fv_rederived/prices_asof, else UNKNOWN",
               "legacy_fields": {k: e[k] for k in LEGACY_KEYS if k in e}}
        _append(rec, root)
        j.append(rec)
        done.append(t)
    doc = _write_projection(j, root)
    return {"migrated": done, "skipped": skipped, "n_projection": len([k for k in doc if not k.startswith("_")])}


def projection_drift(root: str = None) -> list:
    """[] when vci_fv_inputs.json equals the render of the journal (hand edits fire)."""
    p = _p(root, PROJECTION)
    j = read_journal(root)
    if not j:
        return ["ISA-0771: %s has no journal (%s) - the projection has no canonical writer yet" % (PROJECTION, JOURNAL)]
    try:
        with open(p, encoding="utf-8") as fh:
            disk = json.load(fh)
    except Exception as exc:                                            # noqa: BLE001
        return ["ISA-0771: %s unreadable (%s)" % (PROJECTION, exc)]
    want = render_projection(j, {k: v for k, v in (disk.get("_meta") or {}).items()
                                 if k not in ("generated_by", "journal", "schema_version", "rule")})
    bad = sorted(k for k in set(want) | set(disk) if k != "_meta" and want.get(k) != disk.get(k))
    corrupt = [r for r in j if r.get("kind") == "CORRUPT_LINE"]
    out = []
    if bad:
        out.append("ISA-0771: %s differs from its journal for %s - hand-edited projection; write through "
                   "vci_fv_inputs.record()" % (PROJECTION, bad))
    if corrupt:
        out.append("ISA-0771: %s has %d corrupt line(s)" % (JOURNAL, len(corrupt)))
    return out


def _selftest(verbose: bool = True) -> int:
    import tempfile
    import shutil
    n = [0]

    def ok(c, m):
        assert c, m
        n[0] += 1
    tmp = tempfile.mkdtemp(prefix="fvi_")
    try:
        Q = {"latent_tam_usd_bn": 60.0, "capture_share": 0.10, "steady_margin": 0.35, "exit_multiple": 8.0,
             "fully_diluted_shares": 372.6e6, "fx_to_local": 1.0}
        side = {"_meta": {"prices_asof": "2026-07-06 (yfinance)"},
                "ABCL": {"asset_structure": "platform", "fv_inputs": dict(Q, latent_tam_usd_bn=22.0), "note": "n",
                         "fv_rederived": "2026-07-06", "win_case_fv_usd": 20.17},
                "ALAB": {"asset_structure": "platform", "fv_inputs": dict(Q), "status": "RETIRED"}}
        with open(os.path.join(tmp, PROJECTION), "w") as fh:
            json.dump(side, fh)
        ok(projection_drift(tmp) and "no journal" in projection_drift(tmp)[0], "D0 pre-migration state is named")
        b = backfill(tmp, today="2026-09-27")
        ok(sorted(b["migrated"]) == ["ABCL", "ALAB"], "B1 sidecar migrated")
        ok(backfill(tmp)["migrated"] == [], "B2 idempotent")
        j = read_journal(tmp)
        ok(all(r["pit_status"] == BACKFILLED and r["capture_basis"] == "HAND_ENTERED_SIDECAR" for r in j),
           "B3 NEGATIVE CONTROL: a backfilled value can never read as captured live (R7.5)")
        ok(j[0]["valuation_as_of"] == "2026-07-06" and j[1]["valuation_as_of"] == "2026-07-06", "B4 as-of from the sidecar")
        ok(projection_drift(tmp) == [], "D1 generated projection is clean")
        proj = json.load(open(os.path.join(tmp, PROJECTION)))
        ok(proj["ALAB"]["status"] == "RETIRED" and proj["ABCL"]["note"] == "n", "B5 legacy fields preserved")
        # the writer
        r1 = record("qbts", Q, valuation_as_of="2026-09-13", author="fixture", root=tmp,
                    asset_structure="platform", source_ids=["10-Q"])
        ok(r1["fv_input_id"].startswith("FVI-QBTS-") and r1["pit_status"] == CAPTURED_LIVE, "W1 record")
        ok(record("QBTS", Q, valuation_as_of="2026-09-13", author="fixture", root=tmp)["fv_input_id"]
           == r1["fv_input_id"] and len(read_journal(tmp)) == 3, "W2 identical content is idempotent")
        r2 = record("QBTS", dict(Q, exit_multiple=9.0), valuation_as_of="2026-10-11", author="fixture", root=tmp)
        ok(r2["supersedes"] == r1["fv_input_id"], "W3 supersession is linked, history kept")
        ok(get(r1["fv_input_id"], tmp) is not None, "W4 superseded record still retrievable (append-only)")
        for bad in ({}, dict(Q, capture_share=1.5), dict(Q, fully_diluted_shares=None)):
            try:
                record("X", bad, valuation_as_of="2026-09-27", author="a", root=tmp)
                ok(False, "W5 must raise")
            except FVInputError:
                ok(True, "W5 incomplete input refused at the writer")
        # resolution
        ok(resolve("QBTS", fv_inputs=dict(Q, exit_multiple=9.0), root=tmp)["state"] == PRESENT, "R1 content match")
        ok(resolve("QBTS", fv_input_id=r1["fv_input_id"], root=tmp)["fv_inputs"]["exit_multiple"] == 8.0,
           "R2 an explicit id is authoritative")
        ok(resolve("QBTS", fv_inputs=dict(Q, exit_multiple=99.0), root=tmp)["state"] == MISSING,
           "R3 MUST-FIRE: an unlineaged hand dict is MISSING_STRUCTURED_FV_INPUT")
        ok(resolve("QBTS", fv_inputs=dict(Q, exit_multiple=99.0), fv_input_id=r1["fv_input_id"], root=tmp)["state"]
           == MISSING, "R4 id/dict conflict refuses")
        ok(resolve("NONE", root=tmp)["state"] == MISSING, "R5 nothing supplied -> MISSING")
        # hand edit of the projection fires
        proj = json.load(open(os.path.join(tmp, PROJECTION)))
        proj["QBTS"]["fv_inputs"]["exit_multiple"] = 12.0
        json.dump(proj, open(os.path.join(tmp, PROJECTION), "w"))
        ok(projection_drift(tmp) and "hand-edited" in projection_drift(tmp)[0],
           "D2 NEGATIVE CONTROL: a hand-edited projection fires")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print("vci_fv_inputs selftest: %d checks PASS" % n[0])
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def opt(k, d=None):
        return argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else d
    if "--selftest" in argv:
        return _selftest()
    cmd = argv[0] if argv else ""
    if cmd == "record":
        r = record(opt("--ticker"), json.loads(opt("--json") or "{}"), valuation_as_of=opt("--as-of"),
                   author=opt("--author"), asset_structure=opt("--asset-structure"),
                   catalyst_type=opt("--catalyst-type"), catalyst_domain=opt("--catalyst-domain"),
                   source_ids=[s for s in [opt("--source")] if s], capture_route="vci_fv_inputs CLI",
                   note=opt("--note"))
        print(json.dumps({"fv_input_id": r["fv_input_id"], "ticker": r["ticker"],
                          "supersedes": r.get("supersedes")}, indent=1))
        return 0
    if cmd == "show":
        cur = current_records(read_journal())
        t = opt("--ticker")
        print(json.dumps(cur.get(str(t).upper()) if t else sorted(cur), indent=1))
        return 0
    if cmd == "check":
        d = projection_drift()
        print("\n".join(d) or "OK: projection == journal render")
        return 1 if d else 0
    if cmd == "backfill":
        print(json.dumps(backfill(), indent=1))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
